"""Process identity checks: never signal a recycled PID."""
import argparse
import json
import os
from pathlib import Path
import signal


def process_identity(pid):
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError("invalid workload PID")
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return {"process_start": fields[19], "state": fields[0]}


def control(pid, expected_start, action):
    if not expected_start:
        raise ValueError("process start identity is required")
    current = process_identity(pid)
    original_present = current is not None and current["process_start"] == str(expected_start)
    if action == "inspect":
        return {"same_process": original_present,
                "contained": bool(original_present and current["state"].startswith(("T", "t")))}
    if action == "verify":
        return {"process_dead": not original_present or current["state"] == "Z"}
    if not original_present:
        if action == "terminate":
            return {"process_dead": True}
        raise RuntimeError("process identity changed")
    sig = {"terminate": signal.SIGKILL, "activate": signal.SIGCONT, "stop": signal.SIGSTOP}[action]
    os.kill(pid, sig)
    return {"signalled": True, "pid": pid}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["terminate", "verify", "activate", "stop", "inspect"])
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--start", required=True)
    args = parser.parse_args()
    print(json.dumps(control(args.pid, args.start, args.action)))


if __name__ == "__main__":
    main()
