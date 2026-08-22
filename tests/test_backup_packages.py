import json
import hashlib
import tarfile
import zipfile
from pathlib import Path

import pytest

from palworld_console.backup_packages import BackupPackageService, BackupRepository, RestoreTransaction
from palworld_console.config_ini import PalWorldSettings, settings_path
from palworld_console.models import CoopMigrationSession, PlayerIdentityMapping, ServerInstance
from palworld_console.services import BackupService
from palworld_console.save_codec import PluginParsedSave
from palworld_console.management import SaveGameService
from palworld_console.player_store import PlayerRepository


def make_saved(root: Path, world: str = "WORLD-A", secret: str = "source-secret") -> Path:
    saved = root / "Pal" / "Saved"
    world_dir = saved / "SaveGames" / "0" / world
    players = world_dir / "Players"
    players.mkdir(parents=True)
    (world_dir / "Level.sav").write_bytes(b"12345678PlM1-world-data")
    (players / "00000000000000000000000000000001.sav").write_bytes(b"player")
    config = saved / "Config" / "WindowsServer" / "PalWorldSettings.ini"
    config.parent.mkdir(parents=True)
    config.write_text(f'OptionSettings=(ServerName="Source",PublicPort=9000,RESTAPIPort=9001,AdminPassword="{secret}",ServerPassword="join-secret",UnknownFlag=True);', encoding="utf-8")
    return saved


def test_world_and_disaster_packages_have_manifest_hashes_and_redacted_config(tmp_path: Path):
    saved = make_saved(tmp_path / "server")
    instance = ServerInstance(id="instance-a", name="A", install_dir=str(tmp_path / "server"))
    service = BackupPackageService()

    world = service.create(instance, saved, tmp_path / "backups", "world")
    disaster = service.create(instance, saved, tmp_path / "backups", "disaster")

    world_manifest = service.validate(world)
    disaster_manifest = service.validate(disaster)
    assert world_manifest.components == ("world",)
    assert disaster_manifest.components == ("world", "config")
    assert disaster_manifest.world_id == "WORLD-A"
    assert disaster_manifest.player_count == 1
    assert disaster_manifest.save_format == "PlM1/Oodle"
    assert "AdminPassword" in disaster_manifest.redacted_fields
    raw = disaster.read_bytes()
    assert b"source-secret" not in raw
    assert b"join-secret" not in raw
    with zipfile.ZipFile(disaster) as archive:
        config = PalWorldSettings.from_text(archive.read("payload/config/PalWorldSettings.ini").decode())
        assert config.values["AdminPassword"] == ""
        assert config.values["ServerPassword"] == ""

    report = service.export_report(disaster, tmp_path / "checksums.txt")
    report_text = report.read_text(encoding="utf-8")
    assert "包 SHA-256" in report_text
    assert "payload/config/PalWorldSettings.ini" in report_text
    assert "source-secret" not in report_text


def test_export_requires_confirmation_flag_and_replaces_atomically(tmp_path: Path):
    saved = make_saved(tmp_path / "server")
    service = BackupPackageService(); package = service.create(ServerInstance(id="i"), saved, tmp_path / "packages")
    target = tmp_path / "export.pwcbackup"; target.write_bytes(b"old")
    with pytest.raises(FileExistsError): service.export(package, target)
    service.export(package, target, overwrite=True)
    assert service.validate(target).schema == "palworld-console-backup-v1"


def test_package_validation_rejects_duplicate_casefold_path(tmp_path: Path):
    package = tmp_path / "bad.pwcbackup"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("Manifest.JSON", "{}")
        archive.writestr("checksums.sha256", "")
        archive.writestr("metadata/players.json", "{}")
    with pytest.raises(ValueError, match="重复"):
        BackupPackageService().validate(package)


def test_legacy_zip_path_traversal_is_rejected(tmp_path: Path):
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape/Level.sav", b"bad")
    with pytest.raises(ValueError, match="不安全路径"):
        BackupPackageService().import_source(archive, ServerInstance(), tmp_path / "out")


def test_raw_level_import_is_marked_incomplete(tmp_path: Path):
    level = tmp_path / "Level.sav"
    level.write_bytes(b"legacy-save")
    package = BackupPackageService().import_source(level, ServerInstance(id="i"), tmp_path / "out")
    manifest = BackupPackageService().validate(package)
    assert manifest.incomplete is True
    assert manifest.player_count == 0


def test_restore_preflight_reports_missing_target_saved_directory(tmp_path: Path):
    source_saved = make_saved(tmp_path / "source", "WORLD-SOURCE")
    package = BackupPackageService().create(ServerInstance(id="source"), source_saved, tmp_path / "packages")
    target = ServerInstance(id="target", install_dir=str(tmp_path / "missing-server"))
    with pytest.raises(FileNotFoundError, match="目标服务器安装目录不存在"):
        RestoreTransaction().execute_local(package, target, BackupRepository(tmp_path / "repository", target.id), ("world",), lambda: pytest.fail("不应停止服务"), lambda: None, lambda: True)


@pytest.mark.parametrize("archive_kind", ["zip", "tar.gz", "tgz"])
def test_legacy_compressed_archives_import_to_verified_package(tmp_path: Path, archive_kind: str):
    saved = make_saved(tmp_path / "server", "WORLD-ARCHIVE")
    archive_path = tmp_path / f"upload.{archive_kind}"
    if archive_kind == "zip":
        with zipfile.ZipFile(archive_path, "w") as archive:
            for path in saved.rglob("*"):
                if path.is_file():
                    archive.write(path, Path("Saved") / path.relative_to(saved))
    else:
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(saved, arcname="Saved")
    progress = []
    package = BackupPackageService().import_source(archive_path, ServerInstance(id="imported"), tmp_path / "packages", on_progress=lambda percent, message: progress.append((percent, message)))
    manifest = BackupPackageService().validate(package)
    assert manifest.components == ("world",)
    assert manifest.world_id == "WORLD-ARCHIVE"
    assert progress[0][0] == 10
    assert progress[-1][0] == 100


