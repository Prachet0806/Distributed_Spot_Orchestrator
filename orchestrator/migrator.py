# orchestrator/migrator.py
"""V1 LEGACY FROZEN — do not extend.

Use the V2 path instead: MigrationPlanner -> MigrationCoordinator
(`orchestrator/migration_planner.py`, `orchestrator/migration_coordinator.py`).

This module is kept runnable behind `--engine v1` / direct import only
for backward compatibility until the AWS E2E + soak gates pass
(ADR-024). No new features, states, or callers should be added here.
"""
import warnings

warnings.warn(
    "orchestrator.migrator.Migrator is V1 legacy (frozen). Use V2 "
    "MigrationPlanner/MigrationCoordinator.",
    DeprecationWarning,
    stacklevel=2,
)
from orchestrator.utils import SSHClient
from orchestrator.config_loader import load_runtime_config
from storage.job_registry import JobRegistry
from orchestrator.instance_manager import provision_instance
from orchestrator.utils import retry
from storage.job_states import JobState
from orchestrator.metrics import get_metrics
from orchestrator.constants import (
    CHECKPOINT_DIR,
    DEFAULT_RETRIES,
    INITIAL_RETRY_DELAY,
    SSH_TIMEOUT_CRIU_DUMP,
    SSH_TIMEOUT_CRIU_RESTORE,
)
import os
import logging
import time
import uuid


