# Protocols.md — Production-Grade V2 Protocol & Cross-Cutting Semantics

**Status:** V2 Architectural Freeze Baseline  
**Scope:** Protocols #1–#16 and cross-cutting semantics  
**System:** CRIU-based, self-contained workload migration and Spot-arbitrage platform  
**V2 workload constraint:** workloads must be directly freezeable/restorable by CRIU and have no external application dependencies.

> **Normative rule:** Individual component specifications MUST conform to this document. A component MUST NOT introduce semantics that contradict it.

---

## 0. Architectural Constitution

### 0.1 Decision ≠ Planning ≠ Execution

```text
Evidence → Policy/Decision → Planner → Coordinator → Executor
```

- **Policy** decides *what should happen*.
- **Planner** decides *how it should happen*.
- **Coordinator** executes an approved plan.
- **Execution components** perform infrastructure/process operations.

No component may silently collapse these responsibilities.

### 0.2 Observation ≠ Authority

Market, infrastructure, workload, and telemetry components produce evidence. Observation does not itself mutate authoritative state.

### 0.3 Missing ≠ Favorable

Unknown, unavailable, stale, or insufficient-confidence data MUST NOT be converted into an optimistic value.

Examples:

```text
unknown capacity ≠ available capacity
unknown price ≠ zero price
unknown checkpoint duration ≠ zero duration
unknown risk ≠ low risk
```

### 0.4 Four Independent State Domains

**Job state**

```text
REGISTERED → READY → RUNNING → MIGRATING
                              ├→ RECONCILIATION_REQUIRED
                              ├→ RECOVERY_REQUIRED
                              ├→ RESTART_REQUIRED
                              └→ FAILED
```

**Migration state**

```text
PLANNED → PRECHECKING → CHECKPOINTING → PERSISTING
→ PROVISIONING → TRANSFERRING → RESTORING → FENCING
→ VALIDATING → ACTIVATING → FINALIZING → SUCCESS
```

Terminal alternatives:

```text
ABORTED | SUPERSEDED | FAILED
```

**PlanStep state**

```text
PENDING → RUNNING → SUCCEEDED
                  ├→ FAILED
                  └→ UNKNOWN
SKIPPED
```

**Operation state**

```text
ISSUED → RUNNING → SUCCEEDED
                 ├→ FAILED
                 └→ UNKNOWN
```

These domains MUST NOT be conflated.

---

**Cross-Protocol Semantic Freeze — Normative Decisions 1–12.**
The following twelve decisions are frozen and normative for every protocol in this document.

### 0.5 Semantic Vocabulary (Decision 1)

| Term | Definitive Meaning | Owner |
|------|-------------------|-------|
| `ABORT` | Stop the current migration attempt and clean up | Coordinator |
| `REPLAN` | Objective remains valid, current plan is no longer executable | Recovery Policy → Planner |
| `SUPERSEDED` | Migration/plan replaced by a newer migration/plan | Planner/Coordinator lifecycle |
| `FAILED` | Migration cannot complete under the applicable policy | Migration lifecycle |
| `RECOVERY_REQUIRED` | Workload requires recovery from an existing durable checkpoint | Recovery Policy |
| `RESTART_REQUIRED` | No usable durable checkpoint; cold restart required | Recovery Policy |
| `RECONCILIATION_REQUIRED` | Authoritative execution state is uncertain | Reconciliation |
| `UNKNOWN` | External operation outcome cannot yet be determined | Operation protocol |
| `PARTIAL_RESTORE` | Restore produced an incomplete execution environment | CRIU/Checkpoint |
| `DEADLINE_EXCEEDED` | Failure condition indicating the applicable execution deadline was missed | Failure taxonomy |

**Critical rules:**
- `REPLAN` is **not** `ABORT`. `REPLAN` = objective survives, current plan dies, new plan required.
- `ABORT` = current migration ends, objective is not automatically continued.
- `REPLAN` → new `MigrationPlan` → old plan = `SUPERSEDED`.
- `SUPERSEDED` is a lifecycle result, not a RecoveryDecision.
- `RECONCILIATION_REQUIRED` is a gate, not a RecoveryDecision.

### 0.6 Four Independent State Domains (Decision 2)

**Job state**
```text
REGISTERED → READY → RUNNING → MIGRATING
                              ├→ RECONCILIATION_REQUIRED
                              ├→ RECOVERY_REQUIRED
                              ├→ RESTART_REQUIRED
                              └→ FAILED
```

**Migration state**
```text
PLANNED → PRECHECKING → CHECKPOINTING → PERSISTING
→ PROVISIONING → TRANSFERRING → RESTORING → FENCING
→ VALIDATING → ACTIVATING → FINALIZING → SUCCESS
```

Terminal alternatives:
```text
ABORTED | SUPERSEDED | FAILED
```

**PlanStep state**
```text
PENDING → RUNNING → SUCCEEDED
                  ├→ FAILED
                  └→ UNKNOWN
SKIPPED
```

**Operation state**
```text
ISSUED → RUNNING → SUCCEEDED
                 ├→ FAILED
                 └→ UNKNOWN
```

These domains MUST NOT be conflated.

### 0.7 UNKNOWN Is an Operation Outcome (Decision 3)

`UNKNOWN` does **not** mean:
- failure
- retry
- checkpoint durability state
- success

It means: **We don't know what happened externally.**

```text
COMMAND
  │
  ▼
UNKNOWN
  │
  ▼
GetOperationStatusQuery
  │
  ├── SUCCEEDED
  ├── FAILED
  └── UNKNOWN
        │
        ▼
   escalation/reconciliation
```

**Never:** `UNKNOWN → blind retry`

Checkpoint durability remains:
```text
LOCAL → PERSISTING → DURABLE → VALIDATED
```
No `UNKNOWN` checkpoint durability state.

### 0.8 Command / Query / Event / State Semantics (Decision 4)

**Command** — requests a side effect
```yaml
Command:
  command_id:
  operation_id:
  job_id:
  migration_id:
  execution_epoch:
  correlation_id:
  deadline:
  payload:
```

**Query** — read-only request
```yaml
Query:
  query_id:
  query_type:
  correlation_id:
  job_id:
  migration_id:
  issued_at:
  deadline:
  parameters:
```

**Event** — something happened
```yaml
Event:
  event_id:
  event_type:
  job_id:
  migration_id:
  execution_epoch:
  effective_at:
  observed_at:
  producer:
  correlation_id:
  payload:
```

### 0.9 Registry CAS Is the Sole State Mutation Path (Decision 5)

No component may arbitrarily mutate authoritative Registry state.

```
Component
    │
    ▼
TransitionRequest
    │
    ▼
Coordinator
    │
    ▼
Registry.transition()
    │
    ├── expected version
    ├── expected epoch
    ├── legal transition
    ├── active migration check
    └── atomic CAS
```

### 0.10 `version` vs `execution_epoch` (Decision 6)

| Field | Purpose |
|-------|---------|
| `version` | Registry optimistic-concurrency version |
| `execution_epoch` | execution ownership/fencing generation |

Normal mutation: `version++`, epoch unchanged.  
Ownership transition: `version++`, `epoch++` (atomic).

### 0.11 Ownership Transfer Protocol (Decision 7)

```
SOURCE
  │
  ▼
TRANSITIONING
  │
  ├─ invalidate old epoch
  ├─ fence source
  ├─ confirm source termination
  ├─ establish target ownership
  └─ atomic Registry transition
  │
  ▼
TARGET
```

Critical invariant: **There must never be a durable authoritative state where both source and target are simultaneously owners.**  
If fencing cannot establish certainty: `FENCING_FAILED` → `RECONCILIATION_REQUIRED`.

### 0.12 Recovery Policy → Planner Boundary (Decision 8)

```
Recovery Policy
      │
      │ RecoveryDecision
      ▼
   Planner
      │
      │ MigrationPlan
      ▼
Coordinator
      │
      ▼
Executors
```

Recovery Policy **never executes**.  
Planner **never executes**.  
Coordinator **never decides recovery strategy**.

### 0.13 Reconciliation Is a Gate, Not a Decision (Decision 9)

```
Finding
   │
   ▼
Reconciliation
   │
   ├── ownership certain
   │       ▼
   │    continue
   │
   └── ownership uncertain
         ▼
RECONCILIATION_REQUIRED
      │
      ▼
resolve ownership
      ▼
Recovery Policy
```

