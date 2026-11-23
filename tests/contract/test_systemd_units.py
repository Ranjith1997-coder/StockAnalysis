"""Contract tests: systemd unit files (T1.3).

Each deployed unit is executable, consistent with how the code imports, and
covered by `make deploy`. This layer exists because a wrong ExecStart, a
missing ``-m`` (relative imports break) or a unit left out of
``RESTART_SERVICES`` fails only in production.
"""
from __future__ import annotations

import ast

import pytest

from scripts.deploy import RESTART_SERVICES
from tests.contract.unit_files import (
    CONFIG_DIR,
    ML_SERVICES,
    load_units,
)

pytestmark = pytest.mark.contract

UNITS = load_units()


def _unit_id(unit) -> str:
    return unit.name


def _memory_max_mb(unit) -> int:
    raw = unit.settings.get("MemoryMax", "")
    if not raw:
        return 0
    suffix = raw[-1].upper()
    number = raw[:-1] if suffix in {"K", "M", "G"} else raw
    try:
        value = int(number)
    except ValueError:
        return 0
    return {"K": value / 1024, "M": value, "G": value * 1024}.get(suffix, value)


def test_units_match_deploy_restart_list() -> None:
    """Every shipped unit is restarted by deploy, and nothing else is."""
    assert {u.name for u in UNITS} == set(RESTART_SERVICES)


def test_interpreter_is_the_venv_python() -> None:
    """A bare ``python`` picks up the system interpreter and loses dependencies."""
    for unit in UNITS:
        assert unit.interpreter.endswith((".venv/bin/python", ".venv/bin/python3")), (
            f"{unit.name}: ExecStart interpreter must be the project venv "
            f"(got {unit.interpreter})"
        )


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_working_directory_and_pythonpath_are_set(unit) -> None:
    assert unit.settings.get("WorkingDirectory"), f"{unit.name}: WorkingDirectory missing"
    assert unit.env.get("PYTHONPATH"), f"{unit.name}: PYTHONPATH missing"
    assert unit.env.get("REDIS_URL"), f"{unit.name}: REDIS_URL not set explicitly"


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_entry_target_resolves(unit) -> None:
    assert unit.entry_file.is_file(), f"{unit.name}: entry file {unit.entry_file} missing"
    if unit.uses_module:
        assert unit.module.startswith("services."), (
            f"{unit.name}: service modules live under services.*"
        )


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_file_path_entries_have_no_relative_imports(unit) -> None:
    """``python path/to/main.py`` cannot resolve ``from .x import y``.

    A file-path unit must therefore contain no relative imports; use
    ``python -m pkg.mod`` for any module that does.
    """
    if unit.uses_module:
        pytest.skip("module entry (-m) supports relative imports")
    tree = ast.parse(unit.entry_file.read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0
    ]
    assert not offenders, (
        f"{unit.name}: {unit.entry_file.name} has relative imports at lines "
        f"{offenders}; switch ExecStart to `python -m <package>.main`"
    )


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_restart_policy_is_declared(unit) -> None:
    assert unit.settings.get("Restart") == "always", f"{unit.name}: Restart=always required"
    assert unit.settings.get("RestartSec", "").isdigit(), f"{unit.name}: RestartSec missing"


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_secrets_are_loaded(unit) -> None:
    """A unit must get .env either from EnvironmentFile or via load_dotenv()."""
    if unit.settings.get("EnvironmentFile"):
        return
    source = unit.entry_file.read_text()
    assert "load_dotenv" in source or "lib.logging_util" in source or "lib.notification" in source, (
        f"{unit.name}: no EnvironmentFile and the entry point never loads .env "
        f"({unit.entry_file.relative_to(CONFIG_DIR.parent)}); add EnvironmentFile= "
        "to the unit or call load_dotenv() at startup"
    )


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_memory_limit_is_declared(unit) -> None:
    assert _memory_max_mb(unit) > 0, f"{unit.name}: MemoryMax missing or unparseable"


@pytest.mark.parametrize("unit", UNITS, ids=_unit_id)
def test_ml_units_have_torch_headroom(unit) -> None:
    """Torch+CUDA-less inference needs >1 GB RSS; a tight cap means OOM-kill loops."""
    if unit.service not in ML_SERVICES:
        pytest.skip("not an ML service")
    assert _memory_max_mb(unit) >= 1024, (
        f"{unit.name}: MemoryMax={unit.settings.get('MemoryMax')} is too low for torch"
    )
