"""Write-before-restore journal in the root-owned worker control domain.

An issued restore is never repeated after a lost response. A verified,
identity-matched stopped process can be reported from its durable receipt;
every other uncertain frontier remains UNKNOWN for reconciliation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from worker.atomic_io import write_json
from worker.process_control import process_identity
from worker.workload_launcher import stage_trusted_script, validate_trusted_digest


JOURNAL_ROOT = Path("/var/lib/spot-worker/restores")
IMAGES_ROOT = Path("/opt/job_workspace/checkpoints")
WRAPPER = Path("/opt/spot-worker-control/checkpoint/criu_wrapper.sh")
BOOT_IDENTITY = Path("/var/lib/spot-worker/identity.json")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _paths(operation_id, journal_root):
    if not IDENTIFIER.fullmatch(operation_id):
        raise ValueError("invalid restore operation identity")
    root = Path(journal_root)
    return root / (operation_id + ".issued.json"), root / (operation_id + ".result.json")


def _request(operation_id, images_dir, images_root):
    directory = Path(images_dir).resolve()
    root = Path(images_root).resolve()
    if not directory.is_relative_to(root) or not IDENTIFIER.fullmatch(directory.name):
        raise ValueError("restore images must be a named checkpoint directory")
    return {"operation_id": operation_id, "checkpoint_id": directory.name,
            "images_dir": str(directory)}


def restore_status(operation_id, images_dir, *, journal_root=JOURNAL_ROOT,
                   images_root=IMAGES_ROOT, identity_lookup=process_identity):
    issued, result_path = _paths(operation_id, journal_root)
    request = _request(operation_id, images_dir, images_root)
    if not issued.is_file():
        return {"state": "UNKNOWN", "reason": "restore command was not durably issued"}
    if json.loads(issued.read_text()) != request:
        raise RuntimeError("restore operation request mismatch")
    if not result_path.is_file():
        return {"state": "UNKNOWN", "reason": "restore issued without a terminal receipt"}
    receipt = json.loads(result_path.read_text())
    if receipt.get("request") != request:
        raise RuntimeError("restore terminal receipt request mismatch")
    if receipt.get("state") == "FAILED":
        return {"state": "FAILED", "reason": receipt.get("reason", "restore failed")}
    if receipt.get("state") != "SUCCEEDED":
        return {"state": "UNKNOWN", "reason": "invalid restore terminal receipt"}
    pid = int(receipt["process_id"])
    observed = identity_lookup(pid)
    if (not observed or observed.get("process_start") != str(receipt["process_start"])
            or not observed.get("state", "").startswith(("T", "t"))):
        return {"state": "UNKNOWN", "reason": "restored process is not identity-matched and contained"}
    return {"state": "SUCCEEDED", "result": {"process_id": pid,
            "process_start": receipt["process_start"], "contained": True,
            "checkpoint_id": request["checkpoint_id"]}}


def run_restore(operation_id, images_dir, *, journal_root=JOURNAL_ROOT,
                images_root=IMAGES_ROOT, wrapper=WRAPPER,
                runner=subprocess.run, identity_lookup=process_identity,
                identity_path=BOOT_IDENTITY):
    issued, result_path = _paths(operation_id, journal_root)
    request = _request(operation_id, images_dir, images_root)
    if issued.is_file():
        return restore_status(operation_id, images_dir, journal_root=journal_root,
                              images_root=images_root, identity_lookup=identity_lookup)
    boot_path = Path(identity_path)
    boot_stat = boot_path.stat()
    if os.name == "posix" and (boot_stat.st_uid != 0 or boot_stat.st_mode & 0o077):
        raise PermissionError("worker boot identity must be private to root")
    provider = json.loads(boot_path.read_text()).get("provider")
    if provider == "aws":
        script = Path(images_dir) / "appstate" / "workload.py"
        if not script.is_file() or script.is_symlink():
            raise ValueError("restored workload script is missing or linked")
        digest = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
        validate_trusted_digest(digest)
        stage_trusted_script(script, digest)
    elif provider != "local":
        raise ValueError("unsupported worker provider for restore")
    root = issued.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        stat = root.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o077:
            raise PermissionError("restore journal must be private to root")
    try:
        fd = os.open(issued, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        prior = restore_status(operation_id, images_dir, journal_root=journal_root,
                               images_root=images_root, identity_lookup=identity_lookup)
        return prior
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(request, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    if os.name == "posix":
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    environment = {**os.environ, "RESTORE_OPERATION_ID": operation_id}
    try:
        completed = runner(["bash", str(wrapper), "restore", request["images_dir"]],
                           capture_output=True, text=True, env=environment, timeout=190)
    except subprocess.TimeoutExpired:
        return {"state": "UNKNOWN", "reason": "restore wrapper timed out"}
    if completed.returncode != 0:
        if completed.returncode in {1, 4}:
            write_json(result_path, {"request": request, "state": "FAILED",
                                     "reason": completed.stderr[-1000:]})
            return {"state": "FAILED", "returncode": completed.returncode,
                    "stderr": completed.stderr}
        return {"state": "UNKNOWN", "returncode": completed.returncode,
                "stderr": completed.stderr}
    wrapper_receipt = json.loads((Path(images_dir) / "restore.receipt.json").read_text())
    if wrapper_receipt.get("operation_id") != operation_id:
        return {"state": "UNKNOWN", "reason": "wrapper receipt does not bind the operation"}
    pid = int(wrapper_receipt["process_id"])
    observed = identity_lookup(pid)
    if (not observed or observed.get("process_start") != str(wrapper_receipt["process_start"])
            or not observed.get("state", "").startswith(("T", "t"))):
        return {"state": "UNKNOWN", "reason": "restored process containment is unproven"}
    write_json(result_path, {"request": request, "state": "SUCCEEDED",
                             "process_id": pid, "process_start": str(wrapper_receipt["process_start"])})
    return restore_status(operation_id, images_dir, journal_root=journal_root,
                          images_root=images_root, identity_lookup=identity_lookup)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "status"])
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--images-dir", required=True)
    args = parser.parse_args()
    try:
        result = (run_restore(args.operation_id, args.images_dir) if args.action == "run"
                  else restore_status(args.operation_id, args.images_dir))
        print(json.dumps(result, sort_keys=True))
        if args.action == "run":
            if result["state"] == "SUCCEEDED":
                return 0
            return result.get("returncode", 2)
        return 0
    except Exception as exc:
        print(f"restore guard failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
