"""The example config is what a new machine starts from, so it must load as shipped."""

from pathlib import Path

from config_loader import PROJECT_ROOT, load_config


def test_example_config_loads_without_credentials() -> None:
    """A setting added to the loader without adding it here fails this test."""
    config = load_config(PROJECT_ROOT / "config" / "config.example.yaml", with_credentials=False)

    assert config["source_folder"] == "deleteditems"
    assert config["stac"]["is_test_instance"] is True
    assert config["alerts"]["enabled"] is False


def test_example_config_is_safe_to_run_as_shipped() -> None:
    """Copied without edits, it reaches nothing live and writes nothing."""
    config = load_config(Path(PROJECT_ROOT / "config" / "config.example.yaml"), with_credentials=False)

    assert config["stac"]["upload_enabled"] is False
    assert config["stac"]["save_enabled"] is False
    assert config["mailbox_actions"]["tag_enabled"] is False
    assert config["mailbox_actions"]["move_enabled"] is False
