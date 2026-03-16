"""
LiveAlertFormatter
──────────────────
Consistent HTML builder for real-time (live-options) Telegram alerts.

Every alert gets:
  • A timestamped header  →  📈 [10:32:15] NIFTY — Title
  • Key-value rows        →    Label: value
  • A signal line         →  → Action text

Usage:
    from analyser.LiveAlertFormatter import F

    msg = F.build(
        F.header("NIFTY", "PCR Crossover → BULLISH", "📈"),
        F.kv("PCR", "0.97 → 1.03  (crossed above 1.0)"),
        F.kv_pair("CE OI", f"{ce_oi:,.0f}", "PE OI", f"{pe_oi:,.0f}"),
        F.signal("PE writers building. Bias shifts <b>UP</b>."),
    )
"""

from datetime import datetime


class LiveAlertFormatter:

    @staticmethod
    def header(symbol: str, title: str, emoji: str) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        return f"{emoji} <b>[{ts}] {symbol} — {title}</b>"

    @staticmethod
    def kv(label: str, value: str) -> str:
        """Single key-value row:  Label: <code>value</code>"""
        return f"  {label}: <code>{value}</code>"

    @staticmethod
    def kv_pair(label1: str, val1: str, label2: str, val2: str) -> str:
        """Two key-value pairs on one line:  L1: v1  |  L2: v2"""
        return f"  {label1}: <code>{val1}</code>  |  {label2}: <code>{val2}</code>"

    @staticmethod
    def kv_bold(label: str, value: str) -> str:
        """Key-value with bold value:  Label: <b>value</b>"""
        return f"  {label}: <b>{value}</b>"

    @staticmethod
    def signal(text: str) -> str:
        """Action / interpretation line:  → text"""
        return f"→ {text}"

    @staticmethod
    def note(text: str) -> str:
        """Supplementary context line (smaller emphasis):  ⁿ text"""
        return f"  <i>{text}</i>"

    @staticmethod
    def build(*lines) -> str:
        """Join non-empty lines with newlines."""
        return "\n".join(line for line in lines if line)


# Module-level shorthand so callers can write  F.header(...)  instead of
# LiveAlertFormatter.header(...)
F = LiveAlertFormatter
