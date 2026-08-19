import json
import uuid
import zipfile
from pathlib import Path

import pytest

from palworld_console.save_tools import CONVERSION_FORMAT, SaveToolsService
from palworld_console.gamepass import discover_worlds, extract_world


def _u32(value): return int(value).to_bytes(4, "little")
def _u64(value): return int(value).to_bytes(8, "little")
def _utf16(value): return _u32(len(value)) + value.encode("utf-16-le")


def _write_xgp_world(root, save_id="A1", players=("0001",)):
    user = root / "0123456789ABCDEF_0123456789ABCDEF0123456789ABCDEF"
    user.mkdir(parents=True)
    containers = []
    payloads = {"Level": b"LEVEL", "LevelMeta": b"META", "LocalData": b"LOCAL", "WorldOption": b"OPTION"}
    payloads.update({f"Players-{uid}": f"PLAYER-{uid}".encode() for uid in players})
    for sequence, (suffix, payload) in enumerate(payloads.items(), 1):
        container_uuid = uuid.uuid4(); file_uuid = uuid.uuid4()
        directory = user / container_uuid.bytes_le.hex().upper(); directory.mkdir()
        listing = _u32(4) + _u32(1) + "Data".encode("utf-16-le") + b"\0" * 120
        listing += b"\0" * 16 + file_uuid.bytes
        (directory / f"container.{sequence}").write_bytes(listing)
        (directory / file_uuid.bytes_le.hex().upper()).write_bytes(payload)
        name = f"{save_id}-{suffix}"
        entry = _utf16(name) + _utf16(name) + _utf16("") + sequence.to_bytes(1, "little")
        entry += _u32(4) + container_uuid.bytes + _u64(116444736000000000 + sequence * 10_000_000) + _u64(0) + _u64(len(payload))
        containers.append(entry)
    index = _u32(0xE) + _u32(len(containers)) + _u32(0) + _utf16("PocketpairInc.Palworld")
    index += _u64(116444736000000000) + _u32(0) + _utf16("index") + _u64(0) + b"".join(containers)
    (user / "containers.index").write_bytes(index)
    return user


class FakeCodec:
    def __init__(self):
        self.calls = []

    def convert_file(self, source: Path, output: Path):
        self.calls.append((Path(source), Path(output)))
        if Path(source).suffix.lower() == ".sav":
            Path(output).write_text(json.dumps({"converted": Path(source).name}), encoding="utf-8")
            mode = "sav-to-json"
        else:
            Path(output).write_bytes(b"converted-sav")
            mode = "json-to-sav"
        return {"mode": mode, "source": str(source), "output": str(output), "bytes": Path(output).stat().st_size}

    def steam_id_to_uid(self, value: str):
        self.calls.append(value)
        return {"steam_id": "76561198000000000", "palworld_uid": "ABCD", "nosteam_uid": "1234"}

    def decode(self, _level: Path):
        return {
            "players": [
                {"player_uid": "42", "pals": [{"individual_id": "pal-a", "level": 90, "melee": 120}]},
                {"player_uid": "42", "pals": [{"individual_id": "pal-a", "level": 1}]},
            ],
            "guilds": [{"guild_id": "guild-empty", "players": []}],
            "bases": [{"base_id": "base-1", "guild_id": "missing", "position": {"x": 10, "y": 20}, "data_status": "partial", "read_only_reason": "worker missing"}],
        }


class IdentityCodec:
    def __init__(self, old_guid="A" * 32, new_guid="B" * 32):
        self.old_guid = old_guid; self.new_guid = new_guid; self.mappings = []

    def decode(self, level: Path):
        if "identity-" in str(level.parent):
            return {"players": [{"player_guid": self.new_guid, "instance_id": "old-instance"}]}
        return {"players": [
            {"player_guid": self.old_guid, "instance_id": "old-instance", "nickname": "Old"},
            {"player_guid": self.new_guid, "instance_id": "new-instance", "nickname": "New"},
        ]}

    def migrate_identities(self, world: Path, mappings, output: Path):
        self.mappings = mappings
        import shutil
        shutil.copytree(world, output)
        old = output / "Players" / f"{self.old_guid}.sav"; new = output / "Players" / f"{self.new_guid}.sav"
        new.write_bytes(old.read_bytes()); old.unlink()
        return {"migrated": 1, "players": mappings}


