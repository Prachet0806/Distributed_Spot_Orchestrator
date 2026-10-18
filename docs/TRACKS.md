# Prod Tracks — execution plan (multi-region pilot, retryable batch)

Source: prod-readiness audit 2026-09. Status ledger: `IMPLEMENTATION_STATUS.md`.
Test vehicles: `test-catalogue.md`. Runbook: `e2e-v2.md`.

## Track A — Live signal (wk 1–2). Owner: infra + control plane.

Goal: first real migration per region pair; measured latency table.

- [ ] A1 AWS pilot account/OUs (dev + prod separated); CloudTrail, Config, GuardDuty on. *Gate: audit trail flowing.*
- [ ] A2 Install Terraform; `terraform -C infra/aws validate/plan/apply` in dev. *Gate: apply clean (never run before).*
- [ ] A3 Confirm DynamoDB tables + GSIs ACTIVE; `list_by_state` against real Dynamo.
- [ ] A4 Bake worker AMI v1 per candidate region (Ubuntu 22.04, pinned CRIU/kernel, wrapper, deps); record IDs + digests. *Gate: `preflight` passes everywhere.*
- [ ] A5 Publish authoritative pool definitions (AMI/SG IDs) and fill `config/runtime.yaml` (registry, bucket, SSH key); pass KMS ARN as `CHECKPOINT_KMS_KEY_ARN`; register fixture job; manual migration per region pair. *Gate: SUCCESS + epoch+1 in Dynamo.*
- [ ] A6 Replace `wan_allowance_seconds` estimates with measured P95 per-pair latencies. *Gate: deadline math uses measurements.*
- [ ] A7 Cross-region KMS decrypt drill (candidate-region worker role decrypts home-key ciphertext).

## Track B — Durability (wk 2–4, overlaps A). Owner: control plane.

- [~] B1 Wire Dynamo-backed Plan/Checkpoint/Audit/History/Config/Pool/Operation/Reservation stores into `main.py`. *(partial: code and Moto restart tests pass; live IAM/table verification pending)*
- [~] B2 Plan-step Dynamo CAS; `control_plane_lease` record + 15 s heartbeat / 45 s TTL self-fencing. *(partial: lease + per-iteration loop guard wired; plan-step CAS done; Dynamo record live-verifies on apply)*
- [~] B3 §14.3 bootstrap algorithm (lease → in-flight → UNKNOWN resolution → budget → frontier → sweep → resume). *(partial: report wired; unresolved frontiers are quarantined as `RECONCILIATION_REQUIRED` until phase replay is qualified)*
- [ ] B4 Crash matrix on real infra (every state incl. mid-FENCING → §14.4 outcomes). *Gate: RTO ≤2 min, RPO 0, zero workload kills.*

## Track C — Safety evidence (wk 3–5). Owner: control plane + worker.

- [~] C1 Worker→CPR telemetry path (T1–T4 contract). *(heartbeat and SSH intake wired; live transport/identity drill pending)*
- [x] C2 Real L1/L2 validator probes; retire `stub-v1` in prod wiring. *(done emulated: `orchestrator/validator_probes.py` wired in `main.py`, 11 tests; live backlog is instance-id→IP resolution)*
- [x] C3 Per-pool concurrency double-guard + pool lifecycle (DEGRADED/RETIRING/RETIRED), emulated-tested; live contention drill pending.
- [~] C4 Interruption, fencing-uncertainty, split-brain drills (S07/S13/S17/S21/S24). *(partial: S21 done emulated via stable `event_id` dedup; S07/S13/S17/S24 still need live drills)*
- [~] C5 Replan/supersession paths (S04/S15/S16) + plan-creation dedup + §10.8 retry wiring. *(partial: S04/S15 done emulated via `orchestrator/replan.py` + bounded main-loop path; S16 mechanism done, main-loop trigger needs plan persistence; §10.8 retry wiring done)*

## Track D — Security hardening (wk 3–5). Owner: platform security.

- [ ] D1 Least-privilege roles (§11): scope worker IAM, executor task roles, tag-conditioned EC2, read-only reconciliation, break-glass.
- [x] D2 SSH trust: strict known-hosts in `deploy_worker.py` and explicit first-connect pinning; live fingerprint verification pending.
- [~] D3 Worker-controller trust W1–W5 (§12.6.1). *(root-owned control tree, digest checks, fixed workload UID, egress rule, controller/CPR wiring and emulated tests done; baked AMI/IMDS/channel drill pending)*
- [ ] D4 Checkpoint secret policy + verification.
- [ ] D5 Verify `EMU_TRUST_MODE` unset in AWS; rotate exposed material.

## Track E — Operate + graduate (wk 5–8). Owner: SRE + control plane.

- [ ] E1 Prometheus scraping, dashboards, alerts; audit pipeline verification.
- [ ] E2 Playbooks (RECONCILIATION_REQUIRED, epoch repair, KMS/Dynamo outage, home-region halt); Dynamo PITR + KMS rotation drills.
- [ ] E3 Billing validation per workload class.
- [ ] E4 2-week multi-region soak (P95 overhead ≤20%, savings positive, zero split-brains).
- [ ] E5 History→estimator/risk feedback wiring.
- [x] E6 V1 code and compatibility removed by explicit owner request; ADR-024 revised. Live AWS and soak qualification remain required.

## Standing rules

- No step executes an irreversible action without its gate green.
- Every orchestration/persistence PR cites S/P/C rows from `test-catalogue.md`.
- `IMPLEMENTATION_STATUS.md` updated with each landing.
