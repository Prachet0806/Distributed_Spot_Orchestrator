"""CRIU needs the source process's file ownership and modes after transfer."""
import os
from pathlib import Path
import stat
import subprocess

import pytest


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="ownership restoration requires root on Linux")
def test_restore_recreates_spotjob_progress_file_ownership(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    images = tmp_path / "images"
    appstate = images / "appstate"
    appstate.mkdir(parents=True)
    (appstate / "progress.json").write_text('{"tick":1}')
    (appstate / "ownership.tsv").write_text("progress.json 62000 62000 640\n")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    criu = fakebin / "criu"
    criu.write_text("#!/bin/sh\nexit 1\n")
    criu.chmod(0o755)
    env = {**os.environ, "WORKSPACE_ROOT": str(workspace),
           "PATH": str(fakebin) + os.pathsep + os.environ["PATH"]}
    wrapper = Path(__file__).resolve().parents[1] / "checkpoint" / "criu_wrapper.sh"
    result = subprocess.run(["bash", str(wrapper), "restore", str(images)],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1  # Fake CRIU fails after appstate restoration.
    restored = workspace / "progress.json"
    assert restored.read_text() == '{"tick":1}'
    assert (restored.stat().st_uid, restored.stat().st_gid) == (62000, 62000)
    assert stat.S_IMODE(restored.stat().st_mode) == 0o640
