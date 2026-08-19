import json
import hashlib
import tarfile
import zipfile
from pathlib import Path

import pytest

from palworld_console.backup_packages import BackupPackageService, BackupRepository, RestoreTransaction
from palworld_console.config_ini import PalWorldSettings, settings_path
from palworld_console.models import ServerInstance
from palworld_console.services import BackupService
from palworld_console.save_codec import PluginParsedSave
from palworld_console.management import SaveGameService


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
    monkeypatch.setattr("palworld_console.backup_packages.time.sleep", lambda seconds: sleeps.append(seconds))
    assert RestoreTransaction._wait_for_health(lambda: next(checks), timeout_seconds=10, interval_seconds=2)
    assert sleeps == [2, 2]
