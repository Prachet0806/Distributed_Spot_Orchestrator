# Distributed Spot Arbitrage Orchestrator — Overall Architecture & Components Map

**Architecture status:** V2 architectural baseline  
**Scope:** Overall system architecture, component boundaries, responsibilities, interactions, and cross-cutting principles.

---

## 1. System Purpose

The system is a **cost-optimization orchestrator for long-running, checkpointable workloads** running on cloud compute.

### Primary objectives

1. Reduce compute cost by exploiting Spot/interruptible capacity over On-Demand compute.
2. Use migration as a cost-optimization mechanism.

### Constraint

Migration may introduce up to approximately **20% completion-time overhead**.

### Current workload scope

V1/V2 initially targets **checkpointable workloads**, while keeping the architecture extensible to broader workload classes through workload adapters and estimation.

### Recovery model

On Spot interruption, the system attempts to preserve progress and resume from approximately the same execution point.

**Exactly-once execution is a future goal, not a V2 guarantee.**

### HA model

V2 assumes **at most one active Migration Coordinator is authorized to execute a given migration**.

Leader election, leases, and full coordinator HA are deferred.

---

# 2. Core Architectural Principle

The architecture enforces:

> **Observation ≠ Estimation ≠ Economics ≠ Policy ≠ Execution ≠ Validation**

| Concern | Meaning |
|---|---|
| Observation | What is happening / what is currently observed? |
| Estimation | What is likely to happen? |
| Economics | What are the expected costs, savings, and risks? |
| Policy | What should the system do? |
| Execution | Actually perform the operation. |
| Validation | Determine whether the resulting state/workload is correct. |

This separation is especially important because the system has two operating regimes:

- **Normal arbitrage:** can wait for better information.
- **Emergency recovery:** must act within an interruption deadline and therefore requires dedicated fast paths and conservative fallbacks.

---

# 3. Overall Component Map

```text
                              ┌──────────────────────────────┐
                              │        External World        │
                              │ Cloud Market / Infrastructure│
                              │ Workloads / Workers / Events │
                              └───────────────┬──────────────┘
                                              │
              ┌───────────────────────────────┼────────────────────────────┐
              │                               │                            │
              ▼                               ▼                            ▼
     ┌──────────────────┐           ┌──────────────────┐          ┌──────────────────┐
     │  Market & Risk   │           │ Infrastructure   │          │ Workload Adapter │
     │     Monitor      │           │     Monitor      │          │                  │
     │   Component 3    │           │   Component 4    │          │   Observation    │
     └────────┬─────────┘           └────────┬─────────┘          └────────┬─────────┘
              │                              │                             │
              │                              │                             ▼
              │                              │                    ┌──────────────────┐
              │                              │                    │ Workload         │
              │                              │                    │ Estimator        │
              │                              │                    │ Component 2      │
              │                              │                    └────────┬─────────┘
              │                              │                             │
              └────────────────┬─────────────┴─────────────────────────────┘
                               │
                               ▼
                    ┌──────────────────────────┐
                    │      Job Registry         │
                    │      Component 1          │
                    │                            │
                    │ Authoritative logical      │
                    │ job/control-plane state    │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │   Cost & Risk Evaluator  │
                    │      Component 5          │
                    │                            │
                    │ Normal economics           │
                    │ Recovery feasibility      │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
                    ▼                         ▼
            ┌─────────────────┐       ┌─────────────────┐
            │ Arbitrage /     │       │ Recovery Policy │
            │ Decision Engine │       │                 │
            └────────┬────────┘       └────────┬────────┘
                     │                         │
                     └────────────┬────────────┘
                                  ▼
                         ┌──────────────────┐
                         │ Migration Planner│
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │    Migration     │
                         │    Coordinator   │
                         └───────┬──────────┘
                                 │
              ┌──────────────────┼────────────────────┐
              │                  │                    │
              ▼                  ▼                    ▼
       ┌──────────────┐  ┌──────────────┐    ┌────────────────┐
       │ Checkpoint   │  │ Provisioner  │    │ Transfer /     │
       │ Manager      │  │              │    │ Restore        │
       └──────────────┘  └──────────────┘    └────────────────┘
                                                       │
                                                       ▼
                                               ┌────────────────┐
                                               │ Validation     │
                                               │ Manager        │
                                               └────────────────┘
```