def test_local_restore_replaces_world_and_preserves_target_identity_and_secrets(tmp_path: Path):
    source_saved = make_saved(tmp_path / "source", "WORLD-SOURCE", "do-not-export")
    source = ServerInstance(id="source", name="Source", install_dir=str(tmp_path / "source"))
    service = BackupPackageService()
    package = service.create(source, source_saved, tmp_path / "packages", "disaster")

    target_root = tmp_path / "target"
    target_saved = make_saved(target_root, "WORLD-TARGET", "target-secret")
    target_config = settings_path(target_root)
    current = PalWorldSettings.load(target_config)
    current.values.update({"ServerName": "Target", "PublicPort": 8211, "RESTAPIPort": 8212, "AdminPassword": "target-secret", "ServerPassword": "target-join"})
    current.save(target_config)
    target = ServerInstance(id="target", name="Target", install_dir=str(target_root))
    repository = BackupRepository(tmp_path / "repository", target.id)
    calls = []

    result = RestoreTransaction().execute_local(
        package, target, repository, ("world", "config"),
        lambda: calls.append("stop"), lambda: calls.append("start"), lambda: True,
        "credential-admin", "credential-join",
    )

    restored = PalWorldSettings.load(target_config)
    assert calls == ["stop", "start"]
    assert result.restored is True
    assert (target_saved / "SaveGames" / "0" / "WORLD-SOURCE" / "Level.sav").exists()
    assert not (target_saved / "SaveGames" / "0" / "WORLD-TARGET").exists()
    assert restored.values["ServerName"] == "Target"
    assert restored.values["PublicPort"] == 8211
    assert restored.values["AdminPassword"] == "credential-admin"
    assert restored.values["ServerPassword"] == "credential-join"
    assert repository.list()[0]["protected"] is True


def test_local_restore_health_failure_restores_original_world(tmp_path: Path):
    source_saved = make_saved(tmp_path / "source", "WORLD-NEW")
    package = BackupPackageService().create(ServerInstance(id="source", install_dir=str(tmp_path / "source")), source_saved, tmp_path / "packages", "world")
    target_root = tmp_path / "target"
    make_saved(target_root, "WORLD-OLD")
    target = ServerInstance(id="target", install_dir=str(target_root))
    repository = BackupRepository(tmp_path / "repository", target.id)
    health_calls = []

    def health():
        health_calls.append(True)
        return len(health_calls) > 1

    with pytest.raises(RuntimeError, match="已自动回滚"):
        RestoreTransaction().execute_local(package, target, repository, ("world",), lambda: None, lambda: None, health)
    assert (target_root / "Pal" / "Saved" / "SaveGames" / "0" / "WORLD-OLD" / "Level.sav").exists()
    assert not (target_root / "Pal" / "Saved" / "SaveGames" / "0" / "WORLD-NEW").exists()


def test_local_restore_point_failure_restarts_original_service(tmp_path: Path, monkeypatch):
    source_saved = make_saved(tmp_path / "source", "WORLD-NEW")
    package = BackupPackageService().create(ServerInstance(id="source", install_dir=str(tmp_path / "source")), source_saved, tmp_path / "packages", "world")
    target_root = tmp_path / "target"; make_saved(target_root, "WORLD-OLD")
    target = ServerInstance(id="target", install_dir=str(target_root))
    calls = []
    monkeypatch.setattr(BackupPackageService, "create", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("restore point failed")))

    with pytest.raises(RuntimeError, match="restore point failed"):
        RestoreTransaction().execute_local(package, target, BackupRepository(tmp_path / "repository", target.id), ("world",), lambda: calls.append("stop"), lambda: calls.append("start"), lambda: True)
    assert calls == ["stop", "start"]


def test_coop_migration_session_roundtrip_and_mapping_validation(tmp_path: Path):
    service = BackupPackageService()
    session = CoopMigrationSession(
        instance_id="server", source_path=str(tmp_path / "source"), target_world_path=str(tmp_path / "target"),
        phase="waiting_placeholders", source_players=({"player_guid": "A" * 32, "nickname": "Alice", "instance_id": "instance-a"}, {"player_guid": "C" * 32, "nickname": "Bob", "instance_id": "instance-c"}),
        baseline_player_files=("A" * 32 + ".sav", "C" * 32 + ".sav"), placeholder_players=({"player_guid": "B" * 32, "nickname": "Alice", "instance_id": "instance-b"},),
    )
    service.save_migration_session(tmp_path, session)
    loaded = service.load_migration_session(tmp_path, "server")
    assert loaded is not None and loaded.phase == "waiting_placeholders"
    with pytest.raises(ValueError, match="全部"):
        service.build_identity_mappings(loaded, {"A" * 32: "B" * 32}, tmp_path)
    loaded = CoopMigrationSession(**{**loaded.__dict__, "source_players": (loaded.source_players[0],)})
    mapped = service.build_identity_mappings(loaded, {"A" * 32: "B" * 32}, tmp_path)
    assert mapped.mappings == (PlayerIdentityMapping("A" * 32, "B" * 32, "Alice", "Alice", "instance-a", True, "instance-b", "confirmed"),)
    duplicate_session = CoopMigrationSession(**{**loaded.__dict__, "source_players": ({"player_guid": "A" * 32}, {"player_guid": "C" * 32})})
    with pytest.raises(ValueError, match="重复"):
        service.build_identity_mappings(duplicate_session, {"A" * 32: "B" * 32, "C" * 32: "B" * 32}, tmp_path)


