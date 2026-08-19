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


def test_patch_manifest_supports_deep_pal_fields_by_stable_instance_id(tmp_path: Path):
    pal = {"individual_id": "pal-a", "stable_id_valid": True, "is_lucky": False, "rank_attack": 1, "rank_defence": 2, "rank_craftspeed": 3, "skills": ["Lucky"], "active_skills": ["FireBall"], "learned_skills": ["WindCutter"]}
    document = PluginParsedSave.create({"players": [{"player_uid": "42", "pals": [pal]}]}, tmp_path / "Level.sav", FakePlugin())
    changed = document.properties["players"][0]["pals"][0]
    changed["is_lucky"] = True; changed["rank_attack"] = 20; changed["active_skills"] = ["FireBall", "DragonCannon"]

    patch = document.patch_manifest()

    assert patch["pals"] == [{"individual_id": "pal-a", "owner_uid": "42", "fields": {"is_lucky": True, "active_skills": ["FireBall", "DragonCannon"], "rank_attack": 20}}]


def test_patch_manifest_emits_guild_and_base_operations_by_stable_id(tmp_path: Path):
    payload = {
        "players": [],
        "guilds": [{"guild_id": "guild-1", "name": "Old Guild", "base_camp_level": 3}],
        "bases": [{"base_id": "base-1", "name": "Old Base", "position": {"x": 1.0, "y": 2.0, "z": 3.0}}],
    }
    document = PluginParsedSave.create(payload, tmp_path / "Level.sav", FakePlugin())
    document.properties["guilds"][0]["name"] = "New Guild"
    document.properties["guilds"][0]["base_camp_level"] = 8
    document.properties["bases"][0]["name"] = "New Base"
    document.properties["bases"][0]["position"]["x"] = 100.5

    patch = document.patch_manifest()

    assert patch["guilds"] == [{"guild_id": "guild-1", "fields": {"name": "New Guild", "base_camp_level": 8}}]
    assert patch["bases"] == [{"base_id": "base-1", "fields": {"name": "New Base", "position": {"x": 100.5}}}]


def test_roundtrip_verifies_guild_and_base_fields(tmp_path: Path):
    plugin = PlmCodecPlugin(tmp_path)
    plugin.decode = lambda _path: {
        "players": [],
        "guilds": [{"guild_id": "guild-1", "name": "Builders", "base_camp_level": 6}],
        "bases": [{"base_id": "base-1", "name": "North", "position": {"x": 12.5, "y": 20, "z": -4}}],
    }
    patch = {
        "format": SAVE_PATCH_FORMAT, "players": [], "pals": [], "inventory": [],
        "guilds": [{"guild_id": "guild-1", "fields": {"name": "Builders", "base_camp_level": 6}}],
        "bases": [{"base_id": "base-1", "fields": {"name": "North", "position": {"x": 12.5, "z": -4.0}}}],
        "invariants": {},
    }

    assert plugin.verify_roundtrip(tmp_path / "Level.sav", patch)["bases"][0]["base_id"] == "base-1"
    plugin.decode = lambda _path: {"players": [], "guilds": [{"guild_id": "guild-1", "name": "Wrong"}], "bases": []}
    with pytest.raises(RuntimeError, match="公会字段验证失败"):
        plugin.verify_roundtrip(tmp_path / "Level.sav", patch)


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


def test_plugin_exposes_versioned_convert_and_steam_uid_commands(tmp_path: Path, monkeypatch):
    plugin = PlmCodecPlugin(tmp_path)
    calls = []
    def run(args, timeout=600):
        calls.append((args, timeout))
        if args[0] == "convert": return json.dumps({"mode": "sav-to-json", "output": args[-1]})
        if args[0] == "restore-map": return json.dumps({"mask_textures": 1, "hidden_locations": 2, "output": args[-1]})
        if args[0] == "expand-palbox": return json.dumps({"old_slots": 480, "new_slots": 960, "output": args[-1]})
        return json.dumps({"steam_id": "76561198000000000", "palworld_uid": "ABCD", "nosteam_uid": "1234"})
    monkeypatch.setattr(plugin, "_run", run)

    converted = plugin.convert_file(tmp_path / "Level.sav", tmp_path / "Level.json")
    uid = plugin.steam_id_to_uid("76561198000000000")
    restored = plugin.restore_map(tmp_path / "LocalData.sav", tmp_path / "LocalData-new.sav")
    expanded = plugin.expand_palbox(tmp_path / "world", "A" * 32, 960, tmp_path / "expanded")

    assert converted["mode"] == "sav-to-json"
    assert calls[0][0][0] == "convert"
    assert calls[1][0] == ["steam-uid", "--steam-id", "76561198000000000"]
    assert uid["palworld_uid"] == "ABCD"
    assert calls[2][0][0] == "restore-map"
    assert restored["mask_textures"] == 1
    assert calls[3][0][0] == "expand-palbox"
    assert expanded["new_slots"] == 960


