from pathlib import Path

import pytest

from palworld_console.save_codec import PluginParsedSave


class FakePlugin:
    pass


def test_plugin_patch_manifest_only_contains_supported_changed_player_fields(tmp_path: Path):
    payload = {"players": [{"player_uid": "42", "nickname": "Before", "level": 3, "exp": 10, "pals": [{"level": 1}]}], "guilds": []}
    document = PluginParsedSave.create(payload, tmp_path / "Level.sav", FakePlugin())
    document.properties["players"][0]["level"] = 9
    document.properties["players"][0]["pals"][0]["level"] = 50
    assert document.patch_manifest() == {
        "format": "palworld-console-save-patch-v2",
        "players": [{"player_uid": "42", "fields": {"level": 9}}],
        "pals": [], "inventory": [], "guilds": [], "bases": [],
        "invariants": {"player_uids": ["42"], "pal_count": 1, "inventory_count": 0},
    }


def test_plugin_roundtrip_verification_detects_mismatch(tmp_path: Path):
    from palworld_console.save_codec import PlmCodecPlugin
    plugin = PlmCodecPlugin(tmp_path)
    plugin.decode = lambda _path: {"players": [{"player_uid": "42", "level": 8}]}
    with pytest.raises(RuntimeError, match="字段验证失败"):
        plugin.verify_roundtrip(tmp_path / "Level.sav", {"operations": [{"player_uid": "42", "fields": {"level": 9}}]})
