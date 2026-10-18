# ADR-014 — Sole V2 engine

Status: accepted; revised 2026-10-14 alongside ADR-024.

Decision: V2 is the only executable engine. No admission router, runtime
engine flag, legacy adapter, or cross-engine handoff is retained. Unsupported
engine identities and record shapes are rejected. Recovery follows V2 semantics.

Telemetry/configuration schema versions are independent of execution engines.
