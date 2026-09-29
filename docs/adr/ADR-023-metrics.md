# ADR-023 — Metrics backend

Status: accepted. Date: 2026-09-07.

Context: CloudWatch-agent gating would couple correctness to AWS telemetry.
Decision: Prometheus /metrics stays canonical for app instrumentation;
CloudWatch (or infra telemetry) covers EC2/ASG/Dynamo/S3/KMS/API only.
Dashboards/alerts scrape Prometheus (or managed Prometheus) in AWS.
Consequences: keep main.py /metrics; no CloudWatch prerequisite for E2E.
