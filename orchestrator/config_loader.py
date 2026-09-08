# orchestrator/config_loader.py
import os
import yaml
import time
from pathlib import Path
from orchestrator.constants import CONFIG_CACHE_TTL

RUNTIME_CONFIG_PATH = Path("config/runtime.yaml")
V2_BASELINE_PATH = Path("config/v2_baseline.yaml")

# Module-level cache
_CONFIG_CACHE = {"data": None, "mtime": 0, "cached_at": 0}

REQUIRED_CONFIG_KEYS = [
    "checkpoint_bucket",
    "source_region",
    "instance_type",
    "ssh_key_name",
    "target_region",
    "registry_backend",
    "dynamodb_table",
]


def _validate_config(result):
    """Validate that required config keys are present, raising SystemExit if missing."""
    missing = [k for k in REQUIRED_CONFIG_KEYS if not result.get(k)]
    if missing:
        raise SystemExit(f"Missing required config keys: {', '.join(missing)}")
    return result


def load_runtime_config(force_reload=False):
    """
    Loads runtime configuration for orchestrator components with caching.
    
    Priority:
      1) Environment variables
      2) config/runtime.yaml (if present)
    
    Args:
        force_reload: If True, bypass cache and reload from disk
    
    Returns:
        Dict with runtime configuration
    """
    global _CONFIG_CACHE
    
    now = time.time()
    
    # Check cache validity
    if not force_reload and _CONFIG_CACHE["data"]:
        # Check if cache is still fresh
        cache_age = now - _CONFIG_CACHE["cached_at"]
        
        if cache_age < CONFIG_CACHE_TTL:
            # Check if file was modified
            if RUNTIME_CONFIG_PATH.exists():
                current_mtime = RUNTIME_CONFIG_PATH.stat().st_mtime
                if current_mtime == _CONFIG_CACHE["mtime"]:
                    # Cache is fresh and file unchanged
                    return _CONFIG_CACHE["data"]
            else:
                # File doesn't exist, cache env-only config
                if cache_age < CONFIG_CACHE_TTL:
                    return _CONFIG_CACHE["data"]
    
    # Load from file
    cfg = {}
    file_mtime = 0
    
    if RUNTIME_CONFIG_PATH.exists():
        file_mtime = RUNTIME_CONFIG_PATH.stat().st_mtime
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
    optimization_mode = os.getenv("OPTIMIZATION_MODE") or cfg.get("optimization_mode")
    auto_provision = os.getenv("AUTO_PROVISION")
    if auto_provision is None:
        auto_provision = cfg.get("auto_provision")
    else:
        auto_provision = str(auto_provision).lower() in ("1", "true", "yes")

    result = {
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
        "optimization_mode": optimization_mode,
        "raw": cfg,
    }
    
    # Validate required config keys
    result = _validate_config(result)
    
    # Update cache
    _CONFIG_CACHE = {
        "data": result,
        "mtime": file_mtime,
        "cached_at": now
    }
    
    return result


_V2_BASELINE_CACHE: dict = {"data": None, "mtime": 0}


def load_v2_baseline(force_reload=False):
    """Load frozen V2 behavioral baseline (ADR-006..024).

    Returns the parsed `config/v2_baseline.yaml` dict. Cached by mtime;
    callers must treat the result as read-only (plans pin copies).
    """
    global _V2_BASELINE_CACHE
    mtime = V2_BASELINE_PATH.stat().st_mtime if V2_BASELINE_PATH.exists() else 0
    if not force_reload and _V2_BASELINE_CACHE["data"] is not None \
            and _V2_BASELINE_CACHE["mtime"] == mtime:
        return _V2_BASELINE_CACHE["data"]
    if not V2_BASELINE_PATH.exists():
        raise SystemExit("Missing config/v2_baseline.yaml (V2 frozen baseline)")
    with open(V2_BASELINE_PATH) as f:
        data = yaml.safe_load(f) or {}
    _V2_BASELINE_CACHE = {"data": data, "mtime": mtime}
    return data