class MapCodec:
    def restore_map(self, source: Path, output: Path):
        output.write_bytes(b"restored-map")
        return {"mask_textures": 2, "hidden_locations": 8, "bytes": output.stat().st_size}

    def convert_file(self, source: Path, output: Path):
        output.write_text(json.dumps({"verified": source.name}), encoding="utf-8")
        return {"mode": "sav-to-json"}


class PalboxCodec:
    def __init__(self, guid="A" * 32): self.guid = guid
    def decode(self, _level): return {"players": [{"player_guid": self.guid, "instance_id": "instance-a"}]}
    def expand_palbox(self, world, player_guid, slots, output):
        import shutil
        shutil.copytree(world, output)
        return {"player_guid": player_guid, "container_id": "box-a", "old_slots": 480, "new_slots": slots, "used_slots": 120}


def make_world(root: Path, world_id: str = "WORLD") -> Path:
    world = root / "SaveGames" / "0" / world_id
    players = world / "Players"
    players.mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"level-data")
    (players / "00000000000000000000000000000001.sav").write_bytes(b"player-data")
    return world


def test_conversion_package_contains_manifest_and_verifies(tmp_path: Path):
    source = tmp_path / "source"
    make_world(source)
    output = tmp_path / "world.pwc-conversion"

    package = SaveToolsService(tmp_path / "storage").create_conversion_package(source, output=output)
    report = SaveToolsService(tmp_path / "storage").verify_conversion_package(output)

    assert Path(package.path) == output
    assert package.file_count == 2
    assert report["valid"] is True
    assert report["entries"] == 2
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == CONVERSION_FORMAT
        assert {entry["path"] for entry in manifest["entries"]} == {"Level.sav", "Players/00000000000000000000000000000001.sav"}


def test_conversion_package_rejects_modified_payload(tmp_path: Path):
    source = tmp_path / "source"
    make_world(source)
    output = tmp_path / "world.pwc-conversion"
    service = SaveToolsService(tmp_path / "storage")
    service.create_conversion_package(source, output=output)

    replacement = tmp_path / "modified.pwc-conversion"
    with zipfile.ZipFile(output) as original, zipfile.ZipFile(replacement, "w") as modified:
        for name in original.namelist():
            data = original.read(name)
            if name.endswith("Level.sav"):
                data = b"tampered"
            modified.writestr(name, data)

    with pytest.raises(ValueError, match="校验失败"):
        service.verify_conversion_package(replacement)


def test_plan_records_source_and_package_target(tmp_path: Path):
    source = tmp_path / "source"
    make_world(source)

    operation = SaveToolsService(tmp_path / "storage").plan("coop-to-dedicated", source)

    assert operation.operation == "coop-to-dedicated"
    assert operation.target_kind == "package"
    assert operation.source_path == str(source.resolve())


def test_conversion_package_can_be_materialized_for_redeployment(tmp_path: Path):
    source = tmp_path / "source"
    make_world(source)
    package = tmp_path / "world.pwc-conversion"
    service = SaveToolsService(tmp_path / "storage")
    service.create_conversion_package(source, output=package)

    materialized = service.materialize_source(package)

    world = materialized / "SaveGames" / "imported-world"
    assert (world / "Level.sav").read_bytes() == b"level-data"
    assert len(list((world / "Players").glob("*.sav"))) == 1


def test_conversion_package_rejects_unlisted_files(tmp_path: Path):
    source = tmp_path / "source"
    make_world(source)
    package = tmp_path / "world.pwc-conversion"
    service = SaveToolsService(tmp_path / "storage")
    service.create_conversion_package(source, output=package)
    modified = tmp_path / "extra.pwc-conversion"
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(modified, "w") as target:
        for name in original.namelist():
            target.writestr(name, original.read(name))
        target.writestr("Saved/../escape.txt", b"bad")

    with pytest.raises(ValueError, match="未登记文件"):
        service.verify_conversion_package(modified)


