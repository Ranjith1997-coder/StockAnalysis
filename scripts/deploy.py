"""Deploy latest master to the production server.

Production is a physical machine reached over Tailscale SSH (see
configs/server_metadata.yaml) — NOT an EC2 instance. Deployment is
git-pull based:

  1. Verify local HEAD is pushed to origin/master
  2. git pull --ff-only on the server
  3. Sync systemd unit files from configs/ (only units already installed)
  4. Restart StockAnalysis services in dependency order
  5. Report per-service status

Usage:
    make deploy        # or: PYTHONPATH=. python scripts/deploy.py

Environment overrides (via .env or shell):
    DEPLOY_SERVER   SSH target         (default: hacker@100.92.21.31)
    DEPLOY_APP_DIR  repo dir on server (default: ~/StockAnalysis)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

SERVER = os.getenv("DEPLOY_SERVER", "hacker@100.92.21.31")
APP_DIR = os.getenv("DEPLOY_APP_DIR", "~/StockAnalysis")
REPO_ROOT = Path(__file__).resolve().parent.parent

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

# Restart order: dependencies first, monolith (stockanalysis) last.
# stockanalysis-auth is a timer-triggered oneshot — never restarted here.
# stockanalysis-positional is timer-driven (20:00 IST EOD run) — also skipped.
RESTART_SERVICES = [
    "stockanalysis-notification",
    "stockanalysis-data-gateway",
    "stockanalysis-market-data",
    "stockanalysis-analysis-engine",
    "stockanalysis-signal-intelligence",
    "stockanalysis-resource-monitor",
    "stockanalysis-paper-trading",
    "stockanalysis",
]


def run_local(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=timeout)


def ssh(command, timeout=120):
    return subprocess.run(["ssh", *SSH_OPTS, SERVER, command],
                          capture_output=True, text=True, timeout=timeout)


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def check_local_state():
    """Local HEAD must equal origin/master — unpushed commits can't deploy."""
    run_local(["git", "fetch", "origin"])
    head = run_local(["git", "rev-parse", "HEAD"]).stdout.strip()
    origin_master = run_local(["git", "rev-parse", "origin/master"]).stdout.strip()
    if not head or head != origin_master:
        die(f"local HEAD {head[:7]} != origin/master {origin_master[:7]} — "
            "commit and push first")
    dirty = run_local(["git", "status", "--porcelain"]).stdout.strip()
    if dirty:
        print("WARNING: local working tree is dirty — "
              "uncommitted changes will NOT be deployed:")
        print(dirty)
    return head


def pull_on_server(expected_head):
    print(f"Pulling latest master on {SERVER}:{APP_DIR} ...")
    pull = ssh(f"cd {APP_DIR} && git pull --ff-only origin master", timeout=300)
    output = (pull.stdout + pull.stderr).strip()
    if output:
        print(output)
    if pull.returncode != 0:
        die("git pull failed on server — check for a dirty tree or a stale "
            ".git/index.lock (remove it only after verifying no git process "
            "is running: pgrep -a git)")
    server_head = ssh(f"cd {APP_DIR} && git rev-parse HEAD").stdout.strip()
    if server_head != expected_head:
        die(f"server is at {server_head[:7]}, expected {expected_head[:7]}")
    print(f"Server at {server_head[:7]} [ok]")


def sync_unit_files():
    """Copy configs/stockanalysis*.service to the server — only units that
    are already installed there. Never installs new units; that stays a
    deliberate ops action."""
    unit_files = sorted((REPO_ROOT / "configs").glob("stockanalysis*.service"))
    installed = {
        os.path.basename(p)
        for p in ssh("ls /etc/systemd/system/stockanalysis*.service 2>/dev/null"
                     ).stdout.split()
    }
    synced = []
    for path in unit_files:
        if path.name not in installed:
            continue
        remote_tmp = f"/tmp/{path.name}.deploy"
        scp = subprocess.run(["scp", *SSH_OPTS, str(path), f"{SERVER}:{remote_tmp}"],
                             capture_output=True, text=True, timeout=60)
        if scp.returncode != 0:
            die(f"scp {path.name} failed: {scp.stderr.strip()}")
        install = ssh(f"sudo cp {remote_tmp} /etc/systemd/system/{path.name} "
                      f"&& rm -f {remote_tmp}")
        if install.returncode != 0:
            die(f"installing {path.name} failed: {install.stderr.strip()}")
        synced.append(path.name)
    if not synced:
        print("No installed unit files to sync.")
        return
    reload = ssh("sudo systemctl daemon-reload")
    if reload.returncode != 0:
        die(f"daemon-reload failed: {reload.stderr.strip()}")
    print(f"Unit files synced: {', '.join(synced)}")


def restart_services():
    print("Restarting services ...")
    failed = []
    for svc in RESTART_SERVICES:
        result = ssh(f"sudo systemctl restart {svc}", timeout=180)
        if result.returncode != 0:
            failed.append(svc)
            print(f"  {svc}: FAILED — {result.stderr.strip()}")
        else:
            print(f"  {svc}: restarted")
    if failed:
        die(f"restart failed for: {', '.join(failed)} — check "
            "journalctl -u <service>")


def verify_statuses():
    print("Waiting 15s for services to settle ...")
    time.sleep(15)
    result = ssh("systemctl is-active " + " ".join(RESTART_SERVICES))
    states = result.stdout.split()
    down = [svc for svc, state in zip(RESTART_SERVICES, states)
            if state != "active"]
    print("Service status:")
    for svc, state in zip(RESTART_SERVICES, states):
        print(f"  {svc}: {state}")
    if down:
        die(f"not active after restart: {', '.join(down)} — inspect with "
            "'journalctl -u <service> -n 50' or 'make server-svcs-status'")
    print("All services active. Deploy complete.")


def main():
    head = check_local_state()
    print(f"Deploying {head[:7]} to {SERVER}")
    pull_on_server(head)
    sync_unit_files()
    restart_services()
    verify_statuses()


if __name__ == "__main__":
    main()