def test_prepare_restore_migration_preserves_downloaded_target_snapshot(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); instance = ServerInstance(id="server")
    source_saved = make_saved(tmp_path / "source", "SOURCE")
    package = service.create(instance, source_saved, tmp_path / "packages")
    migration_root = tmp_path / "app" / "migrations" / instance.id / "restore"
    target_saved = make_saved(migration_root / "snapshot", "TARGET")
    marker = migration_root / "snapshot" / "download-complete.marker"; marker.write_text("ok", encoding="utf-8")
    source_player = {"player_guid": "A" * 32, "nickname": "Source", "instance_id": "source-instance"}
    target_player = {"player_guid": "B" * 32, "nickname": "Target", "instance_id": "target-instance"}
    monkeypatch.setattr("palworld_console.backup_packages.PlmCodecPlugin.probe", lambda _self: (True, "ready"))
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda world, _root=None: (source_player,) if "source-world" in str(world) else (target_player,)))
    monkeypatch.setattr(BackupPackageService, "temporary_identity_targets_from_player_center", classmethod(lambda cls, _instance, targets, _root: tuple(targets)))

    session = service.prepare_restore_migration(package, instance, target_saved, str(target_saved / "SaveGames" / "0" / "TARGET"), tmp_path / "app")

    assert marker.read_text(encoding="utf-8") == "ok"
    assert Path(session.target_snapshot_path, "Level.sav").is_file()
    assert session.source_players == (source_player,)
    assert session.placeholder_players == (target_player,)


def test_migration_player_sync_never_uses_another_instance_cache(tmp_path: Path, monkeypatch):
    repository = PlayerRepository(tmp_path / "players.db")
    try:
        repository.upsert_save_snapshot("other-server", {"players": [{"player_uid": "shared", "nickname": "Other", "level": 1}]})
    finally:
        repository.close()
    monkeypatch.setattr("palworld_console.backup_packages.PlmCodecPlugin.decode", lambda *_args: (_ for _ in ()).throw(RuntimeError("decode failed")))
    target = {"player_uid": "shared", "player_guid": "B" * 32, "nickname": "Current", "level": 20}

    targets, report = BackupPackageService.sync_migration_player_center("current-server", tmp_path, tmp_path, (target,), online_error="rest failed")

    assert targets == ()
    assert report["stale"] is True
    assert report["source"] == "cache"


def test_schema_v2_deployed_session_upgrades_to_incremental_mode(tmp_path: Path):
    service = BackupPackageService()
    mapping = PlayerIdentityMapping(old_guid="A" * 32, new_guid="B" * 32, status="migrated", confirmed=True)
    session = CoopMigrationSession(instance_id="server", source_path="source", target_world_path="target", schema_version=2, mappings=(mapping,))
    path = service.save_migration_session(tmp_path, session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("player_sync_source", "player_sync_detail", "player_sync_stale", "source_world_deployed", "content_report", "client_data_package_path"):
        payload.pop(key, None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    upgraded = service.load_migration_session(tmp_path, "server")

    assert upgraded is not None
    assert upgraded.schema_version == 3
    assert upgraded.source_world_deployed is True
    assert upgraded.player_sync_stale is True


def test_restore_mapping_keeps_completed_players_and_only_maps_pending(tmp_path: Path):
    service = BackupPackageService()
    completed = PlayerIdentityMapping("A" * 32, "B" * 32, status="migrated")
    session = CoopMigrationSession(
        instance_id="server", source_path="source", target_world_path="target", phase="mapping_ready",
        source_players=({"player_guid": "A" * 32}, {"player_guid": "C" * 32, "instance_id": "old-c"}),
        placeholder_players=({"player_guid": "D" * 32, "instance_id": "new-d"},), mappings=(completed,),
        pending_player_guids=("C" * 32,),
    )

    updated = service.confirm_restore_mappings(session, {"C" * 32: "D" * 32}, tmp_path)

    assert updated.pending_player_guids == ()
    assert updated.mappings[0].status == "migrated"
    assert updated.mappings[1].old_guid == "C" * 32
    assert updated.mappings[1].status == "confirmed"


def test_identity_target_candidates_exclude_same_used_and_duplicate_guids():
    service = BackupPackageService(); old = "A" * 32; used = "B" * 32; available = "C" * 32
    targets = (
        {"player_guid": old, "nickname": "same"},
        {"player_guid": used, "nickname": "used"},
        {"player_guid": available, "nickname": "first"},
        {"player_guid": available.lower(), "nickname": "duplicate"},
        {"player_guid": "", "nickname": "invalid"},
    )

    result = service.available_identity_targets(old, targets, {used})

    assert result == ({"player_guid": available, "nickname": "first"},)


def test_temporary_identity_targets_only_use_active_player_center_records_below_level_three():
    targets = (
        {"player_guid": "A" * 32, "player_uid": "100", "nickname": "stale-a", "level": 80},
        {"player_guid": "B" * 32, "player_uid": "200", "nickname": "stale-b", "level": 1},
        {"player_guid": "C" * 32, "player_uid": "300", "nickname": "stale-c", "level": 1},
        {"player_guid": "D" * 32, "player_uid": "400", "nickname": "not-in-center", "level": 1},
    )
    center = (
        {"player_uid": "100", "nickname": "Temporary A", "level": 2, "save_status": "active"},
        {"player_uid": "200", "nickname": "Level Three", "level": 3, "save_status": "active"},
        {"player_uid": "300", "nickname": "Missing", "level": 1, "save_status": "missing"},
    )

    result = BackupPackageService.temporary_identity_targets(targets, center)

    assert result == ({"player_guid": "A" * 32, "player_uid": "100", "nickname": "Temporary A", "level": 2},)


def test_restore_mapping_accepts_identity_preserving_target_and_rejects_reuse(tmp_path: Path):
    service = BackupPackageService(); old_a = "A" * 32; old_c = "C" * 32; target = "B" * 32
    session = CoopMigrationSession(instance_id="server", source_path="source", target_world_path="target", phase="mapping_ready", source_players=({"player_guid": old_a, "nickname": "Alice"}, {"player_guid": old_c, "nickname": "Carol"}), placeholder_players=({"player_guid": old_a}, {"player_guid": target}), pending_player_guids=(old_a, old_c))

    updated = service.confirm_restore_mappings(session, {old_a: old_a}, tmp_path)
    assert updated.mappings[0].old_guid == old_a
    assert updated.mappings[0].new_guid == old_a
    with pytest.raises(ValueError, match="已分配给其他玩家"):
        service.confirm_restore_mappings(session, {old_a: target, old_c: target}, tmp_path)


def test_refresh_restore_placeholders_only_returns_players_added_after_baseline(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); saved = make_saved(tmp_path / "snapshot", "WORLD")
    world = saved / "SaveGames" / "0" / "WORLD"; players = world / "Players"
    existing = "A" * 32 + ".sav"; pending = "C" * 32 + ".sav"; placeholder = "D" * 32 + ".sav"
    (players / existing).write_bytes(b"existing"); (players / pending).write_bytes(b"pending"); (players / placeholder).write_bytes(b"placeholder")
    decoded = ({"player_guid": "A" * 32}, {"player_guid": "C" * 32}, {"player_guid": "D" * 32, "nickname": "New"})
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda *_args: decoded))
    monkeypatch.setattr(BackupPackageService, "temporary_identity_targets_from_player_center", classmethod(lambda cls, _instance, targets, _root: tuple(targets)))
    session = CoopMigrationSession(
        instance_id="server", source_path="old", target_world_path="target", phase="waiting_placeholders",
        baseline_player_files=(existing.upper(), pending.upper()), pending_player_guids=("C" * 32,), package_path="backup.pwcbackup",
    )

    updated = service.refresh_restore_placeholders(session, saved, tmp_path / "app")

    assert tuple(service._player_guid(item) for item in updated.placeholder_players) == ("D" * 32,)
    assert updated.phase == "mapping_ready"
    assert Path(updated.source_path, "Players", placeholder).is_file()


