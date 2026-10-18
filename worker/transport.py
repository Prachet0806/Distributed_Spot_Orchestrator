# worker/transport.py — worker control channel (ADR-019 trust split).
"""Two transports, one interface. Emulated transport runs commands locally
(tests, lab). SSH transport verifies host keys unless the explicit
`EMU_TRUST_MODE=true` escape hatch is set — and CI must assert the AWS
path never enables it.
"""
import os
import subprocess
from abc import ABC, abstractmethod


def emu_trust_mode() -> bool:
    return os.getenv("EMU_TRUST_MODE", "").lower() in ("1", "true", "yes")


class WorkerTransport(ABC):
    @abstractmethod
    def run_command(self, command: str, timeout: float = 60.0) -> dict:
        """Run a shell command. Returns {returncode, stdout, stderr}."""

    @abstractmethod
    def close(self):
        """Release underlying resources."""


class EmulatedWorkerTransport(WorkerTransport):
    """Local subprocess transport for emulation and unit tests."""

    def run_command(self, command: str, timeout: float = 60.0) -> dict:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr}

    def close(self):
        return None


class SSHWorkerTransport(WorkerTransport):
    """SSH transport for real workers (passwordless sudo assumed)."""

    def __init__(self, host: str, ssh_client=None, emu_trust: bool = False,
                 known_hosts_path: str = None, strict_host_keys: bool = False):
        self.host = host
        self._ssh = ssh_client
        # Trust must be explicit and never cross into AWS deployments silently.
        self.emu_trust = emu_trust or emu_trust_mode()
        # Track D2: pinned known_hosts + strict refusal of first-connect TOFU.
        self.known_hosts_path = known_hosts_path
        self.strict_host_keys = strict_host_keys

    def _client(self):
        if self._ssh is None:
            from orchestrator.utils import SSHClient
            kwargs = {}
            if self.known_hosts_path is not None:
                kwargs["known_hosts_path"] = self.known_hosts_path
            if self.strict_host_keys:
                kwargs["strict"] = True
            self._ssh = SSHClient(self.host, **kwargs)
            self._ssh.connect()
        return self._ssh

    def run_command(self, command: str, timeout: float = 60.0) -> dict:
        client = self._client()
        result = client.run_command(command, check=False, timeout_override=timeout)
        return {"returncode": getattr(result, "returncode", 0),
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", "")}

    def _sftp(self):
        import paramiko
        client = self._client()
        if not os.path.isfile(client.known_hosts_path):
            raise RuntimeError("file transfer requires pinned known_hosts")
        ssh = paramiko.SSHClient()
        ssh.load_host_keys(client.known_hosts_path)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(client.host, port=client.port, username=client.user,
                    key_filename=client.key_path, timeout=client.timeout,
                    banner_timeout=client.timeout, auth_timeout=client.timeout)
        return ssh, ssh.open_sftp()

    def fetch_directory(self, remote, local):
        from pathlib import Path
        import stat
        import posixpath
        ssh, sftp = self._sftp()
        def fetch(source, destination):
            destination.mkdir(parents=True, exist_ok=False)
            for entry in sftp.listdir_attr(source):
                if entry.filename in {".", ".."} or "/" in entry.filename or "\\" in entry.filename:
                    raise RuntimeError("unsafe remote file name")
                src, dst = posixpath.join(source, entry.filename), destination / entry.filename
                if stat.S_ISDIR(entry.st_mode):
                    fetch(src, dst)
                    dst.chmod(stat.S_IMODE(entry.st_mode))
                elif stat.S_ISREG(entry.st_mode):
                    sftp.get(src, str(dst))
                    dst.chmod(stat.S_IMODE(entry.st_mode))
                else:
                    raise RuntimeError("checkpoint contains an unsupported file type")
        try:
            fetch(remote, Path(local))
        finally:
            sftp.close()
            ssh.close()

    def send_directory(self, local, remote):
        from pathlib import Path
        import posixpath
        import stat
        ssh, sftp = self._sftp()
        def send(source, destination):
            sftp.mkdir(destination)
            for path in source.iterdir():
                if path.is_symlink():
                    raise RuntimeError("checkpoint cannot contain links")
                dst = posixpath.join(destination, path.name)
                if path.is_dir():
                    send(path, dst)
                elif path.is_file():
                    sftp.put(str(path), dst)
                    sftp.chmod(dst, stat.S_IMODE(path.stat().st_mode))
                else:
                    raise RuntimeError("unsupported local file type")
            sftp.chmod(destination, stat.S_IMODE(source.stat().st_mode))
        try:
            send(Path(local), remote)
        finally:
            sftp.close()
            ssh.close()

    def send_file(self, local, remote):
        ssh, sftp = self._sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()
            ssh.close()

    def close(self):
        try:
            if self._ssh is not None:
                self._ssh.close()
        except Exception:
            pass
