"""Bulk coverage for every registered MessageFormatter (audit 2026-11-23).

The module has 64 formatters; individual field-by-field tests left ~40 of them
partially covered. These tests build one synthetic payload that satisfies any
attribute a formatter can read, then render every key in both directions. They
prove each formatter is registered, reachable and cannot raise on complete data;
the exact-text assertions live in the per-signal tests next to this file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.analysis_engine.analyser.MessageFormatter import MessageFormatter

MODULE = (
    Path(__file__).resolve().parents[2]
    / "services/analysis_engine/analyser/MessageFormatter.py"
)

LISTS = {
    "top_strikes": [(20_000.0, 100_000, 50.0), (20_100.0, 80_000, 40.0)],
    "call_walls": [(20_200.0, 150_000, 1.0)],
    "put_walls": [(19_800.0, 150_000, -1.0)],
    "conditions_detail": ["G1 Wall Breach", "G2 Volume"],
    "conditions": ["R1", "R2", "R3", "R4", "R5"],
    "conditions_met": ["G1", "G2", "G3"],
    "triggers": {"G1 Wall Breach": "CALL unwound 60%", "G2 Volume": "3.3x avg"},
    "levels": [("R1", 20_200.0), ("S1", 19_800.0)],
    "strikes": [20_000.0, 20_100.0],
}
STRING_HINTS = (
    "pattern", "action", "signal", "zone", "category", "direction", "regime",
    "trend", "bias", "strength", "divergence", "side", "type", "expiry",
    "mode", "level_name", "alignment", "volatility", "breakout", "label",
    "magnitude", "source", "date", "symbol",
)
BOOL_HINTS = (
    "confirm", "aligned", "weakening", "narrowing", "confirmed", "active",
)


class _AnyData:
    """Payload with a plausible value for whatever attribute a formatter reads."""

    def __getattr__(self, name: str):
        low = name.lower()
        if low in LISTS:
            return LISTS[low]
        if low == "conditions_count":
            return 3
        if any(hint in low for hint in BOOL_HINTS):
            return True
        if any(hint in low for hint in STRING_HINTS):
            return "BULLISH" if "bearish" not in low else "BEARISH"
        return 100.0

    def __bool__(self) -> bool:
        return True

    def __iter__(self):
        return iter([self])

    def __len__(self) -> int:
        return 1


def registered_keys() -> list[str]:
    """Keys registered in the module source (the registry itself is import-time)."""
    tree = ast.parse(MODULE.read_text())
    keys = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "register"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                keys.append(dec.args[0].value)
    return sorted(set(keys))


KEYS = registered_keys()


def test_every_source_formatter_is_registered() -> None:
    assert len(KEYS) >= 60, f"only parsed {len(KEYS)} formatters — parser broken?"
    registered = set(MessageFormatter.registered_types())
    assert set(KEYS) <= registered


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("trend", ["BULLISH", "BEARISH"])
def test_formatter_renders_for_every_signal(key, trend) -> None:
    lines = MessageFormatter.format(key, _AnyData(), trend)
    assert isinstance(lines, list) and lines, f"{key} rendered nothing"
    assert all(isinstance(line, str) for line in lines), f"{key} returned non-str"
    assert any(line.strip() for line in lines), f"{key} returned blank output"


# ── Exact-text tests for the formatters that were <=56 % covered ─────────────

from collections import namedtuple  # noqa: E402

CostOfCarry = namedtuple(
    "FuturesCostOfCarry",
    ["action", "basis_pct", "basis_5d_mean", "basis_trend", "ann_coc", "days_to_expiry"],
)
PcrBias = namedtuple("PCR_BIAS", ["bias", "strength", "total_pcr", "trend_direction"])
Rsi = namedtuple("RSIAnalysis", ["value", "price_trend", "zone_candles"])
FutureAction = namedtuple(
    "FutureActionAnalysis",
    ["expiry", "action", "price_percentage", "oi_percentage", "score", "confidence"],
)
Accel = namedtuple(
    "OIOAcceleration",
    ["side", "accel_ratio", "recent_velocity", "prev_velocity", "signal"],
)
Capitulation = namedtuple(
    "OICapitulation",
    ["side", "total_unwound", "unwound_pct", "top_strikes",
     "num_significant_strikes", "signal", "expiry"],
)
MaxPain = namedtuple(
    "MaxPainAnalysis",
    ["current_price", "max_pain_strike", "deviation_pct",
     "signal_strength", "pcr", "expiry", "max_pain_type"],
)
GexImbalance = namedtuple(
    "GEXImbalance",
    ["dominant_side", "magnitude", "gex_ce", "gex_pe", "imbalance_ratio"],
)


def _render(key, data, trend="BULLISH"):
    return MessageFormatter.format(key, data, trend)


class TestExactFormatterText:
    def test_cost_of_carry_icons_and_optional_fields(self):
        line = _render("FUTURE_COST_OF_CARRY", CostOfCarry(
            "BACKWARDATION", -0.25, -0.1, -0.15, None, None))[0]
        assert "⚠️" in line and "BACKWARDATION" in line
        assert "basis=-0.25%" in line
        assert "ann_coc" not in line and "expiry=" not in line

        line = _render("FUTURE_COST_OF_CARRY", CostOfCarry(
            "HIGH_COST_OF_CARRY", 2.0, 1.9, 0.1, 24.3, 30))[0]
        assert "🔥" in line
        assert "ann_coc=24.3%" in line and "expiry=30d" in line

        line = _render("FUTURE_COST_OF_CARRY", CostOfCarry(
            "BASIS_EXPANDING", 0.2, 0.1, 0.1, None, 8))[0]
        assert "📊" in line and "expiry=8d" in line

    def test_pcr_bias_suffix_only_when_strength_present(self):
        line = _render("PCR_BIAS", PcrBias("BULLISH", "STRONG", 0.72, "RISING"))[0]
        assert "0.720" in line and "[STRONG/RISING]" in line

        line = _render("PCR_BIAS", PcrBias("NEUTRAL", "", 1.0, ""))[0]
        assert "[" not in line

    def test_rsi_with_and_without_zone_candles(self):
        lines = _render("RSI", Rsi(28.5, "FALLING", 3), "BULLISH")
        assert "28.50" in lines[0] and "Oversold 3c" in lines[0]
        assert "🟢" in lines[0]
        assert "Price trend: <code>FALLING</code>" in lines[1]

        lines = _render("RSI", Rsi(88.2, "RISING", 4), "BEARISH")
        assert "🔴" in lines[0] and "Overbought 4c" in lines[0]

    def test_future_action_single_and_list(self):
        single = _render("FUTURE_ACTION", FutureAction(
            "current", "long_buildup", 1.25, 0.75, 55, "HIGH"))
        assert len(single) == 1
        assert "🟢" in single[0] and "long_buildup" in single[0]
        assert "p%:<code>1.25</code>" in single[0]
        assert "55" in single[0] and "HIGH" in single[0]

        many = _render("FUTURE_ACTION", [
            FutureAction("current", "short_buildup", -1.0, 2.0, 40, "MEDIUM"),
            FutureAction("current", "short_covering", 0.5, -1.0, 30, "LOW"),
        ])
        assert len(many) == 2
        assert "🔴" in many[0] and "🟢" in many[1]

    def test_oi_acceleration_side_icons_and_numbers(self):
        lines = _render("OI_ACCELERATION", Accel(
            "CALL", 2.5, 3_000_000, 1_200_000, "call writers accelerating"))
        assert "🔴" in lines[0] and "CALL" in lines[0]
        assert "2.5x" in lines[0]
        assert "3,000,000/day" in lines[0]
        assert "<i>call writers accelerating</i>" in lines[1]

        lines = _render("OI_ACCELERATION", Accel("PUT", 1.5, 2_000_000, 1_300_000, "x"))
        assert "🟢" in lines[0]

    def test_oi_capitulation_side_icons_and_top_strikes(self):
        data = Capitulation(
            "CALL", 600_000, 42.5,
            [(20_000, 200_000, 67), (20_100, 150_000, 50), (20_200, 100_000, 40), (20_300, 50_000, 20)],
            3, "call covering", "2024-01-25")
        lines = _render("OI_CAPITULATION", data)
        assert "🟢" in lines[0] and "600,000" in lines[0] and "42.5%" in lines[0]
        assert "20000(-200,000/67%)" in lines[1]
        assert "20300" not in lines[1], "Top must be limited to 3 strikes"
        assert "exp=2024-01-25" in lines[1]

        lines = _render("OI_CAPITULATION", Capitulation(
            "PUT", 1, 1.0, [], 0, "x", "2024-01-25"))
        assert "🔴" in lines[0]

    def test_max_pain_second_line_only_with_pcr(self):
        lines = _render("MAX_PAIN", MaxPain(
            20_050.0, 20_000.0, 0.25, "MODERATE", 1.15, "2024-01-25", "Bullish"))
        assert "Price=<code>20050.00</code>" in lines[0]
        assert "MP=<code>20000.00</code>" in lines[0]
        assert "Dev=<code>+0.25%</code>" in lines[0]
        assert "Exp=2024-01-25" in lines[1] and "Type=Bullish" in lines[1]

        lines = _render("MAX_PAIN", MaxPain(
            20_050.0, 20_000.0, 0.25, "WEAK", 0.0, "2024-01-25", "Neutral"))
        assert len(lines) == 1

    def test_gex_imbalance_emoji_and_headwind(self):
        lines = _render("GEX_IMBALANCE", GexImbalance("CE", "STRONG", 900.0, 100.0, 9.0))
        assert "🔴" in lines[0] and "🔥" in lines[0]
        assert "CE GEX: <code>900 Cr</code>" in lines[1]
        assert "9.0x" in lines[1]
        assert "SELL every rally" in lines[2]

        lines = _render("GEX_IMBALANCE", GexImbalance("PE", "EXTREME", 100.0, 800.0, 8.0))
        assert "🟢" in lines[0] and "💥" in lines[0]
        assert "BUY every dip" in lines[2]
