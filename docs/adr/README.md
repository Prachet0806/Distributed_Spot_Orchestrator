# ADRs 006–024 — V2 behavioral freeze

| ADR | Decision |
| --- | --- |
| 006 | Hysteresis: margin 10%, cooldown 900s, 3 favorable obs, 20% hard overhead ceiling; emergency bypasses hysteresis |
| 007 | Short-job economics: net_benefit formula; SHORT<15m no arbitrage; MEDIUM>=max(2x cost, 20% savings); STATEFUL needs DURABLE |
| 008 | Cold restart default false; no-checkpoint → RECOVERY_REQUIRED (RESTART_REQUIRED only if enabled); FAILED only after exhaustion |
| 009 | Placement v3 weights + tie-break ending in pool_id lexicographic; placement ranks, never computes economics |
| 010 | Risk = historical interruption + provision-failure + restore-failure rates; LOW confidence + conservative default when thin |
| 011 | TTLs: arbitrage/proactive 600s, emergency = remaining deadline; op 300s, step 60s, UNKNOWN 30s; step_deadline=min(expiry,deadline,timeout) |
| 012 | Retry: op 3x exp(2s→30s, ±25% jitter); max 2 replans, 3 recoveries; same op_id = retry; UNKNOWN → query → reconcile |
| 013 | Retention: latest 3 DURABLE + latest VALIDATED; max age 24h; GC grace 6h; never delete referenced; multipart via transfer cleanup |
| 014 | engine_version V1\|V2 pinned at admission, immutable; no mid-job upgrade |
| 015 | Audit gating: ownership/admission/plan-activation/durability/fencing/termination/terminal/recovery sync; telemetry async |
| 016 | Hub-and-spoke home-region S3 + SSE-KMS (+MRK); 64MiB parts; manifest.json + manifest.sha256 |
| 017 | Concurrency: global 3, per-pool 2, per-job 1; READY→all, DEGRADED→emergency-only, NOT_READY/UNKNOWN→none |
| 018 | Worker: Ubuntu 22.04 x86_64, pinned CRIU/kernel, baked AMI (no boot apt install); AMI owns wrapper/controller/deps |
| 019 | Emulated trust (AutoAddPolicy/sudo/flag-file) only behind EMU_TRUST_MODE=true; separate Emulated/AWS transports |
| 020 | CPR ASG min=max=1 + DynamoDB lease; test crash/replacement/lease-loss/duplicate/mid-fence; home-outage = accepted limitation |
| 021 | Moto for API units; DynamoDB-Local for CAS/Ledger; real Linux kernel/VM for CRIU/worker; moto is not full integration |
| 022 | Overhead = (migrated-baseline)/baseline; P95 ≤20% over SHORT/MEDIUM/LONG/STATEFUL × CPU/IO × ckpt-size × same/cross matrix |
| 023 | Prometheus /metrics canonical; CloudWatch for infra/AWS-native only; no CloudWatch-agent gate on correctness |
| 024 | V1 3-gate deletion: emulated-green→deprecated; AWS-green→V2-default; soak→delete code; JSON CLI compat last |

Each file `ADR-0xx-*.md` holds context + consequences. Config values live in `config/v2_baseline.yaml`.
