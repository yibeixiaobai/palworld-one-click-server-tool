import base64
import json
import zipfile
from pathlib import Path

import pytest

from palworld_console.models import ServerInstance
from palworld_console.services import (
    BackupService,
    RemoteHostClient,
    RemoteServerInspector,
    ServerConfigBootstrap,
    WindowsRemotePath,
    WindowsRemoteServerLifecycle,
)


def test_powershell_command_uses_utf16le_encoded_command():
    command = RemoteHostClient.powershell_command("Write-Output '中文路径'")
    encoded = command.rsplit(" ", 1)[-1]
    assert "-EncodedCommand" in command
    assert base64.b64decode(encoded).decode("utf-16le") == "Write-Output '中文路径'"


@pytest.mark.parametrize("path", ["C:\\", r"C:\Windows", r"D:\Program Files", r"C:\Users", r"\\server\share\palworld"])
def test_windows_path_rejects_dangerous_targets(path):
    with pytest.raises(ValueError):
        WindowsRemotePath.normalize(path)


def test_windows_path_normalizes_drive_and_separators():
    assert WindowsRemotePath.normalize("d:/PalworldServer/demo") == r"D:\PalworldServer\demo"
    assert RemoteHostClient._sftp_path(r"D:\PalworldServer\demo\config.ini") == "/D:/PalworldServer/demo/config.ini"


class WindowsProbeClient:
    host = "windows.example"

    def run_powershell(self, _script):
        payload = {
            "platform": "windows",
            "os": "Microsoft Windows Server 2022 Standard",
            "version": "10.0.20348",
            "architecture": "AMD64",
            "elevated": True,
            "powershell_version": "5.1.20348.1",
            "volumes": [
                {"root": "C:\\", "total_bytes": 100, "free_bytes": 20, "writable": True},
                {"root": "D:\\", "total_bytes": 200, "free_bytes": 150, "writable": True},
            ],
            "install_dir": r"D:\PalworldServer\abc12345",
            "installed": False,
            "service_state": "not_found",
        }
        return 0, "noise\nPALWORLD_CONSOLE_WINDOWS_PROFILE:" + json.dumps(payload), ""


def test_remote_inspector_selects_windows_and_recommends_largest_drive():
    profile = RemoteServerInspector(WindowsProbeClient(), instance_id="abc12345").discover()
    assert profile["platform"] == "windows"
    assert profile["service_manager"] == "winsw"
    assert profile["volumes"][1]["recommended"] is True
    assert profile["install_dir"] == r"D:\PalworldServer\abc12345"


def test_old_remote_profile_is_migrated_as_inferred_linux():
    instance = ServerInstance.from_dict({"kind": "remote", "remote_profile": {"install_dir": "/srv/pal"}})
    assert instance.remote_profile["platform"] == "linux"
    assert instance.remote_profile["platform_inferred"] is True


def test_windows_service_xml_has_limited_account_and_no_credentials():
    instance = ServerInstance(id="12345678-rest", name="Production & Test", kind="remote")
    xml = WindowsRemoteServerLifecycle.service_xml(instance, r"D:\PalworldServer\12345678", "PalworldConsole-12345678", 8211, 8212)
    assert r"NT AUTHORITY\LocalService" in xml
    assert "-port=8211" in xml and "-RESTAPIPort=8212" in xml
    assert "Production &amp; Test" in xml
    assert "password" not in xml.lower()


class WindowsConfigClient:
    def __init__(self, files):
        self.files = files
        self.writes = []

    def read_text(self, path, missing_ok=False):
        return self.files.get(path, "")

    def write_text_atomic_windows(self, path, content, backup=True):
        self.writes.append((path, content, backup))
        self.files[path] = content
        return ""


def test_windows_remote_config_uses_windows_server_directory():
    install = r"D:\PalworldServer\abc12345"
    template = install + r"\DefaultPalWorldSettings.ini"
    client = WindowsConfigClient({template: 'OptionSettings=(ServerName="Default");'})
    instance = ServerInstance(name="Win", kind="remote", install_dir=install, remote_profile={"platform": "windows", "install_dir": install})
    result = ServerConfigBootstrap.ensure_remote(client, instance, "secret")
    assert result.config_path.endswith(r"Pal\Saved\Config\WindowsServer\PalWorldSettings.ini")
    assert client.writes and "secret" in client.writes[0][1]


class WindowsBackupClient:
    def run_powershell(self, script):
        if "Compress-Archive" in script:
            return 0, "", ""
        if "MISSING" in script:
            return 0, "READY", ""
        return 0, "", ""

    def download_file(self, _remote, local):
        with zipfile.ZipFile(local, "w") as archive:
            archive.writestr("Saved/SaveGames/world.sav", b"data")


def test_windows_remote_backup_downloads_and_validates_zip(tmp_path: Path):
    instance = ServerInstance(kind="remote", remote_profile={"platform": "windows"})
    result = BackupService().create_remote(WindowsBackupClient(), instance, tmp_path, r"D:\PalworldServer\abc12345")
    assert result and result.suffix == ".zip"
    BackupService.validate_zip(result)
