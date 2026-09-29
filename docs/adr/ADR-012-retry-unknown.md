# ADR-012 — Retry budgets and UNKNOWN

Status: accepted. Date: 2026-09-07.

Context: blind retry duplicates infrastructure; UNKNOWN needs resolution.
Decision: per-operation 3 attempts, exp backoff 2s→30s ±25% jitter;
max 2 replans, 3 recovery attempts per migration. Same operation_id =
idempotent retry; new ID = new operation. UNKNOWN → GetOperationStatusQuery:
resolved SUCCESS continue, FAILED retry-if-budget, else RECONCILIATION_REQUIRED.
Consequences: coordinator + executors (ClientToken=operation_id); no blind retry.
