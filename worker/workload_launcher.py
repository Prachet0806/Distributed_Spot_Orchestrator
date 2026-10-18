"""Launch a staged CPU workload once; SQLite journals attempts before Popen."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from worker.atomic_io import write_json


POLICY_PATH = Path("/opt/spot-worker-control/allowed-workloads.json")
STAGED_ROOT = Path("/opt/spot-worker-staged")


def stage_trusted_script(script, digest, root=STAGED_ROOT):
    """Pin exactly the reviewed bytes in a root-owned directory before launch."""
    script, root = Path(script), Path(root)
    if script.is_symlink():
        raise ValueError("workload script must not be a symlink")
    fd = os.open(script, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as source:
            content = source.read()
    except BaseException:
        raise
    if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("workload changed during admission")
    if root.is_symlink() or not root.is_dir():
        raise PermissionError("trusted script staging directory is missing or linked")
    owner = root.stat()
    if owner.st_uid != 0 or owner.st_mode & 0o022:
        raise PermissionError("trusted script staging directory is writable")
    target = root / (digest.split(":", 1)[1] + ".py")
    try:
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        out = None
    if out is not None:
        with os.fdopen(out, "wb") as staged:
            staged.write(content)
            staged.flush()
            os.fsync(staged.fileno())
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    if target.is_symlink() or not target.is_file():
        raise PermissionError("trusted script staging target is unsafe")
    target_stat = target.stat()
    if target_stat.st_uid != 0 or target_stat.st_mode & 0o022:
        raise PermissionError("trusted script staging target is writable")
    if "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise PermissionError("trusted script staging target digest mismatch")
    return target


def validate_trusted_digest(digest, policy_path=POLICY_PATH, *, enforce_owner=True):
    policy_path = Path(policy_path)
    if policy_path.is_symlink() or not policy_path.is_file():
        raise PermissionError("trusted workload policy is missing or linked")
    stat = policy_path.stat()
    if enforce_owner and (not hasattr(os, "geteuid") or stat.st_uid != 0
                          or stat.st_mode & 0o022):
        raise PermissionError("trusted workload policy is not root-owned")
    policy = json.loads(policy_path.read_text())
    allowed = policy.get("allowed_sha256")
    if (policy.get("schema") != "trusted-scripts/v1" or not isinstance(allowed, list)
            or any(not isinstance(item, str) for item in allowed)):
        raise ValueError("invalid trusted workload policy")
    if digest not in allowed:
        raise PermissionError("workload script digest is not approved for this worker")


class WorkloadLauncher:
    def __init__(self, workspace):
        requested = Path(workspace).absolute()
        if (os.getenv("WORKER_PROVIDER", "aws") != "local"
                and (requested != Path("/opt/job_workspace") or requested.is_symlink())):
            raise PermissionError("production workload workspace must be the fixed data directory")
        self.workspace = requested.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        if os.getenv("WORKER_PROVIDER", "aws") == "local":
            self.db_path = self.workspace / "launches.sqlite3"
        else:
            workspace_key = hashlib.sha256(str(self.workspace).encode()).hexdigest()
            self.db_path = Path("/var/lib/spot-worker/launches") / (workspace_key + ".sqlite3")

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS launches (operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT)")

    def _db(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def launch(self, specification):
        if specification.get("gpu") or specification.get("external_side_effects"):
            raise ValueError("unsupported workload capabilities")
        environment = specification.get("environment", {})
        forbidden = ("WORKLOAD_", "AWS_", "SPOT_", "PYTHON", "LD_", "DYLD_", "SUDO_")
        if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or key.startswith(forbidden)
            or key in {"WORKER_PROVIDER", "INSTANCE_ID", "WORKSPACE_ROOT",
                       "PATH", "HOME", "USER", "LOGNAME", "ENV", "BASH_ENV", "TMPDIR"}
            or not isinstance(value, str) or "\x00" in value
            for key, value in environment.items()
        ):
            raise ValueError("workload environment overrides a reserved worker setting")
        op = specification["operation_id"]
        staged_input = self.workspace / specification["script"]
        script = staged_input.resolve()
        if (staged_input.is_symlink() or not script.is_relative_to(self.workspace)
                or not script.is_file()):
            raise ValueError("workload script must be staged inside the workspace")
        digest = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
        if specification.get("script_digest") != digest:
            raise ValueError("workload artifact digest mismatch")
        local = os.getenv("WORKER_PROVIDER", "aws") == "local"
        if not local:
            if not hasattr(os, "geteuid") or os.geteuid() != 0:
                raise PermissionError("production workload launch requires root control")
            validate_trusted_digest(digest)
            script = stage_trusted_script(script, digest)
        self._init_db()
        request = json.dumps(specification, sort_keys=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request, result FROM launches WHERE operation_id=?", (op,)).fetchone()
            if row:
                if row[0] != request:
                    raise RuntimeError("launch operation request mismatch")
                if row[1] is None:
                    raise RuntimeError("launch outcome UNKNOWN; reconcile before repeating")
                return json.loads(row[1])
            db.execute("INSERT INTO launches VALUES (?, ?, NULL)", (op, request))
        # Never pass the root launcher's AWS credentials or Python loader state
        # to the unprivileged job process.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "WORKER_PROVIDER": "local" if local else "aws"}
        env.update({"WORKSPACE_ROOT": str(self.workspace), "WORKLOAD_JOB_ID": specification["job_id"],
                    "INSTANCE_ID": specification["instance_id"],
                    "WORKLOAD_EXECUTION_EPOCH": str(specification["execution_epoch"])})
        env["WORKLOAD_SCRIPT"] = str(script)
        env["WORKLOAD_ARGUMENTS"] = json.dumps(specification.get("arguments", []))
        env.update({str(k): str(v) for k, v in environment.items()})
        # File-backed standard streams avoid terminal/socket dependencies at dump.
        options = {}
        if not local:
            import pwd
            account = pwd.getpwnam("spotjob")
            if account.pw_uid == 0 or account.pw_gid == 0:
                raise PermissionError("spotjob must be unprivileged")
            env.update({"HOME": "/opt/job_workspace", "USER": "spotjob", "LOGNAME": "spotjob"})
            options = {"user": account.pw_uid, "group": account.pw_gid,
                       "extra_groups": []}
        log_fd = os.open(self.workspace / "workload.log",
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND
                         | getattr(os, "O_NOFOLLOW", 0), 0o640)
        log_stat = os.fstat(log_fd)
        if (not stat.S_ISREG(log_stat.st_mode)
                or (not local and (log_stat.st_uid != 0 or log_stat.st_mode & 0o022))):
            os.close(log_fd)
            raise PermissionError("workload log must be a root-owned regular file")
        with os.fdopen(log_fd, "ab") as log:
            process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("job_runner.py"))],
                                       cwd=self.workspace, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True, **options)
        result = {"pid": process.pid, "job_id": specification["job_id"],
                  "instance_id": specification["instance_id"], "execution_epoch": specification["execution_epoch"],
                  "operation_id": op, "launched_at": datetime.now(timezone.utc).isoformat()}
        if sys.platform == "linux":
            result["process_start"] = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        write_json(self.workspace / "execution.json", result)
        with self._db() as db:
            db.execute("UPDATE launches SET result=? WHERE operation_id=?", (json.dumps(result), op))
        return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    print(json.dumps(WorkloadLauncher(args.workspace).launch(json.loads(Path(args.spec).read_text()))))


if __name__ == "__main__":
    main()
