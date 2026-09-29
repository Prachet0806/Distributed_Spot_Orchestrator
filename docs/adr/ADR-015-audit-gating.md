# ADR-015 — Audit gating

Status: accepted. Date: 2026-09-07.

Context: full sync audit is slow; async-only loses safety evidence.
Decision: sync persist-before-transition for ownership, admission, plan
activation, durability declaration, fencing start/complete, termination
confirm, terminal states, recovery/reconciliation transitions. Metrics,
heartbeats, observations, progress, debug stay async.
Consequences: executors call audit store on the gating list; transition
rejected if gate record missing.
