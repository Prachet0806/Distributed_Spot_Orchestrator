# Distributed Spot Arbitrage Orchestrator — Product Requirements Document

**Version:** V2  
**Status:** Architecture-aligned draft  
**Date:** 2026-08-24

## 1. Product Overview

A production-grade control plane for reducing compute cost by intelligently moving long-running checkpointable workloads between compute capacity. V2 targets self-contained workloads that can be directly frozen/restored with CRIU and have no external application dependencies.

Primary objectives:
1. Spot cost optimization.
2. Recovery from involuntary Spot interruption.

Target completion-time overhead: **≤ ~20%**.

## 2. Problem Statement

Blindly moving workloads to cheaper Spot capacity can increase total cost or reduce reliability because migration has checkpoint, persistence, transfer, provisioning, restore, validation, capacity, interruption-risk, IAM, quota, and network costs.

The system therefore keeps two questions separate:

- **Economic:** Is migration economically worthwhile?
- **Recovery:** Can recovery reach a compatible, ready environment within the interruption deadline?

## 3. Goals

- Reduce expected compute cost through Spot arbitrage.
- Recover interrupted workloads when technically feasible.
- Preserve execution correctness.
- Keep completion-time overhead around or below 20%.
- Support self-contained CRIU-compatible workloads.
- Provide durable state, idempotency, fencing, checkpoint integrity, reconciliation, auditability, and deadline enforcement.

## 4. Non-Goals

V2 does not initially support workloads requiring external application state, multi-service transactional checkpoints, application-level state migration, or unsupported CRIU behavior.

V2 also defers fleet-wide scheduling, cross-job fairness, diversified allocation, multi-target speculative provisioning, recovery-storm optimization, correlated interruption optimization, exactly-once execution, full HA/leader election, and full artifact-signing infrastructure.

## 5. Target Users

- Platform/infrastructure engineers.
- Workload/application owners.
- Operations/reliability teams.

## 6. Core Scenarios

### 6.1 Routine Spot Arbitrage

```text
Workload
  ↓
Market observation + workload estimation
  ↓
Compatibility + readiness
  ↓
Recovery feasibility
  ↓
Cost/risk evaluation
  ↓
Arbitrage policy
  ↓
Migration plan
  ↓
Execution
  ↓
Validation
  ↓
History/audit
```

### 6.2 Spot Interruption Recovery

```text
Interruption event
  ↓
Deadline established
  ↓
Emergency policy
  ↓
Compatible + READY candidate
  ↓
Recovery feasibility
  ↓
Plan
  ↓
Fast execution
  ↓
Fencing → Restore → Validate
```

Emergency events bypass routine market-polling freshness delays.

### 6.3 No Feasible Recovery

If no candidate satisfies the emergency requirements, the system must not fabricate feasibility. It enters the defined recovery-required path, normally using the latest valid durable checkpoint or cold restart semantics defined by the workload/recovery policy.

### 6.4 Failed Migration

A failed migration must not leave a job indefinitely marked as migrating. Failure is classified, retry/abort semantics are applied, dangling state is cleared, and reconciliation handles mismatches.

### 6.5 Unexpected Infrastructure State

Registry/infrastructure mismatches enter Reconciliation. Reconciliation requests remediation through the normal Policy → Planner → Coordinator path and does not directly mutate infrastructure.

## 7. Functional Requirements

### FR-1 Job Registry

Maintain authoritative durable state including `job_id`, Registry `version`, `execution_epoch`, lifecycle state, workload profile/contract, execution information, active migration, and checkpoint references.

### FR-2 Workload Eligibility

Represent eligibility for CRIU checkpointing, self-contained execution, reconnectability, and unsupported external dependencies.

### FR-3 Workload Estimation

Provide progress, remaining runtime, checkpoint-size estimate, checkpoint-duration estimate, and prediction confidence. Support insufficient-confidence results and re-baselining after material execution-context changes.

### FR-4 Market/Risk Observation

Observe Spot prices, price changes, volatility, interruption events, and interruption-risk/frequency evidence. Interruption notices require an immediate path to recovery.

### FR-5 Infrastructure Observation

Observe instance/resource state, capacity evidence, runtime readiness, and anomalies. Infrastructure Monitor must not execute provisioning merely to probe capacity.

### FR-6 Candidate Compatibility

