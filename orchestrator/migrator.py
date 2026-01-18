# orchestrator/migrator.py
from orchestrator.utils import SSHClient
from orchestrator.config_loader import load_runtime_config
from storage.job_registry import JobRegistry
from orchestrator.instance_manager import provision_instance
from orchestrator.utils import retry
import os


class Migrator:
    def __init__(self, registry: JobRegistry, checkpoint_bucket: str | None = None):
        self.registry = registry
        config = load_runtime_config()
        # Bucket can be provided explicitly, via env var, or config file
        self.checkpoint_bucket = checkpoint_bucket or os.getenv("CHECKPOINT_BUCKET") or config.get("checkpoint_bucket")
        if not self.checkpoint_bucket:
            raise RuntimeError("checkpoint_bucket is required (env CHECKPOINT_BUCKET or config/runtime.yaml)")
        self.runtime_config = config

    def migrate(
        self,
        job_id,
        target_region,
        target_ip=None,
        autoprovision=False,
        provision_overrides=None,
    ):
        job = self.registry.get(job_id)
        source_ip = job["public_ip"]
        pid = job["pid"]

        # ==========================================
        # STEP 1: FREEZE (SOURCE)
        # ==========================================
        source_ssh = SSHClient(source_ip)
        source_ssh.connect()

        try:
            self.registry.update(job_id, "CHECKPOINTING")
            retry(
                lambda: source_ssh.run_command(
                    f"sudo bash /opt/job_workspace/checkpoint/criu_wrapper.sh dump {pid}"
                ),
                retries=3,
                delay=5,
            )

            self.registry.update(job_id, "UPLOADING")
            retry(
                lambda: source_ssh.run_command(
                    f"python3 /opt/job_workspace/storage/s3_manager.py upload {job_id} "
                    f"--bucket {self.checkpoint_bucket}"
                ),
                retries=3,
                delay=5,
            )

            # Prevent split-brain
            source_ssh.run_command(f"sudo kill -9 {pid}")

        finally:
            source_ssh.close()

        # ==========================================
        # STEP 2: MOVE (INFRA)
        # ==========================================
        self.registry.update(job_id, "PROVISIONING")

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

        # ==========================================
        # STEP 3: THAW (TARGET)
        # ==========================================
        target_ssh = SSHClient(target_ip)
        target_ssh.connect()

        try:
            # Preflight on target
            self.registry.update(job_id, "VALIDATING")
            retry(lambda: target_ssh.run_command("criu --version"), retries=2, delay=3)
            retry(lambda: target_ssh.run_command("sudo criu check"), retries=2, delay=3)

            self.registry.update(job_id, "DOWNLOADING")
            retry(
                lambda: target_ssh.run_command(
                    f"python3 /opt/job_workspace/storage/s3_manager.py download {job_id} "
                    f"--bucket {self.checkpoint_bucket}"
                ),
                retries=3,
                delay=5,
            )

            self.registry.update(job_id, "RESTORING")
            retry(
                lambda: target_ssh.run_command(
                    "sudo bash /opt/job_workspace/checkpoint/criu_wrapper.sh restore"
                ),
                retries=3,
                delay=5,
            )

            self.registry.update(
                job_id,
                "RUNNING",
                region=target_region,
                public_ip=target_ip,
            )

        finally:
            target_ssh.close()
