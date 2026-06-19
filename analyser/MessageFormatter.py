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

def _fmt_one_future_action(d) -> str:
    _ae = {"long_buildup": "🟢", "short_buildup": "🔴",
           "short_covering": "🟢", "long_unwinding": "🔴"}
    e    = _ae.get(d.action, "")
    line = (f"  Futures: {e} <b>{d.action}</b> "
            f"p%:<code>{d.price_percentage:.2f}</code> "
            f"oi%:<code>{d.oi_percentage:.2f}</code>")
    line += _score_suffix(d)
    return line


@MessageFormatter.register("FUTURE_ACTION")
def _fmt_future_action(data, trend):
    items = data if isinstance(data, list) else [data]
    return [_fmt_one_future_action(d) for d in items]


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


@MessageFormatter.register("FUTURE_OI_TREND")
def _fmt_future_oi_trend(data, trend):
    e = "📈" if trend == "BULLISH" else "📉"
    line = f"  Futures OI Trend: {e} <b>{data.action}</b>"
    parts = [f"OI 10d={data.oi_chg_10d:+.1f}%"]
    if data.oi_chg_20d is not None:
        parts.append(f"20d={data.oi_chg_20d:+.1f}%")
    parts.append(f"Price 10d={data.price_chg_10d:+.1f}%")
    if data.price_chg_20d is not None:
        parts.append(f"20d={data.price_chg_20d:+.1f}%")
    line += f" <code>{' | '.join(parts)}</code>"
    return [line]


@MessageFormatter.register("FUTURE_COST_OF_CARRY")
def _fmt_future_coc(data, trend):
    e = "⚠️" if data.action == "BACKWARDATION" else ("🔥" if data.action == "HIGH_COST_OF_CARRY" else "📊")
    line = f"  Cost of Carry: {e} <b>{data.action}</b>"
    parts = [f"basis={data.basis_pct:+.2f}%"]
    if data.ann_coc is not None:
        parts.append(f"ann_coc={data.ann_coc:.1f}%")
    if data.days_to_expiry is not None:
        parts.append(f"expiry={data.days_to_expiry}d")
    line += f" <code>{' | '.join(parts)}</code>"
    return [line]


# ── PCR ───────────────────────────────────────────────────────────────────────

@MessageFormatter.register("PCR_EXTREME")
def _fmt_pcr_extreme(data, trend):
    confirmed_str = " ✓confirmed" if getattr(data, "confirmed", False) else ""
    return [f"  PCR Extreme: <b>{data.zone}</b> "
            f"PCR=<code>{data.pcr_value:.3f}</code>{confirmed_str} - <i>{data.signal}</i>"]


@MessageFormatter.register("PCR_BIAS")
def _fmt_pcr_bias(data, trend):
    strength  = getattr(data, "strength", "")
    trend_dir = getattr(data, "trend_direction", "")
    suffix    = f" [{strength}/{trend_dir}]" if strength else ""
    return [f"  PCR Bias: <b>{data.bias}</b> PCR=<code>{data.total_pcr:.3f}</code>{suffix}"]


@MessageFormatter.register("PCR_TREND")
def _fmt_pcr_trend(data, trend):
    abs_str = f" abs={data.pcr_change_abs:+.3f}" if hasattr(data, "pcr_change_abs") else ""
    return [f"  PCR Trend: <b>{data.trend}</b> "
            f"PCR=<code>{data.pcr_current:.3f}</code> "
            f"Δ=<code>{data.pcr_change_pct:.2f}%</code>{abs_str}"]


@MessageFormatter.register("PCR_INTRADAY_TREND")
def _fmt_pcr_intraday_trend(data, trend):
    return [f"  PCR Intraday Trend: <b>{data.trend}</b> "
            f"<code>{data.pcr_first:.3f}→{data.pcr_last:.3f}</code> "
            f"Δ=<code>{data.pcr_change_pct:+.2f}%</code> "
            f"over {data.snapshots} snapshots"]


@MessageFormatter.register("PCR_REVERSAL")
def _fmt_pcr_reversal(data, trend):
    return [
        f"  PCR Reversal: <b>{data.reversal_type}</b> "
        f"{data.previous_zone}→{data.current_zone}",
        f"    PCR: <code>{data.previous_pcr:.3f}</code> → "
        f"<code>{data.current_pcr:.3f}</code> | <i>{data.signal}</i>",
    ]