Recovery Policy MUST NOT decide while ownership is uncertain.

### 0.14 Formal PlanStep Contract (Decision 10)

```yaml
PlanStep:
  step_id:
  type:
  depends_on:

  state: PENDING | RUNNING | SUCCEEDED | FAILED | UNKNOWN | SKIPPED
  operation_id:
  timeout_seconds:
  retry_budget:
  criticality:
  priority:
  rollback_class:
  resource_requirements:
  deadline:
  failure_injection_points:
```

V2 join policy: `ALL_SUCCEEDED` only. `ANY_SUCCEEDED` and `QUORUM` reserved.

### 0.15 Compatibility ≠ Readiness ≠ Placement (Decision 11)

```text
Workload Contract
      │
      ▼
Compatibility
      │
      ▼
Readiness
      │
      ▼
Placement
```

### 0.16 Emergency Is a Distinct Execution Model (Decision 12)

```text
                 Migration
                    │
           ┌────────┴────────┐
           │                 │
        NORMAL            EMERGENCY
           │                 │
      normal DAG        deadline-aware DAG
      normal cadence   fast-path observations
      economic goal    survival goal
      serial where     parallel where
      appropriate      safe
```

---

# 1. Universal Identity & Versioning

| Field | Meaning | Owner |
|---|---|---|
| `job_id` | immutable logical workload identity | Registry |
| `version` | Registry optimistic-concurrency version | Registry |
| `execution_epoch` | execution ownership/fencing generation | ownership protocol |
| `migration_id` | migration attempt identity | migration lifecycle |
| `engine_version` | engine selected at admission (`V1 \| V2`); immutable for the migration attempt | admission |
| `plan_id` | plan identity | Planner |
| `plan_version` | plan revision (integer; normally `1`; replanning creates a new `plan_id`) | Planner |
| `plan_hash` | SHA-256 over canonicalized JSON of immutable plan content (excludes volatile fields: timestamps, step runtime state) | Planner |
| `step_id` | PlanStep identity | Planner |
| `operation_id` | individual side-effect identity — **ULID** (time-ordered, lexicographically sortable) | Coordinator |
| `event_id` | event deduplication identity | event producer |
| `correlation_id` | tracing/audit correlation | control plane |
| `checkpoint_id` | checkpoint identity | Checkpoint subsystem |
| `checkpoint_lineage_id` | checkpoint ancestry | Checkpoint subsystem |

Rules:

1. Every side-effecting command carries `operation_id` (ULID).
2. Every event carries `event_id`.
3. Every migration-scoped durable record carries `job_id`, `migration_id`, and `execution_epoch`.
4. `version` MUST NOT be used as a fencing token.
5. `execution_epoch` MUST NOT be used as a generic write version.
6. `operation_id` is generated by the Coordinator and passed to executors; executors MUST NOT generate replacement IDs for the same logical operation.
7. Retries of the same logical operation reuse the same `operation_id`; genuinely new operations receive new IDs.
8. `plan_hash` covers only canonicalized immutable plan content; volatile fields (`created_at`, execution timestamps, step runtime state) are excluded.
9. `engine_version` is fixed at admission. A V2 migration that fails follows V2 failure semantics (`FAILED` / `RECOVERY_REQUIRED` / `RESTART_REQUIRED`); silent substitution with V1 is prohibited.

---

# 2. Time Semantics

## 2.1 Clock Classes

**Wall clock**: `effective_at`, deadlines, audit timestamps, event chronology.

**Monotonic clock**: elapsed duration, timeouts, retry backoff, remaining execution budget.

Elapsed durations MUST use monotonic time where available.

## 2.2 Deadline Ownership

At interruption ingestion:

```text
effective_at + interruption_window → absolute_deadline
```

Market/Risk Monitor establishes the authoritative absolute deadline.

The Migration Plan stores it.

The Coordinator owns remaining-budget calculation after execution begins.

The Coordinator MUST NOT repeatedly reconstruct the deadline from unrelated timestamps.

---

# 3. Protocol #1 — Workload Contract

## 3.1 Purpose

Defines static workload migration requirements. It is declaration, not observation, compatibility assessment, readiness, placement, or execution.

## 3.2 Canonical Contract

```yaml
WorkloadContract:
  contract_version:
  job_id:

  checkpoint:
    checkpointable: true
    criu_compatible:
    required_criu_version:
    required_kernel_features:
    required_filesystem_features:

  runtime:
    artifact_digest:
    architecture:

  resources:
    cpu:
    memory:
    gpu:
      required:
      vendor:
      model:
      memory:
      compute_capability:

  network:
    required_ports:
    reconnectable:

  storage:
    process_local_state_only:
    required_filesystem_features:

  data:
    checkpoint_location:
    input_location:
    working_data_location:

  security:
    required_secret_refs:

  migration:
    allowed_regions:
    allowed_azs:
    engine_version:
```

V2 admission requires:

```text
checkpointable = true
external_application_dependencies = NONE
reconnectable = true
```

Credentials MUST be references, never checkpoint/contract material.

Runtime artifacts MUST be identified by immutable digest.

## 3.3 Admission gate (normative)

No job enters orchestration without passing admission. The gate is a pure
function `admit(WorkloadContract) → ADMITTED | REJECTED(reason)` owned by
the Admission Manager (component-arch Appendix A item 1), persisted on the
Registry job record alongside the admitted contract and `engine_version`:

```text
1. schema valid (contract version known, all §3.2 sections present)
2. checkpointable = true, mechanism = CRIU, consistency = process-local
3. external_application_dependencies = NONE (I1)
4. reconnectable = true; no non-reconstructable network state
5. runtime artifact digest present and resolvable in at least one pool
6. resource requirements declarable and satisfiable by ≥1 pool definition
7. checkpoint durably persistable (size bounds, storage reachable in test)
8. validation contract present (minimum_level, tolerances or strict flag)
9. secret refs only — no secret material in contract or (later) images
```

Rejection is terminal for that submission (new submission = new admission).
Admission verdicts are immutable and audited. Until the Admission Manager
is built, admission is a manual checklist executed at job registration and
recorded in the registration ticket — jobs MUST NOT be registered without
it. `engine_version` is assigned here per §16.1 component-arch and is
immutable thereafter (ADR-014).

---

# 4. Protocol #2 — Source of Truth & Authority

## 4.1 Authority Classes

- **Authoritative State:** persisted state owned by a designated mutator.
- **Authoritative Observation:** designated observer's best-known external evidence.
- **Derived State:** reproducibly computed from authoritative inputs.
- **External Reality:** actual infrastructure/process state.

External reality informs reconciliation; it does not silently mutate logical state.

## 4.2 Registry Authority

The Job Registry is authoritative for:

- job identity,
- job lifecycle,
- migration association,
- execution ownership,
- execution epoch,
- optimistic-concurrency version.

All authoritative mutations MUST pass through an atomic transition/CAS mechanism.

## 4.3 Version/Epoch Rule

Normal mutation:

```text
version++
epoch unchanged
```

Ownership transition:

```text
version++
epoch++
```

Where ownership changes, the relevant updates MUST be atomic.

## 4.4 Ownership

```text
SOURCE → TRANSITIONING → TARGET
```

`TRANSITIONING` is bounded. If ownership cannot be established with certainty:

```text
RECONCILIATION_REQUIRED
```

---

# 5. Protocol #3 — Migration State Machine

## 5.1 Core Flow

```text
PLANNED
 ↓
PRECHECKING
 ↓
CHECKPOINTING
 ↓
PERSISTING
 ↓
PROVISIONING
 ↓
TRANSFERRING
 ↓
RESTORING
 ↓
FENCING
 ↓
VALIDATING
 ↓
ACTIVATING
 ↓
FINALIZING
 ↓
SUCCESS
```

Terminal alternatives:

```text
ABORTED
SUPERSEDED
FAILED
```

## 5.2 Regimes

Each migration has immutable:

```text
ARBITRAGE | PROACTIVE | EMERGENCY
```

A higher-priority trigger does not mutate the existing regime. It causes replan or a new migration attempt as defined by policy.

## 5.3 Fencing Boundary

Before confirmed fencing:

```text
SOURCE remains authoritative
rollback is possible
```

After confirmed fencing:

```text
SOURCE cannot be restored as the rollback owner
recovery is forward-only
```

## 5.4 Emergency Parallelism

Emergency plans may execute independent steps concurrently.

V2 supports only:

