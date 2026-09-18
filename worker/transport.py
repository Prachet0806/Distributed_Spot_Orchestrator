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
        result = client.run_command(command, check=False)
        return {"returncode": getattr(result, "returncode", 0),
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", "")}

    def close(self):
        try:
            if self._ssh is not None:
                self._ssh.close()
        except Exception:
            pass