def test_restore_continuation_keeps_immutable_original_source(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); saved = make_saved(tmp_path / "snapshot", "WORLD")
    world = saved / "SaveGames" / "0" / "WORLD"; players = world / "Players"
    (players / ("A" * 32 + ".sav")).write_bytes(b"old"); (players / ("B" * 32 + ".sav")).write_bytes(b"new")
    source = tmp_path / "source"; (source / "Players").mkdir(parents=True); (source / "Level.sav").write_bytes(b"source-level")
    (source / "Players" / ("A" * 32 + ".sav")).write_bytes(b"source-player")
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda world, *_args: ({"player_guid": "B" * 32, "instance_id": "new-instance", "nickname": "New"},)))
    monkeypatch.setattr(BackupPackageService, "temporary_identity_targets_from_player_center", classmethod(lambda cls, _instance, targets, _root: tuple(targets)))
    session = CoopMigrationSession(instance_id="server", source_path=str(source), original_source_path=str(source), target_world_path=str(world), phase="waiting_placeholders", baseline_player_files=(("A" * 32 + ".sav").upper(),), pending_player_guids=("A" * 32,))
    updated = service.refresh_restore_placeholders(session, saved, tmp_path / "app")
    assert updated.original_source_path == str(source)
    assert updated.latest_snapshot_path.endswith("current-world")


def test_deployment_refresh_replaces_stale_target_snapshot(tmp_path: Path):
    service = BackupPackageService(); old_saved = make_saved(tmp_path / "old", "WORLD")
    old_world = old_saved / "SaveGames" / "0" / "WORLD"
    session = CoopMigrationSession(
        instance_id="server", source_path=str(old_world), original_source_path=str(old_world),
        target_world_path="/srv/palworld/Pal/Saved/SaveGames/0/WORLD",
        target_snapshot_path=str(old_world), latest_snapshot_path=str(old_world),
        target_world_hash="stale", latest_snapshot_hash="stale", phase="candidate_ready",
    )
    current_saved = make_saved(tmp_path / "current", "WORLD")
    current_world = current_saved / "SaveGames" / "0" / "WORLD"
    (current_world / "Level.sav").write_bytes(b"server-autosave-after-preflight")

    updated = service.refresh_restore_target_snapshot(session, current_saved, tmp_path / "app")

    pinned = Path(updated.latest_snapshot_path)
    assert pinned.name == "deployment-world"
    assert (pinned / "Level.sav").read_bytes() == b"server-autosave-after-preflight"
    assert updated.target_world_hash == updated.latest_snapshot_hash
    assert updated.target_world_hash != "stale"
    assert updated.snapshot_generation == 1


