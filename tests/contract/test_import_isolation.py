"""Contract tests: service imports must not require torch (T1.2).

paper-trading once crashed on the server because a module-level transitive
import pulled in torch, which is only installed on ML hosts. Unit tests never
saw it because the dev venv has torch installed. These tests run the real entry
modules in a subprocess with ``torch`` blocked.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.contract.unit_files import ML_SERVICES, REPO_ROOT, load_units

pytestmark = pytest.mark.contract

UNITS = load_units()

# Blocks `import torch` for the whole subprocess. Installed as sitecustomize.py
# via PYTHONPATH so it applies before the test target is imported.
TORCH_BLOCKER = """
import importlib.abc
import sys


class _BlockTorch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "torch" or fullname.startswith("torch."):
            raise ImportError("torch is blocked by test_import_isolation")
        return None


sys.meta_path.insert(0, _BlockTorch())
"""


def _service_of(unit) -> str:
    """Package name (underscores), matching unit_files.ML_SERVICES."""
    if unit.uses_module:
        return unit.module.split(".")[1]
    return unit.service.replace("-", "_")


def _entry_import_code(unit) -> str:
    if unit.uses_module:
        return f"import importlib; importlib.import_module({unit.module!r})"
    return (
        "import runpy; "
        f"runpy.run_path({str(unit.script)!r}, run_name='import_check')"
    )


def _run(unit, tmp_path, block_torch: bool) -> subprocess.CompletedProcess:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    if block_torch:
        (site / "sitecustomize.py").write_text(TORCH_BLOCKER)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(site)])
    env["REDIS_URL"] = "redis://127.0.0.1:1"  # nothing listens; import only
    return subprocess.run(
        [sys.executable, "-c", _entry_import_code(unit)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_torch_blocker_actually_blocks(tmp_path) -> None:
    """Guard the guard: if the blocker stops working, every test below is vacuous."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(TORCH_BLOCKER)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    result = subprocess.run(
        [sys.executable, "-c", "import torch"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "blocked by test_import_isolation" in result.stderr


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.name)
def test_non_ml_service_imports_without_torch(unit, tmp_path) -> None:
    service = _service_of(unit)
    if service in ML_SERVICES:
        pytest.skip(f"{service} legitimately imports torch (ML_SERVICES)")

    result = _run(unit, tmp_path, block_torch=True)
    assert result.returncode == 0, (
        f"{unit.name}: importing {unit.module or unit.script} fails without torch.\n"
        f"Move the torch-dependent import inside the function that needs it.\n"
        f"--- stderr ---\n{result.stderr[-4000:]}"
    )


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.name)
def test_entry_file_has_no_module_level_torch(unit) -> None:
    if _service_of(unit) in ML_SERVICES:
        pytest.skip("ML service")
    source = unit.entry_file.read_text()
    for line_no, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("import torch", "from torch")):
            pytest.fail(
                f"{unit.entry_file.name}:{line_no}: module-level torch import in a "
                "non-ML service"
            )
