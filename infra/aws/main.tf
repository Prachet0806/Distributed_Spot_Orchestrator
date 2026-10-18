provider "aws" {
  region = var.target_region
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "checkpoints" {
  bucket = "spot-arbitrage-checkpoints-${random_id.bucket_suffix.hex}"
}

resource "aws_s3_bucket_versioning" "checkpoints" {
  bucket = aws_s3_bucket.checkpoints.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "checkpoints" {
  bucket = aws_s3_bucket.checkpoints.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.checkpoint_key.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "checkpoints" {
  bucket = aws_s3_bucket.checkpoints.id
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "checkpoints" {
  bucket                  = aws_s3_bucket.checkpoints.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_instance" "spot_worker" {
  count         = var.create_demo_worker ? 1 : 0
  ami           = var.ami_id
  instance_type = var.instance_type # Locked for Phase 1 CPU compatibility
  key_name      = var.ssh_key_name

  vpc_security_group_ids = [aws_security_group.spot_worker_sg.id]
  iam_instance_profile   = aws_iam_instance_profile.worker_profile.name

  # Spot Request
  instance_market_options {
    market_type = "spot"
    spot_options {
      max_price          = var.max_spot_price
      spot_instance_type = "one-time"
    }
  }

  # The Bootstrap Script
  user_data = replace(file("${path.module}/userdata.sh"), "\r\n", "\n")

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  tags = {
    Name        = "Spot-Worker-${var.target_region}"
    Role        = "Worker"
    ManagedBy   = "spot-arbitrage-terraform"
    Environment = var.environment
  }
}

output "checkpoint_bucket_name" {
  value = aws_s3_bucket.checkpoints.id
}
