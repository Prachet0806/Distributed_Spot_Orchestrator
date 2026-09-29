# Component Architecture Specification — V2

**Status:** Draft — Component Freeze Baseline (builds on frozen `Protocols.md`)
**Scope:** Component catalogue, responsibility boundaries, dependency graph, protocol mapping, Command/Query/Event matrix, state ownership, persistence architecture.
**Normative rule:** Every component MUST conform to `Protocols.md`. Components reference protocol semantics; they MUST NOT redefine them.

---

## Table of Contents

```text
1.  Architectural Component Model          (layered view)
2.  Component Catalogue                     (per-component boundaries)
3.  Protocol ↔ Component Mapping
4.  Command / Query / Event Matrix
5.  Component Dependency Graph              (normative)
6.  State Ownership Matrix                  (definitive)
7.  Persistence Architecture
8.  Interface Contracts                     (derived from C/Q/E semantics)
9.  Failure Boundaries                      (operation outcome topology)
10. Idempotency Boundaries
11. Security Boundaries
12. Runtime / Deployment Topology
13. Scaling Model
14. HA / Failure Domains
15. Cross-Region Architecture
16. V1 → V2 Migration Architecture
17. Implementation Dependency Graph
18. Architecture Invariants
```

Sections 12–15 define the physical/distributed shape on top of the logical freeze of §1–11. Sections 16–18 complete the architectural constitution: coexistence strategy, build order, and the normative invariant set that gates implementation design.

---

# 1. Architectural Component Model

## 1.1 Layers

```text
┌─────────────────────────────────────────────────────────────────────┐
│                        ADMISSION                                    │
│   Workload Admission Manager                                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ admitted WorkloadContract
┌──────────────────────────────▼──────────────────────────────────────┐
│                        OBSERVATION                                  │
│   Market/Risk Monitor · Infrastructure Monitor · Workload Estimator │
│   (evidence only — never mutates authoritative state)               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ evidence
┌──────────────────────────────▼──────────────────────────────────────┐
│                        DECISION                                     │
│   Policy/Decision Engine · Recovery Policy                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ PolicyDecision / RecoveryDecision
┌──────────────────────────────▼──────────────────────────────────────┐
│                        PLANNING                                     │
│   Compatibility → Readiness → Placement → Migration Planner        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ immutable MigrationPlan
┌──────────────────────────────▼──────────────────────────────────────┐
│                        EXECUTION CONTROL                            │
│   Migration Coordinator                                             │
│   (sole issuer of operation_id; sole caller of executors)           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ Commands
┌──────────────┬───────────────▼───────────────┬──────────────────────┐
│  EXECUTION   │ Provisioner · Transfer Mgr ·  │ CRIU/Checkpoint Mgr  │
└──────────────┴───────────────┬───────────────┴──────────────────────┘
                               │ outcomes / events
┌──────────────────────────────▼──────────────────────────────────────┐
│                        CORRECTNESS                                  │
│   Validator                                                         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ ValidationResult
┌──────────────────────────────▼──────────────────────────────────────┐
│                        CONSISTENCY                                  │
│   Reconciliation Manager · Cleanup Executor                         │
└─────────────────────────────────────────────────────────────────────┘

PERSISTENCE (owned stores):  Job Registry · Pool Registry · Plan Store ·
                             Event Ledger · Checkpoint Metadata · Audit Store ·
                             Migration History · Configuration Store

CROSS-CUTTING:               Config/Version Mgr · Security/IAM/KMS ·
                             Observability · Migration History feed
```

## 1.2 Layer Rules

| Rule | Statement |
|---|---|
| L1 | Observation components produce evidence; they never mutate authoritative state (Protocols §0.2, §26). |
| L2 | Only Decision components choose strategies; only the Planner constructs plans; only the Coordinator issues commands (§0.1). |
| L3 | All authoritative Job/Migration state mutation flows through `Registry.transition()` CAS — there is no second path (Decision 5). |
| L4 | Executors receive Commands carrying Coordinator-assigned `operation_id`; they never mint replacements for the same logical operation. |
| L5 | Persistence components expose typed stores; no component reads another component's private tables directly except through its published Query contracts. |
| L6 | Cross-cutting services are consumed by all layers but depend on none of them upward (acyclic dependency rule, §5). |

---

# 2. Component Catalogue

Each entry below fixes: purpose, owns, does-NOT-own, key interactions, persistence, idempotency boundary, security boundary, and governing protocols.

---

### 2.1 Workload Admission Manager

| Aspect | Definition |
|---|---|
| **Layer** | Admission |
| **Purpose** | Gate workloads into the V2-supported class before any orchestration. |
| **Owns** | admission verdict (`ADMITTED`/`REJECTED` + reason code); contract schema validation |
| **Does NOT own** | job lifecycle; candidate selection; economics; execution |
| **Inputs** | candidate `WorkloadContract`, artifact digest, secret refs |
| **Outputs** | admitted contract persisted to Registry; rejection record |
| **Commands consumed** | none (invoked at submission) |
| **Queries consumed** | artifact availability (Storage), preflight cache (Pool Registry) |
| **Events consumed** | none |
| **Events produced** | `WORKLOAD_ADMITTED`, `WORKLOAD_REJECTED` |
| **Authoritative state** | admission record (via Registry write) |
| **Persistence** | Registry (job row created here), Configuration Store (admission policy version) |
| **Failure modes** | schema invalid → `WORKLOAD_INVALID`; artifact unverifiable → reject; never optimistic on UNKNOWN digest |
| **Idempotency** | re-submission of identical contract returns prior verdict |
| **Security** | verifies digest signature chain; records submitter identity in audit |
| **Protocols** | #1, #14, #15 |

---

### 2.2 Market/Risk Monitor

| Aspect | Definition |
|---|---|
| **Layer** | Observation |
| **Purpose** | Observe spot prices, volatility, interruption notices; derive risk evidence. |
| **Owns** | market/risk observation series; interruption event ingestion + dedup; authoritative absolute-deadline computation at ingestion |
| **Does NOT own** | provisioning; migration decisions; candidate selection; capacity probing |
| **Inputs** | provider price feeds; interruption signals (event lane) |
| **Outputs** | price/volatility observations; risk estimates; `SpotInterruptionEvent` with computed absolute deadline |
| **Commands consumed** | none — observation only |
| **Events consumed** | provider interruption notices |
| **Events produced** | `SPOT_PRICE_OBSERVATION`, `RISK_OBSERVATION`, `SPOT_INTERRUPTION` (bypasses normal freshness — emergency fast path) |
| **Derived state** | risk model outputs (`risk_model_version` stamped) |
| **Persistence** | observation history (its own store); deadline recorded into migration context at ingestion |
| **Failure modes** | feed failure → stale-flagged last valid estimate, confidence degraded; NEVER zero-risk substitution |
| **Idempotency** | duplicate interruption notices dedup by `event_id` before dispatch |
| **Security** | read-only provider IAM; no instance permissions |
| **Protocols** | #2, #3 (deadline ownership), #6, #13 |

---

### 2.3 Infrastructure Monitor

| Aspect | Definition |
|---|---|
| **Layer** | Observation |
| **Purpose** | Observe actual instance/process/runtime readiness state. |
| **Owns** | infrastructure observation series; readiness evidence; capacity evidence aggregation (from provisioning outcomes — never by launching probes) |
| **Does NOT own** | provisioning; termination; job lifecycle; placement |
| **Inputs** | provider describe APIs; worker heartbeats |
| **Outputs** | instance-state observations; readiness/capacity evidence snapshots (versioned, timestamped) |
| **Queries consumed** | — (it IS the query target for `GetInstanceStateQuery`) |
| **Events consumed** | provider lifecycle events |
| **Events produced** | `INFRASTRUCTURE_MISMATCH` candidates → forwarded to Reconciliation Manager as findings input |
| **Authoritative state** | none (observations only); external reality authority rests with providers |
| **Persistence** | observation store with TTL |
| **Failure modes** | API failure → stale evidence flagged; emergency lane uses synchronous provider query with bounded latency |
| **Idempotency** | observation writes keyed by `(resource_id, observed_at)` |
| **Security** | read-only EC2/SSM IAM |
| **Protocols** | #2, #5 (readiness evidence), #6 |

---

### 2.4 Workload Estimator

| Aspect | Definition |
|---|---|
| **Layer** | Observation (estimation sub-layer) |
| **Purpose** | Predict progress, remaining runtime, checkpoint size/duration with explicit confidence. |
| **Owns** | prediction outputs; re-baselining after execution-context change; prediction-error feedback consumption |
| **Does NOT own** | job lifecycle; decisions; checkpoint execution |
| **Inputs** | workload observations (worker adapter), Migration History feedback |
| **Outputs** | `WorkloadEstimate {progress, remaining_runtime, checkpoint_size/duration_est, prediction_confidence, estimator_version, model_version}` |
| **Events consumed** | `MIGRATION_COMPLETED` (feedback trigger) |
| **Events produced** | estimate-snapshot events (audit) |
| **Derived state** | estimates only — Registry stores latest snapshot as reference, never as truth |
| **Persistence** | model artifacts via Config/Version Manager; estimate snapshots (audit refs) |
| **Failure modes** | insufficient data ⇒ `UNKNOWN` estimate + LOW confidence (never fabricated precision) |
| **Idempotency** | estimates are pure functions of (observation window, model version) |
| **Security** | read-only on history/telemetry |
| **Protocols** | #2, #6, #13, #16 (model version pinning) |

---

### 2.5 Policy / Decision Engine (Arbitrage + Proactive)

| Aspect | Definition |
|---|---|
| **Layer** | Decision |
| **Purpose** | Choose STAY / MIGRATE (+ target class) under ARBITRAGE or PROACTIVE regimes; enforce hysteresis. |
| **Owns** | policy decision records (immutable); precedence enforcement `EMERGENCY > PROACTIVE > ARBITRAGE` within a job |
| **Does NOT own** | planning; execution; feasibility computation; reconciliation |
| **Inputs** | CostAnalysis, risk evidence, WorkloadEstimate, PlacementRecommendation, job lifecycle |
| **Outputs** | `PolicyDecision {decision, regime, reason_code, confidence{level,basis}, policy_version}` |
| **Queries consumed** | placement recommendation, cost analyses, readiness freshness |
| **Events consumed** | `SPOT_PRICE_OBSERVATION`, `RISK_OBSERVATION` (triggers evaluation), `RECONCILIATION_FINDING` (blocks decisions while gate active) |
| **Events produced** | decision-audit events |
| **Persistence** | decision records (append-only, audit-referenced) |
| **Failure modes** | missing/stale inputs ⇒ DEFER with reason; economics never overrides safety precedence |
| **Idempotency** | deterministic given pinned input snapshot versions |
| **Security** | none beyond audit write |
| **Protocols** | #2, #7 (precedence semantics), #12, #14 |

