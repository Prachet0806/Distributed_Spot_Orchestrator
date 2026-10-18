"""Root-owned worker control identity initialized from IMDSv2 at boot."""


def control_userdata(admission_epoch: int) -> str:
    epoch = int(admission_epoch)
    if epoch < 0:
        raise ValueError("admission epoch must be nonnegative")
    return f'''#!/bin/bash
set -euo pipefail
python3 -I /opt/spot-worker-control/control_entry.py --self-check
command -v criu >/dev/null
python3 -c 'import boto3, yaml, paramiko'
criu check >/dev/null
bash /opt/spot-worker-control/scripts/bootstrap_worker_security.sh
python3 - <<'PY'
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

base = "http://169.254.169.254/latest"
token = urlopen(Request(base + "/api/token", method="PUT",
    headers={{"X-aws-ec2-metadata-token-ttl-seconds": "21600"}}), timeout=5).read().decode()
document = json.load(urlopen(Request(base + "/dynamic/instance-identity/document",
    headers={{"X-aws-ec2-metadata-token": token}}), timeout=5))
instance_id, region = document["instanceId"], document["region"]
if not instance_id.startswith("i-") or not region:
    raise SystemExit("invalid instance identity document")
identity = {{"provider": "aws", "instance_id": instance_id,
    "admission_epoch": {epoch}, "lease_region": region,
    "lease_table": "spot_arbitrage_control_plane_lease"}}
directory = Path("/var/lib/spot-worker")
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(directory, 0o700)
temporary = directory / "identity.pending.json"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as output:
    json.dump(identity, output)
    output.flush()
    os.fsync(output.fileno())
os.replace(temporary, directory / "identity.json")
PY
'''
