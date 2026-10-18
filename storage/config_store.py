# storage/config_store.py — immutable versioned config (ADR: config pinning).
from __future__ import annotations

from copy import deepcopy
from storage.backend import Documents


class ConfigStore:
    def __init__(self, table_name="spot_arbitrage_config", dynamodb_resource=None):
        self._docs = Documents("config_id", table_name, dynamodb_resource)

    def publish(self, domain: str, version: str, payload: dict) -> dict:
        identity = f"{domain}@{version}"
        with self._docs.lock:
            try:
                existing = self._docs.get(identity)["payload"]
            except KeyError:
                existing = None
            if existing is not None:
                if existing != payload:
                    raise RuntimeError(f"{identity} immutable: payload differs")
                return existing
            self._docs.put({"config_id": identity, "payload": payload}, create=True)
            return deepcopy(payload)

    def resolve(self, pins: dict[str, str]) -> dict:
        return {domain: self._docs.get(f"{domain}@{version}")["payload"]
                for domain, version in pins.items()}
