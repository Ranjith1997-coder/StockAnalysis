# StockAnalysis

## Project Overview

StockAnalysis is a comprehensive automated stock market analysis tool designed for Indian equity markets (NSE). It supports both intraday (5-minute intervals) and positional (daily) trading strategies, leveraging multiple data sources and technical analysis techniques to identify trends, generate insights, and provide real-time notifications via Telegram.

## Features

### Analysis Modes
- **Intraday Analysis**: Real-time monitoring during market hours (9:15 AM - 3:30 PM) with 5-minute interval data
- **Positional/EOD Analysis**: End-of-day analysis starting at 4:00 PM with daily data spanning 2 years

### Data Sources & Integration
- **Yahoo Finance**: Primary source for historical and real-time price data
- **NSE (National Stock Exchange)**: Derivatives data including futures and options
- **Zerodha API**: Option chain data and futures metadata for enhanced derivatives analysis
- **StockEdge API**: Post-market analysis including FII/DII flows, sector performance, F&O participant OI, and index returns

### Analysis Modules
- **Volume Analysis**: Detects unusual volume patterns and breakouts
- **Technical Analysis**: Uses multiple technical indicators (RSI, MACD, moving averages, etc.)
- **Candlestick Pattern Analysis**: Identifies key reversal and continuation patterns
- **Implied Volatility (IV) Analysis**: Monitors option chain IV changes
- **Put-Call Ratio (PCR) Analysis**: Monitors PCR changes for market sentiment
- **Max Pain Analysis**: Calculates the max pain strike price where option writers have minimum losses
- **Futures Analysis**: Analyzes futures rollover, OI changes, and premium/discount

### Automated Reports
- **Top Gainers and Losers**: Top 5 stocks by percentage change
- **Index Reports**: Real-time updates on major Indian indices (Nifty 50, Bank Nifty, Fin Nifty, etc.)
- **Global Indices Reports**: Monitors major international indices
  - **USA**: S&P 500, Dow Jones, NASDAQ
  - **Europe**: FTSE 100, DAX, CAC 40
  - **Asia**: Nikkei 225, Hang Seng, Shanghai Composite, KOSPI
- **Commodity Reports**: Tracks precious metals (Gold, Silver, Platinum, Copper), Crude Oil, and USD/INR
- **52-Week High/Low**: Tracks stocks hitting new 52-week highs or lows
- **Post-Market Analysis**: 
  - FII/DII cash and derivatives flows (last 5 days)
  - Sector performance (top 5 gainers/losers)
  - F&O participant OI breakdown
  - NSE Index returns (top 10 gainers/losers)

### Notification System
- **Three Telegram Channels**: Intraday alerts, positional/EOD alerts, and a dedicated real-time options channel
- **Interactive Bot**: Command Router architecture — `/ltp`, `/gainers`, `/losers`, `/watchlist`, `/holidays`, `/straddle`, `/walls`, `/status`, `/enctoken`
- **System Health Dashboard** (`/status`): live feed lag, RAM usage, LLM token budget
- **Options Seller Commands**: `/straddle` (ATM premium, ±1SD range, PCR) and `/walls` (OI walls with tick-level and session delta)

### Additional Features
- **Modular Architecture**: Easily extensible analyzer and data source framework
- **Parallel Processing**: ThreadPoolExecutor for efficient multi-stock analysis
- **Automated Scheduling**: Can auto-start at market open and shutdown system after EOD
- **Selective Analysis**: Command-line arguments to analyze specific stocks or indices
- **Environment-based Configuration**: Feature flags for enabling/disabling components
- **Intelligence Layer**: Cross-layer signal confluence detection (live + intraday + positional) via `SignalCorrelator`; LLM-powered trade narratives via Google Gemini Flash (`ENABLE_INTELLIGENCE`, `ENABLE_NARRATOR`)
- **Holiday-aware Deployment**: `make service-stop` exits early on non-trading days; SSH retry-poll connects the moment the instance is reachable instead of a fixed sleep

## Getting Started

### Prerequisites

- Python 3.7 or higher
- Virtual environment (recommended)
- Required Python packages (listed in `requirements.txt`)

### Installation

1. **Clone the Repository**
   ```bash
   git clone https://github.com/yourusername/StockAnalysis.git
   cd StockAnalysis
   ```