class Migrator:
    def __init__(self, registry: JobRegistry, checkpoint_bucket: str | None = None):
        self.registry = registry
        config = load_runtime_config()
        # Bucket can be provided explicitly, via env var, or config file
        self.checkpoint_bucket = checkpoint_bucket or os.getenv("CHECKPOINT_BUCKET") or config.get("checkpoint_bucket")
        if not self.checkpoint_bucket:
            raise RuntimeError("checkpoint_bucket is required (env CHECKPOINT_BUCKET or config/runtime.yaml)")
        self.runtime_config = config
        self.log = logging.getLogger("orchestrator.migrator")
        self.metrics = get_metrics()

        self._step_order = [
            "CHECKPOINTING",
            "UPLOADING",
            "PROVISIONING",
            "VALIDATING",
            "DOWNLOADING",
            "RESTORING",
        ]
        self._step_index = {name: idx for idx, name in enumerate(self._step_order)}

    def migrate(
        self,
        job_id,
        target_region,
        target_ip=None,
        autoprovision=False,
        provision_overrides=None,
        reason=None,
    ):
        job = self.registry.get(job_id)
        source_ip = job["public_ip"]
        pid = job["pid"]
        migration_id = job.get("migration_id") or str(uuid.uuid4())
        source_region = job.get("region")
        start_time = time.time()

        def log_event(step, level="info", **extra):
            payload = {
                "job_id": job_id,
                "migration_id": migration_id,
                "step": step,
                "source_region": source_region,
                "target_region": target_region,
                "reason": reason,
                **extra,
            }
            msg = str(payload)
            getattr(self.log, level)(msg)

        def step_done(step_name):
            last = job.get("last_completed_step")
            if not last:
                return False
            return self._step_index.get(last, -1) >= self._step_index.get(step_name, -1)

        try:
            # ==========================================
            # STEP 1: FREEZE (SOURCE)
            # ==========================================
            source_ssh = SSHClient(source_ip)
            source_ssh.connect()

            try:
                if not step_done("CHECKPOINTING"):
                    self.registry.update(
                        job_id,
                        JobState.CHECKPOINTING.value,
                        migration_id=migration_id,
                        reason=reason,
                    )
                    log_event("CHECKPOINTING")
                    retry(
                        lambda: source_ssh.run_command(
                            f"sudo bash /opt/job_workspace/checkpoint/criu_wrapper.sh dump {pid}",
                            timeout_override=SSH_TIMEOUT_CRIU_DUMP
                        ),
                        retries=DEFAULT_RETRIES,
                        initial_delay=INITIAL_RETRY_DELAY,
                    )
                    self.registry.update(job_id, JobState.CHECKPOINTING.value, last_completed_step="CHECKPOINTING")
                    job["last_completed_step"] = "CHECKPOINTING"

                if not step_done("UPLOADING"):
                    self.registry.update(job_id, JobState.UPLOADING.value)
                    log_event("UPLOADING")
                    size_result = source_ssh.run_command(
                        "du -sb /opt/job_workspace/checkpoint",
                        check=False,
                    )
                    try:
                        size_bytes = int(size_result.stdout.strip().split()[0])
                        self.registry.update(job_id, JobState.UPLOADING.value, checkpoint_size_bytes=size_bytes)
                        self.metrics.set_gauge("checkpoint_size_bytes", size_bytes)
                    except Exception:
                        pass
                    retry(
                        lambda: source_ssh.run_command(
                            f"python3 /opt/job_workspace/storage/s3_manager.py upload {job_id} "
                            f"--bucket {self.checkpoint_bucket}"
                        ),
                        retries=DEFAULT_RETRIES,
                        initial_delay=INITIAL_RETRY_DELAY,
                    )
                    self.registry.update(job_id, JobState.UPLOADING.value, last_completed_step="UPLOADING")
                    job["last_completed_step"] = "UPLOADING"

                # Prevent split-brain
                source_instance_id = _get_instance_id(source_ssh)
                self.registry.update(job_id, JobState.UPLOADING.value, source_instance_id=source_instance_id)
                job["source_instance_id"] = source_instance_id  # Update local job dict
                source_ssh.run_command(f"sudo kill -9 {pid}")
                ps_check = source_ssh.run_command(f"ps -p {pid}", check=False)
                if ps_check.returncode == 0:
                    raise RuntimeError(f"Source PID {pid} still running after kill")
                self.registry.update(job_id, JobState.UPLOADING.value, source_pid_terminated=True)

            finally:
                source_ssh.close()

            # ==========================================
            # STEP 2: MOVE (INFRA)
            # ==========================================
            if not step_done("PROVISIONING"):
                self.registry.update(job_id, JobState.PROVISIONING.value)
                log_event("PROVISIONING")

                if not target_ip:
                    if autoprovision:
                        cfg = self.runtime_config.get("raw", {})
                        ami_id = (provision_overrides or {}).get("ami_id") or cfg.get("target_ami_id")
                        sg_id = (provision_overrides or {}).get("security_group_id") or cfg.get("target_security_group_id")
                        key_name = (provision_overrides or {}).get("ssh_key_name") or self.runtime_config.get("ssh_key_name")
                        inst_type = (provision_overrides or {}).get("instance_type") or self.runtime_config.get("instance_type")
                        max_price = (provision_overrides or {}).get("max_spot_price") or cfg.get("max_spot_price")
                        if not all([ami_id, sg_id, key_name, inst_type]):
                            raise RuntimeError("Auto-provision missing required parameters (ami_id, security_group_id, ssh_key_name, instance_type)")
                        _, target_ip, _ = provision_instance(
                            region=target_region,
                            ami_id=ami_id,
                            security_group_id=sg_id,
                            key_name=key_name,
                            instance_type=inst_type,
                            max_spot_price=max_price,
                        )
                        print(f"✅ Provisioned target in {target_region}: {target_ip}")
                    else:
                        print(f"⚠️ MANUAL STEP: Provision worker in {target_region}")
                        target_ip = input(f"Enter IP of new worker in {target_region}: ")

                self.registry.update(job_id, JobState.PROVISIONING.value, last_completed_step="PROVISIONING")
                job["last_completed_step"] = "PROVISIONING"

            # ==========================================
            # STEP 3: THAW (TARGET)
            # ==========================================
            target_ssh = SSHClient(target_ip)
            target_ssh.connect()

            try:
                target_instance_id = _get_instance_id(target_ssh)
                if target_instance_id and job.get("source_instance_id") == target_instance_id:
                    raise RuntimeError("Split-brain detected: source and target instance IDs match")
                self.registry.update(job_id, JobState.PROVISIONING.value, target_instance_id=target_instance_id)

                # Preflight on target
                if not step_done("VALIDATING"):
                    self.registry.update(job_id, JobState.VALIDATING.value)
                    log_event("VALIDATING")
                    retry(lambda: target_ssh.run_command("criu --version"), retries=2, initial_delay=3)
                    try:
                        retry(lambda: target_ssh.run_command("sudo criu check"), retries=2, initial_delay=3)
                    except Exception as exc:
                        log_event("VALIDATING", level="warning", alert="criu_check_failed", error=str(exc))
                        raise
                    self.registry.update(job_id, JobState.VALIDATING.value, last_completed_step="VALIDATING")
                    job["last_completed_step"] = "VALIDATING"

                if not step_done("DOWNLOADING"):
                    self.registry.update(job_id, JobState.DOWNLOADING.value)
                    log_event("DOWNLOADING")
                    retry(
                        lambda: target_ssh.run_command(
                            f"python3 /opt/job_workspace/storage/s3_manager.py download {job_id} "
                            f"--bucket {self.checkpoint_bucket}"
                        ),
                        retries=DEFAULT_RETRIES,
                        initial_delay=INITIAL_RETRY_DELAY,
                    )
                    self.registry.update(job_id, JobState.DOWNLOADING.value, last_completed_step="DOWNLOADING")
                    job["last_completed_step"] = "DOWNLOADING"

                if not step_done("RESTORING"):
                    self.registry.update(job_id, JobState.RESTORING.value)
                    log_event("RESTORING")
                    retry(
                        lambda: target_ssh.run_command(
                            "sudo bash /opt/job_workspace/checkpoint/criu_wrapper.sh restore",
                            timeout_override=SSH_TIMEOUT_CRIU_RESTORE
                        ),
                        retries=DEFAULT_RETRIES,
                        initial_delay=INITIAL_RETRY_DELAY,
                    )
                    self.registry.update(job_id, JobState.RESTORING.value, last_completed_step="RESTORING")
                    job["last_completed_step"] = "RESTORING"

                self.registry.update(
                    job_id,
                    JobState.RUNNING.value,
                    region=target_region,
                    public_ip=target_ip,
                )
                log_event("RUNNING")

            finally:
                target_ssh.close()

            duration = time.time() - start_time
            self.metrics.inc("migration_success_total")
            self.metrics.observe("migration_duration_seconds", duration)
        except Exception as exc:
            log_event("FAILED", level="error", error=str(exc))
            self.metrics.inc("migration_failure_total")
            if self.metrics.get_counter("migration_failure_total") >= 3:
                log_event("FAILED", level="warning", alert="migration_failures_exceeded")
            # Attempt rollback on migration failure
            try:
                self.rollback(job_id, reason=str(exc))
            except Exception:
                pass
            self.metrics.observe("migration_duration_seconds", time.time() - start_time)
            try:
                self.registry.update(job_id, JobState.FAILED.value, last_error=str(exc))
            except Exception:
                pass
            raise


