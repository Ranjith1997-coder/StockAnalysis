"""
Pre-Market Report Generator
============================
Generates a comprehensive pre-market analysis report before Indian market open (9:15 AM).

Sections:
1. Global Market Cues (US, Europe, Asia)
2. US Bond Yields (13-Week, 10-Year, 30-Year + yield curve)
3. Commodities & Currencies (Crude, Gold, Silver, USD/INR)
4. India VIX (level + 5-day trend)
5. FII/DII Activity (previous day from StockEdge)
6. NSE Pre-Open Session (top gainers/losers, buy/sell ratio)

Data Sources:
- yfinance: Global indices, bond yields, commodities, currencies, India VIX
  (all fetched in a SINGLE download call to avoid thread-safety issues)
- StockEdge API: FII/DII activity
- NSE API: Pre-open session data

Scheduling:
- Run at ~9:07 AM (after NSE pre-open session data is available, before 9:15 open)
- Global cues & bond yields can be fetched anytime (24/7 markets)
- NSE pre-open data only available 9:00-9:08 AM
"""

import requests
import yfinance as yf
import pandas as pd
import traceback
import time as time_module
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from common.logging_util import logger
from notification.Notification import TELEGRAM_NOTIFICATIONS
from nse.nse_utils import nse_urlfetch


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration — Tickers & URLs
# ═══════════════════════════════════════════════════════════════════════════════

# Global Indices (yfinance tickers)
GLOBAL_INDICES = {
    "US": {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "Dow Jones": "^DJI",
    },
    "Europe": {
        "DAX": "^GDAXI",
        "FTSE 100": "^FTSE",
    },
    "Asia": {
        "Nikkei 225": "^N225",
        "Hang Seng": "^HSI",
        "Shanghai": "000001.SS",
    },
}

# US Bond Yields
BOND_YIELD_TICKERS = {
    "US 13-Week": "^IRX",     # 13-week T-bill (short end)
    "US 10-Year": "^TNX",     # 10-year Treasury (most important)
    "US 30-Year": "^TYX",     # 30-year Treasury (long end)
}

# Commodities
COMMODITY_TICKERS = {
    "Brent Crude": "BZ=F",
    "Gold": "GC=F",
    "Silver": "SI=F",
}

# Currencies
CURRENCY_TICKERS = {
    "USD/INR": "USDINR=X",
}

# India VIX
INDIA_VIX_TICKER = "^INDIAVIX"

# FII/DII API (StockEdge — same source as post_market_analysis/fii_dii.py)
FII_DII_URL = "https://api.stockedge.com/Api/FIIDashboardApi/GetLatestFIIActivities?lang=en"
FII_DII_RETRIES = 3
FII_DII_TIMEOUT = 15

# NSE Pre-Open Session
NSE_PREOPEN_NIFTY_URL = "https://www.nseindia.com/api/market-data-pre-open?key=NIFTY"


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_float(val):
    """Safely convert a pandas scalar / Series element to float."""
    try:
        if hasattr(val, "iloc"):
            val = val.iloc[0]
        f = float(val)
        return f if pd.notna(f) else None
    except Exception:
        return None


def _chg(pct):
    """Return a colored dot + formatted change string for a percentage."""
    if pct > 0:
        return f"\U0001F7E2 +{pct:.2f}%"     # green circle
    elif pct < 0:
        return f"\U0001F534 {pct:.2f}%"       # red circle
    else:
        return f"\u26AA 0.00%"                 # white circle


def _extract_ticker_data(data, ticker):
    """Extract a single ticker's OHLCV DataFrame from a multi-ticker yf.download result.

    With group_by='ticker', the MultiIndex columns are (Ticker, Field).
    """
    if data is None or data.empty:
        return None

    if not isinstance(data.columns, pd.MultiIndex):
        # Single ticker result — return as-is
        return data

    # group_by='ticker' → level-0 is ticker symbol
    if ticker in data.columns.get_level_values(0):
        return data[ticker]

    # Fallback: level-0 is field, level-1 is ticker (no group_by)
    if ticker in data.columns.get_level_values(1):
        return data.xs(ticker, axis=1, level=1)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PreMarketReport Class
