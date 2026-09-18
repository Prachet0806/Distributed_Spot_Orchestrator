# scripts/worker_ctl.py — Track A worker control (start/status/stop/logs).
"""Thin wrapper over SSHClient for pilot worker ops. Uses the [j] pattern so
process kills never match their own remote shell (pkill -f footgun)."""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")
from orchestrator.utils import SSHClient  # noqa: E402

WS = "/opt/job_workspace"
SELF_SAFE = "[j]ob_runner.py"

# Windows OpenSSH client intermittently reports exit 4294967295 for commands
# that demonstrably executed server-side. All mutating commands below are
# therefore followed by state verification instead of trusting the exit code.
BOGUS_RC = "4294967295"


def _ssh(args):
    return SSHClient(args.host, user=args.user, key_path=args.key, timeout=args.timeout)


def _put(args, local_path, remote_path):
    """Copy a file via paramiko/SCP (reliable where long ssh commands flake)."""
    import paramiko
    from scp import SCPClient
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, key_filename=args.key)
    try:
        with SCPClient(client.get_transport()) as scp:
            scp.put(local_path, remote_path=remote_path)
    finally:
        client.close()


def _run_verified(ssh, command, verify, attempts=6, wait=5.0, timeout=60):
    """Run a command, tolerating the bogus -1 exit; verify server-side state."""
    try:
        ssh.run_command(command, timeout_override=timeout)
    except RuntimeError as exc:
        if BOGUS_RC not in str(exc):
            raise
    last = ""
    for _ in range(attempts):
        time.sleep(wait)
        last = ssh.run_command(verify, timeout_override=timeout).stdout
        if "READY-OK" in last:
            return last
    raise RuntimeError(f"verify failed after run: {last[-500:]}")


def cmd_start(args):
    ssh = _ssh(args)
    ssh.connect()
    env = (f"WORKLOAD_JOB={args.job} WORKLOAD_TICKS={args.ticks} "
           f"WORKLOAD_TICK_SLEEP={args.tick_sleep}")
    # systemd unit: reliable daemonization over non-interactive SSH
    # (background &/nohup leaves the channel attached and hangs/fails).
    unit = (
        "[Unit]\nDescription=Spot arbitrage worker job\nAfter=network.target\n\n"
        "[Service]\nType=simple\nUser=ubuntu\n"
        f"WorkingDirectory={WS}\n"
        f"Environment={env} WORKLOAD_PROGRESS_PATH={WS}/progress.json\n"
        f"ExecStart=/usr/bin/python3 {WS}/worker/job_runner.py\n"
        "Restart=no\n"
        f"StandardOutput=append:{WS}/job.log\nStandardError=inherit\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".service",
                                     delete=False) as f:
        f.write(unit)
        unit_path = f.name
    try:
        _put(args, unit_path, "/tmp/spot-job.service")
    finally:
        os.remove(unit_path)

    def _cmd(command):
        try:
            ssh.run_command(command)
        except RuntimeError as exc:
            if BOGUS_RC not in str(exc):
                raise

    _cmd(f"sudo cp /tmp/spot-job.service /etc/systemd/system/spot-job.service")
    _cmd("sudo systemctl daemon-reload")
    _cmd(f"pkill -f '{SELF_SAFE}' || true")
    _cmd(f"rm -f {WS}/READY {WS}/job.log")
    _cmd("sudo systemctl restart spot-job.service")
    out = _run_verified(
        ssh, "true",
        f"test -f {WS}/READY && test $(find {WS}/READY -mmin -2) && echo READY-OK || echo waiting",
        attempts=8, wait=5.0)
    print("started")
    ssh.close()


def cmd_status(args):
    ssh = _ssh(args)
    ssh.connect()
    r = ssh.run_command(
        f"ps aux | grep {SELF_SAFE} | head -3; echo ---READY---; "
        f"cat {WS}/READY 2>/dev/null || echo NO-READY; echo ---LOG---; "
        f"tail -5 {WS}/job.log 2>/dev/null; echo ---PROGRESS---; "
        f"cat {WS}/progress.json 2>/dev/null || echo NO-PROGRESS")
    print(r.stdout)
    ssh.close()


def cmd_stop(args):
    ssh = _ssh(args)
    ssh.connect()
    ssh.run_command(f"pkill -f '{SELF_SAFE}'; echo stopped")
    print("stopped")
    ssh.close()


def cmd_run(args):
    ssh = _ssh(args)
    ssh.connect()
    r = ssh.run_command(args.command, timeout_override=args.timeout)
    print(r.stdout)
    if r.stderr:
        print("STDERR:", r.stderr[-2000:])
    ssh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--timeout", type=int, default=60)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--job", default="fixture")
    s.add_argument("--ticks", default="2000000")
    s.add_argument("--tick-sleep", default="0.0002")
    sub.add_parser("status")
    sub.add_parser("stop")
    r = sub.add_parser("run")
    r.add_argument("command")
    args = ap.parse_args()
    {"start": cmd_start, "status": cmd_status, "stop": cmd_stop,
     "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
