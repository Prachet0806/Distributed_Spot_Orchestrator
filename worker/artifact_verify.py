"""Verify all staged checkpoint files before writing a transfer receipt."""
import argparse
import hashlib
import json
from pathlib import Path
from worker.atomic_io import write_json


def verify(directory, manifest, operation_id):
    directory = Path(directory).resolve()
    for relative, expected in manifest["files"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or path.is_symlink() or not path.is_file():
            raise RuntimeError("unsafe or missing checkpoint member")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected["sha256"] or path.stat().st_size != expected["size_bytes"]:
            raise RuntimeError("staged checkpoint checksum mismatch")
    receipt = {"state": "SUCCEEDED", "operation_id": operation_id,
               "checkpoint_id": manifest["job_id"], "result": {"status": "COMPLETED",
               "bytes_transferred": manifest["artifact"]["size_bytes"]}}
    write_json(directory / "transfer.receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, json.loads(Path(args.manifest).read_text()), args.operation_id)))


if __name__ == "__main__":
    main()
