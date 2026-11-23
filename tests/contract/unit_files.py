"""Parse ``configs/stockanalysis*.service`` into structured units.

Used by the contract tests so the unit list lives in one place: the tests must
not hard-code which services exist or how they start. See
docs/testing-strategy.md T1.2/T1.3.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

# Services that legitimately import torch (or a torch-importing package).
ML_SERVICES = frozenset({"volatility_engine", "pinn_trainer"})


@dataclass(frozen=True)
class Unit:
    """One systemd unit, parsed from its .service file."""

    name: str            # unit name, e.g. "stockanalysis-market-data"
    path: Path
    exec_start: str
    interpreter: str
    module: str | None   # set for `python -m pkg.mod`
    script: Path | None  # set for `python path/to/main.py`
    args: tuple[str, ...]
    settings: dict[str, str]
    env: dict[str, str]

    @property
    def service(self) -> str:
        """Short service name, e.g. "market-data"."""
        return self.name.removeprefix("stockanalysis-") if self.name != "stockanalysis" else "orchestrator"

    @property
    def uses_module(self) -> bool:
        return self.module is not None

    @property
    def entry_file(self) -> Path:
        if self.script is not None:
            return self.script
        assert self.module is not None
        return REPO_ROOT / (self.module.replace(".", "/") + ".py")


def _parse(path: Path) -> Unit:
    settings: dict[str, str] = {}
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "Environment":
            name, sep2, val = value.partition("=")
            if sep2:
                env[name] = val
            continue
        if key not in settings:
            settings[key] = value

    if "ExecStart" not in settings:
        raise ValueError(f"{path.name}: no ExecStart")

    tokens = shlex.split(settings["ExecStart"])
    interpreter = tokens[0]
    if len(tokens) > 2 and tokens[1] == "-m":
        module, script, args = tokens[2], None, tuple(tokens[3:])
    elif len(tokens) > 1:
        module, script, args = None, REPO_ROOT / tokens[1], tuple(tokens[2:])
    else:
        raise ValueError(f"{path.name}: ExecStart has no entry target")

    return Unit(
        name=path.stem,
        path=path,
        exec_start=settings["ExecStart"],
        interpreter=interpreter,
        module=module,
        script=script,
        args=args,
        settings=settings,
        env=env,
    )


def load_units() -> list[Unit]:
    """Every deployed unit, sorted for stable test ids."""
    return [_parse(p) for p in sorted(CONFIG_DIR.glob("stockanalysis*.service"))]


def deployed_service_names() -> list[str]:
    """Names as they appear in scripts/deploy.py by convention."""
    return [unit.name for unit in load_units()]
