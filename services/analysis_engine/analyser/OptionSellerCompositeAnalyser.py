"""
OptionSellerCompositeAnalyser
─────────────────────────────
Composite analyser that cross-reads stock.analysis (already fully populated by all
earlier analysers) and emits three high-probability option-seller setups:

    GAMMA_TRAP         — Kill-switch: close short positions, directional move in progress.
    RANGE_BOUND_SETUP  — Iron Condor / Strangle candidate: stock trapped in a box with
                         overpriced vol and no institutional directional push.
    SKEW_FADE_SETUP    — Directional credit spread: sell the overpriced side when a panic
                         exhausts itself at a key OI level with a confirming reversal candle.

REGISTRATION ORDER
    Must be registered AFTER PanicModeAnalyser — reads PANIC_EXHAUSTION from stock.analysis.
    Controls its own dispatch (no @BaseAnalyzer.both decorators on setup methods) so that
    execution order is guaranteed: Gamma Trap → Range Bound → Skew Fade.
    Gamma Trap sets stock.analysis["NEUTRAL"]["GAMMA_TRAP_ACTIVE"] = True before Range Bound
    runs, which allows Range Bound to suppress itself in the same cycle.

SCORING BYPASS
    All three setups emit into stock.analysis["NEUTRAL"] and set
    stock.analysis["PRIORITY_OVERRIDE"] so should_notify() bypasses the score gate.
    Weights in constants.py are 0 so these keys never inflate base scores.
"""

import traceback
from collections import namedtuple

from .Analyser import BaseAnalyzer
from .IVAnalyser import nearest_expiry
from common.Stock import Stock
from lib.logging_util import get_logger
logger = get_logger("analyser")
from common.scoring import NotificationPriority
import common.constants as constant
import common.shared as shared


# ── Output namedtuples (module-level: not recreated on every call) ────────────

RANGE_BOUND_SETUP_NT = namedtuple("RangeBoundSetup", [
    "conditions_met",    # int : number of conditions that fired (out of 6)
    "conditions_detail", # list[str] : human-readable names of fired conditions
    "iv_percentile",     # float | None : IVP from IV_RANK_EXTREME or IV_RANK
    "iv_trigger",        # str | None : "IV_RANK" | "IV_PREMIUM" — which signal fired R1
    "iv_hv_ratio",       # float | None : IV/HV ratio (only when iv_trigger="IV_PREMIUM")
    "iv_premium_pct",    # float | None : IV premium % above HV (only when iv_trigger="IV_PREMIUM")
    "put_wall_strike",   # float | None : nearest put wall strike (floor)
    "call_wall_strike",  # float | None : nearest call wall strike (ceiling)
    "max_pain_dev_pct",  # float | None : spot % deviation from max pain
    "setup_type",        # str : "IRON_CONDOR" (both walls) | "STRANGLE"
    "mode",              # str : app_ctx.mode.value
    "gex_supports",      # bool : True when GEX regime is POSITIVE + MODERATE/STRONG
    "triggers",          # dict[str, str] : {condition_name: metric_string} for fired conditions
])

SKEW_FADE_SETUP_NT = namedtuple("SkewFadeSetup", [
    "conditions_met",        # int
    "fade_direction",        # str : "BULLISH" | "BEARISH" — direction of the trade
    "panic_direction",       # str : direction of the original panic being faded
    "exhaustion_confidence", # str : "MODERATE" | "HIGH" | "EXTREME"
    "sr_level",              # float : the OI S/R strike being tested
    "sr_proximity_pct",      # float : how close spot is to that strike (%)
    "candle_key",            # str : analysis key of the confirming candle pattern
    "pcr_signal",            # str : which PCR key confirmed ("PCR_REVERSAL" etc.)
    "mode",                  # str
    "triggers",              # dict[str, str] : {condition_name: metric_string}
])

GAMMA_TRAP_NT = namedtuple("GammaTrap", [
    "conditions_met",    # int : 3 or 4
    "conditions_detail", # list[str]
    "direction",         # str : "BULLISH" | "BEARISH" — direction of the move
    "breach_signal",     # str : "OI_CAPITULATION" | "OI_WALL_MIGRATION" | "GEX_WALL_BREACH"
    "volume_signal",     # str : "VOLUME_BREAKOUT" | "VOLUME_CLIMAX" | "FUTURES_VOLUME_SURGE" (indices) | None
    "mode",              # str
    "triggers",          # dict[str, str] : {condition_name: metric_string}
])


