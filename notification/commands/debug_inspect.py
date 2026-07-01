"""Pure inspection functions for the debug command suite.

Returns plain dicts/strings — no Telegram or HTTP code.
Shared by the Telegram bot handlers (notification/commands/debug.py)
and the terminal client (scripts/debug_cli.py).
"""
from __future__ import annotations

import os
import time

import common.shared as shared
from notification.commands._helpers import find_stock_by_symbol


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def _safe_len(obj) -> int:
    try:
        return len(obj)
    except Exception:
        return 0


def _fmt_age(ts) -> str:
    if not ts:
        return "never"
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "never"
    secs = time.time() - ts
    if secs < 60:
        return f"{secs:.0f}s ago"
    if secs < 3600:
        return f"{secs / 60:.1f}m ago"
    return f"{secs / 3600:.1f}h ago"


# ── inspection functions ─────────────────────────────────────────────────────

def _read_market_data_registry() -> dict:
    """Read market-data service stats from Redis service registry."""
    try:
        from notification.commands._helpers import _get_redis
        _r = _get_redis()
        if _r is None:
            return {}
        raw = _r.hgetall("service:registry:market-data")
        if not raw or raw.get("status") != "healthy":
            return {}
        return {
            "ws_connected": raw.get("ws1_connected", "False") == "True",
            "ws_options_connected": raw.get("ws2_connected", "False") == "True",
            "ws_tick_count": int(raw.get("tick_count", 0)),
            "ws1_subscribed": int(raw.get("ws1_subs", 0)),
            "ws2_subscribed": int(raw.get("ws2_subs", 0)),
            "sensibull_feeds": int(raw.get("sensibull_feeds", 0)),
            "last_equity_tick": _fmt_age(float(raw.get("last_equity_tick", 0))),
        }
    except Exception:
        return {}


def inspect_overview() -> dict:
    ctx = shared.app_ctx
    tm = ctx.zd_ticker_manager

    signals_emitted = ctx.signal_bus.total_emitted if ctx.signal_bus else 0
    confluences = ctx.correlator.total_confluences if ctx.correlator else 0

    llm_tokens = 0
    llm_limit = 900_000
    if ctx.narrator:
        client = getattr(ctx.narrator, "_client", None)
        if client:
            llm_tokens = getattr(client, "_daily_tokens", 0)
            llm_limit = getattr(client, "DAILY_TOKEN_LIMIT", 900_000)

    # Read WS stats from market-data service registry (fallback to monolith's tm)
    md = _read_market_data_registry()
    if md:
        ws_connected = md["ws_connected"]
        ws_options_connected = md["ws_options_connected"]
        ws_tick_count = md["ws_tick_count"]
        ws1_subscribed = md["ws1_subscribed"]
        ws2_subscribed = md["ws2_subscribed"]
        ws2_reconnects = 0  # not published yet
        tick_queue_depth = 0
        unknown_tokens = 0
        ws_reconnects = 0
        last_equity_tick = md["last_equity_tick"]
        sensibull_info = {"active": md["sensibull_feeds"] > 0, "feed_count": md["sensibull_feeds"], "feeds": []}
    else:
        # Fallback: read from monolith's own tm (dev mode or transition period)
        ws2_subscribed = 0
        ws2_reconnects = 0
        if tm and getattr(tm, "_kt_options", None):
            kt_opt = tm._kt_options
            ws2_subscribed = _safe_len(getattr(kt_opt, "subscribed_tokens", {}))
            ws2_reconnects = getattr(kt_opt, "reconnect_attempts", 0) if hasattr(kt_opt, "reconnect_attempts") else 0
        ws1_subscribed = 0
        if tm and getattr(tm, "_kt_base", None):
            ws1_subscribed = _safe_len(getattr(tm._kt_base, "subscribed_tokens", {}))
        ws_connected = tm.connected if tm else False
        ws_options_connected = getattr(tm, "options_connected", False) if tm else False
        ws_tick_count = getattr(tm, "_tick_count", 0) if tm else 0
        ws_reconnects = getattr(tm, "reconnect_attempts", 0) if tm else 0
        tick_queue_depth = tm.tick_queue.qsize() if tm else 0
        unknown_tokens = _safe_len(getattr(tm, "_unknown_tokens", set())) if tm else 0
        last_equity_tick = _fmt_age(ctx.last_equity_tick_time)
        sensibull_info = _inspect_sensibull_feed(ctx)

    return {
        "mode": ctx.mode.name if ctx.mode else "NOT_SET",
        "production": os.getenv("PRODUCTION", "0") == "1",
        "intraday_cycle_count": ctx.intraday_cycle_count,
        "last_cycle_time": _fmt_age(ctx.last_cycle_time),
        "stocks": len(ctx.stock_token_obj_dict),
        "indices": len(ctx.index_token_obj_dict),
        "commodities": len(ctx.commodity_token_obj_dict),
        "global_indices": len(ctx.global_indices_token_obj_dict),
        "signals_emitted": signals_emitted,
        "confluences": confluences,
        "ws_connected": ws_connected,
        "ws_options_connected": ws_options_connected,
        "ws_tick_count": ws_tick_count,
        "ws_reconnects": ws_reconnects,
        "tick_queue_depth": tick_queue_depth,
        "unknown_tokens": unknown_tokens,
        "ws1_subscribed": ws1_subscribed,
        "ws2_subscribed": ws2_subscribed,
        "ws2_reconnects": ws2_reconnects,
        "llm_tokens_used": llm_tokens,
        "llm_token_limit": llm_limit,
        "llm_budget_pct": round(llm_tokens / llm_limit * 100, 1) if llm_limit else 0,
        "memory_rss_mb": round(_get_rss_mb(), 1),
        "last_equity_tick": last_equity_tick,
        "options_source": ctx.options_source,
        "error_count": ctx.error_count,
        "monitor_results": dict(ctx.monitor_result_counts),
        "sensibull_ws": sensibull_info,
    }