def test_sav_to_json_conversion_validates_candidate_and_replaces_atomically(tmp_path: Path):
    source = tmp_path / "Level.sav"; source.write_bytes(b"sav")
    output = tmp_path / "Level.json"
    codec = FakeCodec(); service = SaveToolsService(tmp_path / "storage", codec)

    report = service.convert_save_file(source, output)

    assert json.loads(output.read_text(encoding="utf-8"))["converted"] == "Level.sav"
    assert report["backup"] == ""
    assert report["sha256"]
    assert len(codec.calls) == 1


def test_json_to_sav_roundtrips_and_backs_up_existing_target(tmp_path: Path):
    source = tmp_path / "Level.json"; source.write_text("{}", encoding="utf-8")
    output = tmp_path / "Level.sav"; output.write_bytes(b"old-save")
    codec = FakeCodec(); service = SaveToolsService(tmp_path / "storage", codec)

    report = service.convert_save_file(source, output)

    assert output.read_bytes() == b"converted-sav"
    assert Path(report["backup"]).read_bytes() == b"old-save"
    assert [call[0].suffix for call in codec.calls] == [".json", ".sav"]


def test_steam_id_conversion_delegates_to_isolated_codec(tmp_path: Path):
    codec = FakeCodec(); service = SaveToolsService(tmp_path / "storage", codec)
    result = service.steam_id_to_uid("steam_76561198000000000")
    assert result["palworld_uid"] == "ABCD"
    assert codec.calls == ["steam_76561198000000000"]


def test_diagnostics_reports_duplicate_relations_and_illegal_values(tmp_path: Path):
    source = tmp_path / "world"
    make_world(source)
    report = SaveToolsService(tmp_path / "storage", FakeCodec()).diagnose(source)

    assert (report.players, report.pals, report.guilds, report.bases) == (2, 2, 1, 1)
    categories = [finding.category for finding in report.findings]
    assert categories.count("重复身份") == 2
    assert "空公会" in categories
    assert "关系缺失" in categories
    assert categories.count("非法数值") == 2


def test_gamepass_discovery_reads_world_and_players_without_mutating_wgs(tmp_path: Path):
    user = _write_xgp_world(tmp_path / "wgs", players=("0001", "ABCD"))
    before = {path.relative_to(user).as_posix(): path.read_bytes() for path in user.rglob("*") if path.is_file()}

    worlds = discover_worlds(tmp_path / "wgs")

    assert len(worlds) == 1
    assert worlds[0].save_id == "A1"
    assert worlds[0].player_count == 2
    assert "Level.sav" in worlds[0].files
    after = {path.relative_to(user).as_posix(): path.read_bytes() for path in user.rglob("*") if path.is_file()}
    assert after == before


def test_gamepass_to_steam_extracts_layout_and_preserves_existing_target(tmp_path: Path):
    user = _write_xgp_world(tmp_path / "wgs", players=("0001", "ABCD"))
    target = tmp_path / "SteamWorld"; target.mkdir(); (target / "old.txt").write_text("old")

    report = extract_world(user, "A1", target)

    assert (target / "Level.sav").read_bytes() == b"LEVEL"
    assert (target / "Players" / "0001.sav").read_bytes() == b"PLAYER-0001"
    assert (target / "Players" / "ABCD.sav").read_bytes() == b"PLAYER-ABCD"
    assert report["player_count"] == 2
    assert report["read_only_source"] is True
    assert Path(report["backup"]).joinpath("old.txt").read_text() == "old"


def test_gamepass_failed_extract_keeps_existing_target(tmp_path: Path):
    user = _write_xgp_world(tmp_path / "wgs")
    level_dir = next(path for path in user.iterdir() if path.is_dir() and any(p.read_bytes() == b"LEVEL" for p in path.iterdir() if p.is_file()))
    for path in level_dir.iterdir():
        if not path.name.startswith("container."):
            path.unlink()
    target = tmp_path / "SteamWorld"; target.mkdir(); (target / "old.txt").write_text("old")

    with pytest.raises(FileNotFoundError):
        extract_world(user, "A1", target)

    assert (target / "old.txt").read_text() == "old"
    assert not list(tmp_path.glob("SteamWorld*.bak"))


