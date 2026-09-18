# orchestrator/control_plane_lease.py — CPR self-fencing lease (Track B2).
"""Single-writer lease for the control plane (ADR-020: min=max=1 + lease).

The holder heartbeats every 15 s against a 45 s TTL. Any process that is
not the fresh holder MUST halt instead of actuating (self-fencing): at most
one coordinator executes a migration. Backed by the
`spot_arbitrage_control_plane_lease` table when wired (PK `lease_id`);
local dict backend otherwise. Clocks are injectable for deterministic tests.
"""
from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

LEASE_TABLE = "spot_arbitrage_control_plane_lease"
PRIMARY_LEASE_ID = "cpr-primary"
HEARTBEAT_INTERVAL_SECONDS = 15.0
LEASE_TTL_SECONDS = 45.0


def _now_iso(wall: Callable[[], float]) -> str:
    return datetime.fromtimestamp(wall(), tz=timezone.utc).isoformat()


class LeaseStore:
    """Lease records with compare-and-swap semantics on both backends."""

    def __init__(self, table_name: str = LEASE_TABLE, dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._leases: Dict[str, Dict[str, Any]] = {}

    def get(self, lease_id: str) -> Optional[Dict[str, Any]]:
        if self.dynamodb is not None:
            resp = self.dynamodb.Table(self.table_name).get_item(
                Key={"lease_id": lease_id})
            item = resp.get("Item")
            return deepcopy(item) if item else None
        doc = self._leases.get(lease_id)
        return deepcopy(doc) if doc else None

    def acquire(self, lease_id: str, owner: str, expires_at: str) -> bool:
        """First-wins acquire (or re-acquire by the same owner)."""
        if self.dynamodb is not None:
            try:
                self.dynamodb.Table(self.table_name).put_item(
                    Item={"lease_id": lease_id, "owner": owner,
                          "expires_at": expires_at, "version": 1},
                    ConditionExpression=(
                        "attribute_not_exists(lease_id) OR #o = :owner"),
                    ExpressionAttributeNames={"#o": "owner"},
                    ExpressionAttributeValues={":owner": owner},
                )
                return True
            except Exception as exc:
                if "ConditionalCheckFailedException" in str(exc):
                    return False
                raise
        cur = self._leases.get(lease_id)
        if cur is not None and cur.get("owner") != owner:
            return False
        version = int(cur.get("version", 0)) + 1 if cur else 1
        self._leases[lease_id] = {"lease_id": lease_id, "owner": owner,
                                  "expires_at": expires_at, "version": version}
        return True

    def renew(self, lease_id: str, owner: str, expected_version: int,
              expires_at: str) -> bool:
        """CAS renew: only the recorded owner at the expected version wins."""
        if self.dynamodb is not None:
            try:
                self.dynamodb.Table(self.table_name).update_item(
                    Key={"lease_id": lease_id},
                    UpdateExpression="SET expires_at = :exp, version = version + :one",
                    ConditionExpression="#o = :owner AND version = :ver",
                    ExpressionAttributeNames={"#o": "owner"},
                    ExpressionAttributeValues={
                        ":exp": expires_at, ":owner": owner,
                        ":ver": expected_version, ":one": 1},
                )
                return True
            except Exception as exc:
                if "ConditionalCheckFailedException" in str(exc):
                    return False
                raise
        cur = self._leases.get(lease_id)
        if cur is None or cur.get("owner") != owner:
            return False
        if int(cur.get("version", 0)) != expected_version:
            return False
        cur["expires_at"] = expires_at
        cur["version"] = expected_version + 1
        return True

    def release(self, lease_id: str, owner: str) -> None:
        cur = self.get(lease_id)
        if cur is None or cur.get("owner") != owner:
            return
        if self.dynamodb is not None:
            try:
                self.dynamodb.Table(self.table_name).delete_item(
                    Key={"lease_id": lease_id},
                    ConditionExpression="#o = :owner",
                    ExpressionAttributeNames={"#o": "owner"},
                    ExpressionAttributeValues={":owner": owner},
                )
            except Exception:
                pass
        self._leases.pop(lease_id, None)


class ControlPlaneLease:
    """Heartbeat-guarded CPR ownership. Losing it means halt, not act."""

    def __init__(
        self,
        store: Optional[LeaseStore] = None,
        lease_id: str = PRIMARY_LEASE_ID,
        owner: Optional[str] = None,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        ttl_seconds: float = LEASE_TTL_SECONDS,
        wall_clock: Optional[Callable[[], float]] = None,
    ):
        import uuid
        self.store = store or LeaseStore()
        self.lease_id = lease_id
        self.owner = owner or f"cpr-{uuid.uuid4().hex[:12]}"
        self.heartbeat_interval = heartbeat_interval_seconds
        self.ttl = ttl_seconds
        self._wall = wall_clock or time.time
        self._version = 0
        self._holder = False
        self._last_beat = 0.0

    def _expiry_iso(self) -> str:
        return _now_iso(lambda: self._wall() + self.ttl)

    def acquire(self) -> bool:
        ok = self.store.acquire(self.lease_id, self.owner, self._expiry_iso())
        if ok:
            doc = self.store.get(self.lease_id) or {}
            self._version = int(doc.get("version", 1))
            self._holder = True
            self._last_beat = self._wall()
        else:
            self._holder = False
        return ok

    def heartbeat(self) -> bool:
        """Renew; False means fenced — the caller must halt actuation."""
        if not self._holder:
            return False
        ok = self.store.renew(self.lease_id, self.owner,
                              self._version, self._expiry_iso())
        if ok:
            self._version += 1
            self._last_beat = self._wall()
        else:
            self._holder = False
        return ok

    def heartbeat_due(self) -> bool:
        return self._holder and (self._wall() - self._last_beat) >= self.heartbeat_interval

    def is_holder(self) -> bool:
        return self._holder

    def release(self) -> None:
        try:
            self.store.release(self.lease_id, self.owner)
        finally:
            self._holder = False
