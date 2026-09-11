import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).parents[1] / "windows-app" / "rename_gui" / "config_store.py"
)
_SPEC = importlib.util.spec_from_file_location("rename_gui_config_store", _MODULE_PATH)
assert _SPEC and _SPEC.loader
config_store = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = config_store
_SPEC.loader.exec_module(config_store)


def test_gui_config_roundtrip_preserves_all_tools_and_unknown_sections(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '''tools = ["claude-code", "codex", "continue", "zed", "windsurf", "aider"]

[structured_naming]
mode = "apply"
modules = ["工作流"]
''',
        "utf-8",
    )
    monkeypatch.setattr(config_store, "config_path", lambda: path)

    values = config_store.load()
    config_store.save(values)
    saved = path.read_text("utf-8")

    assert values.tools == ["claude-code", "codex", "continue", "zed", "windsurf", "aider"]
    assert '[structured_naming]\nmode = "apply"\nmodules = ["工作流"]' in saved
    assert not list(tmp_path.glob("*.tmp"))


def test_gui_config_allows_disabling_every_tool(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('tools = ["codex"]\n', "utf-8")
    monkeypatch.setattr(config_store, "config_path", lambda: path)

    values = config_store.load()
    values.tools = []
    config_store.save(values)

    assert config_store.load().tools == []


def test_gui_config_escapes_windows_style_model_value(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("", "utf-8")
    monkeypatch.setattr(config_store, "config_path", lambda: path)
    values = config_store.Values(codex_model='D:\\中文 path\\model "test"')

    config_store.save(values)

    assert config_store.load().codex_model == values.codex_model


def test_gui_config_roundtrips_structured_mode_model_and_modules(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("", "utf-8")
    monkeypatch.setattr(config_store, "config_path", lambda: path)
    values = config_store.Values(
        structured_mode="apply",
        structured_model="gpt-5.6-terra",
        structured_modules=["Workflow", "置信度"],
    )

    config_store.save(values)
    loaded = config_store.load()

    assert loaded.structured_mode == "apply"
    assert loaded.structured_model == "gpt-5.6-terra"
    assert loaded.structured_modules == ["Workflow", "置信度"]


def test_gui_config_invalid_structured_mode_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[structured_naming]\nmode = "unexpected"\n', "utf-8")
    monkeypatch.setattr(config_store, "config_path", lambda: path)
    values = config_store.load()

    config_store.save(values)

    assert config_store.load().structured_mode == "off"


def test_gui_config_reads_valid_toml_comments_quotes_and_multiline_arrays(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[structured_naming]
mode = 'apply' # enabled
model = 'gpt-5.6-terra'
modules = [
  'Workflow',
  'Confidence', # allowed
]
""",
        "utf-8",
    )
    monkeypatch.setattr(config_store, "config_path", lambda: path)

    values = config_store.load()
    config_store.save(values)
    loaded = config_store.load()

    assert loaded.structured_mode == "apply"
    assert loaded.structured_model == "gpt-5.6-terra"
    assert loaded.structured_modules == ["Workflow", "Confidence"]


def test_gui_config_save_ignores_brackets_inside_array_strings_and_comments(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        '''[structured_naming]
mode = "apply"
modules = [
  "Workflow [V2]",
  "Confidence", # ] is not the array close
]
''',
        "utf-8",
    )
    monkeypatch.setattr(config_store, "config_path", lambda: path)

    values = config_store.load()
    config_store.save(values)

    assert config_store.load().structured_modules == ["Workflow [V2]", "Confidence"]