---

# 4. Component Inventory

| # | Component | Primary question | Primary responsibility |
|---|---|---|---|
| 1 | **Job Registry** | What is true about this job now? | Authoritative logical job/control-plane state |
| 2 | **Workload Estimator** | What is likely to happen next? | Workload behavior prediction |
| 3 | **Market & Risk Monitor** | What is happening in the market/risk environment? | Market observation and risk estimation |
| 4 | **Infrastructure Monitor** | What is actually happening to infrastructure? | Infrastructure observation |
| 5 | **Cost & Risk Evaluator** | What are the expected consequences? | Economic/risk analysis |
| 6 | **Arbitrage Policy / Decision Engine** | Should we pursue normal migration? | Normal arbitrage decision |
| 7 | **Recovery Policy** | What should we do under interruption? | Emergency recovery decision |
| 8 | **Migration Planner** | How should the action be executed? | Concrete migration/recovery plan |
| 9 | **Migration Coordinator** | How do we execute and reconcile the plan? | Migration orchestration |
| 10 | **Checkpoint Manager** | How do we capture durable state? | Checkpoint creation/persistence |
| 11 | **Provisioner** | How do we acquire target infrastructure? | Infrastructure execution |
| 12 | **Transfer / Restore** | How do we move and restore workload state? | State movement and restoration |
| 13 | **Validation Manager** | Did migration/recovery actually work? | Post-operation correctness validation |
| 14 | **Control / Reconciliation** | What if independently observed truths disagree? | Cross-component reconciliation |
| 15 | **Migration History** | What actually happened? | Historical migration outcomes |
| 16 | **Observability / Telemetry** | What happened over time? | Metrics, logs, traces, time series |

---

# 5. Component 1 — Job Registry

## Purpose

The authoritative, durable, concurrency-safe representation of the logical job.

> **What is true about this job right now?**

A logical job persists across migrations.

```text
job-123
 ├── migration-001
 ├── migration-002
 └── migration-003
```

## Owns

- Job identity
- Workload profile/capabilities
- Current lifecycle state
- Current execution context
- Latest workload estimate snapshot
- Liveness snapshot
- Active migration reference
- Migration reason
- Version/concurrency metadata

## V2 lifecycle

```text
PENDING
   ↓
STARTING
   ↓
RUNNING
   │
   ├──→ MIGRATING ──success────→ RUNNING
   │       │
   │       └──failure→ RECOVERY_REQUIRED
   │                         │
   │                         ▼
   │                  RESTART_REQUIRED
   │                         │
   │                         ▼
   │                      STARTING
   │
   └──→ COMPLETING
              ↓
          COMPLETED

Unrecoverable path → FAILED
```

## Important boundary

The Registry never decides:

- whether to migrate
- which target to select
- whether migration is economically beneficial
- how to execute migration
- whether recovered application state is correct

## Migration visibility

The Job lifecycle remains:

```text
MIGRATING
```

while the migration record carries:

```text
migration_reason:
  ARBITRAGE
  INTERRUPTION_RECOVERY
```

## Key invariants

- At most one active migration in V2.
- Stale updates cannot overwrite newer state.
- A migration cannot remain permanently dangling.
- Terminal migration finalization clears `active_migration_id`.
- Logical job state is authoritative.
- A local checkpoint does not automatically imply recoverability.
- Migration finalization should atomically reconcile migration and job state where possible.

---

# 6. Component 2 — Workload Estimator

## Purpose

Predict workload behavior from observations.

> **What is likely to happen next, given what we currently observe?**

```text
Workload
   ↓
Workload Adapter
   ↓
Observation Validation
   ↓
Workload Estimator
   ↓
WorkloadEstimate
```

## Output

```text
WorkloadEstimate
├── progress
├── remaining_runtime
├── expected_completion
├── confidence
├── checkpoint_size_estimate
├── checkpoint_duration_estimate
├── execution_context
├── observed_at
├── generated_at
└── estimator_version
```

## Owns

- Progress estimation
- Remaining runtime prediction
- Completion prediction
- Confidence
- Checkpoint size/duration prediction
- Resource-context-aware estimation
- Re-baselining after migration
- Prediction-error feedback

## Does not own

- Job lifecycle
- Migration decisions
- Economics
- Checkpoint execution
- Infrastructure state
- Recovery policy