---

### 2.6 Recovery Policy

| Aspect | Definition |
|---|---|
| **Layer** | Decision |
| **Purpose** | Decide recovery strategy after failure/interruption: `RETRY_RESTORE | FORWARD_RECOVERY | COLD_RESTART | REPLAN | FAILED`. |
| **Owns** | `RecoveryDecision` records (immutable); post-fencing strategy selection; durable-checkpoint selection among candidates |
| **Does NOT own** | execution; plan construction; fencing; registry mutation; reconciliation resolution |
| **Inputs** | failure classification, checkpoint context (age/durability/lineage/integrity), restore progress, readiness freshness, RecoveryFeasibility, remaining deadline, reconciliation context, ownership state |
| **Outputs** | `RecoveryDecision` per Protocols §9.3 contract |
| **Gate behavior** | refuses to decide while `OWNERSHIP_UNCERTAIN` / `SPLIT_BRAIN` (§9.6) |
| **Events consumed** | failure events, `VALIDATION_COMPLETED`, checkpoint-integrity events, reconciliation findings |
| **Events produced** | `RECOVERY_DECISION` (mandatory audit event) |
| **Persistence** | decision records (append-only) |
| **Failure modes** | infeasible + no bound ⇒ FAILED/NO_VIABLE_CANDIDATE; never fabricates conservative bounds |
| **Idempotency** | deterministic over pinned inputs + policy_version |
| **Security** | audit write only |
| **Protocols** | #4, #7, #9, #10, #11 |

---

### 2.7 Candidate Compatibility Engine

| Aspect | Definition |
|---|---|
| **Layer** | Planning (stage 1 of 3) |
| **Purpose** | Answer: can this pool theoretically execute this workload? |
| **Owns** | `CompatibilityAssessment` (versioned derived artifact) |
| **Checks (exhaustive)** | CPU arch, CPU/mem/storage capacity-vs-requirement, GPU profile match, kernel/CRIU compatibility vs checkpoint contract, filesystem features, runtime artifact digest, region/AZ constraints, reconnectability |
| **Does NOT own** | readiness evidence (IAM/KMS/network/current capacity); placement; selection |
| **Boundary rule** | NO secret-resolution checks, NO instantaneous capacity checks — those belong to Readiness (Decision 11) |
| **Outputs** | assessment incl. per-dimension results; `INCOMPATIBLE | COMPATIBLE | UNKNOWN` |
| **Idempotency** | pure function of (WorkloadContract.version, pool.version, checkpoint-context when present) |
| **Protocols** | #1, #5, #9 (runtime immutability invariant §17.7) |

---

### 2.8 Candidate Readiness Engine

| Aspect | Definition |
|---|---|
| **Layer** | Planning (stage 2) |
| **Purpose** | Answer: can we use this compatible candidate right now? |
| **Owns** | `ReadinessAssessment`; freshness metadata (`assessed_at`, SLA class) |
| **Consumes evidence from** | Infrastructure Monitor (capacity/state), IAM/KMS probe results, artifact-store availability, network path checks |
| **States** | `READY | PREPARABLE | NOT_READY | UNKNOWN` — PREPARABLE requires bounded, automatable preparation fitting the deadline |
| **Emergency rule** | eligibility = `COMPATIBLE ∧ READY ∧ fresh`; UNKNOWN/PREPARRABLE excluded |
| **Does NOT own** | compatibility dimensions; ranking |
| **Idempotency** | assessment = f(evidence snapshot versions) |
| **Protocols** | #5, #15 (IAM/KMS preflight evidence), #11 (evidence staleness → finding input) |

---

### 2.9 Placement Engine

| Aspect | Definition |
|---|---|
| **Layer** | Planning (stage 3) |
| **Purpose** | Deterministically rank eligible candidates; emit recommendation. |
| **Owns** | `PlacementRecommendation {recommendation_id, ranked_candidates, selected_candidate_id, policy_version, expires_at}` |
| **Hard filters** | INCOMPATIBLE / UNKNOWN / NOT_READY rejected before ranking |
| **Ranking** | weighted scoring from versioned `placement_policy.yaml`; consumes pre-computed CostAnalysis + risk evidence (never computes economics itself); tie-break: lexicographic `pool_id` |
| **Concurrency check** | verifies pool `max_concurrent_migrations` headroom; exceeded ⇒ pool excluded with `CAPACITY_UNAVAILABLE` |
| **Does NOT own** | decision authority (Policy decides); provisioning |
| **Expiry** | recommendation carries `expires_at`; stale recommendation cannot authorize irreversible steps |
| **Protocols** | #5, #14 (policy version pinning) |

---

### 2.10 Migration Planner

| Aspect | Definition |
|---|---|
| **Layer** | Planning |
| **Purpose** | Convert PolicyDecision / RecoveryDecision + evidence into an immutable `MigrationPlan` (DAG of PlanSteps). |
| **Owns** | plan construction; structural/semantic/security/temporal plan validation; `plan_id`, `plan_hash`; `supersedes_plan_id` linkage |
| **Plan contents** | per Protocols §10.1: metadata, objective/regime/deadline, source/target, checkpoint selection, pinned input-snapshot + config/model versions, authorization block, DAG steps |
| **Step generation rules** | NORMAL: serial critical-path DAG. EMERGENCY: parallelizes independent branches (CHECKPOINT ∥ PROVISION → JOIN → TRANSFER → …); join = ALL_SUCCEEDED only; FENCE step always present with `rollback_class=FORWARD_ONLY`, `criticality=SAFETY_CRITICAL`, `deadline_behavior=COMPLETE_FENCING` |
| **Does NOT own** | step execution state; command issuance; economic choice; fencing authority invention (fencing exists only because the plan declares it) |
| **Validation gates** | DAG acyclicity; dependencies exist; unique step/operation types; candidate READY-fresh; checkpoint DURABLE-valid; deadline feasible (critical-path + margin ≤ budget); policy/config versions authorized |
| **Outputs** | `MigrationPlan + PlanValidationResult` — failed validation produces NO executable plan |
| **Persistence** | Plan Store (immutable records; new revision = new plan_id) |
| **Protocols** | #8 (primary), #4, #6, #7, #9, #10, #14 |

---

### 2.11 Migration Coordinator

| Aspect | Definition |
|---|---|
| **Layer** | Execution Control |
| **Purpose** | Execute an approved plan exactly; own step-execution state; issue all commands; enforce deadlines; drive abort/retry/cleanup per plan + protocol. |
| **Owns (authoritative via stores)** | PlanStep execution states; Operation records; remaining-budget calculation (monotonic clock); branch coordination for parallel steps |
| **Sole authorities** | generates every `operation_id` (ULID); sole invoker of executors; sole submitter of `Registry.transition()` for job/migration state changes; final plan-validity gate before each irreversible step |
| **Final gate (pre-irreversible step)** | plan hash intact ∧ not expired ∧ epoch valid ∧ candidate still READY-fresh ∧ checkpoint still valid ∧ config versions unchanged (Q6) ∧ budget sufficient ∧ reconciliation gate clear |
| **UNKNOWN handling** | outcome UNKNOWN ⇒ `GetOperationStatusQuery` against executor/provider ⇒ resolve within budget ⇒ unresolved escalates to Reconciliation (never blind retry) |
| **Fencing special case** | once begun, completes regardless of deadline expiry; confirmation = epoch invalidation + verified source termination |
| **Crash recovery** | reconstructs from durable plan-store step states + operation ledger + external reality queries; idempotent replay by operation_id |
| **Does NOT own** | recovery strategy (asks Recovery Policy); plan redesign; direct infrastructure access outside issued Commands |
| **Persistence** | Plan Store (step-state updates), Event Ledger (command/completion events), Audit Store |
| **Protocols** | #3, #4, #6, #8 (primary consumer), #11, #12 |

---

### 2.12 Provisioner

| Aspect | Definition |
|---|---|
| **Layer** | Execution |
| **Purpose** | Acquire/release target compute from a selected pool. |
| **Owns** | provision/terminate operation outcomes; provider client-token mapping (`provider_client_token = operation_id`) |
| **Contract** | `execute(ProvisionCommand) -> OperationResult`; `execute(TerminateCommand) -> OperationResult`; `query(GetOperationStatusQuery)` |
| **AMI model** | launches pre-installed immutable AMI pinned by pool's `ami_id`/digest — no user-data installation path |
| **Capacity discipline** | NEVER launches probe instances; capacity evidence flows only from real outcomes fed back to Infrastructure Monitor |
| **Failure modes** | emits SUCCEEDED / FAILED(`PROVISION_FAILED`,`CAPACITY_UNAVAILABLE`,`QUOTA_EXCEEDED`) / UNKNOWN(timeout) |
| **Idempotency boundary** | same operation_id ⇒ same instance; replays query-before-create when token unsupported |
| **Security** | scoped EC2 run/terminate role; tags instances with job/migration/epoch for orphan attribution |
| **Protocols** | #6, #9 (AMI pinning), #15 |

---

### 2.13 Transfer Manager

| Aspect | Definition |
|---|---|
| **Layer** | Execution |
| **Purpose** | Move checkpoint bytes source↔S3↔target with integrity. |
| **Operations** | multipart upload (64MB parts, SHA-256 per part + whole), resumable download, verify, abort-multipart (cleanup) |
| **Integrity rule** | download completes only after whole-object checksum matches manifest; mismatch ⇒ `CHECKPOINT_INTEGRITY_FAILED` at destination, source artifact untouched |
| **Ownership rule** | transfer is a copy — never mutates source durability state |
| **Encryption** | TLS in transit; objects written SSE-KMS |
| **Failure modes** | TRANSFER_FAILED / TRANSFER_TIMEOUT / INTEGRITY_FAILED; partial upload leaves TEMPORARY object + abortable multipart handle |
| **Idempotency** | resume keyed by operation_id manifest; re-upload after verified absence only |
| **Protocols** | #6, #9, #15 |