def test_inspect_world_players_uses_lightweight_decoder_and_storage_root(tmp_path: Path, monkeypatch):
    world = tmp_path / "world"; (world / "Players").mkdir(parents=True); (world / "Level.sav").write_bytes(b"level")
    seen = {}
    monkeypatch.setattr("palworld_console.backup_packages.PlmCodecPlugin.probe", lambda self: (seen.setdefault("root", self.app_root) is not None, "ready"))
    monkeypatch.setattr("palworld_console.backup_packages.PlmCodecPlugin.decode_players", lambda _self, _level: {"players": [{"player_guid": "0" * 31 + "1", "instance_id": "host-instance"}]})

    players = BackupPackageService.inspect_world_players(world, tmp_path / "app-storage")

    assert players[0]["player_guid"].endswith("1")
    assert seen["root"] == tmp_path / "app-storage"


def test_refresh_coop_placeholders_matches_player_filenames_case_insensitively(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); target = tmp_path / "target"; players_dir = target / "Players"; players_dir.mkdir(parents=True); (target / "Level.sav").write_bytes(b"level")
    guid = "ABCDEF0123456789ABCDEF0123456789"; (players_dir / f"{guid.lower()}.sav").write_bytes(b"player")
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda *_args: ({"player_guid": guid, "instance_id": "new-instance"},)))
    monkeypatch.setattr(BackupPackageService, "temporary_identity_targets_from_player_center", classmethod(lambda cls, _instance, targets, _root: tuple(targets)))
    session = CoopMigrationSession(instance_id="server", source_path="source", target_world_path=str(target), phase="waiting_placeholders", baseline_player_files=())

    updated = service.refresh_coop_placeholders(session, tmp_path / "storage")

    assert service._player_guid(updated.placeholder_players[0]) == guid
    assert updated.phase == "mapping_ready"


def test_initial_restore_candidate_uses_complete_source_world_and_exports_local_data(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); storage = tmp_path / "app"
    source = tmp_path / "source-world"; target = tmp_path / "target-world"
    for world, level in ((source, b"source-level"), (target, b"target-level")):
        (world / "Players").mkdir(parents=True)
        (world / "Level.sav").write_bytes(level)
        (world / "Players" / ("A" * 32 + ".sav")).write_bytes(b"player")
    (source / "LevelMeta.sav").write_bytes(b"source-meta")
    (source / "WorldOption.sav").write_bytes(b"source-options")
    (source / "UnknownExtension.bin").write_bytes(b"unknown-world-data")
    (source / "LocalData.sav").write_bytes(b"client-only")
    (target / "target-only.bin").write_bytes(b"must-not-survive")
    player = {"player_guid": "A" * 32, "player_uid": "A" * 32, "instance_id": "source-instance", "nickname": "Source", "level": 20, "pals": [], "items": {}}
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda *_args: (player,)))
    monkeypatch.setattr("palworld_console.backup_packages.PlmCodecPlugin.decode", lambda _self, _path: {"players": [player], "guilds": [{"guild_id": "guild-1"}], "bases": [{"base_id": "base-1"}]})
    session = CoopMigrationSession(
        instance_id="server", source_path=str(source), original_source_path=str(source), target_world_path=str(target),
        target_snapshot_path=str(target), latest_snapshot_path=str(target), original_source_hash=hashlib.sha256((source / "Level.sav").read_bytes()).hexdigest(),
        latest_snapshot_hash=hashlib.sha256((target / "Level.sav").read_bytes()).hexdigest(), source_players=(player,), pending_player_guids=("A" * 32,), phase="candidate_ready",
    )

    updated, report = service.build_restore_candidate(session, storage)

    candidate = storage / "migrations" / "server" / "restore" / "candidate"
    assert (candidate / "Level.sav").read_bytes() == b"source-level"
    assert (candidate / "LevelMeta.sav").read_bytes() == b"source-meta"
    assert (candidate / "WorldOption.sav").read_bytes() == b"source-options"
    assert (candidate / "UnknownExtension.bin").read_bytes() == b"unknown-world-data"
    assert not (candidate / "target-only.bin").exists()
    assert not (candidate / "LocalData.sav").exists()
    manifest = service.verify_client_data_package(Path(updated.client_data_package_path))
    assert manifest["files"][0]["sha256"] == hashlib.sha256(b"client-only").hexdigest()
    assert report["world_mode"] == "source-authoritative"
    assert updated.content_report["non_identity_files"]["verified"] is True


def test_incremental_restore_candidate_retains_latest_server_progress(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); storage = tmp_path / "app"
    source = tmp_path / "source-world"; latest = tmp_path / "latest-world"
    for world, level in ((source, b"source-level"), (latest, b"latest-level")):
        (world / "Players").mkdir(parents=True)
        (world / "Level.sav").write_bytes(level)
        (world / "Players" / ("A" * 32 + ".sav")).write_bytes(b"player")
    (source / "source-only.bin").write_bytes(b"old")
    (latest / "new-server-progress.bin").write_bytes(b"new")
    player = {"player_guid": "A" * 32, "player_uid": "A" * 32, "instance_id": "instance", "nickname": "Source", "level": 20, "pals": [], "items": {}}
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda *_args: (player,)))
    session = CoopMigrationSession(
        instance_id="server", source_path=str(latest), original_source_path=str(source), target_world_path=str(latest),
        target_snapshot_path=str(latest), latest_snapshot_path=str(latest), original_source_hash=hashlib.sha256((source / "Level.sav").read_bytes()).hexdigest(),
        latest_snapshot_hash=hashlib.sha256((latest / "Level.sav").read_bytes()).hexdigest(), source_players=(player,), source_world_deployed=True, phase="candidate_ready",
    )

    updated, report = service.build_restore_candidate(session, storage)

    candidate = storage / "migrations" / "server" / "restore" / "candidate"
    assert (candidate / "Level.sav").read_bytes() == b"latest-level"
    assert (candidate / "new-server-progress.bin").read_bytes() == b"new"
    assert not (candidate / "source-only.bin").exists()
    assert report["world_mode"] == "server-incremental"
    assert updated.source_world_deployed is True


