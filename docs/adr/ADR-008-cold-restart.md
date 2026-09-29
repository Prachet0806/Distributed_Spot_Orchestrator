# ADR-008 — Cold restart default

Status: accepted. Date: 2026-09-07.

Context: interruption with no DURABLE checkpoint needs explicit semantics.
Decision: allow_cold_restart=false. No checkpoint + allowed → RESTART_REQUIRED.
No checkpoint + forbidden → RECOVERY_REQUIRED (not FAILED). FAILED only after
recovery attempted/exhausted or conclusively unrecoverable.
Consequences: RecoveryPolicy + coordinator must honor the three-state split;
never auto-FAILED on missing checkpoint.
