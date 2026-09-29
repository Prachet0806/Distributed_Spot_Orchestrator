# Distributed Spot Arbitrage Orchestrator — V2
# Contracts, Components & Contract Dependency Matrix

**Status:** Architecture reference / V2 cross-cutting contract map

## 1. Component Inventory

1. Job Registry
2. Workload Estimator
3. Market/Risk Monitor
4. Infrastructure Monitor
5. Candidate Compatibility
6. Recovery Feasibility
7. Cost & Risk Evaluator
8. Policy Engine
9. Migration Planner
10. Migration Coordinator
11. Provisioner
12. CRIU / Checkpoint-Restore Manager
13. Validator
14. Reconciliation Manager
15. Migration History
16. Observability / Audit

## 2. Contract Inventory

| ID | Contract |
|---|---|
| C01 | Identity Contract |
| C02 | Time Contract |
| C03 | State Ownership Contract |
| C04 | Event Contract |
| C05 | Command Idempotency Contract |
| C06 | Migration Transaction Contract |
| C07 | Fencing Contract |
| C08 | Abort & Rollback Contract |
| C09 | Checkpoint Durability Contract |
| C10 | Checkpoint Retention Contract |
| C11 | Candidate & Plan Freshness Contract |
| C12 | Decision Precedence Contract |
| C13 | Failure Taxonomy Contract |
| C14 | Reconciliation Contract |
| C15 | Security Contract |
| C16 | Model & Configuration Versioning Contract |
| C17 | Observability & Audit Contract |
| C18 | Confidence Semantics Contract |
| C19 | Workload Security Boundary |
| C20 | Control-Plane Boundedness Contract |
| C21 | Cross-Job Scope Boundary |

## 3. Contract Definitions

### C01 — Identity
`job_id` = immutable logical identity; `version` = Registry concurrency; `execution_epoch` = fencing/ownership; `migration_id` = migration attempt; `operation_id` = one command and reused across retries; `event_id` = event deduplication; `correlation_id` = telemetry correlation.

### C02 — Time
Wall-clock time is used for absolute timestamps/deadlines. Monotonic time is used for elapsed durations/timeouts. Market/Risk Monitor establishes an interruption deadline once; Coordinator owns remaining-budget calculation afterward.

### C03 — State Ownership
Migration ownership is `SOURCE | TRANSITIONING | TARGET`. Coordinator owns authoritative ownership transitions. `TRANSITIONING` is bounded by a timeout.

### C04 — Event
At-least-once delivery. Events carry `event_id`, type, source, `observed_at`, `effective_at`, correlation, and applicable job/migration/epoch/version. Deduplication is durable; stale events are checked against current authoritative state when applied.

### C05 — Command Idempotency
Every command has an `operation_id`. Retries reuse it. `UNKNOWN` outcomes must be resolved against actual infrastructure state before blind retry. AWS client tokens use `operation_id` where supported.

### C06 — Migration Transaction
Migration is a durable state machine: `PLANNED → PRECHECKING → CHECKPOINTING → PERSISTING → PROVISIONING → TRANSFERRING → RESTORING → FENCING → VALIDATING → ACTIVATING → FINALIZING`.

### C07 — Fencing
Fencing is pre-authorized in the Migration Plan. Confirmed fencing requires both control-plane epoch invalidation and actual source-process termination. Signal sent is not termination confirmed.

### C08 — Abort & Rollback
Rollback to source exists only before confirmed fencing. After fencing: retry restore, forward recovery, or `RECOVERY_REQUIRED`/cold restart.

### C09 — Checkpoint Durability
`LOCAL → PERSISTING → DURABLE → VALIDATED`. `DURABLE` requires upload + checksum verification + metadata commitment. `VALIDATED` requires restore + Validator success. `DURABLE != VALIDATED`.

### C10 — Checkpoint Retention
Keep at least N recent durable checkpoints. Active-migration references are retention-locked. GC cannot delete locked checkpoints.

