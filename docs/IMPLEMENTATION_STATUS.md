# Implementation Status Ledger — docs-claim ↔ code-truth

Single source for "what's real". Updated when code lands, not on dates.
Legend: **built** (implemented + tested) · **partial** (mechanism exists,
wiring/proof missing) · **scheduled** (specified, unbuilt, track owned) ·
**deferred** (explicit non-goal).
Invariant labels follow component-architecture §18 (IA-1, I1–I15); note
Protocols I14 ("validation ≠ ownership") is a different invariant from
§18 I14 ("audit-before-irreversible") used below.

## Credential-free qualification snapshot (2026-10-18)

The production iteration runs against injected Moto services. Separate Python
processes recover plans and operations through a Moto HTTP server. Terraform
validates locally (with deprecation warnings for DynamoDB `hash_key`). The
isolated Linux lab on WSL2 kernel `6.6.87.2-microsoft-standard-WSL2` and
CRIU `3.16.1` completed dump, durable artifact publication, integrity check,
and target staging; CRIU restore then terminated with signal 11. The failed
pre-fence run resumed the original source process and returned its registry
state to `RUNNING`. Fencing, activation, and resumed target progress remain
unproven. The wall-clock
fixture overhead gate has alternated between passing and exceeding its 20%
threshold on this host. The latest Linux suite passed 398 tests and failed
the 20% overhead gate (37.2% median); an isolated repeat also failed (21.6%
median). The local service runner, shell syntax and Terraform validation
passed. Terraform still warns about deprecated DynamoDB `hash_key` fields.
`lab-results/` contains the latest local evidence. No live AWS migration,
provider IAM, KMS, or economics result has been verified.

## Contracts & policies (Protocols)

| Claim | Status | Evidence / gap |
|---|---|---|
| 4-domain states, CAS sole path, version≠epoch | built | `storage/dynamo_registry.py` `transition()`, `storage/v2_transitions.py`, tests |
| ULID `operation_id`, sole minter, reuse-on-retry | built | coordinator + provisioner replay test |
| UNKNOWN → resolve, never blind retry | partial | timeout→UNKNOWN + probe resolution at every executor done (emulated, `tests/test_outcome_resolution.py`); live provider-status queries unwired |
| Plan hash/TTL/pins/authorization | built | planner + kernel tests |
| Step retry/backoff/priority (§10.8) | built | `orchestrator/step_retry.py` + coordinator wiring (op-ID reuse, UNKNOWN≠attempt, backoff consumes deadline, injection rejection, priority/criticality order); `tests/test_step_retry.py` (19 tests) |
| Emergency deadline §24.1 (allowances, owner) | partial | formula + ingestion owner built (emulated): `orchestrator/interruption_ingestion.py` computes once from flag `detected_at`, `main.py` reuses it; remaining gap is the measured P95 allowance table (Track A6) |
| Hysteresis / short-job / overhead ceiling | built | policy + tests |
| Recovery vocabulary + recon gate | built | `decide_recovery()` + tests |
| Validation L1–L4 + verdicts + epoch/lineage | built | validator + tests; L1/L2 **transport-backed probes** (`orchestrator/validator_probes.py`, `ps -p` + T1 freshness) wired in `main.py` via coordinator context sink; evidence-absence → INCONCLUSIVE (I7/I15); instance-id→IP resolution still live backlog |
| Reconciliation taxonomy/lifecycle/gate | built | manager + tests; live drills scheduled |
| Checkpoint durability/lineage/locks/GC | built | stores + S3 sha/manifest + tests |
| Fencing: pre-auth + epoch rotation + verified kill | built | coordinator + kernel tests; prod SSH wiring exists, controller trust scheduled (W1–W5) |
| Audit-before-irreversible (§18 I14) | built | coordinator writes FENCE_STARTED pre-hooks (fail-closed), FENCE_CONFIRMED/COMPLETED/FAILED post-facto; `tests/test_audit_gate.py`; stores wired in `main.py` (Track B1) |
| Telemetry contract (T1–T4) | partial | T1 emission (`job_runner` heartbeat/v1) + CPR intake/freshness library (`orchestrator/worker_telemetry.py`) + tests; T4 refusal emission (controller outbox) + filing (`file_refusal`) built emulated; live transport drill scheduled |
| Worker-controller trust §12.6 (W1–W5) | partial | emulated controller (`worker/controller.py`: IMDS-env binding, epoch gate, op-ID dedup, T4 outbox, primitive-only) + CPR client (`orchestrator/worker_controller.py`: gated fence hooks, refusal filing), 21 tests; live cutover (AMI/IMDS/channel + `main.py` switch) scheduled |
| Admission gate §3.3 | built | `orchestrator/admission.py` (contract checklist enforced, frozen `WorkloadContract`, idempotent resubmit); PENDING lifecycle + loop-owned provision/promote; `tests/test_spotctl_admission.py` |
| Dispatcher/thread-pool/scaling target §13 | scheduled | interim single loop documented (§13.0) |
| CPR lease + bootstrap §14.3 + fencing matrix §14.4 | partial | Durable lease and bootstrap report exist. `main.py` quarantines reported/unknown/orphan frontiers as `RECONCILIATION_REQUIRED`; restore has a root-owned issue journal/status receipt. Automatic crash-phase replay remains a gate. |
| Sole V2 engine §16 | built | V1 modules, routing, JSON CLI/registry, state table, executor aliases, and old-record adapters removed by owner request; strict V2 emulation retained |

