# ADR-007 — Short-job economics

Status: accepted. Date: 2026-09-07.

Context: tiny workloads pay more in checkpoint/provision/restore than they save.
Decision: net_benefit = remaining_savings − migration_cost
(migration = checkpoint+transfer+provision+restore+overhead+risk).
SHORT (<15m): no arbitrage/proactive. MEDIUM (15–60m): net_benefit ≥
max(2×migration_cost, 20%×remaining_savings). LONG/STATEFUL: normal policy;
STATEFUL additionally requires DURABLE checkpoint. Emergency ignores this rule.
Consequences: implement in CostRiskEvaluator + policy; needs remaining-runtime
and full cost breakdown inputs.
