"""Privileged worker code is immutable and checked before command dispatch."""
import json
import hashlib
from pathlib import Path

import pytest

from control_entry import verify_control_tree
from scripts.install_worker_control import install_control
from orchestrator.criu_handlers import WRAPPER_PATH
from orchestrator.worker_bootstrap import control_userdata


def test_installed_control_tree_is_pinned_and_write_once(tmp_path):
    destination = tmp_path / "control"
    script = tmp_path / "approved.py"
    script.write_text("print('approved')\n")
    digest = install_control(destination, require_root=False, allowed_scripts=[script])
    assert verify_control_tree(destination, require_root=False) == digest
    assert (destination / "worker" / "control_cli.py").is_file()
    assert (destination / "checkpoint" / "criu_wrapper.sh").is_file()
    bootstrap = (destination / "scripts" / "bootstrap_worker_security.sh").read_text()
    assert "--uid 62000 --gid 62000" in bootstrap
    assert "iptables -I OUTPUT 1 -m owner --uid-owner 62000 -j REJECT" in bootstrap
    assert "ip6tables -I OUTPUT 1 -m owner --uid-owner 62000 -j REJECT" in bootstrap
    assert "control_entry.py spot-monitor" in bootstrap
    assert json.loads((destination / "allowed-workloads.json").read_text())[
        "allowed_sha256"] == ["sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()]
    assert WRAPPER_PATH == "/opt/spot-worker-control/checkpoint/criu_wrapper.sh"
    assert "--self-check" in control_userdata(0)
    assert "bootstrap_worker_security.sh" in control_userdata(0)
    with pytest.raises(FileExistsError):
        install_control(destination, require_root=False)


def test_mutated_control_file_fails_integrity_check(tmp_path):
    destination = tmp_path / "control"
    install_control(destination, require_root=False)
    (destination / "worker" / "controller.py").write_text("tampered")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        verify_control_tree(destination, require_root=False)


def test_control_manifest_rejects_path_traversal(tmp_path):
    destination = tmp_path / "control"
    install_control(destination, require_root=False)
    manifest = destination / "manifest.json"
    document = json.loads(manifest.read_text())
    document["files"]["../escape.py"] = "0" * 64
    document["digest"] = "sha256:" + hashlib.sha256(json.dumps(
        document["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest.write_text(json.dumps(document))
    with pytest.raises(RuntimeError, match="escapes"):
        verify_control_tree(destination, require_root=False)