## Runtime wiring (`main.py`)

| Claim | Status |
|---|---|
| Decision→plan→execute loop, hysteresis streaks, baseline policy | built |
| Dynamo-backed plans/checkpoints/audit/history/config/pools/operations/reservations | partial | Stores and tables exist; local Moto persistence/restart passes. Live IAM and apply are unverified. |
| Real validator probes | partial | L1 (`ps -p`) + L2 (T1 freshness) transport-backed probes (`orchestrator/validator_probes.py`) wired in `main.py` via coordinator context sink, emulated-tested (`tests/test_validator_probes.py`, 12 tests); live backlog is instance-id→IP resolution (Track A5/C2); unprobed `Validator()` now returns INCONCLUSIVE; test health is explicitly injected |
| Per-pool concurrency + DEGRADED lifecycle | built | `pool_lifecycle.py` (transitions, regime gate, thread-safe tracker) + placement exclusions + coordinator acquire/release; `tests/test_pool_lifecycle.py` (9 tests) |
| Plan-creation dedup | partial | in-process migration_id-keyed dedup + `create_successor_plan` (new plan_id, same attempt, fresh estimate required); durable cross-restart dedup deferred |
| Replan/supersession wiring (§25) | partial | `orchestrator/replan.py` (signal/budget/preempt) + coordinator `replan_requested` (plan-expired/deadline-drift, pre-fence only) + bounded main-loop successor path; expired no-op plans restore job RUNNING + signal (FAILED is terminal per `v2_transitions.py`, so it would strand the job); S16 main-loop trigger needs plan persistence (Sprint 4) |
| SSH trust (pinned known-hosts, strict mode, D5 emu-trust guard) | built | `scripts/deploy_worker.py` (`--pin`/`--known-hosts`, RejectPolicy default, EMU_TRUST_MODE abort); `SSHClient(strict=...)`; `tests/test_deploy_ssh_trust.py`; note: ADR-019's `AWSWorkerTransport` is code's `SSHWorkerTransport` (naming drift, same interface split) |

## Infra & ops

| Claim | Status |
|---|---|
| TF: tables, KMS, bucket, vars/outputs | built (code; **never applied**) | `terraform validate` passes locally with DynamoDB schema deprecation warnings. |
| Baked AMI + digest pinning | partial | Immutable root-owned worker tree installer, self-check and script allowlist built; no AMI baked or live digest attested. Some emulation fixtures retain placeholder digests. |
| IAM policies (§11) | partial | Store ARNs, checkpoint KMS access, worker lease read added; live policy simulation and apply are pending. |
| Dashboards/alerts/playbooks | scheduled |
| Billing proof, PITR/KMS drills, soak | scheduled |