def test_plugin_decode_players_uses_lightweight_helper_command(tmp_path: Path, monkeypatch):
    plugin = PlmCodecPlugin(tmp_path); calls = []
    def run(args, timeout=600):
        calls.append((args, timeout))
        output = Path(args[args.index("--output") + 1])
        output.write_text(json.dumps({"players": [{"player_guid": "0" * 31 + "1", "instance_id": "host"}]}), encoding="utf-8")
        return ""
    monkeypatch.setattr(plugin, "_run", run)

    result = plugin.decode_players(tmp_path / "Level.sav")

    assert result["players"][0]["instance_id"] == "host"
    assert calls[0][0][0] == "decode-players"
    assert calls[0][1] == 240


def test_plugin_run_reports_helper_timeout(tmp_path: Path, monkeypatch):
    plugin = PlmCodecPlugin(tmp_path)
    monkeypatch.setattr(plugin, "probe", lambda: (True, "ready"))
    def timeout(*_args, **_kwargs): raise __import__("subprocess").TimeoutExpired("helper", 3)
    monkeypatch.setattr("palworld_console.save_codec.subprocess.run", timeout)
    with pytest.raises(RuntimeError, match="执行超时.*decode-players"):
        plugin._run(["decode-players"], timeout=3)


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


def test_helper_lightweight_player_decode_merges_level_and_0001_player_file(tmp_path: Path, monkeypatch):
    core = types.ModuleType("palsav.core"); core.decompress_sav_to_gvas = lambda _data: (b"", 49); core.compress_gvas_to_sav = lambda data, _save_type: data
    gvas = types.ModuleType("palsav.gvas"); gvas.GvasFile = object
    paltypes = types.ModuleType("palsav.paltypes"); paltypes.PALWORLD_TYPE_HINTS = {}; paltypes.PALWORLD_CUSTOM_PROPERTIES = {}
    monkeypatch.setitem(sys.modules, "palsav", types.ModuleType("palsav")); monkeypatch.setitem(sys.modules, "palsav.core", core); monkeypatch.setitem(sys.modules, "palsav.gvas", gvas); monkeypatch.setitem(sys.modules, "palsav.paltypes", paltypes)
    namespace = {"__name__": "plm_helper_players_test"}; exec(compile(PLM_HELPER, "plm_helper.py", "exec"), namespace)
    host_guid = "00000001-0000-0000-0000-000000000000"; host_file = "00000001000000000000000000000000"
    world = {"CharacterSaveParameterMap": {"value": [{"key": {"PlayerUId": {"value": host_guid}, "InstanceId": {"value": ""}}, "sp": {"IsPlayer": {"value": True}, "NickName": {"value": "Host"}, "Level": {"value": {"value": 20}}}}]}}
    level = tmp_path / "Level.sav"; players = tmp_path / "Players"; players.mkdir(); level.write_bytes(b"level"); (players / f"{host_file}.sav").write_bytes(b"player")
    player_properties = {"SaveData": {"value": {"PlayerUId": {"value": host_guid}, "IndividualId": {"value": {"InstanceId": {"value": "host-instance"}}}}}}
    namespace["load"] = lambda _path: (None, 49, world)
    namespace["save_parameter"] = lambda entry: entry["sp"]
    namespace["read_gvas"] = lambda _path: (types.SimpleNamespace(properties=player_properties), 49)

    decoded = namespace["decode_players"](level)

    assert decoded["format"] == "PlM1-players-v1"
    assert decoded["players"][0]["player_guid"] == host_file
    assert decoded["players"][0]["instance_id"] == "host-instance"
    assert decoded["players"][0]["nickname"] == "Host"
    assert decoded["warnings"] == []


