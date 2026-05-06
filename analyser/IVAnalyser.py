import traceback
import numpy as np
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple

# ── module-level namedtuples ──────────────────────────────────────────────────
IV_SPIKE_NT    = namedtuple("IV_SPIKE",   ["expiry", "iv", "iv_change", "source"])
IV_TREND_NT    = namedtuple("IV_TREND",   ["expiry", "trend", "iv_change_pct", "atm_iv"])
IV_RANK_NT     = namedtuple("IV_RANK",    ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
IV_PREMIUM_NT  = namedtuple("IV_PREMIUM", [
    "hv", "atm_iv", "iv_hv_ratio", "iv_premium_pct",
    "zone", "expiry", "hv_period", "signal",
])

# ── helpers ───────────────────────────────────────────────────────────────────

def _to_pct(v: float) -> float:
    """Sensibull returns IV / IVP on a 0-1 scale. Convert to percentage (0-100)."""
    return v * 100 if v < 1.0 else v


class IVAnalyser(BaseAnalyzer):
    IV_PERCENTAGE_CHANGE = 30

    def __init__(self) -> None:
        self.analyserName = "IV Analyser"
        super().__init__()

    def reset_constants(self):
        IVAnalyser.IV_TREND_CONTINUATION_DAYS = 3
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            IVAnalyser.IV_PERCENTAGE_CHANGE      = 5
            IVAnalyser.IV_TREND_PERCENTAGE_CHANGE = 8
            IVAnalyser.IV_HV_ELEVATED_RATIO      = 1.3
            IVAnalyser.IV_HV_EXPENSIVE_RATIO     = 1.6
            IVAnalyser.IV_HV_EXTREME_RATIO       = 2.0
            IVAnalyser.IV_HV_MIN_BARS            = 50
            IVAnalyser.IV_HV_PERIOD_BARS         = 50
        else:
            IVAnalyser.IV_PERCENTAGE_CHANGE      = 20
            IVAnalyser.IV_TREND_PERCENTAGE_CHANGE = 20
            IVAnalyser.IV_HV_ELEVATED_RATIO      = 1.2
            IVAnalyser.IV_HV_EXPENSIVE_RATIO     = 1.5
            IVAnalyser.IV_HV_EXTREME_RATIO       = 2.0
            IVAnalyser.IV_HV_MIN_BARS            = 21
            IVAnalyser.IV_HV_PERIOD_BARS         = 20

        logger.debug(
            f"[IVAnalyser] constants reset | mode={shared.app_ctx.mode.name} "
            f"IV_PCT_CHG={IVAnalyser.IV_PERCENTAGE_CHANGE} "
            f"IV_TREND_PCT_CHG={IVAnalyser.IV_TREND_PERCENTAGE_CHANGE} "
            f"HV_RATIOS=elevated:{IVAnalyser.IV_HV_ELEVATED_RATIO}/"
            f"expensive:{IVAnalyser.IV_HV_EXPENSIVE_RATIO}/"
            f"extreme:{IVAnalyser.IV_HV_EXTREME_RATIO}"
        )

    def _get_nearest_expiry_columns(self, stock):
        """
        Returns (nearest_expiry, atm_iv_col, atm_iv_pct_col) from per_expiry_map.
        Returns (None, None, None) if unavailable.
        """
        try:
            per_expiry = (
                stock.sensibull_ctx.get("current", {})
                .get("stats", {})
                .get("per_expiry_map", {})
            )
            if not per_expiry:
                return None, None, None
            nearest = sorted(per_expiry.keys())[0]
            suffix = nearest.replace("-", "")
            return nearest, f"atm_iv_{suffix}", f"atm_iv_percentile_{suffix}"
        except Exception:
            return None, None, None

    # ── analyse_spike_in_ATM_IV ───────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_spike_in_ATM_IV(self, stock: Stock):
        """
        Detects an IV spike using Sensibull's atm_iv_change field (change vs prev close)
        from per_expiry_map. Falls back to comparing last 2 historical_data snapshots
        when atm_iv_change is unavailable (intraday path).

        SOURCE DATA (DEBUG):   raw atm_iv, atm_iv_change per expiry from per_expiry_map
        ANALYSER INPUT (DEBUG): normalised iv values after decimal→% conversion
        CONDITION (DEBUG):     abs(iv_change_pct) vs IV_PERCENTAGE_CHANGE threshold
        """
        try:
            logger.debug(f"[IV_SPIKE] {stock.stock_symbol} — start")

            # ── source data ───────────────────────────────────────────────────
            per_expiry = (
                stock.sensibull_ctx.get("current", {})
                .get("stats", {})
                .get("per_expiry_map", {})
            )
            if not per_expiry:
                logger.debug(f"[IV_SPIKE] {stock.stock_symbol} — no per_expiry_map, skip")
                return False

            is_intraday = shared.app_ctx.mode.name == shared.Mode.INTRADAY.name

            # Expiry selection strategy:
            #   Stocks  — nearest expiry only (both modes).
            #             Far-expiry IV data for stocks is thin and unreliable.
            #   Indices — all available expiries (both modes).
            #             NIFTY/BANKNIFTY have deep liquidity across weeklies and
            #             monthlies; a spike on the far leg signals event positioning.
            #             The prev_iv <= 0 guard below handles any corrupt entries.
            if not stock.is_index:
                nearest = sorted(per_expiry.keys())[0]
                per_expiry = {nearest: per_expiry[nearest]}

            logger.debug(
                f"[IV_SPIKE] {stock.stock_symbol} ({'index' if stock.is_index else 'stock'}) — "
                f"source: {len(per_expiry)} expir(ies): {list(per_expiry.keys())}"
            )

            res = False

            for expiry, expiry_data in per_expiry.items():
                raw_atm_iv     = expiry_data.get("atm_iv")
                raw_iv_change  = expiry_data.get("atm_iv_change")

                # ── log what we received ──────────────────────────────────────
                logger.debug(
                    f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} | "
                    f"raw atm_iv={raw_atm_iv} raw atm_iv_change={raw_iv_change} "
                    f"mode={'intraday' if is_intraday else 'positional'}"
                )

                if is_intraday:
                    # Derive the historical_data column for this specific expiry.
                    # For stocks the loop already has only one expiry (nearest).
                    # For indices we iterate all expiries, each with its own column.
                    iv_col = f"atm_iv_{expiry.replace('-', '')}"

                    hist_df = stock.sensibull_ctx.get("historical_data")
                    if hist_df is None or hist_df.empty or iv_col not in hist_df.columns:
                        logger.debug(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"no historical_data or column '{iv_col}' missing, skip"
                        )
                        continue

                    iv_series = hist_df[iv_col].dropna()
                    logger.debug(
                        f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                        f"historical IV series ({len(iv_series)} rows): "
                        f"{iv_series.tail(3).tolist()}"
                    )

                    if len(iv_series) < 2:
                        logger.info(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"insufficient history ({len(iv_series)} rows, need 2), skip"
                        )
                        continue

                    prev_iv = iv_series.iloc[-2]
                    curr_iv = iv_series.iloc[-1]
                    if prev_iv == 0:
                        logger.debug(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"prev_iv=0, skip"
                        )
                        continue

                    iv_change_pct = ((curr_iv - prev_iv) / prev_iv) * 100
                    display_iv    = _to_pct(curr_iv)

                    # ── condition evaluation ──────────────────────────────────
                    logger.debug(
                        f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} | "
                        f"INPUT prev_iv={prev_iv:.4f} curr_iv={curr_iv:.4f} "
                        f"(display={display_iv:.1f}%) | "
                        f"CONDITION |iv_change_pct|={abs(iv_change_pct):.2f}% "
                        f">= threshold={IVAnalyser.IV_PERCENTAGE_CHANGE}% → "
                        f"{'PASS' if abs(iv_change_pct) >= IVAnalyser.IV_PERCENTAGE_CHANGE else 'FAIL'}"
                    )

                    if abs(iv_change_pct) >= IVAnalyser.IV_PERCENTAGE_CHANGE:
                        stock.set_analysis("NEUTRAL", "IV_SPIKE", IV_SPIKE_NT(
                            expiry=expiry, iv=display_iv,
                            iv_change=iv_change_pct, source="sensibull_5min",
                        ))
                        logger.info(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"SIGNAL EMITTED | ATM IV={display_iv:.1f}% "
                            f"5-min change={iv_change_pct:+.2f}%"
                        )
                        res = True

                else:
                    # Positional: use Sensibull's pre-computed atm_iv_change vs prev close
                    if raw_atm_iv is None or raw_iv_change is None or raw_atm_iv <= 0:
                        logger.debug(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"missing atm_iv or atm_iv_change, skip"
                        )
                        continue

                    # Normalise decimal → % when atm_iv is on 0-1 scale.
                    # Scale both atm_iv and atm_iv_change together so the
                    # relationship (prev_iv = atm_iv - atm_iv_change) stays intact.
                    if raw_atm_iv < 1.0:
                        atm_iv    = raw_atm_iv * 100
                        iv_change = raw_iv_change * 100
                    else:
                        atm_iv    = raw_atm_iv
                        iv_change = raw_iv_change

                    prev_iv = atm_iv - iv_change

                    # prev_iv must be positive — a zero or negative value means
                    # Sensibull's atm_iv_change is corrupt (e.g. roll artefact
                    # where the reported change exceeds the current IV itself).
                    if prev_iv <= 0:
                        logger.debug(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"prev_iv={prev_iv:.2f}% <= 0 (corrupt atm_iv_change={iv_change:+.2f}%), skip"
                        )
                        continue

                    iv_change_pct = (iv_change / prev_iv) * 100

                    # ── condition evaluation ──────────────────────────────────
                    logger.debug(
                        f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} | "
                        f"INPUT atm_iv={atm_iv:.1f}% (raw={raw_atm_iv}) "
                        f"iv_change={iv_change:+.2f}% (raw={raw_iv_change}) "
                        f"prev_iv={prev_iv:.1f}% | "
                        f"CONDITION |iv_change_pct|={abs(iv_change_pct):.2f}% "
                        f">= threshold={IVAnalyser.IV_PERCENTAGE_CHANGE}% → "
                        f"{'PASS' if abs(iv_change_pct) >= IVAnalyser.IV_PERCENTAGE_CHANGE else 'FAIL'}"
                    )

                    if abs(iv_change_pct) >= IVAnalyser.IV_PERCENTAGE_CHANGE:
                        stock.set_analysis("NEUTRAL", "IV_SPIKE", IV_SPIKE_NT(
                            expiry=expiry, iv=atm_iv,
                            iv_change=iv_change_pct, source="sensibull_change",
                        ))
                        logger.info(
                            f"[IV_SPIKE] {stock.stock_symbol} expiry={expiry} — "
                            f"SIGNAL EMITTED | ATM IV={atm_iv:.1f}% "
                            f"daily change={iv_change_pct:+.2f}%"
                        )
                        res = True

            if not res:
                logger.debug(f"[IV_SPIKE] {stock.stock_symbol} — no spike detected across all expiries")
            return res

        except Exception as e:
            logger.error(f"[IV_SPIKE] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── analyse_trend_in_ATM_IV ───────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_trend_in_ATM_IV(self, stock: Stock):
        """
        Detects a sustained IV trend (rising or falling) over the last N snapshots
        using Sensibull historical_data atm_iv column for the nearest expiry.

        SOURCE DATA (DEBUG):   IV series tail from historical_data
        ANALYSER INPUT (DEBUG): last N values used for trend check
        CONDITION (DEBUG):     monotonic rising/falling + iv_change_pct vs threshold
        """
        try:
            logger.debug(f"[IV_TREND] {stock.stock_symbol} — start")

            nearest_expiry, iv_col, _ = self._get_nearest_expiry_columns(stock)
            if iv_col is None:
                logger.debug(
                    f"[IV_TREND] {stock.stock_symbol} — no expiry data in per_expiry_map, skip"
                )
                return False

            # ── source data ───────────────────────────────────────────────────
            hist_df = stock.sensibull_ctx.get("historical_data")
            if hist_df is None or hist_df.empty or iv_col not in hist_df.columns:
                logger.debug(
                    f"[IV_TREND] {stock.stock_symbol} — "
                    f"no historical_data or column '{iv_col}' missing, skip"
                )
                return False

            n         = IVAnalyser.IV_TREND_CONTINUATION_DAYS
            iv_series = hist_df[iv_col].dropna()

            logger.debug(
                f"[IV_TREND] {stock.stock_symbol} expiry={nearest_expiry} | "
                f"SOURCE column='{iv_col}' total_rows={len(iv_series)} "
                f"last_5={iv_series.tail(5).tolist()}"
            )

            if len(iv_series) < n:
                logger.info(
                    f"[IV_TREND] {stock.stock_symbol} — "
                    f"insufficient history ({len(iv_series)} rows, need {n}), skip"
                )
                return False

            ivs        = iv_series.iloc[-n:].tolist()
            display_iv = _to_pct(ivs[-1])

            if ivs[0] == 0:
                logger.debug(f"[IV_TREND] {stock.stock_symbol} — ivs[0]=0, skip")
                return False

            iv_change_pct = ((ivs[-1] - ivs[0]) / ivs[0]) * 100
            is_rising  = all(ivs[i] < ivs[i + 1] for i in range(n - 1))
            is_falling = all(ivs[i] > ivs[i + 1] for i in range(n - 1))

            # ── analyser input + condition evaluation ─────────────────────────
            logger.debug(
                f"[IV_TREND] {stock.stock_symbol} expiry={nearest_expiry} | "
                f"INPUT last_{n}_ivs={[f'{v:.4f}' for v in ivs]} "
                f"iv_change_pct={iv_change_pct:+.2f}% | "
                f"CONDITION monotonic_rising={is_rising} monotonic_falling={is_falling} "
                f"|change|={abs(iv_change_pct):.2f}% >= threshold={IVAnalyser.IV_TREND_PERCENTAGE_CHANGE}% → "
                f"{'PASS' if (is_rising or is_falling) and abs(iv_change_pct) >= IVAnalyser.IV_TREND_PERCENTAGE_CHANGE else 'FAIL'}"
            )

            res = False
            if is_rising and abs(iv_change_pct) >= IVAnalyser.IV_TREND_PERCENTAGE_CHANGE:
                stock.set_analysis("NEUTRAL", "IV_TREND", IV_TREND_NT(
                    expiry=nearest_expiry, trend="UPWARD",
                    iv_change_pct=iv_change_pct, atm_iv=display_iv,
                ))
                logger.info(
                    f"[IV_TREND] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"SIGNAL EMITTED UPWARD | ATM IV={display_iv:.1f}% "
                    f"change={iv_change_pct:+.2f}% over {n} snapshots"
                )
                res = True
            elif is_falling and abs(iv_change_pct) >= IVAnalyser.IV_TREND_PERCENTAGE_CHANGE:
                stock.set_analysis("NEUTRAL", "IV_TREND", IV_TREND_NT(
                    expiry=nearest_expiry, trend="DOWNWARD",
                    iv_change_pct=iv_change_pct, atm_iv=display_iv,
                ))
                logger.info(
                    f"[IV_TREND] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"SIGNAL EMITTED DOWNWARD | ATM IV={display_iv:.1f}% "
                    f"change={iv_change_pct:+.2f}% over {n} snapshots"
                )
                res = True
            else:
                logger.debug(
                    f"[IV_TREND] {stock.stock_symbol} — no qualifying trend "
                    f"(rising={is_rising}, falling={is_falling}, "
                    f"change={iv_change_pct:+.2f}%, threshold={IVAnalyser.IV_TREND_PERCENTAGE_CHANGE}%)"
                )

            return res

        except Exception as e:
            logger.error(f"[IV_TREND] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── analyse_iv_rank ───────────────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_iv_rank(self, stock: Stock):
        """
        Emits IV_RANK or IV_RANK_EXTREME based on Sensibull's atm_iv_percentile.

        IVP < 10   → IV_RANK_EXTREME VERY_LOW   — historically cheapest
        IVP < 20   → IV_RANK LOW                — cheap options
        IVP 20-70  → no signal
        IVP > 70   → IV_RANK HIGH               — expensive options
        IVP > 85   → IV_RANK_EXTREME VERY_HIGH  — IV crush risk

        SOURCE DATA (DEBUG):   raw atm_iv, atm_iv_percentile, atm_ivp_type from API
        ANALYSER INPUT (DEBUG): normalised ivp (0-100 scale) after decimal conversion
        CONDITION (DEBUG):     ivp vs zone thresholds (10/20/70/85)
        """
        try:
            logger.debug(f"[IV_RANK] {stock.stock_symbol} — start")

            per_expiry = (
                stock.sensibull_ctx.get("current", {})
                .get("stats", {})
                .get("per_expiry_map", {})
            )
            if not per_expiry:
                logger.debug(f"[IV_RANK] {stock.stock_symbol} — no per_expiry_map, skip")
                return False

            # Only nearest expiry for rank signal
            nearest_expiry = sorted(per_expiry.keys())[0]
            expiry_data    = per_expiry[nearest_expiry]

            raw_atm_iv  = expiry_data.get("atm_iv")
            raw_ivp     = expiry_data.get("atm_iv_percentile")
            ivp_type    = expiry_data.get("atm_ivp_type", "")

            # ── source data ───────────────────────────────────────────────────
            logger.debug(
                f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} | "
                f"SOURCE raw_atm_iv={raw_atm_iv} raw_ivp={raw_ivp} ivp_type='{ivp_type}'"
            )

            if raw_atm_iv is None or raw_ivp is None:
                logger.debug(
                    f"[IV_RANK] {stock.stock_symbol} — "
                    f"atm_iv or atm_iv_percentile missing, skip"
                )
                return False

            # Normalise decimal → % (0.69 → 69.0, 0.30 → 30.0)
            atm_iv = _to_pct(raw_atm_iv)
            ivp    = _to_pct(raw_ivp)

            # ── analyser input + condition evaluation ─────────────────────────
            logger.debug(
                f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} | "
                f"INPUT atm_iv={atm_iv:.1f}% (raw={raw_atm_iv}) "
                f"ivp={ivp:.1f} (raw={raw_ivp}) | "
                f"CONDITION thresholds: VERY_HIGH>85 HIGH>70 NORMAL[20-70] LOW<20 VERY_LOW<10"
            )

            res = False
            if ivp > 85:
                stock.set_analysis("NEUTRAL", "IV_RANK_EXTREME", IV_RANK_NT(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="VERY_HIGH", ivp_type=ivp_type,
                ))
                logger.info(
                    f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"SIGNAL IV_RANK_EXTREME VERY_HIGH | IVP={ivp:.1f} ATM IV={atm_iv:.1f}% "
                    f"CONDITION ivp={ivp:.1f} > 85 ✓"
                )
                res = True
            elif ivp > 70:
                stock.set_analysis("NEUTRAL", "IV_RANK", IV_RANK_NT(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="HIGH", ivp_type=ivp_type,
                ))
                logger.info(
                    f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"SIGNAL IV_RANK HIGH | IVP={ivp:.1f} ATM IV={atm_iv:.1f}% "
                    f"CONDITION ivp={ivp:.1f} > 70 ✓"
                )
                res = True
            elif ivp < 10:
                stock.set_analysis("NEUTRAL", "IV_RANK_EXTREME", IV_RANK_NT(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="VERY_LOW", ivp_type=ivp_type,
                ))
                logger.info(
                    f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"SIGNAL IV_RANK_EXTREME VERY_LOW | IVP={ivp:.1f} ATM IV={atm_iv:.1f}% "
                    f"CONDITION ivp={ivp:.1f} < 10 ✓"
                )
                res = True
            elif ivp < 20:
                stock.set_analysis("NEUTRAL", "IV_RANK", IV_RANK_NT(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="LOW", ivp_type=ivp_type,
                ))
                logger.info(
                    f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"SIGNAL IV_RANK LOW | IVP={ivp:.1f} ATM IV={atm_iv:.1f}% "
                    f"CONDITION ivp={ivp:.1f} < 20 ✓"
                )
                res = True
            else:
                logger.debug(
                    f"[IV_RANK] {stock.stock_symbol} expiry={nearest_expiry} — "
                    f"no signal | IVP={ivp:.1f} in normal range [20-70], no edge"
                )

            return res

        except Exception as e:
            logger.error(f"[IV_RANK] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── analyse_iv_vs_hv ─────────────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_iv_vs_hv(self, stock: Stock):
        """
        Compares ATM IV (from Sensibull) against Historical Volatility (HV) computed
        from priceData to detect when options premium is expensive or cheap.

        Methodology:
            Positional : HV = std(log_returns_daily, 20-day window) × √252 × 100
            Intraday   : HV = std(log_returns_5m, last N bars) × √(252 × 75) × 100

        IV Premium zones:
            ratio < 0.8            → CHEAP
            [0.8, elevated)        → FAIR
            [elevated, expensive)  → ELEVATED
            [expensive, extreme)   → EXPENSIVE  ← signal emitted
            >= extreme             → EXTREME     ← signal emitted

        SOURCE DATA (DEBUG):   raw atm_iv from per_expiry_map, price close series length
        ANALYSER INPUT (DEBUG): normalised atm_iv, computed HV, ratio
        CONDITION (DEBUG):     iv_hv_ratio vs zone thresholds
        """
        try:
            logger.debug(f"[IV_VS_HV] {stock.stock_symbol} — start")

            # ── 1. ATM IV from Sensibull ──────────────────────────────────────
            per_expiry = (
                stock.sensibull_ctx.get("current", {})
                .get("stats", {})
                .get("per_expiry_map", {})
            )
            if not per_expiry:
                logger.debug(f"[IV_VS_HV] {stock.stock_symbol} — no per_expiry_map, skip")
                return False

            nearest_expiry = sorted(per_expiry.keys())[0]
            raw_atm_iv     = per_expiry[nearest_expiry].get("atm_iv")

            logger.debug(
                f"[IV_VS_HV] {stock.stock_symbol} expiry={nearest_expiry} | "
                f"SOURCE raw_atm_iv={raw_atm_iv}"
            )

            if raw_atm_iv is None or raw_atm_iv <= 0:
                logger.debug(f"[IV_VS_HV] {stock.stock_symbol} — atm_iv missing or zero, skip")
                return False

            # Normalise decimal → % (0.196 → 19.6)
            atm_iv = _to_pct(raw_atm_iv)

            # ── 2. HV ─────────────────────────────────────────────────────────
            is_intraday = shared.app_ctx.mode.name == shared.Mode.INTRADAY.name

            if is_intraday and getattr(stock, "daily_hv", None) is not None:
                # Use pre-cached daily HV (computed at morning bias from 1y daily
                # bars before priceData was overwritten with 5m data).
                # This avoids overnight gap bars inflating intraday HV.
                hv = stock.daily_hv
                hv_period_label = "20d(cached)"
                logger.debug(
                    f"[IV_VS_HV] {stock.stock_symbol} | "
                    f"SOURCE daily_hv={hv:.1f}% (cached at morning bias)"
                )
            else:
                # Positional mode: compute from priceData (2y daily bars).
                # Intraday fallback: daily_hv unavailable (morning bias not run).
                price_data = stock.priceData
                if price_data is None or price_data.empty:
                    logger.debug(f"[IV_VS_HV] {stock.stock_symbol} — no priceData, skip")
                    return False

                closes   = price_data["Close"].dropna()
                min_bars = IVAnalyser.IV_HV_MIN_BARS
                need_rows = min_bars + 1

                logger.debug(
                    f"[IV_VS_HV] {stock.stock_symbol} | "
                    f"SOURCE priceData rows={len(closes)} "
                    f"(need>={need_rows} for {min_bars}-bar HV)"
                    + (" [fallback — daily_hv not cached]" if is_intraday else "")
                )

                if len(closes) < need_rows:
                    logger.debug(
                        f"[IV_VS_HV] {stock.stock_symbol} — "
                        f"insufficient price data ({len(closes)} rows, need {need_rows}), skip"
                    )
                    return False

                window_closes = closes.iloc[-(IVAnalyser.IV_HV_PERIOD_BARS + 1):]
                log_returns   = np.log(window_closes / window_closes.shift(1)).dropna()

                if len(log_returns) < 2:
                    logger.debug(f"[IV_VS_HV] {stock.stock_symbol} — not enough log returns, skip")
                    return False

                std_returns = float(log_returns.std())
                if std_returns == 0:
                    logger.warning(f"[IV_VS_HV] {stock.stock_symbol} — HV std=0 (flat price), skip")
                    return False

                if is_intraday:
                    bars_per_year   = 252 * 75
                    hv_period_label = f"{IVAnalyser.IV_HV_PERIOD_BARS}×5m"
                else:
                    bars_per_year   = 252
                    hv_period_label = f"{IVAnalyser.IV_HV_PERIOD_BARS}d"

                hv = std_returns * (bars_per_year ** 0.5) * 100

            # ── 3. Zone classification ────────────────────────────────────────
            iv_hv_ratio    = atm_iv / hv
            iv_premium_pct = (atm_iv - hv) / hv * 100

            thresholds = {
                "EXTREME":   IVAnalyser.IV_HV_EXTREME_RATIO,
                "EXPENSIVE": IVAnalyser.IV_HV_EXPENSIVE_RATIO,
                "ELEVATED":  IVAnalyser.IV_HV_ELEVATED_RATIO,
            }

            if iv_hv_ratio >= IVAnalyser.IV_HV_EXTREME_RATIO:
                zone = "EXTREME"
            elif iv_hv_ratio >= IVAnalyser.IV_HV_EXPENSIVE_RATIO:
                zone = "EXPENSIVE"
            elif iv_hv_ratio >= IVAnalyser.IV_HV_ELEVATED_RATIO:
                zone = "ELEVATED"
            elif iv_hv_ratio >= 0.8:
                zone = "FAIR"
            else:
                zone = "CHEAP"

            # ── analyser input + condition evaluation ─────────────────────────
            logger.debug(
                f"[IV_VS_HV] {stock.stock_symbol} expiry={nearest_expiry} | "
                f"INPUT atm_iv={atm_iv:.1f}% (raw={raw_atm_iv}) "
                f"HV={hv:.1f}% ({hv_period_label}) | "
                f"CONDITION ratio={iv_hv_ratio:.2f} premium={iv_premium_pct:+.1f}% "
                f"thresholds=elevated:{IVAnalyser.IV_HV_ELEVATED_RATIO}/"
                f"expensive:{IVAnalyser.IV_HV_EXPENSIVE_RATIO}/"
                f"extreme:{IVAnalyser.IV_HV_EXTREME_RATIO} → zone={zone}"
            )

            logger.info(
                f"[IV_VS_HV] {stock.stock_symbol} [{nearest_expiry}] "
                f"ATM IV={atm_iv:.1f}% HV={hv:.1f}% ratio={iv_hv_ratio:.2f} zone={zone}"
            )

            # ── 4. Emit signal only for EXPENSIVE or EXTREME ──────────────────
            if zone not in ("EXPENSIVE", "EXTREME"):
                logger.debug(
                    f"[IV_VS_HV] {stock.stock_symbol} — zone={zone}, "
                    f"no signal (need EXPENSIVE or EXTREME)"
                )
                return False

            signal_str = (
                f"ATM IV={atm_iv:.1f}% is {iv_premium_pct:+.1f}% above HV={hv:.1f}% "
                f"({zone}) — options overpriced, seller has edge"
            )
            stock.set_analysis("NEUTRAL", "IV_PREMIUM", IV_PREMIUM_NT(
                hv=round(hv, 2),
                atm_iv=atm_iv,
                iv_hv_ratio=round(iv_hv_ratio, 2),
                iv_premium_pct=round(iv_premium_pct, 1),
                zone=zone,
                expiry=nearest_expiry,
                hv_period=hv_period_label,
                signal=signal_str,
            ))
            logger.info(
                f"[IV_VS_HV] {stock.stock_symbol} — SIGNAL IV_PREMIUM {zone} | {signal_str}"
            )
            return True

        except Exception as e:
            logger.error(f"[IV_VS_HV] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False
