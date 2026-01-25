# Backtesting Infrastructure

A comprehensive backtesting framework for testing trading strategies using your existing analyzer functions.

## Features

- ✅ **Flexible Strategy Testing**: Test any analyzer method from your BaseAnalyzer classes
- ✅ **Multiple Stocks**: Backtest across multiple stocks simultaneously
- ✅ **Combined Strategies**: Test multiple analyzer methods together
- ✅ **Position Management**: Configurable position sizing, stop loss, and targets
- ✅ **Risk Management**: Support for stop losses, targets, and max positions
- ✅ **Long/Short**: Support for both bullish and bearish trades
- ✅ **Detailed Metrics**: Comprehensive performance metrics (Sharpe ratio, drawdown, profit factor, etc.)
- ✅ **Trade Analysis**: Detailed trade-by-trade breakdown
- ✅ **Easy Integration**: Works seamlessly with existing analyzers

## Quick Start

### Basic Usage

```python
from backtest.backtest import Backtester
from analyser.TechnicalAnalyser import TechnicalAnalyser

# Initialize analyzer
technical_analyzer = TechnicalAnalyser()
technical_analyzer.reset_constants()

# Create backtester
backtester = Backtester(
    stock_symbols='RELIANCE',
    analyzer_methods=technical_analyzer.analyse_rsi,
    start_date='2024-01-01',
    end_date='2025-12-31',
    initial_capital=100000,
    position_size=20000,
    stop_loss_pct=3,
    target_pct=5
)

# Run backtest
results = backtester.run_all()

# Generate report
backtester.generate_report()
```

## Parameters

### Backtester Initialization

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `stock_symbols` | str or List[str] | Stock symbol(s) to backtest | Required |
| `analyzer_methods` | Callable or List[Callable] | Analyzer method(s) to test | Required |
| `start_date` | str or datetime | Start date for backtest | Required |
| `end_date` | str or datetime | End date for backtest | Required |
| `initial_capital` | float | Starting capital | 100000 |
| `position_size_method` | str | 'equal', 'percentage', 'kelly' | 'equal' |
| `position_size` | float | Amount per trade or % of capital | 10000 |
| `stop_loss_pct` | float | Stop loss percentage | None |
| `target_pct` | float | Target profit percentage | None |
| `trailing_stop` | bool | Enable trailing stop loss | False |
| `allow_short` | bool | Allow bearish/short trades | True |
| `max_positions` | int | Max concurrent positions | 1 |
| `timeframe` | str | 'intraday' or 'positional' | 'positional' |
| `interval` | str | Data interval ('day', '5minute', etc.) | 'day' |

## Examples

### 1. Single Stock, Single Strategy

```python
from backtest.backtest import Backtester
from analyser.TechnicalAnalyser import TechnicalAnalyser

technical_analyzer = TechnicalAnalyser()
technical_analyzer.reset_constants()

backtester = Backtester(
    stock_symbols='TCS',
    analyzer_methods=technical_analyzer.analyse_ema_crossover,
    start_date='2024-01-01',
    end_date='2025-12-31',
    initial_capital=100000,
    stop_loss_pct=2,
    target_pct=5
)

results = backtester.run_all()
backtester.generate_report()
```

### 2. Multiple Stocks

```python
stocks = ['TCS', 'INFY', 'HDFCBANK', 'ICICIBANK']

backtester = Backtester(
    stock_symbols=stocks,
    analyzer_methods=technical_analyzer.analyse_rsi,
    start_date='2024-01-01',
    end_date='2025-12-31',
    initial_capital=500000,
    position_size_method='percentage',
    position_size=20  # 20% per trade
)

results = backtester.run_all()
```

### 3. Combined Strategies

```python
from analyser.VolumeAnalyser import VolumeAnalyser

technical_analyzer = TechnicalAnalyser()
volume_analyzer = VolumeAnalyser()

analyzer_methods = [
    technical_analyzer.analyse_rsi,
    technical_analyzer.analyse_ema_crossover,
    volume_analyzer.analyse_volume_and_price
]

backtester = Backtester(
    stock_symbols='NIFTY',
    analyzer_methods=analyzer_methods,
    start_date='2024-01-01',
    end_date='2025-12-31',
    initial_capital=200000
)

results = backtester.run_all()
```

### 4. Strategy Comparison

```python
strategies = {
    'RSI': [technical_analyzer.analyse_rsi],
    'EMA': [technical_analyzer.analyse_ema_crossover],
    'MACD': [technical_analyzer.analyze_macd],
}

results_comparison = {}

for strategy_name, methods in strategies.items():
    backtester = Backtester(
        stock_symbols='SBIN',
        analyzer_methods=methods,
        start_date='2024-01-01',
        end_date='2025-12-31',
        initial_capital=100000
    )
    backtester.run_all()
    results_comparison[strategy_name] = backtester.results['SBIN'].metrics

# Compare performance
for strategy, metrics in results_comparison.items():
    print(f"{strategy}: Return={metrics['total_return_percent']:.2f}%, "
          f"Win Rate={metrics['win_rate']:.2f}%")
```

## Performance Metrics

The backtester calculates the following metrics:

| Metric | Description |
|--------|-------------|
| Total Trades | Number of completed trades |
| Winning Trades | Number of profitable trades |
| Losing Trades | Number of losing trades |
| Win Rate | Percentage of winning trades |
| Total Return | Absolute profit/loss |
| Total Return % | Percentage return on capital |
| Average Win | Average profit per winning trade |
| Average Loss | Average loss per losing trade |
| Max Win | Largest single profit |
| Max Loss | Largest single loss |
| Profit Factor | Gross profit / Gross loss |
| Risk/Reward Ratio | Avg Win / Avg Loss |
| Max Drawdown | Maximum peak-to-trough decline |
| Sharpe Ratio | Risk-adjusted return |
| Expectancy | Expected value per trade |
| Avg Holding Period | Average days held per position |

## Accessing Results

### Get Trade Summary

```python
# Get trades for specific stock
trades_df = backtester.get_trades_dataframe('RELIANCE')
print(trades_df)

# Get all trades
all_trades = backtester.get_trades_dataframe()
```

### Access Metrics

```python
# Get metrics for specific stock
metrics = backtester.results['RELIANCE'].metrics
print(f"Win Rate: {metrics['win_rate']:.2f}%")
print(f"Total Return: {metrics['total_return_percent']:.2f}%")
print(f"Sharpe Ratio: {metrics['sharpe_ratio']:.2f}")
```

### Export Results

```python
# Save to JSON file
backtester.generate_report(output_file='backtest_results.json')

# Print summary to console
backtester.generate_report()
```

## Trade Object Structure

Each trade contains:

```python
{
    'entry_date': datetime,
    'entry_price': float,
    'exit_date': datetime,
    'exit_price': float,
    'signal_type': 'BULLISH' or 'BEARISH',
    'position_size': float,
    'pnl': float,
    'pnl_percent': float,
    'holding_period': int,
    'exit_reason': 'target' | 'stop_loss' | 'signal_reversal' | 'end_of_data',
    'stop_loss': float,
    'target': float,
    'analysis_details': dict  # Analyzer outputs that triggered the trade
}
```

## Position Sizing Methods

### Equal Position Size
```python
position_size_method='equal'
position_size=20000  # Fixed ₹20,000 per trade
```

### Percentage of Capital
```python
position_size_method='percentage'
position_size=20  # 20% of available capital per trade
```

## Risk Management

### Stop Loss
```python
stop_loss_pct=3  # Exit if price drops 3% (long) or rises 3% (short)
```

### Target
```python
target_pct=5  # Exit if price rises 5% (long) or drops 5% (short)
```

### Max Positions
```python
max_positions=2  # Maximum 2 concurrent positions
```

### Allow/Disallow Shorting
```python
allow_short=True   # Allow bearish trades
allow_short=False  # Only long positions
```

## Advanced Usage

### Custom Analyzer Function

You can test any function that follows the analyzer pattern:

```python
def custom_strategy(stock: Stock):
    # Your custom logic
    if some_condition:
        stock.set_analysis("BULLISH", "CUSTOM", {"reason": "..."})
        return True
    return False

backtester = Backtester(
    stock_symbols='NIFTY',
    analyzer_methods=custom_strategy,
    ...
)
```

### Testing Index Options

```python
# Test on NIFTY or BANKNIFTY
backtester = Backtester(
    stock_symbols=['NIFTY', 'BANKNIFTY'],
    analyzer_methods=technical_analyzer.analyse_rsi,
    ...
)
```

## Tips for Better Backtesting

1. **Use Sufficient Historical Data**: Ensure your indicators have enough data to calculate properly
2. **Realistic Transaction Costs**: Consider adding slippage and brokerage (to be implemented)
3. **Avoid Over-Optimization**: Don't tune parameters to fit historical data perfectly
4. **Out-of-Sample Testing**: Test on data not used for parameter optimization
5. **Multiple Timeframes**: Test across different market conditions
6. **Compare to Buy & Hold**: Always benchmark against simple buy-and-hold strategy

## Running Examples

```bash
cd /Users/rkumark/Ranjith/StockAnalysis
python backtest/backtest_example.py
```

See `backtest_example.py` for 8 different example scenarios.

## Future Enhancements

- [ ] Intraday backtesting with 5-minute data
- [ ] Transaction costs (brokerage, slippage)
- [ ] Walk-forward analysis
- [ ] Monte Carlo simulation
- [ ] Portfolio-level backtesting
- [ ] Optimization framework
- [ ] Visual charts (equity curve, drawdown)
- [ ] Integration with live trading

## Troubleshooting

### "No data found for stock"
- Check if the stock symbol is correct
- Verify the date range includes trading days
- Ensure you have internet connection for data download

### "Not enough data for indicators"
- Indicators like EMA(200) need 200+ days of data
- The backtester loads extra buffer data automatically
- Check if your start_date is too close to stock listing date

### "No trades executed"
- Your strategy conditions might be too strict
- Verify analyzer methods are generating signals
- Check if stop_loss/target parameters are realistic

## Support

For issues or questions, check:
- `backtest_example.py` for usage examples
- Analyzer implementations in `analyser/` directory
- Stock class in `common/Stock.py`
