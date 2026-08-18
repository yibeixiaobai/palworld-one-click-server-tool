from pathlib import Path

import pytest

import hashlib
import json
import sys
import types

from palworld_console.save_codec import HELPER_API_VERSION, PLM_HELPER, SAVE_PATCH_FORMAT, PlmCodecPlugin, PluginParsedSave


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
        "invariants": {"player_uids": ["42"], "pal_count": 1, "inventory_count": 0, "pal_instance_ids": [], "unchanged_pal_fields": {}},
    }


def test_patch_manifest_uses_stable_pal_instance_id_and_preserves_other_pals(tmp_path: Path):
    payload = {"players": [{"player_uid": "42", "pals": [
        {"individual_id": "pal-a", "stable_id_valid": True, "melee": 12, "level": 3},
        {"individual_id": "pal-b", "stable_id_valid": True, "melee": 38, "level": 3},
    ]}]}
    document = PluginParsedSave.create(payload, tmp_path / "Level.sav", FakePlugin())
    document.properties["players"][0]["pals"][0]["melee"] = 38
    patch = document.patch_manifest()
    assert patch["pals"] == [{"individual_id": "pal-a", "owner_uid": "42", "fields": {"melee": 38}}]
    assert patch["invariants"]["pal_instance_ids"] == ["pal-a", "pal-b"]
    assert patch["invariants"]["unchanged_pal_fields"]["pal-b"]["melee"] == 38


def test_cached_helper_contract_is_upgraded_without_rebuilding_native_plugin(tmp_path: Path):
    plugin = PlmCodecPlugin(tmp_path)
    plugin.root.mkdir(parents=True)
    plugin.helper.write_text("legacy helper", encoding="utf-8")
    plugin.manifest.write_text(json.dumps({"source_commit": "legacy"}), encoding="utf-8")
    manifest = json.loads(plugin.manifest.read_text(encoding="utf-8"))
    plugin._ensure_helper_contract(manifest)
    upgraded = json.loads(plugin.manifest.read_text(encoding="utf-8"))
    assert plugin.helper.read_text(encoding="utf-8") == PLM_HELPER
    assert upgraded["helper_api_version"] == HELPER_API_VERSION
    assert upgraded["patch_format"] == SAVE_PATCH_FORMAT
    assert upgraded["helper_sha256"] == hashlib.sha256(PLM_HELPER.encode("utf-8")).hexdigest()


def test_plugin_roundtrip_verification_detects_mismatch(tmp_path: Path):
    plugin = PlmCodecPlugin(tmp_path)
    plugin.decode = lambda _path: {"players": [{"player_uid": "42", "level": 8}]}
    with pytest.raises(RuntimeError, match="字段验证失败"):
        plugin.verify_roundtrip(tmp_path / "Level.sav", {"format": SAVE_PATCH_FORMAT, "players": [{"player_uid": "42", "fields": {"level": 9}}]})


def test_plugin_roundtrip_rejects_legacy_operations_patch(tmp_path: Path):
    plugin = PlmCodecPlugin(tmp_path)
    plugin.decode = lambda _path: {"players": []}
    with pytest.raises(RuntimeError, match="仅支持"):
        plugin.verify_roundtrip(tmp_path / "Level.sav", {"operations": []})


def test_helper_decodes_complete_pal_guild_and_base_relationships(monkeypatch):
    core = types.ModuleType("palsav.core"); core.decompress_sav_to_gvas = lambda _data: (b"", 49); core.compress_gvas_to_sav = lambda data, _save_type: data
    gvas = types.ModuleType("palsav.gvas"); gvas.GvasFile = object
    paltypes = types.ModuleType("palsav.paltypes"); paltypes.PALWORLD_TYPE_HINTS = {}; paltypes.PALWORLD_CUSTOM_PROPERTIES = {}
    package = types.ModuleType("palsav")
    monkeypatch.setitem(sys.modules, "palsav", package); monkeypatch.setitem(sys.modules, "palsav.core", core); monkeypatch.setitem(sys.modules, "palsav.gvas", gvas); monkeypatch.setitem(sys.modules, "palsav.paltypes", paltypes)
    namespace = {"__name__": "plm_helper_test"}; exec(compile(PLM_HELPER, "plm_helper.py", "exec"), namespace)
    player_uid = "0000002a-0000-0000-0000-000000000000"
    pal_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    world = {
        "CharacterSaveParameterMap": {"value": [
            {"key": {"PlayerUId": {"value": player_uid}}, "sp": {"IsPlayer": {"value": True}, "NickName": {"value": "Alice"}, "Level": {"value": {"value": 20}}, "Exp": {"value": 3000}}},
            {"key": {"InstanceId": {"value": pal_id}}, "sp": {"OwnerPlayerUId": {"value": player_uid}, "CharacterID": {"value": "SheepBall"}, "NickName": {"value": "Cotton"}, "Level": {"value": {"value": 10}}, "Exp": {"value": 500}, "Gender": {"value": {"value": "EPalGenderType::Female"}}, "PassiveSkillList": {"value": {"values": ["Lucky"]}}, "EquipWaza": {"value": {"values": ["FireBall"]}}, "MasteredWaza": {"value": {"values": ["WindCutter"]}}}},
        ]},
        "CharacterContainerSaveData": {"value": [{"key": {"ID": {"value": "worker-container"}}, "value": {"Slots": {"value": {"values": [{"RawData": {"value": {"instance_id": pal_id}}}]}}}}]},
        "GroupSaveDataMap": {"value": [{"value": {"GroupType": {"value": "EPalGroupType::Guild"}, "RawData": {"value": {"group_id": "guild-1", "guild_name": "Builders", "base_camp_level": 8, "admin_player_uid": player_uid, "players": [{"player_uid": player_uid, "player_info": {"player_name": "Alice"}}]}}}}]},
        "BaseCampSaveData": {"value": [{"key": "base-1", "value": {"RawData": {"value": {"name": "主基地", "group_id_belong_to": "guild-1", "transform": {"translation": {"x": 1, "y": 2, "z": 3}}}}, "WorkerDirector": {"value": {"RawData": {"value": {"container_id": "worker-container"}}}}, "ModuleMap": {"storage_container_id": "items-1"}}}]},
        "ItemContainerSaveData": {"value": []},
    }
    namespace["load"] = lambda _path: (None, 49, world)
    namespace["save_parameter"] = lambda entry: entry["sp"]
    namespace["player_items"] = lambda *_args: ({key: [] for key in namespace["PLAYER_CONTAINER_KEYS"]}, "complete", "")
    decoded = namespace["decode"]("Level.sav")
    pal = decoded["players"][0]["pals"][0]
    assert pal["passive_skills"] == ["Lucky"]
    assert pal["active_skills"] == ["FireBall"]
    assert decoded["guilds"][0]["base_ids"] == ["base-1"]
    assert decoded["bases"][0]["worker_pal_ids"] == [pal_id]
    assert decoded["bases"][0]["container_ids"] == ["items-1"]
