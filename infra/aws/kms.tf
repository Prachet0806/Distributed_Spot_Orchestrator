# Checkpoint encryption key (ADR-016: S3 + SSE-KMS, MRK-capable).
# Pass its ARN back as kms_key_arn to scope the worker KMS policy (iam.tf).

resource "aws_kms_key" "checkpoint_key" {
  description             = "Spot arbitrage checkpoint encryption (home region hub)"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name        = "spot-arbitrage-checkpoints"
    Environment = var.environment
  }
}

resource "aws_kms_alias" "checkpoint_key" {
  name          = "alias/spot-arbitrage-checkpoints-${var.environment}"
  target_key_id = aws_kms_key.checkpoint_key.key_id
}