## Critical rules

### Insufficient information is valid

```text
remaining_runtime = UNKNOWN
confidence = LOW
```

is preferable to fabricated precision.

### Post-migration re-baselining

Changing execution resources invalidates or reduces confidence in the previous performance estimate.

### Checkpoint prediction feedback

Track estimated vs actual checkpoint size and duration.

### Ordering authority

The Estimator may perform defensive stale-observation checks.

The **Job Registry is authoritative** for persisted ordering.

---

# 7. Component 3 — Market & Risk Monitor

## Purpose

Observe and normalize external compute-market conditions and derive risk signals.

> **What is happening in the compute market and associated risk environment?**

## Outputs

### MarketSnapshot

```text
pool_id
region
AZ
instance_type
spot_price
on_demand_price
volatility
observed_at
source
```

### RiskSnapshot

```text
pool_id
interruption_probability
interruption_frequency
capacity_risk
confidence
observed_at
model_version
```

### CandidatePoolSnapshot

Combines market/risk information with explicit freshness and coherence metadata.

## Owns

- Spot price observation
- On-Demand price observation
- Price history/volatility
- Interruption event ingestion
- Interruption history
- Risk-model orchestration
- Market/risk freshness
- Snapshot coherence
- Source reconciliation
- Conservative risk bias
- Risk-model fallback

## Does not own

- Provisioning
- Migration
- Candidate selection
- Migration economics
- Job lifecycle
- Recovery decisions

## Capacity boundary

Capacity signals may only come from:

- provider-published signals
- historical provisioning outcomes
- asynchronous provisioning records

The Monitor must **never launch an instance to probe capacity**.

## Snapshot coherence

A snapshot must explicitly indicate whether its constituent observations fall within the configured coherence window.

Freshness and coherence are distinct.

## Conflicting risk sources

For valid, comparable sources, prefer the higher credible risk estimate.

Stale or invalid sources do not win merely because their value is higher.

## Risk fallback

```text
Primary risk model
       ↓ failure
Statistical / historical model
       ↓ failure
Last valid estimate
       ↓
UNKNOWN
```

Confidence and model source/version identify the tier used.

---

# 8. Market & Risk Monitor — Emergency Fast Path

Spot interruption events are **not ordinary market data**.

```text
Spot interruption notice
          ↓
Event ingestion
          ↓
Validate / deduplicate
          ↓
Immediate dispatch
          ↓
Recovery Policy
```

Do not wait for:

- a fresh price
- a coherent market snapshot
- the next polling cycle

Cached/stale risk information may still be supplied as degraded context.

---

# 9. Component 4 — Infrastructure Monitor

## Purpose

Observe actual infrastructure state.

> **What is actually happening to the infrastructure?**

## Owns observation of

- Instance lifecycle
- Instance health
- Worker connectivity
- Execution location
- Provisioning outcomes
- Termination outcomes
- Infrastructure anomalies
- Infrastructure events

## Does not own

- Provisioning
- Termination
- Migration
- Job lifecycle
- Migration target selection
- Economics
- Recovery decisions

## Observation boundary

```text
Infrastructure Monitor
        ↓
OBSERVES
```

Never:

```text
Infrastructure Monitor
        ↓
PROBES BY LAUNCHING
```

Capacity evidence from provisioning must come from actual recorded outcomes.

---

# 10. Infrastructure Monitor — Emergency Observation Lane

Routine:

```text
Periodic polling / provider events
        ↓
Infrastructure Monitor
        ↓
InfrastructureSnapshot
```

Emergency:

```text
Recovery request
      ↓
Emergency Observation API
      ↓
Synchronous provider query and/or push subscription
      ↓
Current infrastructure state
```

The emergency path must not depend solely on the next routine polling cycle.

### Emergency invariant

> The emergency observation path must have a bounded low-latency response appropriate to the remaining recovery deadline.

---

# 11. Cross-Component Reconciliation

Infrastructure and logical job state intentionally have different authorities:

```text
Infrastructure Monitor
→ actual infrastructure observation

Job Registry
→ authoritative logical job state
```

If they disagree:

```text
Infrastructure Monitor:
instance-i42 terminated

Job Registry:
job-123 still owns instance-i42
```

the **Control / Reconciliation** layer resolves the discrepancy.