## Workloads

| Claim | Status |
|---|---|
| Fixture + stateful Monte Carlo, hooks, READY/heartbeat files | built + tested |
| Real CRIU restore proof on workers | partial | Local WSL2 dump and transfer pass; transport now preserves CRIU file modes and the wrapper restores job-file ownership. Restore still crashes in CRIU with signal 11 after process creation. Native Linux and AWS worker qualification pending. |
| Workload privilege and network boundary | partial | Production launcher pins approved script bytes, drops to fixed UID/GID 62000, sanitizes environment; boot helper installs IPv4/IPv6 owner egress deny and root IMDS monitor. Unit/syntax checks pass; baked-image execution and quotas remain open. |

## Phase 0/A baseline landing (locked scope, no new deployable components)

| Claim | Status | Evidence / gap |
|---|---|---|
| Structured V2 history | built | `migration_history.py` accepts structured results and rejects stringified/unsupported result schemas; `tests/test_main_wiring.py` |
| Checkpoint durability lookup | built | `main.query_checkpoint_durable()` + `CheckpointStore.latest_durable()`; `checkpoint_durable=True` hardcode removed; never inferred |
| Per-candidate emergency feasibility | built | `_feas_by_pool`/`_survivors` in `main.py`; all-infeasible → ABANDON; assessment-pool_id → region remap (KeyError fix) |
| Event fidelity (`detection_lag`) | built | `ingest_interruption` stamps `noticed_at` + `detection_lag_seconds`; main logs lag on EMERGENCY |
| `scripts/spotctl.py` shim (run/status/history/cost) + `runtime publish` | built | single entry; normal path takes no PID/IP/AMI; `config/pools.json` seed at startup; `tests/test_spotctl_admission.py` (18 tests) |
| WorkloadContract + admission (PENDING, no PID/IP) | built | `orchestrator/admission.py` (frozen contract, idempotent resubmit); loop owns PENDING ticks (provision → telemetry promotion), `--states` default `PENDING,RUNNING` |
| PENDING lifecycle (v2 table + staging carve-out) | built | `PENDING: {RUNNING, FAILED}` additive in `v2_transitions.py` (v1 frozen untouched); PENDING→PENDING attr staging in both registries (CAS version still bumps) |
| Admission execution (provision-then-promote) | built | `_handle_pending_job`: placement-picked initial pool → `provision_from_pool` (rate-limited, 300s retry throttle) → FRESH-heartbeat+pid promotion via `registry.transition`; failures skip, never raise; `tests/test_main_wiring.py` (6 tests) |
| Candidate-authoritative execution provisioning | built | coordinator `pool_definition_provider` seam (`_pool_definition_for` in `main.py`); `_execute_provisioning` requires a resolved definition when a pool provider is configured; missing definitions fail |
| PoolDefinition validation + candidate-authoritative provision | built | `validate_pool_definition`/`definition_from_pool`; `Provisioner.provision_from_pool` (strict AWS definition; emulation is explicit) |
| Telemetry-driven readiness/confidence | built (loop) | contract requirements + registry pools + telemetry confidence/artifact wired; PENDING→RUNNING promotion on FRESH heartbeat+pid; full worker reporting (PA-9 live drill) scheduled |
| Contract drift propagation (Phase B close-out) | built | `_requirements_for_job` carries runtime digest, allowed regions, secret refs; digest mismatch fails deterministically at compatibility |
| P95 timing model | built (same-job priors) | `p95_step_estimates` + `measured_overrides` in step builder; cross-job durable aggregation deferred to HistoryStore Dynamo scan |
| Warm rescue | deferred | negative test pins absence; opt-in flag lands with standby implementation, off by default |
| `test_overhead.py` gate | built | single-sample wall-clock was noise-dominated (0.11–0.43 same code); gates median-of-3, threshold unchanged at 0.20 (ADR-022) |
