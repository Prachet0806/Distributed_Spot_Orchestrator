# ADR-022 — Overhead acceptance method

Status: accepted. Date: 2026-09-07.

Context: ≤20% must be measured, not simulated.
Decision: overhead = (migrated − baseline)/baseline wall-clock; P95 ≤20%
across SHORT/MEDIUM/LONG/STATEFUL × CPU/IO × ckpt-size × same/cross-region
fixture set, recording checkpoint/upload/provision/download/restore/downtime/
bytes/CPU/migration-count. Monte Carlo supplements economics only.
Consequences: build overhead harness + fixture set; gate E2E on P95.
