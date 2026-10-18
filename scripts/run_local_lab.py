"""Credential-free qualification runner. Never constructs real AWS clients."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["services", "linux", "all"], default="services")
    parser.add_argument("--output", default="lab-results")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
                "aws_qualification": "NOT_RUN", "results": []}
    env = {**os.environ, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
           "AWS_SESSION_TOKEN": "testing", "AWS_EC2_METADATA_DISABLED": "true"}
    if args.mode in {"services", "all"}:
        command = [sys.executable, "-m", "pytest", "tests/test_runtime_integration.py",
                   "tests/test_durable_restart.py", "tests/integration", "-q", "--disable-warnings",
                   f"--junitxml={output / 'services.xml'}"]
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        (output / "services.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        evidence["results"].append({"level": "emulated-services", "exit_code": result.returncode})
    if args.mode in {"linux", "all"}:
        if sys.platform != "linux" or os.geteuid() != 0:
            evidence["results"].append({"level": "real-linux", "status": "BLOCKED", "reason": "Linux root with CRIU and SSH server required"})
        else:
            command = [sys.executable, "scripts/linux_lab.py", "--output", str(output)]
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            (output / "linux.log").write_text(result.stdout + result.stderr)
            evidence["results"].append({"level": "real-linux", "exit_code": result.returncode})
    (output / "summary.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))
    return 0 if all(r.get("exit_code") == 0 for r in evidence["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
