import os
import argparse
import paramiko
from scp import SCPClient


def create_ssh_client(ip, key_path, user="ubuntu"):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, username=user, key_filename=key_path)
    return client


def deploy(ip, key_path, project_root):
    print(f"🚀 Deploying code to Worker: {ip}")

    ssh = create_ssh_client(ip, key_path)
    scp = SCPClient(ssh.get_transport())

    remote_base = "/opt/job_workspace"

    # Upload critical folders
    for folder in ["worker", "storage", "checkpoint"]:
        local_path = os.path.join(project_root, folder)
        remote_path = os.path.join(remote_base, folder)
        print(f"   📂 Copying {folder}...")
        scp.put(local_path, recursive=True, remote_path=remote_base)

    # Fix permissions & install deps
    print("   🔧 Setting permissions and installing deps...")
    ssh.exec_command(f"sudo chown -R ubuntu:ubuntu {remote_base}")
    # Use python3 -m pip to avoid PATH issues on some AMIs
    ssh.exec_command(f"python3 -m pip install boto3 requests paramiko scp")

    scp.close()
    ssh.close()
    print("✅ Deployment Complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", required=True, help="Public IP of the worker")
    parser.add_argument("--key", required=True, help="Path to SSH private key")
    parser.add_argument("--root", default=".", help="Project root directory")
    args = parser.parse_args()

    deploy(args.ip, args.key, args.root)