@MessageFormatter.register("PCR_POS_REVERSAL")
def _fmt_pcr_pos_reversal(data, trend):
    return [
        f"  PCR Pos Reversal: <b>{data.reversal_type}</b> "
        f"{data.previous_zone}→{data.current_zone}",
        f"    3d avg: <code>{data.previous_pcr:.3f}</code> → "
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
    return [f"  IV Spike: {d.expiry} ATM IV=<code>{d.iv:.1f}%</code> Δ=<code>{d.iv_change:+.2f}%</code>"
            for d in items]


@MessageFormatter.register("IV_TREND")
def _fmt_iv_trend(data, trend):
    items = data if isinstance(data, list) else [data]
    return [f"  IV Trend: {d.expiry} <b>{d.trend}</b> "
            f"ATM IV=<code>{d.atm_iv:.1f}%</code> Δ=<code>{d.iv_change_pct:+.2f}%</code>"
            for d in items]


@MessageFormatter.register("IV_RANK")
def _fmt_iv_rank(data, trend):
    items = data if isinstance(data, list) else [data]
    return [f"  IV Rank: {d.expiry} IVP=<code>{d.iv_percentile:.1f}</code> "
            f"ATM IV=<code>{d.atm_iv:.1f}%</code> (<b>{d.category}</b>)"
            for d in items]


@MessageFormatter.register("IV_RANK_EXTREME")
def _fmt_iv_rank_extreme(data, trend):
    items = data if isinstance(data, list) else [data]
    return [f"  IV Rank Extreme: {d.expiry} IVP=<code>{d.iv_percentile:.1f}</code> "
            f"ATM IV=<code>{d.atm_iv:.1f}%</code> (<b>{d.category}</b>)"
            for d in items]


@MessageFormatter.register("IV_PREMIUM")
def _fmt_iv_premium(data, trend):
    zone_emoji = {"EXTREME": "🔥", "EXPENSIVE": "💰"}.get(data.zone, "📊")
    return [
        f"  {zone_emoji} <b>IV PREMIUM</b> [{data.zone}] "
        f"IV=<code>{data.atm_iv:.1f}%</code> "
        f"HV=<code>{data.hv:.1f}%</code> "
        f"Ratio=<code>{data.iv_hv_ratio:.2f}x</code> "
        f"Premium=<code>{data.iv_premium_pct:+.1f}%</code>",
        f"    {data.expiry} [{data.hv_period}] — seller has edge",
    ]


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


@MessageFormatter.register("OI_POSITIONAL_TREND")
def _fmt_oi_positional_trend(data, trend):
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        type_emoji = {
            "CALL_BUILDUP_ALIGNED": "🔴🔗",
            "CALL_BUILDUP":         "🔴",
            "PUT_BUILDUP_ALIGNED":  "🟢🔗",
            "PUT_BUILDUP":          "🟢",
            "BALANCED_ACCUMULATION": "⚪",
        }.get(d.buildup_type, "")
        lines.append(
            f"  {type_emoji} OI Positional Trend: <b>{d.buildup_type}</b> "
            f"[{d.days_analysed}d] "
            f"Call=<code>{d.call_oi_change_pct:+.1f}%</code> "
            f"Put=<code>{d.put_oi_change_pct:+.1f}%</code> "
            f"Fut=<code>{d.futures_oi_change_pct:+.1f}%</code> "
            f"PCR=<code>{d.current_pcr}</code>"
        )
        lines.append(f"    <i>{d.signal}</i>")
    return lines


@MessageFormatter.register("OI_ACCELERATION")
def _fmt_oi_acceleration(data, trend):
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        e = "🔴" if d.side == "CALL" else "🟢"
        lines.append(
            f"  {e} OI Acceleration: <b>{d.side}</b> "
            f"<code>{d.accel_ratio:.1f}x</code> faster | "
            f"recent=<code>{d.recent_velocity:,.0f}/day</code> "
            f"prev=<code>{d.prev_velocity:,.0f}/day</code>"
        )
        lines.append(f"    <i>{d.signal}</i>")
    return lines


@MessageFormatter.register("OI_CAPITULATION")
def _fmt_oi_capitulation(data, trend):
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        e = "🟢" if d.side == "CALL" else "🔴"
        top_str = " | ".join(
            f"{s:.0f}(-{r:,.0f}/{p:.0f}%)" for s, r, p in d.top_strikes[:3]
        )
        lines.append(
            f"  {e} OI Capitulation: <b>{d.side}</b> "
            f"unwound=<code>{d.total_unwound:,.0f}</code> "
            f"(<code>{d.unwound_pct:.1f}%</code> of total) "
            f"{d.num_significant_strikes} strikes"
        )
        lines.append(f"    Top: {top_str} | exp={d.expiry}")
    return lines


@MessageFormatter.register("OI_WALL_MIGRATION")
def _fmt_oi_wall_migration(data, trend):
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        dir_emoji = {
            "HIGHER":  "⬆️",
            "LOWER":   "⬇️",
            "RETREAT": "⚠️",
            "UNCHANGED": "➡️",
        }.get(d.migration_direction, "")
        if d.migration_direction == "RETREAT":
            lines.append(
                f"  {dir_emoji} OI Wall Migration: <b>{d.side} RETREAT</b> "
                f"prev=<code>{d.prev_wall_strike:.0f}</code> → vanished today"
            )
        else:
            lines.append(
                f"  {dir_emoji} OI Wall Migration: <b>{d.side} {d.migration_direction}</b> "
                f"<code>{d.prev_wall_strike:.0f}→{d.curr_wall_strike:.0f}</code> "
                f"(<code>{d.migration_pts:+.0f} pts / {d.migration_pct:+.2f}%</code>)"
            )
        lines.append(f"    <i>{d.signal}</i>")
    return lines


# ── 52-week ───────────────────────────────────────────────────────────────────

@MessageFormatter.register("52-week-high")
def _fmt_52h(data, trend):
    return ["  💥 Price at <b>52 WEEK HIGH</b>"]


@MessageFormatter.register("52-week-low")
def _fmt_52l(data, trend):
    return ["  💥 Price at <b>52 WEEK LOW</b>"]


@MessageFormatter.register("PANIC_MODE")
def _fmt_panic_mode(data, trend):
    e = "🔴" if trend == "BEARISH" else "🟢"
    conditions = ", ".join(data.conditions_met)
    return [
        f"  {e} <b>PANIC MODE</b> [{data.mode}] {data.direction} "
        f"Price=<code>{data.price_change_pct:+.1f}%</code> "
        f"({data.conditions_count}/6 confirmed)",
        f"    {conditions}",
    ]


@MessageFormatter.register("PANIC_EXHAUSTION")
def _fmt_panic_exhaustion(data, trend):
    e = "🔄"
    ivp_str = f" IVP=<code>{data.iv_percentile:.1f}</code>" if data.iv_percentile else ""
    conditions = ", ".join(data.conditions_met)
    return [
        f"  {e} <b>PANIC EXHAUSTION</b> [{data.mode}] {data.panic_direction} panic burning out"
        f"{ivp_str} ({data.conditions_count}/4 confirmed)",
        f"    {conditions}",
    ]


# ── Option-Seller Composite Setups ────────────────────────────────────────────
# These render as structured trade cards rather than single signal lines.
# Each card has: a bold header with trade type, numbered condition lines,
# and a mode footer so the trader knows whether this is a 0DTE scalp or
# an overnight/weekend hold.

def _mode_tag(mode_str) -> str:
    """Convert mode name to a human-readable trade-duration label."""
    if "intraday" in str(mode_str).lower():
        return "0DTE / Weekly Theta scalp"
    return "Overnight / Weekend decay hold"


@MessageFormatter.register("GAMMA_TRAP")
def _fmt_gamma_trap(data, trend):
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        dir_arrow = "⬆️" if d.direction == "BULLISH" else "⬇️"
        _all_conditions = ["G1 Wall Breach", "G2 Volume", "G3 Futures", "G4 IV Spike"]
        triggers = getattr(d, "triggers", {})

        cond_lines = []
        for ckey in _all_conditions:
            metric = triggers.get(ckey)
            # Map trigger key back to conditions_detail label fragments
            _detail_map = {
                "G1 Wall Breach": ("OI_CAPITULATION", "OI_WALL_MIGRATION", "GEX_WALL_BREACH"),
                "G2 Volume":      ("VOLUME_BREAKOUT", "VOLUME_CLIMAX"),
                "G3 Futures":     ("FUTURES(",),
                "G4 IV Spike":    ("IV_SPIKE",),
            }
            fired = any(
                any(frag in cd for frag in _detail_map.get(ckey, ()))
                for cd in d.conditions_detail
            )
            label = ckey.split(" ", 1)[1]  # strip "G1 " prefix
            if fired and metric:
                cond_lines.append(f"  ✅ {label} — {metric}")
            elif fired:
                cond_lines.append(f"  ✅ {label}")
            else:
                cond_lines.append(f"  ❌ {label} — not detected")

        lines += [
            f"  🚨 <b>GAMMA TRAP — CLOSE SHORTS NOW</b>  "
            f"[<code>{d.conditions_met}/4</code>]",
            f"  Direction: {dir_arrow} <b>{d.direction}</b> breakout confirmed",
            *cond_lines,
            f"  ⏱ Mode: <i>{_mode_tag(d.mode)}</i>",
        ]
    return lines


@MessageFormatter.register("RANGE_BOUND_SETUP")
def _fmt_range_bound(data, trend):
    _setup_labels = {"IRON_CONDOR": "Iron Condor", "STRANGLE": "Strangle"}

    # condition key → (display label, trigger key)
    _all_conditions = [
        ("OVERPRICED_VOL",      "IV overpriced",           "R1 IV"),
        ("CEILING_AND_FLOOR",   "OI walls",                "R2 OI Walls"),
        ("NEUTRAL_MOMENTUM",    "Neutral momentum",        "R3 Momentum"),
        ("NO_INSTIT_PUSH",      "No institutional push",   "R4 Futures"),
        ("MAX_PAIN_MAGNET",     "Max pain magnet",         "R5 MaxPain"),
        ("GEX_POSITIVE_REGIME", "GEX positive regime",     "R6 GEX"),
    ]

    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        setup_label = _setup_labels.get(d.setup_type, d.setup_type)
        triggers = getattr(d, "triggers", {})

        # ── Box range line (always shown) ─────────────────────────────────────
        if d.put_wall_strike and d.call_wall_strike:
            box_width_pct = (d.call_wall_strike - d.put_wall_strike) / d.put_wall_strike * 100
            box_line = (
                f"  📦 Box: <code>{d.put_wall_strike:.0f}</code> ←──→ "
                f"<code>{d.call_wall_strike:.0f}</code>  "
                f"(<code>{box_width_pct:.1f}%</code> wide)"
            )
        elif d.put_wall_strike:
            box_line = f"  📦 Floor: <code>{d.put_wall_strike:.0f}</code> (ceiling not detected)"
        elif d.call_wall_strike:
            box_line = f"  📦 Ceiling: <code>{d.call_wall_strike:.0f}</code> (floor not detected)"
        else:
            box_line = "  📦 Box: walls present (strikes unavailable)"

        # ── Unified condition + metric lines ──────────────────────────────────
        cond_lines = []
        total = len(_all_conditions)
        for (ckey, label, tkey) in _all_conditions:
            fired = ckey in d.conditions_detail
            metric = triggers.get(tkey, "")
            if fired:
                cond_lines.append(f"  ✅ {label}" + (f" — {metric}" if metric else ""))
            else:
                cond_lines.append(f"  ❌ {label} — not confirmed")

        lines += [
            f"  🎯 <b>RANGE PREMIUM — {setup_label}</b>  "
            f"[<code>{d.conditions_met}/{total}</code>]",
            box_line,
            *cond_lines,
            f"  ⏱ Mode: <i>{_mode_tag(d.mode)}</i>",
        ]
    return lines


@MessageFormatter.register("SKEW_FADE_SETUP")
def _fmt_skew_fade(data, trend):
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        fade_arrow = "⬆️" if d.fade_direction == "BULLISH" else "⬇️"
        triggers = getattr(d, "triggers", {})

        cond_lines = []
        for tkey, label in [
            ("S1 Exhaustion", "Exhaustion"),
            ("S2 SR Wall",    "SR wall"),
            ("S3 PCR Trap",   "PCR trap"),
        ]:
            metric = triggers.get(tkey, "")
            cond_lines.append(f"  ✅ {label} — {metric}" if metric else f"  ✅ {label}")

        lines += [
            f"  ⚔️ <b>SKEW FADE — {d.fade_direction} CREDIT SPREAD</b>  [3/3]",
            *cond_lines,
            f"  Trade: {fade_arrow} Sell {d.panic_direction.lower()} side → "
            f"<b>{d.fade_direction}</b> credit spread",
            f"  ⏱ Mode: <i>{_mode_tag(d.mode)}</i>",
        ]
    return lines


# ── GEX Formatters ─────────────────────────────────────────────────────────────

@MessageFormatter.register("GEX_REGIME")
def _fmt_gex_regime(data, trend):
    """
    GEX_REGIME — Gamma regime context card (informational, not a trade card).
    Rendered prominently only when regime has flipped; otherwise compact.
    """
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        regime_emoji = "🟢" if d.regime == "POSITIVE" else "🔴"
        regime_label = "POSITIVE — dealers long gamma (market PINS)" if d.regime == "POSITIVE" \
                       else "NEGATIVE — dealers short gamma (market TRENDS)"

        mag_context = {
            "MILD":     "weak signal, low conviction",
            "MODERATE": "moderate conviction",
            "STRONG":   "high conviction",
        }.get(d.magnitude, d.magnitude)

        flip_str = (
            f"  🔄 <b>REGIME FLIP</b>: {d.prev_regime} → <b>{d.regime}</b>\n"
            if d.regime_flipped else ""
        )
        lines += [
            f"  {regime_emoji} <b>GEX REGIME</b>: {regime_label}",
        ]
        if d.regime_flipped:
            lines.append(f"  🔄 <b>REGIME FLIP</b>: {d.prev_regime} → <b>{d.regime}</b>")
        lines += [
            f"  📊 Net GEX:  <code>{d.gex_total:+.0f} Cr</code>  "
            f"[CE: <code>{d.gex_ce:.0f}</code>  PE: <code>{d.gex_pe:.0f}</code>]  "
            f"— {mag_context}",
        ]
        if d.flip_level:
            lines.append(
                f"  🎯 Flip level: <code>{d.flip_level:.0f}</code> "
                f"(above = pin, below = trend)"
            )
    return lines


@MessageFormatter.register("GEX_WALL_BREACH")
def _fmt_gex_wall_breach(data, trend):
    """
    GEX_WALL_BREACH — Directional breach confirmed by dealer gamma unwind.
    High-conviction: spot crossed a gamma wall AND dealers stopped defending.
    """
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        dir_arrow = "⬆️" if d.breach_side == "CALL" else "⬇️"
        dir_label = "BULLISH breakout" if d.breach_side == "CALL" else "BEARISH breakdown"
        wall_label = "call wall" if d.breach_side == "CALL" else "put wall"

        lines += [
            f"  {dir_arrow} <b>GEX WALL BREACH — {dir_label}</b>",
            f"  🧱 Breached {wall_label}: <code>{d.breached_strike:.0f}</code>",
            f"  📍 Spot:    <code>{d.spot:.2f}</code>  "
            f"(<code>{d.spot_beyond_pct:.2f}%</code> beyond wall)",
            f"  📉 Dealer GEX at strike: "
            f"<code>{d.gex_prev_cycle:+.1f} Cr</code> → "
            f"<code>{d.gex_at_strike:+.1f} Cr</code>  "
            f"(dropped <b>{d.gex_drop_pct:.0f}%</b> — dealers stopped defending)",
        ]
    return lines


@MessageFormatter.register("GEX_IMBALANCE")
def _fmt_gex_imbalance(data, trend):
    """
    GEX_IMBALANCE — CE or PE gamma dominance creating a persistent directional headwind.
    """
    items = data if isinstance(data, list) else [data]
    lines = []
    for d in items:
        if d.dominant_side == "CE":
            dir_emoji = "🔴"
            headwind = "dealers must SELL every rally aggressively (bearish headwind)"
        else:
            dir_emoji = "🟢"
            headwind = "dealers must BUY every dip aggressively (bullish floor)"

        mag_emoji = {"MODERATE": "⚠️", "STRONG": "🔥", "EXTREME": "💥"}.get(d.magnitude, "")

        lines += [
            f"  {dir_emoji} <b>GEX IMBALANCE</b> — {mag_emoji} {d.magnitude}",
            f"  📊 CE GEX: <code>{d.gex_ce:.0f} Cr</code>  "
            f"PE GEX: <code>{d.gex_pe:.0f} Cr</code>  "
            f"Ratio: <b>{d.imbalance_ratio:.1f}x</b> ({d.dominant_side} dominant)",
            f"  ⚡ {headwind}",
        ]
    return lines
