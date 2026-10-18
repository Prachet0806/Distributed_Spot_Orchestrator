# Terraform Infrastructure for Spot Arbitrage Cluster

This directory contains the V2 Terraform configuration: worker networking/compute plus the control-plane state layer (7 DynamoDB tables, KMS checkpoint key, hardened S3 checkpoint bucket). Static guards live in `tests/test_infra_tf.py` (no `terraform` binary needed in CI).

> **Status: never applied.** Per `docs/IMPLEMENTATION_STATUS.md` this layout is code-complete but `terraform apply` has never run. Do not treat outputs below as provisioned until Track A2 lands.

## Prerequisites

1. **AWS CLI configured** with appropriate credentials
2. **Terraform installed** (>= 1.0)
3. **EC2 Key Pair** created in every candidate region (one key name shared across regions)
4. **Your public IP address** (orchestrator SSH ingress, CPR → worker channel)

## Quick Start (Track A2, aligned with `docs/e2e-v2.md`)

1. **Copy the example variables file:**
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. **Edit `terraform.tfvars` with your values:**
   ```hcl
   target_region = "us-east-1"
   ami_id        = "ami-0c7217cdde317cfec"  # Baked worker AMI, per region (Track A4)!
   ssh_key_name  = "my-aws-keypair"
   my_ip         = "1.2.3.4/32"             # Orchestrator public IP
   environment   = "dev"                    # dev | staging | prod
   # kms_key_arn filled from outputs after first apply (Track A5)
   ```

3. **Initialize, review, apply (dev account first):**
   ```bash
   terraform init
   terraform validate
   terraform plan
   terraform apply
   ```

4. **Wire the outputs back into the control plane** (`docs/e2e-v2.md` §AWS E2E):
   - `checkpoint_kms_key_arn` → `kms_key_arn` (scope the worker IAM policy, Track A5/A7)
   - `dynamodb_registry_table` → registry table (managed here — do not hand-create)
   - Baked AMI IDs + security group IDs per region → `config/runtime.yaml`

5. **Validate the guards:**
   ```bash
   python -m pytest tests/test_infra_tf.py -v
   ```

## Files

- `main.tf` - S3 checkpoint bucket (hardened) + optional demo spot worker
- `networking.tf` - Security groups (CPR → worker SSH only)
- `iam.tf` - Worker/instance roles (checkpoint bucket + KMS decrypt scope)
- `dynamodb.tf` - 7 V2 state tables (`job_registry`, `candidate_pools`, `plans`, `event_ledger`, `checkpoints`, `audit`, `migration_history`) with `StateIndex`, ledger composite key (`event_id` + `consumer_id`), `LineageIndex`
- `kms.tf` - `aws_kms_key.checkpoint_key` for SSE-KMS checkpoint encryption
- `variables.tf` - Input variable definitions
- `outputs.tf` - Output values (see below)
- `userdata.sh` - Bootstrap script for instances (identity/bootstrap only per ADR-018; fleet code comes from the baked AMI)
- `terraform.tfvars.example` - Example variables file

## Variables

### Required (guarded by `tests/test_infra_tf.py`)

- `target_region` - AWS region (e.g., "us-east-1")
- `ami_id` - Baked worker AMI ID matching the region (Ubuntu 22.04, pinned CRIU/kernel, ADR-018). Required when `create_demo_worker` is true
- `instance_type` - Worker instance type, locked per pool for CRIU portability (default: "t3.micro")
- `ssh_key_name` - EC2 Key Pair name present in `target_region`
- `ssh_user` - SSH username (default: "ubuntu")
- `max_spot_price` - Maximum Spot price USD/hour (default: "0.10")
- `my_ip` - Orchestrator public IP in CIDR format (e.g., "1.2.3.4/32"). Required when `create_demo_worker` is true
- `dynamodb_table_name` - Job registry table name (default: "spot_arbitrage_registry")
- `kms_key_arn` - Checkpoint KMS key ARN; empty until first apply, then back-filled (Track A5)
- `environment` - dev | staging | prod (default: "prod")

### Optional

