"""
Paper Trading — Service Entry Point

Wires signal_router, strategy_builder, span_calculator, and engine together
into a standalone always-running service. See
docs/PAPER_TRADING_DESIGN.md section 9 for the full spec.
"""
from __future__ import annotations

import gc
import os
import queue
import signal
import sys
import time
from datetime import date, datetime, time as dtime

import common.constants as constant
from lib.logging_util import get_logger
logger = get_logger("paper-trading")
from lib.notification.Notification import TELEGRAM_NOTIFICATIONS
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from services.paper_trading import engine, ledger
from services.paper_trading.models import (
    ACCOUNT_KEY,
    CONFIG_KEY,
    COOLDOWN_TTL_SECONDS,
    POSITIONS_OPEN_KEY,
    TRADES_STREAM,
    TRADES_STREAM_MAXLEN,
    PaperAccount,
    PaperPosition,
    cooldown_key,
    daily_pnl_key,
    positions_closed_key,
)
from services.paper_trading.signal_router import (
    check_entry_filters,
    parse_analysis_result,
    parse_confluence_message,
)
from services.paper_trading.span_calculator import (
    SpanCalculator,
    build_instruments_cache,
    load_instruments_cache,
    save_instruments_cache,
)
from services.paper_trading.strategy_builder import build_position

ANALYSIS_GROUP = "paper-trader"
CONFLUENCE_GROUP = "paper-trader-confluence"
COMMANDS_STREAM = "paper:commands"
COMMANDS_GROUP = "paper-trader-cmd"
CONSUMER_NAME = "paper-trader-1"

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
MTM_CYCLE_SECONDS = 3

_running = True
entry_queue: "queue.Queue" = queue.Queue()
exit_queue: "queue.Queue" = queue.Queue()


def signal_handler(signum, frame):
    global _running
    logger.info("[paper-trading] Received signal %s, shutting down...", signum)
    _running = False


def is_market_hours(now: datetime) -> bool:
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ── Account / position persistence ──────────────────────────────────────────

def get_account(redis) -> PaperAccount:
    return PaperAccount.from_redis_mapping(redis.hgetall(ACCOUNT_KEY))


def save_account(redis, account: PaperAccount) -> None:
    account.available_margin = account.capital - account.margin_used
    redis.hset(ACCOUNT_KEY, mapping=account.to_redis_mapping())


def load_open_positions(redis) -> list[PaperPosition]:
    positions = []
    for raw in redis.hgetall(POSITIONS_OPEN_KEY).values():
        try:
            positions.append(PaperPosition.from_json(raw))
        except Exception as e:
            logger.error("[paper-trading] Malformed position in %s: %s", POSITIONS_OPEN_KEY, e, exc_info=True)
    return positions


def persist_new_position(redis, position: PaperPosition) -> None:
    redis.hset(POSITIONS_OPEN_KEY, mapping={position.position_id: position.to_json()})
    redis.set_with_ttl(cooldown_key(position.symbol, position.strategy), "1", ex=COOLDOWN_TTL_SECONDS)

    account = get_account(redis)
    account.margin_used += position.margin_blocked
    account.open_positions += 1
    save_account(redis, account)

    TELEGRAM_NOTIFICATIONS.send_live_options_notification(
        format_entry_notification(position), parse_mode="HTML", symbol=position.symbol
    )
    logger.info("[paper-trading] Opened %s %s credit=₹%.0f margin=₹%.0f",
                position.symbol, position.strategy, position.entry_credit, position.margin_blocked)


