# Telemetry Contract — worker → control plane evidence (normative)

**Status:** contract frozen; transport unwired (see `IMPLEMENTATION_STATUS.md` T1).
Workers emit evidence only. Emission never triggers decisions locally
(autonomy prohibition, component-architecture §12.3).

## T1. Heartbeat — worker liveness

```json
{
  "schema": "heartbeat/v1",
  "job_id": "...",
  "instance_id": "i-...",
  "execution_epoch": 7,
  "pid": 4242,
  "observed_at": "2026-01-01T00:00:00Z",
  "workload": {"state": "RUNNING", "progress": 0.5}
}
```

- Interval: 15 s (worker `job_runner` default). Lapse > 60 s ⇒
  Infrastructure Monitor marks readiness `UNKNOWN` (never down — I7).
- Transport: async, best-effort, never gating. Loss degrades confidence,
  never fabricates health.

## T2. Progress report — estimation evidence

```json
{
  "schema": "progress/v1",
  "job_id": "...",
  "execution_epoch": 7,
  "observed_at": "...",
  "progress": 0.5,
  "cpu_utilization": 0.5,
  "memory_utilization": 0.5,
  "checkpoint_size_bytes": 1048576,
  "checkpoint_duration_seconds": 1.2,
  "observation_confidence": 0.9
}
```

- Written by the workload alongside its durable progress file; shipped on
  the same async channel as heartbeats (piggybacked when fresh).
- Feeds `WorkloadEstimator` observations and L3 validation snapshots.
  Stale (> 5 min) ⇒ estimator treats as degraded, never as zero remaining.

## T3. Preflight result — readiness evidence

```json
{
  "schema": "preflight/v1",
  "pool_id": "...",
  "instance_id": "i-...",
  "criu_version": "3.19",
  "kernel_version": "5.15.0",
  "architecture": "x86_64",
  "capabilities": "unprivileged-ok",
  "unsupported_features": "",
  "passed": true,
  "observed_at": "..."
}
```

- Produced by `criu_wrapper.sh preflight` at pool join and cached in pool
  metadata with TTL (default 1 h). Refresh on AMI change.
- `passed: false` ⇒ pool `NOT_READY` for that workload class until refresh.

## T4. Command refusal — findings-grade evidence

```json
{
  "schema": "refusal/v1",
  "instance_id": "i-...",
  "admission_epoch": 7,
  "operation_id": "...",
  "expected_execution_epoch": 6,
  "reason": "EPOCH_MISMATCH",
  "observed_at": "..."
}
```

- Emitted by the worker controller for every refused command (epoch
  mismatch, unknown operation, pairing failure). Refusals are never silent
  drops: each one fans into the Reconciliation event lane, auto-filing
  `EPOCH_MISMATCH`/`UNKNOWN_OWNERSHIP` findings as evidence.

## Freshness and staleness rules

| Stream | Fresh | Stale action |
|---|---|---|
| heartbeat | ≤ 60 s | readiness `UNKNOWN`; no health inference |
| progress | ≤ 5 min | estimator confidence downgraded; no zero-fill |
| preflight | ≤ TTL (1 h) | pool readiness re-probed before admission |
| refusal | always actionable | filed immediately, never batched |

## Gating rule

Telemetry absence never blocks fencing, activation, or recovery decisions
directly — but any decision requiring evidence it does not have must
resolve to `UNKNOWN`/infeasible rather than optimistic (I7, I15).
Concretely: no fresh heartbeat ⇒ readiness `UNKNOWN` ⇒ emergency
ineligible; no progress ⇒ estimator `LOW` confidence ⇒ conservative
feasibility bound or infeasible.
