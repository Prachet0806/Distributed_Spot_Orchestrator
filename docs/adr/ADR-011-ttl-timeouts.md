# ADR-011 — Plan TTL and timeouts

Status: accepted. Date: 2026-09-07.

Context: stale plans + unbounded steps break deadline safety.
Decision: plan TTL arbitrage/proactive 600s; emergency = remaining
deadline (computed once at ingestion, stored in plan, never recomputed).
Op timeout 300s, step 60s, UNKNOWN budget 30s — all upper bounds.
step_deadline = min(plan_expiry, migration_deadline, op_timeout).
Fencing, once begun, completes (COMPLETE_FENCING).
Consequences: planner emits TTLs; coordinator enforces via monotonic clock.
