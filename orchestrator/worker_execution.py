"""Explicit staging and activation primitives over authenticated worker transport."""
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import time
from datetime import datetime, timezone


class WorkerExecution:
    def __init__(self, transport_for, workspace="/opt/job_workspace", guard=None,
                 controller_channel_factory=None, lease_proof=None):
        self.transport_for = transport_for
        self.workspace = workspace
        self.guard = guard
        self.controller_channel_factory = controller_channel_factory
        self.lease_proof = lease_proof

    def launch(self, job):
        if self.guard:
            self.guard()
        contract = job["workload_contract"]
        script = Path(contract["script"]).resolve()
        if not script.is_file() or script.suffix != ".py":
            raise ValueError("supported workload must be a staged Python script")
        operation_id = job["launch_operation_id"]
        specification = {"operation_id": operation_id, "job_id": job["job_id"],
                         "instance_id": job["instance_id"], "execution_epoch": job["execution_epoch"],
                         "script": "workload.py", "script_digest": "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest(),
                         "arguments": contract.get("arguments", []), "environment": contract.get("environment", {}),
                         "gpu": contract.get("resources", {}).get("gpu", False),
                         "external_side_effects": contract.get("external_side_effects", False)}
        transport = self.transport_for(job["public_ip"])
        transport.send_file(script, self.workspace + "/workload.py")
        with tempfile.TemporaryDirectory(prefix="workload-spec-") as temp:
            path = Path(temp) / "launch.json"
            path.write_text(json.dumps(specification))
            remote = self.workspace + "/launch.json"
            transport.send_file(path, remote)
        result = transport.run_command(
            f"cd {shlex.quote(self.workspace)} && sudo -n python3 -I "
            f"/opt/spot-worker-control/control_entry.py launch "
            f"--workspace {shlex.quote(self.workspace)} --spec {shlex.quote(remote)}", timeout=60)
        if result["returncode"] != 0:
            raise RuntimeError("workload launch failed: " + result.get("stderr", ""))
        return json.loads(result["stdout"])

    def activate(self, state):
        if self.guard:
            self.guard()
        started = datetime.now(timezone.utc)
        if self.controller_channel_factory is not None:
            command = {"operation_id": f"{state.plan.plan_id}:activate", "action": "activate",
                       "instance_id": state.target_instance_id, "job_id": state.plan.job_id,
                       "expected_execution_epoch": state.expected_epoch,
                       "pid": int(state.target_pid), "process_start": state.target_process_start,
                       "lease": self.lease_proof()}
            result = self.controller_channel_factory(state.target_host)(command)
            if not result.get("accepted"):
                raise RuntimeError("worker refused target activation")
            self._confirm_heartbeat(state, started)
            return result
        transport = self.transport_for(state.target_host)
        identity = {"job_id": state.plan.job_id, "instance_id": state.target_instance_id,
                    "execution_epoch": state.expected_epoch, "pid": state.target_pid, "process_start": state.target_process_start}
        with tempfile.TemporaryDirectory(prefix="activation-") as temp:
            path = Path(temp) / "execution.json"
            path.write_text(json.dumps(identity))
            remote = self.workspace + "/execution.pending.json"
            transport.send_file(path, remote)
        result = transport.run_command(
            f"sudo -n mv {shlex.quote(remote)} {shlex.quote(self.workspace + '/execution.json')} && "
            f"cd {shlex.quote(self.workspace)} && sudo -n python3 -I "
            f"/opt/spot-worker-control/control_entry.py process-control activate "
            f"--pid {int(state.target_pid)} --start {shlex.quote(state.target_process_start)}", timeout=30)
        if result["returncode"] != 0:
            raise RuntimeError("activation outcome uncertain")
        self._confirm_heartbeat(state, started)

    def _confirm_heartbeat(self, state, started):
        transport = self.transport_for(state.target_host)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.guard:
                self.guard()
            response = transport.run_command(
                f"cat {shlex.quote(self.workspace + '/heartbeat.json')}", timeout=10)
            if response["returncode"] == 0:
                try:
                    report = json.loads(response["stdout"])
                    observed = datetime.fromisoformat(report["observed_at"].replace("Z", "+00:00"))
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    if (report.get("job_id") == state.plan.job_id
                            and report.get("instance_id") == state.target_instance_id
                            and int(report.get("execution_epoch", -1)) == state.expected_epoch
                            and int(report.get("pid", -1)) == int(state.target_pid)
                            and observed >= started):
                        return
                except (ValueError, TypeError, KeyError):
                    pass
            time.sleep(1)
        raise RuntimeError("activated target did not emit a fresh identity-matched heartbeat")

    def resume_source(self, plan):
        if self.guard:
            self.guard()
        transport = self.transport_for(plan.source_host)
        prefix = (f"cd {shlex.quote(self.workspace)} && sudo -n python3 -I "
                  f"/opt/spot-worker-control/control_entry.py process-control ")
        identity = (f"--pid {int(plan.pid)} "
                    f"--start {shlex.quote(plan.source_process_start)}")
        response = transport.run_command(
            prefix + "activate " + identity, timeout=30)
        if response["returncode"] != 0:
            raise RuntimeError("source rollback identity/transport uncertain")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = transport.run_command(prefix + "inspect " + identity, timeout=10)
            if observed["returncode"] == 0:
                try:
                    status = json.loads(observed["stdout"])
                    if status.get("same_process") and not status.get("contained"):
                        return
                except (ValueError, TypeError):
                    pass
            time.sleep(.2)
        raise RuntimeError("source resume was not verified on the original process")


class CheckpointDataPath:
    def __init__(self, transport_for, storage, checkpoint_store, workspace="/opt/job_workspace"):
        self.transport_for, self.storage = transport_for, storage
        self.checkpoint_store, self.workspace = checkpoint_store, workspace

    def download_to_worker(self, checkpoint_id, host, operation_id=None):
        doc = self.checkpoint_store.get(checkpoint_id)
        with tempfile.TemporaryDirectory(prefix="checkpoint-stage-") as temp:
            local = Path(temp) / "images"
            manifest = self.storage.download(checkpoint_id, dst=str(local), expected_checksum=doc["checksum"])
            transport = self.transport_for(host)
            parent = self.workspace + "/checkpoints"
            result = transport.run_command(f"mkdir -p {shlex.quote(parent)}", timeout=30)
            if result["returncode"] != 0:
                raise RuntimeError("target checkpoint staging failed")
            transport.send_directory(local, parent + "/" + checkpoint_id)
            manifest_file = Path(temp) / "manifest.json"
            manifest_file.write_text(json.dumps(manifest))
            remote_manifest = parent + "/" + checkpoint_id + "/manifest.json"
            transport.send_file(manifest_file, remote_manifest)
            check = transport.run_command(
                f"cd {shlex.quote(self.workspace)} && python3 -I "
                f"/opt/spot-worker-control/control_entry.py verify-artifact "
                f"--directory {shlex.quote(parent + '/' + checkpoint_id)} --manifest {shlex.quote(remote_manifest)} "
                f"--operation-id {shlex.quote(operation_id or checkpoint_id)}", timeout=60)
            if check["returncode"] != 0:
                raise RuntimeError("target artifact integrity verification failed")
        return {"checkpoint_id": checkpoint_id, "bytes_transferred": doc["size_bytes"], "status": "COMPLETED"}