```text
Infrastructure Monitor
        ↓ discrepancy
Control / Reconciliation
        ↓
Job Registry
        ↓
appropriate state/control action
```

---

# 12. Component 5 — Cost & Risk Evaluator

## Purpose

Turn workload, market, infrastructure, migration, and risk information into normalized economic/risk analysis.

> **What are the expected consequences of each option?**

It does not decide what to do.

## Interfaces

```python
evaluate_stay(...) -> CostAnalysis

evaluate_candidate(...) -> CostAnalysis

evaluate_recovery(...) -> RecoveryFeasibility
```

---

# 13. RecoveryFeasibility

Recovery feasibility is distinct from ordinary cost analysis.

```text
RecoveryFeasibility
├── feasible
├── deadline
├── remaining_budget
├── checkpoint_duration
├── provisioning_duration
├── transfer_duration
├── restore_duration
├── validation_duration
├── critical_path_duration
├── completion_probability
├── confidence
├── assumptions
└── generated_at
```

It answers:

> **Can this recovery strategy complete within the interruption deadline?**

---

# 14. Emergency Recovery Timing

Recovery feasibility models concurrency.

```text
              ┌── Checkpoint ────────┐
Start ────────┤                       ├── Transfer
              └── Provision Target ──┘
                                      ↓
                                   Restore
                                      ↓
                                   Validate
```

Therefore recovery duration is based on the **critical path**, not blindly summing all operations.

Example:

```text
T_recovery =
    max(T_checkpoint, T_provision)
    + T_transfer
    + T_restore
    + T_validation
```

subject to the actual plan.

---

# 15. Emergency Evaluation Latency

Evaluation latency and recovery duration are different:

```text
Evaluation latency
= time required to calculate feasibility

Recovery duration
= expected time to execute recovery
```

`evaluate_recovery()` must:

- be computationally fast
- have a bounded latency appropriate to the interruption deadline
- avoid blocking external infrastructure operations

---

# 16. CostAnalysis

```text
CostAnalysis
├── current_cost
├── candidate_cost
├── migration_cost
│   ├── checkpoint
│   ├── storage
│   ├── transfer
│   ├── provisioning
│   └── restore
├── expected_risk_cost
├── expected_total_cost
├── expected_savings
├── completion_time_delta
├── risk
├── confidence
├── assumptions
└── generated_at
```

Migration cost must remain decomposable.

---

# 17. Completion-Time Constraint

The project allows approximately:

```text
≤ 20% completion-time overhead
```

The evaluator reports:

```text
completion_time_delta
```

but the Policy/Decision layer determines whether the overhead is acceptable.

Thus:

```text
Economics ≠ Policy
```

---

# 18. Normal vs Emergency Decision Pipeline

## Normal arbitrage

```text
Job Registry
     ↓
Workload Estimate
     ├───────────────┐
     ↓               ↓
Market/Risk      Infrastructure
     └───────┬───────┘
             ↓
      Cost & Risk Evaluator
             ↓
        CostAnalysis
             ↓
      Arbitrage Policy
             ↓
        Decision Engine
```

Normal arbitrage can wait for better information.

## Emergency recovery

```text
Spot interruption
       ↓
Immediate event dispatch
       ↓
Recovery Policy
       ├── workload estimate / conservative fallback
       ├── emergency infrastructure observation
       └── cached market/risk context
       ↓
Recovery Feasibility
       ↓
Recovery decision
       ↓
Emergency Migration Plan
```

---

# 19. Decision / Policy Boundary

Information/analysis components:

```text
Job Registry
Workload Estimator
Market & Risk Monitor
Infrastructure Monitor
Cost & Risk Evaluator
```

Decision components:

```text
Arbitrage Policy / Decision Engine
Recovery Policy
```

Only the decision layer chooses:

```text
STAY
MIGRATE
DEFER
RECOVER
ABANDON
```

---

# 20. Execution Boundary

```text
Policy / Decision
        ↓
Migration Planner
        ↓
Migration Plan
        ↓
Migration Coordinator
        ↓
Execution components
```

Execution components include:

- Checkpoint Manager
- Provisioner
- Transfer Manager
- Restore Manager
- Validation Manager

Observation components never perform infrastructure operations merely to observe state.

---

# 21. Migration Planner

The Planner converts a decision into a concrete execution strategy.

Owns:

