#!/bin/bash
set -euo pipefail

# Worker bootstrap for a baked, pinned AMI. Launch never fetches mutable code.

# The privileged controller and CRIU wrapper must be immutable in the image.
python3 -I /opt/spot-worker-control/control_entry.py --self-check

# Check the immutable runtime before advertising readiness.
command -v criu >/dev/null
python3 -c 'import boto3, yaml, paramiko'
criu check >/dev/null

# Establish the workload UID/GID, workspace permissions, egress rule and
# privileged interruption monitor before admitting a job.
bash /opt/spot-worker-control/scripts/bootstrap_worker_security.sh

# Bind the control identity to IMDSv2.
python3 - <<'PY'
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

base = "http://169.254.169.254/latest"
token = urlopen(Request(base + "/api/token", method="PUT",
    headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"}), timeout=5).read().decode()
document = json.load(urlopen(Request(base + "/dynamic/instance-identity/document",
    headers={"X-aws-ec2-metadata-token": token}), timeout=5))
directory = Path("/var/lib/spot-worker")
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(directory, 0o700)
identity = {"provider": "aws", "instance_id": document["instanceId"],
    "admission_epoch": 0, "lease_region": document["region"],
    "lease_table": "spot_arbitrage_control_plane_lease"}
temporary = directory / "identity.pending.json"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as output:
    json.dump(identity, output)
    output.flush()
    os.fsync(output.fileno())
os.replace(temporary, directory / "identity.json")
PY
touch /tmp/userdata_complete
