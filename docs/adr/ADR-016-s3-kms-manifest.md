# ADR-016 — S3 + KMS and manifest

Status: accepted. Date: 2026-09-07.

Context: checkpoints must be durable, encrypted, verifiable cross-region.
Decision: hub-and-spoke home-region bucket + SSE-KMS (+MRK where needed);
scoped IAM; no secrets in checkpoints. 64MiB multipart parts with per-part +
whole SHA-256; manifest.json (schema 1: ids, lineage, source, sizes, criu
versions) + manifest.sha256.
Consequences: TransferManager + S3Manager implement exactly this layout.
