"""Fixed entrypoint for root-owned worker control code, launched with -I."""
import hashlib
import json
import sys
from pathlib import Path


def verify_control_tree(root: Path, *, require_root: bool = True) -> str:
    if root.is_symlink():
        raise RuntimeError("worker control root must not be a symlink")
    root = root.absolute()
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema") != "worker-control/v1":
        raise RuntimeError("invalid worker control manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("empty worker control manifest")
    digest = "sha256:" + hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != manifest.get("digest"):
        raise RuntimeError("worker control manifest digest mismatch")
    paths = {root, root / "manifest.json"}
    for name in files:
        path = root / name
        paths.add(path)
        paths.update(parent for parent in path.parents if parent == root or root in parent.parents)
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeError("worker control path escapes its root")
        stat = path.stat()
        if require_root and (stat.st_uid != 0 or stat.st_mode & 0o022):
            raise RuntimeError("worker control path is not root-owned and read-only")
    for name, expected in files.items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"worker control file digest mismatch: {name}")
    return digest


def main():
    root = Path(__file__).resolve().parent
    if sys.argv[1:] == ["--self-check"]:
        print(verify_control_tree(root))
        return
    verify_control_tree(root)
    sys.path.insert(0, str(root))
    if len(sys.argv) > 1:
        action = sys.argv[1]
        if action in {"launch", "process-control", "verify-artifact", "restore", "spot-monitor"}:
            sys.argv.pop(1)
            if action == "launch":
                from worker.workload_launcher import main as action_main
            elif action == "process-control":
                from worker.process_control import main as action_main
            elif action == "restore":
                from worker.restore_guard import main as action_main
            elif action == "spot-monitor":
                from worker.spot_interrupt import main as action_main
            else:
                from worker.artifact_verify import main as action_main
            return action_main()
    from worker.control_cli import main as control_main
    control_main()


if __name__ == "__main__":
    raise SystemExit(main())
