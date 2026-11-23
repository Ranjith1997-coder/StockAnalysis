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
import threading
import time
from datetime import date, datetime, time as dtime

import common.constants as constant
from common.market_calendar import is_trading_day
from lib.logging_util import get_logger
logger = get_logger("paper-trading")
from lib.notification.Notification import TELEGRAM_NOTIFICATIONS
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from services.paper_trading import engine, ledger
from services.paper_trading.models import (
    ACCOUNT_KEY,
    CONFIG_KEY,
    CONFIG_SCHEMA,
    COOLDOWN_TTL_SECONDS,
    POSITIONS_OPEN_KEY,
    STOP_LOSS_COOLDOWN_TTL_SECONDS,
    TRADES_STREAM,
    TRADES_STREAM_MAXLEN,
    PaperAccount,
    PaperPosition,
    cooldown_key,
    daily_pnl_key,
    parse_config_value,
    positions_closed_key,
)
from services.paper_trading.signal_router import (
    MAX_POSITIONS,
    check_entry_filters,
    get_pinn_confirmation,
    parse_analysis_result,
    parse_confluence_message,
    parse_pinn_signal,
    portfolio_margin_exceeded,
)
from services.paper_trading.span_calculator import (
    SpanCalculator,
    build_instruments_cache,
    load_instruments_cache,
    load_instruments_fetched_at,
    save_instruments_cache,
)
from services.paper_trading.strategy_builder import build_position

# pinn:signals stream consumed by pinn_signal_consumer().  Defined locally
# (must match SIGNALS_STREAM in services/volatility_engine/signal_emitter.py)
# so this service never imports volatility_engine — that chain pulls torch,
# which is excluded from requirements.txt and will not fit the unit's
# MemoryMax=200M.  PINN's absence must leave paper-trading fail-open.
PINN_SIGNALS_STREAM = "pinn:signals"

ANALYSIS_GROUP = "paper-trader"
CONFLUENCE_GROUP = "paper-trader-confluence"
PINN_GROUP = "paper-trader-pinn"
COMMANDS_STREAM = "paper:commands"
COMMANDS_GROUP = "paper-trader-cmd"
CONSUMER_NAME = "paper-trader-1"

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
ENTRY_WINDOW_START = dtime(9, 20)
ENTRY_WINDOW_END = dtime(15, 0)
ENTRY_MAX_AGG_AGE_SECONDS = 10.0
MTM_CYCLE_SECONDS = 3

# Design doc section 9.3's original thresholds -- never empirically
# validated (no accepted PINN model has run against live composite signals
# yet, see docs/pinn-volatility-engine.md 0.6.1), kept as documented pending
# real data to tune against.
PINN_CONFIRM_SUPPRESS_THRESHOLD = 0.5
PINN_CONFIRM_BOOST_THRESHOLD = 1.0

_running = True
entry_queue: "queue.Queue" = queue.Queue()
exit_queue: "queue.Queue" = queue.Queue()

# Guards every read-modify-write of paper:account and paper:positions:open.
# MTM, strategy-processor and command-listener threads all mutate them; RLock
# because persist_closed_position() is called from inside other locked paths.
_state_lock = threading.RLock()

# Validated /paper_config overrides (see models.CONFIG_SCHEMA).
_runtime_config: dict = {}


def signal_handler(signum, frame):
    global _running
    logger.info("[paper-trading] Received signal %s, shutting down...", signum)
    _running = False


def is_market_hours(now: datetime) -> bool:
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ── Account / position persistence ──────────────────────────────────────────

def get_account(redis, today: "str | None" = None) -> PaperAccount:
    """Load the account, resetting daily_* counters if the trading day changed
    (the caller's save_account persists the reset)."""
    account = PaperAccount.from_redis_mapping(redis.hgetall(ACCOUNT_KEY))
    today = today or date.today().isoformat()
    if account.roll_day(today):
        logger.debug(f"[paper-trading] ACCOUNT | SOURCE trading_day={today} — daily counters reset")
    return account


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
    with _state_lock:
        redis.hset(POSITIONS_OPEN_KEY, mapping={position.position_id: position.to_json()})
        account = get_account(redis)
        account.margin_used += position.margin_blocked
        account.open_positions += 1
        save_account(redis, account)

    TELEGRAM_NOTIFICATIONS.send_live_options_notification(
        format_entry_notification(position), parse_mode="HTML", symbol=position.symbol
    )
    logger.info("[paper-trading] Opened %s %s credit=₹%.0f margin=₹%.0f",
                position.symbol, position.strategy, position.entry_credit, position.margin_blocked)


def persist_closed_position(redis, position: PaperPosition) -> bool:
    """Book a closed position. Returns False (no-op) if another thread already
    removed it from paper:positions:open -- HDEL is the atomic claim."""
    with _state_lock:
        return _persist_closed_position_locked(redis, position)