def test_identity_migration_removes_placeholder_before_rebinding_guild_and_pals(tmp_path: Path, monkeypatch):
    core = types.ModuleType("palsav.core"); core.decompress_sav_to_gvas = lambda _data: (b"", 49); core.compress_gvas_to_sav = lambda data, _save_type: data
    gvas = types.ModuleType("palsav.gvas"); gvas.GvasFile = object
    paltypes = types.ModuleType("palsav.paltypes"); paltypes.PALWORLD_TYPE_HINTS = {}; paltypes.PALWORLD_CUSTOM_PROPERTIES = {}
    monkeypatch.setitem(sys.modules, "palsav", types.ModuleType("palsav")); monkeypatch.setitem(sys.modules, "palsav.core", core); monkeypatch.setitem(sys.modules, "palsav.gvas", gvas); monkeypatch.setitem(sys.modules, "palsav.paltypes", paltypes)
    namespace = {"__name__": "plm_helper_migration_test"}; exec(compile(PLM_HELPER, "plm_helper.py", "exec"), namespace)
    old_clean = "a" * 32; new_clean = "b" * 32
    old_uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"; new_uid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    old_instance = "old-instance"; new_instance = "new-instance"
    world = {
        "CharacterSaveParameterMap": {"value": [
            {"key": {"PlayerUId": {"value": old_uid}, "InstanceId": {"value": old_instance}}, "sp": {"IsPlayer": {"value": True}}},
            {"key": {"PlayerUId": {"value": new_uid}, "InstanceId": {"value": new_instance}}, "sp": {"IsPlayer": {"value": True}}},
            {"key": {"InstanceId": {"value": "old-pal"}}, "sp": {"OwnerPlayerUId": {"value": old_uid}}},
            {"key": {"InstanceId": {"value": "temp-pal"}}, "sp": {"OwnerPlayerUId": {"value": new_uid}}},
        ]},
        "GroupSaveDataMap": {"value": [{"value": {"GroupType": {"value": "Guild"}, "RawData": {"value": {
            "individual_character_handle_ids": [{"instance_id": old_instance, "guid": old_uid}, {"instance_id": new_instance, "guid": new_uid}],
            "admin_player_uid": old_uid, "players": [{"player_uid": old_uid}, {"player_uid": new_uid}],
        }}}}]},
    }
    class FakeGvas:
        def __init__(self, properties=None): self.properties = properties or {}
        def write(self, _types): return b"encoded"
    player = FakeGvas({"SaveData": {"value": {
        "PlayerUId": {"value": old_uid},
        "IndividualId": {"value": {"PlayerUId": {"value": old_uid}, "InstanceId": {"value": old_instance}}},
    }}})
    source = tmp_path / "world"; (source / "Players").mkdir(parents=True)
    (source / "Level.sav").write_bytes(b"level"); (source / "Players" / f"{old_clean.upper()}.sav").write_bytes(b"old"); (source / "Players" / f"{new_clean.upper()}.sav").write_bytes(b"new")
    mapping = tmp_path / "mapping.json"; mapping.write_text(json.dumps({"format": "palworld-console-identity-migration-v1", "mappings": [{"old_guid": old_clean, "new_guid": new_clean, "old_instance_id": old_instance, "new_instance_id": new_instance}]}), encoding="utf-8")
    namespace["load"] = lambda _path: (FakeGvas(), 49, world)
    namespace["read_gvas"] = lambda _path: (player, 49)
    namespace["save_parameter"] = lambda entry: entry["sp"]
    namespace["enum_value"] = lambda value: "EPalGroupType::Guild" if value else ""
    namespace["decode"] = lambda _path: {"players": [{"player_guid": new_clean.upper()}]}

    report = namespace["migrate_identities"](source, mapping, tmp_path / "output")

    entries = world["CharacterSaveParameterMap"]["value"]
    assert {entry["key"]["InstanceId"]["value"] for entry in entries} == {old_instance, "old-pal"}
    assert entries[0]["key"]["PlayerUId"]["value"] == new_uid
    assert entries[1]["sp"]["OwnerPlayerUId"]["value"] == new_uid
    guild = world["GroupSaveDataMap"]["value"][0]["value"]["RawData"]["value"]
    assert guild["admin_player_uid"] == new_uid
    assert guild["players"] == [{"player_uid": new_uid}]
    assert guild["individual_character_handle_ids"] == [{"instance_id": old_instance, "guid": new_uid}]
    assert report["players"][0]["placeholder_hits"] == 1