def _inspect_sensibull_feed(ctx) -> dict:
    """Inspect the Sensibull WebSocket feed(s)."""
    feeds = ctx.sensibull_feed
    if not feeds:
        return {"active": False, "feed_count": 0, "feeds": []}
    feeds_list = feeds if isinstance(feeds, list) else [feeds]
    feed_infos = []
    for i, feed in enumerate(feeds_list):
        thread = getattr(feed, "_thread", None)
        stop_event = getattr(feed, "_stop_event", None)
        subs = getattr(feed, "_subscriptions", [])
        feed_infos.append({
            "index": i,
            "thread_alive": thread.is_alive() if thread else False,
            "stopped": stop_event.is_set() if stop_event else True,
            "subscription_count": _safe_len(subs),
            "subscriptions": [
                {"underlying": s.get("underlying"), "expiry": s.get("expiry")}
                for s in (subs if isinstance(subs, list) else [])
            ],
        })
    return {
        "active": True,
        "feed_count": len(feeds_list),
        "feeds": feed_infos,
    }


def inspect_stock(symbol: str) -> dict:
    stock = find_stock_by_symbol(symbol)
    if stock is None:
        return {"error": f"Symbol {symbol} not found in tracked instruments"}

    # Refresh live tick data from Redis (market-data service snapshots)
    from notification.commands._helpers import refresh_stock_from_redis
    refresh_stock_from_redis(symbol)

    pd_data = stock.priceData
    price_info = {}
    if pd_data is not None and len(pd_data) > 0:
        price_info = {
            "rows": len(pd_data),
            "columns": list(pd_data.columns),
            "first_date": str(pd_data.index[0]),
            "last_date": str(pd_data.index[-1]),
            "last_close": float(pd_data["Close"].iloc[-1]) if "Close" in pd_data.columns else None,
        }

    analysis = stock.analysis or {}
    bullish = analysis.get("BULLISH", {})
    bearish = analysis.get("BEARISH", {})
    neutral = analysis.get("NEUTRAL", {})

    sensibull = stock.sensibull_ctx or {}
    sensibull_info = {
        "last_fetch_time": _fmt_age(sensibull.get("last_fetch_time") or 0),
        "has_current": bool(sensibull.get("current", {}).get("underlying_info")),
        "oi_chain_history_count": _safe_len(sensibull.get("oi_chain_history", [])),
        "has_historical_data": len(sensibull.get("historical_data", [])) > 0,
        "has_iv_chart_history": len(sensibull.get("iv_chart_history", [])) > 0,
        "has_oi_history": len(sensibull.get("oi_history", [])) > 0,
    }

    options_agg = getattr(stock, "options_aggregate", {}) or {}
    options_info = {}
    if options_agg:
        options_info = {
            "last_updated": _fmt_age(options_agg.get("last_updated", 0.0)),
            "atm_strike": options_agg.get("atm_strike"),
            "live_pcr": options_agg.get("live_pcr"),
            "total_ce_oi": options_agg.get("total_ce_oi"),
            "total_pe_oi": options_agg.get("total_pe_oi"),
            "ce_wall": options_agg.get("max_oi_ce_strike"),
            "pe_wall": options_agg.get("max_oi_pe_strike"),
        }

    zerodha_data = getattr(stock, "zerodha_data", {}) or {}

    return {
        "symbol": stock.stock_symbol,
        "name": stock.stockName,
        "is_index": stock.is_index,
        "ltp": stock.ltp,
        "ltp_change_perc": stock.ltp_change_perc,
        "daily_hv": stock.daily_hv,
        "prevDayOHLCV": stock.prevDayOHLCV,
        "priceData": price_info,
        "analysis": {
            "timestamp": analysis.get("Timestamp"),
            "bullish_trends": list(bullish.keys()) if bullish else [],
            "bearish_trends": list(bearish.keys()) if bearish else [],
            "neutral_trends": list(neutral.keys()) if neutral else [],
            "no_of_trends": analysis.get("NoOfTrends", 0),
            "priority_override": analysis.get("PRIORITY_OVERRIDE"),
        },
        "sensibull": sensibull_info,
        "options_aggregate": options_info,
        "zerodha_data": {
            "last_price": zerodha_data.get("last_price"),
            "volume_traded": zerodha_data.get("volume_traded"),
            "change": zerodha_data.get("change"),
        } if zerodha_data else {},
    }


