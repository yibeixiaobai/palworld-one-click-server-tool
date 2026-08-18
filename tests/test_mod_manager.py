import json
import time
import zipfile
from pathlib import Path

import pytest

from palworld_console.mod_manager import LocalArchiveProvider, LocalPakProvider, ModEnvironment, ModManager, ModManifest, ModPackageService, WorkshopCatalogService, WorkshopProvider, parse_workshop_id


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


def test_install_plan_maps_ue4ss_native_and_pak(tmp_path):
    env = ModEnvironment("Windows", "windows", mods_dir=str(tmp_path / "Mods"), settings_path=str(tmp_path / "Mods" / "PalModSettings.ini"), palserver_exe=str(tmp_path / "PalServer.exe"), supported=True, ue4ss_root=str(tmp_path / "UE4SS"), ue4ss_mods_dir=str(tmp_path / "UE4SS" / "Mods"), native_mods_dir=str(tmp_path / "UE4SS" / "NativeMods"), paks_dir=str(tmp_path / "Pal" / "Content" / "Paks"))
    assert Path(ModManager.build_install_plan(ModManifest("A", mod_type="ue4ss"), env).target).parts[-3:] == ("UE4SS", "Mods", "A")
    assert Path(ModManager.build_install_plan(ModManifest("B", mod_type="native"), env).target).parts[-3:] == ("UE4SS", "NativeMods", "B")
    assert Path(ModManager.build_install_plan(ModManifest("C", mod_type="pak", archive_path="C:/C.pak"), env, allow_unverified=True).target).parts[-2:] == ("Paks", "C.pak")


def test_detected_environment_is_ue4ss_only_and_legacy_mods_are_read_only(tmp_path):
    root = tmp_path / "server"; (root / "UE4SS" / "Mods").mkdir(parents=True); (root / "UE4SS" / "NativeMods").mkdir(); (root / "PalServer.exe").write_bytes(b"exe")
    environment = ModManager.detect_local(root)
    assert environment.ue4ss_only is True
    assert ModManager.build_install_plan(ModManifest("Legacy", mod_type="official"), environment).read_only is True


def test_ue4ss_local_install_uses_enabled_marker_without_palmodsettings(tmp_path):
    root = tmp_path / "server"; (root / "UE4SS" / "Mods").mkdir(parents=True); (root / "UE4SS" / "NativeMods").mkdir(); (root / "PalServer.exe").write_bytes(b"exe")
    source = tmp_path / "source"; source.mkdir(); (source / "main.lua").write_text("return {}", encoding="utf-8")
    env = ModManager.detect_local(root); manifest = ModManifest("Demo", mod_type="ue4ss", ue4ss_kind="script", archive_path=str(source), server_supported=True)
    ModManager(tmp_path / "cache").install_local(manifest, env, [], lambda: None, lambda: None, lambda: True)
    assert (root / "UE4SS" / "Mods" / "Demo" / "enabled.txt").exists()
    assert not (root / "Mods" / "PalModSettings.ini").exists()


def test_url_import_uses_content_type_when_url_has_no_extension(tmp_path, monkeypatch):
    class Response:
        headers = {"Content-Type": "application/zip"}
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size=-1):
            if hasattr(self, "done"): return b""
            self.done = True
            import io
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as bundle:
                bundle.writestr("Info.json", json.dumps({"PackageName": "UrlMod", "InstallRules": ["DedicatedServer"]}))
            return buffer.getvalue()
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    manifest = ModPackageService(tmp_path).prepare_url("https://example.test/download?id=1")
    assert manifest.package_name == "UrlMod" and manifest.validation_status == "awaiting_confirmation"


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


def test_workshop_catalog_parses_titles_authors_and_images():
    source = '''
    <div class="workshopItem">
      <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=123"><img class="workshopItemPreviewImage" src="https://img.test/123.jpg"></a>
      <div class="workshopItemTitle"><span>中文模组</span></div>
      <div class="workshopItemAuthorName">By 作者甲</div>
    </div>
    <div class="workshopItem">
      <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=456">备用标题</a>
    </div>'''
    items = WorkshopCatalogService.parse_catalog(source)
    assert [(item.workshop_id, item.title, item.author, item.preview_url) for item in items] == [
        ("123", "中文模组", "作者甲", "https://img.test/123.jpg"),
        ("456", "备用标题", "", ""),
    ]


def test_workshop_catalog_uses_stale_cache_when_network_fails(tmp_path, monkeypatch):
    service = WorkshopCatalogService(tmp_path, ttl_seconds=1, timeout_seconds=1)
    cache = tmp_path / "catalog-trend-1-e3b0c44298fc.json"
    payload = {"items": [{"workshop_id": "1", "title": "缓存模组"}], "page": 1, "query": "", "sort": "trend", "fetched_at": "cached"}
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    old = time.time() - 20
    import os
    os.utime(cache, (old, old))
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    page = service.fetch()
    assert page.from_cache is True and page.items[0].title == "缓存模组"


def test_failed_mod_install_removes_new_paks_directory(tmp_path):
    root = tmp_path / "server"; exe = root / "PalServer.exe"; exe.parent.mkdir(parents=True); exe.write_bytes(b"exe")
    mods = root / "Mods" / "Workshop"; settings = root / "Mods" / "PalModSettings.ini"
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Info.json", json.dumps({"PackageName": "Example", "InstallRules": ["DedicatedServer"]}))
    manifest = LocalArchiveProvider().prepare(archive, tmp_path / "cache")
    environment = ModEnvironment("Windows", "windows", mods_dir=str(mods), settings_path=str(settings), palserver_exe=str(exe), supported=True)
    with pytest.raises(RuntimeError):
        ModManager(tmp_path / "cache").install_local(manifest, environment, [], lambda: None, lambda: None, lambda: False)
    assert not (root / "Pal" / "Content" / "Paks").exists()


def test_remote_workshop_provider_downloads_and_extracts_verified_archive(tmp_path):
    class Client:
        def __init__(self): self.commands = []
        def run(self, command):
            self.commands.append(command)
            if "rm -rf" in command and command.startswith("rm -rf"):
                return 0, "", ""
            return 0, "", ""
        def download_file(self, _remote, local):
            local.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(local.with_suffix(".zip"), "w") as bundle:
                bundle.writestr("Info.json", json.dumps({"PackageName": "RemoteMod", "InstallRules": ["DedicatedServer"]}))
            # The provider expects tar.gz; build it from a small temporary tree.
            import tarfile
            source = local.parent / "remote-source"; source.mkdir(exist_ok=True)
            (source / "Info.json").write_text(json.dumps({"PackageName": "RemoteMod", "InstallRules": ["DedicatedServer"]}), encoding="utf-8")
            with tarfile.open(local, "w:gz") as archive: archive.add(source, arcname=".")

    client = Client()
    manifest = WorkshopProvider(Path("steamcmd")).prepare_remote(client, "123", tmp_path / "cache", "/opt/steamcmd")
    assert manifest.package_name == "RemoteMod" and manifest.workshop_id == "123"
    assert any("+workshop_download_item" in command for command in client.commands)
