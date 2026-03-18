"""
MessageFormatter
────────────────
Registry-based formatter for batch analysis alert lines.

Each analysis type registers a formatter function that receives (data, trend)
and returns a list[str] of HTML lines to append to the Telegram message.

If no formatter is found for an analysis_type, a generic fallback fires so
no alert is ever silently dropped.

Registration happens at module import time via @MessageFormatter.register().
Importers only need:
    from analyser.MessageFormatter import MessageFormatter
"""

from common.logging_util import logger
from typing import Callable


class MessageFormatter:
    # analysis_type → fn(data, trend) → list[str]
    _registry: dict[str, Callable] = {}

    @classmethod
    def register(cls, *analysis_types: str):
        """Decorator — register one formatter for one or more analysis_type keys."""
        def decorator(fn: Callable) -> Callable:
            for key in analysis_types:
                cls._registry[key] = fn
            return fn
        return decorator

    @classmethod
    def format(cls, analysis_type: str, data, trend: str) -> list[str]:
        """
        Format one analysis result into HTML lines.
        Never returns empty — falls back to a generic line if no formatter registered.
        """
        fn = cls._registry.get(analysis_type)
        if fn:
            try:
                return fn(data, trend)
            except Exception as exc:
                logger.error(f"MessageFormatter error for '{analysis_type}': {exc}")
                return [f"  {analysis_type}: (format error)"]
        # ── Fallback: log once per unknown type, never drop the alert ──────────
        logger.warning(f"MessageFormatter: no formatter registered for '{analysis_type}' — using fallback")
        return [f"  {analysis_type}: {data}"]

    @classmethod
    def registered_types(cls) -> list[str]:
        return sorted(cls._registry.keys())


# ── Shared helpers ────────────────────────────────────────────────────────────

_CONF_EMOJI = {"HIGH": "🔥", "MEDIUM": "📈", "LOW": "📊", "VERY_LOW": "⚪"}


def _score_suffix(data) -> str:
    if hasattr(data, 'score') and hasattr(data, 'confidence'):
        e = _CONF_EMOJI.get(data.confidence, "")
        return f" | {e} <code>{data.score}</code> ({data.confidence})"
    return ""


def _mtf_suffix(data) -> str:
    return " 🔗MTF" if (hasattr(data, 'mtf_aligned') and data.mtf_aligned) else ""


def _pvo_line(d) -> str:
    base = (f"  PVO: <b>{d.pattern}</b> "
            f"p:<code>{d.price_pct:.2f}%</code> "
            f"v:<code>{d.vol_pct:.2f}%</code> "
            f"oi:<code>{d.oi_pct:.2f}%</code>")
    return base + _score_suffix(d) + _mtf_suffix(d)


# ── Volume ────────────────────────────────────────────────────────────────────

@MessageFormatter.register("Volume")
def _fmt_volume(data, trend):
    return [
        f"  Volume {trend.lower()}: <code>{data.Volume_rate_percent:.2f}%</code>",
        f"  Price {trend.lower()}: <code>{data.price_change_percent:.2f}%</code>",
    ]


@MessageFormatter.register("VOLUME_BREAKOUT")
def _fmt_volume_breakout(data, trend):
    e = "📈" if trend == "BULLISH" else "📉"
    return [f"  {e} Vol Breakout: Vol=<code>{data.volume:,.0f}</code> "
            f"({data.volume_ratio:.1f}x MA) "
            f"Price=<code>{data.price_change_pct:+.2f}%</code>"]


@MessageFormatter.register("OBV_DIVERGENCE")
def _fmt_obv(data, trend):
    e = "🟢" if trend == "BULLISH" else "🔴"
    return [
        f"  {e} OBV Div: <b>{data.divergence_type}</b> "
        f"Price=<code>{data.price_previous:.2f}→{data.price_current:.2f}</code>",
        f"    OBV trend=<code>{data.trend}</code> "
        f"weakening=<code>{data.trend_weakening}</code>",
    ]


@MessageFormatter.register("VOLUME_CLIMAX")
def _fmt_climax(data, trend):
    return [
        f"  🚨 Vol Climax: <b>{data.climax_type}</b> "
        f"Vol=<code>{data.volume:,.0f}</code> ({data.volume_ratio:.1f}x MA)",
        f"    Price trend=<code>{data.price_trend_pct:+.1f}%</code> "
        f"close_pos=<code>{data.close_position:.2f}</code>",
    ]


