"""Sensibull session helpers shared by the fetcher and the auth service."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone


def client_info_expiry(raw: str | None) -> datetime | None:
    """Expiry of the Sensibull client_info JWT (None when absent/invalid).

    The access_token is useless without a live client_info pair; when client_info
    expires the API returns 401 even though the token looks well formed.
    """
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp")
    if not exp:
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        return None