### C11 — Candidate & Plan Freshness
Plans contain `created_at`, `expires_at`, and `input_snapshot_versions`. Expired plans cannot execute irreversible operations. Stale/not-ready candidates cause `ABORT → REPLAN`.

### C12 — Decision Precedence
For one job: `EMERGENCY > PROACTIVE > ARBITRAGE`. This is not fleet scheduling.

### C13 — Failure Taxonomy
Shared enum: `WORKLOAD_INVALID`, `RUNTIME_INCOMPATIBLE`, `CHECKPOINT_FAILED`, `CHECKPOINT_CORRUPTED`, `TRANSFER_FAILED`, `PROVISION_FAILED`, `AUTHORIZATION_FAILED`, `QUOTA_EXCEEDED`, `CAPACITY_UNAVAILABLE`, `RESTORE_FAILED`, `VALIDATION_FAILED`, `FENCING_FAILED`, `DEADLINE_EXCEEDED`, `CONTROL_PLANE_UNAVAILABLE`, `UNKNOWN_OUTCOME`. Each centrally defines retryable/recoverable/replan/reconciliation/terminal semantics.

### C14 — Reconciliation
Two triggers: event-driven anomalies and periodic discovery/orphan sweeps. Reconciliation detects, classifies, persists, escalates, and requests remediation; it does not directly execute infrastructure mutations.

### C15 — Security
Credentials are references, not migration-owned state. Targets resolve secrets through authorized IAM. Runtime artifacts are digest-pinned and must be available at the destination.

### C16 — Model & Configuration Versioning
Every decision records estimator-model, risk-model, and policy-config versions. Versions are immutable after deployment.

### C17 — Observability & Audit
Operational telemetry is asynchronous/best-effort. Decision audit is durable control-plane state containing inputs, decision, reason, versions, plan, regime, and constraints.

### C18 — Confidence
`observation_confidence` and `prediction_confidence` are distinct semantic quantities and must not share generic threshold logic.

### C19 — Workload Security Boundary
V2 prefers self-contained CRIU workloads without privileged in-workload orchestration agents. Control-plane privileges remain outside the workload where possible.

### C20 — Control-Plane Boundedness
At minimum, one active migration per job and bounded global control operations.

### C21 — Cross-Job Scope
Policy precedence and migration coordination are per-job. V2 does not implement fleet scheduling, cross-job fairness, diversification, recovery-storm management, or global capacity optimization.

## 4. Contract Dependency Matrix

Legend: **A** = authority/owner, **P** = primary producer, **C** = consumer/participant, **E** = enforcement/execution.