def persist_closed_position(redis, position: PaperPosition) -> None:
    today = date.today().isoformat()

    redis.hdel(POSITIONS_OPEN_KEY, position.position_id)
    redis.hset(positions_closed_key(today), mapping={position.position_id: position.to_json()})
    redis.xadd(TRADES_STREAM, {
        "position_id": position.position_id,
        "symbol": position.symbol,
        "strategy": position.strategy,
        "signal_source": position.signal_source,
        "entry_credit": str(position.entry_credit),
        "exit_premium": str(position.exit_premium),
        "pnl": str(position.pnl),
        "exit_reason": position.exit_reason or "",
        "timestamp": str(position.exit_timestamp),
    }, maxlen=TRADES_STREAM_MAXLEN)

    account = get_account(redis)
    pnl = position.pnl or 0.0
    account.realized_pnl += pnl
    account.daily_realized_pnl += pnl
    account.margin_used = max(0.0, account.margin_used - position.margin_blocked)
    account.open_positions = max(0, account.open_positions - 1)
    account.daily_trades += 1
    if pnl >= 0:
        account.daily_wins += 1
    else:
        account.daily_losses += 1
    save_account(redis, account)

    pnl_key = daily_pnl_key(today)
    redis.hset(pnl_key, mapping={
        "realized": str(account.daily_realized_pnl),
        "trades_count": str(account.daily_trades),
        "wins": str(account.daily_wins),
        "losses": str(account.daily_losses),
    })

    ledger.insert_trade(position)

    TELEGRAM_NOTIFICATIONS.send_live_options_notification(
        format_exit_notification(position), parse_mode="HTML", symbol=position.symbol
    )
    logger.info("[paper-trading] Closed %s %s reason=%s pnl=₹%.0f",
                position.symbol, position.strategy, position.exit_reason, pnl)


def format_entry_notification(position: PaperPosition) -> str:
    legs_str = "\n".join(
        f"{leg.side} {leg.option_type} {leg.strike:.0f} @ ₹{leg.entry_premium:.2f}"
        for leg in position.legs
    )
    return (
        f"📋 <b>PAPER TRADE OPENED</b>\n"
        f"Symbol: {position.symbol} | Strategy: {position.strategy} | Mode: {position.mode}\n"
        f"{legs_str}\n"
        f"Credit: ₹{position.entry_credit:.0f} | Margin: ₹{position.margin_blocked:.0f}\n"
        f"Signal: {position.signal_source}"
    )


def format_exit_notification(position: PaperPosition) -> str:
    pnl = position.pnl or 0.0
    icon = "✅" if pnl >= 0 else "❌"
    return (
        f"{icon} <b>PAPER TRADE CLOSED</b>\n"
        f"Symbol: {position.symbol} | Strategy: {position.strategy}\n"
        f"Entry credit: ₹{position.entry_credit:.0f} | Exit debit: ₹{(position.exit_premium or 0):.0f}\n"
        f"P&L: ₹{pnl:+.0f}\n"
        f"Exit reason: {position.exit_reason}"
    )


# ── Instruments cache (docs/PAPER_TRADING_DESIGN.md section 4.5) ───────────

def try_load_instruments(redis) -> "dict | None":
    """One attempt at fetching + caching Zerodha instruments. None on failure."""
    enctoken = redis.hget("auth:zerodha", "enctoken")
    if not enctoken:
        return None
    try:
        from lib.zerodha.zerodha_connect import KiteConnect
        kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA, enctoken=enctoken)
        raw_instruments = kc.instruments()
    except Exception as e:
        logger.error("[paper-trading] Instruments fetch failed: %s", e, exc_info=True)
        return None

    cache = build_instruments_cache(raw_instruments, symbols=constant.LIVE_OPTIONS_INDICES)
    if cache:
        save_instruments_cache(redis, cache)
    return cache


def wait_for_instruments(redis, retry_seconds: int = 30) -> dict:
    cache = load_instruments_cache(redis, constant.LIVE_OPTIONS_INDICES)
    if cache:
        return cache
    while _running:
        cache = try_load_instruments(redis)
        if cache:
            return cache
        logger.warning("[paper-trading] Waiting for valid enctoken to fetch instruments...")
        time.sleep(retry_seconds)
    return {}


# ── Threads ──────────────────────────────────────────────────────────────

