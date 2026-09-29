# ADR-021 — Emulated backend split

Status: accepted. Date: 2026-09-07.

Context: moto alone cannot prove CAS/ledger/CRIU semantics.
Decision: moto for AWS API units; DynamoDB-Local for registry CAS + ledger;
moto/local-S3 abstraction for bytes; real Linux kernel/VM for CRIU + worker
controller; local processes/VMs for full emulation.
Consequences: test harness wires per-layer backends; CRIU tests never mocked.
