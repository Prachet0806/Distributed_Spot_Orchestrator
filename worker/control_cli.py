"""Authenticated SSH endpoint with independent lease and process checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from worker.controller import WorkerController
from worker.command_journal import CommandJournal
from worker.process_control import control
from worker.process_control import process_identity
from worker.atomic_io import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True, help="JSON command envelope")
    parser.add_argument("--identity", default="/var/lib/spot-worker/identity.json")
    args = parser.parse_args()
    boot = json.loads(Path(args.identity).read_text())
    command = json.loads(args.command)
    def lease_valid(cmd):
        if boot.get("provider") == "local":
            lease = json.loads(Path(boot["lease_file"]).read_text())
        else:
            import boto3
            lease = boto3.resource("dynamodb", region_name=boot["lease_region"]).Table(
                boot["lease_table"]).get_item(Key={"lease_id": "cpr-primary"}, ConsistentRead=True).get("Item")
        if not lease:
            return False
        expiry = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
        proof = cmd.get("lease", {})
        return (expiry > datetime.now(timezone.utc) and lease["owner"] == proof.get("owner")
                and int(lease["generation"]) == proof.get("generation"))

    class Executor:
        def terminate(self, pid):
            return control(pid, command.get("process_start"), "terminate")

        def verify(self, pid):
            return control(pid, command.get("process_start"), "verify")["process_dead"]

        def preflight(self):
            result = subprocess.run(["criu", "check"], capture_output=True, text=True, timeout=30)
            return {"passed": result.returncode == 0, "detail": result.stdout + result.stderr}

        def activate(self, pid, expected_epoch):
            observed = process_identity(pid)
            if (observed is None or observed["process_start"] != str(command["process_start"])
                    or not observed["state"].startswith(("T", "t"))):
                raise RuntimeError("target is not the contained process")
            workspace = Path("/opt/job_workspace")
            identity = {"job_id": command["job_id"], "instance_id": boot["instance_id"],
                        "execution_epoch": expected_epoch, "pid": pid,
                        "process_start": command["process_start"]}
            write_json(workspace / "execution.json", identity)
            owner = workspace.stat()
            os.chown(workspace / "execution.json", owner.st_uid, owner.st_gid)
            boot["admission_epoch"] = expected_epoch
            write_json(args.identity, boot)
            os.chmod(args.identity, 0o600)
            return control(pid, command["process_start"], "activate")

    controller = WorkerController(instance_id=boot["instance_id"], admission_epoch=boot["admission_epoch"],
        executor=Executor(), journal=CommandJournal(Path(args.identity).with_name("commands.sqlite3")),
        lease_validator=lease_valid)
    print(json.dumps(controller.handle_command(command)))


if __name__ == "__main__":
    main()