```text
ALL_SUCCEEDED
```

join semantics.

Parallelism MUST be represented in the durable plan DAG.

---

# 6. Protocol #4 — Abort / Retry / Cleanup

## 6.1 Retry Layers

Retry is bounded independently by:

1. operation retry budget,
2. migration attempt budget,
3. absolute deadline.

A retry requires all applicable budgets to remain valid.

## 6.2 UNKNOWN Outcome

```text
UNKNOWN
 ↓
GetOperationStatusQuery / external-state query
 ↓
SUCCEEDED | FAILED | UNKNOWN
```

Blind retry is prohibited.

Persistent ambiguity after the resolution budget escalates to reconciliation.

## 6.3 Abort

Before fencing:

- source remains authoritative,
- target is cleaned,
- partial artifacts are cleaned.

After fencing, abort is not rollback; forward recovery applies.

## 6.4 REPLAN

`REPLAN` means the objective remains valid but the current plan is no longer executable.

```text
old plan → SUPERSEDED
new plan → new plan_id
```

## 6.5 Rollback Classes

Every PlanStep carries a `rollback_class` defining its failure semantics:

| Class | Semantics | Valid Phase |
|---|---|---|
| `FULL_ROLLBACK` | Terminate target processes, delete target artifacts; source remains authoritative; job → `RUNNING` | Pre-fencing only |
| `PARTIAL_CLEANUP` | Remove resources created by the failed operation; source remains authoritative; job → `RECOVERY_REQUIRED` if a durable checkpoint exists, else `RESTART_REQUIRED` | Pre-fencing |
| `FORWARD_ONLY` | Source is no longer authoritative; retain target for recovery; job → `RECOVERY_REQUIRED` | Post-fencing |
| `COMPENSATING_ACTION` | Execute an explicit inverse operation without claiming restoration of prior state | Any |
| `NONE` | No cleanup action | Terminal steps |

Per-step assignments:

| Step | Rollback Class |
|---|---|
| CHECKPOINT | `FULL_ROLLBACK` |
| PERSIST | `FULL_ROLLBACK` |
| PROVISION | `FULL_ROLLBACK` |
| TRANSFER | `PARTIAL_CLEANUP` |
| RESTORE (pre-fence) | `FULL_ROLLBACK` |
| FENCE | **`FORWARD_ONLY`** (Q2 decision) |
| VALIDATE (post-fence) | `FORWARD_ONLY` |
| ACTIVATE | `FORWARD_ONLY` |
| FINALIZE | `COMPENSATING_ACTION` / `NONE` |

`FULL_ROLLBACK` after confirmed fencing is prohibited (Invariant I4).

## 6.6 Deadline Exhaustion Per State

| Migration State | On `DEADLINE_EXCEEDED` |
|---|---|
| PLANNED … RESTORING (pre-fence) | Stop initiating new work; abort via Recovery Policy (`REPLAN` if objective survives) |
| FENCING (started, unconfirmed) | **Complete fencing to a safe conclusion.** The deadline never interrupts an in-progress ownership transition — partial fencing leaves ownership ambiguous |
| FENCING (confirmed) and beyond | Forward recovery only; the migration cannot be rolled back regardless of deadline |

## 6.7 Superseded Cleanup

When a migration becomes `SUPERSEDED`:

- **Safety-critical cleanup** (e.g., terminate a target that could assume execution ownership) MUST complete before the superseding migration starts.
- **Non-safety-critical cleanup** (temporary artifacts, partial transfers) MAY proceed asynchronously under GC/reconciliation.
- A target provisioned by a superseded migration MUST NOT be reused by the new migration; each migration attempt owns its own target.

---

# 7. Protocol #5 — Candidate Compatibility, Readiness & Placement

The layers are strictly separated:

```text
Compatibility → Readiness → Placement
```

## 7.1 Compatibility

Question:

> Can this candidate theoretically execute the workload?

Checks include CPU, memory, GPU model/compute capability, kernel, CRIU, filesystem, runtime artifact, and required features.

Results:

```text
INCOMPATIBLE | COMPATIBLE | UNKNOWN
```

`UNKNOWN` is not execution-eligible in V2.

## 7.2 Readiness

Question:

> Can this compatible candidate be used now?

Checks include:

- capacity evidence,
- IAM,
- KMS,
- network,
- artifact availability,
- current infrastructure state.

Emergency eligibility requires:

```text
COMPATIBLE + READY
```

## 7.3 Placement

Placement ranks eligible candidates.

Ranking MUST be deterministic; equal scores require a stable tie-break key.

Placement configuration MUST be versioned and pinned into the plan.

**Tie-break rule:** when total scores are equal, candidates are ordered lexicographically by `pool_id`. This guarantees reproducible placement for identical inputs.

**Boundary:** Placement consumes already-computed economic evidence (`CostAnalysis`, risk estimates). It ranks; it does not compute economics.

## 7.4 Candidate Pool

A candidate is a placement abstraction and may represent a pool rather than a single instance.

Pools have:

- lifecycle state,
- health,
- supported workload profiles,
- capacity,
- concurrency limits,
- region/AZ,
- runtime compatibility.

### 7.4.1 Readiness States

```text
INCOMPATIBLE | UNKNOWN | COMPATIBLE | PREPARABLE | READY
```

`PREPARABLE` means: the candidate can become `READY` through automated, bounded, plan-declarable preparation (e.g., artifact pull, IAM role attachment) whose duration is estimable and fits within the applicable deadline. A candidate requiring human intervention or with unbounded preparation time is `NOT_READY`, never `PREPARABLE`.

Emergency eligibility requires `READY` — never `PREPARABLE`.

### 7.4.2 Pool Lifecycle

```text
ACTIVE → DEGRADED → RETIRING → RETIRED
```

| State | Meaning | Eligibility |
|---|---|---|
| `ACTIVE` | Fully operational | Eligible |
| `DEGRADED` | Reduced capacity or elevated failure rate | Eligible only for non-emergency; excluded from emergency |
| `RETIRING` | Draining; no new migrations admitted | Ineligible |
| `RETIRED` | Removed from discovery | Ineligible |

