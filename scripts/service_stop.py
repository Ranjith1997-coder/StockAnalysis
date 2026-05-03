"""
service_stop.py — Start the EC2 instance (if stopped) and halt stock_analysis.service.

Holiday / weekend guard:
  On non-trading days intraday_monitor.py exits immediately at boot and, if
  SHUTDOWN_SYSTEM=1 is set, calls /sbin/shutdown -h now.  Starting a stopped
  instance triggers boot → holiday-exit → OS-shutdown before we can SSH in.

  Behaviour on a non-trading day:
    STOPPED  → exits early  (service cannot be running; nothing to stop)
    RUNNING  → SSHs in directly (no boot wait needed) and stops the service

  --force bypasses the guard (for dev / debugging):
    make service-stop-force
    python scripts/service_stop.py --force

  Even with --force, SSH connection uses a retry-poll rather than a fixed sleep
  so we connect the instant the daemon is reachable and stop the service before
  intraday_monitor.py has a chance to trigger an OS shutdown.
"""

import os
import sys
import time
import socket
import boto3
import paramiko
from dotenv import load_dotenv

load_dotenv(override=True)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
EC2_INSTANCE_ID = os.getenv("EC2_INSTANCE_ID")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
SSH_USERNAME = os.getenv("SSH_USERNAME", "ec2-user")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

_required = {
    "EC2_INSTANCE_ID": EC2_INSTANCE_ID,
    "SSH_KEY_PATH": SSH_KEY_PATH,
    "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
    "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY,
}
missing = [k for k, v in _required.items() if not v]
if missing:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(missing)}\n"
        "Please set them in your .env file or environment."
    )

ec2 = boto3.client(
    "ec2",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)


def get_instance_state(instance_id: str) -> str:
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


def _wait_for_ssh(host: str, port: int = 22, retries: int = 30, interval: int = 3) -> None:
    """Poll TCP port 22 until sshd accepts connections.

    Using a retry loop instead of a fixed sleep means we connect the instant
    the daemon is ready — critical on holidays where intraday_monitor.py can
    trigger an OS shutdown shortly after boot.
    """
    print(f"Polling {host}:22 for SSH availability ", end="", flush=True)
    for attempt in range(retries):
        try:
            with socket.create_connection((host, port), timeout=3):
                print(" ready.")
                return
        except OSError:
            print(".", end="", flush=True)
            time.sleep(interval)
    print()
    raise RuntimeError(f"SSH on {host}:{port} not reachable after {retries * interval}s")


def ensure_instance_running(instance_id: str) -> None:
    state = get_instance_state(instance_id)

    if state == "running":
        print(f"Instance {instance_id} is already running.")
        return
    if state == "stopped":
        print(f"Instance {instance_id} is stopped — starting it...")
        ec2.start_instances(InstanceIds=[instance_id])
        waiter = ec2.get_waiter("instance_running")
        print("Waiting for instance to reach running state...")
        waiter.wait(InstanceIds=[instance_id])
    else:
        raise RuntimeError(
            f"Instance {instance_id} is in '{state}' state — cannot proceed."
        )


def get_public_ip(instance_id: str) -> str:
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]["PublicIpAddress"]


def stop_service(public_ip: str, poll_ssh: bool = True) -> None:
    if poll_ssh:
        _wait_for_ssh(public_ip)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting via SSH to {public_ip} ...")
    ssh.connect(public_ip, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)

    try:
        _run(ssh, "sudo systemctl stop stock_analysis.service")
        _run(ssh, "sudo systemctl status stock_analysis.service --no-pager || true")
    finally:
        ssh.close()
        print("SSH connection closed.")


def _run(ssh: paramiko.SSHClient, cmd: str) -> str:
    print(f"  $ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(out)
    if err:
        print(err)
    return out


def main() -> None:
    force = "--force" in sys.argv

    if not force:
        # ── Holiday / weekend guard ────────────────────────────────────────
        # Starting a stopped instance on a non-trading day is unsafe: systemd
        # launches intraday_monitor.py which detects the holiday and (if
        # SHUTDOWN_SYSTEM=1) immediately shuts the OS down — the instance
        # disappears before we can connect.
        try:
            from common.market_calendar import is_trading_day
            from datetime import date
            if not is_trading_day(date.today()):
                state = get_instance_state(EC2_INSTANCE_ID)
                if state != "running":
                    print(
                        f"Today is not a trading day and the instance is '{state}'.\n"
                        "The service cannot be running — nothing to stop.\n"
                        "For dev/testing use: make service-stop-force"
                    )
                    sys.exit(0)
                # Instance is already running (started manually) — SSH directly,
                # no boot-wait poll needed since the instance is already up.
                print(
                    "WARNING: Today is not a trading day but the instance is running.\n"
                    "Connecting directly to stop the service."
                )
                public_ip = get_public_ip(EC2_INSTANCE_ID)
                print(f"Public IP: {public_ip}")
                stop_service(public_ip, poll_ssh=False)
                print("==> stock_analysis.service stopped successfully.")
                return
        except ImportError:
            print("WARNING: common.market_calendar not importable — skipping holiday check.")

    print(f"==> service-stop  |  instance: {EC2_INSTANCE_ID}")
    ensure_instance_running(EC2_INSTANCE_ID)
    public_ip = get_public_ip(EC2_INSTANCE_ID)
    print(f"Public IP: {public_ip}")
    # poll_ssh=True: connect the instant sshd is ready instead of a fixed sleep
    stop_service(public_ip, poll_ssh=True)
    print("==> stock_analysis.service stopped successfully.")


if __name__ == "__main__":
    main()
