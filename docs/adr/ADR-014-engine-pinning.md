# ADR-014 — Engine version pinning

Status: accepted. Date: 2026-09-07.

Context: plan/contract/validation semantics are version-sensitive.
Decision: Job.engine_version ∈ {V1,V2} set at admission, immutable.
No mid-job upgrade via deploy; new engine needs new admission (or explicit
future re-admission protocol).
Consequences: admission + registry enforce immutability; coordinator rejects
version-mismatched plans.
