"""
MarketNarrator — converts a Confluence into an LLM-generated trade thesis.

Also provides positional EOD analysis: gathers all stock/index signals after
the positional run and sends them to the LLM for an overnight market briefing.

Runs asynchronously in a background thread so the raw alert fires instantly
and the narrative follows 1-3 seconds later without blocking the pipeline.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor

from intelligence.correlator import Confluence
from intelligence.context_builder import ContextBuilder, MarketContext
from intelligence.llm_client import LLMClient
from notification.Notification import TELEGRAM_NOTIFICATIONS
from common.logging_util import logger
import common.shared as shared


SYSTEM_PROMPT = """You are a senior Indian stock market options analyst.
You trade NIFTY and BANKNIFTY weekly options on NSE.
You receive confluence signals from a multi-layer automated analysis system that monitors:
- POSITIONAL layer: daily indicators (RSI, MACD, EMA crossovers, candlestick patterns)
- INTRADAY layer: 5-minute cycle indicators (same as above but on intraday data)
- LIVE layer: per-tick options data (PCR crossover, OI wall breach, IV skew flip, straddle decay)

When signals from multiple layers align in the same direction, it's called a confluence.
Your job is to interpret the confluence and provide a concise, actionable trade thesis.

Rules:
- Be specific: recommend exact strike prices, entry ranges, stop losses
- Weekly options only — use the nearest weekly expiry
- Consider time decay: if less than 60 minutes to close, avoid new positions
- If VIX is above 18, prefer selling strategies (short straddle/strangle)
- If VIX is below 13, prefer buying strategies (directional CE/PE)
- Always mention what would invalidate the trade
- Keep the response under 200 words
- Do not use markdown formatting — use plain text with line breaks"""


PROMPT_TEMPLATE = """## Confluence Detected
Symbol: {symbol}
Direction: {direction} ({level} conviction, score {score:.0f})
Has contradicting signals: {contradiction}

## Signals ({signal_count} total)
{signals_block}

## Market Snapshot
{context_block}

## Respond with this structure:

SIGNAL ANALYSIS:
[1 line per signal — what it means in plain language]

MARKET CONTEXT:
[2-3 lines on current conditions relevant to the trade]

TRADE IDEA:
Action: [BUY CE / BUY PE / SELL STRADDLE / AVOID]
Strike: [specific strike price]
Entry: [price range]
Target: [price range with spot level]
Stop loss: [price range with spot level]
Invalidation: [what negates this setup]

CONFIDENCE: [1-10] — [one line reasoning]"""


POSITIONAL_SYSTEM_PROMPT = """You are a senior Indian stock market options analyst providing an end-of-day briefing.
The PRIMARY user trades NIFTY and BANKNIFTY weekly options on NSE. Stock trades are secondary.

You analyse NSE stocks and indices using technical indicators (RSI, MACD, EMA, Bollinger Bands,
Supertrend, Stochastic, Pivot Points, candlestick patterns) and derivatives data (PCR, max pain,
OI buildup, OI walls, futures OI, FII/DII flows).

Rules:
- FIRST trade idea must always be a NIFTY or BANKNIFTY weekly options trade
- Be specific: exact strike, CE or PE, entry premium range, stop loss, target
- Use OI walls to determine strikes: call walls = resistance (sell CE above, buy PE below), put walls = support (buy CE above, sell PE below)
- VIX > 20: elevated fear — prefer selling premium (straddle/strangle) or buying ITM options for safety
- VIX < 13: low volatility — prefer buying OTM directional options
- FII selling + DII buying = distribution — be cautious on long directional CE
- Global markets all red + FII selling = avoid overnight long CE positions
- 52-week lows in blue-chip banks (HDFCBANK, KOTAKBANK) signals sector weakness — factor into BANKNIFTY view
- Complete every section fully before ending the response
- Do not use markdown formatting — use plain text with line breaks"""


POSITIONAL_PROMPT_TEMPLATE = """## End-of-Day Positional Analysis — {date}

## Index Performance
{index_report}

## Global Cues
{global_report}

## Commodities
{commodity_report}

## FII/DII Flows
{fii_dii_report}

## Sector Performance
{sector_report}

## 52-Week Highs/Lows
{week52_report}

## Stock Alerts (only stocks that triggered alerts)
{stock_alerts}

## Top Movers
{movers_summary}

## Respond with this exact structure (complete every section):

MARKET OVERVIEW:
[4-5 lines: NIFTY/BANKNIFTY direction, global cues impact, FII/DII interpretation, VIX reading, overall bias for tomorrow]

SECTOR THEMES:
[Which sectors are strong/weak and why — link to individual stock signals from the alerts]

INDEX OPTIONS TRADE (primary — NIFTY or BANKNIFTY weekly):
Symbol: NIFTY or BANKNIFTY
Direction: BULLISH / BEARISH / NEUTRAL
Action: BUY CE / BUY PE / SELL STRADDLE / SELL STRANGLE / AVOID
Strike: [specific strike based on OI walls and max pain]
Entry premium: [range]
Stop loss: [premium level]
Target: [premium level]
Expiry: [nearest weekly]
Reasoning: [why this strike, what confirms direction, what OI data supports it]
Invalidation: [what market condition negates this trade]

