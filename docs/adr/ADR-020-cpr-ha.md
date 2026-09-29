# ADR-020 — CPR HA and home-outage scope

Status: accepted. Date: 2026-09-07.

Context: active-active CPR is future work; crash safety is not.
Decision: ASG min=max=1 + DynamoDB lease + self-fencing; AWS E2E covers CPR
crash/replacement/lease-loss/duplicate/mid-fence + worker-continuation during
outage. Home-region outage itself is an accepted limitation (no new decisions,
reconcile after restore).
Consequences: implement lease + bootstrap-from-stores; exclude home-outage test.
