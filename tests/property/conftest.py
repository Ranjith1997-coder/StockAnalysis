"""Hypothesis configuration for the property layer.

CI runs with ``derandomize`` so a failure on the runner reproduces on a laptop.
Locally hypothesis keeps exploring new examples (the failing case is printed).
"""
from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    derandomize=True,
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("local", max_examples=100, deadline=None)

settings.load_profile("ci" if os.getenv("CI") else "local")
