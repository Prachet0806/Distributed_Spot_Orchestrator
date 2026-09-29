# Spot Market Arbitrage Cluster

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python\&logoColor=white)
![Terraform](https://img.shields.io/badge/terraform-1.5%2B-purple?logo=terraform\&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-EC2%20Spot%20|%20S3%20|%20DynamoDB-orange?logo=amazon-aws\&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

**Cost-optimize long-running batch workloads by live-migrating running processes between AWS regions.**

Submit a job with one command and the V2 control plane takes ownership: provisioning, spot-price placement, CRIU live migration, interruption recovery, and savings reporting. Running processes are treated as portable state — frozen with CRIU and resumed where compute is cheapest.

> **What V2 does not promise:** exactly-once execution (future goal, not a guarantee), full coordinator HA / leader election (deferred — single-writer lease + CAS instead), multi-cloud, fleet-wide optimization, or ML-driven policy. See [Non-goals](#non-goals).

---

## How V2 thinks

One separation rule governs the whole design:

> **Observation ≠ Estimation ≠ Economics ≠ Policy ≠ Execution ≠ Validation**

| Concern | Question it answers | Components |
|---|---|---|
| Observation | What is happening? | Spot watcher, interruption ingestion, worker telemetry, infrastructure monitor |
| Estimation | What is likely next? | Workload estimator (progress, remaining runtime, checkpoint size/duration + confidence) |
| Economics | What does each option cost? | Cost/risk evaluator, recovery-feasibility engine (critical-path vs deadline) |
| Policy | What should we do? | Arbitrage / proactive / recovery policies with precedence `EMERGENCY > PROACTIVE > ARBITRAGE` |
| Execution | Do it exactly once | Migration planner (immutable hash-pinned plan) → coordinator (sole command issuer) |
| Validation | Did it actually work? | L1 infra → L2 app → L3 tolerance → L4 strict validator; inconclusive never promotes |

Two regimes share this pipeline but obey different clocks: **arbitrage** can wait for better information (hysteresis, cooldowns, short-job rules); **emergency recovery** must fit inside a provider interruption deadline, so it uses a dedicated fast path (parallel checkpoint ∥ provision, forward-only after fencing, cold-restart fallback).

Per-job tick in `orchestrator/main.py`:

```text
poll prices → estimate workload → derive requirements from the job's
WorkloadContract → load pool definitions from the registry →
compatibility → readiness (telemetry-graded) → placement rank →
economics → policy (or per-candidate feasibility in EMERGENCY) →
hash-pinned plan → coordinator executes → validator gates →
history + audit recorded → reconciliation sweeps
```

Admitted (`PENDING`) jobs are owned the same way: the loop provisions an initial worker from the placement pick and promotes `PENDING → RUNNING` on a fresh worker heartbeat — PID and host are discovered, never user-supplied.

---

## Safety invariants (enforced, tested)

| Invariant | Meaning |
|---|---|
| Precedence | Emergency decisions override arbitrage; economics never overrides safety |
| Audit-before-irreversible | Fence commands require a durable audit write first (fail-closed) |
| Fence-never-inferred | Confirmed fencing needs epoch invalidation **and** verified source termination; otherwise `RECONCILIATION_REQUIRED` |
| UNKNOWN-never-blind | Timeouts resolve via status probes within budget, then escalate — never blind-retry |
| Single writer | Control-plane lease (15 s heartbeat / 45 s TTL) + per-iteration self-fencing; every mutation is CAS-guarded |
| UNKNOWN ≠ eligible | Stale/missing telemetry or durability evidence excludes candidates and blocks recovery — never assumed healthy |

---

## Persistence map (what survives a crash)

| Store | Table | Holds |
|---|---|---|
| Job Registry | `spot_arbitrage_registry` | Job lifecycle, `execution_epoch`, active migration ref (CAS sole mutation path) |
| Pool Registry | `spot_arbitrage_candidate_pools` | Versioned pool definitions (AMI, SG, runtime digest, lifecycle) |
| Plan Store | `spot_arbitrage_plans` | Immutable plans + CAS step states (crash reconstruction) |
| Event Ledger | `spot_arbitrage_event_ledger` | `(event_id, consumer_id)` dedup; FAILED re-arms one retry per redispatch |
| Checkpoint Metadata | `spot_arbitrage_checkpoints` | Durability (`LOCAL→PERSISTING→DURABLE`), lineage, locks, GC floor |
| Audit Store | `spot_arbitrage_audit` | Append-only mandatory events |
| Migration History | `spot_arbitrage_migration_history` | Estimated-vs-actual per migration (feeds estimator + risk feedback) |
| Configuration | S3-versioned `config/{domain}/v{N}.yaml` | Pinned policy/placement/baseline versions referenced by plans |

On restart the orchestrator acquires the lease, replays the §14.3 bootstrap report (in-flight → UNKNOWN resolution → budget → frontier → sweep → resume), and never actuates without a fresh lease. You never re-register or repair jobs by hand after a crash.

---

## Quickstart (operator path)

### Prerequisites

* Python 3.10+, Terraform 1.5+, configured AWS CLI
* SSH key pair uploaded to all target regions
* Ubuntu 22.04 worker AMI with CRIU (`sudo criu check` passes), Python, and `criu_wrapper.sh`; copy it to every candidate region (AMI baking itself stays out-of-band for v1)

### 1. Infrastructure (Terraform — never hand-create tables)

```bash
cd infra/aws
terraform init
terraform apply
```

This creates the DynamoDB tables, KMS key, and hardened checkpoint bucket. Confirm tables + GSIs ACTIVE before proceeding.

### 2. Configure pools and runtime

```bash
# Register one runtime artifact across regions (records AMI/digest per pool)
python scripts/spotctl.py runtime publish --digest sha256:<yours> \
  --regions us-east-1,us-west-2 --instance-type t3.micro \
  --ami us-east-1=ami-111 --ami us-west-2=ami-222 \
  --pools-file config/pools.json
```

Fill `config/runtime.yaml` (bucket, key name, security groups, candidate regions). Pool rows in `config/pools.json` are seeded into the registry at orchestrator startup; the registry is authoritative thereafter.

### 3. Submit a job (no PID, IP, AMI, or Dynamo edits)

```bash
python scripts/spotctl.py run --name risk-sim --script ./monte_carlo.py \
  --cpu 4 --memory 16GiB --regions us-east-1,us-west-2
python scripts/spotctl.py status --name risk-sim
```

The job is admitted `PENDING` with an immutable workload contract. The loop provisions its first worker and promotes it on heartbeat.

### 4. Run the orchestrator

```bash
export CHECKPOINT_BUCKET=<your_bucket>
python -m orchestrator.main --multi-job \
  --regions us-east-1,us-west-2 --instance-type t3.micro --migrate
```

Key flags: `--engine v2` (default; `v1` is frozen legacy per ADR-024), `--states PENDING,RUNNING`, `--max-migrations-per-hour`, `--max-concurrent-migrations`, `--check-spot-flag` (worker interruption lane), `--reconcile-interval`, `--health-port`. Health: `GET /health`, Prometheus metrics at `/metrics` (set `HEALTH_AUTH_TOKEN` to require `?token=`).

### 5. Observe economics

```bash
python scripts/spotctl.py history --name risk-sim   # what happened per migration
python scripts/spotctl.py cost --name risk-sim      # did it save money
```

---

## Verify & troubleshoot

No-AWS emulated gate (correctness harness):

```bash
python -m pytest tests/test_e2e_emulated.py tests/test_overhead.py \
  tests/test_interruption_ingestion.py tests/test_validator_probes.py \
  tests/test_replan_wiring.py tests/test_lease_bootstrap.py \
  tests/test_step_retry.py -v
```

Full suite: `make test` (or `python -m pytest tests/ -v`); coverage: `make test-coverage`. Overhead gate: median-of-3 wall-clock ratio ≤ 20% (`tests/test_overhead.py`, ADR-022).

| Symptom | Fix |
|---|---|
| CRIU fails | Kernel check + `sudo criu check` on the AMI |
| SSH timeout | Security-group ingress for the orchestrator IP |
| S3 AccessDenied | Worker IAM role + `kms_key_arn` scoping |
| `RECONCILIATION_REQUIRED` | Read the finding first — ownership is uncertain; do not force transitions |
| Job stuck `PENDING` | `status` shows `admission_status`: `NO_ELIGIBLE_POOL` (contract vs pools), `provision-failed` (quota/config), or awaiting heartbeat |
| Migration loops | Raise cooldown / savings margin in `config/v2_baseline.yaml` |
| `EMU_TRUST_MODE` set | Unset for any AWS path — relaxed trust is emulation-only (CI asserts this) |

The AWS proof matrix (crash-mid-migration, duplicate provision, checksum corruption, runtime drift, live interruption, post-fence restart, …) is defined in `docs/test-catalogue.md` (S-scenarios) and runs against real infra per `docs/e2e-v2.md`.

---

## Contributor path

Repo map: `orchestrator/` (control plane) · `worker/` (job runner, controller, transports) · `storage/` (8 stores + transition tables) · `config/` (runtime, placement, baseline, logging) · `infra/aws/` (Terraform) · `scripts/` (`spotctl.py` user CLI, `deploy_worker.py`, `worker_ctl.py`, `registry_cli.py` legacy) · `tests/` (333-test S/P/C suite).

| Doc | What it is |
|---|---|
| `docs/overall_architecture_and_components_map.md` | System purpose, 16-component map, authority rules |
| `docs/component-architecture-v2.md` | Frozen layers, catalogue, dependency graph, state ownership, persistence |
| `docs/Protocols.md` | Normative protocol freeze (components must not redefine it) |
| `docs/e2e-v2.md` | Emulated + AWS end-to-end runbook |
| `docs/test-catalogue.md` | Chaos (S) / property (P) / contract (C) test vehicles |
| `docs/IMPLEMENTATION_STATUS.md` | Docs-claim ↔ code-truth ledger (update it with every landing) |
| `docs/TRACKS.md` | Prod tracks A–E execution plan |
| `docs/telemetry-contract.md` | T1–T4 worker telemetry schemas |
| `docs/adr/` | 20 ADRs (policy, placement, trust, HA, V1 deletion…) |
| `spot-arbitrage-architecture.json` (+ generated `.html` view) | Explorable runtime-architecture diagram spec |

Standing rules: no irreversible action without its gate green; every orchestration/persistence PR cites its S/P/C rows; the status ledger moves with the code, not on dates.

---

## Roadmap

* **Tracks A–B:** live signal + durability — first real migration per region pair; Dynamo live-verify; crash matrix (RTO ≤ 2 min, RPO 0)
* **Tracks C–D:** safety evidence + hardening — telemetry path, live drills (S07/S13/S17/S24), least-privilege roles, worker-controller trust cutover
* **Track E:** operate + graduate — dashboards, playbooks, billing validation, 2-week soak, V1 deletion per ADR-024 (emulated green → AWS green → soak → delete; `registry_cli` JSON compat retained until records migrate)
* **Deferred by decision:** warm-standby pools (opt-in only), REST API, predictive pricing/ML, multi-cloud

V1 (`Migrator`/`DecisionEngine`) is frozen behind `--engine v1` and covered by immutability pins in `tests/test_models_v2.py`. New work targets V2.
