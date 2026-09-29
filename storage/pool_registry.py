# storage/pool_registry.py — versioned candidate pool definitions.
"""Pool definitions are durable config; capacity/readiness stay observations.

Phase A (PA-7): a PoolDefinition is the authoritative provisioning spec
(region, AMI, SG, IAM profile, runtime digest, CRIU/kernel, GPU,
transport). The Provisioner consumes it directly (PA-8); the global
runtime.yaml fallback is warned-and-metered, never silent.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

# Required keys for a candidate-authoritative definition. ami_id and
# security_group_id may be empty ONLY for emulated/local pools; any real
# (provider == "aws") definition without them is rejected.
REQUIRED_POOL_FIELDS = ("pool_id", "region", "instance_type")
AWS_REQUIRED_POOL_FIELDS = ("ami_id", "security_group_id")


def validate_pool_definition(definition: dict, *, strict_aws: bool = True) -> list[str]:
    """Return a list of validation errors ([] == valid). Never raises."""
    if not isinstance(definition, dict):
        return ["definition must be a mapping"]
    errors = [f"missing field: {k}" for k in REQUIRED_POOL_FIELDS
              if not definition.get(k)]
    if strict_aws and definition.get("provider", "aws") == "aws":
        errors += [f"missing AWS field: {k}" for k in AWS_REQUIRED_POOL_FIELDS
                   if not definition.get(k)]
    return errors


def definition_from_pool(pool: Any) -> dict:
    """Build a registry definition from a CandidatePool object."""
    get = lambda k, d=None: getattr(pool, k, d)
    rt = get("runtime_profile")
    return {
        "pool_id": get("pool_id", ""),
        "provider": get("provider", "aws"),
        "region": get("region", ""),
        "availability_zone": get("availability_zone", ""),
        "instance_type": get("instance_type", ""),
        "architecture": get("architecture", "x86_64"),
        "ami_id": get("ami_id", "") or "",
        "security_group_id": get("security_group_id", "") or "",
        "iam_profile": get("iam_profile", "") or "",
        "runtime_artifact_digest": getattr(rt, "artifact_digest", "") or "",
        "criu_version": getattr(rt, "criu_version", "") or "",
        "kernel_version": getattr(rt, "kernel_version", "") or "",
        "lifecycle": get("lifecycle", "ACTIVE"),
        "max_concurrent_migrations": get("max_concurrent_migrations", 2),
    }


def load_definitions_file(path: str) -> dict:
    """Load {pool_id: definition} from a JSON file. Missing file ⇒ {}."""
    import json
    import os
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data
    return {}


def save_definitions_file(path: str, definitions: dict) -> None:
    """Persist {pool_id: definition} to a JSON file (atomic-ish)."""
    import json
    import os
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(definitions, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


class PoolRegistryStore:
    def __init__(self, table_name: str = "spot_arbitrage_candidate_pools", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._pools: dict[str, dict] = {}

    def put_pool(self, pool_id: str, definition: dict, version: int) -> dict:
        cur = self._pools.get(pool_id)
        if cur is not None and version <= int(cur.get("version", 0)):
            raise RuntimeError(f"pool {pool_id} version must increase")
        doc = {"pool_id": pool_id, "version": version,
               "definition": deepcopy(definition)}
        self._pools[pool_id] = doc
        if self.dynamodb is not None:
            self.dynamodb.Table(self.table_name).put_item(
                Item=deepcopy(doc),
                ConditionExpression="attribute_not_exists(pool_id)")
        return deepcopy(doc)

    def get_pool(self, pool_id: str) -> dict:
        try:
            return deepcopy(self._pools[pool_id])
        except KeyError:
            raise KeyError(f"pool {pool_id} not found")
