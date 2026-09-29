# ADR-018 — Worker base image

Status: accepted. Date: 2026-09-07.

Context: boot-time `apt install criu` blows the interruption budget and drifts.
Decision: baked immutable AMI — Ubuntu 22.04 x86_64, pinned CRIU + tested
kernel — containing wrapper, controller, runtime deps, monitoring config;
no workload secrets. Pool registry pins AMI/digest; provisioner launches it.
Owner: infra/DevOps pipeline, not Coordinator.
Consequences: add AMI pipeline (P1); userdata shrinks to identity/bootstrap only.
