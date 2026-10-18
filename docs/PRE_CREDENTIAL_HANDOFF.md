# Pre-credential implementation handoff

This document records what can be checked without live AWS credentials and
the evidence required before enabling migrations in an AWS account.

## Local commands

On Windows, install `requirements-dev.lock` in a virtual environment and run:

```powershell
python -m pytest -q --disable-warnings
python scripts/run_local_lab.py --mode services --output lab-results
terraform -chdir=infra/aws validate
```

The services runner uses fake AWS credentials and local Moto endpoints. It
exercises the production iteration, durable stores, and a restart across
independent Python processes. Its `summary.json` and JUnit XML are written to
`lab-results/`, which is ignored by Git. It cannot establish IAM, KMS, EC2,
or real S3 behavior.

On a Linux host with root, CRIU, OpenSSH server, and
`requirements-dev-linux.lock` installed:

```bash
python scripts/run_local_lab.py --mode linux --output lab-results
```

The Linux lab starts source and target SSH workers in separate PID/mount
namespaces, then runs the production coordinator through dump, artifact
publication, target staging, restore, fence, validation, activation, and
progress advancement. A passing result requires all phases to succeed.

## Current evidence and open gates

As of 2026-10-18, the local service runner passes and Terraform validates.
The Linux lab on WSL2 kernel `6.6.87.2-microsoft-standard-WSL2` with CRIU
`3.16.1` dumps a running Python fixture and verifies the staged artifact;
An earlier restore failed because transfer changed `workload.log` from mode
`0640` to `0644`; checkpoint transport now preserves modes, and the wrapper
restores recorded ownership for writable job files. With that fixed, CRIU
restore still fails with signal 11 after creating the restored processes. The
lab saves the CRIU log tail in `linux-evidence.json`. On that pre-fence failure, the local lab
verified that the registry returned to `RUNNING` and the original source PID
was no longer stopped. This is an observed local failure with a verified
source rollback, not a passed migration. Re-run on a native Linux worker or a
compatible kernel and CRIU build before crediting the real migration gate.

Workload admission rejects declared GPU and external side effects. The worker
image now requires a root-owned, digest-checked control tree and a baked
allowlist of reviewed script hashes. The production launcher copies approved
bytes to a root-owned staging directory; restore rechecks and stages that
same script on the target. Launch drops the process to fixed UID/GID
62000 (`spotjob`), and does not pass controller credentials to it. The boot
helper denies IPv4 and IPv6 egress for that UID and runs IMDS interruption
polling in a root-owned systemd service. Its workspace permissions keep the
job out of checkpoint directories. These boot and privilege boundaries are
syntax/unit checked but have **not** been exercised on a baked AMI. The local
fixture intentionally uses `WORKER_PROVIDER=local` and bypasses them. Memory,
CPU and filesystem quotas are still absent, so only reviewed CPU batch scripts
are eligible for an initial pilot.

The fixture's 20% wall-clock overhead test has both passed and exceeded its
threshold on this host. The latest WSL suite measured a 37.2% median, and an
isolated repeat measured 21.6%; both fail the gate. Keep the threshold and
record measurements on the qualified workload/kernel and checkpoint interval.
Completed-job economics
requires a successful restore and actual compute/storage/network billing.

Crash recovery retains plans and operation receipts and quarantines restart
frontiers as `RECONCILIATION_REQUIRED`. Restore now has a root-owned issue
journal and status receipt, so an unknown restore response cannot blindly
launch a second process. Automatic phase replay remains open: remaining
external commands need live status resolvers and crash-at-every-phase tests,
especially around fencing and activation. Do not claim automatic recovery
based on the bootstrap report alone.

Before AWS execution, review the Terraform plan, provision a dev account,
publish a pinned worker AMI and candidate pool definitions, configure the
checkpoint bucket and `CHECKPOINT_KMS_KEY_ARN`, and validate instance profile,
lease reads, S3 writes/reads, KMS decrypt, network access, and SSH host keys
on real workers. Then run a fixture migration, forced interruption, and
failure recovery in each intended region pair. These are live qualification
gates; local success does not substitute for them.

The controller also requires `SSH_PRIVATE_KEY_PATH` and
`SSH_KNOWN_HOSTS_PATH` for execution. Pin each worker's host key against a
trusted instance identity before adding it to the known-hosts file.
