"""Verify all staged checkpoint files before writing a transfer receipt."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from worker.atomic_io import write_json


def _check_files(directory, manifest, *, receipt_present=False):
    directory = Path(directory).resolve()
    expected_files = set(manifest["files"])
    members = list(directory.rglob("*"))
    if any(path.is_symlink() for path in members):
        raise RuntimeError("staged checkpoint contains a link")
    actual_files = {
        str(path.relative_to(directory)).replace("\\", "/")
        for path in members
        if path.is_file()
    }
    allowed = expected_files | {"manifest.json"}
    if receipt_present:
        allowed.add("transfer.receipt.json")
    if actual_files != allowed:
        raise RuntimeError("staged checkpoint has missing or unexpected files")
    for relative, expected in manifest["files"].items():
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise RuntimeError("unsafe checkpoint member")
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise RuntimeError("unsafe or missing checkpoint member")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if (
            digest.hexdigest() != expected["sha256"]
            or path.stat().st_size != expected["size_bytes"]
        ):
            raise RuntimeError("staged checkpoint checksum mismatch")


def verify(directory, manifest, operation_id):
    _check_files(directory, manifest)
    receipt = {
        "state": "SUCCEEDED",
        "operation_id": operation_id,
        "checkpoint_id": manifest["job_id"],
        "artifact_sha256": manifest["artifact"]["sha256"],
        "result": {
            "status": "COMPLETED",
            "bytes_transferred": manifest["artifact"]["size_bytes"],
        },
    }
    write_json(directory / "transfer.receipt.json", receipt)
    return receipt


def _paths(workspace, checkpoint_id, operation_id):
    for value in (checkpoint_id, operation_id):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value):
            raise ValueError("unsafe transfer identity")
    parent = Path(workspace).resolve() / "checkpoints"
    if parent.is_symlink():
        raise RuntimeError("unsafe checkpoint parent")
    return (
        parent,
        parent / checkpoint_id,
        parent / f".{checkpoint_id}.{operation_id}.staging",
    )


def status(workspace, checkpoint_id, operation_id, checksum):
    _, final, _ = _paths(workspace, checkpoint_id, operation_id)
    receipt_path = final / "transfer.receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, NotADirectoryError):
        return {"operation_id": operation_id, "state": "UNKNOWN"}
    if (
        receipt.get("state") == "SUCCEEDED"
        and receipt.get("operation_id") == operation_id
        and receipt.get("checkpoint_id") == checkpoint_id
        and receipt.get("artifact_sha256") == checksum
    ):
        try:
            manifest = json.loads((final / "manifest.json").read_text())
            if (
                manifest.get("job_id") == checkpoint_id
                and manifest.get("artifact", {}).get("sha256") == checksum
            ):
                _check_files(final, manifest, receipt_present=True)
                return receipt
        except (FileNotFoundError, json.JSONDecodeError, RuntimeError):
            pass
    return {"operation_id": operation_id, "state": "UNKNOWN"}


def prepare(workspace, checkpoint_id, operation_id, checksum):
    parent, final, staging = _paths(workspace, checkpoint_id, operation_id)
    current = status(workspace, checkpoint_id, operation_id, checksum)
    if current["state"] == "SUCCEEDED":
        return current
    if final.exists() or final.is_symlink():
        raise RuntimeError("checkpoint destination has a conflicting receipt")
    parent.mkdir(parents=True, exist_ok=True)
    if staging.is_symlink():
        raise RuntimeError("unsafe checkpoint staging path")
    if staging.exists():
        shutil.rmtree(staging)
    return {"operation_id": operation_id, "state": "READY"}


def commit(workspace, checkpoint_id, operation_id, checksum):
    parent, final, staging = _paths(workspace, checkpoint_id, operation_id)
    current = status(workspace, checkpoint_id, operation_id, checksum)
    if current["state"] == "SUCCEEDED":
        return current
    if final.exists() or final.is_symlink() or staging.is_symlink():
        raise RuntimeError("checkpoint destination has a conflicting receipt")
    manifest = json.loads((staging / "manifest.json").read_text())
    if (
        manifest.get("job_id") != checkpoint_id
        or manifest.get("artifact", {}).get("sha256") != checksum
    ):
        raise RuntimeError("checkpoint manifest identity mismatch")
    receipt = verify(staging, manifest, operation_id)
    os.rename(staging, final)
    if os.name != "nt":
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "commit", "status"])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--checkpoint-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--checksum", required=True)
    args = parser.parse_args()
    actions = {"prepare": prepare, "commit": commit, "status": status}
    print(
        json.dumps(
            actions[args.action](
                args.workspace, args.checkpoint_id, args.operation_id, args.checksum
            )
        )
    )


if __name__ == "__main__":
    main()
