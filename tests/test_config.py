from rename import config


def test_structured_naming_defaults_off(tmp_path):
    cfg = config.load(tmp_path / "missing.toml")

    assert cfg.structured_naming.mode == "off"
    assert cfg.structured_naming.enabled is False
    assert cfg.structured_naming.applies is False
    assert cfg.codex_home is None


def test_structured_naming_loads_effective_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '''
tools = []

[structured_naming]
mode = "apply"
model = "gpt-test"
reasoning_effort = "medium"
modules = ["Confidence", "Workflow"]
confidence_threshold = 0.9
idle_seconds = 12
max_messages = 9
max_input_chars = 5000
max_output_bytes = 2000
timeout_seconds = 22

[codex]
home = "D:/Codex Data"
''',
        "utf-8",
    )

    cfg = config.load(path)

    assert cfg.tools == ()
    assert cfg.codex_home == "D:/Codex Data"
    assert cfg.structured_naming.mode == "apply"
    assert cfg.structured_naming.model == "gpt-test"
    assert cfg.structured_naming.reasoning_effort == "medium"
    assert cfg.structured_naming.modules == ("Confidence", "Workflow")
    assert cfg.structured_naming.confidence_threshold == 0.9
    assert cfg.structured_naming.idle_seconds == 12
    assert cfg.structured_naming.max_messages == 9
    assert cfg.structured_naming.max_input_chars == 5000
    assert cfg.structured_naming.max_output_bytes == 2000
    assert cfg.structured_naming.timeout_seconds == 22


def test_invalid_structured_mode_fails_closed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[structured_naming]\nmode = "surprise"\n', "utf-8")

    cfg = config.load(path)

    assert cfg.structured_naming.mode == "off"


def test_default_toml_is_loadable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config.DEFAULT_TOML, "utf-8")

    cfg = config.load(path)

    assert cfg.structured_naming.mode == "off"
    assert cfg.structured_naming.model == "gpt-5.6-terra"