def _persist_closed_position_locked(redis, position: PaperPosition) -> bool:
    today = date.today().isoformat()

    if redis.hdel(POSITIONS_OPEN_KEY, position.position_id) == 0:
        logger.debug(f"[paper-trading] {position.symbol} | SOURCE position_id={position.position_id} "
                     f"— already closed elsewhere, skipping")
        return False
    # Cooldown starts at EXIT so a stopped-out trade can't immediately re-enter.
    ttl = (STOP_LOSS_COOLDOWN_TTL_SECONDS if position.exit_reason in ("STOP_LOSS", "GAMMA_TRAP")
           else COOLDOWN_TTL_SECONDS)
    redis.set_with_ttl(cooldown_key(position.symbol, position.strategy), "1", ex=ttl)
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
    return True


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
    """Redis copy if present (it carries a TTL, so it can't be stale forever),
    else fetch from Zerodha. In-process staleness/misses are handled by
    SpanCalculator.maybe_refresh()."""
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
                process_analysis_fields(redis, fields)
            except Exception as e:
                logger.exception("[paper-trading] Error parsing analysis result %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(constant.ANALYSIS_RESULTS_STREAM, ANALYSIS_GROUP, msg_id)
                except Exception as e:
                    logger.debug("[paper-trading] xack analysis %s: %s", msg_id, e)


def process_analysis_fields(redis, fields) -> "tuple[list, list]":
    """Parse one analysis:results entry and queue its signals (T5 seam).

    The stream plumbing stays in ``analysis_consumer``; this is the unit the
    e2e harness drives so a simulated cycle exercises the real parser.
    """
    entries, exits = parse_analysis_result(fields)
    for exit_signal in exits:
        exit_queue.put(exit_signal)
    for entry_signal in entries:
        entry_queue.put(entry_signal)
    return entries, exits


def process_confluence_fields(fields):
    """Parse one signals:confluence entry and queue it (T5 seam)."""
    signal = parse_confluence_message(fields)
    if signal:
        entry_queue.put(signal)
    return signal


def process_pinn_fields(fields):
    """Parse one pinn:signals entry and queue it (T5 seam)."""
    signal = parse_pinn_signal(fields)
    if signal:
        entry_queue.put(signal)
    return signal


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
                process_confluence_fields(fields)
            except Exception as e:
                logger.exception("[paper-trading] Error parsing confluence %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(constant.CONFLUENCE_STREAM, CONFLUENCE_GROUP, msg_id)
                except Exception as e:
                    logger.debug("[paper-trading] xack confluence %s: %s", msg_id, e)


def pinn_signal_consumer(redis):
    try:
        redis.xgroup_create(PINN_GROUP, PINN_SIGNALS_STREAM, mkstream=True)
    except Exception as e:
        logger.debug("[paper-trading] xgroup_create %s: %s", PINN_GROUP, e)

    while _running:
        try:
            messages = redis.xreadgroup(
                PINN_GROUP, CONSUMER_NAME,
                {PINN_SIGNALS_STREAM: ">"}, count=10, block=5000,
            )
        except Exception as e:
            logger.error("[paper-trading] pinn consumer error: %s", e, exc_info=True)
            time.sleep(2)
            continue
        if not messages:
            continue
        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            try:
                process_pinn_fields(fields)
            except Exception as e:
                logger.exception("[paper-trading] Error parsing pinn signal %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(PINN_SIGNALS_STREAM, PINN_GROUP, msg_id)
                except Exception as e:
                    logger.debug("[paper-trading] xack pinn %s: %s", msg_id, e)


def _close_at_market(redis, position: PaperPosition, reason: str) -> bool:
    options_live = redis.hgetall(f"data:options_live:{position.symbol}")
    if options_live:
        engine.update_leg_premiums(position, options_live)
    engine.close_position(position, reason, now=time.time(), options_live=options_live)
    return persist_closed_position(redis, position)


def _handle_exit_signal(redis, signal) -> None:
    direction = getattr(signal, "direction", None)
    with _state_lock:
        for position in load_open_positions(redis):
            if position.symbol != signal.symbol:
                continue
            if signal.position_id and position.position_id != signal.position_id:
                continue
            if direction and not engine.is_threatened_by_move(position, direction):
                logger.debug(f"[{signal.reason}] {position.symbol} | SOURCE direction={direction} "
                             f"— {position.strategy} {position.position_id} has no short leg on that side, kept")
                continue
            if not direction:
                logger.debug(f"[{signal.reason}] {position.symbol} | SOURCE direction=None "
                             f"— closing {position.strategy} {position.position_id} regardless of side")
            _close_at_market(redis, position, signal.reason)


def check_entry_gate(redis, symbol: str, now: "datetime | None" = None) -> str:
    """Time/data-freshness gate for new entries. Returns '' if allowed, else reason."""
    now = now or datetime.now()
    if not (ENTRY_WINDOW_START <= now.time() <= ENTRY_WINDOW_END):
        return "outside_entry_window"
    if not is_trading_day(now.date()):
        return "not_trading_day"
    agg = redis.hgetall(f"data:options_agg:{symbol}")
    if engine.is_agg_data_stale(agg, now=now.timestamp(), max_age_seconds=ENTRY_MAX_AGG_AGE_SECONDS):
        return "stale_options_agg"
    return ""


def _handle_entry_signal(redis, span_calculator: SpanCalculator, signal) -> None:
    gate_reason = check_entry_gate(redis, signal.symbol)
    if gate_reason:
        logger.debug("[paper-trading] Entry rejected for %s/%s: %s", signal.symbol, signal.strategy, gate_reason)
        return

    account = get_account(redis)
    open_positions = load_open_positions(redis)

    passed, reason = check_entry_filters(signal, redis, account, open_positions,
                                         max_positions=int(_runtime_config.get("max_positions", MAX_POSITIONS)))
    if not passed:
        logger.debug("[paper-trading] Entry rejected for %s/%s: %s", signal.symbol, signal.strategy, reason)
        return

    # ── PINN confirmation (design doc 9.3, only for non-PINN-sourced
    # signals -- a PINN signal confirming itself would be circular) ──
    if signal.signal_source != "PINN_MISPRICING":
        pinn_z = get_pinn_confirmation(redis, signal)
        if pinn_z is not None:
            if pinn_z < PINN_CONFIRM_SUPPRESS_THRESHOLD:
                logger.info("[paper-trading] %s %s suppressed by PINN (z=%.2f < %.1f)",
                            signal.symbol, signal.strategy, pinn_z, PINN_CONFIRM_SUPPRESS_THRESHOLD)
                return
            if pinn_z > PINN_CONFIRM_BOOST_THRESHOLD:
                logger.info("[paper-trading] %s %s boosted by PINN (z=%.2f > %.1f)",
                            signal.symbol, signal.strategy, pinn_z, PINN_CONFIRM_BOOST_THRESHOLD)
                signal.signal_context["pinn_confirmed"] = True
                signal.signal_context["pinn_z"] = pinn_z

    # signal.mode comes straight from the analysis:results message for
    # composite setups (worker.py now echoes the job's actual intraday/
    # positional mode back onto the result -- see signal_router.py) or
    # defaults to "intraday" for CONFLUENCE signals, which can only ever
    # fire during market hours anyway (LIVE/INTRADAY layers don't exist
    # outside 09:15-15:30).
    position = build_position(signal, redis, span_calculator, account, mode=signal.mode,
                              risk_scale=float(_runtime_config.get("risk_scale", 1.0)))
    if position is None:
        return
    with _state_lock:
        # Re-read: margin may have moved while SPAN was being called.
        if portfolio_margin_exceeded(get_account(redis), new_margin=position.margin_blocked):
            logger.debug("[paper-trading] Entry rejected for %s/%s: portfolio_margin_exceeded_with_new "
                         "(new=₹%.0f)", signal.symbol, signal.strategy, position.margin_blocked)
            return
        persist_new_position(redis, position)


def drain_signal_queues(redis, span_calculator: SpanCalculator) -> int:
    """Handle at most one queued exit and one queued entry.

    Returns the number handled (0 means the caller should idle). Extracted from
    ``strategy_processor`` so the e2e harness can drain deterministically.
    """
    handled = 0
    try:
        _handle_exit_signal(redis, exit_queue.get_nowait())
        handled += 1
    except queue.Empty:
        pass
    try:
        _handle_entry_signal(redis, span_calculator, entry_queue.get_nowait())
        handled += 1
    except queue.Empty:
        pass
    return handled


def strategy_processor(redis, span_calculator: SpanCalculator):
    while _running:
        if not drain_signal_queues(redis, span_calculator):
            time.sleep(0.5)


def apply_mtm_result(redis, result: PaperPosition) -> bool:
    """Persist one MTM outcome. The write-back of a still-open position only
    happens if it is still in paper:positions:open -- another thread (exit
    signal / manual close) may have closed it while MTM was evaluating."""
    with _state_lock:
        if result.status == "CLOSED":
            return persist_closed_position(redis, result)
        if redis.hget(POSITIONS_OPEN_KEY, result.position_id) is None:
            logger.debug(f"[paper-trading] {result.symbol} | SOURCE position_id={result.position_id} "
                         f"— closed during MTM, not writing back")
            return False
        redis.hset(POSITIONS_OPEN_KEY, mapping={result.position_id: result.to_json()})
        return True


def settle_expired_positions(redis, now: datetime) -> None:
    """Outside market hours MTM doesn't run; still settle anything past expiry."""
    for position in load_open_positions(redis):
        if engine.is_past_expiry(position, now):
            result = engine.evaluate_position(position, redis, now=now)
            if result is not None and result.status == "CLOSED":
                apply_mtm_result(redis, result)


def mtm_once(redis, now: datetime, spot_tracker) -> int:
    """Evaluate every open position once. Returns the number evaluated (T5 seam)."""
    if not is_market_hours(now):
        try:
            settle_expired_positions(redis, now)
        except Exception as e:
            logger.error("[paper-trading] expiry settlement error: %s", e, exc_info=True)
        return 0

    move_pct: dict[str, float] = {}
    total_unrealized = 0.0
    evaluated = 0

    for position in load_open_positions(redis):
        if position.symbol not in move_pct:
            spot = engine.read_spot(redis, position.symbol)
            if spot is not None:
                spot_tracker.update(position.symbol, spot, now.timestamp())
            move_pct[position.symbol] = spot_tracker.move_pct(position.symbol)

        result = engine.evaluate_position(
            position, redis, now=now,
            gamma_trap_triggered=engine.check_gamma_trap_proxy(position, move_pct[position.symbol]),
        )
        if result is None:
            continue
        evaluated += 1
        if apply_mtm_result(redis, result) and result.status != "CLOSED":
            total_unrealized += engine.compute_unrealized_pnl(
                result, engine.compute_current_debit(result)
            )

    with _state_lock:
        account = get_account(redis)
        account.unrealized_pnl = total_unrealized
        save_account(redis, account)
    return evaluated


def mtm_engine(redis):
    spot_tracker = engine.SpotMoveTracker()
    while _running:
        mtm_once(redis, datetime.now(), spot_tracker)
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


def apply_config(overrides: dict) -> None:
    """Apply validated config values to the running service."""
    _runtime_config.update(overrides)
    engine.set_exit_params(
        stop_loss_multiple=overrides.get("stop_loss_multiple"),
        profit_target_pct=overrides.get("profit_target_pct"),
    )


def load_config(redis) -> dict:
    """Read paper:config at startup; ignore (and log) unsupported/invalid keys."""
    valid = {}
    for key, raw in (redis.hgetall(CONFIG_KEY) or {}).items():
        parsed = parse_config_value(key, raw)
        if parsed is None:
            logger.warning("[paper-trading] Ignoring unsupported/invalid config %s=%s (supported: %s)",
                           key, raw, ", ".join(CONFIG_SCHEMA))
            continue
        valid[key] = parsed
    apply_config(valid)
    return valid


def _handle_command(redis, fields: dict) -> None:
    command = fields.get("command", "")
    if command == "close":
        target = fields.get("position_id") or "all"
        with _state_lock:
            for position in load_open_positions(redis):
                if target != "all" and position.position_id != target:
                    continue
                _close_at_market(redis, position, "MANUAL")
    elif command == "reset":
        # Book open positions at market (reason RESET) so the ledger keeps a
        # record, then restore a fresh account.
        with _state_lock:
            for position in load_open_positions(redis):
                _close_at_market(redis, position, "RESET")
            save_account(redis, PaperAccount(trading_day=date.today().isoformat()))
    elif command == "config_set":
        key, value = fields.get("key"), fields.get("value")
        parsed = parse_config_value(key, value) if key else None
        if parsed is None:
            logger.warning("[paper-trading] Rejected config_set %s=%s (supported: %s)",
                           key, value, ", ".join(CONFIG_SCHEMA))
            return
        redis.hset(CONFIG_KEY, mapping={key: value})
        apply_config({key: parsed})


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
    span_calculator = SpanCalculator(
        instruments,
        refresher=lambda: try_load_instruments(redis),
        loaded_at=load_instruments_fetched_at(redis),
    )
    load_config(redis)

    if not redis.hgetall(ACCOUNT_KEY):
        save_account(redis, PaperAccount())

    threads = [
        threading.Thread(target=analysis_consumer, args=(redis,), name="analysis-consumer", daemon=True),
        threading.Thread(target=confluence_consumer, args=(redis,), name="confluence-consumer", daemon=True),
        threading.Thread(target=pinn_signal_consumer, args=(redis,), name="pinn-consumer", daemon=True),
        threading.Thread(target=strategy_processor, args=(redis, span_calculator), name="strategy-processor", daemon=True),
        threading.Thread(target=mtm_engine, args=(redis,), name="mtm-engine", daemon=True),
        threading.Thread(target=command_listener, args=(redis,), name="command-listener", daemon=True),
    ]
    for t in threads:
        t.start()

    logger.info("[paper-trading] Started, %d worker threads running", len(threads))

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