2. **Set Up Virtual Environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows use `.venv\Scripts\activate`
   ```

3. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

### Configuration

Create a `.env` file in the project root with the following environment variables:

#### Core Configuration
- `ENV_PRODUCTION`: Set to `1` for production mode, `0` for development mode
- `ENV_SHUTDOWN`: Set to `1` to enable automatic system shutdown after EOD analysis

#### Mode Selection (Development Only)
- `ENV_DEV_INTRADAY`: Set to `1` to run intraday analysis in dev mode
### Makefile targets

A `Makefile` wraps all common workflows:

```bash
make venv              # Create .venv/
make install           # Install production dependencies
make install-dev       # Install prod + dev/test tools
make install-deploy    # Install deploy tools (boto3, paramiko)
make env-check         # Verify required .env vars are set

make run-prod          # PRODUCTION=1 intraday monitor
make run-dev           # PRODUCTION=0 intraday monitor (safe)
make run-premarket     # Global cues + pre-open report
make run-postmarket    # Post-market analysis pipeline
make deploy            # git pull + restart service on EC2 via SSH
make service-stop      # Start EC2 (if stopped) + stop service; exits early on holidays
make service-stop-force  # Same but bypasses holiday guard (dev/debugging)

make test              # Full test suite
make test-fast         # Stop on first failure
make test-cov          # Coverage report
make lint              # ruff check
make format            # ruff format
make typecheck         # pyright
make logs              # Tail logs/monitor.log (last 50 lines)
make logs-follow       # Follow logs/monitor.log live
make clean             # Remove __pycache__, .pyc, pytest cache
```

#### Basic Usage
Run the main analysis script:
```bash
python intraday/intraday_monitor.py
```

The script automatically determines the mode based on:
- **Production mode**: Uses current time to decide (intraday if before 3:30 PM, positional after)
- **Development mode**: Uses `ENV_DEV_INTRADAY` or `ENV_DEV_POSITIONAL` flags

#### Analyze Specific Stock, Index, or Commodity
```bash
# Analyze a specific stock
python intraday/intraday_monitor.py --stock RELIANCE

# Analyze a specific index
python intraday/intraday_monitor.py --index NIFTY

# Analyze a specific commodity
python intraday/intraday_monitor.py --commodity GOLD

# Analyze a specific global index
python intraday/intraday_monitor.py --global-index SPX
Extending the Project

### Adding a New Analyzer
1. Create a new class inheriting from the analyzer base in `analyser/`
2. Implement the required analysis methods
3. Register it in `intraday_monitor.py` with `orchestrator.register(YourAnalyzer())`

### Adding a New Post-Market Data Source
1. Create a new source file in `post_market_analysis/` inheriting from `PostMarketSource`
2. Implement `fetch_raw()` and `normalize()` methods
3. Add analysis logic in `analysis.py` (`analyse_your_source()`)
4. Create a summary formatter in `summary.py`
5. Register in `registry.py`'s `SOURCE_CLASSES` list

### Example: Adding Custom Indicator
```python
from analyser.BaseAnalyser import BaseAnalyser

class CustomAnalyser(BaseAnalyser):
    def analyse_positional(self, stock):
        # Your analysis logic
        if condition_met:
            stock.add_analysis_reason("Custom signal detected")
            return True
        return False
```

## Known Limitations

- Requires active internet connection for data fetching
- Yahoo Finance data may have occasional delays or gaps
- NSE website structure changes may break derivatives data fetching
- Zerodha enctoken expires and needs manual refresh
- Post-market APIs are third-party and subject to rate limits

## Troubleshooting

### No Data Available
- Check internet connection
- Verify Yahoo Finance is accessible
- Ensure stock symbols are correct (use NSE symbols, not BSE)

### Telegram Notifications Not Working
- Verify bot token and chat ID are correct
- Ensure `ENV_PRODUCTION=1` for notifications to be sent
- Check bot has permission to send messages to the chat

### Zerodha Integration Issues
- Enctoken expires regularly - need to login to Zerodha and extract new token
- Ensure `ENV_ENABLE_ZERODHA_API=1` or `ENV_ENABLE_ZERODHA_DERIVATIVES=1`

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes with appropriate tests
4. Submit a pull request with a clear description

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Disclaimer

This tool is for educational and informational purposes only. It is not financial advice. Always do your own research and consult with a qualified financial advisor before making investment decisions. The authors are not responsible for any financial losses incurred from using this tool.

## Contact

For questions or support, please open an issue on GitHub
**Intraday (single run):**
```bash
# Set ENV_DEV_INTRADAY=1 and ENV_PRODUCTION=0 in .env
python intraday/intraday_monitor.py
```

