"""
MarketNarrator — converts a Confluence into an LLM-generated trade thesis.

Also provides positional EOD analysis: gathers all stock/index signals after
the positional run and sends them to the LLM for an overnight market briefing.

Runs asynchronously in a background thread so the raw alert fires instantly
and the narrative follows 1-3 seconds later without blocking the pipeline.
"""

from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor

from intelligence.correlator import Confluence
from intelligence.context_builder import ContextBuilder, MarketContext
from intelligence.llm_client import LLMClient
from notification.Notification import TELEGRAM_NOTIFICATIONS
from common.logging_util import logger
import common.shared as shared


# Tradable index symbols that route to the options desk
_INDEX_SYMBOLS = {"NIFTY 50", "NIFTY BANK", "FINNIFTY", "NIFTY", "BANKNIFTY"}

# ── Options Desk (Index confluences) ──────────────────────────────────────────
_INDEX_SYSTEM_PROMPT = (
    "You are an institutional derivatives trader for the Indian Stock Market. "
    "Your focus is strictly on NIFTY and BANKNIFTY weekly options."
)

_INDEX_PROMPT_TEMPLATE = """A HIGH confluence signal has fired for {symbol}.

## Confluence
Direction: {direction} ({level} conviction, score {score:.0f})
Signals ({signal_count} total):
{signals_block}
Contradicting signals: {contradiction}

## Market Snapshot
{context_block}

TASK: Based on the OI walls, PCR, and Straddle premium, recommend a specific weekly options strategy.
RULES:
- Respect the VIX regime (if VIX is low, prefer buying; if high, prefer credit spreads)
- Anchor your Strike selection to the nearest OI Wall
- Provide a clear Strike, Entry Zone, Stop Loss, and Target
- Keep the response under 150 words
- Do not use markdown formatting — plain text with line breaks"""

# ── Delta-One Desk (Equity confluences) ───────────────────────────────────────
_EQUITY_SYSTEM_PROMPT = (
    "You are an institutional equity and futures swing trader for the Indian Stock Market. "
    "You specialize in price action, volume breakouts, and pure delta-one trading. "
    "You do NOT trade stock options due to liquidity constraints."
)

_EQUITY_PROMPT_TEMPLATE = """A HIGH confluence signal has fired for {symbol}.

## Confluence
Direction: {direction} ({level} conviction, score {score:.0f})
Signals ({signal_count} total):
{signals_block}
Contradicting signals: {contradiction}

## Market Snapshot
{context_block}

TASK: Based on the technical momentum and available context, recommend a pure Equity/Futures trade setup.
RULES:
- Strictly DO NOT recommend stock options — recommend Cash (delivery/intraday) or Futures execution
- Provide a clear Entry Zone, Stop Loss, and Target based on support/resistance logic
- Detail the immediate invalidation condition for this trade
- Keep the response under 150 words
- Do not use markdown formatting — plain text with line breaks"""


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

    # Minimum seconds between LLM narratives for the same symbol (any direction).
    # Prevents re-flooding when the same stock keeps firing confluences.
    NARRATE_SYMBOL_COOLDOWN = 1800  # 30 min

    def __init__(self, llm: LLMClient, context_builder: ContextBuilder):
        self._llm = llm
        self._ctx = context_builder
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="narrator")
        self._last_narrated: dict[str, float] = {}  # symbol -> last narration epoch

    def narrate_async(self, confluence: Confluence):
        """Submit narrative generation to background thread. Non-blocking.

        Guards:
        1. Per-symbol cooldown (NARRATE_SYMBOL_COOLDOWN).
        2. Time-decay gate: skips if < 60 min to market close.
        3. Asset class router: index → options desk prompt, equity → delta-one prompt.
        """
        now = time.time()
        last = self._last_narrated.get(confluence.symbol, 0.0)
        if now - last < self.NARRATE_SYMBOL_COOLDOWN:
            remaining = int(self.NARRATE_SYMBOL_COOLDOWN - (now - last))
            logger.debug(
                f"[Narrator] Skipping {confluence.symbol} — cooldown active "
                f"({remaining}s remaining)"
            )
            return

        # Asset class router — determines context depth and prompt
        is_index = confluence.symbol.upper() in _INDEX_SYMBOLS
        if is_index:
            ctx = self._ctx.build_index(confluence.symbol)
            system_prompt = _INDEX_SYSTEM_PROMPT
            template = _INDEX_PROMPT_TEMPLATE
            desk = "options"
        else:
            ctx = self._ctx.build(confluence.symbol)
            system_prompt = _EQUITY_SYSTEM_PROMPT
            template = _EQUITY_PROMPT_TEMPLATE
            desk = "equity"

        if ctx.minutes_to_close is not None and ctx.minutes_to_close < 60:
            logger.info(
                f"[Narrator] Skipping {confluence.symbol} — "
                f"{ctx.minutes_to_close}m to close (< 60m gate)"
            )
            return

        logger.debug(f"[Narrator] Routing {confluence.symbol} → {desk} desk")

        self._last_narrated[confluence.symbol] = now
        self._executor.submit(self._narrate, confluence, ctx, system_prompt, template)

    def _narrate(self, confluence: Confluence, ctx: MarketContext,
                 system_prompt: str, template: str):
        """Call LLM with routed prompts and send result to Telegram."""
        try:
            prompt = self._build_prompt(confluence, ctx, template)
            response = self._llm.generate(system_prompt, prompt)

            if not response:
                logger.debug(f"[Narrator] No response for {confluence.symbol} confluence")
                return

            msg = self._format_telegram(confluence, response)
            TELEGRAM_NOTIFICATIONS.send_live_options_notification(msg, parse_mode="HTML")
            logger.info(f"[Narrator] Sent narrative for {confluence.symbol} "
                        f"{confluence.direction.value} {confluence.level}")

        except Exception as e:
            logger.error(f"[Narrator] Failed to generate narrative: {e}")

    def _build_prompt(self, confluence: Confluence, ctx: MarketContext,
                      template: str) -> str:
        signals_lines = []
        for s in sorted(confluence.signals, key=lambda s: s.timestamp):
            age = f"{s.age_seconds:.0f}s ago" if s.age_seconds < 120 else f"{s.age_seconds / 60:.0f}m ago"
            signals_lines.append(
                f"- [{s.layer.value.upper()}] {s.source} "
                f"({s.strength.name}, {age})"
            )

        return template.format(
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
