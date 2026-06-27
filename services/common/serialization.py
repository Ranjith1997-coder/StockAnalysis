"""
Serialization helpers for transmitting pandas DataFrames and nested dicts over Redis.

DataFrames use pandas' built-in JSON serialization (orient='split') which is compact
and preserves dtypes. Nested dicts use standard json.dumps with default=str for
datetime/numpy types.
"""

from __future__ import annotations

import json
import pandas as pd
from typing import Any


def dataframe_to_json(df: pd.DataFrame) -> str:
    """Serialize a DataFrame to a JSON string for Redis storage."""
    if df is None or df.empty:
        return "{}"
    return df.to_json(orient="split", date_format="iso")


def dataframe_from_json(json_str: str) -> pd.DataFrame:
    """Deserialize a JSON string back to a DataFrame."""
    if not json_str or json_str == "{}":
        return pd.DataFrame()
    return pd.read_json(json_str, orient="split")


def safe_json_dumps(obj: Any) -> str:
    """Serialize any Python object to JSON, handling non-serializable types."""
    return json.dumps(obj, default=str, separators=(",", ":"))


def safe_json_loads(json_str: str) -> Any:
    """Deserialize a JSON string, returning None on failure."""
    if not json_str or json_str == "{}":
        return None
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None