| Contract | Authority | Primary Producers | Primary Consumers / Participants |
|---|---|---|---|
| C01 Identity | Registry for `job_id`/`version`; Registry+Coordinator for epoch transitions | Registry, Coordinator, command/event producers | All 16 |
| C02 Time | Market/Risk Monitor for emergency deadline; Coordinator for remaining budget | Market/Risk Monitor | Estimator, Infrastructure Monitor, Feasibility, Policy, Planner, Coordinator, Validator, Reconciliation, History, Observability |
| C03 State Ownership | Migration Coordinator | Coordinator | Registry, Planner, Validator, Reconciliation, History |
| C04 Event | Event producers + durable consumer ledgers | Market/Risk Monitor, Infrastructure Monitor, execution components | Registry, Policy, Coordinator, Reconciliation, Observability |
| C05 Command Idempotency | Command/execution boundary | Coordinator/command originators | Provisioner, CRIU Manager, Validator, Reconciliation |
| C06 Migration Transaction | Migration Coordinator | Planner creates plan; Coordinator advances transaction | Registry, Planner, Coordinator, Provisioner, CRIU, Validator, Reconciliation, History |
| C07 Fencing | Planner authorizes; Coordinator executes | Planner | Coordinator, Registry, CRIU/Worker Controller, Reconciliation, Validator |
| C08 Abort & Rollback | Coordinator state machine | Planner defines plan semantics | Coordinator, Provisioner, CRIU, Validator, Reconciliation |
| C09 Checkpoint Durability | CRIU layer for bytes; Registry for authoritative references | CRIU Manager | Registry, Estimator, Feasibility, Planner, Coordinator, Validator, Reconciliation, History |
| C10 Checkpoint Retention | Checkpoint/cleanup subsystem | CRIU Manager / Coordinator | Registry, Coordinator, Reconciliation, History |
| C11 Candidate & Plan Freshness | Planner for plan validity; observation providers for snapshot freshness | Market/Risk Monitor, Infrastructure Monitor, Compatibility, Planner | Compatibility, Feasibility, Cost/Risk, Policy, Planner, Coordinator |
| C12 Decision Precedence | Policy Engine | Policy Engine | Planner, Coordinator, Registry |
| C13 Failure Taxonomy | Central architectural contract | Execution components | Coordinator, Planner, Policy, Reconciliation, History, Observability, Registry |
| C14 Reconciliation | Reconciliation Manager | Infrastructure Monitor, Validator, Coordinator, periodic sweeps | Reconciliation, Policy, Planner, Coordinator, Registry, History |
| C15 Security | Workload/Runtime Contract + platform authorization | Registry/Admission, Compatibility, Provisioner | Compatibility, Provisioner, CRIU, Coordinator, Validator |
| C16 Model/Config Versioning | Model/config deployment system | Estimator, Risk Model, Policy configuration | Cost/Risk, Policy, Planner, History, Audit |
| C17 Observability/Audit | Observability/Audit layer; Policy/transaction owns durable decision record | All components emit telemetry; Policy/Coordinator emit audit | All components, operators/audit consumers |
| C18 Confidence | Producing component | Estimator, Market/Risk Monitor, Infrastructure Monitor, Compatibility | Feasibility, Cost/Risk, Policy, Planner |
| C19 Workload Security Boundary | Workload Contract / Admission | Registry/Admission | Compatibility, Provisioner, CRIU, Coordinator, Validator |
| C20 Control-Plane Boundedness | Coordinator/control plane | Policy, Planner | Coordinator, Provisioner, Reconciliation |
| C21 Cross-Job Scope | Architecture invariant | Policy/Planner enforce per-job scope | Policy, Planner, Coordinator, Provisioner |

## 5. Component × Contract Matrix

Legend: `●` = direct/important dependency, `○` = secondary/reference dependency, `—` = no material dependency.