- target selection within decision constraints
- migration sequence
- normal vs emergency strategy
- deadline-aware plan construction
- checkpoint/transfer/restore ordering
- supported parallelism
- speculative provisioning where explicitly supported

Does not:

- make economic decisions
- execute infrastructure operations
- validate final correctness

For emergency recovery it may emit:

```text
checkpoint + provision in parallel
```

when justified.

---

# 22. Migration Coordinator

The Coordinator executes the Migration Plan.

Owns:

- Plan execution
- Pre-checks
- Checkpoint coordination
- Provisioning coordination
- Transfer coordination
- Restore coordination
- Validation coordination
- Per-step timeouts
- Emergency deadline watchdog
- Failure handling
- Cleanup of losing branches
- Migration finalization

### Two timing mechanisms

**Step timeout**

```text
checkpoint timeout
provision timeout
restore timeout
...
```

**Deadline budget**

```text
interruption deadline
        ↓
remaining wall-clock budget
        ↓
preempt/abandon before plan becomes impossible
```

These are distinct.

---

# 23. Emergency Migration Concurrency

```text
                  ┌── Checkpoint ──────┐
Migration Plan ───┤                    ├── Transfer → Restore → Validate
                  └── Provision ──────┘
```

If one branch fails, the Coordinator cleans up the other branch where appropriate.

Example:

```text
Checkpoint fails
      ↓
terminate speculative target
      ↓
reconcile migration
```

---

# 24. ABANDON Semantics

`ABANDON` means live recovery cannot proceed within the supported strategy/deadline.

The resulting state must be explicit.

Example:

```text
Emergency recovery impossible
        ↓
ABANDON
        ↓
RECOVERY_REQUIRED / RESTART_REQUIRED
        ↓
Restart from last durable checkpoint
```

If no durable checkpoint exists:

```text
RECOVERY_REQUIRED
        ↓
No viable checkpoint
        ↓
FAILED / cold restart according to workload contract
```

The exact no-checkpoint policy belongs in Recovery Policy/workload semantics and must not be left implicit.

---

# 25. Validation

Validation is separate from migration execution.

### L1 — Infrastructure health

Target is alive/reachable.

### L2 — Application health

Workload/process resumes.

### L3 — Tolerance-bounded state equivalence

Restored state satisfies workload-specific numerical/invariant tolerances.

### L4 — Strict determinism

Exact equivalence only where the workload explicitly requires it.

For scientific/financial workloads, strict bitwise equivalence is not universally appropriate because parallel floating-point operations can legitimately produce small differences.

---

# 26. Migration History → Risk/Model Feedback

Actual outcomes feed future models:

```text
Migration History
├── actual migration duration
├── actual checkpoint size
├── actual transfer duration
├── actual provisioning success
├── actual interruptions
├── actual cost
├── actual savings
└── failures/errors
          ↓
      Risk / Cost Models
          ↓
   Better future estimates
```

Actual, estimated, and predicted values remain distinct.

---

# 27. Data Ownership

| Data | Authoritative owner |
|---|---|
| Job identity | Job Registry |
| Job lifecycle | Job Registry |
| Current execution association | Job Registry |
| Latest workload estimate | Job Registry |
| Workload observations | Workload/Telemetry layer |
| Runtime prediction | Workload Estimator |
| Market price observation | Market & Risk Monitor |
| Interruption events/history | Market & Risk Monitor / history |
| Risk estimate | Risk Model |
| Actual infrastructure state | Infrastructure Monitor/provider |
| Migration plan | Migration Planner |
| Active migration execution | Migration Coordinator |
| Checkpoint artifact | Checkpoint Manager / durable storage |
| Migration outcome history | Migration History |
| Cost analysis | Cost & Risk Evaluator |
| Recovery feasibility | Cost & Risk Evaluator |
| Final migration decision | Policy / Decision Engine |

---

# 28. Cross-Component Authority Rules

1. **Registry authority:** authoritative for logical job state and persisted ordering.
2. **Infrastructure authority:** authoritative for observed infrastructure facts, not logical job ownership.
3. **Estimation authority:** owns predictions it generates, not job state.
4. **Market authority:** owns normalized market/risk observations and provenance.
5. **Economics authority:** Cost & Risk Evaluator owns economic analysis, not policy.
6. **Policy authority:** only Policy/Decision components choose actions.
7. **Execution authority:** only execution components perform mutations.
8. **Validation authority:** Validation determines whether resulting state meets correctness criteria.
9. **Reconciliation authority:** Control/Reconciliation resolves cross-domain contradictions.

