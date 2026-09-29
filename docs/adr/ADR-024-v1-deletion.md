# ADR-024 — V1 deletion gates

Status: accepted. Date: 2026-09-07.

Context: early deletion strands persisted registry records and operators.
Decision: 3 gates — emulated-green → V1 deprecated; AWS-green → V2 default;
soak → delete migration code. registry_cli JSON compat retained through
code deletion, removed only after all records migrated (Phase D).
Consequences: Phase 0 freeze (this change) → later phased removal, never a
flag-day with state on disk.
