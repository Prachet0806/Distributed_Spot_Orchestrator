# ADR-017 — Concurrency and DEGRADED pools

Status: accepted. Date: 2026-09-07.

Context: unbounded migration bursts exhaust quotas and flap pools.
Decision: global 3, per-pool 2, per-job 1. READY→all regimes;
DEGRADED→emergency-only (proactive off by default); NOT_READY/UNKNOWN→none.
Emergency has priority but still respects global exhaustion.
Consequences: placement + coordinator enforce headroom checks; exceeding a
pool yields CAPACITY_UNAVAILABLE, not queueing.
