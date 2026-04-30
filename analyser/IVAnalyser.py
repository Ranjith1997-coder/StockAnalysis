import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple

class IVAnalyser(BaseAnalyzer):
    IV_PERCENTAGE_CHANGE = 30
    def __init__(self) -> None:
        self.analyserName = "IV Analyser"
        super().__init__()
    
    def reset_constants(self):
        IVAnalyser.IV_TREND_CONTINUATION_DAYS = 3
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            IVAnalyser.IV_PERCENTAGE_CHANGE = 5
            IVAnalyser.IV_TREND_PERCENTAGE_CHANGE = 8
        else:
            IVAnalyser.IV_PERCENTAGE_CHANGE = 20
            IVAnalyser.IV_TREND_PERCENTAGE_CHANGE = 20

        logger.debug(f"IVAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"IV_PERCENTAGE_CHANGE = {IVAnalyser.IV_PERCENTAGE_CHANGE}")

    def _get_nearest_expiry_columns(self, stock):
        """
        Returns (nearest_expiry, atm_iv_col, atm_iv_pct_col) for the nearest expiry,
        derived from sensibull_ctx["current"]["stats"]["per_expiry_map"].
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

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_spike_in_ATM_IV(self, stock: Stock):
        """
        Detects an IV spike using Sensibull's atm_iv_change field (change vs prev close)
        from per_expiry_map. Falls back to comparing last 2 historical_data snapshots
        when atm_iv_change is unavailable.
        """
        try:
            logger.debug(f'Inside analyse_spike_in_ATM_IV for stock {stock.stock_symbol}')

            per_expiry = (
                stock.sensibull_ctx.get("current", {})
                .get("stats", {})
                .get("per_expiry_map", {})
            )
            if not per_expiry:
                logger.info(f"No Sensibull per_expiry_map for {stock.stock_symbol}, skipping IV spike check")
                return False

            IV_SPIKE = namedtuple("IV_SPIKE", ["expiry", "iv", "iv_change", "source"])
            is_intraday = shared.app_ctx.mode.name == shared.Mode.INTRADAY.name
            res = False

            for expiry, expiry_data in per_expiry.items():
                atm_iv = expiry_data.get("atm_iv")
                atm_iv_change = expiry_data.get("atm_iv_change")

                if is_intraday:
                    # Intraday: atm_iv_change is vs prev day close — not useful here.
                    # Compare the last 2 Sensibull snapshots (~5-min delta) from historical_data.
                    nearest_expiry, iv_col, _ = self._get_nearest_expiry_columns(stock)
                    if nearest_expiry != expiry or iv_col is None:
                        continue
                    hist_df = stock.sensibull_ctx.get("historical_data")
                    if hist_df is None or hist_df.empty or iv_col not in hist_df.columns:
                        continue
                    iv_series = hist_df[iv_col].dropna()
                    if len(iv_series) < 2:
                        logger.info(f"Insufficient Sensibull history for intraday IV spike check: "
                                    f"{stock.stock_symbol} expiry {expiry}")
                        continue
                    prev_iv = iv_series.iloc[-2]
                    curr_iv = iv_series.iloc[-1]
                    if prev_iv == 0:
                        continue
                    iv_change_pct = ((curr_iv - prev_iv) / prev_iv) * 100
                    if abs(iv_change_pct) >= IVAnalyser.IV_PERCENTAGE_CHANGE:
                        stock.set_analysis("NEUTRAL", "IV_SPIKE", IV_SPIKE(
                            expiry=expiry,
                            iv=curr_iv,
                            iv_change=iv_change_pct,
                            source="sensibull_5min"
                        ))
                        logger.info(f"Intraday IV spike detected for {stock.stock_symbol} expiry {expiry}: "
                                    f"ATM IV={curr_iv:.1f}%, 5-min change={iv_change_pct:.2f}%")
                        res = True
                else:
                    # Positional: use Sensibull's pre-computed atm_iv_change (vs prev close)
                    if atm_iv is not None and atm_iv_change is not None and atm_iv > 0:
                        prev_iv = atm_iv - atm_iv_change
                        iv_change_pct = (atm_iv_change / prev_iv) * 100 if prev_iv != 0 else 0
                        if abs(iv_change_pct) >= IVAnalyser.IV_PERCENTAGE_CHANGE:
                            stock.set_analysis("NEUTRAL", "IV_SPIKE", IV_SPIKE(
                                expiry=expiry,
                                iv=atm_iv,
                                iv_change=iv_change_pct,
                                source="sensibull_change"
                            ))
                            logger.info(f"Positional IV spike detected for {stock.stock_symbol} expiry {expiry}: "
                                        f"ATM IV={atm_iv:.1f}%, daily change={iv_change_pct:.2f}%")
                            res = True

            return res
        except Exception as e:
            logger.error(f"Error in analyse_spike_in_ATM_IV for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_trend_in_ATM_IV(self, stock: Stock):
        """
        Detects a sustained IV trend (rising or falling) over the last N snapshots
        using Sensibull historical_data atm_iv column for the nearest expiry.
        """
        try:
            logger.debug(f'Inside analyse_trend_in_ATM_IV for stock {stock.stock_symbol}')

            nearest_expiry, iv_col, _ = self._get_nearest_expiry_columns(stock)
            if iv_col is None:
                logger.info(f"No Sensibull expiry data for {stock.stock_symbol}, skipping IV trend check")
                return False

            hist_df = stock.sensibull_ctx.get("historical_data")
            if hist_df is None or hist_df.empty or iv_col not in hist_df.columns:
                logger.info(f"No Sensibull historical IV data for {stock.stock_symbol} column {iv_col}")
                return False

            n = IVAnalyser.IV_TREND_CONTINUATION_DAYS
            iv_series = hist_df[iv_col].dropna()
            if len(iv_series) < n:
                logger.info(f"Insufficient Sensibull history ({len(iv_series)} rows, need {n}) "
                            f"for IV trend check: {stock.stock_symbol}")
                return False

            ivs = iv_series.iloc[-n:].tolist()
            if ivs[0] == 0:
                return False

            iv_change_pct = ((ivs[-1] - ivs[0]) / ivs[0]) * 100
            IV_TREND = namedtuple("IV_TREND", ["expiry", "trend", "iv_change_pct", "atm_iv"])
            res = False

            if (all(ivs[i] < ivs[i + 1] for i in range(n - 1))
                    and abs(iv_change_pct) >= IVAnalyser.IV_TREND_PERCENTAGE_CHANGE):
                stock.set_analysis("NEUTRAL", "IV_TREND", IV_TREND(
                    expiry=nearest_expiry, trend="UPWARD",
                    iv_change_pct=iv_change_pct, atm_iv=ivs[-1]
                ))
                logger.info(f"IV upward trend detected for {stock.stock_symbol} expiry {nearest_expiry} "
                            f"over {n} snapshots: {iv_change_pct:.2f}%")
                res = True
            elif (all(ivs[i] > ivs[i + 1] for i in range(n - 1))
                  and abs(iv_change_pct) >= IVAnalyser.IV_TREND_PERCENTAGE_CHANGE):
                stock.set_analysis("NEUTRAL", "IV_TREND", IV_TREND(
                    expiry=nearest_expiry, trend="DOWNWARD",
                    iv_change_pct=iv_change_pct, atm_iv=ivs[-1]
                ))
                logger.info(f"IV downward trend detected for {stock.stock_symbol} expiry {nearest_expiry} "
                            f"over {n} snapshots: {iv_change_pct:.2f}%")
                res = True

            return res
        except Exception as e:
            logger.error(f"Error in analyse_trend_in_ATM_IV for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_iv_rank(self, stock: Stock):
        """
        Emits IV_RANK or IV_RANK_EXTREME signals based on Sensibull's atm_iv_percentile
        (IV rank: 0-100 scale showing where current IV sits vs historical range).

        IVP < 20   → IV_RANK LOW   — cheap options, buyers have edge
        IVP 20-70  → Normal range  — no signal
        IVP > 70   → IV_RANK HIGH  — expensive options, sellers have edge
        IVP > 85   → IV_RANK_EXTREME HIGH — IV crush risk after event
        IVP < 10   → IV_RANK_EXTREME LOW  — historically cheapest, big move expected
        """
        try:
            logger.debug(f'Inside analyse_iv_rank for stock {stock.stock_symbol}')

            per_expiry = (
                stock.sensibull_ctx.get("current", {})
                .get("stats", {})
                .get("per_expiry_map", {})
            )
            if not per_expiry:
                logger.info(f"No Sensibull per_expiry_map for {stock.stock_symbol}, skipping IV rank check")
                return False

            IV_RANK = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
            res = False

            # Only analyse nearest expiry for rank signal
            nearest_expiry = sorted(per_expiry.keys())[0]
            expiry_data = per_expiry[nearest_expiry]

            atm_iv = expiry_data.get("atm_iv")
            ivp = expiry_data.get("atm_iv_percentile")
            ivp_type = expiry_data.get("atm_ivp_type", "")

            if atm_iv is None or ivp is None:
                logger.info(f"atm_iv or atm_iv_percentile missing for {stock.stock_symbol} expiry {nearest_expiry}")
                return False

            if ivp > 85:
                stock.set_analysis("NEUTRAL", "IV_RANK_EXTREME", IV_RANK(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="VERY_HIGH", ivp_type=ivp_type
                ))
                logger.info(f"IV rank EXTREME HIGH for {stock.stock_symbol}: IVP={ivp:.1f}%, ATM IV={atm_iv:.1f}%")
                res = True
            elif ivp > 70:
                stock.set_analysis("NEUTRAL", "IV_RANK", IV_RANK(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="HIGH", ivp_type=ivp_type
                ))
                logger.info(f"IV rank HIGH for {stock.stock_symbol}: IVP={ivp:.1f}%, ATM IV={atm_iv:.1f}%")
                res = True
            elif ivp < 10:
                stock.set_analysis("NEUTRAL", "IV_RANK_EXTREME", IV_RANK(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="VERY_LOW", ivp_type=ivp_type
                ))
                logger.info(f"IV rank EXTREME LOW for {stock.stock_symbol}: IVP={ivp:.1f}%, ATM IV={atm_iv:.1f}%")
                res = True
            elif ivp < 20:
                stock.set_analysis("NEUTRAL", "IV_RANK", IV_RANK(
                    expiry=nearest_expiry, atm_iv=atm_iv,
                    iv_percentile=ivp, category="LOW", ivp_type=ivp_type
                ))
                logger.info(f"IV rank LOW for {stock.stock_symbol}: IVP={ivp:.1f}%, ATM IV={atm_iv:.1f}%")
                res = True

            return res
        except Exception as e:
            logger.error(f"Error in analyse_iv_rank for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

