---
name: stockanalysis-deploy
description: "Deploy StockAnalysis to the production server (physical machine via Tailscale, NOT EC2). USE FOR: deploy, redeploy, ship to server, restart services, install systemd unit, deploy failure, service not starting after deploy, server unreachable. DO NOT USE FOR: local dev runs, EC2/boto3 operations."---

# Deploying StockAnalysis

## Target

Production is a **physical machine**, not EC2:
`hacker@100.92.21.31` (Tailscale), repo at `~/StockAnalysis`, units in
`/etc/systemd/system/stockanalysis*.service`. See `configs/server_metadata.yaml`.

`scripts/deploy.py` (via `make deploy`) does: verify local HEAD == origin/master →
`git pull --ff-only` on the server → sync unit files for **already-installed**
units → restart the 8 services below → verify `is-active`.

## Preflight (never skip)

1. `git status` clean except known untracked `data/` artifacts.
2. Full suite green: `make test-fast` (currently ~1894 tests).
3. Branch is `master` and local == `origin/master`. Deploy pulls origin, so
   unpushed commits are silently not deployed.

## Restart set and order (`scripts/deploy.py`)

notification → data-gateway → market-data → analysis-engine →
signal-intelligence → resource-monitor → paper-trading → stockanalysis (last).

Do **not** restart `stockanalysis-auth` (timer-triggered oneshot) or the
positional timer unit. `stockanalysis-pinn-trainer` and
`stockanalysis-volatility-engine` are not in the deploy list; restart them
explicitly if their code changed.

## Launch mode rule (this has broken production twice)

A service whose `main.py` uses relative imports (`from .x import y`) **must**
launch as `python -m pkg.module`. Check with:

```bash
grep -c '^from \.' services/<svc>/main.py
```

Non-zero → the unit needs `-m`. File-path launch dies with
"attempted relative import with no known parent package". `-m` requires the
unit's `WorkingDirectory` + `PYTHONPATH` to be set (they are).

## New units are NOT auto-installed

`deploy.py` only syncs units that already exist in `/etc/systemd/system`. For a
new service: `sudo cp configs/<unit> /etc/systemd/system/ &&
sudo systemctl daemon-reload && sudo systemctl enable --now <unit>`.

## Known failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `index.lock` on pull | crashed/editor git process | `pgrep -a git` — only if none, remove the lock |
| restart hangs / ssh times out | machine rebooting or power loss (battery was 0 Wh, not charging — no UPS) | retry; `tailscale ping`, `nc -z host 22`; if port 22 closed but tailscale pongs, userspace/sshd is down — needs console |
| `ModuleNotFoundError: torch` | torch is deliberately absent from `requirements.txt` | `make install-ml` on the server (CPU wheel), then restart |
| service `activating` then dead | wrong launch mode (above) or `MemoryMax` too small for torch | check `journalctl -u <svc> -n 50` |
| 2 orchestrators / Telegram `Conflict` | duplicate enabled unit or manual process | keep only `stockanalysis.service`; `pgrep -af orchestrator.main` |

## Verification

```bash
systemctl is-active stockanalysis{,-notification,-data-gateway,-market-data,-analysis-engine,-signal-intelligence,-auth,-resource-monitor,-paper-trading}
redis-cli HGET service:registry:paper-trading status
redis-cli HGET service:registry:volatility-engine models
journalctl -u <svc> -n 50 --no-pager
make server-svcs-status
```

Restarting during market hours is safe but expect a ~30 s gap; do it before
09:15 or after 15:30 IST unless urgent.

## Rollback

On the server: `git reset --hard <previous-sha>` + restart the affected units,
or revert the commit locally, push, and redeploy. Unit files are versioned in
`configs/`, so rollback also restores service definitions on next deploy.
