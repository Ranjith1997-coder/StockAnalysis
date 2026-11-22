---
description: Deploy master to the production server, verify services and heartbeats
---

Deploy StockAnalysis to production (physical server via Tailscale — see the
`stockanalysis-deploy` skill).

1. **Preflight — stop on any failure**
   - `git branch --show-current` must be `master`.
   - `git fetch origin && git status -sb`: local must equal `origin/master`.
     If ahead, stop and report that a push is needed (deploy pulls origin).
   - Run `make test-fast`. If it fails, stop and report the failure.
2. **Deploy**
   - Run `make deploy` and capture output.
   - Likely failure modes: stale `.git/index.lock` (check `pgrep -a git` before
     removing), pull conflict, ssh restart timeout (server reboot/power).
3. **Post-deploy extras** (deploy does not cover these)
   - If `configs/*.service` changed for a unit that is *not* installed on the
     server, install it: `cp` → `daemon-reload` → `enable --now`.
   - Restart `stockanalysis-pinn-trainer` and
     `stockanalysis-volatility-engine` if their code changed (they are not in
     the deploy restart list).
4. **Verify and report**
   - `systemctl is-active` for all 11 units.
   - `redis-cli HGET service:registry:paper-trading status` and
     `... volatility-engine models`.
   - `journalctl -u stockanalysis -n 30 --no-pager` for errors/Conflicts.
   - Report the deployed commit and any service not `active`.

$ARGUMENTS
