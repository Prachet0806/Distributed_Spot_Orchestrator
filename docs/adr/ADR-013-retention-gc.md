# ADR-013 — Checkpoint retention and GC

Status: accepted. Date: 2026-09-07.

Context: unbounded checkpoints cost; aggressive GC breaks recovery.
Decision: retain latest 3 DURABLE + latest VALIDATED; max_recovery_age 24h;
GC grace 6h. Never delete checkpoints referenced by active migration /
recovery / plan / reconciliation. Partial multiparts cleaned by transfer
cleanup, not checkpoint GC.
Consequences: checkpoint store enforces locks + lineage; GC is mark-and-sweep
on (eligible ∧ unreferenced ∧ old ∧ past-grace).
