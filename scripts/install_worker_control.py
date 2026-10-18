"""Install the privileged worker control code into a fresh, root-owned AMI path.

Run during image baking, before any workload is admitted. A new AMI is required
for updates; replacing a live control tree would invalidate in-flight commands.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = Path("/opt/spot-worker-control")


def control_files(source: Path = ROOT) -> list[Path]:
    files = [source / "control_entry.py", source / "common_constants.py",
             source / "checkpoint" / "criu_wrapper.sh",
             source / "scripts" / "bootstrap_worker_security.sh"]
    files.extend(sorted((source / "worker").rglob("*.py")))
    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"missing or unsafe control source: {path}")
    return files


def install_control(destination: Path = DEFAULT_DESTINATION,
                    source: Path = ROOT, *, require_root: bool = True,
                    allowed_scripts=()) -> str:
    if require_root and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        raise PermissionError("worker control installation requires root")
    destination = Path(destination).absolute()
    source = Path(source).resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("control tree already exists; bake a new image")
    if destination.parent.is_symlink():
        raise ValueError("control parent must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(destination.name + ".staging-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o755)
    try:
        manifest = {}
        for original in control_files(source):
            relative = original.relative_to(source)
            target = stage / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            data = original.read_bytes().replace(b"\r\n", b"\n")
            target.write_bytes(data)
            target.chmod(0o755 if target.suffix == ".sh" else 0o644)
            if require_root:
                os.chown(target, 0, 0)
            manifest[relative.as_posix()] = hashlib.sha256(data).hexdigest()
        allowed = sorted({"sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
                          for path in allowed_scripts})
        policy_path = stage / "allowed-workloads.json"
        policy_path.write_text(json.dumps({"schema": "trusted-scripts/v1",
                                           "allowed_sha256": allowed},
                                          sort_keys=True, indent=2) + "\n")
        policy_path.chmod(0o644)
        if require_root:
            os.chown(policy_path, 0, 0)
        manifest["allowed-workloads.json"] = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        digest = "sha256:" + hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (stage / "manifest.json").write_text(json.dumps({
            "schema": "worker-control/v1", "digest": digest,
            "files": manifest}, sort_keys=True, indent=2) + "\n")
        (stage / "manifest.json").chmod(0o644)
        if require_root:
            os.chown(stage / "manifest.json", 0, 0)
            for directory in [stage, *[p for p in stage.rglob("*") if p.is_dir()]]:
                os.chown(directory, 0, 0)
                directory.chmod(0o755)
        stage.rename(destination)
        return digest
    except BaseException:
        shutil.rmtree(stage)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--allow-script", type=Path, action="append", default=[],
                        help="Reviewed Python script to permit on this AMI")
    args = parser.parse_args()
    print(install_control(args.destination, allowed_scripts=args.allow_script))


if __name__ == "__main__":
    main()
