import json
import os
import subprocess

import pytest

import rename.codex_executable as codex_executable
import rename.namers.structured_codex as subject
from rename.config import StructuredNamingConfig
from rename.models import Message

READY = {
    "ready": True,
    "module": "置信度",
    "summary": "修复中文路径下的会话标题",
    "reason_code": "explicit_goal",
    "evidence_message_ids": ["user-1"],
    "confidence": 0.91,
}

UNCLEAR = {
    "ready": False,
    "module": None,
    "summary": None,
    "reason_code": "insufficient_context",
    "evidence_message_ids": [],
    "confidence": 0.2,
}


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def classifier(monkeypatch, fake_run, **kwargs):
    monkeypatch.setattr(subject.shutil, "which", lambda name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(subject.subprocess, "run", fake_run)
    return subject.StructuredCodexNamer(**kwargs)


def write_result(argv, value, *, multiline=False):
    path = argv[argv.index("--output-last-message") + 1]
    with open(path, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2 if multiline else None)


@pytest.mark.parametrize("payload", [READY, UNCLEAR])
def test_parses_ready_and_unclear_decisions(monkeypatch, payload):
    def fake_run(argv, **kwargs):
        write_result(argv, payload)
        return Proc()

    decision = classifier(monkeypatch, fake_run).classify(
        [Message("user", "请修复中文路径下的会话标题")],
        cwd=r"C:\项目\置信度",
        modules=("置信度",),
    )
    assert decision == subject.NamingDecision(**payload)


def test_reads_complete_multiline_json_file(monkeypatch):
    def fake_run(argv, **kwargs):
        write_result(argv, READY, multiline=True)
        return Proc(stdout="untrusted streaming output")

    decision = classifier(monkeypatch, fake_run).classify(
        [Message("user", "fix it")], cwd=None, modules=("Core",)
    )
    assert decision.summary == READY["summary"]


def test_rejects_code_fence_and_invalid_field_types(monkeypatch):
    outputs = [
        "```json\n" + json.dumps(READY) + "\n```",
        json.dumps({**READY, "ready": 1}),
    ]

    def fake_run(argv, **kwargs):
        path = argv[argv.index("--output-last-message") + 1]
        with open(path, "w", encoding="utf-8") as output:
            output.write(outputs.pop(0))
        return Proc()

    target = classifier(monkeypatch, fake_run)
    for _ in range(2):
        with pytest.raises(subject.ClassifierOutputError):
            target.classify([Message("user", "fix it")], cwd=None, modules=("Core",))


def test_rejects_oversized_output(monkeypatch):
    def fake_run(argv, **kwargs):
        path = argv[argv.index("--output-last-message") + 1]
        with open(path, "wb") as output:
            output.write(b"x" * 65)
        return Proc()

    target = classifier(monkeypatch, fake_run, max_response_bytes=64)
    with pytest.raises(subject.ClassifierOutputError, match="64-byte"):
        target.classify([Message("user", "fix it")], cwd=None, modules=("Core",))


def test_timeout_and_nonzero_are_errors_without_retry(monkeypatch):
    calls = []

    def timeout(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 2)

    target = classifier(monkeypatch, timeout, timeout=2)
    with pytest.raises(subject.ClassifierExecutionError, match="timed out"):
        target.classify([Message("user", "fix it")], cwd=None, modules=("Core",))
    assert len(calls) == 1

    def nonzero(argv, **kwargs):
        calls.append(argv)
        return Proc(returncode=2, stderr="unknown option --ignore-rules")

    monkeypatch.setattr(subject.subprocess, "run", nonzero)
    with pytest.raises(subject.ClassifierExecutionError, match="status 2"):
        target.classify([Message("user", "fix it")], cwd=None, modules=("Core",))
    assert len(calls) == 2


def test_command_has_all_safety_flags_and_bounded_evidence(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        schema_path = argv[argv.index("--output-schema") + 1]
        seen["schema"] = json.loads(open(schema_path, encoding="utf-8").read())
        write_result(argv, READY)
        return Proc()

    messages = [Message("user", "old-" + "x" * 1000)]
    messages.extend(Message("assistant", f"answer {i}") for i in range(30))
    messages.append(Message("user", "请修复中文路径下的会话标题"))
    target = classifier(monkeypatch, fake_run)
    target.classify(messages, cwd=r"C:\项目\置信度", modules=("置信度",))

    argv = seen["argv"]
    assert argv[0] == r"C:\Tools\codex.exe"
    assert argv[1] == "exec"
    for flag in (
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        "--output-last-message",
    ):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"
    assert argv[-1] == "-"
    prompt = seen["kwargs"]["input"]
    assert prompt.count("answer ") < 30
    assert "user-2" in prompt
    assert "置信度" in prompt
    assert "6-16 Chinese characters" in prompt
    assert "verb-object task phrase" in prompt
    assert "Never copy a conversational request as the summary" in prompt
    assert "A completed explicit request still counts as ready" in prompt
    assert "都用我们的比特浏览器测试一下" in prompt
    assert "测试比特浏览器" in prompt
    assert "Omit project/module names" in prompt
    assert prompt not in argv
    assert seen["schema"]["additionalProperties"] is False
    assert seen["schema"]["properties"]["summary"]["maxLength"] == 64
    assert seen["kwargs"]["shell"] is False
    expected_flags = 0x08000000 if os.name == "nt" else 0
    assert seen["kwargs"]["creationflags"] == expected_flags


def test_configurable_model_and_resolved_executable(monkeypatch):
    monkeypatch.setattr(subject.shutil, "which", lambda name: r"D:\Codex\codex.exe")
    target = subject.StructuredCodexNamer(model="custom-model")
    assert target.available()
    argv = target._argv("schema.json", "output.json")
    assert argv[0].casefold() == r"D:\Codex\codex.exe".casefold()
    assert argv[argv.index("--model") + 1] == "custom-model"


@pytest.mark.parametrize("suffix", [".cmd", ".BAT"])
def test_windows_batch_executable_uses_controlled_comspec_wrapper(monkeypatch, suffix):
    resolved = rf"C:\Program Files\Codex\codex{suffix}"

    def fake_which(name):
        return resolved if name == "codex" else None

    monkeypatch.setattr(subject.shutil, "which", fake_which)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    target = subject.StructuredCodexNamer()
    argv = target._argv("schema.json", "output.json")
    assert isinstance(argv, str)
    assert r"C:\Windows\System32\cmd.exe" in argv
    assert " /d /v:off /s /c " in argv
    assert resolved in argv
    assert "--ephemeral" in argv
    assert argv.endswith(' "-""')
    assert "中文 prompt & literal" not in argv


def test_available_is_false_when_executable_does_not_resolve(monkeypatch, tmp_path):
    monkeypatch.setattr(subject.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    target = subject.StructuredCodexNamer()
    assert not target.available()
    with pytest.raises(subject.ClassifierUnavailableError):
        target.classify([], cwd=None, modules=())


def test_windows_desktop_executable_resolves_without_path(monkeypatch, tmp_path):
    executable = tmp_path / "OpenAI" / "Codex" / "bin" / "build-id" / "codex.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"codex")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(subject.shutil, "which", lambda name: None)
    monkeypatch.setattr(codex_executable.sys, "platform", "win32")

    target = subject.StructuredCodexNamer()

    assert target.executable == str(executable.resolve())
    assert target.available()


def test_evaluator_classifier_uses_the_production_config_window(monkeypatch):
    monkeypatch.setattr(subject.shutil, "which", lambda name: r"C:\Tools\codex.exe")
    config = StructuredNamingConfig(
        max_messages=7,
        max_input_chars=4321,
        max_output_bytes=7654,
        timeout_seconds=17,
    )

    target = subject.classifier_from_config(config)

    assert target.max_messages == config.max_messages
    assert target.max_input_chars == config.max_input_chars
    assert target.max_response_bytes == config.max_output_bytes
    assert target.timeout == config.timeout_seconds


def test_classifier_passes_configured_codex_home_only_via_environment(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        write_result(argv, READY)
        return Proc()

    home = r"D:\中文 CodexHome"
    target = classifier(monkeypatch, fake_run, codex_home=home)
    target.classify([Message("user", "fix it")], cwd=None, modules=("Core",))

    assert seen["env"]["CODEX_HOME"] == home
    assert home not in " ".join(seen["argv"])