def test_client_data_package_rejects_tampering(tmp_path: Path):
    package = tmp_path / "client-data.zip"
    payload = b"local-data"
    manifest = {
        "schema": "palworld-console-client-data-v1",
        "files": [{"archive_path": "files/000/LocalData.sav", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}],
    }
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("files/000/LocalData.sav", b"tampered")
    with pytest.raises(ValueError, match="校验失败"):
        BackupPackageService.verify_client_data_package(package)


def test_refresh_coop_placeholders_surfaces_identity_parse_error(tmp_path: Path, monkeypatch):
    service = BackupPackageService(); target = tmp_path / "target"; players_dir = target / "Players"; players_dir.mkdir(parents=True); (target / "Level.sav").write_bytes(b"level"); (players_dir / ("A" * 32 + ".sav")).write_bytes(b"player")
    monkeypatch.setattr(BackupPackageService, "inspect_world_players", staticmethod(lambda *_args: (_ for _ in ()).throw(RuntimeError("player file corrupt"))))
    session = CoopMigrationSession(instance_id="server", source_path="source", target_world_path=str(target), phase="waiting_placeholders", baseline_player_files=())

    with pytest.raises(RuntimeError, match="临时角色身份解析失败.*player file corrupt"):
        service.refresh_coop_placeholders(session, tmp_path / "storage")


def test_world_candidates_ignore_embedded_backup_history(tmp_path: Path):
    savegames = tmp_path / "SaveGames"; current = savegames / "0" / "WORLD"; backup = current / "backup" / "world" / "old"
    current.mkdir(parents=True); backup.mkdir(parents=True)
    (current / "Level.sav").write_bytes(b"current"); (backup / "Level.sav").write_bytes(b"old")

    candidates = BackupPackageService._world_candidates(savegames)

    assert [world for _root, world, _relative in candidates] == [current.resolve()]


def test_normalize_world_directory_excludes_embedded_backups(tmp_path: Path):
    source = tmp_path / "WORLD"; players = source / "Players"; backup = source / "backup" / "world" / "old"
    players.mkdir(parents=True); backup.mkdir(parents=True)
    (source / "Level.sav").write_bytes(b"current"); (players / ("A" * 32 + ".sav")).write_bytes(b"player"); (backup / "Level.sav").write_bytes(b"old")

    BackupPackageService().normalize_local_save(source, tmp_path / "normalized")

    world = tmp_path / "normalized" / "SaveGames" / "imported-world"
    assert (world / "Level.sav").read_bytes() == b"current"
    assert not (world / "backup").exists()


class FakePlmPlugin:
    def __init__(self, fail=False): self.fail = fail; self.patch = None
    def apply_patch(self, _source, patch, output): self.patch = patch; Path(output).write_bytes(b"merged")
    def verify_roundtrip(self, _path, _patch=None):
        if self.fail: raise RuntimeError("roundtrip failed")
        return {}


def parsed_save(path: Path, player: dict, guild="guild-1", plugin=None):
    properties = {"players": [player], "guilds": [{"guild_id": guild, "players": [{"player_uid": player["player_uid"]}]}] if guild else []}
    return PluginParsedSave.create(properties, path, plugin or FakePlmPlugin())


def test_single_player_merge_uses_stable_relations_and_supported_fields(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "source.sav"; target_path = tmp_path / "target.sav"; output = tmp_path / "output.sav"
    source_path.write_bytes(b"source"); target_path.write_bytes(b"target")
    source_player = {"player_uid": "42", "nickname": "新名字", "level": 30, "exp": 5000, "pals": [{"individual_id": "pal-1", "level": 12}], "items": {"bag": [{"ContainerId": "bag-1", "SlotIndex": 2, "StackCount": 99}]}}
    target_player = {"player_uid": "42", "nickname": "旧名字", "level": 10, "exp": 100, "pals": [{"individual_id": "pal-1", "level": 3}], "items": {"bag": [{"ContainerId": "bag-1", "SlotIndex": 2, "StackCount": 1}]}}
    source = parsed_save(source_path, source_player); target = parsed_save(target_path, target_player)
    monkeypatch.setattr(SaveGameService, "load", lambda _self, path: source if Path(path) == source_path else target)

    RestoreTransaction._merge_single_player(source_path, target_path, output, "42")

    assert output.read_bytes() == b"merged"
    assert target.properties["players"][0]["level"] == 30
    assert target.properties["players"][0]["pals"][0]["level"] == 12
    assert target.properties["players"][0]["items"]["bag"][0]["StackCount"] == 99
    assert target.plugin.patch["format"] == "palworld-console-save-patch-v2"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda source, target: target["guilds"].clear(), "公会关系"),
        (lambda source, target: target["players"][0]["pals"].clear(), "帕鲁 GUID"),
        (lambda source, target: target["players"][0]["items"].clear(), "容器或槽位"),
        (lambda source, target: target["players"].append(dict(target["players"][0])), "一个且仅一个"),
    ],
)
def test_single_player_merge_rejects_incomplete_or_ambiguous_relations(tmp_path: Path, monkeypatch, mutate, message):
    source_path = tmp_path / "source.sav"; target_path = tmp_path / "target.sav"; output = tmp_path / "output.sav"
    source_path.write_bytes(b"source"); target_path.write_bytes(b"target")
    player = {"player_uid": "42", "level": 30, "pals": [{"individual_id": "pal-1", "level": 12}], "items": {"bag": [{"ContainerId": "bag-1", "SlotIndex": 2, "StackCount": 99}]}}
    source = parsed_save(source_path, player); target = parsed_save(target_path, {**player, "pals": [dict(player["pals"][0])], "items": {"bag": [dict(player["items"]["bag"][0])]}})
    mutate(source.properties, target.properties)
    monkeypatch.setattr(SaveGameService, "load", lambda _self, path: source if Path(path) == source_path else target)
    with pytest.raises(ValueError, match=message): RestoreTransaction._merge_single_player(source_path, target_path, output, "42")


