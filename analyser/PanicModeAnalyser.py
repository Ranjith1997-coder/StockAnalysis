import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple


class PanicModeAnalyser(BaseAnalyzer):
    """
    Composite analyser that detects panic conditions by cross-reading
    stock.analysis already populated by all earlier analysers.

    MUST be registered LAST in AnalyserOrchestrator.

    Two signals:
        PANIC_MODE       -- active panic: >= PANIC_MIN_CONDITIONS (4/6) in same direction
        PANIC_EXHAUSTION -- burning out:  >= EXHAUSTION_MIN_CONDITIONS (3/4) contrarian

    Mode-specific price thresholds:
        Intraday   : >= 1.5% from prev close
        Positional : >= 3.0% daily move

    PANIC_MODE conditions (6):
        C1  Price momentum    -- ltp_change_perc >= threshold
        C2  IV expanding      -- IV_SPIKE or IV_TREND(UPWARD) in NEUTRAL
        C3  OI confirming     -- OI_INTRADAY_TREND[intraday] / OI_BUILDUP or OI_SHIFT[positional]
        C4  Futures confirm   -- FUTURE_ACTION_SHORT/LONG_BUILDUP, SIGNAL_SCORE_MEDIUM+
        C5  Volume surge      -- VOLUME_BREAKOUT or VOLUME_CLIMAX in panic direction
        C6  PCR confirming    -- PCR_BIAS, PCR_EXTREME, or PCR_TREND in panic direction

    PANIC_EXHAUSTION conditions (4):
        E1  IV extreme        -- IV_RANK_EXTREME NEUTRAL, ivp_type=VERY_HIGH or IVP > 80
        E2  Contrarian PCR    -- PCR_EXTREME or PCR_REVERSAL in OPPOSITE direction
        E3  Volume climax     -- VOLUME_CLIMAX in panic direction (exhaustion candle)
        E4  OI wall holding   -- OI_WALL (contrarian) or NEUTRAL OI_SUPPORT_RESISTANCE
    """

    INTRADAY_PRICE_THRESHOLD   = 1.5
    POSITIONAL_PRICE_THRESHOLD = 3.0
    PANIC_MIN_CONDITIONS       = 4
    EXHAUSTION_MIN_CONDITIONS  = 3

    def __init__(self) -> None:
        self.analyserName = "Panic Mode Analyser"
        super().__init__()

    def reset_constants(self):
        pass

    @staticmethod
    def _get(stock, sentiment, signal_type):
        return stock.analysis.get(sentiment, {}).get(signal_type)

    @staticmethod
    def _any_match(data, attr, value):
        if data is None:
            return False
        items = data if isinstance(data, list) else [data]
        return any(getattr(item, attr, None) == value for item in items)

    def _price_threshold(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            return PanicModeAnalyser.INTRADAY_PRICE_THRESHOLD
        return PanicModeAnalyser.POSITIONAL_PRICE_THRESHOLD

    def _is_intraday(self):
        return shared.app_ctx.mode.name == shared.Mode.INTRADAY.name

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_panic_mode(self, stock):
        """
        Detects an active panic move confirmed across IV, OI, futures, volume, and PCR.
        Fires when >= PANIC_MIN_CONDITIONS (4/6) align in the same direction.
        """
        try:
            logger.debug(f"Inside analyse_panic_mode for {stock.stock_symbol}")

            price_change = stock.ltp_change_perc
            if price_change is None:
                return False

            threshold   = self._price_threshold()
            is_intraday = self._is_intraday()

            if price_change <= -threshold:
                direction = "BEARISH"
            elif price_change >= threshold:
                direction = "BULLISH"
            else:
                return False

            conditions_met = []

            # C1 price momentum (always present when we reach here)
            conditions_met.append(f"price({price_change:+.1f}%)")

            # C2 IV expanding
            iv_spike = self._get(stock, "NEUTRAL", "IV_SPIKE")
            iv_trend  = self._get(stock, "NEUTRAL", "IV_TREND")
            if iv_spike is not None or self._any_match(iv_trend, "trend", "UPWARD"):
                conditions_met.append("IV_EXPANDING")

            # C3 OI confirming direction
            if is_intraday:
                oi_signal = self._get(stock, direction, "OI_INTRADAY_TREND")
            else:
                oi_signal = self._get(stock, direction, "OI_BUILDUP")
            if oi_signal is None:
                oi_signal = self._get(stock, direction, "OI_SHIFT")
            if oi_signal is not None:
                conditions_met.append("OI_CONFIRM")

            # C4 futures confirming
            if direction == "BEARISH":
                fut = (
                    self._get(stock, "BEARISH", "FUTURE_ACTION_SHORT_BUILDUP")
                    or self._get(stock, "BEARISH", "FUTURE_ACTION_LONG_UNWINDING")
                    or self._get(stock, "BEARISH", "FUTURE_ACTION")
                    or self._get(stock, "BEARISH", "FUTURE_SIGNAL_SCORE_HIGH")
                    or self._get(stock, "BEARISH", "FUTURE_SIGNAL_SCORE_MEDIUM")
                )
            else:
                fut = (
                    self._get(stock, "BULLISH", "FUTURE_ACTION_LONG_BUILDUP")
                    or self._get(stock, "BULLISH", "FUTURE_ACTION_SHORT_COVERING")
                    or self._get(stock, "BULLISH", "FUTURE_ACTION")
                    or self._get(stock, "BULLISH", "FUTURE_SIGNAL_SCORE_HIGH")
                    or self._get(stock, "BULLISH", "FUTURE_SIGNAL_SCORE_MEDIUM")
                )
            if fut is not None:
                conditions_met.append("FUTURES_CONFIRM")

            # C5 volume surge
            if (self._get(stock, direction, "VOLUME_BREAKOUT")
                    or self._get(stock, direction, "VOLUME_CLIMAX")):
                conditions_met.append("VOLUME_SURGE")

            # C6 PCR confirming
            if (self._get(stock, direction, "PCR_BIAS")
                    or self._get(stock, direction, "PCR_EXTREME")
                    or self._get(stock, direction, "PCR_TREND")):
                conditions_met.append("PCR_CONFIRM")

            count = len(conditions_met)
            if count < PanicModeAnalyser.PANIC_MIN_CONDITIONS:
                logger.debug(
                    f"PANIC_MODE not triggered for {stock.stock_symbol} ({direction}): "
                    f"{count}/{PanicModeAnalyser.PANIC_MIN_CONDITIONS} -- {conditions_met}"
                )
                return False

            mode_label = "intraday" if is_intraday else "positional"
            signal = (
                f"{direction} panic -- {count}/6 confirmed: "
                f"{', '.join(conditions_met)}"
            )

            PANIC_MODE = namedtuple("PANIC_MODE", [
                "direction", "price_change_pct", "conditions_met",
                "conditions_count", "mode", "signal"
            ])
            stock.set_analysis(direction, "PANIC_MODE", PANIC_MODE(
                direction=direction,
                price_change_pct=price_change,
                conditions_met=conditions_met,
                conditions_count=count,
                mode=mode_label,
                signal=signal,
            ))
            logger.info(f"PANIC MODE detected for {stock.stock_symbol}: {signal}")
            return True

        except Exception as e:
            logger.error(f"Error in analyse_panic_mode for {stock.stock_symbol}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_panic_exhaustion(self, stock):
        """
        Detects panic exhaustion -- the move is burning out.
        Fires in the CONTRARIAN direction when >= EXHAUSTION_MIN_CONDITIONS (3/4).
        """
        try:
            logger.debug(f"Inside analyse_panic_exhaustion for {stock.stock_symbol}")

            price_change = stock.ltp_change_perc
            if price_change is None:
                return False

            threshold   = self._price_threshold()
            is_intraday = self._is_intraday()

            if price_change <= -threshold:
                panic_direction = "BEARISH"
                contrarian      = "BULLISH"
            elif price_change >= threshold:
                panic_direction = "BULLISH"
                contrarian      = "BEARISH"
            else:
                return False

            conditions_met = []
            iv_percentile  = None

            # E1 IV at extreme (fear/greed peaked)
            iv_rank_extreme = self._get(stock, "NEUTRAL", "IV_RANK_EXTREME")
            if iv_rank_extreme is not None:
                items = iv_rank_extreme if isinstance(iv_rank_extreme, list) else [iv_rank_extreme]
                for item in items:
                    ivp      = getattr(item, "iv_percentile", None)
                    ivp_type = getattr(item, "ivp_type", None)
                    if ivp_type == "VERY_HIGH" or (ivp is not None and ivp > 80):
                        label = f"IV_EXTREME(IVP={ivp:.0f})" if ivp else "IV_EXTREME"
                        conditions_met.append(label)
                        iv_percentile = ivp
                        break

            # E2 contrarian PCR (excessive put buying after sell-off = exhaustion)
            if (self._get(stock, contrarian, "PCR_EXTREME")
                    or self._get(stock, contrarian, "PCR_REVERSAL")):
                conditions_met.append("PCR_CONTRARIAN")

            # E3 volume climax in panic direction
            if self._get(stock, panic_direction, "VOLUME_CLIMAX"):
                conditions_met.append("VOLUME_CLIMAX")

            # E4 OI wall holding at structural level
            oi_wall = self._get(stock, contrarian, "OI_WALL")
            if oi_wall is None:
                oi_wall = self._get(stock, "NEUTRAL", "OI_SUPPORT_RESISTANCE")
            if oi_wall is not None:
                conditions_met.append("OI_WALL_HOLDING")

            count = len(conditions_met)
            if count < PanicModeAnalyser.EXHAUSTION_MIN_CONDITIONS:
                logger.debug(
                    f"PANIC_EXHAUSTION not triggered for {stock.stock_symbol} "
                    f"(panic={panic_direction}): "
                    f"{count}/{PanicModeAnalyser.EXHAUSTION_MIN_CONDITIONS} -- {conditions_met}"
                )
                return False

            mode_label = "intraday" if is_intraday else "positional"
            signal = (
                f"{panic_direction} panic exhaustion -- {count}/4 confirmed: "
                f"{', '.join(conditions_met)}"
            )

            PANIC_EXHAUSTION = namedtuple("PANIC_EXHAUSTION", [
                "panic_direction", "conditions_met", "conditions_count",
                "iv_percentile", "mode", "signal"
            ])
            stock.set_analysis(contrarian, "PANIC_EXHAUSTION", PANIC_EXHAUSTION(
                panic_direction=panic_direction,
                conditions_met=conditions_met,
                conditions_count=count,
                iv_percentile=iv_percentile,
                mode=mode_label,
                signal=signal,
            ))
            logger.info(f"PANIC EXHAUSTION detected for {stock.stock_symbol}: {signal}")
            return True

        except Exception as e:
            logger.error(f"Error in analyse_panic_exhaustion for {stock.stock_symbol}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
