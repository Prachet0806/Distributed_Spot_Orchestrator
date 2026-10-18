import os
import argparse
import subprocess
import paramiko


def pin_host_key(ip, known_hosts_path, port=22):
    """Explicit first-connect trust step (Track D2).

    Fetch the worker's host keys with ssh-keyscan and append them to the
    deployment known_hosts file, printing SHA256 fingerprints for operator
    verification. This is the ONLY sanctioned TOFU moment: it is explicit,
    logged, and auditable — never silent inline acceptance. Raises when
    no keys are retrieved (fail closed, no deploy).
    """
    print(f"   🔑 Pinning host keys for {ip} → {known_hosts_path} ...")
    proc = subprocess.run(
        ["ssh-keyscan", "-p", str(port), "-T", "10", ip],
        capture_output=True, text=True, timeout=60,
    )
    entries = [ln for ln in (proc.stdout or "").splitlines()
               if ln and not ln.startswith("#")]
    if not entries:
        raise RuntimeError(
            f"ssh-keyscan returned no host keys for {ip} "
            f"(stderr: {(proc.stderr or '').strip()}): refusing to deploy")
    os.makedirs(os.path.dirname(os.path.abspath(known_hosts_path)) or ".",
                exist_ok=True)
    with open(known_hosts_path, "a") as f:
        for entry in entries:
            f.write(entry + "\n")
    # Operator-verifiable fingerprints of exactly what was pinned.
    try:
        fp = subprocess.run(
            ["ssh-keygen", "-lf", known_hosts_path],
            capture_output=True, text=True, timeout=30,
        )
        if fp.stdout.strip():
            print(f"   🔑 Pinned fingerprints:\n{fp.stdout.strip()}")
    except Exception:
        pass
    print(f"   🔑 Pinned {len(entries)} host key(s) for {ip}. "
          f"Verify fingerprints out-of-band before production use.")
    return entries


def create_ssh_client(ip, key_path, user="ubuntu", known_hosts_path=None,
                      allow_tofu=False):
    """Paramiko client with strict host-key policy (Track D2).

    Default is RejectPolicy: unknown host keys are refused. `allow_tofu`
    downgrades to a LOUD WarningPolicy for explicitly-approved first contact
    only — silent AutoAddPolicy is never used.
    """
    client = paramiko.SSHClient()
    if known_hosts_path and os.path.exists(known_hosts_path):
        client.load_host_keys(known_hosts_path)
    else:
        client.load_system_host_keys()
    if allow_tofu:
        print("   ⚠️  TOFU explicitly allowed for this connection "
              "(WarningPolicy); pin keys with --pin for production.")
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(ip, username=user, key_filename=key_path)
    return client


def deploy(ip, key_path, project_root, known_hosts_path=None,
           pin=False, allow_tofu=False):
    print(f"🔎 Verifying pinned worker image: {ip}")

    if pin:
        if not known_hosts_path:
            raise SystemExit("--pin requires --known-hosts <path>")
        pin_host_key(ip, known_hosts_path)
    elif not allow_tofu and not (
            known_hosts_path and os.path.exists(known_hosts_path)):
        raise SystemExit(
            "Refusing silent first-connect TOFU (Track D2): pass --pin "
            "--known-hosts <path> to pin keys explicitly, or --allow-tofu "
            "for a loud, one-off exception.")

    ssh = create_ssh_client(ip, key_path, known_hosts_path=known_hosts_path,
                            allow_tofu=allow_tofu)
    try:
        digest = verify_worker_control(ssh)
    finally:
        ssh.close()
    print(f"✅ Worker control image verified: {digest}")
    return digest


def verify_worker_control(ssh):
    """Refuse deployment to a worker without a pinned, root-owned controller."""
    root = "/opt/spot-worker-control"
    for path in (root, root + "/control_entry.py",
                 root + "/checkpoint/criu_wrapper.sh"):
        _, stdout, stderr = ssh.exec_command(f"stat -c '%u %a' {path}")
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"worker control path missing: {path}: {stderr.read().decode()}")
        parts = stdout.read().decode().strip().split()
        if len(parts) != 2 or parts[0] != "0" or int(parts[1], 8) & 0o022:
            raise RuntimeError(f"worker control path is writable or not root-owned: {path}")
    _, stdout, stderr = ssh.exec_command(
        f"sudo -n python3 -I {root}/control_entry.py --self-check")
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError("worker control integrity check failed: " + stderr.read().decode())
    digest = stdout.read().decode().strip()
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError("worker control returned an invalid digest")
    return digest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", required=True, help="Public IP of the worker")
    parser.add_argument("--key", required=True, help="Path to SSH private key")
    parser.add_argument("--root", default=".", help="Kept for CLI continuity; image contents are immutable")
    parser.add_argument("--known-hosts", default=None,
                        help="Pinned known_hosts file for this deployment")
    parser.add_argument("--pin", action="store_true",
                        help="Pin worker host keys explicitly before deploy")
    parser.add_argument("--allow-tofu", action="store_true",
                        help="Loud one-off trust-on-first-use (non-production)")
    args = parser.parse_args(argv)

    # Track D5: emulation trust must never ride into an AWS deployment.
    if os.getenv("EMU_TRUST_MODE", "").lower() in ("1", "true", "yes"):
        raise SystemExit(
            "EMU_TRUST_MODE is set: refusing AWS worker deployment "
            "(Track D5). Unset it for any real-infrastructure path.")

    deploy(args.ip, args.key, args.root,
           known_hosts_path=args.known_hosts, pin=args.pin,
           allow_tofu=args.allow_tofu)


if __name__ == "__main__":
    main()