Pool capacity is an **observation**, not pool definition. The pool record persists identity/topology/constraints; capacity and readiness are derived dynamically (Protocol #2 authority model).

### 7.4.3 Pool Concurrency

Each pool declares `max_concurrent_migrations`. Admission of a new migration targeting a pool MUST verify:

```text
active_migrations_targeting(pool) < pool.max_concurrent_migrations
```

Exceeding this yields `CAPACITY_UNAVAILABLE` for that pool, not queueing.

### 7.4.4 Storage

Candidate pools live in a dedicated registry (`spot_arbitrage_candidate_pools`), separate from the Job Registry. Pool definitions are durable configuration; assessments are versioned derived artifacts.

---

# 8. Protocol #6 — Command / Event / Query / State

## 8.1 Command

A command requests a side effect.

```yaml
Command:
  command_id:
  operation_id:
  job_id:
  migration_id:
  execution_epoch:
  correlation_id:
  deadline:
  payload:
```

## 8.2 Query

Queries are read-only.

V2 standard queries and their typed responses:

| Query | Response |
|---|---|
| `GetCandidateReadinessQuery` | `CandidateReadinessResult {pool_id, readiness_status, assessment_id, assessed_at, snapshot_version}` |
| `GetCheckpointValidityQuery` | `CheckpointValidityResult {checkpoint_id, durability, integrity_verified, lineage_valid, execution_epoch, assessed_at}` |
| `GetOperationStatusQuery` | `OperationStatusResult {operation_id, state, result, error, completed_at}` |
| `GetExecutionOwnershipQuery` | `ExecutionOwnershipResult {job_id, execution_epoch, authoritative_owner, active_migration_id}` |
| `GetInstanceStateQuery` | `InstanceStateResult {instance_id, state, public_ip}` |
| `GetMigrationPlanQuery` | `MigrationPlanResult {plan_id, plan_hash, state, expires_at}` |

A Query MUST NOT produce infrastructure side effects.

**Timeout semantics:** a query that exceeds its deadline returns an explicit timeout result; it never blocks migration progress indefinitely. A timed-out query MAY be retried; it is not itself an `UNKNOWN` operation outcome.

## 8.3 Event

Events represent facts and use at-least-once delivery.

```yaml
Event:
  event_id:
  event_type:
  job_id:
  migration_id:
  execution_epoch:
  effective_at:
  observed_at:
  version:
  correlation_id:
  payload:
```

Consumers MUST be idempotent.

## 8.4 State

State is the authoritative persisted representation and can only be mutated by its authority.

## 8.5 Event Ordering

Events cannot be assumed to arrive in order. Consumers evaluate version, epoch, effective time, and sequence metadata where present.

Event producers SHOULD attach a monotonically increasing `sequence` within their stream scope. Consumers apply stale-checks against current authoritative state before acting on any event.

## 8.6 Command Cancellation

A running command MAY be cancelled by issuing a cancellation request referencing its `operation_id`.

```text
CANCEL_REQUESTED → executor acknowledges → CANCELLED
```

Cancellation semantics are step-specific:

- Idempotent read-like or queued steps cancel cleanly.
- Steps with external side effects in flight resolve via the UNKNOWN protocol first — cancellation MUST NOT skip outcome resolution.
- Fencing steps cannot be cancelled once begun (Protocol #4 §6.6).

## 8.7 Transport Independence

The protocol semantics (Command/Query/Event/State) are independent of transport. V2 uses an **in-process dispatcher plus durable Event Ledger**; the same contracts permit later substitution of a distributed transport without semantic change.

---

# 9. Protocol #7 — Recovery Policy

Recovery Policy decides how to proceed after a failure or changed condition.

## 9.1 Decisions

```text
RETRY_RESTORE
FORWARD_RECOVERY
COLD_RESTART
REPLAN
FAILED
```

`RECONCILIATION_REQUIRED` is a gate, not a RecoveryDecision.

`SUPERSEDED` is a lifecycle result, not a RecoveryDecision.

**Pre-fencing hierarchy:** source healthy → retry/replan; source unhealthy → recovery required. Abort of the migration objective is a Coordinator outcome, not a RecoveryDecision.

**Post-fencing hierarchy (no rollback):**

```text
same checkpoint restorable?  → RETRY_RESTORE
durable checkpoint exists?   → FORWARD_RECOVERY
cold restart permitted?      → COLD_RESTART
otherwise                    → FAILED
```

`RETRY_RESTORE` requires ALL of: target controllable ∧ source non-authoritative ∧ checkpoint integrity valid ∧ lineage valid ∧ epoch consistent ∧ remaining deadline sufficient ∧ retry budget remaining.

## 9.2 Inputs

Recovery Policy consumes:

- failure classification (`code`, `transience`, `phase`),
- checkpoint context: durability/lineage/integrity, `checkpoint_created_at`, `checkpoint_age_seconds`,
- migration/restore progress (`migration_progress_percent`, `restore_progress_percent`, current step),
- candidate readiness and freshness (`readiness_assessed_at`, freshness SLA),
- recovery feasibility,
- remaining deadline,
- **reconciliation context** (`NONE | FINDING_PRESENT | OWNERSHIP_UNCERTAIN | SPLIT_BRAIN`),
- ownership state,
- workload policy (including `checkpoint_policy.max_recovery_age_seconds`),
- migration regime.

Progress is evidence, not policy: high restore progress never by itself authorizes continuation.

## 9.3 Decision Contract

```yaml
RecoveryDecision:
  decision_id:
  job_id:
  migration_id:
  execution_epoch:

  decision: RETRY_RESTORE | FORWARD_RECOVERY | COLD_RESTART | REPLAN | FAILED
  reason_code:            # e.g., NO_VIABLE_CANDIDATE, RESTORE_FAILED_RETRYABLE
  failure_code:

  replan_required: bool   # true ⇒ Planner must create a new plan even if the strategy repeats

  checkpoint_id:
  lineage_id:

  recovery_estimate:
    estimated_completion_at:
    estimated_duration_seconds:
    critical_path_duration_seconds:
    safety_margin_seconds:

  confidence:
    level: HIGH | MEDIUM | LOW
    basis:
      evidence:
      evidence_age_seconds:
      model_versions:
      uncertainty_reason:

  expires_at:
  policy_version:
```

The decision is immutable once issued. `replan_required = true` means the existing MigrationPlan must not continue; it does not by itself change the strategy.

## 9.4 Confidence

Low-confidence emergency feasibility is conservative: an EMERGENCY plan is executable only with a defensible conservative completion bound. Unknown estimates without such a bound are infeasible — never zero.

## 9.5 Estimated Completion

A recovery decision SHOULD include `estimated_completion_at` for deadline enforcement. It derives from current time plus critical-path duration; it never extends the authoritative interruption deadline.

## 9.6 Reconciliation Gate

If reconciliation context is `OWNERSHIP_UNCERTAIN` or `SPLIT_BRAIN`, Recovery Policy MUST refuse to decide. The job remains in `RECONCILIATION_REQUIRED` until ownership is re-established.

---

# 10. Protocol #8 — Migration Planning

Planner converts a policy decision into an immutable executable DAG.

## 10.1 MigrationPlan

```yaml
MigrationPlan:
  plan_id:
  plan_version:
  job_id:
  migration_id:
  regime:

  created_at:
  expires_at:
  absolute_deadline:

  created_by:
  planner_version:
  creation_reason:
  supersedes_plan_id:

  input_snapshot_versions:
  policy_versions:
  model_versions:

  candidate_id:
  candidate_assessment_version:
  checkpoint_id:

  plan_hash:
  steps:
```

## 10.2 Immutability

A started plan MUST NOT be modified in place. Replanning creates a new plan linked with `supersedes_plan_id`.

## 10.3 PlanStep

```yaml
PlanStep:
  step_id:
  type: CHECKPOINT | PERSIST | PROVISION | TRANSFER | RESTORE |
        FENCE | VALIDATE | ACTIVATE | FINALIZE | CLEANUP

  depends_on: [step_id]      # DAG edges; [] = immediately eligible

  state: PENDING | RUNNING | SUCCEEDED | FAILED | UNKNOWN | SKIPPED

  operation_type:            # maps to a Protocol #6 Command type
  operation_id:              # assigned at issue time by Coordinator (ULID)

  timeout_seconds:
  deadline_behavior: FAIL_ON_EXCEED | COMPLETE_FENCING   # fencing never abandons mid-transition

  retry_budget:
    max_attempts:
    backoff:
    retryable_failure_codes: []

  criticality: SAFETY_CRITICAL | CORRECTNESS_CRITICAL |
               EXECUTION_CRITICAL | BEST_EFFORT
  priority: int              # lower value = scheduled earlier under contention

  join_policy: ALL_SUCCEEDED # V2 supports this value only

  rollback_class: FULL_ROLLBACK | PARTIAL_CLEANUP |
                  FORWARD_ONLY | COMPENSATING_ACTION | NONE

  resource_requirements:
    cpu:
    memory:
    network:
    iam_refs: []

  failure_injection_points: []   # disabled by default; test-only; audited
```

**Step state ownership:** the Planner owns step *definition*; the Coordinator owns step *execution state*; the Registry/plan store persists it durably.

**Criticality assignments (normative):**

| Step | Criticality |
|---|---|
| CHECKPOINT, RESTORE, VALIDATE | `CORRECTNESS_CRITICAL` |
| PROVISION, TRANSFER | `EXECUTION_CRITICAL` |
| FENCE | `SAFETY_CRITICAL` |
| CLEANUP | `BEST_EFFORT` |

A `BEST_EFFORT` step MUST NEVER delay a `SAFETY_CRITICAL` step under deadline pressure.

**Effective step deadline:**

```text
effective_deadline = min(step_timeout_deadline, plan.expires_at, absolute_deadline)
```

Exception: once FENCING has begun, its completion takes precedence over all three bounds because partial fencing is unsafe (Protocol #4 §6.6).

## 10.4 Join Semantics

V2 supports:

```text
ALL_SUCCEEDED
```

Only. A dependent step becomes eligible when **all** of its `depends_on` steps are `SUCCEEDED`. Other join policies (`ANY_SUCCEEDED`, `QUORUM`) are reserved for future diversified provisioning and MUST NOT be used in V2 plans.

Partial branch failure is never resolved locally by the Coordinator: the failure flows to Recovery Policy (`REPLAN` / `RETRY_RESTORE` / `FORWARD_RECOVERY` / `FAILED`), which may issue a new plan.

## 10.5 Deadline Composition

Normal step execution is bounded by:

```text
min(step_timeout, plan_expiry, absolute_deadline)
```

Fencing is special: once confirmed fencing has begun, fencing completion takes precedence over ordinary cancellation because partial fencing is unsafe.

## 10.6 Plan Hash and Authorization

`plan_hash` = SHA-256 over the plan's canonicalized JSON (sorted keys, no volatile fields). It provides tamper detection, idempotency, and deterministic plan identity for audit.

Authorization record (no cryptographic signing in V2):

```yaml
authorization:
  planner_identity:
  policy_version:
  config_version:
  authorized_at:
  authorization_reference:    # e.g., policy_decision_id / recovery_decision_id
```

## 10.7 Replan Semantics

Replanning creates a NEW immutable plan:

```text
P1 (superseded) ← supersedes_plan_id ← P2 (new plan_id, plan_version=1)
```

`plan_version` increments only if a plan record itself is revised pre-admission. Once admitted/executing, a plan is immutable — invalidation produces a successor via `supersedes_plan_id`, never an in-place mutation.

## 10.8 Step retry, backoff, and priority semantics (normative)

`retry_budget`, `priority`, and `criticality` are execution directives, not
hints. The Coordinator interprets them exactly as follows:

1. **Attempt budget.** A step may be attempted at most `max_attempts` times
   (ADR-012 default 3, pinned per step at plan creation). Every attempt
   reuses the step's `operation_id` (I11); a new ID means a new operation.
2. **Retryable codes only.** A `FAILED` outcome retries iff its failure code
   is listed in `retryable_failure_codes` **and** attempts remain **and** the
   migration attempt budget and absolute deadline both remain valid (§6.1).
   Unlisted codes escalate immediately to Recovery Policy / reconciliation.
3. **Backoff.** Between attempts: exponential backoff `base → max` with
   ±25% jitter (ADR-012 defaults 2 s → 30 s). Backoff sleep consumes the
   step's `effective_deadline`; expiry during backoff is `DEADLINE_EXCEEDED`,
   not a further attempt.
4. **UNKNOWN is not an attempt.** An `UNKNOWN` outcome never consumes
   `max_attempts`. It enters outcome resolution (`GetOperationStatusQuery`
   within the UNKNOWN budget, default 30 s); only a resolved `FAILED` with a
   retryable code retries. Unresolved ⇒ reconciliation finding (I4).
5. **Priority.** Under executor contention (thread pool, semaphore slots),
   lower `priority` value schedules first. Priority never overrides
   `criticality`: a `BEST_EFFORT` step (e.g. `CLEANUP`, `FINALIZE`) MUST NOT
   delay a `SAFETY_CRITICAL` step (FENCE) under deadline pressure — the
   Coordinator preempts best-effort work first.
6. **`failure_injection_points`.** Test-only, disabled by default, every use
   audited. Production plans MUST carry an empty list; the Coordinator
   rejects plans with non-empty injection lists unless the test harness flag
   is set.

---

# 11. Protocol #9 — Checkpoint / CRIU

## 11.1 Scope

V2 supports directly CRIU-freezeable workloads with no external application dependencies.

**Worker model:** targets run a pre-installed immutable AMI containing pinned CRIU, kernel, `criu_wrapper.sh`, and runtime prerequisites. No package installation occurs during emergency recovery.

## 11.2 Lifecycle

```text
LOCAL → PERSISTING → DURABLE → VALIDATED
```

`UNKNOWN` is an operation outcome, not a checkpoint durability state (Decision 3).

Additional artifact states:

```text
TEMPORARY → COMMITTED      # storage-level; only COMMITTED may contribute to DURABLE
CORRUPTED                   # integrity failure; terminal for that artifact
EXPIRED                     # past retention; GC-eligible when unlocked
```

A partially uploaded object MUST NEVER be promoted to `DURABLE`.

## 11.3 DURABLE

A checkpoint is `DURABLE` only after:

1. bytes are persisted,
2. checksum/integrity is verified,
3. metadata is committed,
4. lineage is recorded.

Storage baseline: **S3 + SSE-KMS**, multipart upload with per-part and whole-object SHA-256.

## 11.4 VALIDATED

`VALIDATED` requires an actual restore followed by Validator success.

A durable checkpoint may remain unvalidated indefinitely.

## 11.5 Lineage

Checkpoint metadata includes:

```text
checkpoint_id
parent_checkpoint_id
checkpoint_lineage_id
sequence                    # monotonic within lineage
execution_epoch
created_at / durable_at / validated_at
runtime_artifact_digest
criu_version
kernel_version
architecture
checksum / checksum_algorithm
size_bytes
artifact_location           # s3_uri
validation_status
metadata_version
```

Lineage validity requires: matching `lineage_id`, contiguous `sequence`, `parent_checkpoint_id` consistency, and `execution_epoch` compatibility with the restoring context.

## 11.6 Retention

Retain:

- N most recent durable checkpoints,
- checkpoints referenced by active migrations (**locked**),
- policy-required recovery checkpoints.

GC MUST NOT delete locked checkpoints. GC operates only on `DURABLE ∧ unlocked ∧ outside retention floor`. Lineage extension is serialized — one operation at a time may create the next generation.

## 11.7 Restore Outcomes

```text
SUCCEEDED
FAILED
PARTIAL_RESTORE
UNKNOWN
```

`PARTIAL_RESTORE` is a failure code, not a checkpoint state: some of the process tree restored, the remainder did not, and no process is in an inconsistent state by CRIU's report. On `PARTIAL_RESTORE` the target process tree MUST be contained/terminated; the checkpoint itself remains usable if intact. Recovery Policy selects the next strategy.

## 11.8 CRIU Wrapper Interface

All CRIU invocation goes through `criu_wrapper.sh` on the worker:

```text
criu_wrapper.sh dump     <pid> <dir> [options]
    exit 0 success | 1 criu error | 2 timeout | 3 preflight failure

criu_wrapper.sh restore  <dir> [options]   # prints restored root PID on success
    exit 0 success | 1 criu error | 2 timeout |
    exit 3 PARTIAL_RESTORE | 4 environment incompatible

criu_wrapper.sh verify   <dir>
    exit 0 valid | 1 invalid | 2 indeterminate

criu_wrapper.sh preflight                  # cached per pool with TTL
    reports criu_version, kernel_version, architecture,
    capabilities, filesystem features, unsupported features, passed
```

Preflight runs at worker admission and before first migration to a pool; results are cached in pool metadata.

## 11.9 Restore Safety Gate

Restore is permitted only when ALL hold:

```text
checkpoint durability = DURABLE (or VALIDATED)
integrity verified (checksum + criu verify)
lineage valid for this job/epoch
CRIU version compatible
kernel compatible
architecture matches
runtime artifact digest available on target
target READY
```

Any failure yields `RESTORE_NOT_PERMITTED` before invocation — never discovered post hoc.

## 11.10 Concurrency Rules

1. One active dump per job.
2. One restore per target process namespace.
3. Active-migration checkpoints are retention-locked.
4. Lineage mutation is serialized.

## 11.11 Checkpoint Failure Taxonomy

Each code carries `transience: TRANSIENT | PERMANENT | UNKNOWN`:

```text
CRIU_DUMP_FAILED            CRIU_DUMP_TIMEOUT
CHECKPOINT_PERSIST_FAILED   CHECKPOINT_INTEGRITY_FAILED   CHECKPOINT_CORRUPTED
CHECKPOINT_TRANSFER_FAILED  CHECKPOINT_TRANSFER_TIMEOUT
CRIU_RESTORE_FAILED         CRIU_RESTORE_TIMEOUT          PARTIAL_RESTORE
CRIU_VERSION_INCOMPATIBLE   KERNEL_INCOMPATIBLE           RUNTIME_INCOMPATIBLE
UNSUPPORTED_PROCESS_STATE
CHECKPOINT_NOT_FOUND        CHECKPOINT_LINEAGE_INVALID    CHECKPOINT_EPOCH_MISMATCH
```

---

# 12. Protocol #10 — Validation

## 12.1 Validation Levels

```text
L1 Process      — process tree alive, expected structure, health/heartbeat responsive
L2 Runtime      — restored state consistent, required artifacts/config present,
                  network re-established, execution_epoch correct, no unexpected processes
L3 Workload     — tolerance-bounded equivalence of workload state and outputs
L4 Ownership    — checkpoint lineage valid AND epoch matches expected ownership context;
                  plus strict bitwise determinism when the contract demands it
```

L1 alone can never produce `VALID`: a process can be alive and wrong.

## 12.2 Results

```text
VALID
INVALID
INCONCLUSIVE
```

`INCONCLUSIVE` is never silently promoted to `VALID` or `INVALID`; it routes to Recovery Policy as insufficient evidence.

## 12.3 Validation Phases

| Phase | When | Purpose |
|---|---|---|
| `PRE_MIGRATION` | before checkpoint | baseline snapshot for comparison |
| `POST_RESTORE` | after CRIU restore | L1/L2 gate before deeper checks |
| `POST_VALIDATION` | after full suite | final correctness record |

Snapshots capture: `metrics`, `process_state`, `resource_state`, `custom_checks` per phase.

## 12.4 Tolerance Configuration (Versioned)

```yaml
ValidationContract:
  version: string                      # pinned into MigrationPlan.input_snapshot_versions
  minimum_level: L3
  strict_determinism: false            # Q8: opt-in via Workload Contract
  numerical_tolerance:
    relative_epsilon:
    absolute_delta:
  invariants: []                       # workload-specific named invariants
  metrics:
    <metric_name>:
      tolerance_type: ABSOLUTE | RELATIVE | PERCENTAGE
      value: number
      metric_path: string              # e.g., "process.memory_rss"
```

The contract version is pinned at plan creation; the Coordinator's final gate rejects execution if the resolved version differs (Protocol #14 drift rule).

## 12.5 Strict Mode

L4 bitwise determinism applies ONLY when the Workload Contract declares `strict_determinism: true`. It MUST never be a universal V2 criterion; parallel floating-point workloads legitimately produce small divergences handled by L3 tolerances.

## 12.6 Epoch and Lineage Checks

Validator verifies:

```text
target.execution_epoch == expected.execution_epoch
checkpoint.lineage_id / sequence / parent_checkpoint_id consistent
```

A mismatch yields `CHECKPOINT_LINEAGE_INVALID` or epoch-mismatch failure; validation cannot pass regardless of process health.

## 12.7 Post-Fencing Validation

After fencing:

```text
VALIDATION_FAILED or INCONCLUSIVE
 ↓
Recovery Policy
 ↓
RETRY_RESTORE | FORWARD_RECOVERY | COLD_RESTART | FAILED
```

Source rollback is prohibited. Validator reports facts; Recovery Policy decides strategy (Invariant I14: validation success does not itself transfer ownership).

## 12.8 Budget

Validation consumes the migration plan's remaining deadline via the Coordinator's monotonic clock. Validator never extends or redefines the deadline.

---

# 13. Protocol #11 — Reconciliation

## 13.1 Purpose

Reconciliation resolves disagreement between logical state and external reality.

## 13.2 Trigger Paths

### Event-driven

Triggered by anomalies, lifecycle events, fencing uncertainty, or ownership mismatch.

### Periodic sweep

Discovers:

- orphan resources,
- stale jobs,
- split-brain,
- missing target/source,
- ownership mismatch.

## 13.3 Finding

```yaml
ReconciliationFinding:
  finding_id:
  job_id:
  migration_id:
  execution_epoch:

  mismatch_type:
    SOURCE_STILL_ALIVE_AFTER_FENCE | TARGET_MISSING | SOURCE_MISSING |
    TARGET_UNOWNED | ORPHAN_INSTANCE | ORPHAN_CHECKPOINT | EPOCH_MISMATCH |
    REGISTRY_INFRASTRUCTURE_MISMATCH | STALE_MIGRATION |
    DANGLING_MIGRATION_REFERENCE | UNKNOWN_OWNERSHIP | SPLIT_BRAIN

  observed_state:
  authoritative_state:
  evidence_refs: []          # immutable; append-only

  severity: HIGH | MEDIUM | LOW
  confidence:

  recommended_remediation:   # recommendation, never a command
    type:
    reason:
    pre_authorized: bool     # true ⇒ Cleanup Executor may act directly

  status: OPEN | ACKNOWLEDGED | REMEDIATING | RESOLVED | ESCALATED
  detected_at:
  trigger: EVENT_DRIVEN | PERIODIC_SWEEP
```

**Finding lifecycle:** `OPEN → ACKNOWLEDGED → REMEDIATING → RESOLVED | ESCALATED`. A resolved finding cannot reopen without a new observation/event.

## 13.4 Authority Boundary

Reconciliation detects and requests remediation.

It does not independently choose migration strategy.

```text
Finding
 ↓
RECONCILIATION_REQUIRED when authority is uncertain
 ↓
Policy / Planner
 ↓
Coordinator
```

Historical orphan cleanup MAY use a dedicated, pre-authorized Cleanup Executor **only** when `recommended_remediation.pre_authorized = true` and the action is within predefined policy (e.g., abort an S3 multipart upload, delete a TEMPORARY artifact). Anything requiring judgment routes through Policy/Planner.

**Ownership uncertainty rule:** `UNKNOWN_OWNERSHIP`, `SPLIT_BRAIN`, and `EPOCH_MISMATCH` findings set the job to `RECONCILIATION_REQUIRED`. No recovery decision may proceed until reconciliation re-establishes authoritative ownership. In particular, a successfully *issued* fence signal is never treated as a completed fence — confirmation requires both epoch invalidation and verified source termination (Protocol #4 §6.6).

---

# 14. Protocol #12 — Observability & Audit

## 14.1 Operational Observability

Metrics, logs, traces, and alerts are asynchronous and MUST NOT gate decisions.

Core metrics:

```text
migration_success_rate / failure_rate / duration
checkpoint_duration / checkpoint_size
transfer_duration / restore_duration / validation_duration
estimated_vs_actual_{runtime, checkpoint_size, checkpoint_duration}
spot_savings / migration_cost / net_savings
interruption_count / recovery_success_rate / deadline_miss_rate
capacity_failure_rate / candidate_readiness_failure_rate
```

## 14.2 Decision Audit

Decision audit is durable, append-only, and records:

- decision,
- inputs,
- evidence versions,
- policy/model/config versions,
- plan identity (`plan_id`, `plan_hash`),
- timestamps,
- correlation identifiers.

Audit records are append-only: no UPDATE, no DELETE outside controlled retention/archival.

### AuditRecord schema

```yaml
AuditRecord:
  audit_id:
  event_type:
  job_id:
  execution_epoch:
  migration_id:
  operation_id:
  event_id:
  correlation_id:
  actor:
  component:
  timestamp:
  previous_state:
  new_state:
  decision:
  reason:
  input_snapshot_refs: []
  plan_id:
  plan_hash:
  config_versions:
  model_versions:
  outcome:
```

### Mandatory audit events

```text
MIGRATION_CREATED        PLAN_CREATED           PLAN_SUPERSEDED
STATE_TRANSITION         COMMAND_ISSUED         COMMAND_COMPLETED
COMMAND_UNKNOWN          CHECKPOINT_CREATED     CHECKPOINT_DURABLE
FENCE_STARTED            FENCE_CONFIRMED        VALIDATION_COMPLETED
RECOVERY_DECISION        MIGRATION_COMPLETED    MIGRATION_FAILED
RECONCILIATION_FINDING   RECONCILIATION_RESOLVED
```

## 14.3 Correlation Envelope

Every migration-scoped artifact carries:

```text
job_id · execution_epoch · migration_id · correlation_id
(+ operation_id for commands, + event_id for events)
```

Migration-scoped work may use `correlation_id = migration_id`; cross-migration job analysis may use `correlation_id = job_id`.

Telemetry failure never blocks migration; missing a mandatory durable audit record blocks the associated state transition where required by policy (audit is control-plane correctness, telemetry is not).

---

# 15. Protocol #13 — Migration History & Feedback

Each migration records:

```text
migration_id
job_id
regime
source
target
checkpoint
estimated durations
actual durations
estimated checkpoint size
actual checkpoint size
estimated cost
actual cost
failure taxonomy
retry count
validation outcome
final outcome
model versions
configuration versions
```

The system MUST distinguish:

```text
estimated
predicted
actual
```

These values MUST NOT be conflated.

Future ML models require immutable versioning and shadow evaluation before promotion.

---

# 16. Protocol #14 — Configuration & Versioning

Configuration is versioned and immutable once referenced by an active plan.

Relevant versions include:

```text
policy_config_version
placement_config_version
validator_config_version
recovery_config_version
estimator_model_version
risk_model_version
```

Plans MUST pin decision-relevant configuration/model versions at creation time. Components receive a resolved immutable config object for the migration — they never re-fetch "latest" mid-migration.

**Drift detection (Q6):** the Coordinator's final gate compares the pinned versions against currently resolved versions before each irreversible step:

```text
pinned_version != resolved_version  ⇒  PLAN_STALE → ABORT / REPLAN (pre-fencing)
```

Post-fencing, configuration changes do not retroactively alter execution semantics of the in-flight migration.

Published configuration artifacts are immutable: new behavior means a new version, never an in-place edit.

---

# 16A. Storage & Persistence Topology

Frozen substrate decisions (Q5, Q7) and their schema obligations:

| Store | Table | Key Structure | Contents |
|---|---|---|---|
| Job Registry | `spot_arbitrage_registry` | PK `job_id` (+ GSI on state) | job lifecycle, epoch, version, active migration |
| Candidate Pools | `spot_arbitrage_candidate_pools` | PK `pool_id` | pool definitions (durable config); capacity/readiness remain observations |
| Event Ledger | `spot_arbitrage_event_ledger` | **PK `event_id`, SK `consumer_id`** (Q5) | per-consumer dedup/processing records; TTL > max replay window |
| Migration History | `spot_arbitrage_migration_history` | separate table (Q7); PK `migration_id`, GSI `job_id` | full estimated-vs-actual records |
| Checkpoint Metadata | checkpoint subsystem store | PK `checkpoint_id`; lineage index | lineage, durability, integrity, S3 URI |
| Audit Records | audit store (append-only) | PK `audit_id` | mandatory audit events; no UPDATE/DELETE |

Rules:

1. Event Ledger entries are `(event_id, consumer_id)` pairs — independent processing per consumer; duplicate delivery yields a no-op.
2. Migration History is never embedded in the Job Registry: different lifecycle and access pattern.
3. Pool definitions are durable configuration; transient pool readiness/capacity is derived evidence (Protocol #2).
4. All stores carry the correlation envelope where migration-scoped.

---

# 17. Protocol #15 — Security

## 17.1 Credentials

Credentials are references, never migration-owned state.

Checkpoints MUST NOT contain credential material.

## 17.2 IAM

Candidate readiness MUST verify required permissions.

IAM failures are classified:

```text
TRANSIENT | PERMANENT | UNKNOWN
```

## 17.3 KMS

Target must demonstrate checkpoint decryptability before readiness is sufficient.

## 17.4 Runtime Trust

Runtime artifacts are pinned by immutable digest and must exist at the destination.

## 17.5 Checkpoint Encryption

V2 baseline:

```text
S3 + SSE-KMS
```

Access is controlled by IAM/KMS.

## 17.6 IAM Preflight

Before any irreversible operation:

```text
IAM permissions ∧ KMS permissions ∧ S3 permissions ∧ target role ∧ required APIs
```

MUST be validated. Failure yields `IAM_UNAVAILABLE` / `KMS_UNAVAILABLE` with `TRANSIENT | PERMANENT | UNKNOWN` transience — never a silent downgrade.

## 17.7 Runtime Environment Immutability (Protocol #5 ↔ #9 invariant)

A checkpoint MAY be restored only into a candidate whose declared environment satisfies the checkpoint's compatibility contract:

```text
checkpoint.criu_version      compatible with candidate AMI's pinned CRIU
checkpoint.kernel_version    compatible with candidate kernel
checkpoint.architecture      == candidate architecture
runtime_artifact_digest      available on candidate
```

Candidate Compatibility verifies these dimensions against pool/AMI metadata; mismatch ⇒ `INCOMPATIBLE`. This invariant exists because V2 targets CRIU process migration — not generic application deployment.

---

# 18. Protocol #16 — Testability

## 18.1 Clock Injection

Tests MUST control:

- wall clock,
- monotonic clock,
- deadline,
- elapsed time,
- timeout behavior.

## 18.2 Deterministic Failure Injection

Each critical PlanStep exposes injection points such as:

```text
after_command_issued
after_external_success
before_state_commit
after_state_commit
during_transfer
during_restore
during_fencing
during_validation
```

## 18.3 Operation Outcome Injection

Tests MUST simulate:

```text
SUCCESS
FAILED
UNKNOWN
```

including the case where external infrastructure succeeds but the control plane crashes before recording the outcome.

## 18.4 Required Test Classes

- state-machine tests,
- property tests,
- contract tests,
- idempotency tests,
- event deduplication/order tests,
- fencing tests,
- emergency deadline tests,
- reconciliation tests,
- checkpoint integrity tests,
- partial-restore tests,
- crash-recovery tests,
- security/IAM tests,
- chaos scenarios.

---

# 19. Central Failure Taxonomy

Every failure is classified centrally.

```yaml
Failure:
  code:
  transience: TRANSIENT | PERMANENT | UNKNOWN
  retryable:
  requires_replan:
  requires_reconciliation:
  terminal:
  safety_impact:
```

Core codes:

```text
PLAN_STALE
CANDIDATE_INCOMPATIBLE
COMPATIBILITY_UNKNOWN
READINESS_FAILED
CAPACITY_UNAVAILABLE
NO_VIABLE_CANDIDATE
IAM_UNAVAILABLE
KMS_UNAVAILABLE
CHECKPOINT_FAILED
CHECKPOINT_INTEGRITY_FAILED
TRANSFER_FAILED
RESTORE_FAILED
PARTIAL_RESTORE
VALIDATION_FAILED
FENCING_FAILED
DEADLINE_EXCEEDED
OWNERSHIP_MISMATCH
EPOCH_CONFLICT
OPERATION_OUTCOME_UNKNOWN
```

---

# 20. Universal Idempotency

Every side-effecting command has exactly one logical `operation_id`.

Retries reuse that ID.

If the provider supports client tokens:

```text
provider_client_token = operation_id
```

If not, actual infrastructure state MUST be queried before deciding whether to retry.

---

# 21. Universal Event Semantics

Delivery is:

```text
AT_LEAST_ONCE
```

Therefore consumers MUST be idempotent.

Deduplication may use:

```text
(event_id, consumer_id)
```

Events carry sufficient version/epoch/effective-time information to reject stale effects.

---

# 22. Plan Staleness

A plan may become stale because of:

- candidate readiness drift,
- capacity drift,
- market drift,
- workload observation drift,
- configuration drift,
- checkpoint validity drift,
- security/IAM drift.

Upstream components detect/report drift.

The Coordinator performs the final gate before irreversible execution.

---

# 23. Reconciliation / Remediation Pipeline

```text
External Reality
      ↓
Observation
      ↓
Reconciliation Finding
      ↓
Reconciliation Manager
      ↓
RECONCILIATION_REQUIRED if ownership uncertain
      ↓
Policy / Planner
      ↓
Migration Plan
      ↓
Coordinator
      ↓
Command
```

Reconciliation MUST NOT shortcut to arbitrary infrastructure execution.

---

# 24. Emergency Semantics

Emergency migration is a distinct execution model, not normal migration with shorter timeouts.

Priority:

```text
1. execution correctness
2. ownership certainty
3. interruption deadline
4. workload recovery
5. cost optimization
```

Emergency plans MAY:

- parallelize independent operations,
- use conservative feasibility bounds,
- use fast-path observations,
- bypass ordinary polling cadence,
- choose more expensive candidates when necessary.

Emergency plans MUST NOT:

- treat compatibility `UNKNOWN` as compatible,
- blind retry unknown operations,
- bypass fencing,
- fabricate missing estimates,
- bypass authority boundaries.

---

## 24.1 Emergency deadline computation (normative)

The absolute deadline is computed **once, at interruption ingestion**, by the
Market/Risk Monitor — never by the Coordinator, never recomputed from fresh
timestamps:

```text
absolute_deadline =
    interruption.effective_at
    + interruption.window_seconds
    - wan_allowance(source_region, home_region, target_region)
    - ingestion_skew_margin
```

### Per-region-pair WAN allowance

Cross-region command legs (Home decision → source fence, Home → target
restore) consume wall-clock that single-region math hides. Each ordered
region pair carries a configured allowance:

```yaml
# config/v2_baseline.yaml → execution.wan_allowance_seconds
execution:
  wan_allowance_seconds:
    default: 15.0
    pairs:
      "us-east-1/us-west-2": 20.0
```

Allowances are initial estimates only. Track A6 (prod checklist) requires
replacing them with measured P95 command-leg latencies per pair before the
multi-region pilot. The feasibility critical path uses
`leg(source→Home) + leg(Home→target) + margin` for transfer estimates.

### Ownership and storage

| Step | Owner | Rule |
|---|---|---|
| Compute `absolute_deadline` | Market/Risk Monitor, at ingestion | formula above; stored on the interruption record |
| Carry it | Migration Plan (`absolute_deadline`) | immutable once admitted |
| Enforce remaining budget | Coordinator, via monotonic clock | `remaining = absolute_deadline − now`; never reconstruct from other timestamps |
| Crash recovery | Bootstrap (§14.3 component-arch) | wall-clock `absolute_deadline` is the conservative basis; monotonic anchors do not survive restart |

`ingestion_skew_margin` (default 5 s, same config block) covers clock skew
between the worker IMDS notice, the ingestion path, and Home wall-clock.
Emergency plans with no computable deadline are infeasible — never zero.

---

# 25. Replan / Supersession

```text
Plan P1
  ↓ invalidated
Recovery Policy = REPLAN
  ↓
Planner → P2
  ├─ supersedes_plan_id = P1
  ↓
P1 = SUPERSEDED
```

A new plan revision does not automatically imply a new migration attempt.

A new `migration_id` is required when the execution attempt itself changes.

---

# 26. Authority Matrix

| Concern | Authoritative owner |
|---|---|
| Workload declaration | Registry / Workload Contract |
| Job lifecycle | Job Registry |
| Registry `version` | Job Registry |
| Execution epoch | ownership protocol / Registry |
| Migration execution state | Coordinator via Registry |
| Migration regime | Migration record |
| Plan | Planner |
| Policy decision | Policy Engine |
| Candidate compatibility | Compatibility component |
| Candidate readiness observation | Infrastructure Monitor / readiness subsystem |
| Placement ranking | Placement |
| Checkpoint lifecycle | Checkpoint subsystem |
| Checkpoint integrity | Checkpoint subsystem + Validator for restore validity |
| Restore correctness | Validator |
| External execution reality | Infrastructure/worker providers |
| Reconciliation finding | Reconciliation Manager |
| Configuration version | Configuration subsystem |
| Audit record | Observability/Audit subsystem |
| Migration history | History subsystem |

---

# 27. Non-Negotiable Invariants

**I1 — Self-contained workload:** V2 workloads have no external application dependencies.

**I2 — One active migration:** at most one active migration exists per job.

**I3 — One authoritative owner:** a job cannot have two authoritative execution owners.

**I4 — Fencing boundary:** rollback exists only before confirmed fencing.

**I5 — No blind retry:** UNKNOWN external outcomes are resolved before retry.

**I6 — No stale overwrite:** older observations/events cannot overwrite newer authoritative state.

**I7 — Missing ≠ favorable:** missing evidence never becomes an optimistic value.

**I8 — Decision/execution separation:** policy does not execute.

**I9 — Reconciliation cannot silently remediate:** findings require the authorized remediation path.

**I10 — Plan immutability:** started plans are not mutated in place.

**I11 — Stable operation identity:** retries reuse `operation_id`.

**I12 — Epoch fencing:** ownership transitions are protected by `execution_epoch`.

**I13 — Durable checkpoint integrity:** persistence + integrity + metadata commit are required for `DURABLE`.

**I14 — Validation ≠ ownership:** validation success does not itself transfer execution ownership.

**I15 — Auditability:** every migration decision is reconstructible from durable records.

**I16 — Emergency safety precedence:** safety and ownership correctness override economics.

---

# 28. Protocol Dependency Map

```text
#1 Workload Contract
        │
        ├───────────────┐
        ▼               ▼
#5 Compatibility    #9 Checkpoint
        │               │
        ▼               ▼
#5 Placement        #10 Validator
        │               │
        └───────┬───────┘
                ▼
        #7 Recovery Policy
                │
                ▼
        #8 Migration Planner
                │
                ▼
        #6 Command/Event/Query
                │
                ▼
        #3 Migration State Machine
                │
                ▼
        #4 Abort/Retry/Cleanup
                │
                ▼
        #11 Reconciliation

Cross-cutting:
#2 Source of Truth → all protocols
#12 Observability → all protocols
#13 History → execution + decisions
#14 Configuration → policy + planning + validation
#15 Security → admission + execution + storage
#16 Testability → all protocols
```

---

# 29. Protocol Freeze Criteria

A protocol is frozen only when:

1. every externally visible object has a schema;
2. authority is assigned;
3. legal transitions are enumerated;
4. failure behavior is defined;
5. retry semantics are defined;
6. UNKNOWN semantics are defined;
7. deadlines are defined;
8. idempotency is defined;
9. crash recovery is defined where applicable;
10. cross-protocol dependencies are explicit;
11. security implications are addressed;
12. deterministic testing hooks exist.

No implementation may introduce a new cross-protocol semantic without amending this document.

---

# 30. Recommended Implementation Order

```text
1. Registry + CAS + identity/epoch
2. Command/Event/Query primitives
3. CRIU Checkpoint subsystem
4. Validator
5. Candidate / Readiness / Placement
6. Recovery Policy
7. Planner / DAG
8. Coordinator
9. Reconciliation
10. Observability / Audit
11. Configuration / Security
12. Test harness + deterministic failure injection
13. End-to-end migration
```

The execution layer is the highest-risk implementation surface:

```text
CRIU
Checkpoint persistence
Transfer
Restore
Fencing
Validation
```

Higher-level components MUST consume these interfaces rather than invent execution semantics independently.

---

# 31. Final V2 Control-Plane Model

```text
                    ┌──────────────────────┐
                    │  WORKLOAD CONTRACT   │
                    └──────────┬───────────┘
                               │
                               ▼
                 ┌─────────────────────────┐
                 │ Compatibility /         │
                 │ Readiness / Placement   │
                 └──────────┬──────────────┘
                            │
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼
       Market/Risk      Workload       Infrastructure
         Monitor        Estimator          Monitor
             └──────────────┼──────────────┘
                            ▼
                   ┌──────────────────┐
                   │   POLICY ENGINE  │
                   │ Arbitrage        │
                   │ Proactive        │
                   │ Emergency        │
                   └────────┬─────────┘
                            │ Decision
                            ▼
                   ┌──────────────────┐
                   │     PLANNER      │
                   │    Migration DAG │
                   └────────┬─────────┘
                            │ Plan
                            ▼
                   ┌──────────────────┐
                   │   COORDINATOR    │
                   │ state/deadline/  │
                   │ execution        │
                   └────────┬─────────┘
                             │ Commands
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Checkpoint      Provisioner     Transfer
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                          Restore
                             │
                             ▼
                          Fencing              ← ownership transition FIRST:
                             │                   epoch invalidation + verified
                             ▼                   source termination (§0.11).
                         Validator            ← post-fence correctness gate:
                             │                   VALID/INVALID/INCONCLUSIVE.
                             ▼                   Post-fence failure is forward-
                        ACTIVATING            ← only, never rollback (I4).
                             │
                             ▼
                        Registry / Epoch
                      (ownership commit)
                             │
                             ▼
                        Finalization

        ┌─────────────────────────────────────┐
        │          RECONCILIATION             │
        │ logical state ↔ external reality    │
        └─────────────────────────────────────┘
                            │
                            ▼
                     Recovery / Replan

         ┌─────────────────────────────────────┐
         │ Observability / Audit / History     │
         │ Configuration / Security / Testing  │
         └─────────────────────────────────────┘
```

### 31.1 Normative execution order (fence-then-validate)

The authoritative step order is §0.4/§5.1:

```text
RESTORING → FENCING → VALIDATING → ACTIVATING → FINALIZING
```

Fencing precedes validation deliberately: once the target is restored, the
source epoch is invalidated and source termination verified *before* anyone
asks whether the restore is correct. A post-fence `INVALID`/`INCONCLUSIVE`
verdict therefore routes to forward recovery (`RETRY_RESTORE` /
`FORWARD_RECOVERY` / `COLD_RESTART` / `FAILED`) and can never resurrect the
source (I2, I4). Validating first would leave two live execution candidates
during the validation window — exactly the split-brain this architecture
exists to prevent. Validation success does not transfer ownership (I14);
only `ACTIVATING` commits the already-fenced epoch transition.

**This is the normative protocol layer. Component specifications should reference these semantics rather than redefine them.**