def analysis_consumer(redis):
    try:
        redis.xgroup_create(ANALYSIS_GROUP, constant.ANALYSIS_RESULTS_STREAM, mkstream=True)
    except Exception as e:
        logger.debug("[paper-trading] xgroup_create %s: %s", ANALYSIS_GROUP, e)

    while _running:
        try:
            messages = redis.xreadgroup(
                ANALYSIS_GROUP, CONSUMER_NAME,
                {constant.ANALYSIS_RESULTS_STREAM: ">"}, count=10, block=5000,
            )
        except Exception as e:
            logger.error("[paper-trading] analysis consumer error: %s", e, exc_info=True)
            time.sleep(2)
            continue
        if not messages:
            continue
        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            try:
                new_entries, new_exits = parse_analysis_result(fields)
                for e in new_entries:
                    entry_queue.put(e)
                for x in new_exits:
                    exit_queue.put(x)
            except Exception as e:
                logger.exception("[paper-trading] Error parsing analysis result %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(constant.ANALYSIS_RESULTS_STREAM, ANALYSIS_GROUP, msg_id)
                except Exception as e:
                    logger.debug("[paper-trading] xack analysis %s: %s", msg_id, e)


def confluence_consumer(redis):
    try:
        redis.xgroup_create(CONFLUENCE_GROUP, constant.CONFLUENCE_STREAM, mkstream=True)
    except Exception as e:
        logger.debug("[paper-trading] xgroup_create %s: %s", CONFLUENCE_GROUP, e)

    while _running:
        try:
            messages = redis.xreadgroup(
                CONFLUENCE_GROUP, CONSUMER_NAME,
                {constant.CONFLUENCE_STREAM: ">"}, count=10, block=5000,
            )
        except Exception as e:
            logger.error("[paper-trading] confluence consumer error: %s", e, exc_info=True)
            time.sleep(2)
            continue
        if not messages:
            continue
        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            try:
                signal = parse_confluence_message(fields)
                if signal:
                    entry_queue.put(signal)
            except Exception as e:
                logger.exception("[paper-trading] Error parsing confluence %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(constant.CONFLUENCE_STREAM, CONFLUENCE_GROUP, msg_id)
                except Exception as e:
                    logger.debug("[paper-trading] xack confluence %s: %s", msg_id, e)


def _handle_exit_signal(redis, signal) -> None:
    for position in load_open_positions(redis):
        if position.symbol != signal.symbol:
            continue
        if signal.position_id and position.position_id != signal.position_id:
            continue
        options_live = redis.hgetall(f"data:options_live:{position.symbol}")
        if options_live:
            engine.update_leg_premiums(position, options_live)
        current_debit = engine.compute_current_debit(position)
        engine.close_position(position, signal.reason, current_debit, now=time.time())
        persist_closed_position(redis, position)


def _handle_entry_signal(redis, span_calculator: SpanCalculator, signal) -> None:
    account = get_account(redis)
    open_positions = load_open_positions(redis)

    passed, reason = check_entry_filters(signal, redis, account, open_positions)
    if not passed:
        logger.debug("[paper-trading] Entry rejected for %s/%s: %s", signal.symbol, signal.strategy, reason)
        return

    # signal.mode comes straight from the analysis:results message for
    # composite setups (worker.py now echoes the job's actual intraday/
    # positional mode back onto the result -- see signal_router.py) or
    # defaults to "intraday" for CONFLUENCE signals, which can only ever
    # fire during market hours anyway (LIVE/INTRADAY layers don't exist
    # outside 09:15-15:30).
    position = build_position(signal, redis, span_calculator, account, mode=signal.mode)
    if position is None:
        return
    persist_new_position(redis, position)


def strategy_processor(redis, span_calculator: SpanCalculator):
    while _running:
        drained = False
        try:
            signal = exit_queue.get_nowait()
            _handle_exit_signal(redis, signal)
            drained = True
        except queue.Empty:
            pass
        try:
            signal = entry_queue.get_nowait()
            _handle_entry_signal(redis, span_calculator, signal)
            drained = True
        except queue.Empty:
            pass
        if not drained:
            time.sleep(0.5)


def mtm_engine(redis):
    while _running:
        now = datetime.now()
        if not is_market_hours(now):
            time.sleep(MTM_CYCLE_SECONDS)
            continue

        gamma_trap_checked: dict[str, bool] = {}
        total_unrealized = 0.0

        for position in load_open_positions(redis):
            if position.symbol not in gamma_trap_checked:
                spot_tick = redis.hgetall(f"data:tick:{position.symbol}")
                gamma_trap_checked[position.symbol] = engine.check_gamma_trap_proxy(spot_tick)

            result = engine.evaluate_position(
                position, redis, now=now,
                gamma_trap_triggered=gamma_trap_checked[position.symbol],
            )
            if result is None:
                continue
            if result.status == "CLOSED":
                persist_closed_position(redis, result)
            else:
                redis.hset(POSITIONS_OPEN_KEY, mapping={result.position_id: result.to_json()})
                total_unrealized += engine.compute_unrealized_pnl(
                    result, engine.compute_current_debit(result)
                )

        account = get_account(redis)
        account.unrealized_pnl = total_unrealized
        save_account(redis, account)

        time.sleep(MTM_CYCLE_SECONDS)


def command_listener(redis):
    try:
        redis.xgroup_create(COMMANDS_GROUP, COMMANDS_STREAM, mkstream=True)
    except Exception as e:
        logger.debug("[paper-trading] xgroup_create %s: %s", COMMANDS_GROUP, e)

    while _running:
        try:
            messages = redis.xreadgroup(
                COMMANDS_GROUP, CONSUMER_NAME,
                {COMMANDS_STREAM: ">"}, count=10, block=5000,
            )
        except Exception as e:
            logger.error("[paper-trading] command listener error: %s", e, exc_info=True)
            time.sleep(2)
            continue
        if not messages:
            continue
        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            try:
                _handle_command(redis, fields)
            except Exception as e:
                logger.exception("[paper-trading] Error handling command %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(COMMANDS_STREAM, COMMANDS_GROUP, msg_id)
                except Exception as e:
                    logger.debug("[paper-trading] xack command %s: %s", msg_id, e)


def _handle_command(redis, fields: dict) -> None:
    command = fields.get("command", "")
    if command == "close":
        target = fields.get("position_id") or "all"
        for position in load_open_positions(redis):
            if target != "all" and position.position_id != target:
                continue
            options_live = redis.hgetall(f"data:options_live:{position.symbol}")
            if options_live:
                engine.update_leg_premiums(position, options_live)
            current_debit = engine.compute_current_debit(position)
            engine.close_position(position, "MANUAL", current_debit, now=time.time())
            persist_closed_position(redis, position)
    elif command == "reset":
        for position in load_open_positions(redis):
            redis.hdel(POSITIONS_OPEN_KEY, position.position_id)
        save_account(redis, PaperAccount())
    elif command == "config_set":
        key, value = fields.get("key"), fields.get("value")
        if key and value is not None:
            redis.hset(CONFIG_KEY, mapping={key: value})


def update_heartbeat(redis) -> None:
    redis.hset("service:registry:paper-trading", mapping={
        "name": "paper-trading",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    })
    redis.expire("service:registry:paper-trading", 120)


def main():
    global _running
    import threading

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)
    try:
        redis.get("ping")
        logger.info("[paper-trading] Connected to Redis at %s", redis_url)
        logger.info("[paper-trading] v%s starting", BUILD_LABEL)
    except Exception as e:
        logger.error("[paper-trading] Cannot connect to Redis at %s: %s", redis_url, e, exc_info=True)
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("paper-trading")

    ledger.init_db()

    TELEGRAM_NOTIFICATIONS.is_production = os.getenv(constant.ENV_PRODUCTION, "0") == "1"
    TELEGRAM_NOTIFICATIONS.is_intraday = True

    instruments = wait_for_instruments(redis)
    span_calculator = SpanCalculator(instruments)

    if not redis.hgetall(ACCOUNT_KEY):
        save_account(redis, PaperAccount())

    threads = [
        threading.Thread(target=analysis_consumer, args=(redis,), name="analysis-consumer", daemon=True),
        threading.Thread(target=confluence_consumer, args=(redis,), name="confluence-consumer", daemon=True),
        threading.Thread(target=strategy_processor, args=(redis, span_calculator), name="strategy-processor", daemon=True),
        threading.Thread(target=mtm_engine, args=(redis,), name="mtm-engine", daemon=True),
        threading.Thread(target=command_listener, args=(redis,), name="command-listener", daemon=True),
    ]
    for t in threads:
        t.start()

    logger.info("[paper-trading] Started, 5 worker threads running")

    from lib.logging_util import refresh_level_from_redis

    heartbeat_counter = 0
    while _running:
        update_heartbeat(redis)
        refresh_level_from_redis(redis, "paper-trading")
        time.sleep(30)
        heartbeat_counter += 1
        if heartbeat_counter % 10 == 0:
            gc.collect()

    logger.info("[paper-trading] Shutting down...")
    for t in threads:
        t.join(timeout=5)
    redis.hset("service:registry:paper-trading", mapping={
        "status": "shutdown", "last_heartbeat": str(time.time()),
    })
    redis.close()


if __name__ == "__main__":
    main()