class OptionSellerCompositeAnalyser(BaseAnalyzer):
    """
    Option-seller composite — emits three high-probability trade setups by
    reading the fully-populated stock.analysis dictionary.

    Execution order is hardcoded: Gamma Trap → Range Bound → Skew Fade.
    @BaseAnalyzer decorators are intentionally stripped from setup methods
    because this class controls its own dispatch.
    """

    # ── Thresholds (mode-agnostic by design) ─────────────────────────────────
    RANGE_BOUND_MIN_CONDITIONS  = 5   # of 6 must fire (raised from 4 — 4/6 was too loose, causing noise)
    SKEW_FADE_MIN_CONDITIONS    = 3   # of 3 — strict, all must fire
    GAMMA_TRAP_MIN_CONDITIONS   = 3   # of 4 must fire

    # Iron Condor is a NEUTRAL strategy — it should only fire on stocks that are
    # genuinely range-bound, not trending. Stocks moving >2% intraday are NOT
    # range-bound regardless of IV/OI structure.
    RANGE_BOUND_MAX_MOVE_PCT = 2.0

    MAX_PAIN_PROXIMITY_PCT  = 1.5   # spot within 1.5% of max pain = magnet zone
    SR_PROXIMITY_PCT        = 0.5   # spot within 0.5% of S/R strike = "testing the wall"

    # Only high-quality reversal patterns are accepted as candle confirmation.
    # Continuation patterns (Marubozu, 2/3 cont) are excluded per backtest PF data.
    REVERSAL_CANDLE_KEYS = frozenset([
        "Double_candle_stick_pattern",    # Engulfing, Piercing, Dark Cloud — PF 1.06
        "Single_candle_reversal_pattern", # Hammer, Shooting Star — PF 1.03
        "Triple_candle_stick_pattern",    # Morning/Evening Star — PF 1.09
        "Triple_candle_reversal_pattern", # Same tier — reliable
    ])

    def __init__(self) -> None:
        self.analyserName = "Option Seller Composite Analyser"
        super().__init__()

    def reset_constants(self):
        pass  # thresholds are mode-agnostic — nothing to reset

    # ── Dispatch overrides: enforce Gamma Trap → Range Bound → Skew Fade ─────
    # We override these directly instead of using @BaseAnalyzer.both so the
    # execution sequence is explicit and cannot be re-ordered by the framework.

    def run_all_intraday_analyses(self, stock: Stock) -> bool:
        found  = self.detect_gamma_trap_warning(stock)
        found |= self.detect_range_bound_premium_setup(stock)
        found |= self.detect_skew_fade_setup(stock)
        return found

    def run_all_positional_analyses(self, stock: Stock) -> bool:
        found  = self.detect_gamma_trap_warning(stock)
        found |= self.detect_range_bound_premium_setup(stock)
        found |= self.detect_skew_fade_setup(stock)
        return found

    def run_all_index_intraday_analyses(self, stock: Stock) -> bool:
        return self.run_all_intraday_analyses(stock)

    def run_all_index_positional_analyses(self, stock: Stock) -> bool:
        return self.run_all_positional_analyses(stock)

    # ── Shared helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _get(stock: Stock, sentiment: str, key: str):
        """Safely retrieve a signal from stock.analysis[sentiment][key]."""
        return stock.analysis.get(sentiment, {}).get(key)

    @staticmethod
    def _any(data) -> bool:
        """True if data is a non-empty truthy value (handles list and single item)."""
        if data is None:
            return False
        if isinstance(data, list):
            return len(data) > 0
        return bool(data)

    @staticmethod
    def _get_attr(data, attr, default=None):
        """Get attribute from a namedtuple or the first item in a list."""
        if data is None:
            return default
        item = data[0] if isinstance(data, list) else data
        return getattr(item, attr, default)

    def _is_intraday(self) -> bool:
        return shared.app_ctx.mode.name == shared.Mode.INTRADAY.name

    @staticmethod
    def _items(data) -> list:
        if data is None:
            return []
        return data if isinstance(data, list) else [data]

    @staticmethod
    def _spot(stock: Stock) -> float | None:
        if stock.ltp:
            return float(stock.ltp)
        chain = stock.sensibull_ctx.get("oi_chain") or {}
        ltp = chain.get("current_ltp")
        return float(ltp) if ltp else None

    @staticmethod
    def _per_strike(stock: Stock) -> dict | None:
        chain = stock.sensibull_ctx.get("oi_chain") or {}
        data = chain.get("per_strike_data")
        return data if data else None

    # ── Futures evidence (keys actually emitted by FuturesAnalyser) ──────────
    # FUTURE_ACTION           : .action in {long_buildup, short_buildup, short_covering,
    #                           long_unwinding}, .expiry in {current, next}
    # FUTURE_OI_FROM_OPEN     : .action in {OI_BUILDUP_FROM_OPEN, OI_SHORT_BUILD_FROM_OPEN, ...}
    # FUTURE_PVO_PATTERN      : .pattern e.g. price_up_vol_oi_incr / price_down_vol_oi_incr
    # FUTURE_BREAKOUT_PATTERN : ORB breakout (OI + volume confirmed), .mtf_aligned bool
    _BUILDUP = {
        "BULLISH": {"FUTURE_ACTION": "long_buildup",
                    "FUTURE_OI_FROM_OPEN": "OI_BUILDUP_FROM_OPEN",
                    "FUTURE_PVO_PATTERN": "price_up_vol_oi_incr"},
        "BEARISH": {"FUTURE_ACTION": "short_buildup",
                    "FUTURE_OI_FROM_OPEN": "OI_SHORT_BUILD_FROM_OPEN",
                    "FUTURE_PVO_PATTERN": "price_down_vol_oi_incr"},
    }

    def _futures_buildup(self, stock: Stock, direction: str):
        """First fresh-position buildup entry in `direction` → (key, subtype, item) or None.
        Next-expiry FUTURE_ACTION is ignored (rollover noise, not new conviction)."""
        for key, subtype in self._BUILDUP[direction].items():
            field = "pattern" if key == "FUTURE_PVO_PATTERN" else "action"
            for it in self._items(self._get(stock, direction, key)):
                if getattr(it, field, None) != subtype:
                    continue
                if key == "FUTURE_ACTION" and getattr(it, "expiry", "current") != "current":
                    continue
                return key, subtype, it
        return None

    def _futures_breakout(self, stock: Stock, direction: str):
        """ORB breakout in `direction`, MTF-aligned entries preferred → item or None."""
        items = self._items(self._get(stock, direction, "FUTURE_BREAKOUT_PATTERN"))
        if not items:
            return None
        aligned = [b for b in items if getattr(b, "mtf_aligned", False)]
        return (aligned or items)[0]

    def _futures_volume_surge(self, stock: Stock, direction: str | None = None):
        """Volume surge on the current-month futures (index spot has no volume).

        Returns (ratio, price_chg_pct) when last-bar volume >= SURGE_MULT × mean of the
        prior LOOKBACK bars and the last bar moved in `direction` (any direction when None).
        """
        try:
            fut = (stock.zerodha_ctx.get("futures_data") or {}).get("current")
            lookback = constant.COMPOSITE_FUT_VOL_LOOKBACK
            if fut is None or fut.empty or "volume" not in fut.columns or len(fut) < lookback + 1:
                return None
            vol = fut["volume"].astype(float)
            avg = vol.iloc[-(lookback + 1):-1].mean()
            last = vol.iloc[-1]
            if not avg or avg <= 0:
                return None
            ratio = last / avg
            prev_close, last_close = float(fut["close"].iloc[-2]), float(fut["close"].iloc[-1])
            chg = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
            if ratio < constant.COMPOSITE_FUT_VOL_SURGE_MULT:
                return None
            if direction == "BULLISH" and chg <= 0:
                return None
            if direction == "BEARISH" and chg >= 0:
                return None
            return ratio, chg
        except Exception:
            logger.debug(f"[COMPOSITE] {stock.stock_symbol} — futures volume read failed: {traceback.format_exc()}")
            return None

    @staticmethod
    def _max_pain_strike(stock: Stock) -> float | None:
        """max_pain_strike of the nearest live expiry from Sensibull per_expiry_map."""
        stats = (stock.sensibull_ctx.get("current") or {}).get("stats") or {}
        per_expiry = stats.get("per_expiry_map") or {}
        exp = nearest_expiry(per_expiry.keys())
        if exp is None:
            return None
        mp = (per_expiry.get(exp) or {}).get("max_pain_strike")
        try:
            return float(mp) if mp else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _chain_range_walls(per_strike: dict, spot: float):
        """Floor/ceiling walls straight from the OI chain (no asymmetry requirement).

        floor   = strike below spot with the max put OI,  ceiling = strike above spot with
        the max call OI; each within [MIN, MAX]% of spot and >= MIN_OI_MULT × side average.
        Returns ((put_strike, put_oi) | None, (call_strike, call_oi) | None).
        """
        puts  = [(float(k), float(v.get("put_oi", 0) or 0))  for k, v in per_strike.items()]
        calls = [(float(k), float(v.get("call_oi", 0) or 0)) for k, v in per_strike.items()]
        put_vals  = [oi for _, oi in puts if oi > 0]
        call_vals = [oi for _, oi in calls if oi > 0]
        if not put_vals or not call_vals:
            return None, None
        avg_put, avg_call = sum(put_vals) / len(put_vals), sum(call_vals) / len(call_vals)
        lo, hi = constant.COMPOSITE_RB_WALL_MIN_DIST_PCT, constant.COMPOSITE_RB_WALL_MAX_DIST_PCT
        mult = constant.COMPOSITE_RB_WALL_MIN_OI_MULT

        def _in_band(strike):
            d = abs(strike - spot) / spot * 100
            return lo <= d <= hi

        floor_c = [(s, oi) for s, oi in puts  if s < spot and _in_band(s) and oi > 0]
        ceil_c  = [(s, oi) for s, oi in calls if s > spot and _in_band(s) and oi > 0]
        floor = max(floor_c, key=lambda x: x[1]) if floor_c else None
        ceil  = max(ceil_c,  key=lambda x: x[1]) if ceil_c else None
        if floor and floor[1] < mult * avg_put:
            floor = None
        if ceil and ceil[1] < mult * avg_call:
            ceil = None
        return floor, ceil

    def _holding_sr_level(self, per_strike: dict, spot: float, fade_direction: str):
        """Nearest dominant OI support (BULLISH fade) / resistance (BEARISH fade) that is
        holding and that spot is testing. Returns (strike, oi, proximity_pct) or None.

        Support  = put-OI strike at/below spot (tolerance SR_PROXIMITY_PCT above spot),
        Resistance = call-OI strike at/above spot (tolerance SR_PROXIMITY_PCT below).
        Dominant = OI >= COMPOSITE_SR_MIN_OI_MULT × side average. Holding = writers not
        fleeing (OI not down > COMPOSITE_SR_MAX_OI_DROP_PCT vs prev day, when prev known)
        and spot within SR_PROXIMITY_PCT of the level.
        """
        oi_key, prev_key = (("put_oi", "prev_put_oi") if fade_direction == "BULLISH"
                            else ("call_oi", "prev_call_oi"))
        vals = [float(v.get(oi_key, 0) or 0) for v in per_strike.values()]
        vals = [v for v in vals if v > 0]
        if not vals:
            return None
        avg = sum(vals) / len(vals)
        tol = self.SR_PROXIMITY_PCT / 100
        cands = []
        for k, v in per_strike.items():
            s = float(k)
            oi = float(v.get(oi_key, 0) or 0)
            if oi < constant.COMPOSITE_SR_MIN_OI_MULT * avg:
                continue
            if fade_direction == "BULLISH" and s > spot * (1 + tol):
                continue
            if fade_direction == "BEARISH" and s < spot * (1 - tol):
                continue
            prev = float(v.get(prev_key, 0) or 0)
            if prev > 0 and (prev - oi) / prev * 100 > constant.COMPOSITE_SR_MAX_OI_DROP_PCT:
                continue
            cands.append((s, oi))
        if not cands:
            return None
        strike, oi = min(cands, key=lambda x: abs(x[0] - spot))
        prox = abs(spot - strike) / strike * 100
        if prox > self.SR_PROXIMITY_PCT:
            return None
        return strike, oi, prox

    def _mode_label(self) -> str:
        # Use .name ("INTRADAY" / "POSITIONAL") not .value (1 / 2)
        return shared.app_ctx.mode.name.lower()

    def _set_priority_override(self, stock: Stock, priority: NotificationPriority) -> None:
        """
        Set the priority override only if it is a higher priority than the current one.
        Ensures Gamma Trap (CRITICAL) is never downgraded by a later setup (HIGH).
        """
        _order = [
            NotificationPriority.NONE,
            NotificationPriority.LOW,
            NotificationPriority.MEDIUM,
            NotificationPriority.HIGH,
            NotificationPriority.CRITICAL,
        ]
        current = stock.analysis.get("PRIORITY_OVERRIDE")
        if current is None or _order.index(priority) > _order.index(current):
            stock.analysis["PRIORITY_OVERRIDE"] = priority

    # ─────────────────────────────────────────────────────────────────────────
    # Setup 3 — Gamma Trap Warning (runs FIRST to set suppression flag)
    # ─────────────────────────────────────────────────────────────────────────

    def detect_gamma_trap_warning(self, stock: Stock) -> bool:
        """
        Setup 3: Kill-switch — warn option seller to close short positions.

        A massive directional move is confirmed when 3/4 of these align:
            G1  Wall Breach    OI_CAPITULATION (any sentiment) OR
                               directional OI_WALL_MIGRATION (HIGHER/LOWER, not RETREAT)
            G2  Volume Surge   Stocks: VOLUME_BREAKOUT or VOLUME_CLIMAX.
                               Indices: current-month futures volume surge in the move
                               direction (index spot has no volume; VolumeAnalyser skips indices)
            G3  Futures Fuel   FUTURE_BREAKOUT_PATTERN in the move direction, or a fresh
                               buildup: FUTURE_ACTION.action long/short_buildup (current
                               expiry), FUTURE_OI_FROM_OPEN buildup, FUTURE_PVO_PATTERN buildup
            G4  Vol Expansion  IV_SPIKE (rises only) in NEUTRAL

        If it fires:
            - Sets stock.analysis["NEUTRAL"]["GAMMA_TRAP_ACTIVE"] = True
              so Range Bound can check and suppress itself in the same cycle.
            - Sets PRIORITY_OVERRIDE to CRITICAL so the score gate is bypassed.
        """
        try:
            logger.debug(f"[GAMMA_TRAP] {stock.stock_symbol} — start")
            conditions:   list[str] = []
            triggers:     dict[str, str] = {}
            breach_signal: str | None = None
            volume_signal: str | None = None
            direction:     str | None = None

            # ── G1: Wall Breach ───────────────────────────────────────────────
            # OI_CAPITULATION: either side unwinding = directional clearing
            cap_bull = self._get(stock, "BULLISH", "OI_CAPITULATION")
            cap_bear = self._get(stock, "BEARISH", "OI_CAPITULATION")
            if self._any(cap_bull) or self._any(cap_bear):
                conditions.append("OI_CAPITULATION")
                breach_signal = "OI_CAPITULATION"
                cap_data = cap_bull or cap_bear
                items = cap_data if isinstance(cap_data, list) else [cap_data]
                side = getattr(items[0], "side", "CALL") if items else "CALL"
                direction = "BULLISH" if side == "CALL" else "BEARISH"
                unwound_pct = getattr(items[0], "unwound_pct", None)
                top_strikes = getattr(items[0], "top_strikes", [])
                metric = f"{side} OI unwound"
                if unwound_pct is not None:
                    metric += f" {unwound_pct:.0f}%"
                if top_strikes:
                    metric += f" near {top_strikes[0]}"
                triggers["G1 Wall Breach"] = metric
            else:
                # OI_WALL_MIGRATION with an actual shift (not RETREAT) is a proxy breach
                for sentiment in ("BULLISH", "BEARISH"):
                    mig_data = self._get(stock, sentiment, "OI_WALL_MIGRATION")
                    if mig_data is None:
                        continue
                    items = mig_data if isinstance(mig_data, list) else [mig_data]
                    for m in items:
                        mdir = getattr(m, "migration_direction", "")
                        side = getattr(m, "side", "")
                        # A CALL wall moving HIGHER = sellers conceding → bullish
                        # A PUT wall moving LOWER  = floor collapsing → bearish
                        if side == "CALL" and mdir == "HIGHER":
                            conditions.append("OI_WALL_MIGRATION(CALL_HIGHER)")
                            breach_signal = "OI_WALL_MIGRATION"
                            direction = "BULLISH"
                            mpts = getattr(m, "migration_pts", None)
                            triggers["G1 Wall Breach"] = "CALL wall migrated HIGHER" + (f" +{mpts:.0f}pts" if mpts else "")
                            break
                        elif side == "PUT" and mdir == "LOWER":
                            conditions.append("OI_WALL_MIGRATION(PUT_LOWER)")
                            breach_signal = "OI_WALL_MIGRATION"
                            direction = "BEARISH"
                            mpts = getattr(m, "migration_pts", None)
                            triggers["G1 Wall Breach"] = "PUT wall migrated LOWER" + (f" {mpts:.0f}pts" if mpts else "")
                            break
                    if breach_signal:
                        break

            # GEX_WALL_BREACH: dealer GEX confirmed drop — additive G1 confirmation.
            # Direction inferred from breach_side (CALL breach → bullish, PUT breach → bearish).
            if not breach_signal:
                for sentiment in ("BULLISH", "BEARISH"):
                    gex_breach = self._get(stock, sentiment, "GEX_WALL_BREACH")
                    if self._any(gex_breach):
                        items = gex_breach if isinstance(gex_breach, list) else [gex_breach]
                        for b in items:
                            bside = getattr(b, "breach_side", "")
                            if bside == "CALL":
                                conditions.append("GEX_WALL_BREACH(CALL)")
                                breach_signal = "GEX_WALL_BREACH"
                                direction = "BULLISH"
                                drop = getattr(b, "gex_drop_pct", None)
                                strike = getattr(b, "breached_strike", None)
                                triggers["G1 Wall Breach"] = "GEX CALL wall " + (f"{strike:.0f} " if strike else "") + "broken" + (f", dealer GEX dropped {drop:.0f}%" if drop else "")
                            elif bside == "PUT":
                                conditions.append("GEX_WALL_BREACH(PUT)")
                                breach_signal = "GEX_WALL_BREACH"
                                direction = "BEARISH"
                                drop = getattr(b, "gex_drop_pct", None)
                                strike = getattr(b, "breached_strike", None)
                                triggers["G1 Wall Breach"] = "GEX PUT wall " + (f"{strike:.0f} " if strike else "") + "broken" + (f", dealer GEX dropped {drop:.0f}%" if drop else "")
                            if breach_signal:
                                break
                    if breach_signal:
                        break

            if not breach_signal:
                logger.debug(f"[GAMMA_TRAP] {stock.stock_symbol} — G1 (breach) not met, skip")
                return False

            # ── G2: Volume Surge ──────────────────────────────────────────────
            # Stocks: VolumeAnalyser VOLUME_BREAKOUT / VOLUME_CLIMAX (direction first).
            # Indices: VolumeAnalyser never runs (no index decorators — index spot has no
            # volume), so read the surge from current-month FUTURES volume instead.
            if stock.is_index:
                surge = self._futures_volume_surge(stock, direction)
                if surge:
                    ratio, chg = surge
                    conditions.append("FUTURES_VOLUME_SURGE")
                    volume_signal = "FUTURES_VOLUME_SURGE"
                    triggers["G2 Volume"] = f"futures vol {ratio:.1f}× avg ({chg:+.2f}% bar)"
            else:
                for sentiment in (direction, "BULLISH", "BEARISH"):
                    vb = self._get(stock, sentiment, "VOLUME_BREAKOUT")
                    if self._any(vb):
                        conditions.append("VOLUME_BREAKOUT")
                        volume_signal = "VOLUME_BREAKOUT"
                        vb_item = (vb[0] if isinstance(vb, list) else vb)
                        ratio = getattr(vb_item, "volume_ratio", None)
                        triggers["G2 Volume"] = "BREAKOUT" + (f" {ratio:.1f}× avg" if ratio else "")
                        break
                    vc = self._get(stock, sentiment, "VOLUME_CLIMAX")
                    if self._any(vc):
                        conditions.append("VOLUME_CLIMAX")
                        volume_signal = "VOLUME_CLIMAX"
                        vc_item = (vc[0] if isinstance(vc, list) else vc)
                        ratio = getattr(vc_item, "volume_ratio", None)
                        triggers["G2 Volume"] = "CLIMAX" + (f" {ratio:.1f}× avg" if ratio else "")
                        break

            # ── G3: Futures Fuel ──────────────────────────────────────────────
            # ORB breakout (MTF-aligned preferred) or fresh buildup in the move direction.
            brk = self._futures_breakout(stock, direction)
            if brk is not None:
                mtf = getattr(brk, "mtf_aligned", False)
                conditions.append("FUTURES(FUTURE_BREAKOUT_PATTERN)")
                oi_pct = getattr(brk, "oi_pct", None)
                triggers["G3 Futures"] = ("ORB breakout" + (" MTF-aligned" if mtf else "")
                                          + (f" OI {oi_pct:+.1f}%" if oi_pct else ""))
            else:
                bu = self._futures_buildup(stock, direction)
                if bu is not None:
                    key, subtype, fd_item = bu
                    conditions.append(f"FUTURES({key}:{subtype})")
                    oi_pct = (getattr(fd_item, "oi_percentage", None)
                              or getattr(fd_item, "oi_from_open_pct", None)
                              or getattr(fd_item, "oi_pct", None))
                    label = subtype.replace("_", " ").title()
                    triggers["G3 Futures"] = label + (f" OI {oi_pct:+.1f}%" if oi_pct else "")

            # ── G4: Volatility Expansion (IV rises only — IV_CRUSH is separate) ──
            iv_item = next(
                (i for i in self._items(self._get(stock, "NEUTRAL", "IV_SPIKE"))
                 if getattr(i, "direction", "UP") == "UP"
                 and (getattr(i, "iv_change", None) is None or getattr(i, "iv_change") > 0)),
                None,
            )
            if iv_item is not None:
                conditions.append("IV_SPIKE")
                iv_val = getattr(iv_item, "atm_iv", None) or getattr(iv_item, "iv", None)
                iv_chg = getattr(iv_item, "atm_iv_change", None) or getattr(iv_item, "iv_change", None)
                triggers["G4 IV Spike"] = "IV spike" + (f" ATM IV {iv_val:.1f}" if iv_val else "") + (f" (+{iv_chg:.1f}%)" if iv_chg else "")

            # ── Gate: 3/4 required ────────────────────────────────────────────
            count = len(conditions)
            logger.debug(
                f"[GAMMA_TRAP] {stock.stock_symbol} | "
                f"direction={direction} conditions={count}/4 — {conditions}"
            )
            if count < self.GAMMA_TRAP_MIN_CONDITIONS:
                return False

            mode_label = self._mode_label()
            result = GAMMA_TRAP_NT(
                conditions_met=count,
                conditions_detail=conditions,
                direction=direction,
                breach_signal=breach_signal,
                volume_signal=volume_signal,
                mode=mode_label,
                triggers=triggers,
            )
            stock.set_analysis("NEUTRAL", "GAMMA_TRAP", result._asdict())

            # Write suppression flag — Range Bound checks this before running
            stock.analysis["NEUTRAL"]["GAMMA_TRAP_ACTIVE"] = True

            # Force CRITICAL priority — bypass the score gate
            self._set_priority_override(stock, NotificationPriority.CRITICAL)

            logger.info(
                f"[GAMMA_TRAP] {stock.stock_symbol} — FIRED {direction} "
                f"({count}/4) breach={breach_signal} vol={volume_signal} mode={mode_label}"
            )
            return True

        except Exception as e:
            logger.error(f"[GAMMA_TRAP] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Setup 1 — Range-Bound Premium Setup
    # ─────────────────────────────────────────────────────────────────────────

    def detect_range_bound_premium_setup(self, stock: Stock) -> bool:
        """
        Setup 1: Iron Condor / Strangle candidate.

        Detects a market trapped in a box with overpriced options and no
        institutional directional pressure. Triggers when 5/6 conditions align:

            R1  Overpriced Vol     IV_RANK_EXTREME (VERY_HIGH or HIGH) or
                                   IV_PREMIUM (EXPENSIVE or EXTREME)
            R2  Ceiling & Floor    Put-OI floor below spot AND call-OI ceiling above spot,
                                   derived from the OI chain (centred ranges qualify)
            R3  Neutral Momentum   Absence of VOLUME_BREAKOUT (indices: futures volume
                                   surge) and RSI_DIVERGENCE in any directional bucket
            R4  No Instit. Push    Absence of futures buildup / ORB breakout in any bucket
            R5  Max Pain Magnet    |spot − per_expiry_map[nearest].max_pain_strike| ≤ 1.5%
            R6  GEX Positive       GEX_REGIME POSITIVE with MODERATE/STRONG magnitude

        Suppressed if Gamma Trap fired in the same cycle, or PANIC_MODE /
        PANIC_EXHAUSTION is present (keeps it exclusive with SKEW_FADE).
        """
        try:
            # Hard suppression: do not run if a Gamma Trap is active this cycle
            if stock.analysis.get("NEUTRAL", {}).get("GAMMA_TRAP_ACTIVE"):
                logger.debug(
                    f"[RANGE_BOUND] {stock.stock_symbol} — suppressed by GAMMA_TRAP_ACTIVE"
                )
                return False

            # Panic suppression: an active panic or a panic exhaustion means the market
            # is not in a quiet box. This also keeps RANGE_BOUND and SKEW_FADE (which
            # requires PANIC_EXHAUSTION) mutually exclusive in the same cycle.
            for bucket in ("BULLISH", "BEARISH"):
                for pkey in ("PANIC_MODE", "PANIC_EXHAUSTION"):
                    if self._any(self._get(stock, bucket, pkey)):
                        logger.debug(
                            f"[RANGE_BOUND] {stock.stock_symbol} — suppressed by {pkey} in {bucket}"
                        )
                        return False

            # Price-movement gate: Iron Condor is a neutral strategy. If the stock
            # is moving >2% intraday, it is trending — not range-bound — and an
            # Iron Condor would be dangerous. Skip entirely.
            move_pct = abs(stock.ltp_change_perc) if stock.ltp_change_perc is not None else 0.0
            if move_pct > self.RANGE_BOUND_MAX_MOVE_PCT:
                logger.debug(
                    f"[RANGE_BOUND] {stock.stock_symbol} — suppressed: "
                    f"price move {move_pct:.2f}% > {self.RANGE_BOUND_MAX_MOVE_PCT}% threshold"
                )
                return False

            logger.debug(f"[RANGE_BOUND] {stock.stock_symbol} — start")

            conditions:    list[str] = []
            triggers:      dict[str, str] = {}
            iv_percentile: float | None = None
            iv_trigger:    str | None = None
            iv_hv_ratio:   float | None = None
            iv_premium_pct: float | None = None
            put_wall_strike:  float | None = None
            call_wall_strike: float | None = None
            max_pain_dev:     float | None = None

            # ── R1: Overpriced Volatility ─────────────────────────────────────
            ivp_ok = False
            for key in ("IV_RANK_EXTREME", "IV_RANK"):
                item = self._get(stock, "NEUTRAL", key)
                if item:
                    items = item if isinstance(item, list) else [item]
                    for i in items:
                        cat = getattr(i, "category", "")
                        ivp = getattr(i, "iv_percentile", None)
                        if cat in ("VERY_HIGH", "HIGH") or (ivp is not None and ivp > 70):
                            ivp_ok = True
                            iv_percentile = ivp
                            iv_trigger = key
                            atm_iv = getattr(i, "atm_iv", None)
                            metric = f"IVP {ivp:.0f}th percentile ({cat})" if ivp else cat
                            if atm_iv:
                                metric += f" ATM IV {atm_iv:.1f}"
                            triggers["R1 IV"] = metric
                            break
                if ivp_ok:
                    break

            if not ivp_ok:
                iv_prem = self._get(stock, "NEUTRAL", "IV_PREMIUM")
                if iv_prem:
                    items = iv_prem if isinstance(iv_prem, list) else [iv_prem]
                    for i in items:
                        zone = getattr(i, "zone", "")
                        if zone in ("EXPENSIVE", "EXTREME"):
                            ivp_ok = True
                            iv_trigger = "IV_PREMIUM"
                            iv_hv_ratio = getattr(i, "iv_hv_ratio", None)
                            iv_premium_pct = getattr(i, "iv_premium_pct", None)
                            atm_iv = getattr(i, "atm_iv", None)
                            hv = getattr(i, "hv", None)
                            metric = f"{zone}"
                            if iv_hv_ratio:
                                metric += f" IV/HV {iv_hv_ratio:.2f}x"
                            if iv_premium_pct:
                                metric += f" ({iv_premium_pct:.0f}% above HV)"
                            if atm_iv and hv:
                                metric += f" ATM IV {atm_iv:.1f} vs HV {hv:.1f}"
                            triggers["R1 IV"] = metric
                            break

            if ivp_ok:
                conditions.append("OVERPRICED_VOL")
            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | R1 OVERPRICED_VOL={ivp_ok} "
                f"ivp={iv_percentile}"
            )

            # ── R2: Ceiling (call wall) AND Floor (put wall) ──────────────────
            # Walls are derived DIRECTLY from the OI chain: OIChainAnalyser's OI_WALL
            # only emits when the two walls are asymmetric (a directional signal), so a
            # perfectly centred range — the ideal iron condor — never produced OI_WALL.
            #   floor   = max put-OI strike below spot  (>= MIN_OI_MULT × avg put OI)
            #   ceiling = max call-OI strike above spot (>= MIN_OI_MULT × avg call OI)
            # Fallback (no chain snapshot): nearest_put_wall / nearest_call_wall from any
            # OI_WALL entry.
            has_floor   = False
            has_ceiling = False
            wall_source = None
            per_strike = self._per_strike(stock)
            spot = self._spot(stock)
            if per_strike and spot:
                floor, ceil = self._chain_range_walls(per_strike, spot)
                if floor:
                    has_floor, put_wall_strike = True, floor[0]
                if ceil:
                    has_ceiling, call_wall_strike = True, ceil[0]
                wall_source = "oi_chain"
            else:
                for bucket in ("BULLISH", "BEARISH"):
                    for w in self._items(self._get(stock, bucket, "OI_WALL")):
                        npp = getattr(w, "nearest_put_wall", None)
                        ncp = getattr(w, "nearest_call_wall", None)
                        if npp and put_wall_strike is None:
                            has_floor, put_wall_strike = True, npp[0]
                        if ncp and call_wall_strike is None:
                            has_ceiling, call_wall_strike = True, ncp[0]
                wall_source = "oi_wall_signal"

            both_walls = has_floor and has_ceiling
            if both_walls:
                conditions.append("CEILING_AND_FLOOR")
                triggers["R2 OI Walls"] = (
                    f"Put floor {put_wall_strike:.0f} / Call ceiling {call_wall_strike:.0f}"
                )

            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | R2 WALLS source={wall_source} spot={spot} "
                f"floor={has_floor} ceiling={has_ceiling} "
                f"put_wall={put_wall_strike} call_wall={call_wall_strike}"
            )

            # ── R3: Neutral Momentum — absence of strong directional signals ──
            # Momentum is neutral when there is no volume breakout AND no RSI divergence
            # (which is the highest-conviction technical reversal signal, PF >1.5).
            # Indices: VOLUME_BREAKOUT never runs, so use a futures volume surge instead.
            vol_breakout_present = (
                self._any(self._get(stock, "BULLISH", "VOLUME_BREAKOUT"))
                or self._any(self._get(stock, "BEARISH", "VOLUME_BREAKOUT"))
            )
            if stock.is_index and not vol_breakout_present:
                vol_breakout_present = self._futures_volume_surge(stock, None) is not None
            rsi_div_present = (
                self._any(self._get(stock, "BULLISH", "RSI_DIVERGENCE"))
                or self._any(self._get(stock, "BEARISH", "RSI_DIVERGENCE"))
            )
            neutral_momentum = not vol_breakout_present and not rsi_div_present
            if neutral_momentum:
                conditions.append("NEUTRAL_MOMENTUM")
                triggers["R3 Momentum"] = "no volume breakout, no RSI divergence"
            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | R3 NEUTRAL_MOMENTUM={neutral_momentum} "
                f"vol_breakout={vol_breakout_present} rsi_div={rsi_div_present}"
            )

            # ── R4: No Institutional Directional Push ─────────────────────────
            # Absence of fresh futures positioning (FUTURE_ACTION long/short buildup on the
            # current expiry, FUTURE_OI_FROM_OPEN buildup, FUTURE_PVO_PATTERN buildup) and
            # of an ORB breakout (FUTURE_BREAKOUT_PATTERN). Short covering / long unwinding
            # are acceptable — they are position exits, not new directional conviction.
            push = None
            for d in ("BULLISH", "BEARISH"):
                bu = self._futures_buildup(stock, d)
                if bu is not None:
                    push = f"{bu[0]}:{bu[1]}"
                    break
                if self._futures_breakout(stock, d) is not None:
                    push = f"FUTURE_BREAKOUT_PATTERN:{d}"
                    break
            no_instit_push = push is None
            if no_instit_push:
                conditions.append("NO_INSTIT_PUSH")
                triggers["R4 Futures"] = "no futures buildup / breakout"
            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | R4 NO_INSTIT_PUSH={no_instit_push} push={push}"
            )

            # ── R5: Max Pain Magnet ───────────────────────────────────────────
            # Deviation computed directly from Sensibull per_expiry_map[nearest].max_pain_strike
            # and spot. The MAX_PAIN signal itself only exists when |dev| is LARGE (>=2-3%),
            # so it can never satisfy a <= 1.5% proximity test.
            mp_ok = False
            mp_strike = self._max_pain_strike(stock)
            if mp_strike and spot:
                max_pain_dev = (spot - mp_strike) / mp_strike * 100
                mp_ok = abs(max_pain_dev) <= self.MAX_PAIN_PROXIMITY_PCT
            if mp_ok:
                conditions.append("MAX_PAIN_MAGNET")
                direction_word = "above" if (max_pain_dev or 0) > 0 else "below"
                triggers["R5 MaxPain"] = (
                    f"{abs(max_pain_dev):.1f}% {direction_word} max pain {mp_strike:.0f}"
                )
            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | R5 MAX_PAIN_MAGNET={mp_ok} "
                f"SOURCE max_pain_strike={mp_strike} spot={spot} dev={max_pain_dev}"
            )

            # ── R6: GEX Supports Range ────────────────────────────────────────
            # Positive GEX (MODERATE or STRONG) means dealers are long gamma —
            # they actively dampen moves, creating the ideal premium-selling environment.
            gex_supports = False
            gex_regime_data = self._get(stock, "NEUTRAL", "GEX_REGIME")
            if gex_regime_data:
                items = gex_regime_data if isinstance(gex_regime_data, list) else [gex_regime_data]
                for g in items:
                    if (getattr(g, "regime", "") == "POSITIVE"
                            and getattr(g, "magnitude", "") in ("MODERATE", "STRONG")):
                        gex_supports = True
                        break
            if gex_supports:
                conditions.append("GEX_POSITIVE_REGIME")
                gex_data = gex_regime_data if not isinstance(gex_regime_data, list) else gex_regime_data[0]
                gex_total = getattr(gex_data, "gex_total", None)
                gex_mag = getattr(gex_data, "magnitude", "")
                triggers["R6 GEX"] = f"POSITIVE {gex_mag}" + (f" ({gex_total:+.0f} Cr)" if gex_total is not None else "")
            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | R6 GEX_POSITIVE_REGIME={gex_supports}"
            )

            # ── Gate: 5/6 required (R6 adds confidence) ──────────────────────
            count = len(conditions)
            logger.debug(
                f"[RANGE_BOUND] {stock.stock_symbol} | "
                f"conditions={count}/6 — {conditions}"
            )
            if count < self.RANGE_BOUND_MIN_CONDITIONS:
                return False

            # Determine setup type: IRON_CONDOR only when we have both walls as
            # discrete strikes to define the short-strike legs; else STRANGLE.
            setup_type = "IRON_CONDOR" if (put_wall_strike and call_wall_strike) else "STRANGLE"

            result = RANGE_BOUND_SETUP_NT(
                conditions_met=count,
                conditions_detail=conditions,
                iv_percentile=iv_percentile,
                iv_trigger=iv_trigger,
                iv_hv_ratio=iv_hv_ratio,
                iv_premium_pct=iv_premium_pct,
                put_wall_strike=put_wall_strike,
                call_wall_strike=call_wall_strike,
                max_pain_dev_pct=max_pain_dev,
                setup_type=setup_type,
                mode=self._mode_label(),
                gex_supports=gex_supports,
                triggers=triggers,
            )
            stock.set_analysis("NEUTRAL", "RANGE_BOUND_SETUP", result._asdict())
            self._set_priority_override(stock, NotificationPriority.HIGH)

            logger.info(
                f"[RANGE_BOUND] {stock.stock_symbol} — FIRED {setup_type} "
                f"({count}/6) walls=[{put_wall_strike}, {call_wall_strike}] "
                f"ivp={iv_percentile} mp_dev={max_pain_dev} gex_supports={gex_supports}"
            )
            return True

        except Exception as e:
            logger.error(f"[RANGE_BOUND] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Setup 2 — Volatility Skew Fade Setup
    # ─────────────────────────────────────────────────────────────────────────

    def detect_skew_fade_setup(self, stock: Stock) -> bool:
        """
        Setup 2: Directional credit spread — sell the overpriced side at exhaustion.

        ALL 3 conditions must align (strict gate):

            S1  Exhaustion         PANIC_EXHAUSTION is active.
                                   Read .panic_direction to determine the faded side.
                                   PANIC_EXHAUSTION is stored in the CONTRARIAN bucket:
                                   a bearish panic exhaustion → stored in BULLISH.

            S2  Brick Wall         Spot within ≤ SR_PROXIMITY_PCT of a dominant, holding OI
                                   level from the chain (put-OI support for a BULLISH fade,
                                   call-OI resistance for a BEARISH fade)
                                   AND a high-quality reversal candle (from REVERSAL_CANDLE_KEYS)
                                   exists in the fade-direction bucket.

        Suppressed if Gamma Trap fired in the same cycle.

            S3  PCR Trap           PCR_EXTREME is active (sentiment is at an extreme)
                                   AND PCR_REVERSAL (intraday) or PCR_POS_REVERSAL (positional)
                                   fired in the FADE direction (opposite to the extreme zone).

        Direction logic:
            panic_direction = BEARISH  → fade = BULLISH  (sell puts / buy put credit spread)
            panic_direction = BULLISH  → fade = BEARISH  (sell calls / buy call credit spread)
        """
        try:
            # A live Gamma Trap is a kill-switch for short premium — never open a
            # credit spread into it (would also give paper trading an entry + exit).
            if stock.analysis.get("NEUTRAL", {}).get("GAMMA_TRAP_ACTIVE"):
                logger.debug(f"[SKEW_FADE] {stock.stock_symbol} — suppressed by GAMMA_TRAP_ACTIVE")
                return False

            logger.debug(f"[SKEW_FADE] {stock.stock_symbol} — start")

            # ── S1: PANIC_EXHAUSTION — check both buckets ─────────────────────
            # Stored in contrarian bucket: bearish panic → BULLISH bucket.
            exhaustion_data = None
            for bucket in ("BULLISH", "BEARISH"):
                ex = self._get(stock, bucket, "PANIC_EXHAUSTION")
                if self._any(ex):
                    exhaustion_data = ex
                    break

            if exhaustion_data is None:
                logger.debug(f"[SKEW_FADE] {stock.stock_symbol} — S1 (exhaustion) not present")
                return False

            # Extract fields from the (possibly list-wrapped) namedtuple
            ex_item = exhaustion_data[0] if isinstance(exhaustion_data, list) else exhaustion_data
            panic_direction     = getattr(ex_item, "panic_direction",  None)
            exhaustion_conf     = getattr(ex_item, "confidence",       "MODERATE")

            # The trade direction is opposite to the panic
            if panic_direction == "BEARISH":
                fade_direction = "BULLISH"
            elif panic_direction == "BULLISH":
                fade_direction = "BEARISH"
            else:
                logger.debug(
                    f"[SKEW_FADE] {stock.stock_symbol} — "
                    f"S1 panic_direction unknown ({panic_direction}), skip"
                )
                return False

            logger.debug(
                f"[SKEW_FADE] {stock.stock_symbol} | S1 EXHAUSTION OK "
                f"panic={panic_direction} fade={fade_direction} conf={exhaustion_conf}"
            )

            # ── S2: Brick Wall — holding OI level + reversal candle ───────────
            # Computed from the OI chain directly. OIChainAnalyser's OI_SUPPORT_RESISTANCE
            # is BREACH-only in its directional buckets (BULLISH = resistance breached,
            # BEARISH = support breached), the opposite of the "wall holding" this setup
            # needs, so it is not used here.
            #   BULLISH fade → spot testing a dominant put-OI support that is holding
            #   BEARISH fade → spot testing a dominant call-OI resistance that is holding
            per_strike = self._per_strike(stock)
            spot = self._spot(stock)
            if not per_strike or not spot:
                logger.debug(f"[SKEW_FADE] {stock.stock_symbol} — S2 no OI chain/spot, skip")
                return False
            level = self._holding_sr_level(per_strike, spot, fade_direction)
            if level is None:
                logger.debug(
                    f"[SKEW_FADE] {stock.stock_symbol} — S2 no holding "
                    f"{'support' if fade_direction == 'BULLISH' else 'resistance'} within "
                    f"{self.SR_PROXIMITY_PCT}% of spot={spot}, skip"
                )
                return False
            test_strike, _level_oi, proximity_pct = level

            # Reversal candle must be in the SAME bucket as the SR (= fade direction)
            # to confirm that the level is holding and price is reversing.
            confirming_candle: str | None = None
            for candle_key in self.REVERSAL_CANDLE_KEYS:
                if self._any(self._get(stock, fade_direction, candle_key)):
                    confirming_candle = candle_key
                    break

            if confirming_candle is None:
                logger.debug(
                    f"[SKEW_FADE] {stock.stock_symbol} — "
                    f"S2 no reversal candle in {fade_direction}, skip"
                )
                return False

            logger.debug(
                f"[SKEW_FADE] {stock.stock_symbol} | S2 BRICK_WALL OK "
                f"strike={test_strike} proximity={proximity_pct:.2f}% candle={confirming_candle}"
            )

            # ── S3: PCR Trap — extreme reading + reversal in fade direction ───
            # PCR_EXTREME firing means the crowd is extremely positioned on one side
            # (contrarian signal). We need the corresponding PCR reversal in the
            # fade direction to confirm smart money is beginning to fade the crowd.

            # Determine the PCR reversal key based on mode
            pcr_reversal_key = "PCR_REVERSAL" if self._is_intraday() else "PCR_POS_REVERSAL"

            # PCR_EXTREME must be present (any bucket is fine — it fires where the
            # contrarian sentiment is, which aligns with the fade direction)
            pcr_extreme_present = (
                self._any(self._get(stock, "BULLISH",  "PCR_EXTREME"))
                or self._any(self._get(stock, "BEARISH", "PCR_EXTREME"))
                or self._any(self._get(stock, "NEUTRAL", "PCR_EXTREME"))
            )
            if not pcr_extreme_present:
                logger.debug(
                    f"[SKEW_FADE] {stock.stock_symbol} — S3 PCR_EXTREME not present, skip"
                )
                return False

            # PCR reversal must fire in the fade direction bucket
            pcr_reversal_present = self._any(
                self._get(stock, fade_direction, pcr_reversal_key)
            )
            if not pcr_reversal_present:
                # Also accept PCR_INTRADAY_TREND as confirmation in intraday mode
                if self._is_intraday():
                    pcr_reversal_present = self._any(
                        self._get(stock, fade_direction, "PCR_INTRADAY_TREND")
                    )
                    if pcr_reversal_present:
                        pcr_reversal_key = "PCR_INTRADAY_TREND"

            if not pcr_reversal_present:
                logger.debug(
                    f"[SKEW_FADE] {stock.stock_symbol} — "
                    f"S3 {pcr_reversal_key} not in {fade_direction}, skip"
                )
                return False

            logger.debug(
                f"[SKEW_FADE] {stock.stock_symbol} | S3 PCR_TRAP OK "
                f"pcr_key={pcr_reversal_key} fade_dir={fade_direction}"
            )

            # ── All 3 conditions met ──────────────────────────────────────────
            _candle_labels = {
                "Double_candle_stick_pattern":    "Engulfing/Piercing",
                "Single_candle_reversal_pattern": "Hammer/Shooting Star",
                "Triple_candle_stick_pattern":    "Morning/Evening Star",
                "Triple_candle_reversal_pattern": "Triple reversal",
            }
            _pcr_labels = {
                "PCR_REVERSAL":       "PCR zone crossover",
                "PCR_POS_REVERSAL":   "PCR 3-day reversal",
                "PCR_INTRADAY_TREND": "PCR intraday trend",
            }
            ex_ivp = getattr(ex_item, "iv_percentile", None)
            triggers = {
                "S1 Exhaustion": f"{panic_direction} panic {exhaustion_conf} confidence"
                    + (f" (IVP {ex_ivp:.0f}th)" if ex_ivp else ""),
                "S2 SR Wall": f"OI wall {test_strike:.0f}, {proximity_pct:.2f}% away, "
                    + _candle_labels.get(confirming_candle, confirming_candle),
                "S3 PCR Trap": _pcr_labels.get(pcr_reversal_key, pcr_reversal_key)
                    + f" in {fade_direction}",
            }

            result = SKEW_FADE_SETUP_NT(
                conditions_met=3,
                fade_direction=fade_direction,
                panic_direction=panic_direction,
                exhaustion_confidence=exhaustion_conf,
                sr_level=test_strike,
                sr_proximity_pct=round(proximity_pct, 3),
                candle_key=confirming_candle,
                pcr_signal=pcr_reversal_key,
                mode=self._mode_label(),
                triggers=triggers,
            )
            stock.set_analysis("NEUTRAL", "SKEW_FADE_SETUP", result._asdict())
            self._set_priority_override(stock, NotificationPriority.HIGH)

            logger.info(
                f"[SKEW_FADE] {stock.stock_symbol} — FIRED "
                f"fade={fade_direction} panic={panic_direction} "
                f"sr={test_strike} prox={proximity_pct:.2f}% "
                f"candle={confirming_candle} pcr={pcr_reversal_key} "
                f"exhaustion_conf={exhaustion_conf}"
            )
            return True

        except Exception as e:
            logger.error(f"[SKEW_FADE] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False
