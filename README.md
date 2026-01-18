# Spot Market Arbitrage Cluster

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Terraform](https://img.shields.io/badge/terraform-1.5%2B-purple?logo=terraform&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-EC2%20Spot%20|%20S3%20|%20DynamoDB-orange?logo=amazon-aws&logoColor=white)
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
* **Orchestrator:** A Python-based control loop that polls AWS Spot Price APIs and orchestrates migrations via SSH.
* **Worker:** Ephemeral Spot Instances locked to specific CPU architectures (Intel Skylake+) to ensure CRIU binary compatibility.
* **State Store:** A centralized registry (JSON/DynamoDB) tracking the lifecycle of every job.
* **Key stores and services:**
    - **S3:** checkpoint archive storage
    - **DynamoDB or JSON:** job registry
    - **EC2 Spot:** worker instances

---

## Key Features

* **Live Process Migration:** Pause a job in Virginia and resume it in Oregon without losing memory state or open file descriptors.
* **Price Intelligence:** Real-time polling of AWS Spot markets with volatility tracking to prevent "jittery" migrations.
* **Crash-Safe State Machine:** Transactional migration flow (`CHECKPOINT` → `UPLOAD` → `PROVISION` → `RESTORE`) ensures jobs are never duplicated or lost.
* **Infrastructure as Code:** Fully automated worker provisioning via Terraform with strictly scoped IAM roles.

---

## 🚀 Getting Started

### Prerequisites

* **Python 3.10+**
* **Terraform 1.5+**
* **AWS CLI** (configured with Administrator access for initial setup)
* **SSH Key Pair** (uploaded to all target AWS regions)

### 1. Configure AWS credentials on Orchestrator machine

Provision the Access Key and Secret Key.

```bash
aws configure
```

Verify with 
```bash
aws sts get-caller-identity
```