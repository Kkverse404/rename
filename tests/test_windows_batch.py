from __future__ import annotations

import os
import subprocess

import pytest

from rename.windows_batch import UnsafeBatchArgument, batch_command


@pytest.mark.skipif(os.name != "nt", reason="requires real cmd.exe parsing")
def test_batch_command_executes_unicode_space_ampersand_path(tmp_path):
    folder = tmp_path / "中文 & tools"
    folder.mkdir()
    script = folder / "echo-args.cmd"
    script.write_text("@echo off\necho ARG1=[%~1] ARG2=[%~2]\n", encoding="ascii")

    command = batch_command(os.environ["COMSPEC"], str(script), "app-server", "--stdio")
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "ARG1=[app-server] ARG2=[--stdio]" in result.stdout


def test_batch_command_rejects_percent_expansion_in_path_or_argument():
    with pytest.raises(UnsafeBatchArgument, match="unsupported '%'"):
        batch_command("cmd.exe", r"D:\%TEMP%\codex.cmd", "exec")
    with pytest.raises(UnsafeBatchArgument, match="unsupported '%'"):
        batch_command("cmd.exe", r"D:\codex.cmd", "model=%MODEL%")
