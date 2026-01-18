# Spot Market Arbitrage Cluster

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python\&logoColor=white)
![Terraform](https://img.shields.io/badge/terraform-1.5%2B-purple?logo=terraform\&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-EC2%20Spot%20|%20S3%20|%20DynamoDB-orange?logo=amazon-aws\&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

**Cost-optimize long-running batch workloads by live-migrating running processes between AWS regions.**

The **Spot Market Arbitrage Cluster** is a distributed control system that decouples compute jobs from physical availability zones. By leveraging **CRIU (Checkpoint/Restore In Userspace)**, it treats running processes as portable data objects, automatically freezing and moving them to whichever AWS region offers the lowest spot price.

---

## Architecture

The system operates as a "Financial Hypervisor," consisting of three core components:

```
┌───────────────────────────────────────────────────────────┐
│                      Orchestrator                         │
│   watcher → decision_engine → migrator → registry         │
└───────────────┬───────────────────────────────────────────┘
                │
                │  SSH + CRIU + S3
                ▼
┌───────────────────────────────────────────────────────────┐
│                   Worker Instances (Spot)                 │
│   job_runner + checkpoint files + s3_manager              │
└───────────────────────────────────────────────────────────┘
```

* **Orchestrator:** Python control loop that polls AWS Spot Price APIs and orchestrates migrations via SSH.
* **Worker:** Ephemeral Spot Instances locked to compatible CPU architectures to ensure CRIU portability.
* **State Store:** Centralized registry (DynamoDB/JSON) tracking the lifecycle of every job.
* **Key stores and services:** S3 (checkpoints), DynamoDB/JSON (registry), EC2 Spot (compute).

---

## Key Features

* **Live Process Migration:** Pause a job in Virginia and resume it in Oregon without losing memory state.
* **Price Intelligence:** Real-time polling with volatility tracking to avoid jittery migrations.
* **Crash-Safe State Machine:** Transactional flow (`CHECKPOINT → UPLOAD → PROVISION → RESTORE`).
* **Infrastructure as Code:** Terraform provisioning with least-privilege IAM roles.

---

## Getting Started

### Prerequisites

* **Python 3.10+**
* **Terraform 1.5+**
* **AWS CLI** (configured)
* **SSH Key Pair** (uploaded to all target regions)

### Pre‑Checks

* Create DynamoDB table `spot_arbitrage_registry` (PK: `job_id`).
* Bake a worker AMI with:

  * Ubuntu 22.04
  * CRIU installed (`sudo criu check` passes)
  * Python + project code under `/opt/job_workspace`
  * (Optional) Preinstalled deps: `pip install -r requirements.txt`
  * Copy the AMI to every target region
* Create a worker IAM role with scoped S3 access to the checkpoint bucket.
* Create identical worker security groups in every region (SSH from your IP).

### Step 1. Configure AWS credentials on Orchestrator machine

```bash
aws configure
aws sts get-caller-identity
```

---

## Step 2. Infrastructure (Terraform)

Provision a source worker and the checkpoint bucket:

```bash
cd infra/aws
terraform init
terraform apply
```

Outputs include the checkpoint bucket name and the worker public IP.

---

## Step 3. Bake a Wide Worker AMI (Recommended)

1. Launch an Ubuntu 22.04 instance.
2. Install dependencies:

   ```bash
   sudo apt update && sudo apt install -y criu python3-pip awscli
   sudo criu check
   ```
3. Copy project code to `/opt/job_workspace` and install deps:

   ```bash
   sudo mkdir -p /opt/job_workspace && sudo chown -R ubuntu:ubuntu /opt/job_workspace
   scp -r checkpoint worker storage config requirements.txt ubuntu@<ip>:/opt/job_workspace/
   python3 -m pip install -r /opt/job_workspace/requirements.txt
   ```
4. Create an AMI from the instance and copy it to all regions.

---

## Step 4. Register a Job

Start the sample job on a source worker:

```bash
cd /opt/job_workspace
python worker/job_runner.py &
echo $!  # PID
```

Register it:

```bash
python scripts/registry_cli.py --backend dynamo --table spot_arbitrage_registry --region us-east-1 \
  create --job-id job-1 --state RUNNING --region us-east-1 --public-ip <source_ip> --pid <pid> --workload-type batch
```

---

## Step 5. Run the Orchestrator

```bash
export CHECKPOINT_BUCKET=<your_bucket>
python -m orchestrator.main --multi-job --regions us-east-1,ap-south-1 --instance-type t3.micro --migrate
```

Watch logs for migration decisions and progress.

---

## Verification

* Check DynamoDB for updated `region` and `public_ip`.
* SSH into the target worker and verify the process is running:

  ```bash
  ps aux | grep python
  ```

---

## Troubleshooting

| Symptom         | Fix                                  |
| --------------- | ------------------------------------ |
| CRIU fails      | Verify kernel + `sudo criu check`    |
| SSH timeout     | Fix security group ingress           |
| S3 AccessDenied | Verify IAM role policy + bucket name |
| Migration loops | Increase cooldown or thresholds      |

---

## Roadmap

* Phase 1: Foundation — complete (migration, DynamoDB, auto-provision)
* Phase 2: Reliability — retries, metrics, circuit breakers
* Phase 3: Scale — HA orchestrator, leader election, SQS queue
* Phase 4: Product — job submission API, dashboards, predictive pricing

