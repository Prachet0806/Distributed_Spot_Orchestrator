# Variables for Spot Arbitrage Cluster (workers + state).
# NOTE: checkpoint_bucket_name/dynamodb_table_name/kms_key_arn/environment
# predate the V2 TF layout; the *_V2 names below are the ones main.tf and
# networking.tf actually consume. Prefer passing -var-file with your env.

variable "checkpoint_bucket_name" {
  description = "Name of the S3 bucket for checkpoint storage"
  type        = string
  default     = ""

  validation {
    condition     = var.checkpoint_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.checkpoint_bucket_name))
    error_message = "Bucket name must be lowercase alphanumeric with hyphens."
  }
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table for job registry"
  type        = string
  default     = "spot_arbitrage_registry"
  
  validation {
    condition     = length(var.dynamodb_table_name) > 0
    error_message = "DynamoDB table name cannot be empty."
  }
}

variable "kms_key_arn" {
  description = "ARN of KMS key for checkpoint encryption (optional, empty string for default)"
  type        = string
  default     = ""
}

variable "environment" {
  description = "Environment name (e.g., dev, staging, prod)"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

# --- consumed by main.tf / networking.tf / outputs.tf ---
variable "target_region" {
  description = "AWS region for worker instances"
  type        = string
  default     = "us-east-1"
}

variable "ami_id" {
  description = "Baked worker AMI (Ubuntu 22.04, pinned CRIU/kernel, ADR-018)"
  type        = string
  default     = ""

  validation {
    condition     = var.create_demo_worker == false || can(regex("^ami-[0-9a-f]+$", var.ami_id))
    error_message = "ami_id must be a valid AMI ID when create_demo_worker is true."
  }
}

variable "instance_type" {
  description = "Worker instance type (locked per pool for CRIU portability)"
  type        = string
  default     = "t3.micro"
}

variable "ssh_key_name" {
  description = "EC2 SSH key pair name present in target_region"
  type        = string
  default     = ""
}

variable "ssh_user" {
  description = "SSH username for worker instances"
  type        = string
  default     = "ubuntu"
}

variable "max_spot_price" {
  description = "Maximum Spot price (USD/hour) for one-time requests"
  type        = string
  default     = "0.10"
}

variable "my_ip" {
  description = "Orchestrator public IP in CIDR notation for SSH ingress (e.g. 1.2.3.4/32)"
  type        = string
  default     = ""

  validation {
    condition     = var.create_demo_worker == false || can(cidrhost(var.my_ip, 0))
    error_message = "my_ip must be valid CIDR when create_demo_worker is true."
  }
}

variable "create_demo_worker" {
  description = "Launch the single demo spot worker (ephemeral workers are provisioned per-migration; keep false unless smoke-testing)"
  type        = bool
  default     = false
}