def inspect_signals(symbol: str | None = None) -> dict:
    ctx = shared.app_ctx
    result = {
        "signals_emitted": ctx.signal_bus.total_emitted if ctx.signal_bus else 0,
        "confluences": ctx.correlator.total_confluences if ctx.correlator else 0,
    }

    if symbol and ctx.correlator:
        stock = find_stock_by_symbol(symbol)
        sym = stock.stock_symbol if stock else symbol.upper()
        signals = ctx.correlator.get_buffer_snapshot(sym)
        result["symbol"] = sym
        result["active_signals"] = [
            {
                "direction": s.direction.value,
                "source": s.source,
                "layer": s.layer.value,
                "strength": s.strength.name,
                "age_seconds": round(s.age_seconds, 1),
            }
            for s in signals
        ]
        result["active_signal_count"] = len(signals)
    elif ctx.correlator:
        buffer = getattr(ctx.correlator, "_buffer", {})
        result["symbols_with_signals"] = {
            sym: len(sigs) for sym, sigs in buffer.items() if sigs
        }

    return result


def inspect_cycle() -> dict:
    ctx = shared.app_ctx
    result: dict = {
        "monolith_cycle_count": ctx.intraday_cycle_count,
        "last_cycle_time": _fmt_age(ctx.last_cycle_time),
    }

    # Data-gateway registry from Redis
    try:
        from intraday.intraday_monitor import redis_proxy
        if redis_proxy:
            gw_hash = redis_proxy.hgetall("service:registry:data-gateway")
            if gw_hash:
                result["data_gateway"] = {
                    "status": gw_hash.get("status", "?"),
                    "cycle_count": gw_hash.get("cycle_count", "?"),
                    "price_symbols": gw_hash.get("price_symbols", "?"),
                    "last_heartbeat": _fmt_age(
                        float(gw_hash.get("last_heartbeat", 0))
                        if gw_hash.get("last_heartbeat") else 0
                    ),
                }

            # Last few cycle stream entries
            entries = redis_proxy.xread("data:cycle_stream", count=5)
            if entries:
                for _stream, msgs in entries:
                    result["recent_cycles"] = [
                        {"id": mid, "fields": mf if isinstance(mf, dict) else str(mf)}
                        for mid, mf in msgs
                    ]
    except Exception as e:
        result["redis_error"] = str(e)

    return result


