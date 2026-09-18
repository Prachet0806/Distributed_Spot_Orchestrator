# storage/checkpoint_store.py — checkpoint metadata, lineage, locks.
"""Monotonic durability LOCAL→PERSISTING→DURABLE→VALIDATED. Locks guard GC.

Dynamo shape (when wired): table `spot_arbitrage_checkpoints`,
PK `checkpoint_id`, GSI `lineage_id`.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

_DURABILITY_ORDER = {"LOCAL": 0, "PERSISTING": 1, "DURABLE": 2, "VALIDATED": 3}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckpointStore:
    def __init__(self, table_name: str = "spot_arbitrage_checkpoints", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._items: dict[str, dict] = {}

    def put(self, ref) -> dict:
        from orchestrator.models_v2 import to_dict
        doc = deepcopy(to_dict(ref))
        cid = doc.get("checkpoint_id")
        if not cid:
            raise ValueError("checkpoint requires checkpoint_id")
        if cid in self._items:
            raise KeyError(f"checkpoint {cid} already exists")
        doc.setdefault("durability", "LOCAL")
        doc.setdefault("locks", [])
        doc.setdefault("created_at", _now())
        self._items[cid] = doc
        if self.dynamodb is not None:
            self.dynamodb.Table(self.table_name).put_item(
                Item=doc, ConditionExpression="attribute_not_exists(checkpoint_id)")
        return deepcopy(doc)

    def get(self, checkpoint_id: str) -> dict:
        try:
            return deepcopy(self._items[checkpoint_id])
        except KeyError:
            raise KeyError(f"checkpoint {checkpoint_id} not found")

    def any_durable(self, lineage_id: str) -> bool:
        """True when any DURABLE/VALIDATED checkpoint exists for a lineage."""
        return any(
            d.get("lineage_id") == lineage_id
            and d.get("durability") in ("DURABLE", "VALIDATED")
            for d in self._items.values()
        )

    def set_durability(self, checkpoint_id: str, to_state: str) -> dict:
        doc = self.get(checkpoint_id)
        cur = doc.get("durability", "LOCAL")
        if _DURABILITY_ORDER.get(to_state, -1) < _DURABILITY_ORDER.get(cur, 0):
            raise RuntimeError(f"durability is monotonic: {cur} -> {to_state} refused")
        if to_state not in _DURABILITY_ORDER:
            raise ValueError(f"unknown durability {to_state}")
        doc["durability"] = to_state
        self._items[checkpoint_id] = doc
        return deepcopy(doc)

    def lock(self, checkpoint_id: str, migration_id: str) -> dict:
        doc = self.get(checkpoint_id)
        if migration_id not in doc.get("locks", []):
            doc["locks"].append(migration_id)
        self._items[checkpoint_id] = doc
        return deepcopy(doc)

    def unlock(self, checkpoint_id: str, migration_id: str) -> dict:
        doc = self.get(checkpoint_id)
        doc["locks"] = [m for m in doc.get("locks", []) if m != migration_id]
        self._items[checkpoint_id] = doc
        return deepcopy(doc)

    def gc_candidates(self, keep_n: int = 3, max_age_seconds: float = 86400.0,
                      grace_seconds: float = 21600.0, now: Optional[str] = None) -> list[str]:
        """Eligible ∧ unlocked ∧ old ∧ past-grace. Keeps latest `keep_n` DURABLE+."""
        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        durables = sorted(
            (d for d in self._items.values() if d.get("durability") in ("DURABLE", "VALIDATED")),
            key=lambda d: d.get("created_at", ""), reverse=True,
        )
        keep = {d["checkpoint_id"] for d in durables[:keep_n]}
        out = []
        for doc in self._items.values():
            cid = doc["checkpoint_id"]
            if cid in keep or doc.get("locks"):
                continue
            try:
                created = datetime.fromisoformat(doc.get("created_at", ""))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            age = (now_dt - created).total_seconds()
            if age >= max_age_seconds + grace_seconds:
                out.append(cid)
        return out
