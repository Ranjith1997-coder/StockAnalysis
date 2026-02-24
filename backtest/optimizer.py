"""
Optuna-based threshold optimizer for stock trading strategies.

Plugs into the existing Backtester to intelligently search for optimal
parameters (thresholds) for each analyser method that maximise a chosen
performance metric (Sharpe ratio, profit factor, total return, etc.).

Features:
  - Pre-defined search spaces for every TechnicalAnalyser, CandleStickAnalyser,
    and VolumeAnalyser method.
  - Train / test split to guard against overfitting.
  - Multi-stock aggregation so results generalise across instruments.
  - Parallel trial execution via Optuna's built-in support.
  - Human-readable report + JSON export of best parameters.

Usage:
    from backtest.optimizer import ThresholdOptimizer, SEARCH_SPACES
    opt = ThresholdOptimizer(...)
    best = opt.optimize()
"""

import sys
import os
sys.path.append(os.getcwd())

import json
import logging
import warnings
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

try:
    import optuna
    from optuna.trial import Trial
except ImportError:
    raise ImportError(
        "optuna is required for optimization.  Install it with:\n"
        "  pip install optuna"
    )

from backtest.backtest import Backtester, BacktestResult
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from analyser.VolumeAnalyser import VolumeAnalyser
from common.logging_util import logger
import common.shared as shared

# Suppress yfinance / optuna noise during optimisation
warnings.filterwarnings("ignore", category=FutureWarning)
optuna.logging.set_verbosity(optuna.logging.WARNING)

# ---------------------------------------------------------------------------
# Search-space definitions
# ---------------------------------------------------------------------------
# Each entry maps a *class-level attribute name* to an Optuna suggest spec.
#   "type" : "int" | "float" | "categorical"
#   "low" / "high" / "step"  — for int / float
#   "choices"                 — for categorical
# ---------------------------------------------------------------------------

TECHNICAL_SEARCH_SPACES: Dict[str, Dict[str, dict]] = {

    "analyse_rsi": {
        "RSI_UPPER_THRESHOLD": {"type": "int", "low": 65, "high": 85, "step": 5},
        "RSI_LOWER_THRESHOLD": {"type": "int", "low": 15, "high": 35, "step": 5},
        "RSI_LOOKUP_PERIOD":   {"type": "categorical", "choices": [7, 10, 14, 21]},
        "RSI_TREND_PERIODS":   {"type": "categorical", "choices": [2, 3, 5]},
    },

    "analyse_Bolinger_band": {
        # These are local defaults inside the method; we expose them as class
        # attrs so the optimizer can override them.
        "BB_WINDOW":   {"type": "categorical", "choices": [10, 15, 20, 25, 30]},
        "BB_NUM_STD":  {"type": "float", "low": 1.5, "high": 3.0, "step": 0.25},
    },

    "analyse_ema_crossover": {
        "FAST_EMA_PERIOD":   {"type": "categorical", "choices": [5, 9, 12, 20, 50]},
        "SLOW_EMA_PERIOD":   {"type": "categorical", "choices": [21, 26, 50, 100, 200]},
        "EMA_DIFF_THRESHOLD": {"type": "float", "low": 0.0, "high": 0.5, "step": 0.1},
        "EMA_MIN_SLOPE":      {"type": "float", "low": 0.0, "high": 0.2, "step": 0.05},
    },

    "analyze_macd": {
        "MACD_FAST_PERIOD":   {"type": "categorical", "choices": [8, 10, 12, 16]},
        "MACD_SLOW_PERIOD":   {"type": "categorical", "choices": [20, 26, 30]},
        "MACD_SIGNAL_PERIOD": {"type": "categorical", "choices": [7, 9, 12]},
    },

    "analyse_supertrend": {
        "SUPERTREND_PERIOD":     {"type": "categorical", "choices": [7, 10, 14, 20]},
        "SUPERTREND_MULTIPLIER": {"type": "float", "low": 1.5, "high": 4.0, "step": 0.5},
    },

    "analyse_stochastic": {
        "STOCHASTIC_K_PERIOD": {"type": "categorical", "choices": [5, 9, 14, 21]},
        "STOCHASTIC_D_PERIOD": {"type": "categorical", "choices": [3, 5, 7]},
        "STOCHASTIC_UPPER":    {"type": "int", "low": 70, "high": 85, "step": 5},
        "STOCHASTIC_LOWER":    {"type": "int", "low": 15, "high": 30, "step": 5},
    },

    "analyse_rsi_divergence": {
        "RSI_DIVERGENCE_LOOKBACK":    {"type": "categorical", "choices": [20, 30, 50, 75]},
        "RSI_DIVERGENCE_SWING_ORDER": {"type": "categorical", "choices": [2, 3, 4]},
    },

    "analyse_obv": {
        "OBV_EMA_PERIOD": {"type": "categorical", "choices": [10, 14, 20, 30]},
    },

    "analyse_atr": {
        "ATR_PERIOD":       {"type": "categorical", "choices": [7, 14, 21]},
        "ATR_THRESHOLD":    {"type": "float", "low": 1.5, "high": 4.0, "step": 0.5},
        "ATR_TREND_PERIODS": {"type": "categorical", "choices": [2, 3, 5]},
    },

    "analyse_vwap": {
        "VWAP_DEVIATION_PERCENTAGE": {"type": "float", "low": 1.0, "high": 4.0, "step": 0.5},
        "VWAP_DAYS":                 {"type": "categorical", "choices": [5, 10, 15, 20]},
    },

    "analyse_pivot_points": {
        # Pivot points are formula-based; no meaningful thresholds to tune
        # beyond the crossover logic itself.  Included for completeness.
    },
}