**Positional:**
```bash
# Set ENV_DEV_POSITIONAL=1 and ENV_PRODUCTION=0 in .env
python intraday/intraday_monitor.py
```

#### Production Deployment
In production mode (`ENV_PRODUCTION=1`):
1. Script waits until 9:15 AM if started before market open
2. Runs intraday analysis from 9:15 AM to 3:30 PM (every 5 minutes)
3. Waits until 4:00 PM and runs EOD positional analysis
4. Optionally shuts down the system if `ENV_SHUTDOWN=1`

#### With Telegram Bot
Enable interactive bot mode:
```bash
# Set ENV_ENABLE_TELEGRAM_BOT=1 in .env
python intraday/intraday_monitor.py
```
This runs the analysis in a separate thread while keeping the bot listener active for commands.

### Project Structure

```
StockAnalysis/
├── analyser/              # Analysis modules (Volume, Technical, IV, PCR, Max Pain, Futures,
│                        #   OI Chain, Candlestick, LiveOI, LiveStraddle, PanicMode)
├── backtest/              # Backtesting framework + Optuna optimizer
├── common/                # Shared utilities, constants, scoring, logging, market calendar
├── configs/               # custom_holidays.json, ml_config.yaml
├── data/                  # final_derivatives_list.json, backtest results, models
├── docs/                  # DESIGN.md — full architectural reference
├── fno/                   # SensibullFetcher, OptionWriteStandardDeviation
├── intelligence/          # SignalBus, SignalCorrelator, MarketNarrator, GeminiClient
├── intraday/              # intraday_monitor.py — main entry point
├── notification/
│   ├── Notification.py      # Telegram sender (3 channels)
│   ├── bot_listener.py      # Thin entry point — registers commands, schedules jobs
│   └── commands/            # Command Router: account, market, system modules
├── nse/                   # NSE API wrappers, date helpers
├── post_market_analysis/  # FII/DII, sector perf, F&O OI, index returns pipeline
├── premarket/             # Global cues, bonds, commodities, pre-open report
├── scripts/
│   ├── deploy.py            # git pull + restart via SSH
│   └── service_stop.py      # Start EC2 + stop service (holiday-aware, SSH retry-poll)
├── sentiment/             # FinBERT news sentiment
├── tests/                 # 951 tests across 41 files
├── zerodha/               # WebSocket lifecycle, TickStore, LiveOptionsEngine, LiveStockEngine
├── Makefile               # All common targets (see above)
└── .env.template          # Copy to .env and fill in your credentials
```

### Key Files
- `intraday/intraday_monitor.py`: Main entry point for analysis
- `post_market_analysis/runner.py`: Post-market analysis pipeline
- `common/Stock.py`: Stock data model and price data management
- `analyser/Analyser.py`: Orchestrator for running multiple analyzers
- `notification/Notification.py`: Telegram notification sender
- `notification/bot_listener.py`: Interactive Telegram bot
- `TELEGRAM_POSITIONAL_CHAT_ID`: Chat ID for positional channel

#### Zerodha Configuration (if enabled)
- `ENV_ZERODHA_USERNAME`: Zerodha user ID
- `ENV_ZERODHA_PASSWORD`: Zerodha password
- `ENV_ZERODHA_ENC_TOKEN`: Zerodha enctoken for API authentication

#### Stock/Index Selection
- `NO_OF_STOCKS`: Limit number of stocks to analyze (use `-1` for all in production)
- `NO_OF_INDEX`: Limit number of indices to analyze (use `-1` for all in production)

### Usage

- **Run Intraday Analysis**
  ```bash
  python intraday/intraday_monitor.py
  ```

- **Run Positional Analysis**
  ```bash
  python intraday/intraday_monitor.py
  ```

### Example

The project includes example scripts and configurations to help you get started quickly. Modify the parameters in `intraday_monitor.py` to suit your analysis needs.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request for any enhancements or bug fixes.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Contact

For questions or support, please contact [yourname@domain.com](mailto:yourname@domain.com).

```

### Explanation:
- **Project Overview**: Provides a brief introduction to the project and its purpose.
- **Features**: Lists the key features of the project.
- **Getting Started**: Includes prerequisites, installation steps, and configuration details.
- **Usage**: Describes how to run the analysis scripts.
- **Example**: Mentions the availability of example scripts.
- **Contributing**: Encourages contributions from the community.
- **License**: States the licensing information.
- **Contact**: Provides contact information for support or inquiries.

Feel free to customize the content to better fit your project's specifics and your personal or organizational preferences.