- `checkpoint_bucket_name` - S3 checkpoint bucket name override (lowercase alphanumeric + hyphens)
- `create_demo_worker` - Launch the single demo spot worker (default: false; ephemeral workers are provisioned per-migration — keep false unless smoke-testing)

## Outputs

After `terraform apply`, you'll get (see `outputs.tf`):

- `instance_id`, `instance_public_ip`, `instance_private_ip`, `instance_public_dns` - demo worker (when created)
- `ssh_command` - Ready-to-use SSH command for the demo worker
- `security_group_id` - Worker security group (→ `config/runtime.yaml`)
- `iam_role_arn` - Worker instance role
- `checkpoint_kms_key_arn` - KMS key ARN (→ `kms_key_arn`, Track A5)
- `dynamodb_registry_table` - Registry table name

## Finding AMI IDs

For Ubuntu 22.04 LTS AMIs by region:
- Visit: https://cloud-images.ubuntu.com/locator/ec2/
- Filter by: Ubuntu 22.04 LTS, amd64, hvm-ssd
- Use the AMI ID for your target region

Then bake per Track A4 (CRIU + pinned kernel + Python deps). During baking,
run `sudo python3 scripts/install_worker_control.py --allow-script <reviewed.py>`
from a reviewed checkout. Repeat `--allow-script` for each approved workload.
It prints the immutable control digest and refuses to overwrite an existing
installation. The controller and CRIU wrapper live under
`/opt/spot-worker-control`; `/opt/job_workspace` is writable data and must
never contain code executed with `sudo`. Copy the image to each candidate
region and record the AMI IDs and control digest. Gate: the control
`--self-check` and CRIU preflight pass on each image. The AMI must include
`iptables`, `ip6tables`, `systemd`, the `ubuntu` SSH account, and the pinned
Python/CRIU runtime. Boot creates fixed UID/GID 62000 for the workload,
blocks its network egress, and starts the privileged interruption monitor;
verify those controls on the baked image before admitting a job.

## Security Notes

- Security group only allows SSH from `my_ip` (CPR channel), paired to the CPR security group
- IAM roles scope workers to checkpoint-bucket + KMS-decrypt (least-privilege per §11 lands in Track D1; roles are broad today)
- S3 checkpoint bucket: versioning + SSE-KMS (`checkpoint_key`) + incomplete-multipart abort + public-access block
- DynamoDB state tables with point-in-time recovery drills scheduled (Track E2)
- Spot instances can be terminated by AWS at any time
- `EMU_TRUST_MODE` must be unset on the AWS path (Track D5); CI asserts the default
- Always use `terraform destroy` when done to avoid charges

## Troubleshooting

### "Invalid AMI ID"
- Ensure AMI ID matches the target region
- AMI IDs are region-specific
- Prefer baked AMIs (Track A4) over stock Ubuntu images

### "Key pair not found"
- Create key pair in AWS Console: EC2 → Key Pairs → Create
- Use the exact name in `ssh_key_name`
- Key pair must exist in `target_region`

### "Cannot connect via SSH"
- Check security group allows your IP (`my_ip`)
- Verify instance is running: `aws ec2 describe-instances --instance-ids <id>`
- Wait for userdata script to complete (check `/tmp/userdata_complete`)

### "Permission denied (publickey)"
- Ensure SSH key path is correct
- Use `ssh -i ~/.ssh/keyname.pem ubuntu@<ip>` format
- Check key permissions: `chmod 400 ~/.ssh/keyname.pem`

### "Variables guard fails in CI"
- Every `var.*` referenced across `*.tf` must be declared in `variables.tf` (`test_all_referenced_vars_declared`)
- New V2 vars needed: `dynamodb_table_name`, `kms_key_arn`, `environment`

## Userdata notes (identity + bootstrap only)

- `userdata.sh` checks the baked control digest, dependencies and CRIU, then
  establishes the workload security boundary and binds the worker identity
  from IMDSv2. It fails closed if the AMI is missing any requirement. It does
  not install or fetch code during launch.

## Using baked AMIs
- You can create an AMI from a prepped instance (code + deps + CRIU installed) and set `ami_id` to that AMI.
- If launching in multiple regions, copy the AMI to each region and set region-specific `ami_id`.
- Record AMI IDs and the installed control digest in the pool definition.
