# storage/checkpoint_store.py — checkpoint metadata, lineage, locks.
"""Monotonic durability LOCAL→PERSISTING→DURABLE→VALIDATED. Locks guard GC.

Dynamo shape (when wired): table `spot_arbitrage_checkpoints`,
PK `checkpoint_id`, GSI `lineage_id`.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from storage.backend import Documents, conditional_failure

_DURABILITY_ORDER = {"LOCAL": 0, "PERSISTING": 1, "DURABLE": 2, "VALIDATED": 3}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckpointStore:
    def __init__(
        self, table_name: str = "spot_arbitrage_checkpoints", dynamodb_resource=None
    ):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._docs = Documents("checkpoint_id", table_name, dynamodb_resource)
        self._items = self._docs.local

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
        doc.setdefault("version", 0)
        try:
            return self._docs.put(doc, create=True)
        except Exception as exc:
            if conditional_failure(exc):
                raise KeyError(f"checkpoint {cid} already exists") from exc
            raise

    def get(self, checkpoint_id: str) -> dict:
        return self._docs.get(checkpoint_id)

    def _mutate(self, checkpoint_id: str, change) -> dict:
        for _ in range(10):
            doc = self.get(checkpoint_id)
            version = doc.get("version", 0)
            change(doc)
            doc["version"] = version + 1
            try:
                return self._docs.put(doc, expected_version=version)
            except Exception as exc:
                if not conditional_failure(exc) and str(exc) != "concurrent record update":
                    raise
        raise RuntimeError("checkpoint concurrent update retry limit exceeded")

    def update(self, checkpoint_id: str, **patch) -> dict:
        if {
            "checkpoint_id",
            "lineage_id",
            "execution_epoch",
            "version",
        } & patch.keys():
            raise ValueError("checkpoint identity is immutable")
        return self._mutate(checkpoint_id, lambda doc: doc.update(deepcopy(patch)))

    def any_durable(self, lineage_id: str) -> bool:
        """True when any DURABLE/VALIDATED checkpoint exists for a lineage."""
        return self.latest_durable(lineage_id) is not None

    def latest_durable(self, lineage_id: str) -> dict | None:
        """Latest DURABLE/VALIDATED checkpoint doc for a lineage, else None.

        Lineage convention: lineage_id == job_id (see criu_handlers,
        checkpoint_manager fallback). Never raises: missing lineage or
        unreadable rows yield None (durability is never inferred).
        """
        try:
            cands = [
                d
                for d in self._docs.query_index("LineageIndex", "lineage_id", lineage_id)
                if d.get("lineage_id") == lineage_id
                and d.get("durability") in ("DURABLE", "VALIDATED")
            ]
        except (AttributeError, TypeError):
            return None
        if not cands:
            return None
        cands.sort(key=lambda d: d.get("created_at", ""))
        return deepcopy(cands[-1])

    def set_durability(self, checkpoint_id: str, to_state: str) -> dict:
        if to_state not in _DURABILITY_ORDER:
            raise ValueError(f"unknown durability {to_state}")
        def change(doc):
            cur = doc.get("durability", "LOCAL")
            if _DURABILITY_ORDER[to_state] < _DURABILITY_ORDER.get(cur, 0):
                raise RuntimeError(f"durability is monotonic: {cur} -> {to_state} refused")
            doc["durability"] = to_state
        return self._mutate(checkpoint_id, change)

    def publish_durable(
        self, checkpoint_id: str, *, checksum: str, size_bytes: int,
        artifact_location: str,
    ) -> dict:
        """Commit artifact identity and durability in one versioned write."""
        if not checksum or not artifact_location or int(size_bytes) < 0:
            raise ValueError("durable checkpoint requires artifact metadata")

        def change(doc):
            if doc.get("durability") in {"DURABLE", "VALIDATED"} and doc.get(
                "checksum"
            ) != checksum:
                raise RuntimeError("durable checkpoint artifact identity conflict")
            doc.update(
                durability=(
                    "VALIDATED" if doc.get("durability") == "VALIDATED" else "DURABLE"
                ),
                checksum=checksum,
                size_bytes=int(size_bytes),
                artifact_location=artifact_location,
                integrity_verified=True,
            )

        return self._mutate(checkpoint_id, change)

    def lock(self, checkpoint_id: str, migration_id: str) -> dict:
        def change(doc):
            locks = doc.setdefault("locks", [])
            if migration_id not in locks:
                locks.append(migration_id)
        return self._mutate(checkpoint_id, change)

    def unlock(self, checkpoint_id: str, migration_id: str) -> dict:
        return self._mutate(
            checkpoint_id,
            lambda doc: doc.update(
                locks=[m for m in doc.get("locks", []) if m != migration_id]
            ),
        )

    def gc_candidates(
        self,
        keep_n: int = 3,
        max_age_seconds: float = 86400.0,
        grace_seconds: float = 21600.0,
        now: str | None = None,
    ) -> list[str]:
        """Eligible ∧ unlocked ∧ old ∧ past-grace. Keeps latest `keep_n` DURABLE+."""
        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        durables = sorted(
            (
                d
                for d in self._docs.all()
                if d.get("durability") in ("DURABLE", "VALIDATED")
            ),
            key=lambda d: d.get("created_at", ""),
            reverse=True,
        )
        keep, counts = set(), {}
        for doc in durables:
            lineage = doc.get("lineage_id")
            counts[lineage] = counts.get(lineage, 0) + 1
            if counts[lineage] <= keep_n:
                keep.add(doc["checkpoint_id"])
        out = []
        for doc in self._docs.all():
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