# ── Technical ─────────────────────────────────────────────────────────────────

@MessageFormatter.register("RSI")
def _fmt_rsi(data, trend):
    if hasattr(data, 'zone_candles'):
        zone = "Overbought" if trend == "BEARISH" else "Oversold"
        e = "🔴" if trend == "BEARISH" else "🟢"
        lines = [f"  {e} RSI: <code>{data.value:.2f}</code> ({zone} {data.zone_candles}c)"]
        if hasattr(data, 'price_trend'):
            lines.append(f"    Price trend: <code>{data.price_trend}</code>")
        return lines
    return [f"  RSI: <code>{data.value:.2f}</code>"]


@MessageFormatter.register("rsi_crossover")
def _fmt_rsi_crossover(data, trend):
    return [f"  RSI crossover: <code>{data.prev_value:.2f} → {data.curr_value:.2f}</code>"]


@MessageFormatter.register("BollingerBand")
def _fmt_bb(data, trend):
    if hasattr(data, 'signal_type'):
        above = "above" in data.signal_type
        cmp   = "&gt;" if above else "&lt;"
        band  = "Upper" if above else "Lower"
        bval  = f"{data.upper_band:.2f}" if above else f"{data.lower_band:.2f}"
        e     = "🚀" if above else "📉"
        line  = (f"  BB: {e} Price(<code>{data.close:.2f}</code>) {cmp} "
                 f"{band}(<code>{bval}</code>)")
        if hasattr(data, 'trend') and hasattr(data, 'confirmation_candles'):
            line += f" [{data.trend}, {data.confirmation_candles}c]"
        else:
            line += f" [{data.signal_type}]"
    else:
        cmp  = "&gt;" if trend == "BULLISH" else "&lt;"
        band = "Upper" if trend == "BULLISH" else "Lower"
        bval = f"{data.upper_band:.2f}" if trend == "BULLISH" else f"{data.lower_band:.2f}"
        line = f"  BB: Price(<code>{data.close:.2f}</code>) {cmp} {band}(<code>{bval}</code>)"
    return [line]


@MessageFormatter.register("EMA_CROSSOVER")
def _fmt_ema(data, trend):
    cmp      = "&gt;" if trend == "BULLISH" else "&lt;"
    adx_str  = f" ADX:<code>{data.adx:.1f}</code>" if hasattr(data, 'adx') else ""
    diff_str = f" Δ<code>{data.diff_pct:+.2f}%</code>" if hasattr(data, 'diff_pct') else ""
    return [f"  EMA: <b>{data.direction}</b> "
            f"fast:<code>{data.fast_ema:.2f}</code> {cmp} "
            f"slow:<code>{data.slow_ema:.2f}</code>{diff_str}{adx_str}"]


@MessageFormatter.register("SUPERTREND")
def _fmt_supertrend(data, trend):
    arrow = "↑" if trend == "BULLISH" else "↓"
    return [f"  Supertrend: {arrow} "
            f"ST=<code>{data.supertrend_value:.2f}</code> "
            f"Price=<code>{data.close:.2f}</code> | <i>{data.signal}</i>"]


@MessageFormatter.register("RSI_DIVERGENCE")
def _fmt_rsi_div(data, trend):
    return [f"  RSI Div: <b>{data.divergence_type}</b> "
            f"P:<code>{data.price_previous:.2f}→{data.price_current:.2f}</code> "
            f"RSI:<code>{data.rsi_previous:.1f}→{data.rsi_current:.1f}</code>"]


@MessageFormatter.register("STOCHASTIC")
def _fmt_stoch(data, trend):
    if hasattr(data, 'zone_candles'):
        e  = "🟢" if trend == "BULLISH" else "🔴"
        se = {"STRONG": "🔥", "MODERATE": "📈", "WEAK": "📊"}.get(data.signal_strength, "")
        return [
            f"  {e} Stoch: %K=<code>{data.k_value:.1f}</code> "
            f"%D=<code>{data.d_value:.1f}</code> ({data.zone_candles}c in zone)",
            f"    {se} {data.signal_strength} | {data.signal}",
        ]
    return [f"  Stoch: %K=<code>{data.k_value:.1f}</code> "
            f"%D=<code>{data.d_value:.1f}</code> | <i>{data.signal}</i>"]


