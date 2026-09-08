# storage/pool_registry.py — versioned candidate pool definitions.
"""Pool definitions are durable config; capacity/readiness stay observations."""
from __future__ import annotations

from copy import deepcopy


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
