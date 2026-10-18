# ADR-024 — V1 retirement

Status: accepted; revised 2026-10-14 by explicit project-owner instruction.

Decision: delete the V1 execution engine and all V1 compatibility now. This
supersedes the former staged deletion gates and retained JSON CLI policy.

Removed: Migrator, DecisionEngine, SLA policy, engine selection, registry_cli,
JSON registry, V1 lifecycle table and mutation API, executor aliases, old
result readers, validation stubs, and alternate provisioning fallbacks.

V2 emulation is explicit through MemoryRegistry and EmulatedProvisioner.
Current telemetry schema revision 1 remains supported as a V2 contract.

Consequences: old records and commands are unsupported; there is no automatic
record conversion or engine fallback. This source change neither deletes AWS
resources/data nor satisfies live migration, security, recovery, or soak gates.