@MessageFormatter.register("MACD")
def _fmt_macd(data, trend):
    return [f"  MACD: <i>{data}</i>"]


@MessageFormatter.register("vwap_deviation")
def _fmt_vwap(data, trend):
    cmp  = "&lt;" if trend == "BULLISH" else "&gt;"
    side = "below" if trend == "BULLISH" else "above"
    return [
        f"  VWAP: <code>{data.close:.2f}</code> {cmp} "
        f"<code>{data.vwap:.2f}</code> Dev:<code>{data.deviation:.2f}%</code>",
        f"    Intervals {side}: {data.vwap_days}",
    ]


@MessageFormatter.register("BUY_SELL")
def _fmt_buy_sell(data, trend):
    cmp = "&gt;" if trend == "BULLISH" else "&lt;"
    return [f"  BuySell: Buy <code>{data.buy_quantity:.0f}</code> "
            f"{cmp} Sell <code>{data.sell_quantity:.0f}</code>"]


@MessageFormatter.register("PIVOT_POINTS")
def _fmt_pivot(data, trend):
    return [f"  Pivot: <b>{data.signal}</b> "
            f"Price=<code>{data.close:.2f}</code> "
            f"{data.level_name}=<code>{data.level_value:.2f}</code> "
            f"PP=<code>{data.pivot:.2f}</code>"]


@MessageFormatter.register("ATR")
def _fmt_atr(data, trend):
    return [f"  ATR: <code>{data.atr_value:.2f}</code> "
            f"(<code>{data.atr_percentage:.2f}%</code>)"]


# ── Candlestick patterns ───────────────────────────────────────────────────────