def test_save_tools_detects_gamepass_sources_from_localappdata(tmp_path: Path, monkeypatch):
    import palworld_console.save_tools as save_tools_module
    wgs = tmp_path / "wgs"
    _write_xgp_world(wgs)
    monkeypatch.setattr(save_tools_module, "default_wgs_root", lambda: wgs)

    sources = SaveToolsService(tmp_path / "storage").detect_sources()

    gamepass = next(source for source in sources if source.source_kind == "gamepass")
    assert gamepass.world_id == "A1"
    assert gamepass.save_format == "Xbox/Game Pass WGS"


def test_identity_rebind_builds_verified_candidate_and_backs_up_output(tmp_path: Path):
    old_guid = "A" * 32; new_guid = "B" * 32
    source = tmp_path / "world"; players = source / "Players"; players.mkdir(parents=True)
    (source / "Level.sav").write_bytes(b"level")
    (players / f"{old_guid}.sav").write_bytes(b"old-player"); (players / f"{new_guid}.sav").write_bytes(b"placeholder")
    destination = tmp_path / "output"; destination.mkdir(); (destination / "old.txt").write_text("protected")
    codec = IdentityCodec(old_guid, new_guid)

    report = SaveToolsService(tmp_path / "storage", codec).rebind_world_identity(source, old_guid, new_guid, destination)

    assert not (destination / "Players" / f"{old_guid}.sav").exists()
    assert (destination / "Players" / f"{new_guid}.sav").read_bytes() == b"old-player"
    assert Path(report["backup"]).joinpath("old.txt").read_text() == "protected"
    assert codec.mappings == [{"old_guid": old_guid, "new_guid": new_guid, "old_instance_id": "old-instance", "new_instance_id": "new-instance"}]


def test_identity_rebind_rejects_in_place_write(tmp_path: Path):
    old_guid = "A" * 32; new_guid = "B" * 32
    source = tmp_path / "world"; players = source / "Players"; players.mkdir(parents=True)
    (source / "Level.sav").write_bytes(b"level")
    (players / f"{old_guid}.sav").write_bytes(b"old"); (players / f"{new_guid}.sav").write_bytes(b"new")

    with pytest.raises(ValueError, match="不能原地覆盖"):
        SaveToolsService(tmp_path / "storage", IdentityCodec()).rebind_world_identity(source, old_guid, new_guid, source)


def test_map_restore_backs_up_and_atomically_replaces_local_data(tmp_path: Path):
    source = tmp_path / "LocalData.sav"; source.write_bytes(b"old-map")

    report = SaveToolsService(tmp_path / "storage", MapCodec()).restore_map_file(source)

    assert source.read_bytes() == b"restored-map"
    assert Path(report["backup"]).read_bytes() == b"old-map"
    assert report["mask_textures"] == 2
    assert report["hidden_locations"] == 8


def test_map_restore_rejects_non_local_data(tmp_path: Path):
    source = tmp_path / "Level.sav"; source.write_bytes(b"level")
    with pytest.raises(ValueError, match="仅支持 LocalData"):
        SaveToolsService(tmp_path / "storage", MapCodec()).restore_map_file(source)


def test_palbox_expansion_builds_verified_world_and_protects_existing_output(tmp_path: Path):
    guid = "A" * 32; source = tmp_path / "world"; (source / "Players").mkdir(parents=True)
    (source / "Level.sav").write_bytes(b"level"); (source / "Players" / f"{guid}.sav").write_bytes(b"player")
    destination = tmp_path / "expanded"; destination.mkdir(); (destination / "old.txt").write_text("old")

    report = SaveToolsService(tmp_path / "storage", PalboxCodec(guid)).expand_palbox_world(source, guid, 960, destination)

    assert report["new_slots"] == 960
    assert (destination / "Level.sav").read_bytes() == b"level"
    assert Path(report["backup"]).joinpath("old.txt").read_text() == "old"