CANDLESTICK_SEARCH_SPACES: Dict[str, Dict[str, dict]] = {

    "singleCandleStickPattern": {
        # MOMENTUM patterns: Bullish/Bearish Marubozu
        "MARUBASU_THRESHOLD":     {"type": "float", "low": 0.5, "high": 5.0, "step": 0.5},
        "WICK_PERCENTAGE":        {"type": "float", "low": 0.1, "high": 0.5, "step": 0.1},
    },

    "singleCandleReversalPattern": {
        # REVERSAL patterns: Hammer, Shooting Star (with trend context)
        "HAMMER_BODY_RATIO":      {"type": "float", "low": 0.2, "high": 0.45, "step": 0.05},
        "HAMMER_WICK_MULTIPLIER": {"type": "float", "low": 1.5, "high": 3.0, "step": 0.5},
        # Trend detection parameters for reversal patterns
        "TREND_LOOKBACK_PERIOD":  {"type": "categorical", "choices": [3, 4, 5, 6, 7]},
        "DOWNTREND_MIN_DECLINE":  {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "UPTREND_MIN_INCREASE":   {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "TREND_CONSISTENCY_RATIO": {"type": "float", "low": 0.5, "high": 0.8, "step": 0.1},
    },

    "doubleCandleStickPattern": {
        # REVERSAL patterns only: Engulfing, Piercing Line, Dark Cloud Cover (with trend context)
        "ENGULFING_MIN_BODY_RATIO":      {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "PIERCING_MIN_PENETRATION":      {"type": "float", "low": 0.1, "high": 0.8, "step": 0.1},
        "DARK_CLOUD_MIN_PENETRATION":    {"type": "float", "low": 0.1, "high": 0.8, "step": 0.1},
        # Trend detection parameters for reversal patterns
        "TREND_LOOKBACK_PERIOD":  {"type": "categorical", "choices": [3, 4, 5, 6, 7]},
        "DOWNTREND_MIN_DECLINE":  {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "UPTREND_MIN_INCREASE":   {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "TREND_CONSISTENCY_RATIO": {"type": "float", "low": 0.5, "high": 0.8, "step": 0.1},
    },

    "doubleCandleStickContinuationPattern": {
        # CONTINUATION patterns only: 2 Continuous Increase/Decrease
        # WARNING: These patterns tend to have negative expectancy
        "TWO_CONT_INC_OR_DEC_THRESHOLD": {"type": "float", "low": 0.5, "high": 6.0, "step": 0.5},
    },

    "tripleCandleStickReversalPattern": {
        # REVERSAL patterns: Morning Star, Evening Star (with trend context)
        "STAR_MAX_BODY_RATIO":             {"type": "float", "low": 0.15, "high": 0.45, "step": 0.05},
        # Trend detection parameters for reversal patterns
        "TREND_LOOKBACK_PERIOD":  {"type": "categorical", "choices": [3, 4, 5, 6, 7]},
        "DOWNTREND_MIN_DECLINE":  {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "UPTREND_MIN_INCREASE":   {"type": "float", "low": 0.5, "high": 3.0, "step": 0.25},
        "TREND_CONSISTENCY_RATIO": {"type": "float", "low": 0.5, "high": 0.8, "step": 0.1},
    },

    "tripleCandleStickContinuationPattern": {
        # CONTINUATION patterns only: 3 Continuous Increase/Decrease
        # WARNING: These patterns tend to have negative expectancy
        "THREE_CONT_INC_OR_DEC_THRESHOLD": {"type": "float", "low": 1.0, "high": 8.0, "step": 0.5},
    },
}

VOLUME_SEARCH_SPACES: Dict[str, Dict[str, dict]] = {

    "analyse_volume_and_price": {
        "TIMES_VOLUME":           {"type": "float", "low": 1.5, "high": 5.0, "step": 0.5},
        "VOLUME_PRICE_THRESHOLD": {"type": "float", "low": 1.0, "high": 7.0, "step": 1.0},
        "VOLUME_MA_PERIOD":       {"type": "categorical", "choices": [10, 20, 50]},
    },
}

# Unified lookup
SEARCH_SPACES: Dict[str, Dict[str, Dict[str, dict]]] = {
    "TechnicalAnalyser":   TECHNICAL_SEARCH_SPACES,
    "CandleStickAnalyser": CANDLESTICK_SEARCH_SPACES,
    "VolumeAnalyser":      VOLUME_SEARCH_SPACES,
}

# Map analyser class names to actual classes
ANALYSER_CLASSES = {
    "TechnicalAnalyser":   TechnicalAnalyser,
    "CandleStickAnalyser": CandleStickAnalyser,
    "VolumeAnalyser":      VolumeAnalyser,
}

# Optimisation metric options
METRIC_CHOICES = [
    "sharpe_ratio",
    "profit_factor",
    "total_return_percent",
    "win_rate",
    "expectancy",
    "risk_reward_ratio",
]


# ---------------------------------------------------------------------------
# Helper: suggest a value from an Optuna trial given a param spec
# ---------------------------------------------------------------------------
def _suggest(trial: Trial, name: str, spec: dict):
    """Translate our spec dict into the right Optuna suggest call."""
    if spec["type"] == "int":
        return trial.suggest_int(name, spec["low"], spec["high"], step=spec.get("step", 1))
    elif spec["type"] == "float":
        return trial.suggest_float(name, spec["low"], spec["high"], step=spec.get("step"))
    elif spec["type"] == "categorical":
        return trial.suggest_categorical(name, spec["choices"])
    else:
        raise ValueError(f"Unknown param type: {spec['type']}")


# ---------------------------------------------------------------------------
# Data cache — avoids re-downloading for every trial
# ---------------------------------------------------------------------------
class _DataCache:
    """Simple in-memory cache for yfinance downloads."""

    def __init__(self):
        self._cache: Dict[str, pd.DataFrame] = {}

    def get(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        key = f"{symbol}|{start}|{end}|{interval}"
        if key not in self._cache:
            bt = Backtester(
                stock_symbols=symbol,
                analyzer_methods=lambda s: False,  # dummy; not used
                start_date=start,
                end_date=end,
                interval=interval,
            )
            self._cache[key] = bt.load_data(symbol)
        return self._cache[key].copy()


_data_cache = _DataCache()


# ---------------------------------------------------------------------------
# Core Optimiser
# ---------------------------------------------------------------------------
class ThresholdOptimizer:
    """
    Optuna-powered parameter optimiser for any analyser method.

    Parameters
    ----------
    analyser_class_name : str
        One of 'TechnicalAnalyser', 'CandleStickAnalyser', 'VolumeAnalyser'.
    method_name : str
        Name of the analyser method, e.g. 'analyse_rsi'.
    stock_symbols : list[str]
        Stocks to backtest on.  More stocks = more robust parameters.
    train_start, train_end : str
        Date range for optimisation (in-sample).
    test_start, test_end : str | None
        Date range for out-of-sample validation.  If None, no validation.
    metric : str
        Metric to maximise.  Default 'sharpe_ratio'.
    n_trials : int
        Number of Optuna trials.  Default 150.
    stop_loss_pct, target_pct : float | None
        Trade-level risk management.
    initial_capital, position_size : float
        Capital & sizing for the backtester.
    allow_short : bool
        Whether bearish signals open short positions.
    custom_search_space : dict | None
        Override the default search space for this method.
    mode : str
        'positional' or 'intraday'.  Affects reset_constants().
    """

    def __init__(
        self,
        analyser_class_name: str,
        method_name: str,
        stock_symbols: List[str],
        train_start: str,
        train_end: str,
        test_start: Optional[str] = None,
        test_end: Optional[str] = None,
        metric: str = "sharpe_ratio",
        n_trials: int = 150,
        stop_loss_pct: Optional[float] = 3.0,
        target_pct: Optional[float] = 5.0,
        initial_capital: float = 100000,
        position_size: float = 20000,
        allow_short: bool = True,
        custom_search_space: Optional[Dict[str, dict]] = None,
        mode: str = "positional",
    ):
        if analyser_class_name not in ANALYSER_CLASSES:
            raise ValueError(
                f"Unknown analyser: {analyser_class_name}.  "
                f"Choose from {list(ANALYSER_CLASSES.keys())}"
            )
        if metric not in METRIC_CHOICES:
            raise ValueError(f"Unknown metric: {metric}.  Choose from {METRIC_CHOICES}")

        self.analyser_class_name = analyser_class_name
        self.analyser_class = ANALYSER_CLASSES[analyser_class_name]
        self.method_name = method_name
        self.stock_symbols = stock_symbols
        self.train_start = train_start
        self.train_end = train_end
        self.test_start = test_start
        self.test_end = test_end
        self.metric = metric
        self.n_trials = n_trials
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.initial_capital = initial_capital
        self.position_size = position_size
        self.allow_short = allow_short
        self.mode = mode

        # Resolve search space
        if custom_search_space is not None:
            self.search_space = custom_search_space
        else:
            class_spaces = SEARCH_SPACES.get(analyser_class_name, {})
            self.search_space = class_spaces.get(method_name, {})
            if not self.search_space:
                logger.warning(
                    f"No pre-defined search space for {analyser_class_name}.{method_name}.  "
                    "Provide a custom_search_space or add one to optimizer.py."
                )

        # Pre-load data for all stocks (avoids repeated downloads)
        logger.info("Pre-loading historical data for all stocks...")
        self._train_data: Dict[str, pd.DataFrame] = {}
        self._test_data: Dict[str, pd.DataFrame] = {}
        for sym in self.stock_symbols:
            try:
                self._train_data[sym] = _data_cache.get(sym, train_start, train_end)
                if test_start and test_end:
                    self._test_data[sym] = _data_cache.get(sym, test_start, test_end)
            except Exception as e:
                logger.error(f"Failed to load data for {sym}: {e}")

        # Results storage
        self.study: Optional[optuna.Study] = None
        self.best_params: Dict[str, Any] = {}
        self.best_train_metric: float = 0.0
        self.best_test_metric: Optional[float] = None
        self.train_trade_count: int = 0
        self.test_trade_count: int = 0
        self.train_summary: Dict[str, Any] = {}
        self.test_summary: Dict[str, Any] = {}
        self.all_trials: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def _init_app_context(self):
        """Ensure the shared app context is set for the chosen mode."""
        if self.mode == "intraday":
            shared.app_ctx.mode = shared.Mode.INTRADAY  # type: ignore[assignment]
        else:
            shared.app_ctx.mode = shared.Mode.POSITIONAL  # type: ignore[assignment]

    # ------------------------------------------------------------------
    def _set_params(self, params: Dict[str, Any]):
        """Set class-level thresholds on the analyser class."""
        for attr, value in params.items():
            setattr(self.analyser_class, attr, value)

    # ------------------------------------------------------------------
    def _create_analyser_and_method(self, params: Optional[Dict[str, Any]] = None) -> Tuple[Any, Callable]:
        """Instantiate the analyser and return the bound method.
        
        Parameters
        ----------
        params : dict | None
            If provided, apply these parameters AFTER reset_constants().
            This ensures the optimizer's params override the defaults.
            If None, uses self.best_params (for post-optimization use).
        """
        analyser = self.analyser_class()
        analyser.reset_constants()
        # Apply params AFTER reset_constants() to override defaults
        params_to_apply = params if params is not None else self.best_params
        if params_to_apply:
            for attr, value in params_to_apply.items():
                setattr(self.analyser_class, attr, value)
        method = getattr(analyser, self.method_name)
        return analyser, method

    # ------------------------------------------------------------------
    def _run_backtest_with_data(
        self,
        method: Callable,
        data_dict: Dict[str, pd.DataFrame],
        start: str,
        end: str,
    ) -> Dict[str, BacktestResult]:
        """Run backtester for all stocks using pre-loaded data."""
        results: Dict[str, BacktestResult] = {}

        for sym, full_data in data_dict.items():
            try:
                bt = Backtester(
                    stock_symbols=sym,
                    analyzer_methods=method,
                    start_date=start,
                    end_date=end,
                    initial_capital=self.initial_capital,
                    position_size=self.position_size,
                    stop_loss_pct=self.stop_loss_pct,
                    target_pct=self.target_pct,
                    allow_short=self.allow_short,
                )
                result = self._run_single_backtest(bt, sym, full_data)
                results[sym] = result
            except Exception as e:
                logger.debug(f"Backtest failed for {sym}: {e}")

        return results

    # ------------------------------------------------------------------
    def _run_single_backtest(
        self, bt: Backtester, symbol: str, price_data: pd.DataFrame
    ) -> BacktestResult:
        """Run backtest for a single stock using already-loaded data."""
        from common.Stock import Stock

        result = BacktestResult(symbol, bt.start_date, bt.end_date, bt.initial_capital)
        stock = bt.create_stock_object(symbol, price_data)
        full_price_data = price_data.copy()

        open_positions = []
        available_capital = bt.initial_capital

        test_data = price_data[price_data.index >= bt.start_date]
        test_indices: list[int] = [int(price_data.index.get_loc(idx)) for idx in test_data.index]  # type: ignore[arg-type]

        for current_idx in test_indices:
            current_date = price_data.index[current_idx]
            current_row = price_data.iloc[current_idx]
            current_price = current_row["Close"]

            # Check exits
            for pos in open_positions[:]:
                if pos.check_stop_loss(current_price):
                    pos.close_trade(current_date, current_price, "stop_loss")
                    result.add_trade(pos)
                    available_capital += pos.pnl
                    open_positions.remove(pos)
                    continue
                if pos.check_target(current_price):
                    pos.close_trade(current_date, current_price, "target")
                    result.add_trade(pos)
                    available_capital += pos.pnl
                    open_positions.remove(pos)
                    continue

            if current_idx < 50:
                continue

            analysis = bt.run_analysis_on_date(stock, current_idx, full_price_data)

            has_bullish = len(analysis["BULLISH"]) > 0
            has_bearish = len(analysis["BEARISH"]) > 0 and bt.allow_short

            # Close opposing positions on reversal
            if has_bullish:
                for p in [p for p in open_positions if p.signal_type == "BEARISH"]:
                    p.close_trade(current_date, current_price, "signal_reversal")
                    result.add_trade(p)
                    available_capital += p.pnl
                    open_positions.remove(p)
            if has_bearish:
                for p in [p for p in open_positions if p.signal_type == "BULLISH"]:
                    p.close_trade(current_date, current_price, "signal_reversal")
                    result.add_trade(p)
                    available_capital += p.pnl
                    open_positions.remove(p)

            # Open new positions
            from backtest.backtest import Trade

            if len(open_positions) < bt.max_positions:
                qty = int(bt.calculate_position_quantity(current_price, available_capital))
                if has_bullish and not any(p.signal_type == "BULLISH" for p in open_positions):
                    sl = current_price * (1 - bt.stop_loss_pct / 100) if bt.stop_loss_pct else None
                    tgt = current_price * (1 + bt.target_pct / 100) if bt.target_pct else None
                    trade = Trade(current_date, current_price, "BULLISH", qty, sl, tgt)
                    trade.analysis_details = analysis["BULLISH"].copy()
                    open_positions.append(trade)
                    available_capital -= qty * current_price
                elif has_bearish and not any(p.signal_type == "BEARISH" for p in open_positions):
                    sl = current_price * (1 + bt.stop_loss_pct / 100) if bt.stop_loss_pct else None
                    tgt = current_price * (1 - bt.target_pct / 100) if bt.target_pct else None
                    trade = Trade(current_date, current_price, "BEARISH", qty, sl, tgt)
                    trade.analysis_details = analysis["BEARISH"].copy()
                    open_positions.append(trade)
                    available_capital -= qty * current_price

        # Close remaining
        if open_positions:
            final_date = price_data.index[-1]
            final_price = price_data.iloc[-1]["Close"]
            for pos in open_positions:
                pos.close_trade(final_date, final_price, "end_of_data")
                result.add_trade(pos)

        result.calculate_metrics()
        return result

    # ------------------------------------------------------------------
    def _aggregate_metric(self, results: Dict[str, BacktestResult]) -> float:
        """Average the chosen metric across all stocks."""
        values = []
        for r in results.values():
            val = r.metrics.get(self.metric, 0)
            if val is not None and not np.isnan(val) and not np.isinf(val):
                values.append(val)
        return float(np.mean(values)) if values else -999.0

    # ------------------------------------------------------------------
    def _count_trades(self, results: Dict[str, BacktestResult]) -> int:
        """Count total trades across all stocks."""
        return sum(len(r.trades) for r in results.values())

    # ------------------------------------------------------------------
    def _aggregate_all_metrics(self, results: Dict[str, BacktestResult]) -> Dict[str, Any]:
        """
        Aggregate key metrics across all stocks for reliability assessment.

        Returns a summary dict with averages + per-stock breakdown.
        """
        if not results:
            return {}

        all_trades = []
        per_stock_metrics = {}
        for sym, r in results.items():
            per_stock_metrics[sym] = {
                "trades": r.metrics.get("total_trades", 0),
                "win_rate": r.metrics.get("win_rate", 0),
                "profit_factor": r.metrics.get("profit_factor", 0),
                "max_drawdown": r.metrics.get("max_drawdown", 0),
                "sharpe_ratio": r.metrics.get("sharpe_ratio", 0),
                "total_return_pct": r.metrics.get("total_return_percent", 0),
            }
            all_trades.extend(r.trades)

        total_trades = len(all_trades)
        winning_trades = [t for t in all_trades if t.pnl > 0]
        losing_trades = [t for t in all_trades if t.pnl < 0]

        # Win rate across all trades
        win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0

        # Avg win / avg loss / risk-reward
        avg_win = float(np.mean([t.pnl for t in winning_trades])) if winning_trades else 0
        avg_loss = float(np.mean([t.pnl for t in losing_trades])) if losing_trades else 0
        risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else 0

        # Expectancy (avg P&L per trade)
        expectancy = float(np.mean([t.pnl for t in all_trades])) if all_trades else 0

        # Profit factor across all trades combined
        total_profit = sum(t.pnl for t in winning_trades)
        total_loss_val = abs(sum(t.pnl for t in losing_trades))
        profit_factor = total_profit / total_loss_val if total_loss_val > 0 else float("inf")

        # Max drawdown (average across stocks)
        drawdowns = [m["max_drawdown"] for m in per_stock_metrics.values()]
        avg_drawdown = float(np.mean(drawdowns)) if drawdowns else 0
        max_drawdown = float(np.max(drawdowns)) if drawdowns else 0

        # Sharpe ratio (average across stocks)
        sharpes = [m["sharpe_ratio"] for m in per_stock_metrics.values()
                   if not np.isnan(m["sharpe_ratio"]) and not np.isinf(m["sharpe_ratio"])]
        avg_sharpe = float(np.mean(sharpes)) if sharpes else 0

        # Max consecutive losses
        max_consec_losses = 0
        current_streak = 0
        for t in all_trades:
            if t.pnl < 0:
                current_streak += 1
                max_consec_losses = max(max_consec_losses, current_streak)
            else:
                current_streak = 0

        # Stocks that contributed profits vs losses
        profitable_stocks = sum(1 for m in per_stock_metrics.values() if m["total_return_pct"] > 0)
        losing_stocks = sum(1 for m in per_stock_metrics.values() if m["total_return_pct"] < 0)

        # Avg holding period
        holding_periods = [t.holding_period for t in all_trades if t.holding_period > 0]
        avg_holding = float(np.mean(holding_periods)) if holding_periods else 0

        return {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "risk_reward_ratio": round(risk_reward, 2),
            "expectancy": round(expectancy, 2),
            "sharpe_ratio": round(avg_sharpe, 4),
            "avg_max_drawdown": round(avg_drawdown, 2),
            "worst_max_drawdown": round(max_drawdown, 2),
            "max_consecutive_losses": max_consec_losses,
            "profitable_stocks": profitable_stocks,
            "losing_stocks": losing_stocks,
            "avg_holding_period_days": round(avg_holding, 1),
            "per_stock": per_stock_metrics,
        }

    # ------------------------------------------------------------------
    def _objective(self, trial: Trial) -> float:
        """Single Optuna trial: suggest params → backtest → return metric."""
        # Suggest parameters
        params = {}
        for param_name, spec in self.search_space.items():
            params[param_name] = _suggest(trial, param_name, spec)

        # Validate EMA fast < slow
        if "FAST_EMA_PERIOD" in params and "SLOW_EMA_PERIOD" in params:
            if params["FAST_EMA_PERIOD"] >= params["SLOW_EMA_PERIOD"]:
                return -999.0  # invalid combo

        # Create analyser and get the method, passing params to apply AFTER reset_constants()
        # This fixes the bug where reset_constants() was overwriting optimized params
        _, method = self._create_analyser_and_method(params)

        # Run backtest on training data
        results = self._run_backtest_with_data(
            method, self._train_data, self.train_start, self.train_end
        )

        if not results:
            return -999.0

        return self._aggregate_metric(results)

    # ------------------------------------------------------------------
    def optimize(self) -> Dict[str, Any]:
        """
        Run the optimisation.

        Returns
        -------
        dict with keys:
            best_params        — optimal thresholds
            train_metric       — metric value on training data
            test_metric        — metric value on test data (if provided)
            all_trials         — list of all trial results
        """
        self._init_app_context()

        if not self.search_space:
            logger.error("No search space defined — nothing to optimize.")
            return {"best_params": {}, "train_metric": 0, "test_metric": None}

        n_params = 1
        for spec in self.search_space.values():
            if spec["type"] == "categorical":
                n_params *= len(spec["choices"])
            elif spec["type"] in ("int", "float"):
                rng = spec["high"] - spec["low"]
                step = spec.get("step", 1)
                n_params *= max(1, int(rng / step) + 1)

        logger.info(
            f"Optimizing {self.analyser_class_name}.{self.method_name}  "
            f"({len(self.search_space)} params, ~{n_params} combinations, "
            f"{self.n_trials} trials, {len(self.stock_symbols)} stocks)"
        )

        self.study = optuna.create_study(
            direction="maximize",
            study_name=f"{self.analyser_class_name}_{self.method_name}",
            sampler=optuna.samplers.TPESampler(seed=42),
        )

        self.study.optimize(self._objective, n_trials=self.n_trials, show_progress_bar=True)

        # Extract best
        self.best_params = self.study.best_params
        self.best_train_metric = self.study.best_value

        # Re-run train with best params to get detailed metrics
        # Note: _create_analyser_and_method() now applies best_params AFTER reset_constants()
        _, method = self._create_analyser_and_method()
        train_results = self._run_backtest_with_data(
            method, self._train_data, self.train_start, self.train_end
        )
        self.train_trade_count = self._count_trades(train_results)
        self.train_summary = self._aggregate_all_metrics(train_results)

        # Validate on test set if available
        if self._test_data and self.test_start and self.test_end:
            _, method = self._create_analyser_and_method()
            test_results = self._run_backtest_with_data(
                method, self._test_data, self.test_start, self.test_end
            )
            self.best_test_metric = self._aggregate_metric(test_results)
            self.test_trade_count = self._count_trades(test_results)
            self.test_summary = self._aggregate_all_metrics(test_results)

        # Collect all trials
        self.all_trials = []
        for t in self.study.trials:
            self.all_trials.append({
                "number": t.number,
                "params": t.params,
                "value": t.value,
                "state": str(t.state),
            })

        return {
            "best_params": self.best_params,
            "train_metric": self.best_train_metric,
            "test_metric": self.best_test_metric,
            "train_trades": self.train_trade_count,
            "test_trades": self.test_trade_count,
            "train_summary": self.train_summary,
            "test_summary": self.test_summary,
            "all_trials": self.all_trials,
        }

    # ------------------------------------------------------------------
    def print_results(self):
        """Pretty-print the optimisation results with reliability assessment."""
        print("\n" + "=" * 80)
        print(f"OPTIMISATION RESULTS: {self.analyser_class_name}.{self.method_name}")
        print("=" * 80)
        print(f"Metric optimised : {self.metric}")
        print(f"Stocks tested    : {', '.join(self.stock_symbols)}")
        print(f"Training period  : {self.train_start} → {self.train_end}")
        if self.test_start:
            print(f"Test period      : {self.test_start} → {self.test_end}")
        print(f"Trials run       : {self.n_trials}")
        print("-" * 80)

        print("\nBest parameters:")
        for param, value in self.best_params.items():
            print(f"  {param:<40} = {value}")

        # --- Detailed metrics table ---
        self._print_metrics_comparison()

        # --- Reliability verdict ---
        self._print_reliability_verdict()

        print("=" * 80 + "\n")

    # ------------------------------------------------------------------
    def _print_metrics_comparison(self):
        """Print a side-by-side comparison of train vs test metrics."""
        ts = self.train_summary
        xs = self.test_summary

        print(f"\n{'Metric':<30} {'Train':<20} {'Test':<20}")
        print("-" * 70)

        rows = [
            ("Total Trades",           ts.get("total_trades", "—"),        xs.get("total_trades", "—")),
            ("Win Rate (%)",           ts.get("win_rate", "—"),            xs.get("win_rate", "—")),
            ("Profit Factor",          ts.get("profit_factor", "—"),       xs.get("profit_factor", "—")),
            ("Sharpe Ratio",           ts.get("sharpe_ratio", "—"),        xs.get("sharpe_ratio", "—")),
            ("Expectancy (₹/trade)",   ts.get("expectancy", "—"),          xs.get("expectancy", "—")),
            ("Avg Win (₹)",            ts.get("avg_win", "—"),             xs.get("avg_win", "—")),
            ("Avg Loss (₹)",           ts.get("avg_loss", "—"),            xs.get("avg_loss", "—")),
            ("Risk/Reward Ratio",      ts.get("risk_reward_ratio", "—"),   xs.get("risk_reward_ratio", "—")),
            ("Avg Max Drawdown (%)",   ts.get("avg_max_drawdown", "—"),    xs.get("avg_max_drawdown", "—")),
            ("Worst Max Drawdown (%)", ts.get("worst_max_drawdown", "—"),  xs.get("worst_max_drawdown", "—")),
            ("Max Consecutive Losses", ts.get("max_consecutive_losses", "—"), xs.get("max_consecutive_losses", "—")),
            ("Profitable Stocks",      ts.get("profitable_stocks", "—"),   xs.get("profitable_stocks", "—")),
            ("Losing Stocks",          ts.get("losing_stocks", "—"),       xs.get("losing_stocks", "—")),
            ("Avg Holding (days)",     ts.get("avg_holding_period_days", "—"), xs.get("avg_holding_period_days", "—")),
        ]

        for label, train_val, test_val in rows:
            tv = f"{train_val}" if train_val == "—" else f"{train_val}"
            xv = f"{test_val}" if test_val == "—" or not xs else f"{test_val}"
            print(f"  {label:<30} {tv:<20} {xv:<20}")

    # ------------------------------------------------------------------
    def _print_reliability_verdict(self):
        """Print reliability warnings and overall verdict."""
        ts = self.train_summary
        xs = self.test_summary
        issues = []
        good = []

        # --- Trade count checks ---
        train_trades = ts.get("total_trades", 0)
        test_trades = xs.get("total_trades", 0)

        if train_trades < 30:
            issues.append(f"Very few train trades ({train_trades}) — not statistically significant")
        elif train_trades >= 100:
            good.append(f"Sufficient train trades ({train_trades})")

        if xs and test_trades < 30:
            issues.append(f"Very few test trades ({test_trades}) — test metrics unreliable")
        elif xs and test_trades >= 50:
            good.append(f"Sufficient test trades ({test_trades})")

        # --- Win rate checks ---
        train_wr = ts.get("win_rate", 0)
        if train_wr > 80:
            issues.append(f"Unusually high win rate ({train_wr}%) — may be curve-fitted")
        elif train_wr < 30:
            issues.append(f"Very low win rate ({train_wr}%) — strategy rarely wins")
        elif 40 <= train_wr <= 65:
            good.append(f"Healthy win rate ({train_wr}%)")

        # --- Drawdown checks ---
        worst_dd = ts.get("worst_max_drawdown", 0)
        if worst_dd > 30:
            issues.append(f"Severe worst drawdown ({worst_dd:.1f}%) — high risk of ruin")
        elif worst_dd > 15:
            issues.append(f"Moderate drawdown ({worst_dd:.1f}%) — manageable but watch sizing")
        elif worst_dd <= 15:
            good.append(f"Controlled drawdown (worst {worst_dd:.1f}%)")

        # --- Expectancy check ---
        expectancy = ts.get("expectancy", 0)
        if expectancy <= 0:
            issues.append(f"Negative expectancy (₹{expectancy:.2f}/trade) — loses money on average")
        else:
            good.append(f"Positive expectancy (₹{expectancy:.2f}/trade)")

        # --- Consistency across stocks ---
        profitable = ts.get("profitable_stocks", 0)
        losing = ts.get("losing_stocks", 0)
        total_stocks = profitable + losing
        if total_stocks > 0:
            profitable_pct = profitable / total_stocks * 100
            if profitable_pct < 40:
                issues.append(f"Only {profitable}/{total_stocks} stocks profitable — poor generalisation")
            elif profitable_pct >= 60:
                good.append(f"{profitable}/{total_stocks} stocks profitable — good consistency")

        # --- Consecutive losses check ---
        max_consec = ts.get("max_consecutive_losses", 0)
        if max_consec >= 10:
            issues.append(f"Max {max_consec} consecutive losses — painful losing streaks")
        elif max_consec <= 5:
            good.append(f"Max {max_consec} consecutive losses — manageable")

        # --- Train vs test comparison ---
        if xs and test_trades >= 10:
            train_pf = ts.get("profit_factor", 0)
            test_pf = xs.get("profit_factor", 0)
            if train_pf > 0 and not np.isinf(train_pf):
                pf_ratio = test_pf / train_pf if not np.isinf(test_pf) else 999
                if pf_ratio > 5:
                    issues.append(f"Test profit factor ({test_pf:.2f}) is {pf_ratio:.0f}x train ({train_pf:.2f}) — likely too few test trades")
                elif pf_ratio < 0.3:
                    issues.append(f"Test profit factor ({test_pf:.2f}) << train ({train_pf:.2f}) — overfitting likely")
                elif 0.5 <= pf_ratio <= 2.0:
                    good.append(f"Train/test profit factor consistent ({train_pf:.2f} vs {test_pf:.2f})")

            train_wr = ts.get("win_rate", 0)
            test_wr = xs.get("win_rate", 0)
            if abs(train_wr - test_wr) > 15:
                issues.append(f"Win rate differs: train {train_wr:.1f}% vs test {test_wr:.1f}% — instability")
            elif abs(train_wr - test_wr) <= 10:
                good.append(f"Win rate stable: train {train_wr:.1f}% vs test {test_wr:.1f}%")

        # --- Print verdict ---
        print("\n" + "-" * 80)
        print("RELIABILITY ASSESSMENT")
        print("-" * 80)

        if good:
            for g in good:
                print(f"  ✓  {g}")
        if issues:
            for i in issues:
                print(f"  ⚠  {i}")

        # Overall verdict
        if not issues:
            print("\n  VERDICT: RELIABLE — parameters look robust")
        elif len(issues) <= 2 and len(good) >= 3:
            print("\n  VERDICT: MOSTLY RELIABLE — minor concerns, usable with caution")
        elif len(issues) <= 2:
            print("\n  VERDICT: UNCERTAIN — not enough evidence to be confident")
        else:
            print(f"\n  VERDICT: UNRELIABLE — {len(issues)} issues found, use these parameters with caution")

    # ------------------------------------------------------------------
    def export_results(self, filepath: str):
        """Export results to JSON."""
        # Strip per-stock breakdown for cleaner export
        train_export = {k: v for k, v in self.train_summary.items() if k != "per_stock"}
        test_export = {k: v for k, v in self.test_summary.items() if k != "per_stock"} if self.test_summary else None
        data = {
            "analyser": self.analyser_class_name,
            "method": self.method_name,
            "metric": self.metric,
            "stocks": self.stock_symbols,
            "train_period": [self.train_start, self.train_end],
            "test_period": [self.test_start, self.test_end] if self.test_start else None,
            "n_trials": self.n_trials,
            "best_params": self.best_params,
            "train_metric": self.best_train_metric,
            "test_metric": self.best_test_metric,
            "train_summary": train_export,
            "test_summary": test_export,
            "top_10_trials": sorted(
                self.all_trials, key=lambda x: x["value"] or -999, reverse=True
            )[:10],
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"Results exported to {filepath}")

    # ------------------------------------------------------------------
    def apply_best_params(self):
        """Apply the best parameters to the analyser class (in-memory)."""
        self._set_params(self.best_params)
        logger.info(
            f"Applied best params to {self.analyser_class_name}: {self.best_params}"
        )

    # ------------------------------------------------------------------
    def generate_constants_code(self) -> str:
        """Generate copy-pasteable Python code to set the best thresholds."""
        lines = [
            f"# Optimised thresholds for {self.analyser_class_name}.{self.method_name}",
            f"# Metric: {self.metric} = {self.best_train_metric:.4f} (train)"
            + (f", {self.best_test_metric:.4f} (test)" if self.best_test_metric is not None else ""),
            f"# Stocks: {', '.join(self.stock_symbols)}",
            f"# Period: {self.train_start} → {self.train_end}",
            "",
        ]
        for param, value in self.best_params.items():
            if isinstance(value, float):
                lines.append(f"{self.analyser_class_name}.{param} = {value}")
            elif isinstance(value, int):
                lines.append(f"{self.analyser_class_name}.{param} = {value}")
            else:
                lines.append(f"{self.analyser_class_name}.{param} = {repr(value)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk Optimizer — runs optimisation for ALL methods in an analyser
# ---------------------------------------------------------------------------
class BulkOptimizer:
    """
    Optimise all methods of one or more analysers in a single call.

    Usage:
        bulk = BulkOptimizer(
            stock_symbols=['RELIANCE', 'TCS', 'HDFCBANK'],
            train_start='2023-01-01', train_end='2024-12-31',
            test_start='2025-01-01', test_end='2025-12-31',
        )
        all_results = bulk.optimize_all()
        bulk.print_summary()
        bulk.export_all('optimization_results.json')
    """

    def __init__(
        self,
        stock_symbols: List[str],
        train_start: str,
        train_end: str,
        test_start: Optional[str] = None,
        test_end: Optional[str] = None,
        metric: str = "sharpe_ratio",
        n_trials: int = 100,
        stop_loss_pct: Optional[float] = 3.0,
        target_pct: Optional[float] = 5.0,
        allow_short: bool = True,
        mode: str = "positional",
        analyser_names: Optional[List[str]] = None,
        output_file: Optional[str] = None,
    ):
        self.stock_symbols = stock_symbols
        self.train_start = train_start
        self.train_end = train_end
        self.test_start = test_start
        self.test_end = test_end
        self.metric = metric
        self.n_trials = n_trials
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.allow_short = allow_short
        self.mode = mode
        self.output_file = output_file

        # Which analysers to optimise
        if analyser_names:
            self.analyser_names = analyser_names
        else:
            self.analyser_names = list(SEARCH_SPACES.keys())

        self.results: Dict[str, Dict[str, Any]] = {}  # "Class.method" → result
        self.optimizers: Dict[str, ThresholdOptimizer] = {}

        # Load previously completed results for resume support
        if self.output_file:
            self.results = self._load_existing_results()

    # ------------------------------------------------------------------
    def _load_existing_results(self) -> Dict[str, Dict[str, Any]]:
        """Load previously saved results from the output file (for resume)."""
        if not self.output_file or not os.path.exists(self.output_file):
            return {}
        try:
            with open(self.output_file, "r") as f:
                data = json.load(f)
            loaded = data if isinstance(data, dict) else {}
            if loaded:
                logger.info(
                    f"Resumed from {self.output_file}: "
                    f"{len(loaded)} method(s) already completed — will skip them."
                )
            return loaded
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read {self.output_file} ({e}); starting fresh.")
            return {}

    # ------------------------------------------------------------------
    def _append_result(self, key: str, result: Dict[str, Any]):
        """Persist the current results dict to the output file after each method."""
        if not self.output_file:
            return
        # Build the export-friendly version of all results so far
        export: Dict[str, Any] = {}
        for k, r in self.results.items():
            if "error" not in r:
                ts = r.get("train_summary", {})
                xs = r.get("test_summary", {})
                export[k] = {
                    "best_params": r.get("best_params", {}),
                    "train_metric": r.get("train_metric"),
                    "test_metric": r.get("test_metric"),
                    "train_trades": r.get("train_trades", 0),
                    "test_trades": r.get("test_trades", 0),
                    "train_summary": {kk: v for kk, v in ts.items() if kk != "per_stock"} if ts else {},
                    "test_summary": {kk: v for kk, v in xs.items() if kk != "per_stock"} if xs else {},
                }
            else:
                export[k] = {"error": r["error"]}
        try:
            with open(self.output_file, "w") as f:
                json.dump(export, f, indent=2, default=str)
            logger.info(f"Results for {key} saved to {self.output_file}")
        except IOError as e:
            logger.error(f"Failed to write results to {self.output_file}: {e}")

    # ------------------------------------------------------------------
    def optimize_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Run optimisation for every method that has a search space.

        If *output_file* was provided, each method's result is written to
        disk immediately after it completes.  On a subsequent run the
        already-completed methods are skipped automatically, so you never
        lose progress.
        """
        for analyser_name in self.analyser_names:
            class_spaces = SEARCH_SPACES.get(analyser_name, {})
            for method_name, space in class_spaces.items():
                if not space:
                    continue  # skip empty search spaces

                key = f"{analyser_name}.{method_name}"

                # --- Resume support: skip already-completed methods ---
                if key in self.results and "error" not in self.results[key]:
                    print(f"\n{'━' * 60}")
                    print(f"Skipping (already completed): {key}")
                    print(f"{'━' * 60}")
                    continue

                print(f"\n{'━' * 60}")
                print(f"Optimizing: {key}")
                print(f"{'━' * 60}")

                try:
                    opt = ThresholdOptimizer(
                        analyser_class_name=analyser_name,
                        method_name=method_name,
                        stock_symbols=self.stock_symbols,
                        train_start=self.train_start,
                        train_end=self.train_end,
                        test_start=self.test_start,
                        test_end=self.test_end,
                        metric=self.metric,
                        n_trials=self.n_trials,
                        stop_loss_pct=self.stop_loss_pct,
                        target_pct=self.target_pct,
                        allow_short=self.allow_short,
                        mode=self.mode,
                    )
                    result = opt.optimize()
                    opt.print_results()

                    self.optimizers[key] = opt
                    self.results[key] = result
                except Exception as e:
                    logger.error(f"Optimisation failed for {key}: {e}")
                    self.results[key] = {"error": str(e)}

                # Persist after every method (success or failure)
                self._append_result(key, self.results[key])

        return self.results

    def print_summary(self):
        """Print a summary table of all optimisation results."""
        print("\n" + "=" * 130)
        print("BULK OPTIMISATION SUMMARY")
        print("=" * 130)
        print(
            f"{'Method':<45} "
            f"{'Train ' + self.metric:<20} "
            f"{'Train Trades':<14} "
            f"{'Test ' + self.metric:<20} "
            f"{'Test Trades':<13} "
            f"{'Status':<18}"
        )
        print("-" * 130)

        for key, result in self.results.items():
            if "error" in result:
                print(f"{key:<45} {'ERROR':<20} {'—':<14} {'—':<20} {'—':<13} {result['error'][:18]}")
            else:
                train_val = f"{result['train_metric']:.4f}"
                train_trades = str(result.get("train_trades", "—"))
                test_val = f"{result['test_metric']:.4f}" if result.get("test_metric") is not None else "—"
                test_trades = str(result.get("test_trades", "—"))

                status = "OK"
                test_trade_count = result.get("test_trades", 0)
                train_trade_count = result.get("train_trades", 0)

                if train_trade_count < 30:
                    status = "FEW TRAIN TRADES"
                elif result.get("test_metric") is not None and result["train_metric"] > 0:
                    ratio = result["test_metric"] / result["train_metric"]
                    if test_trade_count < 30:
                        status = "FEW TEST TRADES"
                    elif ratio > 0.7:
                        status = "GOOD"
                    elif ratio < 0.3:
                        status = "OVERFIT?"
                    else:
                        status = "OK"

                print(f"{key:<45} {train_val:<20} {train_trades:<14} {test_val:<20} {test_trades:<13} {status:<18}")

        print("=" * 130 + "\n")

    def export_all(self, filepath: str):
        """Export all results to a single JSON file."""
        export = {}
        for key, result in self.results.items():
            if "error" not in result:
                ts = result.get("train_summary", {})
                xs = result.get("test_summary", {})
                export[key] = {
                    "best_params": result["best_params"],
                    "train_metric": result["train_metric"],
                    "test_metric": result.get("test_metric"),
                    "train_trades": result.get("train_trades", 0),
                    "test_trades": result.get("test_trades", 0),
                    "train_summary": {k: v for k, v in ts.items() if k != "per_stock"} if ts else {},
                    "test_summary": {k: v for k, v in xs.items() if k != "per_stock"} if xs else {},
                }
            else:
                export[key] = {"error": result["error"]}

        with open(filepath, "w") as f:
            json.dump(export, f, indent=2, default=str)
        print(f"All results exported to {filepath}")

    def generate_all_constants_code(self) -> str:
        """Generate Python code for ALL optimised thresholds."""
        lines = [
            "# ============================================================",
            "# AUTO-GENERATED OPTIMISED THRESHOLDS",
            f"# Metric: {self.metric}",
            f"# Stocks: {', '.join(self.stock_symbols)}",
            f"# Train:  {self.train_start} → {self.train_end}",
            "# ============================================================",
            "",
        ]
        for key, opt in self.optimizers.items():
            lines.append(opt.generate_constants_code())
            lines.append("")
        return "\n".join(lines)