@MessageFormatter.register("Single_candle_stick_pattern")
def _fmt_c1_mom(data, trend):
    return [f"  Candle (1) Mom: <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


@MessageFormatter.register("Single_candle_reversal_pattern")
def _fmt_c1_rev(data, trend):
    return [f"  Candle (1) Rev: <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


@MessageFormatter.register("Double_candle_stick_pattern")
def _fmt_c2(data, trend):
    return [f"  Candle (2): <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


@MessageFormatter.register("Double_candle_continuation_pattern")
def _fmt_c2_cont(data, trend):
    return [f"  Candle (2) Cont: <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


@MessageFormatter.register("Triple_candle_stick_pattern")
def _fmt_c3(data, trend):
    return [f"  Candle (3): <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


@MessageFormatter.register("Triple_candle_reversal_pattern")
def _fmt_c3_rev(data, trend):
    return [f"  Candle (3) Rev: <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


@MessageFormatter.register("Triple_candle_continuation_pattern")
def _fmt_c3_cont(data, trend):
    return [f"  Candle (3) Cont: <i>{d}</i>"
            for d in (data if isinstance(data, list) else [data])]


# ── Futures ───────────────────────────────────────────────────────────────────

@MessageFormatter.register("FUTURE_ACTION")
def _fmt_future_action(data, trend):
    _ae = {"long_buildup": "🟢", "short_buildup": "🔴",
           "short_covering": "🟢", "long_unwinding": "🔴"}
    e    = _ae.get(data.action, "")
    line = (f"  Futures: {e} <b>{data.action}</b> "
            f"p%:<code>{data.price_percentage:.2f}</code> "
            f"oi%:<code>{data.oi_percentage:.2f}</code>")
    line += _score_suffix(data)
    return [line]


@MessageFormatter.register("FUTURE_BREAKOUT_PATTERN")
def _fmt_future_breakout(data, trend):
    e    = "🚀" if "up" in data.pattern else "📉"
    line = f"  Futures Breakout: {e} <b>{data.pattern}</b>"
    line += f" ORB:[<code>{data.orb_low:.2f}-{data.orb_high:.2f}</code>]"
    confirms = []
    if data.oi_confirm:
        confirms.append("OI✓")
    if data.vol_confirm:
        confirms.append("Vol✓")
    if confirms:
        line += f" [{', '.join(confirms)}]"
    line += _score_suffix(data) + _mtf_suffix(data)
    return [line]


@MessageFormatter.register("FUTURE_PVO_PATTERN")
def _fmt_pvo(data, trend):
    items = data if isinstance(data, list) else [data]
    return [_pvo_line(d) for d in items]


# ── PCR ───────────────────────────────────────────────────────────────────────

@MessageFormatter.register("PCR_EXTREME")
def _fmt_pcr_extreme(data, trend):
    return [f"  PCR Extreme: <b>{data.zone}</b> "
            f"PCR=<code>{data.pcr_value:.3f}</code> - <i>{data.signal}</i>"]


@MessageFormatter.register("PCR_BIAS")
def _fmt_pcr_bias(data, trend):
    return [f"  PCR Bias: <b>{data.bias}</b> PCR=<code>{data.total_pcr:.3f}</code>"]


@MessageFormatter.register("PCR_TREND")
def _fmt_pcr_trend(data, trend):
    return [f"  PCR Trend: <b>{data.trend}</b> "
            f"PCR=<code>{data.pcr_current:.3f}</code> "
            f"Δ=<code>{data.pcr_change_pct:.2f}%</code>"]


@MessageFormatter.register("PCR_REVERSAL")
def _fmt_pcr_reversal(data, trend):
    return [
        f"  PCR Reversal: <b>{data.reversal_type}</b> "
        f"{data.previous_zone}→{data.current_zone}",
        f"    PCR: <code>{data.previous_pcr:.3f}</code> → "
        f"<code>{data.current_pcr:.3f}</code> | <i>{data.signal}</i>",
    ]


@MessageFormatter.register("PCR_DIVERGENCE")
def _fmt_pcr_div(data, trend):
    return [f"  PCR Div: Near=<code>{data.near_month_pcr:.3f}</code> "
            f"Far=<code>{data.far_month_pcr:.3f}</code> "
            f"Div=<code>{data.divergence:.3f}</code> - <i>{data.signal}</i>"]


# ── Max Pain ──────────────────────────────────────────────────────────────────

@MessageFormatter.register("MAX_PAIN")
def _fmt_max_pain(data, trend):
    lines = [f"  MaxPain: Price=<code>{data.current_price:.2f}</code> "
             f"MP=<code>{data.max_pain_strike:.2f}</code> "
             f"Dev=<code>{data.deviation_pct:+.2f}%</code> ({data.signal_strength})"]
    if data.pcr:
        lines.append(f"    Exp={data.expiry} "
                     f"PCR=<code>{data.pcr:.3f}</code> "
                     f"Type={data.max_pain_type}")
    return lines


@MessageFormatter.register("MAX_PAIN_TREND")
def _fmt_max_pain_trend(data, trend):
    return [
        f"  MP Trend: <b>{data.trend}</b> "
        f"Curr=<code>{data.curr_max_pain:.2f}</code> "
        f"Prev=<code>{data.prev_max_pain:.2f}</code>",
        f"    Exp={data.expiry} "
        f"CurrDev=<code>{data.curr_deviation:+.2f}%</code> "
        f"PrevDev=<code>{data.prev_deviation:+.2f}%</code>",
    ]


@MessageFormatter.register("MAX_PAIN_ALIGNMENT")
def _fmt_max_pain_align(data, trend):
    return [
        f"  MP Align: <b>{data.alignment}</b> "
        f"MP={data.max_pain_type} PCR={data.pcr_type}",
        f"    <i>{data.signal}</i>",
    ]


# ── IV ────────────────────────────────────────────────────────────────────────

@MessageFormatter.register("IV_SPIKE")
def _fmt_iv_spike(data, trend):
    items = data if isinstance(data, list) else [data]
    return [f"  IV Spike: {d.expiry} <code>{d.iv_change:.2f}%</code>"
            for d in items]


@MessageFormatter.register("IV_TREND")
def _fmt_iv_trend(data, trend):
    items = data if isinstance(data, list) else [data]
    return [f"  IV Trend: {d.expiry} <b>{d.trend}</b> "
            f"<code>{d.iv_change_pct:.2f}%</code>"
            for d in items]


# ── OI Chain ──────────────────────────────────────────────────────────────────

@MessageFormatter.register("OI_SUPPORT_RESISTANCE")
def _fmt_oi_sr(data, trend):
    # NEUTRAL variant (compact) when oi_range attribute is present
    if hasattr(data, 'oi_range'):
        return [f"  OI S/R: Range={data.oi_range} | "
                f"S=<code>{data.support_strike:.0f}</code> "
                f"R=<code>{data.resistance_strike:.0f}</code>"]
    # BULLISH/BEARISH variant (full detail)
    return [
        f"  OI S/R: S=<code>{data.support_strike:.0f}</code>"
        f"(OI:{data.support_oi:,.0f}) "
        f"R=<code>{data.resistance_strike:.0f}</code>"
        f"(OI:{data.resistance_oi:,.0f})",
        f"    Price=<code>{data.current_price:.2f}</code> | <i>{data.signal}</i>",
    ]


@MessageFormatter.register("OI_BUILDUP")
def _fmt_oi_buildup(data, trend):
    ratio_val = data.call_put_oi_change_ratio
    ratio_str = f"{ratio_val:.1f}x" if ratio_val != float('inf') else "∞"
    return [
        f"  OI Buildup: <b>{data.buildup_type}</b> "
        f"CallΔ=<code>{data.total_call_oi_change:+,.0f}</code> "
        f"PutΔ=<code>{data.total_put_oi_change:+,.0f}</code> "
        f"Ratio=<code>{ratio_str}</code>",
        f"    <i>{data.signal}</i>",
    ]


@MessageFormatter.register("OI_WALL")
def _fmt_oi_wall(data, trend):
    return [
        f"  OI Wall: <b>{data.wall_type}</b>",
        f"    <i>{data.signal}</i>",
    ]


@MessageFormatter.register("OI_SHIFT")
def _fmt_oi_shift(data, trend):
    call_c = f"{data.call_oi_center:.0f}" if data.call_oi_center else "N/A"
    put_c  = f"{data.put_oi_center:.0f}"  if data.put_oi_center  else "N/A"
    return [
        f"  OI Shift: CallCenter=<code>{call_c}</code> "
        f"PutCenter=<code>{put_c}</code>",
        f"    NewCall=<code>{data.total_new_call_oi:,.0f}</code> "
        f"NewPut=<code>{data.total_new_put_oi:,.0f}</code>",
        f"    <i>{data.signal}</i>",
    ]


@MessageFormatter.register("OI_INTRADAY_TREND")
def _fmt_oi_intraday(data, trend):
    # NEUTRAL variant (compact) — no percentage fields
    if trend == "NEUTRAL":
        return [
            f"  OI Trend: Call={data.call_oi_trend} "
            f"Put={data.put_oi_trend} PCR={data.pcr_trend}",
            f"    <i>{data.signal}</i>",
        ]
    # BULLISH/BEARISH variant (full detail)
    return [
        f"  OI Trend: "
        f"Call={data.call_oi_trend}(<code>{data.call_oi_change_pct:+.1f}%</code>) "
        f"Put={data.put_oi_trend}(<code>{data.put_oi_change_pct:+.1f}%</code>) "
        f"PCR={data.pcr_trend}(<code>{data.first_pcr:.2f}→{data.last_pcr:.2f}</code>)",
        f"    [{data.snapshots_used} snaps] <i>{data.signal}</i>",
    ]


@MessageFormatter.register("OI_SR_SHIFT")
def _fmt_oi_sr_shift(data, trend):
    return [
        f"  OI S/R Shift: "
        f"R:<code>{data.first_resistance:.0f}→{data.last_resistance:.0f}</code> "
        f"S:<code>{data.first_support:.0f}→{data.last_support:.0f}</code>",
        f"    [{data.snapshots_used} snaps] <i>{data.signal}</i>",
    ]


# ── 52-week ───────────────────────────────────────────────────────────────────

@MessageFormatter.register("52-week-high")
def _fmt_52h(data, trend):
    return ["  💥 Price at <b>52 WEEK HIGH</b>"]


@MessageFormatter.register("52-week-low")
def _fmt_52l(data, trend):
    return ["  💥 Price at <b>52 WEEK LOW</b>"]
