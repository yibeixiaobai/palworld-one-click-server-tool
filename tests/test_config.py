from pathlib import Path
from palworld_console.config_ini import PalWorldSettings, RawValue, coerce_setting_value
from palworld_console.models import ServerInstance
from palworld_console.services import RemoteServerInspector


def test_option_settings_roundtrip_preserves_unknown(tmp_path: Path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(';keep this comment\nOptionSettings=(ServerName="A, B",UnknownFlag=True,ExpRate=2.5);\n;keep tail\n', encoding="utf-8")
    settings = PalWorldSettings.load(path)
    assert settings.values["ServerName"] == "A, B"
    assert settings.values["UnknownFlag"] is True
    settings.values["ExpRate"] = 3
    settings.save(path)
    text = path.read_text(encoding="utf-8")
    assert 'ServerName="A, B"' in text and "UnknownFlag=True" in text and "ExpRate=3" in text and ";keep tail" in text


def test_invalid_config_rejected(tmp_path: Path):
    path = tmp_path / "bad.ini"
    path.write_text("ServerName=bad", encoding="utf-8")
    try:
        PalWorldSettings.load(path)
    except ValueError:
        return
    assert False, "invalid config should raise"


def test_old_instance_migrates_with_remote_discovery_defaults():
    instance = ServerInstance.from_dict({"name": "old", "kind": "remote", "host": "example.com"})
    assert instance.discovery_status == "not_checked"
    assert instance.remote_profile == {}


def test_remote_settings_parser_handles_quoted_and_unquoted_values():
    config = 'OptionSettings=(PublicPort=8211,RESTAPIEnabled=True,RESTAPIPort="8212")'
    assert RemoteServerInspector._setting(config, "PublicPort") == "8211"
    assert RemoteServerInspector._setting(config, "RESTAPIPort") == "8212"


def test_nested_crossplay_value_roundtrips_without_quotes(tmp_path: Path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text('OptionSettings=(CrossplayPlatforms=(Steam,Xbox,PS5,Mac),RESTAPIEnabled=True);', encoding="utf-8")
    settings = PalWorldSettings.load(path)
    assert settings.values["CrossplayPlatforms"] == RawValue("(Steam,Xbox,PS5,Mac)")
    settings.save(path)
    assert "CrossplayPlatforms=(Steam,Xbox,PS5,Mac)" in path.read_text(encoding="utf-8")


def test_gui_setting_coercion_preserves_types():
    assert coerce_setting_value("RESTAPIEnabled", "true") is True
    assert coerce_setting_value("ServerPlayerMaxNum", "32") == 32
    assert coerce_setting_value("ExpRate", "2.5") == 2.5
    assert coerce_setting_value("CrossplayPlatforms", "(Steam,Xbox)") == RawValue("(Steam,Xbox)")
