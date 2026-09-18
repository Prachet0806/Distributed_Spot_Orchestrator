"""Track D2/D5: SSH trust — pinned known-hosts, TOFU rules, emu-trust guard.

Vehicles: I15-adjacent security evidence; C-WORKER transport conformance.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "scripts")
import deploy_worker
from orchestrator.utils import SSHClient
from worker.transport import SSHWorkerTransport, emu_trust_mode


def _key(tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("dummy")
    return str(key)


# -- D2: pinning --
def test_pin_host_key_appends_and_fails_closed(tmp_path):
    known = str(tmp_path / "kh" / "known_hosts")
    scanned = "1.2.3.4 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBm=\n"
    with patch("subprocess.run") as run:
        run.side_effect = [
            MagicMock(stdout=scanned, stderr=""),   # ssh-keyscan
            MagicMock(stdout="1.2.3.4 (ED25519) SHA256:abc", stderr=""),  # keygen
        ]
        entries = deploy_worker.pin_host_key("1.2.3.4", known)
    assert len(entries) == 1
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIBm=" in open(known).read()
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(stdout="# commented\n", stderr="refused")
        with pytest.raises(RuntimeError, match="no host keys"):
            deploy_worker.pin_host_key("9.9.9.9", known)


def test_create_ssh_client_defaults_to_reject_policy(tmp_path):
    with patch("paramiko.SSHClient") as cls:
        inst = cls.return_value
        deploy_worker.create_ssh_client("1.2.3.4", _key(tmp_path))
        inst.set_missing_host_key_policy.assert_called_once()
        policy = inst.set_missing_host_key_policy.call_args[0][0]
        import paramiko
        assert isinstance(policy, paramiko.RejectPolicy)


def test_create_ssh_client_tofu_is_loud_and_explicit(tmp_path):
    with patch("paramiko.SSHClient") as cls:
        inst = cls.return_value
        deploy_worker.create_ssh_client("1.2.3.4", _key(tmp_path),
                                        allow_tofu=True)
        import paramiko
        policy = inst.set_missing_host_key_policy.call_args[0][0]
        assert isinstance(policy, paramiko.WarningPolicy)


def test_deploy_refuses_silent_tofu(tmp_path):
    with pytest.raises(SystemExit, match="silent first-connect TOFU"):
        deploy_worker.deploy("1.2.3.4", _key(tmp_path), ".")
    with pytest.raises(SystemExit, match="--pin requires --known-hosts"):
        deploy_worker.deploy("1.2.3.4", _key(tmp_path), ".", pin=True)


# -- D2: orchestrator SSHClient strict mode --
def test_ssh_client_strict_requires_pinned_file(tmp_path):
    key = _key(tmp_path)
    missing = str(tmp_path / "nope" / "known_hosts")
    with pytest.raises(RuntimeError, match="Refusing first-connect TOFU"):
        SSHClient(host="1.2.3.4", key_path=key,
                  known_hosts_path=missing, strict=True).run_command("echo hi")
    # Non-strict default preserved (warn + accept-new) for legacy callers.
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        out = SSHClient(host="1.2.3.4", key_path=key,
                        known_hosts_path=missing).run_command("echo hi")
        assert out.stdout == "ok"
        assert "accept-new" in " ".join(str(a) for a in run.call_args[0][0])


def test_transport_passes_strict_pinning_to_client(monkeypatch):
    monkeypatch.delenv("EMU_TRUST_MODE", raising=False)
    created = {}

    class _FakeSSH:
        def __init__(self, host, **kw):
            created.update(kw)

        def connect(self):
            return None

    with patch("orchestrator.utils.SSHClient", _FakeSSH):
        t = SSHWorkerTransport("h", known_hosts_path="/k/h",
                               strict_host_keys=True)
        t._client()
    assert created == {"known_hosts_path": "/k/h", "strict": True}
    assert SSHWorkerTransport("h").emu_trust is False


# -- D5: emu trust never rides into AWS deploys --
def test_deploy_main_aborts_when_emu_trust_set(monkeypatch, tmp_path):
    monkeypatch.setenv("EMU_TRUST_MODE", "true")
    assert emu_trust_mode() is True
    with pytest.raises(SystemExit, match="EMU_TRUST_MODE"):
        deploy_worker.main(["--ip", "1.2.3.4", "--key", _key(tmp_path)])
