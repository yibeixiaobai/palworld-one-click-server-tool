import json
import zipfile

import pytest

from palworld_console.mod_manager import LocalArchiveProvider, LocalPakProvider, ModEnvironment, ModManager, ModManifest, parse_workshop_id


def test_archive_manifest_and_server_rule(tmp_path):
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Example/Info.json", json.dumps({"PackageName": "Example", "Name": "示例模组", "Version": "1.2", "InstallRules": ["DedicatedServer"], "Dependencies": ["Core"]}))
    manifest = LocalArchiveProvider().prepare(archive, tmp_path / "cache")
    assert manifest.package_name == "Example"
    assert manifest.server_supported is True
    assert manifest.dependencies == ("Core",)


def test_dependency_and_conflict_validation(tmp_path):
    manager = ModManager(tmp_path)
    target = ModManifest("Target", server_supported=True, dependencies=("Core",), conflicts=("Bad",))
    with pytest.raises(ValueError, match="缺少依赖"):
        manager.validate_enable(target, [])
    installed = [ModManifest("Core", enabled=True), ModManifest("Bad", enabled=True)]
    with pytest.raises(ValueError, match="模组冲突"):
        manager.validate_enable(target, installed)


def test_native_linux_rejected_and_wine_detected(tmp_path):
    native = ModManager.detect_remote({"os": "Linux"})
    assert native.server_type == "linux-native" and native.supported is False
    wine = ModManager.detect_remote({"os": "Linux", "wine_path": "/usr/bin/wine64", "palserver_exe": "/srv/pal/PalServer.exe", "service_exec": "/usr/bin/wine64 /srv/pal/PalServer.exe", "mods_writable": True, "settings_writable": True, "mods_dir": "/srv/pal/Pal/Content/Mods", "mod_settings_path": "/srv/pal/Pal/Saved/Config/WindowsServer/PalModSettings.ini"})
    assert wine.server_type == "linux-wine" and wine.supported is True and wine.experimental is True


def test_pak_is_metadata_incomplete_and_workshop_links_parse(tmp_path):
    pak = tmp_path / "Example.pak"; pak.write_bytes(b"pak")
    manifest = LocalPakProvider().prepare(pak, tmp_path / "cache")
    assert manifest.metadata_complete is False and manifest.enabled is False
    assert parse_workshop_id("https://steamcommunity.com/sharedfiles/filedetails/?id=123456") == "123456"


def test_local_install_rolls_back_when_health_check_fails(tmp_path):
    root = tmp_path / "server"; mods = root / "Pal" / "Content" / "Mods"; mods.mkdir(parents=True)
    (mods / "old.txt").write_text("old", encoding="utf-8")
    settings = root / "Pal" / "Saved" / "Config" / "WindowsServer" / "PalModSettings.ini"; settings.parent.mkdir(parents=True); settings.write_text("old-settings", encoding="utf-8")
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Info.json", json.dumps({"PackageName": "Example", "InstallRules": ["DedicatedServer"]}))
    manifest = LocalArchiveProvider().prepare(archive, tmp_path / "cache")
    environment = ModEnvironment("Windows", "windows", mods_dir=str(mods), settings_path=str(settings), supported=True)
    starts = []
    with pytest.raises(RuntimeError, match="健康检查失败"):
        ModManager(tmp_path / "cache").install_local(manifest, environment, [], lambda: None, lambda: starts.append(True), lambda: False)
    assert (mods / "old.txt").read_text(encoding="utf-8") == "old"
    assert settings.read_text(encoding="utf-8") == "old-settings"
    assert starts


def test_local_transaction_can_be_rolled_back(tmp_path):
    root = tmp_path / "server"; mods = root / "Pal" / "Content" / "Mods"; mods.mkdir(parents=True)
    (mods / "old.txt").write_text("old", encoding="utf-8")
    settings = root / "Pal" / "Saved" / "Config" / "WindowsServer" / "PalModSettings.ini"; settings.parent.mkdir(parents=True); settings.write_text("old-settings", encoding="utf-8")
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Info.json", json.dumps({"PackageName": "Example", "InstallRules": ["DedicatedServer"]}))
    manager = ModManager(tmp_path / "cache"); manifest = LocalArchiveProvider().prepare(archive, tmp_path / "cache")
    environment = ModEnvironment("Windows", "windows", mods_dir=str(mods), settings_path=str(settings), supported=True)
    manager.install_local(manifest, environment, [], lambda: None, lambda: None, lambda: True)
    states = manager.rollback_latest_local(environment, lambda: None, lambda: None, lambda: True)
    assert states == {}
    assert (mods / "old.txt").read_text(encoding="utf-8") == "old"
    assert settings.read_text(encoding="utf-8") == "old-settings"
