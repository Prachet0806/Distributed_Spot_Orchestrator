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
import threading
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
        self._lock = threading.RLock()

    def get(self, lease_id: str) -> Optional[Dict[str, Any]]:
        if self.dynamodb is not None:
            resp = self.dynamodb.Table(self.table_name).get_item(
                Key={"lease_id": lease_id}, ConsistentRead=True)
            item = resp.get("Item")
            return deepcopy(item) if item else None
        doc = self._leases.get(lease_id)
        return deepcopy(doc) if doc else None

    def acquire(self, lease_id: str, owner: str, expires_at: str, now: Optional[str] = None) -> bool:
        now = now or datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.get(lease_id)
            if cur and cur.get("owner") != owner and cur["expires_at"] > now:
                return False
            version = int(cur.get("version", 0)) if cur else 0
            generation = int(cur.get("generation", 0)) if cur else 0
            if not cur or cur.get("owner") != owner or cur["expires_at"] <= now:
                generation += 1
            doc = {"lease_id": lease_id, "owner": owner, "expires_at": expires_at,
                   "version": version + 1, "generation": generation}
            if self.dynamodb is not None:
                kwargs = {"Item": doc,
                          "ConditionExpression": "attribute_not_exists(lease_id)"}
                if cur:
                    kwargs.update(ConditionExpression="version = :v AND (#o = :owner OR expires_at <= :now)",
                                  ExpressionAttributeNames={"#o": "owner"},
                                  ExpressionAttributeValues={":v": version, ":owner": owner, ":now": now})
                try:
                    self.dynamodb.Table(self.table_name).put_item(**kwargs)
                except Exception as exc:
                    if "ConditionalCheckFailedException" in str(exc):
                        return False
                    raise
            self._leases[lease_id] = doc
            return True

    def renew(self, lease_id: str, owner: str, expected_version: int,
              expires_at: str, now: Optional[str] = None) -> bool:
        now = now or datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.get(lease_id)
            if (not cur or cur["owner"] != owner or cur["version"] != expected_version
                    or cur["expires_at"] <= now):
                return False
            if self.dynamodb is not None:
                try:
                    self.dynamodb.Table(self.table_name).update_item(
                        Key={"lease_id": lease_id},
                        UpdateExpression="SET expires_at = :exp, version = version + :one",
                        ConditionExpression="#o = :owner AND version = :ver AND expires_at > :now",
                        ExpressionAttributeNames={"#o": "owner"},
                        ExpressionAttributeValues={":exp": expires_at, ":owner": owner,
                                                   ":ver": expected_version, ":one": 1, ":now": now})
                except Exception as exc:
                    if "ConditionalCheckFailedException" in str(exc):
                        return False
                    raise
            self._leases[lease_id] = {**cur, "expires_at": expires_at, "version": expected_version + 1}
            return True

    def release(self, lease_id: str, owner: str) -> None:
        with self._lock:
            cur = self.get(lease_id)
            if not cur or cur["owner"] != owner:
                return
            # Retain generation so a future owner cannot reuse a fencing token.
            if self.dynamodb is not None:
                try:
                    self.dynamodb.Table(self.table_name).update_item(
                        Key={"lease_id": lease_id},
                        UpdateExpression="SET #o = :empty, expires_at = :expired, version = version + :one",
                        ConditionExpression="#o = :owner AND version = :v",
                        ExpressionAttributeNames={"#o": "owner"},
                        ExpressionAttributeValues={":empty": "", ":expired": "1970-01-01T00:00:00+00:00",
                                                   ":one": 1, ":owner": owner, ":v": cur["version"]})
                except Exception as exc:
                    if "ConditionalCheckFailedException" in str(exc):
                        return
                    raise
            self._leases[lease_id] = {**cur, "owner": "", "expires_at": "1970-01-01T00:00:00+00:00",
                                      "version": cur["version"] + 1}


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
        self.generation = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def _expiry_iso(self) -> str:
        return _now_iso(lambda: self._wall() + self.ttl)

    def acquire(self) -> bool:
        ok = self.store.acquire(self.lease_id, self.owner, self._expiry_iso(), now=_now_iso(self._wall))
        if ok:
            doc = self.store.get(self.lease_id) or {}
            self._version = int(doc.get("version", 1))
            self.generation = int(doc.get("generation", 1))
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
                              self._version, self._expiry_iso(), now=_now_iso(self._wall))
        if ok:
            self._version += 1
            self._last_beat = self._wall()
        else:
            self._holder = False
        return ok

    def heartbeat_due(self) -> bool:
        return self._holder and (self._wall() - self._last_beat) >= self.heartbeat_interval

    def is_holder(self) -> bool:
        return self._holder and self._wall() - self._last_beat < self.ttl

    def assert_fresh(self):
        with self._lock:
            doc = self.store.get(self.lease_id)
            if (not self.is_holder() or not doc or doc["owner"] != self.owner
                    or doc["expires_at"] <= _now_iso(self._wall)
                    or int(doc.get("generation", 0)) != self.generation):
                self._holder = False
                raise RuntimeError("control-plane lease is not fresh")

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        def renew_loop():
            while not self._stop.wait(self.heartbeat_interval):
                try:
                    with self._lock:
                        if not self.heartbeat():
                            return
                except Exception:
                    self._holder = False
                    return
        self._thread = threading.Thread(target=renew_loop, daemon=True, name="lease-renewal")
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat_interval + 1)
            self._thread = None
        try:
            self.store.release(self.lease_id, self.owner)
        finally:
            self._holder = False


def check_lease_for_iteration(lease: "ControlPlaneLease") -> tuple[bool, str]:
    """Per-iteration self-fencing guard for the main loop (Sprint 4).

    Returns (True, "continue") when this process holds a fresh lease,
    (False, reason) when it must halt actuation: "not-holder" (never
    acquired) or "fenced" (heartbeat refused — a rival holds the record).
    Heartbeats on schedule; a due heartbeat that succeeds still continues.
    Pure over the injected lease (clocks injectable via the lease).
    """
    try:
        if not lease.is_holder():
            return False, "not-holder"
        if lease.heartbeat_due():
            if not lease.heartbeat():
                return False, "fenced"
        return True, "continue"
    except Exception as exc:
        return False, f"lease-error: {exc}"
