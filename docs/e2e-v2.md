# V2 End-to-End Runbook

## Emulated E2E (no AWS)

```bash
python -m pytest tests/test_e2e_emulated.py tests/test_overhead.py \
  tests/test_interruption_ingestion.py tests/test_validator_probes.py \
  tests/test_replan_wiring.py tests/test_lease_bootstrap.py \
  tests/test_step_retry.py -v
```

Covers: real estimator → compatibility/readiness → economics → policy
(hysteresis/short-job) → hash-pinned plan → coordinator execution with
fencing hooks → epoch transfer → ledger/history audit → clean
reconciliation sweep. Overhead gate: P95-style measured ratio ≤ 20%
(`tests/test_overhead.py`). Plus: interruption dedup + deadline-once
(S21, §24.1), transport-backed L1/L2 probes (C2, S19), replan/
supersession wiring (S04/S15/S16), lease + §14.3 bootstrap report,
§10.8 retry/backoff/priority.

## AWS E2E prerequisites

1. `terraform -C infra/aws apply` (state tables, KMS key, hardened bucket).
2. Bake + copy the worker AMI (Ubuntu 22.04, pinned CRIU/kernel,
   `criu_wrapper.sh`, deps) to every candidate region; fill
   authoritative pool definitions in `config/pools.json` (AMI, security group, runtime digest, and region).
3. Pass the KMS ARN back as `kms_key_arn` to scope the worker policy.
4. Create DynamoDB table `spot_arbitrage_registry` — now managed by
   `infra/aws/dynamodb.tf` (do not hand-create).
5. Register a job, then run the orchestrator with `--migrate`:

```bash
python -m orchestrator.main --multi-job \
  --regions us-east-1,us-west-2 --instance-type t3.micro --migrate
```

## Trust boundary (ADR-019)

`EMU_TRUST_MODE=true` enables relaxed SSH/file trust for emulation only.
The AWS path always verifies host keys and worker identity; CI asserts
`emu_trust_mode()` defaults to false (`tests/test_worker.py`).

## Engine boundary (ADR-024)

V2 is the sole engine. V1 execution, JSON registry compatibility, legacy CLI,
old state transitions, and fallback executor APIs have been removed. Production
uses DynamoDB; the no-AWS gate uses explicit V2 in-memory test collaborators.
This breaking change does not delete or upgrade existing AWS records.
