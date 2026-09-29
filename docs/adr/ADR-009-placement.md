# ADR-009 — Placement weights and tie-break

Status: accepted. Date: 2026-09-07.

Context: ranking must be deterministic and must not recompute economics.
Decision: v3 weights compatibility .30 / readiness .25 / capacity .20 /
locality .10 / reliability .10 / operational_cost .05 over
already-eligible candidates. Tie-break: score → freshness → capacity
confidence → migration time → pool_id lexicographic (final).
Consequences: new placement_policy v3; v2.1 kept for reads; PlacementEngine
consumes CostAnalysis, never builds it.