STOCK SETUPS (2 ideas from the alerts above):
1. [Symbol]: [Action] | Entry: [level] | SL: [level] | Target: [level] | Reason: [1 line]
2. [Symbol]: [Action] | Entry: [level] | SL: [level] | Target: [level] | Reason: [1 line]

52-WEEK LOW WATCHLIST:
[Are these capitulation or value trap? Use sector data and OI flows to judge each name briefly]

RISKS & CONTRADICTIONS:
[What could go wrong, mixed signals, key levels to watch, events that could change the bias]"""


class MarketNarrator:
    """
    Generates LLM-powered trade narratives from confluence events.

    Usage:
        narrator = MarketNarrator(gemini_client, context_builder)
        narrator.narrate_async(confluence)  # non-blocking
    """

    def __init__(self, llm: LLMClient, context_builder: ContextBuilder):
        self._llm = llm
        self._ctx = context_builder
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="narrator")

    def narrate_async(self, confluence: Confluence):
        """Submit narrative generation to background thread. Non-blocking."""
        self._executor.submit(self._narrate, confluence)

    def _narrate(self, confluence: Confluence):
        """Build prompt, call LLM, send result to Telegram."""
        try:
            ctx = self._ctx.build(confluence.symbol)
            prompt = self._build_prompt(confluence, ctx)
            response = self._llm.generate(SYSTEM_PROMPT, prompt)

            if not response:
                logger.debug(f"[Narrator] No response for {confluence.symbol} confluence")
                return

            msg = self._format_telegram(confluence, response)
            TELEGRAM_NOTIFICATIONS.send_live_options_notification(msg, parse_mode="HTML")
            logger.info(f"[Narrator] Sent narrative for {confluence.symbol} "
                        f"{confluence.direction.value} {confluence.level}")

        except Exception as e:
            logger.error(f"[Narrator] Failed to generate narrative: {e}")

    def _build_prompt(self, confluence: Confluence, ctx: MarketContext) -> str:
        signals_lines = []
        for s in sorted(confluence.signals, key=lambda s: s.timestamp):
            age = f"{s.age_seconds:.0f}s ago" if s.age_seconds < 120 else f"{s.age_seconds / 60:.0f}m ago"
            signals_lines.append(
                f"- [{s.layer.value.upper()}] {s.source} "
                f"({s.strength.name}, {age})"
            )

        return PROMPT_TEMPLATE.format(
            symbol=confluence.symbol,
            direction=confluence.direction.value,
            level=confluence.level,
            score=confluence.score,
            contradiction="YES" if confluence.has_contradiction else "NO",
            signal_count=len(confluence.signals),
            signals_block="\n".join(signals_lines),
            context_block=ctx.to_prompt_block(),
        )

    def _format_telegram(self, confluence: Confluence, narrative: str) -> str:
        """Wrap the LLM response in a Telegram-friendly HTML message."""
        icon = "\U0001F4A1"  # light bulb
        direction = confluence.direction.value
        level = confluence.level

        return (
            f"{icon} <b>{confluence.symbol} — {direction} Trade Thesis</b> "
            f"({level})\n\n"
            f"<pre>{narrative}</pre>"
        )

    # ── Positional EOD analysis ────────────────────────────────────────────

    def narrate_positional(self, report_data: dict[str, str]):
        """
        Send all positional analysis data to LLM for overnight market briefing.

        Args:
            report_data: dict with keys matching the prompt template sections.
                Keys: stock_alerts, index_report, commodity_report, global_report,
                      week52_report, sector_report, fii_dii_report, movers_summary
        """
        try:
            prompt = self._build_positional_prompt(report_data)
            response = self._llm.generate(POSITIONAL_SYSTEM_PROMPT, prompt)

            if not response:
                logger.warning("[Narrator] No response for positional analysis")
                return

            from datetime import datetime
            date_str = datetime.now().strftime("%d %b %Y")
            msg = (
                f"\U0001F4CA <b>EOD Market Analysis — {date_str}</b>\n\n"
                f"<pre>{response}</pre>"
            )
            TELEGRAM_NOTIFICATIONS.send_notification(msg, parse_mode="HTML")
            logger.info("[Narrator] Sent positional EOD narrative")

        except Exception as e:
            logger.error(f"[Narrator] Positional narrative failed: {e}")

    def _build_positional_prompt(self, data: dict[str, str]) -> str:
        from datetime import datetime
        import re

        # Strip HTML tags from Telegram messages for clean LLM input
        def strip_html(text: str) -> str:
            return re.sub(r"<[^>]+>", "", text) if text else "No data"

        return POSITIONAL_PROMPT_TEMPLATE.format(
            date=datetime.now().strftime("%d %b %Y"),
            index_report=strip_html(data.get("index_report", "")),
            global_report=strip_html(data.get("global_report", "")),
            commodity_report=strip_html(data.get("commodity_report", "")),
            fii_dii_report=strip_html(data.get("fii_dii_report", "")),
            sector_report=strip_html(data.get("sector_report", "")),
            week52_report=strip_html(data.get("week52_report", "")),
            stock_alerts=strip_html(data.get("stock_alerts", "")),
            movers_summary=strip_html(data.get("movers_summary", "")),
        )

    def shutdown(self):
        """Graceful shutdown of the background executor."""
        self._executor.shutdown(wait=False)