---

# 29. Emergency-Path Design Rules

Every emergency-path component must explicitly define:

1. Can it wait?
2. Maximum latency?
3. Does it bypass normal freshness?
4. Does it perform blocking I/O?
5. What happens when information is unavailable?
6. What conservative fallback exists?

Current decisions:

| Component | Emergency behavior |
|---|---|
| Market & Risk Monitor | Interruption events bypass normal freshness/coherence |
| Infrastructure Monitor | Dedicated low-latency observation lane |
| Workload Estimator | Conservative fallback when estimate unavailable |
| Cost & Risk Evaluator | Dedicated `evaluate_recovery()` with bounded latency |
| Migration Planner | Deadline-aware plan and possible parallel operations |
| Migration Coordinator | Deadline watchdog + step timeouts |
| Recovery Policy | Owns emergency action decision |

---

# 30. V2 Non-Goals

Explicitly deferred:

- Exactly-once execution
- Full coordinator HA
- Leader election / leases
- Multi-coordinator ownership
- Fleet-wide global optimization
- Multi-cloud execution
- Fully autonomous ML-based policy
- Broad arbitrary workload migration
- Speculative multi-target provisioning/racing beyond explicitly supported mechanisms
- Strict bitwise determinism for all workloads

These should not silently become V2 requirements.

---

# 31. Architectural Evolution Path

## Workload expansion

```text
Generic Workload Adapter
        │
        ├── Scientific
        ├── Financial
        ├── Monte Carlo
        ├── ML Training
        └── Future workloads
```

## Estimation

```text
Rule-based
    ↓
Historical
    ↓
Statistical
    ↓
ML / Hybrid
```

## Risk

```text
Historical interruption frequency
    ↓
Statistical model
    ↓
Predictive risk model
```

## Economics

```text
Point estimate
    ↓
Expected cost
    ↓
Risk-adjusted cost
    ↓
Probabilistic multi-objective optimization
```

Interfaces should remain stable as implementations evolve.

---

# 32. Architectural Principles to Preserve

1. **Observation ≠ Execution**
2. **Estimation ≠ Truth**
3. **Economics ≠ Policy**
4. **Policy ≠ Execution**
5. **Execution ≠ Validation**
6. **Missing ≠ Zero**
7. **Emergency ≠ Normal**
8. **Reconciliation has an owner**
9. **Historical ≠ Predicted**
10. **Current state ≠ History**

---

# 33. Current Architectural Boundary

```text
                    ┌───────────────────────────────┐
                    │          OBSERVATION           │
                    │                               │
                    │ Job Registry                  │
                    │ Market & Risk Monitor         │
                    │ Infrastructure Monitor        │
                    │ Workload Adapters             │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │          ESTIMATION            │
                    │                               │
                    │ Workload Estimator             │
                    │ Risk Model                     │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │           ECONOMICS            │
                    │                               │
                    │ Cost & Risk Evaluator          │
                    │ CostAnalysis                    │
                    │ RecoveryFeasibility             │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │            POLICY              │
                    │                               │
                    │ Arbitrage Policy               │
                    │ Recovery Policy                │
                    │ Decision Engine                 │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │           EXECUTION            │
                    │                               │
                    │ Migration Planner              │
                    │ Migration Coordinator          │
                    │ Checkpoint Manager             │
                    │ Provisioner                    │
                    │ Transfer / Restore             │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │           VALIDATION           │
                    │                               │
                    │ Infrastructure                 │
                    │ Application                    │
                    │ State equivalence              │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │        HISTORY / FEEDBACK      │
                    │                               │
                    │ Migration History              │
                    │ Telemetry                      │
                    │ Model improvement              │
                    └───────────────────────────────┘
```

---

# 34. Review Status

**Components 1–5:** architecturally reviewed and frozen subject to implementation-level interface refinement.

### Next component specifications

1. Arbitrage Policy / Decision Engine
2. Recovery Policy
3. Migration Planner
4. Migration Coordinator
5. Checkpoint Manager
6. Provisioner
7. Transfer / Restore
8. Validation Manager
9. Control / Reconciliation
10. Migration History
11. Observability / Telemetry
