# Least-privilege IAM policies for Spot Arbitrage Cluster

# Worker Role - Minimal permissions for checkpoint operations only
resource "aws_iam_role" "spot_worker_role" {
  name = "spot-arbitrage-worker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name    = "spot-arbitrage-worker"
    Purpose = "checkpoint-operations"
  }
}

# S3 Policy - Strictly limited to checkpoint bucket
resource "aws_iam_policy" "worker_s3_checkpoint_policy" {
  name        = "spot-arbitrage-worker-s3-checkpoint"
  description = "Allows worker to upload/download checkpoints to specific S3 bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ListCheckpointBucket"
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = aws_s3_bucket.checkpoints.arn
      },
      {
        Sid      = "CheckpointObjectRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.checkpoints.arn}/*"
      },
      {
        Sid      = "CheckpointObjectWrite"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.checkpoints.arn}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-server-side-encryption"                = "aws:kms"
            "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.checkpoint_key.arn
          }
        }
      }
    ]
  })
}

# KMS Policy - Allow workers to use KMS for checkpoint encryption
resource "aws_iam_policy" "worker_kms_policy" {
  name        = "spot-arbitrage-worker-kms"
  description = "Allows worker to encrypt/decrypt checkpoints with KMS"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KMSCheckpointEncryption"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey"
        ]
        Resource = aws_kms_key.checkpoint_key.arn
      }
    ]
  })
}

# Attach policies to worker role
resource "aws_iam_role_policy_attachment" "worker_s3_attach" {
  role       = aws_iam_role.spot_worker_role.name
  policy_arn = aws_iam_policy.worker_s3_checkpoint_policy.arn
}

resource "aws_iam_role_policy_attachment" "worker_kms_attach" {
  role       = aws_iam_role.spot_worker_role.name
  policy_arn = aws_iam_policy.worker_kms_policy.arn
}

resource "aws_iam_role_policy" "worker_lease_read" {
  name = "spot-arbitrage-worker-lease-read"
  role = aws_iam_role.spot_worker_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem"]
      Resource = aws_dynamodb_table.control_plane_lease.arn
    }]
  })
}

# Instance Profile for attaching role to EC2
resource "aws_iam_instance_profile" "worker_profile" {
  name = "spot-arbitrage-worker-profile"
  role = aws_iam_role.spot_worker_role.name
}

# Orchestrator Role - Permissions for provisioning and managing workers
resource "aws_iam_role" "orchestrator_role" {
  name = "spot-arbitrage-orchestrator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name    = "spot-arbitrage-orchestrator"
    Purpose = "worker-management"
  }
}

# EC2 Policy - Limited to spot instance operations
resource "aws_iam_policy" "orchestrator_ec2_policy" {
  name        = "spot-arbitrage-orchestrator-ec2"
  description = "Allows orchestrator to manage spot instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SpotPriceQuery"
        Effect = "Allow"
        Action = [
          "ec2:DescribeSpotPriceHistory",
          "ec2:DescribeRegions",
          "ec2:DescribeAvailabilityZones"
        ]
        Resource = "*"
      },
      {
        Sid    = "SpotInstanceLaunch"
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:CreateTags"
        ]
        Resource = "*"
      },
      {
        Sid    = "WorkerDescribeAndTerminate"
        Effect = "Allow"
        Action = [
          "ec2:TerminateInstances",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus"
        ]
        Resource = "*"
      },
      {
        Sid    = "KeyPairAccess"
        Effect = "Allow"
        Action = [
          "ec2:DescribeKeyPairs"
        ]
        Resource = "*"
      },
      {
        Sid    = "SecurityGroupAccess"
        Effect = "Allow"
        Action = [
          "ec2:DescribeSecurityGroups"
        ]
        Resource = "*"
      },
      {
        Sid    = "IAMPassRole"
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = aws_iam_role.spot_worker_role.arn
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ec2.amazonaws.com"
          }
        }
      }
    ]
  })
}

# DynamoDB policy for every durable control-plane store.
resource "aws_iam_policy" "orchestrator_dynamodb_policy" {
  name        = "spot-arbitrage-orchestrator-dynamodb"
  description = "Allows orchestrator to access V2 control-plane tables"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ControlPlaneTableAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          aws_dynamodb_table.job_registry.arn,
          aws_dynamodb_table.candidate_pools.arn,
          aws_dynamodb_table.plans.arn,
          aws_dynamodb_table.event_ledger.arn,
          aws_dynamodb_table.checkpoints.arn,
          aws_dynamodb_table.audit.arn,
          aws_dynamodb_table.control_plane_lease.arn,
          aws_dynamodb_table.migration_history.arn,
          aws_dynamodb_table.operations.arn,
          aws_dynamodb_table.pool_reservations.arn,
          aws_dynamodb_table.config.arn
        ]
      },
      {
        Sid    = "ControlPlaneIndexAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:Query"
        ]
        Resource = [
          "${aws_dynamodb_table.job_registry.arn}/index/*",
          "${aws_dynamodb_table.checkpoints.arn}/index/*",
          "${aws_dynamodb_table.audit.arn}/index/*",
          "${aws_dynamodb_table.migration_history.arn}/index/*"
        ]
      }
    ]
  })
}

# S3 checkpoint storage and verification for orchestrator.
resource "aws_iam_policy" "orchestrator_s3_policy" {
  name        = "spot-arbitrage-orchestrator-s3"
  description = "Allows orchestrator to persist and verify checkpoints"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CheckpointBucketRead"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.checkpoints.arn
      },
      {
        Sid      = "CheckpointObjectRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.checkpoints.arn}/*"
      },
      {
        Sid      = "CheckpointObjectWrite"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.checkpoints.arn}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-server-side-encryption"                = "aws:kms"
            "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.checkpoint_key.arn
          }
        }
      }
    ]
  })
}

# Attach policies to orchestrator role
resource "aws_iam_role_policy_attachment" "orchestrator_ec2_attach" {
  role       = aws_iam_role.orchestrator_role.name
  policy_arn = aws_iam_policy.orchestrator_ec2_policy.arn
}

resource "aws_iam_role_policy_attachment" "orchestrator_dynamodb_attach" {
  role       = aws_iam_role.orchestrator_role.name
  policy_arn = aws_iam_policy.orchestrator_dynamodb_policy.arn
}

resource "aws_iam_role_policy_attachment" "orchestrator_s3_attach" {
  role       = aws_iam_role.orchestrator_role.name
  policy_arn = aws_iam_policy.orchestrator_s3_policy.arn
}

resource "aws_iam_role_policy" "orchestrator_checkpoint_kms" {
  name = "spot-arbitrage-orchestrator-checkpoint-kms"
  role = aws_iam_role.orchestrator_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
      Resource = aws_kms_key.checkpoint_key.arn
    }]
  })
}

# Instance Profile for orchestrator
resource "aws_iam_instance_profile" "orchestrator_profile" {
  name = "spot-arbitrage-orchestrator-profile"
  role = aws_iam_role.orchestrator_role.name
}

# Outputs
output "worker_role_arn" {
  value       = aws_iam_role.spot_worker_role.arn
  description = "ARN of the worker IAM role"
}

output "worker_instance_profile_name" {
  value       = aws_iam_instance_profile.worker_profile.name
  description = "Name of the worker instance profile"
}

output "orchestrator_role_arn" {
  value       = aws_iam_role.orchestrator_role.arn
  description = "ARN of the orchestrator IAM role"
}

output "orchestrator_instance_profile_name" {
  value       = aws_iam_instance_profile.orchestrator_profile.name
  description = "Name of the orchestrator instance profile"
}