def rollback(self, job_id, reason="migration_failed"):
        """Attempt to roll back a failed migration.
        
        Rolls back completed steps in reverse order:
        - RESTORING: target process not running, can terminate instance
        - DOWNLOADING: checkpoint already in S3, no cleanup needed
        - VALIDATING: no persistent resources created
        - PROVISIONING: terminate target instance if it was provisioned
        - UPLOADING: S3 artifacts can be cleaned up
        - CHECKPOINTING: no-op (checkpoint is on source)
        """
        job = self.registry.get(job_id)
        last_step = job.get("last_completed_step")
        source_ip = job.get("public_ip")
        
        self.log.warning("Rolling back job %s (reason: %s)", job_id, reason)
        
        # Roll back in reverse order from last completed step
        if last_step == "RESTORING" or not last_step:
            # Target may have the process running - try to clean up
            if source_ip:
                try:
                    ssh = SSHClient(source_ip)
                    ssh.connect()
                    ssh.run_command(f"sudo pkill -f 'python3 /opt/job_workspace/jobs/monte_carlo.py' 2>/dev/null || true")
                    ssh.close()
                except Exception:
                    pass
            # Update state to FAILED
            self.registry.update(job_id, JobState.FAILED.value, last_error=reason)
        
        elif last_step == "DOWNLOADING":
            # Checkpoint downloaded but not restored - S3 artifacts remain, no action needed
            self.registry.update(job_id, JobState.FAILED.value, last_error=reason)
        
        elif last_step == "VALIDATING":
            # Validation didn't create persistent resources
            self.registry.update(job_id, JobState.FAILED.value, last_error=reason)
        
        elif last_step == "PROVISIONING":
            # Target was provisioned - try to terminate it
            target_ip = job.get("public_ip")
            target_instance_id = job.get("target_instance_id")
            if target_ip and target_instance_id:
                try:
                    ssh = SSHClient(target_ip)
                    ssh.connect()
                    ssh.run_command(f"sudo shutdown -h now 2>/dev/null || true")
                    ssh.run_command(f"sudo aws ec2 terminate-instances --instance-ids {target_instance_id} 2>/dev/null || true")
                    ssh.close()
                except Exception:
                    pass
            self.registry.update(job_id, JobState.FAILED.value, last_error=reason)
        
        elif last_step == "UPLOADING":
            # Upload was in progress - S3 artifacts remain, mark as failed
            self.registry.update(job_id, JobState.FAILED.value, last_error=reason)
        
        else:
            # CHECKPOINTING or earlier - no rollback needed
            self.registry.update(job_id, JobState.FAILED.value, last_error=reason)


def _get_instance_id(ssh_client: SSHClient):
    command = (
        "TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' "
        "-H 'X-aws-ec2-metadata-token-ttl-seconds: 21600'); "
        "curl -s -H \"X-aws-ec2-metadata-token: $TOKEN\" "
        "http://169.254.169.254/latest/meta-data/instance-id"
    )
    result = ssh_client.run_command(command, check=False)
    instance_id = result.stdout.strip()
    return instance_id or None
