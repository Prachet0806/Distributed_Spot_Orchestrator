# orchestrator/config_loader.py
import os
import yaml
from pathlib import Path

RUNTIME_CONFIG_PATH = Path("config/runtime.yaml")


def load_runtime_config():
    """
    Loads runtime configuration for orchestrator components.
    Priority:
      1) Environment variables
      2) config/runtime.yaml (if present)
    """
    cfg = {}

    # Load from file if it exists
    if RUNTIME_CONFIG_PATH.exists():
        with open(RUNTIME_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

    # Env vars take precedence
    checkpoint_bucket = os.getenv("CHECKPOINT_BUCKET") or cfg.get("checkpoint_bucket")
    source_region = os.getenv("SOURCE_REGION") or cfg.get("source_region")
    instance_type = os.getenv("INSTANCE_TYPE") or cfg.get("instance_type")
    ssh_key_name = os.getenv("SSH_KEY_NAME") or cfg.get("ssh_key_name")
    target_region = os.getenv("TARGET_REGION") or cfg.get("target_region")
    target_ami_id = os.getenv("TARGET_AMI_ID") or cfg.get("target_ami_id")
    target_security_group_id = os.getenv("TARGET_SECURITY_GROUP_ID") or cfg.get("target_security_group_id")
    max_spot_price = os.getenv("MAX_SPOT_PRICE") or cfg.get("max_spot_price")
    registry_backend = os.getenv("REGISTRY_BACKEND") or cfg.get("registry_backend")
    dynamodb_table = os.getenv("DYNAMO_TABLE") or cfg.get("dynamodb_table")
    dynamodb_region = os.getenv("DYNAMO_REGION") or cfg.get("dynamodb_region") or source_region
    auto_provision = os.getenv("AUTO_PROVISION")
    if auto_provision is None:
        auto_provision = cfg.get("auto_provision")
    else:
        auto_provision = str(auto_provision).lower() in ("1", "true", "yes")

    return {
        "checkpoint_bucket": checkpoint_bucket,
        "source_region": source_region,
        "instance_type": instance_type,
        "ssh_key_name": ssh_key_name,
        "target_region": target_region,
        "target_ami_id": target_ami_id,
        "target_security_group_id": target_security_group_id,
        "max_spot_price": max_spot_price,
        "registry_backend": registry_backend,
        "dynamodb_table": dynamodb_table,
        "dynamodb_region": dynamodb_region,
        "auto_provision": auto_provision,
        "raw": cfg,
    }

