# storage/config_store.py — immutable versioned config (ADR: config pinning).
from __future__ import annotations

from copy import deepcopy


class ConfigStore:
    def __init__(self):
        self._versions: dict[tuple[str, str], dict] = {}

    def publish(self, domain: str, version: str, payload: dict) -> dict:
        key = (domain, version)
        if key in self._versions:
            if self._versions[key] != payload:
                raise RuntimeError(f"{domain}@{version} immutable: payload differs")
            return deepcopy(self._versions[key])
        self._versions[key] = deepcopy(payload)
        return deepcopy(payload)

    def resolve(self, pins: dict[str, str]) -> dict:
        out = {}
        for domain, version in pins.items():
            try:
                out[domain] = deepcopy(self._versions[(domain, version)])
            except KeyError:
                raise KeyError(f"config {domain}@{version} not published")
        return out