def inspect_redis(symbol: str) -> dict:
    result: dict = {"symbol": symbol.upper()}
    try:
        from intraday.intraday_monitor import redis_proxy
        if not redis_proxy:
            result["error"] = "Redis proxy not initialised"
            return result

        for prefix in ("data:price", "data:sensibull", "data:zerodha"):
            key = f"{prefix}:{symbol.upper()}"
            hkeys = redis_proxy.hkeys(key)
            result[prefix] = {
                "exists": bool(hkeys),
                "fields": hkeys,
                "field_count": len(hkeys) if hkeys else 0,
            }

        # Cycle stream length
        try:
            xlen = redis_proxy.xlen("data:cycle_stream")
            result["cycle_stream_length"] = xlen
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)

    return result


def inspect_counters() -> dict:
    ctx = shared.app_ctx
    tm = ctx.zd_ticker_manager

    return {
        "intraday_cycle_count": ctx.intraday_cycle_count,
        "last_cycle_time": _fmt_age(ctx.last_cycle_time),
        "signals_emitted": ctx.signal_bus.total_emitted if ctx.signal_bus else 0,
        "confluences": ctx.correlator.total_confluences if ctx.correlator else 0,
        "monitor_results": dict(ctx.monitor_result_counts),
        "error_count": ctx.error_count,
        "ws_tick_count": getattr(tm, "_tick_count", 0) if tm else 0,
        "ws_reconnects": getattr(tm, "reconnect_attempts", 0) if tm else 0,
        "tick_queue_depth": tm.tick_queue.qsize() if tm else 0,
        "unknown_tokens": _safe_len(getattr(tm, "_unknown_tokens", set())) if tm else 0,
        "llm_tokens_used": (
            getattr(ctx.narrator._client, "_daily_tokens", 0)
            if ctx.narrator and getattr(ctx.narrator, "_client", None) else 0
        ),
        "sensibull_ws": _inspect_sensibull_feed(ctx),
        "memory_rss_mb": round(_get_rss_mb(), 1),
    }


def inspect_memory() -> dict:
    ctx = shared.app_ctx
    return {
        "mode": ctx.mode.name if ctx.mode else "NOT_SET",
        "stock_token_obj_dict": len(ctx.stock_token_obj_dict),
        "index_token_obj_dict": len(ctx.index_token_obj_dict),
        "commodity_token_obj_dict": len(ctx.commodity_token_obj_dict),
        "global_indices_token_obj_dict": len(ctx.global_indices_token_obj_dict),
        "stocks_list": len(ctx.stocks_list),
        "index_list": len(ctx.index_list),
        "commodity_list": len(ctx.commodity_list),
        "global_indices_list": len(ctx.global_indices_list),
        "stockExpires": list(ctx.stockExpires),
        "options_source": ctx.options_source,
        "last_equity_tick_time": _fmt_age(ctx.last_equity_tick_time),
        "llm_budget_warned": ctx.llm_budget_warned,
        "ticker_52w_high_count": len(shared.ticker_52_week_high_list),
        "ticker_52w_low_count": len(shared.ticker_52_week_low_list),
        "sensibull_feed_active": ctx.sensibull_feed is not None,
        "sensibull_feed": _inspect_sensibull_feed(ctx),
        "memory_rss_mb": round(_get_rss_mb(), 1),
    }


def inspect_analyzers() -> dict:
    try:
        from intraday.intraday_monitor import orchestrator
        if orchestrator is None:
            return {"error": "Orchestrator not initialised"}
        analysers = []
        for a in orchestrator.analysers:
            analysers.append({
                "class": type(a).__name__,
                "is_active": getattr(a, "is_active", True),
            })
        return {"analyser_count": len(analysers), "analysers": analysers}
    except Exception as e:
        return {"error": str(e)}