---

### 2.14 CRIU / Checkpoint Manager

| Aspect | Definition |
|---|---|
| **Layer** | Execution |
| **Purpose** | Own checkpoint lifecycle: dump, verify, persist, restore; lineage; retention/GC. |
| **Owns (authoritative)** | Checkpoint metadata store; durability state machine `LOCAL→PERSISTING→DURABLE(→VALIDATED)`; lineage chains; retention locks |
| **Worker interface** | `criu_wrapper.sh dump/restore/verify/preflight` per Protocols §11.8 exit-code contract |
| **Restore safety gate** | enforces §11.9 checklist BEFORE invoking restore; violation ⇒ `RESTORE_NOT_PERMITTED` |
| **Outcomes** | restore: `SUCCEEDED | FAILED | PARTIAL_RESTORE | UNKNOWN`; PARTIAL ⇒ containment flag set, checkpoint remains usable if intact |
| **GC** | deletes only `DURABLE ∧ unlocked ∧ past-floor`; locked = active-migration or policy-required |
| **Concurrency** | one dump/job; one restore/target; serialized lineage extension |
| **Does NOT own** | migration strategy; fencing; workload correctness (that's Validator) |
| **Protocols** | #9 (primary), #6, #10, #15 |

---

### 2.15 Validator

| Aspect | Definition |
|---|---|
| **Layer** | Correctness |
| **Purpose** | Determine whether restored execution satisfies the workload's Validation Contract. |
| **Owns** | `ValidationReport` records (immutable audit artifacts); level execution L1→L4; tolerance application per pinned contract version |
| **Phases** | PRE_MIGRATION baseline capture; POST_RESTORE gate; POST_VALIDATION record |
| **Verifies additionally** | epoch match, lineage validity (L4) — independent of process health |
| **Strict mode** | L4 bitwise only when Workload Contract opts in (Q8) |
| **Results** | `VALID | INVALID | INCONCLUSIVE`; INCONCLUSIVE routes to Recovery Policy as insufficient evidence |
| **Does NOT own** | retry decisions; ownership transition; ongoing reconciliation |
| **Snapshot capture** | via worker SSH channel consistent with CRIU path (no extra agent) |
| **Protocols** | #10 (primary), #6, #12 |

---

### 2.16 Reconciliation Manager

| Aspect | Definition |
|---|---|
| **Layer** | Consistency |
| **Purpose** | Detect/classify divergence between authoritative state and reality; escalate; request remediation. |
| **Triggers** | event-driven (dispatcher subscriptions: fence anomalies, unexpected terminations, epoch conflicts) + periodic sweeps (orphans, stale migrations, dangling refs) |
| **Owns** | `ReconciliationFinding` records + lifecycle (`OPEN→ACKNOWLEDGED→REMEDIATING→RESOLVED|ESCALATED`); evidence refs (immutable) |
| **Gate authority** | sets job → `RECONCILIATION_REQUIRED` on OWNERSHIP_UNCERTAIN / SPLIT_BRAIN / EPOCH_MISMATCH; Recovery Policy blocked until resolved |
| **Remediation path** | Finding → ReconciliationEvent → Policy/Planner → RemediationPlan → Coordinator/Executor. Direct action ONLY via `recommended_remediation.pre_authorized=true` routed to Cleanup Executor (bounded: multipart-abort, TEMPORARY delete) |
| **Never** | silently mutates authoritative state; executes general remediation itself; rewrites history |
| **Protocols** | #11 (primary), #2, #6, #12 |

---

### 2.17 Cleanup Executor

| Aspect | Definition |
|---|---|
| **Layer** | Consistency |
| **Purpose** | Execute narrowly-scoped, pre-authorized resource cleanup. |
| **Allowed actions** | terminate orphan-tagged instances; abort multipart uploads; delete TEMPORARY artifacts; remove stale temp metadata |
| **Authorization sources** | (a) Coordinator cleanup directives during abort/failure paths; (b) Reconciliation pre-authorized recommendations; (c) GC policy from Checkpoint Manager |
| **Forbidden** | fencing; ownership change; strategy choice; deletion of DURABLE/locked checkpoints |
| **Attribution** | relies on provisioner resource tags (job_id/migration_id/epoch) for orphan classification evidence |
| **Idempotency** | every action carries operation_id; repeats are no-ops |
| **Protocols** | #4 (cleanup semantics), #6, #11, #15 |

---

### 2.18 Persistence Stores (eight stores)

| Store | Owner Component | Mutability | Notes |
|---|---|---|---|
| Job Registry | Job Registry service | mutable via CAS only | single mutation path (Decision 5) |
| Candidate Pool Registry | Pool Registry service | definitions mutable (versioned); assessments derived | separate lifecycle from jobs |
| Plan Store | Planner writes / Coordinator advances steps | plan body immutable post-admission; step-state sub-record mutable | enables crash reconstruction |
| Event Ledger | Event subsystem | append-only processing records | `(event_id, consumer_id)` dedup |
| Checkpoint Metadata Store | CRIU/Checkpoint Manager | durability monotonic; lineage append | locks enforced |
| Audit Store | Audit subsystem | strictly append-only | mandatory events; no UPDATE/DELETE |
| Migration History | History writer (Coordinator completion hook) | insert-once, enrich-once | estimated-vs-actual preserved distinctly |
| Configuration Store | Config/Version Manager | immutable versions | S3-versioned artifacts |

---

### 2.19 Cross-Cutting Services

| Service | Purpose | Consumed By |
|---|---|---|
| **Config/Version Manager** | resolve + pin immutable config/model versions; serve resolved snapshots | Planner, Policies, Validator, Coordinator (drift check) |
| **Security/IAM/KMS Gateway** | preflight permission verification (IAM/KMS/S3/APIs); KMS decryptability proof for targets | Readiness Engine, Coordinator (final gate), Transfer/CRIU managers |
| **Event Dispatcher + Ledger** | in-process pub/sub with durable dedup/at-least-once semantics | all components (producers/consumers) |
| **Observability** | metrics/logs/traces (async, non-gating) + Audit pipeline (durable, gating where mandated) | all components |
| **Migration History Writer** | capture estimated-vs-actual per completed migration; expose feedback queries to Estimator/Risk models | Coordinator (write), Estimator/Risk (read) |

---

# 3. Protocol ↔ Component Mapping

| Protocol | Primary Implementer(s) | Primary Consumer(s) |
|---|---|---|
| #1 Workload Contract | Admission Manager, Registry (persistence) | Compatibility, Planner, Validator |
| #2 Source-of-Truth | Job Registry (+ CAS), Authority Matrix §6 | ALL |
| #3 Migration State Machine | Coordinator (driver), Registry (authority) | Reconciliation, History, Policy |
| #4 Abort/Retry/Cleanup | Coordinator, Cleanup Executor | Recovery Policy, Planner |
| #5 Compatibility/Readiness/Placement | three Engines + Pool Registry | Policy, Planner |
| #6 Command/Event/Query/State | Dispatcher/Ledger + all components as producers/consumers | ALL |
| #7 Recovery Policy | Recovery Policy | Planner, Coordinator |
| #8 Migration Planning | Planner | Coordinator |
| #9 Checkpoint/CRIU | CRIU/Checkpoint Manager, Transfer Manager | Recovery, Validator, GC path |
| #10 Validation | Validator | Recovery, Coordinator |
| #11 Reconciliation | Reconciliation Manager, Cleanup Executor | Policy, Coordinator |
| #12 Observability & Audit | Observability svc + Audit Store | ALL (produce), operators (consume) |
| #13 History & Feedback | History Writer + Store | Estimator, Risk Model |
| #14 Config & Versioning | Config/Version Manager | Policy, Planner, Validator, Coordinator |
| #15 Security/IAM/KMS | Security Gateway + Transfer/CRIU (enforcement) | Readiness, Coordinator |
| #16 Testability | DI seams across ALL + injection framework | test harness |

---

# 4. Command / Query / Event Matrix

Legend: **C** = consumes, **P** = produces.

### Commands

| Command | Issued by | Executed by |
|---|---|---|
| PreflightCommand | Coordinator (on pool first-use) | CRIU Mgr (worker) |
| CreateCheckpoint / PersistCheckpoint | Coordinator | CRIU Mgr |
| TransferCheckpoint / VerifyCheckpoint | Coordinator | Transfer Mgr |
| RestoreCheckpoint | Coordinator | CRIU Mgr |
| Provision / Terminate | Coordinator | Provisioner |
| FenceSource | Coordinator | Worker controller via CRIU Mgr |
| ValidateMigration | Coordinator | Validator |
| CleanupCommand | Coordinator / Reconciliation(pre-auth) | Cleanup Executor |
| TransitionRequest | Coordinator (only) | Job Registry CAS |

### Queries

| Query | Issued by | Answered by |
|---|---|---|
| GetCandidateReadinessQuery | Planner, Coordinator(gate) | Readiness Engine |
| GetCheckpointValidityQuery | Coordinator(gate), Recovery | Checkpoint Metadata |
| GetOperationStatusQuery | Coordinator | originating Executor |
| GetExecutionOwnershipQuery | Recovery, Reconciliation | Registry |
| GetInstanceStateQuery | Reconciliation, Readiness | Infrastructure Monitor |
| GetMigrationPlanQuery | Coordinator(recovery), Reconciliation | Plan Store |
| ResolvedConfigSnapshot | all (at use-time) | Config/Version Mgr |
| IAMPreflightQuery | Readiness, Coordinator | Security Gateway |

### Events

| Event | Produced by | Consumed by |
|---|---|---|
| SPOT_INTERRUPTION | Market/Risk Monitor | Policy(EMERGENCY path), Coordinator |
| SPOT_PRICE_OBSERVATION / RISK_OBSERVATION | Market/Risk Monitor | Arbitrage/Proactive Policy |
| WORKLOAD_ADMITTED / REJECTED | Admission | Registry, Audit |
| PLAN_CREATED / PLAN_SUPERSEDED | Planner | Audit, Coordinator |
| STATE_TRANSITION | Coordinator | Audit, Reconciliation, History |
| COMMAND_ISSUED / COMPLETED / UNKNOWN | Coordinator | Audit, Ledger consumers |
| CHECKPOINT_CREATED / CHECKPOINT_DURABLE | CRIU Mgr | Coordinator, Audit |
| SOURCE_TERMINATED_CONFIRMED | CRIU/Fence path | Coordinator(fence confirm) |
| VALIDATION_COMPLETED | Validator | Recovery, Coordinator |
| RECOVERY_DECISION | Recovery Policy | Planner, Audit |
| RECONCILIATION_FINDING / RESOLVED | Reconciliation | Policy, Recovery(gate), Audit |
| MIGRATION_COMPLETED / FAILED | Coordinator | History, Estimator(feedback), Audit |
| ORPHAN_DETECTED | Reconciliation sweep | Cleanup path (if pre-auth) |

---

# 5. Component Dependency Graph (normative)

Arrows point consumer → provider. Upward-layer dependencies are forbidden.

```text
                       ┌────────────────────┐
                       │ Admission Manager  │
                       └─────────┬──────────┘
                                 ▼
     ┌──────────────┐   ┌────────────────────┐
     │ Market/Risk  │   │ Infrastructure     │
     │ Monitor      │   │ Monitor            │
     └──────┬───────┘   └─────────┬──────────┘
            │ events              │ evidence
            ▼                     ▼
     ┌─────────────────────────────────────┐      ┌──────────────────┐
     │ Policy Engine      Recovery Policy  │◄─────│ Reconciliation   │
     └───────────────┬─────────────┬───────┘gate  │ Manager          │
                     │ decisions   │              └────────▲─────────┘
                     ▼             │                       │ findings
     ┌──────────────┐              │                       │
     │ Compatibility│              │                       │
     └──────┬───────┘              │                       │
            ▼                      │                       │
     ┌──────────────┐              │                       │
     │ Readiness    │◄─── Sec/IAM GW                       │
     └──────┬───────┘              │                       │
            ▼                      ▼                       │
     ┌──────────────┐      ┌──────────────┐               │
     │ Placement    │      │ Planner      │◄──────────────┘
     └──────┬───────┘      └──────┬───────┘  remediation reqs
            │ recommendation      │ MigrationPlan
            └──────────►──────────┘
                                ▼
                       ┌────────────────┐
                       │  COORDINATOR   │◄──── Recovery Decisions
                       └───────┬────────┘
             Commands │        │ transitions(CAS)
        ┌─────────────┼────────┼──────────────┐
        ▼             ▼        ▼              ▼
  ┌───────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐
  │Provisioner│ │Transfer  │ │ CRIU/CP  │ │Validator│
  └─────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬────┘
        │            │            │            │
        └────────────┴─────┬──────┴────────────┘
                           ▼ outcomes/events
                   ┌───────────────┐
                   │ Event Ledger /│
                   │ Dispatcher    │
                   └───────────────┘

  Shared downward deps (all components):
    Job Registry(CAS) · Plan Store · Config/Version Mgr ·
    Security GW · Audit · History(write-side: Coordinator)
```

**Acyclic rule:** no arrow may be reversed; e.g., Registry never calls Coordinator; Executors never call Planner. Violations surface in code review as architecture defects.

---

# 6. State Ownership Matrix (definitive)

| State / Data | Persistence Authority | Legal Commit Path | Readers | Domain |
|---|---|---|---|---|
| Job lifecycle | **Job Registry** | Coordinator *requests* → Registry validates legal transition → CAS commit | all | Job |
| `execution_epoch` | **Job Registry** (ownership protocol) | atomic CAS `{version++, epoch++}` on validated ownership-transition request | all | Job |
| Registry `version` | **Job Registry** | CAS precondition on every transition | all | Job |
| Active migration ref | **Job Registry** | one-active guard inside validated transition request | Policy, Recon | Job |
| Migration lifecycle & outcome (incl. terminal states, regime binding) | **Job Registry** | Coordinator-requested transition, legal-transition checked | Recon, History, Policy | Migration |
| Migration regime value | immutable post-admission (recorded at plan admission via Registry write) | — no mutation path — | all | Migration |
| PlanStep execution state | **Plan Store** | Coordinator-*requested* advance; CAS on `(plan_hash, step_id, expected_from_state)` | Recon, crash-recovery | PlanStep |
| Operation state | executing component + Event Ledger | Executor outcome write keyed by `operation_id` | Coordinator | Operation |
| WorkloadContract | **Registry** (admitted) | Admission Manager creates only; never mutated | Compat, Planner, Validator | Declaration |
| CandidatePool definition | **Pool Registry** | versioned admin updates only | Compat/Readiness/Placement | Declaration |
| Capacity / readiness evidence | Infrastructure Monitor (+ probe results) | Infrastructure Monitor writes observations | Readiness, Coordinator gate | Observation |
| Spot price / risk / absolute deadline | Market/Risk Monitor | ingestion-time writes | Policy, Planner | Observation |
| WorkloadEstimate | Estimator | snapshot publish | Feasibility, Policy | Observation |
| CompatibilityAssessment | Compat Engine | versioned artifact insert | Readiness, Placement, Planner | Derived |
| ReadinessAssessment | Readiness Engine | versioned artifact insert | Placement, Policy, gate | Derived |
| PlacementRecommendation | Placement Engine | versioned artifact insert | Policy, Planner | Derived |
| PolicyDecision / RecoveryDecision | respective policy component | append-only record | Planner, Coordinator, Audit | Decision |
| MigrationPlan body | **Plan Store** | Planner inserts; immutable post-admission (hash-verified reads) | Coordinator, Recon | Plan |
| Checkpoint durability / lineage / locks | **Checkpoint Metadata Store** | CRIU Mgr monotonic transitions only | Recovery, Validator, GC | Execution |
| Checkpoint bytes (S3) | external storage reality | Transfer Mgr writes; GC deletes unlocked only | Transfer, CRIU | Data plane |
| ValidationReport | Validator | insert-once | Recovery, Coordinator, Audit | Correctness |
| ReconciliationFinding | Reconciliation Manager | lifecycle transitions only (`OPEN→…→ESCALATED`) | Policy, Recovery, Audit | Consistency |
| Event processing state | **Event Ledger** | ledger subsystem (status transitions per consumer) | consumers | Consistency |
| Config/model versions | Config Store | release process publishes NEW versions; never edits | all (resolved snapshots) | Governance |
| AuditRecord | Audit Store | append-only writer | operators, compliance | Governance |
| MigrationHistory record | History Store | History Writer: insert-once + bounded enrich pass | Estimator, Risk, operators | Governance |

**Authority rules (normative):**

1. **One committing authority per datum.** The "Persistence Authority" column names exactly one store/component per datum. If two appear, split the datum.
2. **Request vs commit.** The Coordinator holds **execution authority** — it decides ordering, issues Commands, owns remaining-budget arithmetic — but it holds **no persistence authority**. Every durable mutation above is a *request* that the owning store validates (legal transition, expected version/state/epoch) and commits atomically. A rejected request is authoritative feedback that must alter Coordinator behavior (e.g., replan or reconcile), never an error to override or retry blindly.
3. **No bypass.** Nothing mutates Job- or Migration-domain fields except through `Registry.transition()`; nothing advances a PlanStep except through a Plan Store conditional commit.

**Fencing verification boundary:** confirmed fencing requires BOTH (a) old epoch invalidated in the Registry AND (b) source termination verified against runtime reality. If (a) succeeds but (b) cannot be verified within the fencing budget, the outcome is **`RECONCILIATION_REQUIRED`** — fencing MUST NEVER be inferred successful from epoch invalidation alone ("signal sent ≠ fence confirmed").

---

# 7. Persistence Architecture

## 7.1 Tables & Keys

| Store | Table | Keys / Indexes | Consistency | Retention / TTL |
|---|---|---|---|---|
| Job Registry | `spot_arbitrage_registry` | PK `job_id`; GSI `state` (StateIndex); attrs: `version(N)`, `execution_epoch(N)`, `active_migration_id`, lifecycle, contract-ref | strong (CAS conditional writes) | lifetime of job + archival policy |
| Pool Registry | `spot_arbitrage_candidate_pools` | PK `pool_id`; attr `version(N)`; GSI `status` | strong | until RETIRED + grace |
| Plan Store | `spot_arbitrage_plans` | PK `plan_id`; attrs `plan_hash`, `supersedes_plan_id`, `migration_id`; step-state sub-document | strong; step updates conditional on `plan_hash` + expected step-revision | plans ≥ audit horizon |
| Event Ledger | `spot_arbitrage_event_ledger` | **Composite primary key — PARTITION `event_id` + SORT `consumer_id`**; optional GSI `received_at` for bounded replay; attrs: `status`, `received_at`, `processed_at`, `attempt_count`, `last_error` | strong; put-if-absent on composite key = dedup gate | TTL > max duplicate-delivery/replay window |
| Checkpoint Metadata | `spot_arbitrage_checkpoints` | PK `checkpoint_id`; GSI `lineage_id` (SK sequence); attrs durability/validation/locks | strong; monotonic conditionals | per retention floor + lock rule |
| Audit Store | `spot_arbitrage_audit` | PK `audit_id`; GSI `(job_id, ts)`; append-only | eventually OK for reads, writes durable-first | WORM; archival pipeline |
| Migration History | `spot_arbitrage_migration_history` | PK `migration_id`; GSI `job_id` | insert-once, single enrich | long-horizon (model training) |
| Configuration | S3 versioned keys `config/{domain}/v{N}.yaml` | version-id pins stored in plans | S3 immutability | indefinite (referenced integrity) |

## 7.2 Transactional Boundaries

1. **Ownership transfer**: single Registry item transaction — `{lifecycle, epoch++, version++, active_migration_id}` atomically. Never split across calls.
2. **Step advance**: Plan Store step-state update conditional on `(plan_hash, from_state)` — CAS prevents concurrent double-advance.
3. **Dedup vs side effect ordering**: Event Ledger record is committed BEFORE dispatcher invokes consumer (crash between ⇒ redelivery, consumer idempotent).
4. **Audit-before-irreversible**: mandated audit event durably written before the irreversible command is issued (audit gating rule §14).

## 7.3 Recovery-after-Control-Plane-Failure Sources of Truth

| Question | Answered from |
|---|---|
| Which migrations were active? | Plan Store (non-terminal step-states) + Registry active refs |
| What had actually completed externally? | operation ledger + provider status queries (GetOperationStatus) |
| Was fencing confirmed? | Registry epoch vs source-process reality (reconcile matrix §16.7 Protocols) |
| Which checkpoints are safe? | Checkpoint Metadata (DURABLE ∧ unlocked ∧ lineage-valid) |
| What did we claim and why? | Audit Store + pinned plan/config versions |

## 7.4 Event Ledger Record Lifecycle

Record states per `(event_id, consumer_id)`:

```text
        put-if-absent (dedup gate)
                 │
                 ▼
             PENDING ──► PROCESSING ──► COMPLETED
                 │            │
                 │            └──► FAILED (last_error, attempt_count++)
                 │                     │  retry w/ backoff ≤ max_attempts
                 │                     ▼
                 └── (dispatch crash)  re-PROCESSING on redelivery
```

Rules:

1. **Commit-before-dispatch:** the ledger insert must durably succeed BEFORE the dispatcher invokes the consumer. A crash after insert but before handling ⇒ redelivery; consumer idempotency makes this safe.
2. **Dedup:** an existing `COMPLETED` record causes redelivery to be skipped entirely.
3. **Terminal failure:** `FAILED` beyond max attempts surfaces as an operational alert (telemetry plane). It becomes a reconciliation finding only if the event gated a mandatory-audit transition that never completed.
4. **Replay:** the optional `received_at` GSI supports bounded-window replay for recovery/audit tooling; replay respects the same dedup gate.
5. **TTL:** entries expire only after the maximum plausible duplicate-delivery and replay horizon has passed.

---

# 8. Interface Contracts (shape-level; full signatures deferred to implementation freeze)

Interfaces are derived from C/Q/E semantics — no bare method calls crossing component boundaries for side effects.

```python
class JobRegistry:            # Persistence / authority
    def transition(self, req: TransitionRequest) -> TransitionResult: ...
    def get(self, job_id) -> JobView: ...                    # query
    def query_ownership(self, q: GetExecutionOwnershipQuery) -> ExecutionOwnershipResult: ...

class MigrationCoordinator:   # Execution control
    def execute(self, plan: MigrationPlan,
                sink: EventSink) -> MigrationResult: ...     # entry; all internal effects flow as Commands/Events
    def cancel(self, op: CancelRequest) -> CancelResult: ...

class Planner:
    def create_plan(self, req: PlanRequest) -> tuple[MigrationPlan, PlanValidationResult]: ...

class RecoveryPolicy:
    def decide(self, ctx: RecoveryContext) -> RecoveryDecision: ...   # pure decision

class PolicyEngine:
    def decide(self, ctx: PolicyContext) -> PolicyDecision: ...

class Provisioner:
    def execute(self, cmd: ProvisionCommand | TerminateCommand) -> OperationHandle: ...
    def query(self, q: GetOperationStatusQuery) -> OperationStatusResult: ...

class CheckpointManager:
    def execute(self, cmd: CheckpointCommand) -> OperationHandle: ...
    def query_validity(self, q: GetCheckpointValidityQuery) -> CheckpointValidityResult: ...
    def select_recovery_checkpoint(self, sel: RecoverySelection) -> CheckpointRef | None: ...

class TransferManager:
    def execute(self, cmd: TransferCommand) -> OperationHandle: ...

class Validator:
    def validate(self, q: ValidateMigrationQuery) -> ValidationReport: ...

class ReconciliationManager:
    def reconcile(self, trigger: TriggerContext) -> list[ReconciliationFinding]: ...
    def request_remediation(self, finding_id) -> RemediationRequest: ...

class CleanupExecutor:
    def execute(self, cmd: CleanupCommand) -> OperationResult: ...   # pre-authorized scope only

class EventDispatcher:
    def publish(self, e: Event) -> None: ...          # ledger-commit then fan-out
    def subscribe(self, consumer_id, types, handler) -> None: ...

class ConfigManager:
    def resolve(self, pins: dict[str, str]) -> ResolvedConfig: ...
    def current_versions(self) -> dict[str, str]: ...
```

Contract-test rule (Protocol #16): each interface above ships a shared conformance suite covering legal args, idempotent replay, UNKNOWN surfacing, and refusal paths.

---

# 9. Failure Boundaries (operation-outcome topology)

Uniform topology for EVERY external operation (Provision, Checkpoint, Persist, Transfer, Restore, Fence, Terminate, Cleanup):

```text
Coordinator issues Command(operation_id, deadline)
        │
        ▼
   Executor executes
        │
   ┌────┼─────────────┐
   ▼    ▼             ▼
SUCCEEDED  FAILED   TIMEOUT/CRASH ⇒ UNKNOWN
   │         │             │
   │         │             ▼
   │         │   GetOperationStatusQuery (resolution budget)
   │         │        ├── resolved SUCCEEDED/FAILED
   │         │        └── budget exhausted ──► Reconciliation finding
   ▼         ▼
 mapped to Failure taxonomy code (+transience)
        │
        ▼
 Step state updated (CAS) → Event emitted → Coordinator applies
 Protocol #4 matrix: retry? abort? replan? forward-recovery?
```

Per-component dominant failure codes:

| Component | Emits |
|---|---|
| Provisioner | PROVISION_FAILED, CAPACITY_UNAVAILABLE, QUOTA_EXCEEDED, OPERATION_OUTCOME_UNKNOWN |
| Transfer | TRANSFER_FAILED/TIMEOUT, CHECKPOINT_INTEGRITY_FAILED |
| CRIU Mgr | CRIU_DUMP_FAILED/TIMEOUT, RESTORE_FAILED/TIMEOUT, PARTIAL_RESTORE, RESTORE_NOT_PERMITTED, CRIU/KERNEL/RUNTIME_INCOMPATIBLE |
| Validator | VALIDATION_FAILED, VALIDATION_INCONCLUSIVE, CHECKPOINT_LINEAGE_INVALID |
| Fence path | FENCING_FAILED, SOURCE_UNREACHABLE; **epoch invalidation succeeded ∧ source termination unverified ⇒ `RECONCILIATION_REQUIRED`** — never inferred success (§6 fencing boundary) |
| Registry CAS | VERSION_CONFLICT, ILLEGAL_TRANSITION, ACTIVE_MIGRATION_CONFLICT |

Escalation defaults: persistent UNKNOWN → reconciliation; SAFETY_CRITICAL failure → immediate stop-the-world for that migration + Recovery consult; BEST_EFFORT failure → log/audit only.

---

# 10. Idempotency Boundaries

| Boundary | Mechanism |
|---|---|
| Provider ops | `provider_client_token = operation_id` where supported; else query-before-act |
| Registry mutations | CAS on `(expected_version [, expected_epoch])` |
| Step advancement | CAS on `(plan_hash, step_id, expected_from_state)` |
| Event consumption | Ledger put-if-absent `(event_id, consumer_id)` precedes handler |
| Checkpoint persist | content-addressed manifest (checksum) ⇒ re-run converges |
| Cleanup actions | target-state verification before destructive act; repeat ⇒ no-op |
| Plan creation | duplicate (policy_decision_id, recovery_decision_id) pair ⇒ return existing plan_id |

---

# 11. Security Boundaries

| Principal | Grants (least privilege) |
|---|---|
| Orchestrator role (Coordinator) | Registry RW; Plan Store RW; S3 checkpoint bucket RW (KMS key A); EC2 Run/Terminate (tag-conditioned); NO direct worker SSH beyond fence/validate channels |
| Executor task roles | one per executor, scoped to its store + its AWS API slice; Provisioner cannot touch S3; Transfer cannot run instances |
| Worker instance profile | decrypt via KMS key A (checkpoint bucket), pull pinned runtime artifact, write telemetry — NOTHING else |
| Reconciliation role | read-everything, write findings; terminate ONLY via Cleanup Executor tag-conditioned policy |
| Human break-glass | audited, time-boxed, separate role |

Key/credential rules (from Protocols §17): checkpoints SSE-KMS(key A); secrets referenced not serialized; KMS-decryptability proven during readiness preflight; all security-sensitive operations emit mandatory audit events.

Fencing authority: exclusively the Coordinator executing a plan-authorized FENCE step — no other principal holds terminate-source permission without tag+epoch conditions.

---

# 12. Runtime / Deployment Topology

## 12.1 Runtime Classes

V2 deploys exactly **two runtime classes** plus managed storage:

| Runtime | Hosts | Count | Rationale |
|---|---|---|---|
| **Control Plane Runtime (CPR)** | Event Dispatcher + Ledger client, Coordinator, Planner, Policy Engine, Recovery Policy, Compatibility/Readiness/Placement engines, Reconciliation Manager, Cleanup Executor, Provisioner/Transfer/CRIU/Validator adapters, Config Manager, Audit & History writers | **exactly 1 active** (ASG min=max=1, multi-AZ replacement) | V2 chose an in-process dispatcher and defers leader election; executors are thin adapters over AWS/SSH, so separate processes would add failure modes without isolation benefit. All module boundaries are interface-bounded (§8), so extraction into separate services later does not require semantic change. |
| **Worker (data plane)** | workload process tree, worker controller (heartbeat / preflight / fence channel), `criu_wrapper.sh`, pinned CRIU + runtime | pool-scaled Spot fleet per candidate region | immutable AMI; holds zero control-plane logic; fully fenceable at all times |
| Storage | DynamoDB tables (§7.1), S3 checkpoint/artifact buckets, KMS | regional managed | accessed by CPR role; workers restricted to artifact/KMS/telemetry profile |

## 12.2 CPR Execution Model

- The Dispatcher runs a bounded thread pool consuming event subscriptions.
- Each active migration executes on a dedicated task; parallel DAG branches run as concurrent sub-tasks within that migration's budgets.
- A global semaphore enforces `MAX_CONCURRENT_MIGRATIONS`; per-pool counters are enforced at Placement admission and re-checked at Provision.
- All persistence access is CPR-local (§12.4); no component opens direct cross-region control writes.

## 12.3 Worker Lifecycle

```text
Provisioner launches instance (pool AMI; tags: job_id, migration_id, execution_epoch)
  → boot → worker controller up → heartbeat begins
  → PREFLIGHT: criu_wrapper.sh preflight → result cached in pool metadata
  → READY  (feeds Readiness evidence via Infrastructure Monitor;
            heartbeat/progress/preflight/refusal schemas: `docs/telemetry-contract.md`)
  → hosts workload (restore target or fresh start)
  → FENCEABLE at all times: controller owns the termination channel
  → CLEANUP: terminated via Cleanup Executor / Provisioner
```

Side paths:

- **Spot reclaim:** IMDS notice → worker writes local flag → Market/Risk ingestion flips EMERGENCY lane (Protocols §24).
- **Unhealthy:** heartbeat lapse or probe failure → Infrastructure Monitor evidence → policy decision (recover/restart).
- **Orphan:** periodic sweep compares live tag-bearing instances against Registry expectations → `ORPHAN_INSTANCE` finding.

**Autonomy prohibition (normative):** workers execute primitives only — dump/restore/verify/preflight, health reporting, flag ingestion. They MUST NOT decide or initiate: migrate, recover, replan, fence, supersede, or any ownership change. A local interruption flag is **evidence for the control plane**, never a trigger for autonomous migration action. Every decision-class action is reachable exclusively through CPR-issued, plan-authorized Commands.

## 12.4 Network Topology

- CPR deploys in the **registry home region**. Recommended: private subnets + VPC endpoints (`dynamodb`, `s3`, `kms`, `ec2`, `sts`); egress only where artifact pulls require it.
- Workers live in candidate regions. Inbound: SSH from CPR security group ONLY (dump/restore/verify/fence). No public inbound. Outbound: home-region S3 (checkpoints), KMS, runtime-artifact store, outbound telemetry/heartbeat channel.
- Security-group pairing is tag-conditioned; worker SG accepts traffic solely from the current CPR SG.

## 12.5 Persistence Access Patterns

Every DynamoDB/S3 control-plane access originates in CPR under one scoped role (§11). Workers use a distinct minimal profile (artifact read, KMS decrypt of checkpoint key, telemetry write). Retry/backoff honors the failure-taxonomy transience classification; sustained storage impairment triggers the fail-closed rule (§13 backpressure).

## 12.6 Worker-Controller Trust & Fence Authorization

The worker controller is the only worker-side component permitted to act on control commands. Trust and authorization:

| Mechanism | Rule |
|---|---|
| Channel identity | Control channel (SSH) restricted by security-group pairing to the current CPR SG — necessary but not sufficient on its own |
| Instance binding | Controller bootstraps identity from the EC2 instance identity document (IMDS), binding all commands to `(instance_id, admission_epoch)` recorded at pool join |
| Command authorization | Every fence/terminate/verify command carries `operation_id` + `expected_execution_epoch`; the controller accepts only if `expected_execution_epoch ≥` its recorded admission epoch — a stale or partitioned CPR therefore cannot fence a worker that has legitimately moved forward |
| Replay protection | `operation_id` dedup controller-side: repeated identical commands are acknowledged exactly once |
| Audit symmetry | Controller logs every accepted AND refused command; logs ship via telemetry and reconcile against CPR-side audit records |

A refused command (epoch mismatch, unknown operation, pairing failure) emits a refusal event into the telemetry stream — refusals are findings-grade evidence, never silent drops.

### 12.6.1 Implementation status and backlog (normative for planning)

The §12.6 trust ceremonies are specified but **unbuilt**: no worker
controller exists, SSH transports run raw commands, and no epoch/refusal
logic ships worker-side. Until the backlog below lands, fencing relies on
CPR-side epoch rotation plus SSH kill/verify (§10 coordinator wiring) and
pilots MUST restrict to retryable batch workloads.

| Item | Exit criteria |
|---|---|
| W1 worker controller | heartbeat + preflight + fence channel per §12.3 lifecycle; autonomy prohibition tested (I8) |
| W2 IMDS identity binding | boot binds `(instance_id, admission_epoch)`; commands outside the binding refused |
| W3 epoch-gated commands | `expected_execution_epoch ≥ admission_epoch` enforced controller-side; stale-CPR fence drill refused |
| W4 operation_id dedup | controller-side replay table; triple-execute converges (I7) |
| W5 refusal telemetry | T4 schema events flow to reconciliation event lane; drill asserts finding filed |

---

# 13. Scaling Model

## 13.0 Topology status: target vs interim (normative for planning)

§13.1–13.2 describe the **target** topology (dispatcher pool, per-migration
tasks, semaphore admission). The current implementation is an **interim
single-loop orchestrator**: one poll loop, no dispatcher runtime, token-bucket
rate limiting only. The interim is pilot-acceptable under these constraints,
all enforced in code today:

- `MAX_CONCURRENT_MIGRATIONS` token bucket is the admission ceiling;
  per-pool double-guard and dispatcher queues are backlog (Track C3).
- Admission beyond the ceiling returns rate-limited/skip, never queues (§13.2
  rule 1 holds).
- Multicast fan-out, transfer parallelism tuning, and dispatcher backpressure
  rules 4–5 are scheduled with the dispatcher build, before fleet scale.

Removing this subsection requires the dispatcher + semaphore + queue
implementation with §13 exit tests green.

## 13.1 Concurrency Ceilings

| Dimension | Mechanism | Default (config) |
|---|---|---|
| Global concurrent migrations | Coordinator semaphore + token bucket | `MAX_CONCURRENT_MIGRATIONS = 3`; `MAX_MIGRATIONS_PER_HOUR = 20`; global min-interval backoff |
| Per-pool concurrent migrations | checked at Placement AND Provision (double-guard) | `pool.max_concurrent_migrations` (default 2) |
| Migrations per job | Registry one-active guard | hard invariant I2 |
| Checkpoint dump concurrency | 1 per job | Protocol #9 §11.10 |
| Restore concurrency | 1 per target namespace | Protocol #9 §11.10 |
| Dispatcher fan-out | bounded thread pool + bounded per-consumer queues | `N = 4 × vCPU` |
| Transfer parallelism | S3 multipart 64 MB × 8 concurrent parts/object | tunable per pool network class |
| Ledger/Audit write volume | ~10–50 durable writes per migration; DynamoDB on-demand absorbs burst | on-demand capacity |

## 13.2 Backpressure Rules

1. **Refuse, don't queue migrations.** Admission beyond ceilings returns BUSY/CAPACITY — queueing hides deadlines and breaks feasibility math.
2. **Fail-closed persistence.** If a mandated ledger/audit write fails, the command is NOT issued (§7.2 boundary 4). Degrading to "issue anyway" is prohibited.
3. **Budget-bounded occupancy.** Every step carries `effective_deadline`; expiry releases the semaphore slot immediately and routes to Protocol #4 handling.
4. **Emergency preemption.** EMERGENCY evaluation bypasses queue position entirely and may supersede a pre-fence ARBITRAGE migration (`SUPERSEDED` path); it never waits behind routine work (Invariant I16).
5. **Slow consumer isolation.** Dispatcher queues are bounded per consumer; overflow parks events durably in the ledger rather than dropping them.

## 13.3 Scaling Non-Goals (V2)

Horizontal Coordinator sharding · multi-region active-active control plane · worker autoscaling beyond static pool definitions · dynamic multipart tuning mid-transfer.

---

# 14. HA / Failure Domains

## 14.1 Control-Plane Singularity

Exactly-one-CPR is enforced by **two independent layers**:

1. **Deployment shape:** ASG `min=max=1`, multi-AZ replacement.
2. **Self-fencing lease (not HA):** CPR must hold a `control_plane_lease` DynamoDB item (heartbeat 15 s, TTL 45 s). On lease loss the instance **stops issuing commands and exits**. This mechanism is explicitly *fencing of the control plane* — fail-stop self-limiting — and is deliberately **not** HA or leader election: there is no takeover logic, no election protocol, no quorum; the ASG's replacement instance simply acquires the now-free lease as a brand-new Coordinator.

Why this is sufficient: every state mutation in the system is CAS-guarded (Registry version/epoch, Plan Store `(plan_hash, step, from_state)`, provider client tokens, ledger put-if-absent). Even a pathological dual-writer converges or loses CAS. The lease removes the ambiguity earlier; CAS makes any residual race benign. Full HA/leader election remains deferred per Protocols.

## 14.2 Failure-Domain Matrix

| Domain | Failure | Detection | Response |
|---|---|---|---|
| CPR process crash (any point) | abrupt exit | ASG restart + lease reacquire | Bootstrap algorithm (§14.3) |
| CPR crash **mid-FENCING** | partially applied fence | fencing reconciliation matrix (§14.4) | per-matrix outcome: complete fence / forward-recover / `RECONCILIATION_REQUIRED` — never infer success |
| Duplicate CPR (misconfig) | dual writer possible | lease-conflict logs | second instance self-fences via lease; system state intact via universal CAS |
| AZ loss — control plane | CPR AZ impaired | ASG health checks | cross-AZ replacement; downtime ≈ boot (<2 min); in-flight ops resolved as UNKNOWN then reconciled on resume |
| AZ loss — storage | DynamoDB/S3 partition-impaired | SDK errors → taxonomy TRANSIENT/PERMANENT | retry/backoff within budget; sustained ⇒ `CONTROL_PLANE_UNAVAILABLE` ⇒ commands halted fail-closed; workers keep running workloads |
| Spot reclaim (worker) | 2-minute notice | worker flag → interruption ingestion | EMERGENCY fast-path regime |
| Worker unhealthy | heartbeat lapse / L1 probe fail | Infrastructure Monitor | readiness loss → Recovery Policy path |
| Provider API throttle/outage | 5xx/throttling | taxonomy classification | backoff inside remaining budget; exhaustion ⇒ `DEADLINE_EXCEEDED` semantics |
| KMS unavailable / denied | decrypt failure | readiness preflight + transfer errors | `KMS_UNAVAILABLE` ⇒ candidate `NOT_READY`; abort before irreversible steps |

## 14.3 Coordinator Bootstrap — State-Reconstruction Algorithm

Formal restart procedure; deterministic given identical durable inputs.

```text
BOOTSTRAP():
  1. Acquire control-plane self-fencing lease        # else exit
  2. IN_FLIGHT ← PlanStore.non_terminal_plans()      # step-state ∉ {SUCCESS, ABORTED, SUPERSEDED, FAILED}
             ∪ Registry.jobs_with(active_migration_id)
  3. FOR EACH m ∈ IN_FLIGHT:
       a. verify plan_hash(m.plan)                    # mismatch ⇒ tamper finding, halt migration
       b. IF m.state == FENCING:
            evaluate Fencing-Reconciliation-Matrix (§14.4)
            apply outcome; continue
       c. FOR EACH step ∈ m.steps WHERE state == RUNNING:
            outcome ← resolve_via_GetOperationStatusQuery(provider)   # bounded resolution budget
            commit SUCCEEDED/FAILED to Plan Store (CAS)
            unresolved ⇒ mark UNKNOWN → reconciliation finding; skip further steps
       d. recompute remaining budget:
            remaining = plan.absolute_deadline − now(wall clock)      # conservative post-crash;
            elapsed-since-step-start read from Plan Store step record # monotonic anchor is NOT
                                                                      # durable across restart
       e. IF reconciliation gate active for m.job: leave for Reconciliation sweep
          ELIF budget insufficient pre-fence: route Recovery Policy (REPLAN/FAILED)
          ELSE resume at executable frontier (all-deps-SUCCEEDED steps)
  4. Jobs with active_migration_id but NO matching non-terminal plan
       → DANGLING_MIGRATION_REFERENCE finding
  5. Trigger periodic reconciliation sweep once (orphans, stale fences)
  6. Resume admission (semaphore + rate limiter) with reconstructed counters
```

Notes:

- Step-start wall-clock timestamps ARE persisted in the Plan Store step records (excluded from `plan_hash` as volatile, but present for reconstruction).
- The monotonic-clock budget authority survives only within a single CPR life; across restarts the absolute wall-clock deadline is the conservative reconstruction basis, exactly as Protocols §2.2 prescribes.
- Bootstrap is idempotent: crashing during bootstrap and rerunning converges to the same decisions because every classification derives from durable state plus provider queries.

## 14.4 Fencing Reconciliation Matrix

Authoritative decision table when a Coordinator (re)discovers a migration whose last persisted state was `FENCING`, or whenever fencing completion is uncertain:

| # | Epoch invalidated? | Source process alive? | Target state | Classification | Action |
|---|---|---|---|---|---|
| 1 | NO | alive | absent / not restored | fence never began | resume plan at FENCING frontier (pre-conditions re-checked) |
| 2 | YES | dead | restored | fence complete control-side; activation pending | proceed VALIDATING → ACTIVATING per plan |
| 3 | YES | dead | absent / restore lost | fenced; recovery artifact path needed | Recovery Policy → `FORWARD_RECOVERY` from DURABLE checkpoint |
| 4 | YES | **alive** | *any* | **SPLIT_BRAIN candidate** | `RECONCILIATION_REQUIRED` (P0); no automatic choice between source/target |
| 5 | UNKNOWN | *any* | *any* | ownership unverified | `RECONCILIATION_REQUIRED` |
| 6 | YES | UNKNOWN (unreachable/unverifiable within budget) | ready | termination unverified | `RECONCILIATION_REQUIRED` |

Hard rules: rows 4–6 never auto-resolve in favor of source OR target; row 2 is the ONLY path that may proceed toward ACTIVATING without fresh Recovery Policy consultation; epoch invalidation alone (without a verified-dead source) can never satisfy "fencing confirmed" (§6 boundary).

## 14.5 Durability Targets

| Property | Target |
|---|---|
| Acknowledged state mutations lost on CPR crash | **0** — every ack follows its durable commit |
| External side effects per logical operation | at-most-once (idempotency boundaries §10) |
| Control-plane RTO | ≤ ~2 minutes (ASG replacement + bootstrap scan) |
| Control-plane RPO | 0 (all authority lives in durable stores) |
| Workload continuity during control-plane outage | unaffected — workers execute independently; interruptions during outage resolve via local flags + post-restoration sweep |

---

# 15. Cross-Region Architecture

## 15.1 Region Roles

| Region | Role |
|---|---|
| **Home** (registry region, e.g., `us-east-1`) | ALL DynamoDB tables · S3 checkpoint bucket · KMS key · CPR deployment · Configuration Store |
| **Candidate regions** (e.g., `us-west-2`, `eu-west-1`, `ap-south-1`) | Worker fleets (source and target), regional provider observation |

Single-writer simplicity: all control-plane writes land in Home. Candidate regions host no authoritative state.

## 15.2 Data Placement

| Data | Placement | Cross-region flow |
|---|---|---|
| Control-plane state | Home only | control ops tolerate WAN RTT; ARBITRAGE/PROACTIVE insensitive, EMERGENCY adds fixed WAN allowance to deadline math (§15.4) |
| Checkpoints | **Home bucket (hub)** | SOURCE region --upload--> HOME --download--> TARGET. Feasibility transfer estimate = leg1 + leg2 + margin. Hub-and-spoke keeps one durability/KMS/IAM story; direct source→target peer copy is a reserved future optimization requiring per-region buckets + multi-region keys |
| Runtime artifacts | replicated read-only per region (pull-through cache) | workers read locally; digest-pinned |
| Telemetry / logs | regional aggregation, shipped Home asynchronously | never gating; schemas + freshness rules: `docs/telemetry-contract.md` |

## 15.3 KMS Strategy

One home-region key protects the checkpoint bucket. Cross-region decrypt is principal-based: worker roles in candidate regions are granted decrypt in the key policy; S3 serves ciphertext region-locally from the bucket endpoint. Multi-region keys (MRK) are the designated migration path IF per-region buckets are introduced later — noted, not built, in V2.

## 15.4 Latency Classes

| Class | Path | Budget treatment |
|---|---|---|
| Control loop | CPR ↔ DynamoDB/S3 (Home-local) | milliseconds — negligible vs step timeouts |
| Command channel | CPR ↔ worker SSH (cross-region) | counted inside each step timeout |
| Emergency fast path | regional interruption ingest → Home decision → cross-region commands | absolute deadline includes a configured per-region-pair WAN allowance established at ingestion |
| Transfer legs | source→Home, Home→target | dominant term of the feasibility critical path; measured throughput feeds Estimator feedback (Protocol #13) |

## 15.5 Regional Failure Semantics

- **Candidate-region outage:** capacity/readiness evidence marks pools unavailable; Placement excludes them; existing workers there follow normal spot/interruption handling.
- **Home-region outage:** total control-plane halt — an explicitly accepted V2 limitation (§14.2). Running workloads continue uninterrupted; interruptions occurring during the outage leave local flags and dangling expectations that the post-restoration reconciliation sweep resolves (orphans, stale migrations, unverified fences).

---

# 16. V1 → V2 Migration Architecture

## 16.1 Engine Admission & Routing

`engine_version` is an **admission/routing decision, never an execution-semantics switch**. Once a migration attempt exists under an engine, that engine's protocols are exclusively authoritative for it.

```text
Workload submission
        │
        ▼
   Admission Manager
        │
        ├── contract unsupported by V2  → engine_version = V1 (or REJECT per policy)
        ├── V2-compatible               → engine_version = V2
        ▼
Job created in Registry with immutable engine_version
```

Rules:

1. `engine_version` is set once at admission and is **immutable for the job and for every migration attempt** of that job.
2. No runtime fallback: a failing V2 migration resolves through V2 terminal semantics (`FAILED` / `RECOVERY_REQUIRED` / `RESTART_REQUIRED`). Silent substitution with V1 mid-flight is prohibited — it would change execution semantics halfway through a migration.
3. Routing selects which decision/planning/execution pipeline owns new migration attempts; it does not fork the substrate.

## 16.2 Coexistence Isolation Boundary

V1 may continue to exist **only behind the shared substrate**. Anything V1 does outside this table constitutes an alternate mutation path around the frozen invariants and is an architecture violation.

| Substrate layer | Shared by V1+V2? | Constraint on V1 |
|---|---|---|
| Job Registry CAS (`transition()`) | **Shared — sole path** | V1 MUST request job/migration transitions through the same CAS API; direct writes prohibited |
| Identity rules (`operation_id` ULID, reuse-on-retry) | Shared | V1 commands carry Coordinator-style operation IDs |
| Event Ledger + dispatcher | Shared | V1 emits/consumes through the same dedup gate |
| Checkpoint Metadata Store + durability model | Shared | V1 checkpoints obey `LOCAL→PERSISTING→DURABLE`; partial artifacts never DURABLE |
| Mandatory audit events | Shared | V1 emits the same event set for its migrations |
| Reconciliation gate | Shared | V1-owned jobs are equally subject to `RECONCILIATION_REQUIRED` blocking |
| Fencing / epoch semantics | Shared conceptually | V1 fencing must produce equivalent evidence (epoch invalidation + verified termination) or its migrations are ineligible for cross-engine supersession |
| Plan DAG / PlanStep state machine | **V2-only** | V1 retains its linear step model internally; not exposed as plans |
| Recovery Policy / Placement engines | **V2-only** | V1 uses its legacy decision engine on V1-owned jobs only |

Implementation shape: the V1 migrator is wrapped as a **legacy adapter** implementing the Command surface where feasible; where legacy internals cannot comply, the adapter restricts V1 to V1-owned jobs exclusively, and those jobs never enter V2 code paths.

## 16.3 Ownership & Cross-Engine Supersession Bridge

```text
Job (engine_version = V1 | V2)
 └── active_migration_id → owned by exactly one engine at any moment
```

| Scenario | Verdict | Procedure |
|---|---|---|
| EMERGENCY trigger for a job whose current migration is **V1, pre-fencing** | **Permitted — V2 supersedes** | (1) V2 verifies pre-fence state AND source intact via shared Registry/audit evidence; (2) Registry CAS conditional transition marks the V1 migration `SUPERSEDED`, conditioned on expected version + pre-fence state; (3) V1 adapter observes supersession idempotently and executes FULL_ROLLBACK cleanup of any V1-provisioned target; (4) V2 creates M2 with `regime=EMERGENCY`. The bridge is admissible precisely because pre-fence V1 state maps cleanly onto V2's rollback-capable phase. |
| Current migration is **V1, post-fencing** | **Prohibited** | forward recovery stays inside the owning engine; V2 may take over only after that migration reaches a terminal state and a fresh decision routes there |
| Any V2 → V1 handoff | **Prohibited** | no fallback direction exists (§16.1 rule 2) |

Bridge requirements: both engines must agree on `SUPERSEDED` semantics for the shared Registry record, and the V1 adapter must honor supersession notices idempotently (it may be crashed at any point of its cleanup).

## 16.4 Cutover & Removal Criteria

```text
Phase 0  V2 shadow    : V2 runs decision/plan/validation read-only beside V1 executions;
                        divergence reports only
Phase 1  V2 canary    : engine_version=V2 admitted for selected workload classes
Phase 2  V2 default   : new admissions default V2; V1 by explicit override
Phase 3  V1 freeze    : no NEW V1 admissions; existing V1 jobs run to natural completion
Phase 4  V1 removal   : zero V1-owned jobs ∧ ≥N days without V1 invocation
                        ⇒ delete adapter + legacy pipeline
```

Exit criteria between phases are operational (success-rate parity, audit parity, reconciliation-finding rates), never calendar-based.

## 16.5 Binding to ADR-024 deletion gates (normative)

`docs/adr/ADR-024-v1-deletion.md` is the executable form of §16.4. Mapping:

| §16.4 phase | ADR-024 gate | Meaning |
|---|---|---|
| Phase 0 V2 shadow | pre-emulated-green | V2 decides/plans read-only beside V1; divergence reports only |
| Phase 1 V2 canary | — | `engine_version=V2` admitted for selected classes |
| Phase 2 V2 default | AWS E2E green | new admissions default V2; V1 by explicit override |
| Phase 3 V1 freeze | emulated green (already declared) | no new V1 admissions; `--engine v1` compat only |
| Phase 4 V1 removal | soak gate | zero V1-owned jobs ∧ N days without V1 invocation ⇒ delete |

Backlog (unbuilt, required before Phase 2): the **legacy adapter** (§16.2
table), the **cross-engine supersession bridge** (§16.3 procedure), and the
admission router (§16.1). Until they land, the only permitted coexistence is
the current one: V1 frozen behind `--engine v1` on V1-owned jobs, V2 on V2
jobs, no cross-engine supersession. Any V1 action outside the §16.2 shared
substrate (Registry CAS, ledger dedup, durability model, audit events,
reconciliation gate) is an architecture violation — today V1 bypasses the
CAS path (`Migrator` direct writes), which is grandfathered exclusively
until Phase 4 removal.

---

# 17. Implementation Dependency Graph

Build order derived from §5; each layer gates the next. "Fake" = Protocol #16 deterministic test double shipped alongside the real implementation.

```text
L0  Domain Contracts
    types · C/Q/E envelopes · failure taxonomy · state enums · errors
    exit: schemas validated; contract tests compile against fakes
        │
L1  Persistence Substrate
    Registry+CAS · Plan Store · Event Ledger · Config Store
    Audit writer · Checkpoint Metadata store · History store
    (+ DynamoDB/S3 repositories, control_plane_lease record)
    exit: CAS / dedup / plan-hash property tests green (fakes + LocalStack)
        │
L2  Protocol Infrastructure
    Dispatcher · Clock (wall+monotonic injectable) · DI seams
    idempotency helpers · correlation-envelope middleware
    exit: ledger-before-dispatch, dedup, stale-event tests green
        │
L3  Execution Primitives          ── each ships Real + Fake ──┐
    Provisioner · TransferManager                             │
    CRIUManager (+ criu_wrapper.sh contract)                  │
    Security/IAM/KMS Gateway (preflight)                      │
    exit: GATE-A = checkpoint→persist→transfer→restore E2E    │
          against fixture worker + fake S3/KMS                │
        ▼                                                     │
L4  Correctness                                               │
    Validator (L1–L4, tolerance contracts, snapshot capture)  │
    exit: golden-path validation + injected                   │
          INVALID / INCONCLUSIVE scenarios                    │
        ▼                                                     │
L5  Orchestration                                             │
    MigrationCoordinator                                      │
    · §10 DAG execution · budgets & final gates               │
    · bootstrap algorithm (§14.3) · fencing matrix (§14.4)    │
    exit: GATE-B = normal-migration E2E on full fakes;        │
          crash-at-every-state recovery tests green           │
        │
        ├──► L6 Decision / Planning
        │      Planner · RecoveryPolicy · PolicyEngine
        │      Compat / Readiness / Placement engines
        │      exit: GATE-C = replan + emergency-supersession +
        │            deadline-exhaustion scenarios green
        │            (chaos S01–S03, S15–S17)
        │
        └──► L7 Resilience & Feedback
               ReconciliationManager · CleanupExecutor
               HistoryWriter · Observability wiring
               exit: orphan/sweep/split-brain/event-ordering
                     scenarios green (S12–S14, S20–S25)
```

Module↔component mapping follows §2 numbering one-to-one. Package boundaries mirror component boundaries so dependency-graph violations are import-time visible; a lint rule enforces §5 acyclicity mechanically.

---

# 18. Architecture Invariants

Normative, individually testable properties. Each names its verification vehicle (property test P#, chaos scenario S#, contract test C# per Protocols §18).

### Availability / continuity

**IA-1 — Control-plane crash ≠ workload failure.**
CPR death at any instant never terminates a running workload; coordination resumes via §14.3.
*Verify:* kill CPR at every migration state (chaos harness); assert worker-process continuity.

### Safety

**I1 — Single active migration.**
`∀ job: active_migration_count ≤ 1`, enforced by Registry guard.
*Verify:* concurrent admission attempts (C#).

**I2 — Fencing irreversible once begun.**
Entering FENCING forbids rollback paths regardless of deadline or failure.
*Verify:* fault-inject after FENCING-start; assert no source-restoring action occurs (S11).

**I3 — Source protection before confirmation.**
`¬fencing_confirmed ⇒ source execution remains protected/authoritative`.
*Verify:* state-machine property test across all legal histories.

**I4 — No blind UNKNOWN retry.**
`UNKNOWN(op) ⇒ resolve ∨ reconcile`; blind reissue prohibited.
*Verify:* outcome-injection UNKNOWN at every executor (S23).

**I5 — One authoritative mutation path.**
Job/Migration-domain mutations occur only via validated `Registry.transition()` CAS commits.
*Verify:* repository-layer contract test rejecting alternate writers + import lint.

**I6 — Plan immutability.**
An admitted plan's body never mutates; successors use `supersedes_plan_id`.
*Verify:* hash check on every read; mutation-attempt test (C#).

**I7 — Operation idempotency.**
Same `operation_id` ⇒ same logical external operation; replays converge.
*Verify:* triple-execute every command type (C#).

**I8 — Worker decision-authority prohibition.**
Workers cannot decide migrate/recover/replan/fence/supersede; only CPR-issued, plan-authorized Commands actuate them.
*Verify:* stale-epoch fence refusal (§12.6); flag-file presence alone triggers no migration.

**I9 — Reconciliation gate blocks recovery.**
`OWNERSHIP_UNCERTAIN ∨ SPLIT_BRAIN ⇒ RECONCILIATION_REQUIRED ∧ RecoveryPolicy refuses to decide`.
*Verify:* fencing-matrix rows 4–6 (S13).

**I10 — Deadline safety vs fencing.**
Deadline breach never interrupts an in-progress ownership transition.
*Verify:* expire clock mid-FENCING; assert safe conclusion (S17 variant).

**I11 — Single persistence authority per datum.**
§6 matrix holds; no datum has two committing authorities.
*Verify:* matrix-conformance review + repo-layer tests.

**I12 — Engine immutability.**
`engine_version` fixed at admission; no cross-engine fallback; post-fencing cross-engine takeover prohibited.
*Verify:* routing tests + supersession-bridge negative cases (§16.3).

**I13 — Partial ≠ durable.**
A partially written checkpoint artifact can never satisfy `DURABLE`.
*Verify:* corruption injection during persist (S05).

**I14 — Audit-before-irreversible.**
Mandated audit events are durably committed before their irreversible command issues.
*Verify:* fail the audit write; assert the command was NOT issued (fail-closed).

**I15 — UNKNOWN eligibility exclusion.**
Compatibility/readiness `UNKNOWN` is never migration-eligible — emphatically in EMERGENCY.
*Verify:* placement hard-filter property test.

These properties constitute the acceptance bar for the Implementation Architecture: every PR touching orchestration or persistence must cite which invariants its tests exercise.

---

**Next artifact:** *Implementation Architecture Specification* — repository/module layout, Python package boundaries, concrete persistence repositories, DI composition root, AWS resource definitions (Terraform-level), and the test-seam catalogue. This document reaches full architecture freeze once §1–18 pass review.

---

# Appendix A — Catalogue Quick Reference

| # | Component | Layer | Primary Protocol |
|---|---|---|---|
| 1 | Workload Admission Manager | Admission | #1 |
| 2 | Market/Risk Monitor | Observation | #3(deadline),#13 |
| 3 | Infrastructure Monitor | Observation | #5,#11 |
| 4 | Workload Estimator | Observation | #13 |
| 5 | Policy Engine (Arb/Pro) | Decision | #7(prec),#12 |
| 6 | Recovery Policy | Decision | #7 |
| 7 | Compatibility Engine | Planning | #5,#9 |
| 8 | Readiness Engine | Planning | #5,#15 |
| 9 | Placement Engine | Planning | #5,#14 |
| 10 | Migration Planner | Planning | #8 |
| 11 | Migration Coordinator | Exec-Control | #3,#4,#6,#8 |
| 12 | Provisioner | Execution | #6,#15 |
| 13 | Transfer Manager | Execution | #9,#15 |
| 14 | CRIU/Checkpoint Manager | Execution | #9 |
| 15 | Validator | Correctness | #10 |
| 16 | Reconciliation Manager | Consistency | #11 |
| 17 | Cleanup Executor | Consistency | #4,#11 |
| 18–25 | Eight Persistence Stores | Persistence | #2,#6,#12–#14 |
| 26 | Config/Version Manager | Cross | #14 |
| 27 | Security/IAM/KMS Gateway | Cross | #15 |
| 28 | Event Dispatcher+Ledger | Cross | #6 |
| 29 | Observability/Audit | Cross | #12 |
| 30 | History Writer | Cross | #13 |