Return `COMPATIBLE`, `INCOMPATIBLE`, or `UNKNOWN`. Emergency recovery considers only `COMPATIBLE + READY` candidates.

### FR-7 Recovery Feasibility

Determine whether checkpoint, persistence/transfer, provisioning, restore, readiness, quota, network/IP, and other required steps fit the available deadline.

### FR-8 Economic Evaluation

Evaluate source/target compute, remaining runtime, migration overhead, checkpoint storage, transfer cost, interruption risk, and expected savings. Price and risk remain separate.

### FR-9 Hysteresis

Prevent migration thrashing through minimum savings margins, cooldowns, and/or persistence of favorable conditions across observations.

### FR-10 Short-Job Economics

Do not migrate short jobs merely because target compute is cheaper. Account for migration overhead and expected savings.

### FR-11 Policy Precedence

For a single job:

```text
EMERGENCY > PROACTIVE > ARBITRAGE
```

### FR-12 Migration Planning

Create plans containing migration identity, source/target, regime, deadline, snapshot/version references, ordered steps, checkpoint requirements, fencing authorization, cleanup, expiration, and relevant model/configuration versions.

### FR-13 Plan Expiration

Plans contain `created_at`, `expires_at`, and `input_snapshot_versions`; expired plans cannot execute irreversible operations.

### FR-14 Idempotent Commands

Every infrastructure/control command carries `operation_id`. Retries reuse it. `UNKNOWN` outcomes must be resolved before retry.

### FR-15 Migration State Machine

The Coordinator executes:

```text
PLANNED → PRECHECKING → CHECKPOINTING → PERSISTING →
PROVISIONING → TRANSFERRING → RESTORING → FENCING →
VALIDATING → ACTIVATING → FINALIZING
```

with explicit failure/recovery states.

### FR-16 Deadline Enforcement

For emergency recovery, the absolute deadline is computed once at interruption ingestion, stored in the plan, and enforced by the Coordinator using monotonic elapsed-time tracking.

### FR-17 Fencing

Confirmed fencing requires both control-plane ownership invalidation and confirmed source-process termination. Fencing must be pre-authorized by the Migration Plan.

### FR-18 Abort/Rollback

Before confirmed fencing, rollback to source is allowed. After confirmed fencing, rollback to the old source is prohibited; only forward recovery, restore retry, cold restart, or reconciliation applies.

### FR-19 Checkpoint Integrity

Verify checkpoint integrity before restore. Distinguish `DURABLE` from `VALIDATED`.

### FR-20 Checkpoint Retention

Retain the required checkpoint floor and lock checkpoints referenced by active migrations.

### FR-21 Validation

Validator determines restored-workload correctness. Validation may use tolerance-bounded equivalence where strict bitwise determinism is inappropriate.

### FR-22 Reconciliation

Support event-driven anomaly detection and periodic discovery/orphan sweeps for stale ownership, orphan resources, post-migration mismatches, and fencing inconsistencies.

### FR-23 Failure Classification

Use the shared failure taxonomy with centrally defined retry, replan, recovery, reconciliation, and terminal semantics.

### FR-24 Security

Credentials are references rather than checkpoint state. Target IAM/KMS and runtime-artifact readiness must be verified before migration.

### FR-25 Audit

Every migration decision must be durably auditable, including job, execution epoch, migration, decision, reason, inputs, model/configuration versions, plan, and outcome.

## 8. Non-Functional Requirements

- **Correctness:** never knowingly allow two authoritative executions of one job.
- **Idempotency:** retries must not unintentionally duplicate infrastructure operations.
- **Recoverability:** transient failures should be safely recoverable where semantics allow.
- **Observability:** correlate operations using `job_id`, `migration_id`, `execution_epoch`, `operation_id`, and `correlation_id`.
- **Auditability:** decision history must survive telemetry-system failure.
- **Deadline awareness:** emergency decisions must account for remaining budget.
- **Bounded concurrency:** at most one active migration per job; global control operations are bounded.
- **Security:** checkpoints must not intentionally contain credential material.
- **Decision reproducibility:** recorded inputs/model/configuration versions should permit reconstruction of decisions.

## 9. Cross-Cutting Contracts

V2 defines 21 contracts:

1. Identity
2. Time
3. State Ownership
4. Event
5. Command Idempotency
6. Migration Transaction
7. Fencing
8. Abort & Rollback
9. Checkpoint Durability
10. Checkpoint Retention
11. Candidate & Plan Freshness
12. Decision Precedence
13. Failure Taxonomy
14. Reconciliation
15. Security
16. Model & Configuration Versioning
17. Observability & Audit
18. Confidence Semantics
19. Workload Security Boundary
20. Control-Plane Boundedness
21. Cross-Job Scope Boundary

These are defined in the V2 Contracts, Components & Dependency Matrix and are normative for component specifications.

## 10. Architecture

The 16 components are:

```text
1  Job Registry
2  Workload Estimator
3  Market/Risk Monitor
4  Infrastructure Monitor
5  Candidate Compatibility
6  Recovery Feasibility
7  Cost & Risk Evaluator
8  Policy Engine
9  Migration Planner
10 Migration Coordinator
11 Provisioner
12 CRIU / Checkpoint-Restore Manager
13 Validator
14 Reconciliation Manager
15 Migration History
16 Observability / Audit
```

Primary decision path:

```text
Job Registry
     ↓
Workload Estimator
     ↓
Candidate Compatibility
     ↓
Recovery Feasibility
     ↓
Cost & Risk Evaluator
     ↓
Policy Engine
     ↓
Migration Planner
     ↓
Migration Coordinator
   ↙      ↓       ↘
Provisioner  CRIU  Validator
              \     /
               Reconciliation
                    ↓
              Policy / Planner
```

Market/Risk Monitor and Infrastructure Monitor provide observation inputs to the decision path. Migration History provides feedback for estimation/risk learning. Observability/Audit spans the entire system.

## 11. Success Criteria

V2 should demonstrate:

- profitable migration of an eligible long-running workload;
- rejection of uneconomic migrations;
- rejection of uneconomic short-job migrations;
- deadline-aware Spot interruption recovery;
- explicit recovery-infeasible behavior;
- correct fencing and no split-brain execution;
- safe idempotent retry after unknown infrastructure outcomes;
- reconciliation of Registry/infrastructure mismatches;
- complete durable migration auditability.

## 12. Acceptance Criteria

- [ ] CRIU-compatible self-contained workload eligibility is enforced.
- [x] (emu) Job Registry is authoritative.
- [x] `version` and `execution_epoch` are distinct.
- [x] (emu) Migration and operation identities are durable.
- [x] (emu) Commands are idempotent.
- [x] (emu) Unknown command outcomes are safely resolved.
- [x] (emu) Interruption events are deduplicated.
- [x] (emu) Emergency deadlines are computed once and enforced centrally.
- [ ] Candidate readiness is validated.
- [x] Compatibility and recovery feasibility remain separate.
- [x] Migration costs and interruption risk enter economics.
- [x] Hysteresis prevents thrashing.
- [x] Plans expire.
- [x] Fencing is pre-authorized and termination-confirmed.
- [x] Abort semantics follow the state-indexed rules.
- [x] Checkpoint integrity is verified.
- [x] Durable and validated checkpoints are distinct.
- [x] Validator validates restored workloads.
- [x] Failed migrations cannot leave dangling active migration state.
- [x] Reconciliation supports event and periodic discovery paths.
- [x] (emu) Reconciliation cannot bypass Policy/Planner/Coordinator.
- [x] (emu) Shared failure taxonomy is used.
- [ ] IAM/KMS/artifact readiness is checked.
- [ ] Credentials are not checkpoint state.
- [ ] Decision/model/configuration versions are persisted.
- [x] (emu) Durable audit records exist.
- [x] (emu) Migration telemetry is correlated.
- [x] At most one active migration exists per job.

### Acceptance evidence (2026-09, emulated)

Checked boxes are verified by the suites cited; unchecked boxes are tracked
in `docs/IMPLEMENTATION_STATUS.md` with owning prod-checklist tracks.
Verdict scope is emulated unless noted — no box claims live-AWS proof.
Boxes marked `[x] (emu)` are verified emulated with live proof still pending;
plain `[x]` predates the two-column convention and carries the same meaning.