# ═══════════════════════════════════════════════════════════════════════════════

class PreMarketReport:
    """Generates comprehensive pre-market analysis report."""

    def __init__(self):
        self.report_sections = {}
        self.errors = []
        # Collects ALL yfinance tickers to fetch in one call
        self._yf_ticker_map = {}  # ticker -> (category, display_name, extra_info)
        self._yf_data = None

    def generate_global_report(self):
        """Fetch global cues, bond yields, commodities, FII/DII, India VIX.

        These are available 24/7 and can be sent immediately at startup.
        yfinance data is fetched in ONE call; FII/DII runs in parallel.
        """
        logger.info("Starting global cues report generation...")

        self._build_ticker_map()

        with ThreadPoolExecutor(max_workers=2) as executor:
            yf_future = executor.submit(self._fetch_all_yfinance_data)
            fii_future = executor.submit(self._fetch_fii_dii)

            try:
                self._yf_data = yf_future.result()
            except Exception as e:
                logger.error(f"yfinance download failed: {e}")
                self._yf_data = None

            try:
                self.report_sections["fii_dii"] = fii_future.result()
            except Exception as e:
                logger.error(f"FII/DII fetch failed: {e}")
                self.errors.append("fii_dii")
                self.report_sections["fii_dii"] = None

        self._parse_global_cues()
        self._parse_bond_yields()
        self._parse_commodities_currencies()
        self._parse_india_vix()

        report = self._format_global_report()
        logger.info("Global cues report generation completed.")
        return report

    def generate_preopen_report(self):
        """Fetch NSE pre-open session data (available 9:00-9:08 AM only)."""
        logger.info("Starting pre-open session report generation...")

        try:
            self.report_sections["preopen"] = self._fetch_nse_preopen()
        except Exception as e:
            logger.error(f"NSE pre-open fetch failed: {e}")
            self.errors.append("preopen")
            self.report_sections["preopen"] = None

        report = self._format_preopen_report()
        logger.info("Pre-open session report generation completed.")
        return report

    # ═══════════════════════════════════════════════════════════════════════════
    # Ticker Map & Unified yfinance Download
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_ticker_map(self):
        """Build a unified map of all yfinance tickers to download."""
        # Global indices
        for region, indices in GLOBAL_INDICES.items():
            for name, ticker in indices.items():
                self._yf_ticker_map[ticker] = ("global", name, {"region": region})

        # Bond yields
        for name, ticker in BOND_YIELD_TICKERS.items():
            self._yf_ticker_map[ticker] = ("bond", name, {})

        # Commodities
        for name, ticker in COMMODITY_TICKERS.items():
            self._yf_ticker_map[ticker] = ("commodity", name, {"is_currency": False})

        # Currencies
        for name, ticker in CURRENCY_TICKERS.items():
            self._yf_ticker_map[ticker] = ("currency", name, {"is_currency": True})

        # India VIX
        self._yf_ticker_map[INDIA_VIX_TICKER] = ("vix", "India VIX", {})

    def _fetch_all_yfinance_data(self):
        """Single yfinance download for all tickers."""
        all_tickers = list(self._yf_ticker_map.keys())
        logger.info(f"Downloading {len(all_tickers)} tickers from yfinance...")

        data = yf.download(
            all_tickers,
            period="10d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
        )

        logger.info(f"yfinance download complete. Shape: {data.shape}")
        return data

    def _get_close_prices(self, ticker):
        """Get close price Series for a ticker from the unified download."""
        if self._yf_data is None:
            return None

        ticker_data = _extract_ticker_data(self._yf_data, ticker)
        if ticker_data is None:
            return None

        try:
            close = ticker_data["Close"]
            # If close is a DataFrame (MultiIndex residue), flatten it
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            return close.dropna()
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # Parsing yfinance data into sections
    # ═══════════════════════════════════════════════════════════════════════════

    def _parse_global_cues(self):
        """Parse global index data from the unified download."""
        results = {}
        for region, indices in GLOBAL_INDICES.items():
            for name, ticker in indices.items():
                close = self._get_close_prices(ticker)
                if close is None or len(close) < 2:
                    logger.warning(f"Insufficient data for {name} ({ticker})")
                    continue
                current = _safe_float(close.iloc[-1])
                prev = _safe_float(close.iloc[-2])
                if current and prev and prev != 0:
                    change_pct = ((current - prev) / prev) * 100
                    results[name] = {
                        "region": region,
                        "price": current,
                        "change_pct": change_pct,
                    }

        self.report_sections["global_cues"] = results if results else None

    def _parse_bond_yields(self):
        """Parse bond yield data from the unified download."""
        results = {}
        for name, ticker in BOND_YIELD_TICKERS.items():
            close = self._get_close_prices(ticker)
            if close is None or len(close) < 2:
                logger.warning(f"Insufficient data for {name} ({ticker})")
                continue
            current = _safe_float(close.iloc[-1])
            prev = _safe_float(close.iloc[-2])
            if current is not None and prev is not None:
                change_bps = (current - prev) * 100
                results[name] = {
                    "yield_pct": current,
                    "change_bps": change_bps,
                    "prev_yield": prev,
                }

        # Yield curve: 13-Week vs 10-Year spread
        if "US 13-Week" in results and "US 10-Year" in results:
            short_yield = results["US 13-Week"]["yield_pct"]
            long_yield = results["US 10-Year"]["yield_pct"]
            spread = long_yield - short_yield
            results["13W-10Y Spread"] = {
                "spread_pct": spread,
                "inverted": spread < 0,
            }

        self.report_sections["bond_yields"] = results if results else None

    def _parse_commodities_currencies(self):
        """Parse commodity and currency data from the unified download."""
        results = {}

        for name, ticker in COMMODITY_TICKERS.items():
            close = self._get_close_prices(ticker)
            if close is None or len(close) < 2:
                logger.warning(f"Insufficient data for {name} ({ticker})")
                continue
            current = _safe_float(close.iloc[-1])
            prev = _safe_float(close.iloc[-2])
            if current and prev and prev != 0:
                change_pct = ((current - prev) / prev) * 100
                results[name] = {
                    "price": current,
                    "change_pct": change_pct,
                    "is_currency": False,
                }

        for name, ticker in CURRENCY_TICKERS.items():
            close = self._get_close_prices(ticker)
            if close is None or len(close) < 2:
                logger.warning(f"Insufficient data for {name} ({ticker})")
                continue
            current = _safe_float(close.iloc[-1])
            prev = _safe_float(close.iloc[-2])
            if current and prev and prev != 0:
                change_pct = ((current - prev) / prev) * 100
                results[name] = {
                    "price": current,
                    "change_pct": change_pct,
                    "is_currency": True,
                }

        self.report_sections["commodities"] = results if results else None

    def _parse_india_vix(self):
        """Parse India VIX data from the unified download."""
        close = self._get_close_prices(INDIA_VIX_TICKER)
        if close is None or len(close) < 2:
            logger.warning("Insufficient India VIX data")
            self.report_sections["india_vix"] = None
            return

        current = _safe_float(close.iloc[-1])
        prev = _safe_float(close.iloc[-2])
        if current is None or prev is None or prev == 0:
            self.report_sections["india_vix"] = None
            return

        change_pct = ((current - prev) / prev) * 100

        # 5-day trend
        trend = "N/A"
        if len(close) >= 5:
            recent = [_safe_float(close.iloc[i]) for i in range(-5, 0)]
            recent = [v for v in recent if v is not None]
            if len(recent) >= 4:
                rising_days = sum(1 for i in range(len(recent) - 1)
                                  if recent[i + 1] > recent[i])
                if rising_days >= 3:
                    trend = "RISING"
                elif rising_days <= 1:
                    trend = "FALLING"
                else:
                    trend = "STABLE"

        self.report_sections["india_vix"] = {
            "vix": current,
            "change_pct": change_pct,
            "prev_vix": prev,
            "trend": trend,
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # API Fetching (FII/DII, NSE Pre-Open)
    # ═══════════════════════════════════════════════════════════════════════════

    def _fetch_fii_dii(self):
        """Fetch FII/DII activity data from StockEdge API (with retry)."""
        last_err = None
        for attempt in range(FII_DII_RETRIES):
            try:
                r = requests.get(FII_DII_URL, timeout=FII_DII_TIMEOUT)
                if r.status_code == 200:
                    return self._parse_fii_dii(r.json())
                last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
            time_module.sleep(2)

        logger.error(f"Failed to fetch FII/DII data after {FII_DII_RETRIES} attempts: {last_err}")
        return None

    def _parse_fii_dii(self, raw):
        """Parse StockEdge FII/DII API response into structured dict."""
        if not raw or not isinstance(raw, list):
            return None

        latest = raw[0] if raw else None
        if not latest:
            return None

        date_str = latest.get("Date", "")
        fii_dii_data = latest.get("FIIDIIData", [])

        result = {"date": date_str, "categories": {}}

        for bucket in fii_dii_data:
            short = bucket.get("ShortName", "")
            category = {
                "name": bucket.get("Name", ""),
                "short": short,
                "value": bucket.get("Value"),
                "children": {},
            }
            for child in (bucket.get("ChildData") or []):
                child_short = child.get("ShortName", "")
                category["children"][child_short] = {
                    "name": child.get("Name", ""),
                    "value": child.get("Value"),
                }
            result["categories"][short] = category

        return result

    def _fetch_nse_preopen(self):
        """Fetch NSE pre-open session data for Nifty 50.
        Only meaningful between 9:00-9:08 AM IST."""
        try:
            response = nse_urlfetch(NSE_PREOPEN_NIFTY_URL)
            if response.status_code != 200:
                logger.warning(f"NSE pre-open fetch failed: HTTP {response.status_code}")
                return None

            data = response.json()
            preopen_data = data.get("data", [])
            if not preopen_data:
                return None

            gainers = []
            losers = []
            total_buy_qty = 0
            total_sell_qty = 0

            for item in preopen_data:
                metadata = item.get("metadata", {})

                symbol = metadata.get("symbol", "")
                final_price = metadata.get("finalPrice", 0)
                prev_close = metadata.get("previousClose", 0)
                pchange = metadata.get("pChange", 0)
                final_quantity = metadata.get("finalQuantity", 0)
                total_buy = metadata.get("totalBuyQuantity", 0)
                total_sell = metadata.get("totalSellQuantity", 0)

                total_buy_qty += total_buy
                total_sell_qty += total_sell

                entry = {
                    "symbol": symbol,
                    "price": final_price,
                    "prev_close": prev_close,
                    "change_pct": pchange,
                    "volume": final_quantity,
                    "buy_qty": total_buy,
                    "sell_qty": total_sell,
                }

                if pchange > 0:
                    gainers.append(entry)
                elif pchange < 0:
                    losers.append(entry)

            gainers.sort(key=lambda x: x["change_pct"], reverse=True)
            losers.sort(key=lambda x: x["change_pct"])

            buy_sell_ratio = (total_buy_qty / total_sell_qty) if total_sell_qty > 0 else 0

            # Detect abnormal pre-open volume (stocks > 2.5x average volume)
            volumes = [item.get("metadata", {}).get("finalQuantity", 0) for item in preopen_data]
            avg_volume = sum(volumes) / len(volumes) if volumes else 0
            high_volume_stocks = []
            for item in preopen_data:
                metadata = item.get("metadata", {})
                vol = metadata.get("finalQuantity", 0)
                if avg_volume > 0 and vol > 2.5 * avg_volume:
                    high_volume_stocks.append({
                        "symbol": metadata.get("symbol", ""),
                        "volume": vol,
                        "multiple": vol / avg_volume,
                    })
            high_volume_stocks.sort(key=lambda x: x["multiple"], reverse=True)

            return {
                "top_gainers": gainers[:5],
                "top_losers": losers[:5],
                "total_buy_qty": total_buy_qty,
                "total_sell_qty": total_sell_qty,
                "buy_sell_ratio": buy_sell_ratio,
                "total_stocks": len(preopen_data),
                "gainers_count": len(gainers),
                "losers_count": len(losers),
                "high_volume_stocks": high_volume_stocks[:5],
            }

        except Exception as e:
            logger.error(f"Error fetching NSE pre-open data: {e}")
            logger.error(traceback.format_exc())
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # Report Formatting
    # ═══════════════════════════════════════════════════════════════════════════

    def _format_global_report(self):
        """Format global cues, bond yields, commodities, FII/DII, India VIX (HTML)."""
        now = datetime.now()
        report_parts = [
            f"\U0001F4CA <b>GLOBAL CUES &amp; FII/DII</b>",
            f"<i>{now.strftime('%d %b %Y, %I:%M %p')}</i>",
        ]

        report_parts.append(self._format_global_cues())
        report_parts.append(self._format_bond_yields())
        report_parts.append(self._format_commodities())
        report_parts.append(self._format_india_vix())
        report_parts.append(self._format_fii_dii())

        if self.errors:
            report_parts.append(f"\n<i>[Data unavailable: {', '.join(self.errors)}]</i>")

        return "\n".join([p for p in report_parts if p])

    def _format_preopen_report(self):
        """Format NSE pre-open session data as a standalone report (HTML)."""
        now = datetime.now()
        report_parts = [
            f"\U0001F514 <b>NSE PRE-OPEN SESSION</b>",
            f"<i>{now.strftime('%d %b %Y, %I:%M %p')}</i>",
        ]

        report_parts.append(self._format_preopen())

        if self.errors:
            report_parts.append(f"\n<i>[Data unavailable: {', '.join(self.errors)}]</i>")

        return "\n".join([p for p in report_parts if p])

    def _format_global_cues(self):
        """Format global market cues section (HTML)."""
        data = self.report_sections.get("global_cues")
        if not data:
            return "\n\U0001F30D <b>Global Cues</b>\n<i>Data unavailable</i>"

        lines = [f"\n\U0001F30D <b>Global Cues</b>"]

        up_count = sum(1 for v in data.values() if v["change_pct"] > 0)
        down_count = sum(1 for v in data.values() if v["change_pct"] < 0)

        region_icons = {"US": "\U0001F1FA\U0001F1F8", "Europe": "\U0001F1EA\U0001F1FA", "Asia": "\U0001F30F"}
        for region in ["US", "Europe", "Asia"]:
            region_indices = [(k, v) for k, v in data.items() if v["region"] == region]
            if region_indices:
                icon = region_icons.get(region, "")
                lines.append(f"{icon} <b>{region}</b>")
                for name, info in region_indices:
                    lines.append(f"  {name}: <code>{info['price']:>10,.2f}</code>  {_chg(info['change_pct'])}")

        # Overall global sentiment
        total = len(data)
        if total > 0:
            if up_count >= total - 1:
                lines.append("\U0001F7E2 <i>Global markets broadly positive</i>")
            elif down_count >= total - 1:
                lines.append("\U0001F534 <i>Global markets broadly negative</i>")

        return "\n".join(lines)

    def _format_bond_yields(self):
        """Format US bond yields section with signals (HTML)."""
        data = self.report_sections.get("bond_yields")
        if not data:
            return "\n\U0001F4C8 <b>US Bond Yields</b>\n<i>Data unavailable</i>"

        lines = [f"\n\U0001F4C8 <b>US Bond Yields</b>"]

        for name in ["US 13-Week", "US 10-Year", "US 30-Year"]:
            if name in data:
                info = data[name]
                sign = "+" if info["change_bps"] >= 0 else ""
                bps_str = f"{sign}{info['change_bps']:.0f} bps"
                warning = " \u26A0\uFE0F" if abs(info["change_bps"]) >= 10 else ""
                lines.append(
                    f"  {name}: <code>{info['yield_pct']:.2f}%</code>  ({bps_str}){warning}"
                )

        # Yield curve spread
        if "13W-10Y Spread" in data:
            spread_info = data["13W-10Y Spread"]
            if spread_info["inverted"]:
                status = "\U0001F534 INVERTED"
            else:
                status = "\U0001F7E2 Normal"
            lines.append(f"  Yield Curve (13W-10Y): <code>{spread_info['spread_pct']:+.2f}%</code> {status}")

        # Actionable signals
        ten_year = data.get("US 10-Year")
        if ten_year:
            if ten_year["change_bps"] >= 10:
                lines.append("\u26A0\uFE0F <i>10Y rising sharply → FII outflow risk elevated</i>")
            elif ten_year["change_bps"] <= -10:
                lines.append("\U0001F7E2 <i>10Y falling → Risk-on, favorable for EMs</i>")
            if ten_year["yield_pct"] >= 5.0:
                lines.append("\U0001F6A8 <i>10Y above 5% → structural headwind for EMs</i>")

        return "\n".join(lines)

    def _format_commodities(self):
        """Format commodities and currencies section with signals (HTML)."""
        data = self.report_sections.get("commodities")
        if not data:
            return "\n\U0001F6E2\uFE0F <b>Commodities &amp; FX</b>\n<i>Data unavailable</i>"

        lines = [f"\n\U0001F6E2\uFE0F <b>Commodities &amp; FX</b>"]

        for name in ["Brent Crude", "Gold", "Silver", "USD/INR"]:
            if name in data:
                info = data[name]
                prefix = "" if info.get("is_currency") else "$"
                lines.append(f"  {name}: <code>{prefix}{info['price']:.2f}</code>  {_chg(info['change_pct'])}")

        # Crude oil signal
        crude = data.get("Brent Crude")
        if crude and abs(crude["change_pct"]) >= 3:
            if crude["change_pct"] > 0:
                lines.append("\U0001F534 <i>Crude surging → negative for Indian market</i>")
            else:
                lines.append("\U0001F7E2 <i>Crude falling sharply → positive for OMCs &amp; market</i>")

        # INR signal
        inr = data.get("USD/INR")
        if inr and abs(inr["change_pct"]) >= 0.5:
            if inr["change_pct"] > 0:
                lines.append("\U0001F534 <i>Rupee weakening → FII outflow pressure</i>")
            else:
                lines.append("\U0001F7E2 <i>Rupee strengthening → positive for flows</i>")

        return "\n".join(lines)

    def _format_india_vix(self):
        """Format India VIX section with regime classification (HTML)."""
        data = self.report_sections.get("india_vix")
        if not data:
            return "\n\U0001F4A5 <b>India VIX</b>\n<i>Data unavailable</i>"

        lines = [f"\n\U0001F4A5 <b>India VIX</b>"]
        lines.append(
            f"  VIX: <code>{data['vix']:.2f}</code>  {_chg(data['change_pct'])}  "
            f"| Trend: <b>{data['trend']}</b>"
        )

        # Regime classification
        if data["vix"] > 20:
            lines.append("\U0001F534 <i>HIGH volatility (&gt;20) → expect wider intraday ranges</i>")
        elif data["vix"] > 15:
            lines.append("\U0001F7E1 <i>MODERATE volatility</i>")
        elif data["vix"] < 12:
            lines.append("\u26A0\uFE0F <i>LOW volatility (&lt;12) → complacency zone, breakout risk</i>")
        else:
            lines.append("\U0001F7E2 <i>NORMAL volatility</i>")

        if data["trend"] == "RISING":
            lines.append("\U0001F4C8 <i>VIX trending up → hedging activity increasing</i>")

        return "\n".join(lines)

    def _format_fii_dii(self):
        """Format FII/DII activity section with net flow signals (HTML)."""
        data = self.report_sections.get("fii_dii")
        if not data:
            return "\n\U0001F4B0 <b>FII/DII Activity</b>\n<i>Data unavailable</i>"

        header = "\U0001F4B0 <b>FII/DII Activity</b>"

        # Parse date
        date_str = data.get("date", "")
        if date_str:
            try:
                d = datetime.fromisoformat(date_str)
                header = f"\U0001F4B0 <b>FII/DII Activity</b> <i>[{d.strftime('%d %b')}]</i>"
            except Exception:
                pass

        lines = [f"\n{header}"]
        categories = data.get("categories", {})

        def _find_category(prefix):
            for short, cat in categories.items():
                if short.upper().startswith(prefix.upper()):
                    return cat
            return None

        def _flow_line(label, val):
            if val is None:
                return None
            dot = "\U0001F7E2" if val >= 0 else "\U0001F534"
            sign = "+" if val >= 0 else ""
            action = "Buy" if val >= 0 else "Sell"
            return f"  {dot} {label}: <code>{sign}{val:,.0f} Cr</code> (Net {action})"

        # FII Cash Market
        fii_cm = _find_category("FII CM")
        fii_val = fii_cm.get("value") if fii_cm else None
        fii_line = _flow_line("<b>FII Cash</b>", fii_val)
        if fii_line:
            lines.append(fii_line)

        # DII Cash Market
        dii_cm = _find_category("DII CM")
        dii_val = dii_cm.get("value") if dii_cm else None
        dii_line = _flow_line("<b>DII Cash</b>", dii_val)
        if dii_line:
            lines.append(dii_line)

        # FII Derivatives breakdown
        fii_idx_fut = _find_category("FII Idx Fut")
        fii_idx_opt = _find_category("FII Idx Opt")
        fii_stk_fut = _find_category("FII Stk Fut")

        def _fmt_val(val):
            if val is None:
                return "N/A"
            sign = "+" if val >= 0 else ""
            return f"{sign}{val:,.0f}"

        deriv_parts = []
        if fii_idx_fut and fii_idx_fut.get("value") is not None:
            deriv_parts.append(f"Idx Fut: {_fmt_val(fii_idx_fut['value'])}")
        if fii_idx_opt and fii_idx_opt.get("value") is not None:
            deriv_parts.append(f"Idx Opt: {_fmt_val(fii_idx_opt['value'])}")
        if fii_stk_fut and fii_stk_fut.get("value") is not None:
            deriv_parts.append(f"Stk Fut: {_fmt_val(fii_stk_fut['value'])}")

        if deriv_parts:
            lines.append(f"  FII Deriv: <code>{' | '.join(deriv_parts)}</code>")

        # Combined signal
        if fii_val is not None and dii_val is not None:
            if fii_val < -1000 and dii_val > 500:
                lines.append("\U0001F7E1 <i>FII selling absorbed by DII → support zone</i>")
            elif fii_val < -1000 and dii_val < -500:
                lines.append("\U0001F6A8 <i>Both FII &amp; DII selling → strong selling pressure</i>")
            elif fii_val > 1000 and dii_val > 0:
                lines.append("\U0001F7E2 <i>Both FII &amp; DII buying → strong demand</i>")
            elif fii_val > 1000:
                lines.append("\U0001F7E2 <i>FII buying → positive for market</i>")
            elif fii_val < -2000:
                lines.append("\U0001F6A8 <i>Heavy FII selling (&gt;2000 Cr) → bearish headwind</i>")

        return "\n".join(lines)

    def _format_preopen(self):
        """Format NSE pre-open session data (HTML)."""
        data = self.report_sections.get("preopen")
        if not data:
            return "\n\U0001F3E6 <b>NSE Pre-Open Session</b>\n<i>Data unavailable (available 9:00-9:08 AM)</i>"

        lines = [f"\n\U0001F3E6 <b>NSE Pre-Open Session</b>"]

        # Advance/decline
        lines.append(
            f"  \U0001F7E2 Advance: <b>{data['gainers_count']}</b>  |  "
            f"\U0001F534 Decline: <b>{data['losers_count']}</b>  "
            f"(of {data['total_stocks']})"
        )
        lines.append(f"  Buy/Sell Ratio: <code>{data['buy_sell_ratio']:.2f}</code>")

        # Pre-open sentiment
        ratio = data["buy_sell_ratio"]
        if ratio > 1.5:
            lines.append("\U0001F7E2 Sentiment: <b>STRONG buying interest</b>")
        elif ratio > 1.1:
            lines.append("\U0001F7E2 Sentiment: Mildly positive")
        elif ratio < 0.7:
            lines.append("\U0001F534 Sentiment: <b>STRONG selling pressure</b>")
        elif ratio < 0.9:
            lines.append("\U0001F534 Sentiment: Mildly negative")
        else:
            lines.append("\u26AA Sentiment: Balanced")

        # Top gainers
        if data.get("top_gainers"):
            lines.append(f"\n\U0001F4C8 <b>Pre-Open Gainers</b>")
            for g in data["top_gainers"]:
                lines.append(f"  \U0001F7E2 {g['symbol']}: <code>{g['price']:.2f}</code>  +{g['change_pct']:.2f}%")

        # Top losers
        if data.get("top_losers"):
            lines.append(f"\U0001F4C9 <b>Pre-Open Losers</b>")
            for l in data["top_losers"]:
                lines.append(f"  \U0001F534 {l['symbol']}: <code>{l['price']:.2f}</code>  {l['change_pct']:.2f}%")

        # Abnormal volume
        if data.get("high_volume_stocks"):
            lines.append(f"\U0001F4CA <b>Abnormal Pre-Open Volume</b>")
            for hv in data["high_volume_stocks"]:
                lines.append(f"  \U0001F525 {hv['symbol']}: <code>{hv['volume']:>10,}</code>  ({hv['multiple']:.1f}x avg)")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def _send_report(report):
    """Send an HTML-formatted report via Telegram, splitting if over 4096 char limit."""
    if not report:
        return
    if len(report) <= 4096:
        TELEGRAM_NOTIFICATIONS.send_notification(report, parse_mode="HTML")
    else:
        # Split on section emoji headers (lines starting with \n + emoji)
        import re
        chunks = re.split(r'\n(?=[\U0001F300-\U0001FAFF])', report)
        current_msg = chunks[0]
        for chunk in chunks[1:]:
            piece = "\n" + chunk
            if len(current_msg) + len(piece) > 4000:
                TELEGRAM_NOTIFICATIONS.send_notification(current_msg, parse_mode="HTML")
                current_msg = piece
            else:
                current_msg += piece
        if current_msg.strip():
            TELEGRAM_NOTIFICATIONS.send_notification(current_msg, parse_mode="HTML")


def run_global_cues_report():
    """
    Generate and send the global cues report (global indices, bond yields,
    commodities, FII/DII, India VIX).  Available 24/7 — send at startup.
    Returns the report string.
    """
    try:
        report_gen = PreMarketReport()
        report = report_gen.generate_global_report()
        logger.info(f"Global cues report generated:\n{report}")
        _send_report(report)
        return report
    except Exception as e:
        logger.error(f"Failed to generate global cues report: {e}")
        logger.error(traceback.format_exc())
        return None


def run_preopen_report():
    """
    Generate and send the NSE pre-open session report.
    Must be called between 9:00-9:08 AM for meaningful data.
    Returns the report string.
    """
    try:
        report_gen = PreMarketReport()
        report = report_gen.generate_preopen_report()
        logger.info(f"Pre-open session report generated:\n{report}")
        _send_report(report)
        return report
    except Exception as e:
        logger.error(f"Failed to generate pre-open session report: {e}")
        logger.error(traceback.format_exc())
        return None