def test_single_player_merge_rejects_roundtrip_failure(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "source.sav"; target_path = tmp_path / "target.sav"; output = tmp_path / "output.sav"
    source_path.write_bytes(b"source"); target_path.write_bytes(b"target")
    source = parsed_save(source_path, {"player_uid": "42", "level": 30, "pals": [], "items": {}})
    target = parsed_save(target_path, {"player_uid": "42", "level": 10, "pals": [], "items": {}}, plugin=FakePlmPlugin(fail=True))
    monkeypatch.setattr(SaveGameService, "load", lambda _self, path: source if Path(path) == source_path else target)
    with pytest.raises(RuntimeError, match="roundtrip failed"): RestoreTransaction._merge_single_player(source_path, target_path, output, "42")


def test_protected_restore_point_cannot_be_deleted(tmp_path: Path):
    saved = make_saved(tmp_path / "server")
    instance = ServerInstance(id="i", install_dir=str(tmp_path / "server"))
    repository = BackupRepository(tmp_path / "repository", instance.id)
    package = BackupPackageService().create(instance, saved, repository.root, "restore-point")
    repository.set_metadata(package, protected=True)
    with pytest.raises(PermissionError, match="受保护"):
        repository.delete(package)


def test_remote_scheduled_backups_are_imported_once_and_sanitized_to_world_package(tmp_path: Path):
    remote_saved = make_saved(tmp_path / "remote", "WORLD-SCHEDULED", "must-not-leak")
    archive = make_legacy_remote_snapshot(tmp_path, remote_saved)
    archive_name = "saved-20260818-040000.tar.gz"

    class ScheduledClient:
        def run(self, command):
            assert "_backups/palworld-console" in command
            return 0, archive_name + "\nignored.txt\n", ""
        def download_file(self, remote, local):
            assert remote.endswith(archive_name)
            Path(local).write_bytes(archive.read_bytes())

    instance = ServerInstance(id="scheduled", kind="remote", install_dir="/srv/palworld", remote_profile={"platform": "linux", "install_dir": "/srv/palworld"})
    repository = BackupRepository(tmp_path / "repository", instance.id)
    imported = repository.import_remote_scheduled(ScheduledClient(), instance)
    assert imported == (archive_name,)
    records = repository.list(); assert len(records) == 1
    assert records[0]["manifest"].backup_type == "world"
    assert records[0]["manifest"].components == ("world",)
    assert b"must-not-leak" not in Path(records[0]["path"]).read_bytes()
    assert repository.import_remote_scheduled(ScheduledClient(), instance, imported) == ()


class RemoteRestoreClient:
    def __init__(self, platform_name="linux"):
        self.platform_name = platform_name
        self.upload_hash = ""
        self.commands = []

    def upload_file(self, local_path, remote_path):
        self.upload_hash = hashlib.sha256(Path(local_path).read_bytes()).hexdigest()
        self.commands.append(("upload", remote_path))

    def read_text(self, _path, missing_ok=False):
        return 'OptionSettings=(ServerName="Target",PublicPort=8211,RESTAPIPort=8212,AdminPassword="old");'

    def run(self, command):
        self.commands.append(("run", command))
        if "test -d" in command and '"saved"' in command:
            return 0, '{"saved":true}', ""
        if command.startswith("saved=$(du -sb"): return 0, '{"saved":1024,"free":10737418240}', ""
        if command.startswith("sha256sum"): return 0, self.upload_hash, ""
        return 0, "", ""

    def run_powershell(self, script):
        self.commands.append(("powershell", script))
        if "Test-Path" in script and "@{saved=" in script:
            return 0, '{"saved":true}', ""
        if "New-Item -ItemType Directory" in script:
            return 0, "", ""
        if "Measure-Object Length" in script: return 0, '{"saved":1024,"free":10737418240}', ""
        if "Get-FileHash" in script: return 0, self.upload_hash, ""
        return 0, "", ""


@pytest.mark.parametrize("platform_name", ["linux", "windows"])
def test_remote_savegames_restore_creates_parent_before_upload(tmp_path: Path, platform_name: str):
    saved = make_saved(tmp_path / "source", "WORLD-NESTED")
    world = saved / "SaveGames" / "0" / "WORLD-NESTED"
    nested = world / "Players" / "nested" / "profile.sav"; nested.parent.mkdir(parents=True); nested.write_bytes(b"nested")
    package = BackupPackageService().create(ServerInstance(id="source"), saved, tmp_path / "packages")

    class ParentAwareClient:
        def __init__(self): self.parent_ready = False; self.events = []
        def run(self, command):
            self.events.append(("run", command))
            if command.startswith("test -d"): return 0, "ok", ""
            if command.startswith("mkdir -p"):
                self.parent_ready = True; return 0, "", ""
            if command.startswith("mv -f"):
                self.parent_ready = False; return 0, "", ""
            if command.startswith("rm -f"): return 0, "", ""
            return 0, "", ""
        def run_powershell(self, script):
            self.events.append(("powershell", script))
            if "Test-Path" in script: return 0, "ok", ""
            if "New-Item -ItemType Directory" in script:
                self.parent_ready = True; return 0, "", ""
            if "Move-Item" in script:
                self.parent_ready = False; return 0, "", ""
            return 0, "", ""
        def upload_file(self, local, remote):
            if not self.parent_ready: raise FileNotFoundError(2, "No such file", remote)
            assert Path(local).is_file()
            self.events.append(("upload", remote))

    client = ParentAwareClient(); calls = []
    target = r"D:\Pal\Saved\SaveGames" if platform_name == "windows" else "/srv/pal/Pal/Saved/SaveGames"
    result = RestoreTransaction().restore_savegames_remote(
        package, target, client, platform_name, lambda: calls.append("stop"), lambda: calls.append("start")
    )

    assert result.restored is True
    assert calls == ["stop", "start"]
    uploads = [value for kind, value in client.events if kind == "upload"]
    assert len(uploads) == 3
    assert any("Players" in path and "nested" in path for path in uploads)


def test_remote_savegames_restore_translates_sftp_errno_and_restarts(tmp_path: Path):
    saved = make_saved(tmp_path / "source", "WORLD-ERROR")
    package = BackupPackageService().create(ServerInstance(id="source"), saved, tmp_path / "packages")

    class MissingPathClient:
        def __init__(self): self.cleaned = False
        def run(self, command):
            if command.startswith("test -d"): return 0, "ok", ""
            if command.startswith("mkdir -p"): return 0, "", ""
            if command.startswith("rm -f"): self.cleaned = True; return 0, "", ""
            return 0, "", ""
        def upload_file(self, _local, remote):
            raise FileNotFoundError(2, "No such file", remote)

    client = MissingPathClient(); calls = []
    with pytest.raises(RuntimeError) as raised:
        RestoreTransaction().restore_savegames_remote(
            package, "/srv/pal/Pal/Saved/SaveGames", client, "linux",
            lambda: calls.append("stop"), lambda: calls.append("start"),
        )

    message = str(raised.value)
    assert "失败阶段：上传远程临时文件" in message
    assert "远程路径不存在或 SFTP 无法访问" in message
    assert "[Errno 2]" not in message
    assert calls == ["stop", "start"]
    assert client.cleaned is True

def make_legacy_remote_snapshot(path: Path, saved: Path, windows=False) -> Path:
    if windows:
        target = path / "remote-current.zip"
        with zipfile.ZipFile(target, "w") as archive:
            for file in saved.rglob("*"):
                if file.is_file(): archive.write(file, Path("Saved") / file.relative_to(saved))
    else:
        target = path / "remote-current.tar.gz"
        with tarfile.open(target, "w:gz") as archive: archive.add(saved, arcname="Saved")
    return target


@pytest.mark.parametrize("platform_name", ["linux", "windows"])
def test_remote_restore_uses_platform_atomic_replace_and_verified_restore_point(tmp_path: Path, monkeypatch, platform_name):
    source_saved = make_saved(tmp_path / "source", "WORLD-REMOTE")
    package = BackupPackageService().create(ServerInstance(id="source", install_dir=str(tmp_path / "source")), source_saved, tmp_path / "packages", "world")
    current_saved = make_saved(tmp_path / "current", "WORLD-CURRENT")
    snapshot = make_legacy_remote_snapshot(tmp_path, current_saved, platform_name == "windows")
    monkeypatch.setattr(BackupService, "create_remote", lambda *_args: snapshot)
    install = r"D:\PalworldServer\target" if platform_name == "windows" else "/srv/palworld/target"
    instance = ServerInstance(id="target", kind="remote", install_dir=install, remote_profile={"platform": platform_name, "install_dir": install})
    repository = BackupRepository(tmp_path / "repository", instance.id)
    client = RemoteRestoreClient(platform_name)
    calls = []

    result = RestoreTransaction().execute_remote(
        package, instance, client, repository, ("world",),
        lambda: calls.append("stop"), lambda: calls.append("start"), lambda: True,
    )

    assert result.restored is True
    assert calls == ["stop", "start"]
    assert Path(result.restore_point).exists()
    assert repository.list()[0]["protected"] is True
    command_text = "\n".join(command for _kind, command in client.commands)
    if platform_name == "windows":
        assert "Expand-Archive" in command_text
        assert "Move-Item" in command_text
    else:
        assert "tar -xzf" in command_text
        assert "mv --" in command_text
        upload_paths = [command for kind, command in client.commands if kind == "upload"]
        assert upload_paths and "/tmp/restore-" in upload_paths[0]
        assert "/srv/palworld/target/_tools" not in upload_paths[0]


def test_remote_restore_waits_for_server_health_after_restart(monkeypatch):
    checks = iter([False, False, True])
    sleeps = []
    waits = []
    monkeypatch.setattr("palworld_console.backup_packages.time.sleep", lambda seconds: sleeps.append(seconds))
    monotonic = iter([0.0, 1.0, 3.0])
    monkeypatch.setattr("palworld_console.backup_packages.time.monotonic", lambda: next(monotonic))
    assert RestoreTransaction._wait_for_health(lambda: next(checks), timeout_seconds=10, interval_seconds=2, on_wait=lambda elapsed, remaining: waits.append((elapsed, remaining)))
    assert sleeps == [2, 2]
    assert waits == [(1.0, 9.0), (3.0, 7.0)]