| Box | Evidence |
|---|---|
| version ≠ epoch | `tests/test_transition_stores.py` (separate bump rules, epoch-conflict rejection) |
| compatibility ≠ feasibility | engine separation + `tests/test_e2e_emulated.py` ordering |
| costs + risk in economics | `CostRiskEvaluator` breakdown incl. `expected_risk_cost` |
| hysteresis | `tests/test_policy_v2.py` (streaks, cooldown, DEFER reasons) |
| plans expire | planner TTL + expiry gate + `tests/test_execution_kernel.py` |
| fencing pre-auth + confirmed | coordinator dual-evidence + fail-closed hooks, kernel tests |
| abort semantics | rollback-class handlers + pre/post-fence tests |
| checkpoint integrity | S3 SHA-256 + manifest + restore gate, transition-stores tests |
| DURABLE ≠ VALIDATED | store monotonicity + validator mapping, tests |
| validator validates | L1–L4 + verdicts + epoch/lineage, `tests/test_correctness.py`; L1/L2 transport-backed probes wired in prod path, `tests/test_validator_probes.py` |
| no dangling migration | terminal clearing + sweeps, kernel/correctness tests |
| reconciliation paths | event + sweep triggers without bypass, `tests/test_correctness.py` |
| one active migration | registry guard + conflict tests |
| interruption dedup | stable `event_id` + ledger gate, `tests/test_interruption_ingestion.py` (S21) |
| deadline-once + enforced | ingestion-lane computation + coordinator monotonic budget, `tests/test_interruption_ingestion.py`, `tests/test_deadlines.py` |
| UNKNOWN resolution | timeout→UNKNOWN + probe resolution + reconciliation escalation, `tests/test_step_retry.py` |
| command idempotency | ULID sole minter + op-ID reuse, `tests/test_execution_kernel.py`, provisioner replay |
| durable identities | ULID migration/operation IDs + plan/ledger stores, kernel + transition-stores tests |
| recon no-bypass | remediation callback + pre-auth-only cleanups, `tests/test_correctness.py` |
| failure taxonomy | `CodedError` + §10.8 retry gating, `tests/test_step_retry.py` |
| durable audit | fail-closed fence gate + terminal records, `tests/test_audit_gate.py` |
| telemetry correlation | `correlation_id` envelope on audit records, `tests/test_audit_gate.py` |
| registry authority | CAS sole mutation path, `tests/test_transition_stores.py` |

## 13. Future Evolution

The architecture is intended to evolve toward broader workload classes, external dependency contracts, richer data-locality modeling, application-aware checkpoint consistency, diversified Spot pools, speculative provisioning, fleet scheduling, correlated interruption modeling, recovery-storm handling, HA/leader election, exactly-once semantics, stronger artifact attestation, ML-based workload/risk models, and richer billing/network-cost models.

These extensions must preserve the V2 separation of concerns.

## 14. Architectural Guardrails

1. Observation does not make decisions.
2. Estimation does not make decisions.
3. Compatibility does not select candidates.
4. Economics does not enforce policy.
5. Policy does not execute infrastructure.
6. Planner authorizes; Coordinator executes.
7. Coordinator does not invent a migration objective.
8. Coordinator has no discretionary fencing authority.
9. Validator does not own ongoing reconciliation.
10. Reconciliation does not directly execute remediation.
11. Registry remains authoritative for logical job state.
12. `version` is not a fencing mechanism.
13. `execution_epoch` is not a Registry concurrency version.
14. `UNKNOWN` is never silently treated as success.
15. Missing information is never silently converted to zero risk/cost.
16. Emergency recovery uses only `COMPATIBLE + READY` candidates.
17. Expired plans cannot execute irreversible operations.
18. Confirmed fencing is the rollback boundary.
19. Durable checkpoint state is not equivalent to validated application state.
20. Operational telemetry cannot become an implicit control-plane dependency.

## 15. Next Engineering Artifacts

1. Migration Coordinator State Transition & Abort-Safety Matrix
2. Coordinator ↔ Provisioner interface
3. Coordinator ↔ CRIU/Checkpoint-Restore interface
4. Coordinator ↔ Validator interface
5. Coordinator ↔ Reconciliation protocol
6. Component 1 — Job Registry 17-point specification
7. Component 2 — Workload Estimator 17-point specification
8. Components 3–16 sequentially
9. Integration contracts
10. Failure-injection test plan
11. End-to-end V2 acceptance test plan

The PRD defines **what the system must accomplish**. The architecture and component specifications define **how responsibilities are divided and implemented**.
