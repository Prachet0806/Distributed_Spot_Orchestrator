"""Real local migration across isolated PID/mount namespaces and pinned SSH.

Requires Linux root, unshare, CRIU and sshd. No AWS clients are constructed.
The filesystem object service proves bytes/integrity, not AWS KMS or IAM.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="lab-results")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("real Linux lab requires Linux root")
    run(["criu", "check"], timeout=60)
    from orchestrator.utils import SSHClient
    from worker.transport import SSHWorkerTransport
    from worker.workload_launcher import WorkloadLauncher
    from orchestrator.worker_execution import WorkerExecution, CheckpointDataPath
    from orchestrator.criu_handlers import make_dump_handler_for, make_restore_handler_for
    from orchestrator.checkpoint_manager import CheckpointManager
    from orchestrator.transfer_manager import TransferManager
    from orchestrator.provisioner import ProvisionResult
    from orchestrator.migration_coordinator import MigrationCoordinator
    from orchestrator.migration_planner import MigrationPlanner
    from orchestrator.workload_estimator import WorkloadEstimator, EstimatorConfig
    from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
    from orchestrator.validator import Validator
    from storage.memory_registry import MemoryRegistry
    from storage.plan_store import PlanStore
    from storage.checkpoint_store import CheckpointStore
    from storage.audit_store import AuditStore
    from storage.operation_store import OperationStore
    from storage.local_artifact_client import LocalArtifactClient
    from storage.s3_manager import S3Manager
    from orchestrator.main import _admit_plan_to_store
    from scripts.install_worker_control import install_control

    evidence = {"kernel": run(["uname", "-r"]), "criu": run(["criu", "--version"]),
                "aws": "NOT_RUN", "worker_isolation": "PID and mount namespaces", "checks": []}
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    processes, transports, logs = [], {}, []
    with tempfile.TemporaryDirectory(prefix="spot-linux-lab-") as temp:
        lab = Path(temp)
        control_tree = lab / "control"
        evidence["control_digest"] = install_control(control_tree)
        key = lab / "client-key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        auth = lab / "authorized_keys"
        shutil.copyfile(str(key) + ".pub", auth)
        auth.chmod(0o600)
        known = lab / "known_hosts"
        known.write_text("")
        Path("/run/sshd").mkdir(exist_ok=True)
        Path("/opt/job_workspace").mkdir(exist_ok=True)
        Path("/opt/spot-worker-control").mkdir(exist_ok=True)
        Path("/var/lib/spot-worker").mkdir(parents=True, exist_ok=True)
        try:
            for name in ("source", "target"):
                directory = lab / name
                workspace = directory / "workspace"
                workspace.mkdir(parents=True)
                control_state = directory / "control-state"
                control_state.mkdir(mode=0o700)
                (control_state / "identity.json").write_text('{"provider":"local"}')
                (control_state / "identity.json").chmod(0o600)
                for package in ("worker", "checkpoint"):
                    shutil.copytree(ROOT / package, workspace / package, ignore=shutil.ignore_patterns("__pycache__"))
                wrapper = workspace / "checkpoint" / "criu_wrapper.sh"
                wrapper.write_bytes(wrapper.read_bytes().replace(b"\r\n", b"\n"))
                shutil.copyfile(ROOT / "common_constants.py", workspace / "common_constants.py")
                (workspace / "checkpoints").mkdir()
                hostkey = directory / "hostkey"
                run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)])
                listen = port()
                with known.open("a") as out:
                    out.write(f"[127.0.0.1]:{listen} " + Path(str(hostkey) + ".pub").read_text())
                config = directory / "sshd_config"
                config.write_text(f"Port {listen}\nListenAddress 127.0.0.1\nHostKey {hostkey}\n"
                    f"PidFile {directory / 'sshd.pid'}\nAuthorizedKeysFile {auth}\nStrictModes no\n"
                    "PasswordAuthentication no\nPermitRootLogin prohibit-password\nUsePAM no\n"
                    "Subsystem sftp internal-sftp\n")
                boot = directory / "boot.sh"
                boot.write_text("#!/bin/bash\nset -e\nmount --make-rprivate /\n"
                    f"mount --bind {workspace} /opt/job_workspace\n"
                    f"mount --bind {control_tree} /opt/spot-worker-control\n"
                    f"mount --bind {control_state} /var/lib/spot-worker\n"
                    f"exec /usr/sbin/sshd -D -e -f {config}\n")
                log = (directory / "server.log").open("w")
                logs.append(log)
                process = subprocess.Popen(["unshare", "--mount", "--pid", "--fork", "--mount-proc", "bash", str(boot)],
                                           stdout=log, stderr=log, start_new_session=True)
                processes.append(process)
                client = SSHClient("127.0.0.1", user="root", key_path=str(key), port=listen,
                                   known_hosts_path=str(known), strict=True, timeout=60)
                transport = SSHWorkerTransport(name, ssh_client=client, strict_host_keys=True)
                last_error = None
                for attempt in range(60):
                    if process.poll() is not None:
                        raise RuntimeError((directory / "server.log").read_text())
                    try:
                        response = transport.run_command("true")
                        last_error = response
                        if response["returncode"] == 0:
                            break
                    except Exception as exc:
                        last_error = repr(exc)
                    time.sleep(.1)
                else:
                    raise RuntimeError(f"namespace SSH worker did not start: {last_error}; "
                                       f"server: {(directory / 'server.log').read_text()}")
                control_check = transport.run_command(
                    "sudo -n python3 -I /opt/spot-worker-control/control_entry.py --self-check")
                if (control_check["returncode"] != 0
                        or control_check["stdout"].strip() != evidence["control_digest"]):
                    raise RuntimeError(f"root-owned control integrity failed on {name}: {control_check}")
                evidence["checks"].append({f"{name}_control_integrity": True})
                transports[name] = transport

            source = transports["source"]
            specification = {"operation_id": "launch-local", "job_id": "fixture", "instance_id": "source",
                             "execution_epoch": 0, "script": "workload.py", "environment": {}}
            script = lab / "workload.py"
            script.write_text("from worker.jobs.fixture import run\nrun(total_ticks=100000, persist_every=1, "
                              "sleep_per_tick=.02, progress_path='/opt/job_workspace/progress.json')\n")
            source.send_file(script, "/opt/job_workspace/workload.py")
            import hashlib
            specification["script_digest"] = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
            spec = lab / "launch.json"
            spec.write_text(json.dumps(specification))
            source.send_file(spec, "/opt/job_workspace/launch.json")
            result = source.run_command("cd /opt/job_workspace && WORKER_PROVIDER=local python3 -I "
                                        "/opt/spot-worker-control/control_entry.py launch "
                                        "--workspace /opt/job_workspace --spec /opt/job_workspace/launch.json")
            if result["returncode"] != 0:
                raise RuntimeError(result)
            identity = json.loads(result["stdout"])
            time.sleep(.5)
            registry = MemoryRegistry()
            registry.create("fixture", state="RUNNING", region="source", public_ip="source",
                            instance_id="source", pid=identity["pid"], process_start=identity["process_start"])
            plans, checkpoints, audit, operations = PlanStore(), CheckpointStore(), AuditStore(), OperationStore()
            storage = S3Manager("lab", s3_client=LocalArtifactClient(lab / "objects"))
            checkpoint = CheckpointManager(storage=storage, checkpoint_store=checkpoints,
                dump_handler=make_dump_handler_for(transports.__getitem__),
                restore_handler=make_restore_handler_for(transports.__getitem__))
            worker = WorkerExecution(transports.__getitem__)
            class Provisioner:
                def provision_from_pool(self, definition, **kw):
                    return ProvisionResult("target", "target", "", "running", region="target")
                def get_operation_status(self, op):
                    return {"state": "UNKNOWN"}
                def terminate(self, identity):
                    raise RuntimeError("active lab worker termination is prohibited")
            class Cleanup:
                def cleanup_migration(self, plan, operations, **kwargs):
                    if kwargs.get("outcome") == "SUCCESS":
                        return []
                    return []
            contexts = {}
            def snapshot(job, phase):
                host = "source" if phase == "pre" else "target"
                response = transports[host].run_command("cat /opt/job_workspace/progress.json")
                return {"progress_delta": json.loads(response["stdout"])["tick"]}
            def contained_probe(**kwargs):
                ctx = next(iter(contexts.values()))
                result = transports["target"].run_command(f"ps -o stat= -p {ctx['target_pid']}")
                stopped = result["returncode"] == 0 and result["stdout"].strip().startswith("T")
                evidence["checks"].append({"target_stopped_before_activation": stopped})
                return {"passed": stopped}
            decision = PolicyDecision(Decision.MIGRATE, MigrationRegime.ARBITRAGE, "local fixture", "target", .9, {})
            estimate = WorkloadEstimator(EstimatorConfig()).estimate("fixture", 0, "long", None)
            plan = MigrationPlanner().create_plan("fixture", 0, MigrationRegime.ARBITRAGE, decision,
                "source", "target", estimate, source_identity=registry.get("fixture"))
            _admit_plan_to_store(plans, plan)
            def fence(action):
                result = source.run_command(f"cd /opt/job_workspace && python3 -I "
                    f"/opt/spot-worker-control/control_entry.py process-control {action} "
                    f"--pid {identity['pid']} --start {identity['process_start']}")
                if result["returncode"] != 0:
                    raise RuntimeError(result)
                return json.loads(result["stdout"])
            coordinator = MigrationCoordinator(registry, Provisioner(), checkpoint, TransferManager(storage),
                Validator(default_tolerance={"version": "fixture-v2", "metrics": {"progress_delta": {"value": 0}}},
                          infra_probe=contained_probe, app_probe=contained_probe), Cleanup(),
                plan_store=plans, checkpoint_store=checkpoints, audit_store=audit, operation_store=operations,
                pool_definition_provider=lambda _: {"pool_id": "target", "provider": "local"},
                checkpoint_data_path=CheckpointDataPath(transports.__getitem__, storage, checkpoints),
                snapshot_provider=snapshot, execution_context_sink=lambda ctx: contexts.update({ctx["migration_id"]: ctx}),
                fence_invalidate=lambda _: True, fence_terminate=lambda _: fence("terminate"),
                fence_verify=lambda _: fence("verify")["process_dead"], activation_handler=worker.activate,
                rollback_source=worker.resume_source)
            start = time.monotonic()
            outcome = coordinator.execute_plan(plan)
            if outcome.current_state.value != "SUCCESS":
                source_status = source.run_command(
                    f"cd /opt/job_workspace && python3 -I "
                    f"/opt/spot-worker-control/control_entry.py process-control inspect "
                    f"--pid {identity['pid']} --start {identity['process_start']}")
                restore_log = transports["target"].run_command(
                    "tail -n 80 /opt/job_workspace/checkpoints/*/restore.log", timeout=10)
                evidence.update(outcome=outcome.current_state.value,
                    failed_step=outcome.operations[-1].step_name if outcome.operations else None,
                    failure=outcome.operations[-1].error if outcome.operations else "no operation receipt",
                    registry_state=registry.get("fixture")["state"],
                    source_after_failure=(json.loads(source_status["stdout"])
                                          if source_status["returncode"] == 0 else source_status),
                    restore_log_tail=(restore_log["stdout"][-6000:]
                                      if restore_log["returncode"] == 0 else None))
                raise RuntimeError(f"migration stopped in {outcome.current_state.value}: "
                                   f"{evidence['failed_step']}: {evidence['failure']}")
            before = snapshot("fixture", "post")["progress_delta"]
            time.sleep(.3)
            after = snapshot("fixture", "post")["progress_delta"]
            assert after > before
            assert fence("verify")["process_dead"]
            assert registry.get("fixture")["public_ip"] == "target"
            assert registry.get("fixture")["execution_epoch"] == 1
            evidence.update(outcome="SUCCESS", duration_seconds=time.monotonic() - start,
                            source_dead=True, target_progress_before=before, target_progress_after=after,
                            checkpoint_bytes=checkpoints.get(outcome.checkpoint_id)["size_bytes"],
                            audit_events=[r["event_type"] for r in audit.list_by_job("fixture")])
        finally:
            for transport in transports.values():
                transport.close()
            for process in processes:
                if process.poll() is None and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            for log in logs:
                log.close()
            (output / "linux-evidence.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