| Component | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | C9 | C10 | C11 | C12 | C13 | C14 | C15 | C16 | C17 | C18 | C19 | C20 | C21 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 Registry | ● | ○ | ● | ● | ○ | ● | ● | ○ | ● | ● | ○ | ○ | ● | ● | ● | ○ | ● | ○ | ● | ○ | ○ |
| 2 Estimator | ● | ● | ○ | ○ | ○ | ○ | — | — | ● | ○ | ● | — | ● | ○ | ○ | ● | ● | ● | ● | — | — |
| 3 Market/Risk Monitor | ● | ● | — | ● | — | — | — | — | — | — | ● | ● | ● | — | ○ | ● | ● | ● | — | ○ | ● |
| 4 Infrastructure Monitor | ● | ● | — | ● | — | — | — | — | — | — | ● | — | ● | ● | ● | — | ● | ● | ● | ● | ○ |
| 5 Compatibility | ● | ● | — | ○ | — | — | — | — | ● | — | ● | — | ● | ○ | ● | ○ | ● | ● | ● | — | — |
| 6 Recovery Feasibility | ● | ● | — | ○ | — | — | — | — | ● | — | ● | — | ● | ○ | ○ | ○ | ● | ● | — | ○ | — |
| 7 Cost/Risk Evaluator | ● | ● | — | ○ | — | — | — | — | ○ | — | ● | — | ● | — | ○ | ● | ● | ● | — | — | ● |
| 8 Policy Engine | ● | ● | — | ● | — | ○ | — | — | ○ | — | ● | ● | ● | ○ | ○ | ● | ● | ● | — | ○ | ● |
| 9 Planner | ● | ● | ● | ○ | ● | ● | ● | ● | ● | ○ | ● | ● | ● | ○ | ● | ○ | ● | ● | ● | ● | ● |
| 10 Coordinator | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ○ | ● | ○ | ● | ● | ● |
| 11 Provisioner | ● | ● | ○ | ○ | ● | ● | ○ | ● | ○ | ○ | ● | — | ● | ○ | ● | ○ | ● | — | ● | ● | — |
| 12 CRIU Manager | ● | ● | ● | ○ | ● | ● | ● | ● | ● | ● | ○ | — | ● | ○ | ● | ○ | ● | — | ● | ● | — |
| 13 Validator | ● | ● | ● | ○ | ● | ● | ● | ● | ● | ○ | ● | — | ● | ● | ● | ○ | ● | ○ | ● | — | — |
| 14 Reconciliation | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | — | ● | ● | ● | ○ | ● | ○ | ● | ● | — |
| 15 Migration History | ● | ○ | ○ | ○ | ○ | ● | ○ | ○ | ● | ● | ○ | ○ | ● | ● | ○ | ● | ● | ○ | — | — | — |
| 16 Observability/Audit | ● | ● | ○ | ● | ○ | ● | ○ | ○ | ● | ○ | ● | ● | ● | ● | ○ | ● | ● | ● | ○ | ○ | ○ |

## 6. Primary Dependency Flow

```text
1 Job Registry
      |
      v
2 Workload Estimator
      |
      +----------------------+
      |                      |
      v                      v
5 Candidate Compatibility   3 Market/Risk Monitor
      |                      |
      +----------+-----------+
                 v
        6 Recovery Feasibility
                 |
                 v
        7 Cost & Risk Evaluator
                 |
                 v
          8 Policy Engine
                 |
                 v
          9 Migration Planner
                 |
                 v
        10 Migration Coordinator
             /      |       \
            v       v        v
      Provisioner  CRIU   Validator
             \      |       /
              \     |      /
                 v
          14 Reconciliation
                 |
                 v
            Policy/Planner
```

## 7. Feedback Loops

### Economic learning

```text
Migration
    ↓
Migration History
    ├──→ Workload Estimator
    └──→ Risk Model
```

### Reconciliation

```text
Infrastructure / Validator
          ↓
Reconciliation
          ↓
Policy
          ↓
Planner
          ↓
Coordinator
```

### Observability

```text
All Components
      ↓
Observability / Audit
```

Operational telemetry is asynchronous; durable decision audit is control-plane state.

## 8. Ownership Rules

1. Registry owns logical identity and Registry concurrency, not every identifier.
2. Planner authorizes; Coordinator executes.
3. CRIU/checkpoint layer owns checkpoint bytes; Registry owns authoritative references.
4. Policy decides; Planner specifies execution.
5. Reconciliation detects and requests remediation; it does not bypass Policy/Planner/Coordinator.
6. Validator determines transactional migration correctness; Reconciliation determines ongoing expected-vs-observed consistency.
7. Observability observes; durable audit records decisions as control-plane state.

## 9. Component-Spec Requirement

Every component specification must contain an **Applicable Contracts** section referencing these IDs.

The component specification must explain how it implements or consumes the contract; it must not redefine contract semantics.

## 10. Next Artifact

The next architecture artifact is:

**Migration Coordinator — State Transition & Abort-Safety Matrix**

For every Coordinator state it must define:

- entry conditions
- exit conditions
- authoritative owner
- durable state
- external side effects
- abort semantics
- rollback behavior
- cleanup
- retry semantics
- timeout
- emergency deadline behavior
- fencing implications
- Reconciliation behavior
