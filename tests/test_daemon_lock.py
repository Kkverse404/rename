from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rename.daemon_lock import DaemonAlreadyRunningError, DaemonLock


def test_only_one_daemon_lock_can_hold_the_same_identity(tmp_path):
    path = tmp_path / "daemon.lock"
    first = DaemonLock(path)
    second = DaemonLock(path)

    with first:
        with pytest.raises(DaemonAlreadyRunningError):
            second.acquire()

    with second:
        pass
    if os.name == "nt":
        assert not path.exists()


def test_second_process_cannot_enter_the_same_daemon_loop(tmp_path):
    path = tmp_path / "daemon.lock"
    worker = Path(__file__).parent / "fixtures" / "hold_daemon_lock.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(DaemonAlreadyRunningError):
            DaemonLock(path).acquire()
    finally:
        process.terminate()
        process.wait(timeout=10)
