"""Contract-test helpers (T1.1): the declared Redis schema.

This module is the single place that says "these keys exist and may only carry
these fields". The producer test runs the real publisher against fakeredis and
fails when it writes an undeclared field; the doc test fails when
`.kilo/skills/options-data-contract/SKILL.md` documents a field that is not
declared here.

Rule (from the skill): adding or renaming a field is a three-part change —
producer, consumer, and this file, in one PR.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Only these data keys are covered; streams, auth and stats keys have their
# own tests. A literal outside this set is caught by test_static_rules.
DATA_KEY_PREFIXES = (
    "data:tick:",
    "data:options_live:",
    "data:options_agg:",
    "data:futures_live:",
)


@dataclass(frozen=True)
class HashSchema:
    """Declared field set for one Redis hash key pattern."""

    key: str
    required: frozenset[str]
    optional: frozenset[str] = field(default_factory=frozenset)
    value_json: bool = False
    field_regex: str = ""
    json_required: frozenset[str] = field(default_factory=frozenset)
    json_optional: frozenset[str] = field(default_factory=frozenset)

    @property
    def all_fields(self) -> frozenset[str]:
        return self.required | self.optional

    @property
    def all_json_fields(self) -> frozenset[str]:
        return self.json_required | self.json_optional

    def field_problem(self, name: str) -> str | None:
        """Return a human-readable problem when ``name`` is not declared."""
        if self.field_regex and re.fullmatch(self.field_regex, name):
            return None
        if name in self.all_fields:
            return None
        return f"{self.key}: undeclared field {name!r}"

    def json_problem(self, payload: dict) -> list[str]:
        problems = [
            f"{self.key}: undeclared JSON field {k!r}"
            for k in payload
            if k not in self.all_json_fields
        ]
        problems += [
            f"{self.key}: missing required JSON field {k!r}"
            for k in self.json_required
            if k not in payload
        ]
        return problems

    def parse_json(self, raw: str) -> dict:
        value = json.loads(raw)
        assert isinstance(value, dict), f"{self.key}: live value is not a JSON object"
        return value


# ── data:options_agg:{SYM} ────────────────────────────────────────────────
# Written by SnapshotPublisher from TickStore.options_aggregate, plus the two
# tick counters. gex_* are a mirror of data:gex_state (do not read them as
# live GEX; the analyser owns the authoritative copy).
OPTIONS_AGG = HashSchema(
    key="data:options_agg:{SYM}",
    required=frozenset({
        "total_ce_oi", "total_pe_oi", "live_pcr", "pcr_window_pct",
        "pcr_strikes", "atm_strike", "atm_straddle_premium", "atm_iv_ce",
        "atm_iv_pe", "iv_skew", "max_oi_ce_strike", "max_oi_pe_strike",
        "max_pain_strike", "future_price", "expiry", "net_ce_oi_change",
        "net_pe_oi_change", "net_ce_oi_change_tick", "net_pe_oi_change_tick",
        "atm_iv", "atm_iv_percentile", "atm_ivp_type", "last_updated",
        "option_tick_count", "tick_count",
    }),
    optional=frozenset({
        "gex_total", "gex_ce", "gex_pe", "gex_regime", "gex_flip_level",
    }),
)

# ── data:options_live:{SYM} ───────────────────────────────────────────────
# Field "{strike}_{CE|PE}" -> JSON tick. buy_qty/sell_qty are QUANTITIES;
# best prices are bid/ask. has_oi=False means OI is last-known (quote mode).
OPTIONS_LIVE = HashSchema(
    key="data:options_live:{SYM}",
    required=frozenset(),
    value_json=True,
    field_regex=r"\d+(\.\d+)?_(CE|PE)",
    json_required=frozenset({
        "ltp", "oi", "prev_oi", "has_oi", "volume", "buy_qty", "sell_qty",
        "timestamp",
    }),
    json_optional=frozenset({
        "open_oi", "bid", "ask", "open", "high", "low", "close", "depth",
        "delta", "gamma", "theta", "vega", "iv", "iv_change",
    }),
)

# ── data:futures_live:{SYM} ───────────────────────────────────────────────
_FUT_FIELDS = ("ltp", "oi", "volume", "change", "buy_qty", "sell_qty")
_FUT_DEPTH_FIELDS = tuple(
    f"{side}_{kind}_{level}"
    for side in ("buy", "sell")
    for kind in ("px", "qty", "ord")
    for level in range(1, 6)
)
FUTURES_LIVE = HashSchema(
    key="data:futures_live:{SYM}",
    required=frozenset(),
    field_regex=(
        r"(current|next)_("
        + "|".join(_FUT_FIELDS + _FUT_DEPTH_FIELDS)
        + ")"
    ),
)

# ── data:futures_live depth (documented shape) ────────────────────────────
FUTURES_FIELDS = frozenset(_FUT_FIELDS)
FUTURES_DEPTH_FIELDS = frozenset(_FUT_DEPTH_FIELDS)

# ── data:tick:{SYM} ───────────────────────────────────────────────────────
TICK = HashSchema(
    key="data:tick:{SYM}",
    required=frozenset({
        "last_price", "open", "high", "low", "close", "volume_traded",
        "total_buy_quantity", "total_sell_quantity", "average_traded_price",
        "change", "timestamp", "tick_count",
    }),
)

# ── service:registry:{SVC} ────────────────────────────────────────────────
REGISTRY_REQUIRED = frozenset({"name", "pid", "status", "last_heartbeat", "version"})
REGISTRY_OPTIONAL = frozenset({
    "commit", "dirty", "worker", "ws_order_statuses", "ws1_connected",
    "ws2_connected", "ws1_subs", "ws2_subs", "ws2_reconnects",
    "sensibull_feeds", "last_equity_tick", "tick_count", "models",
    "total_confluences", "cycle_count", "last_cycle", "last_cycle_time",
    "uptime", "memory_mb", "cpu_pct", "queues", "mode", "consumer",
})

SCHEMAS: dict[str, HashSchema] = {
    "data:options_agg:": OPTIONS_AGG,
    "data:options_live:": OPTIONS_LIVE,
    "data:futures_live:": FUTURES_LIVE,
    "data:tick:": TICK,
}


def schema_for(key: str) -> HashSchema | None:
    """Map a concrete key (``data:options_agg:NIFTY``) to its declared schema."""
    for prefix, schema in SCHEMAS.items():
        if key.startswith(prefix):
            return schema
    return None


def registry_problem(mapping: dict) -> list[str]:
    """Check a service:registry hash against the liveness contract."""
    problems = [
        f"registry: undeclared field {k!r}"
        for k in mapping
        if k not in REGISTRY_REQUIRED and k not in REGISTRY_OPTIONAL
    ]
    problems += [
        f"registry: missing required field {k!r}"
        for k in REGISTRY_REQUIRED
        if not mapping.get(k)
    ]
    return problems
