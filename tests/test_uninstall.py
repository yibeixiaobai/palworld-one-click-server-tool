import base64
import re
import tarfile
from pathlib import Path

import pytest

from palworld_console.models import ServerInstance
from palworld_console.services import BackupService, LocalServerLifecycle, RemoteHostClient, RemoteServerLifecycle, _validate_local_palworld_install


class RemoteUninstallClient(RemoteHostClient):
    def __init__(self):
        super().__init__("server.example", "pal")
        self.commands = []
        self.has_saved_data = True

    def resolve_path(self, candidate="", require_writable_parent=False):
        if candidate == "~":
            return "/home/pal"
        return "/home/pal/palworld-server"

    def validate_install_target(self, install_dir):
        return install_dir

    def validate_palworld_install(self, install_dir):
        return None

    def run(self, command):
        self.commands.append(command)
        if command.startswith("systemctl is-active"):
            return 0, "active", ""
        if command.startswith("if [ ! -e "):
            return (0, "READY", "") if self.has_saved_data else (0, "MISSING", "")
        return 0, "", ""

    def run_stream(self, command, on_output):
        self.commands.append(command)
        on_output("PAL_PROGRESS|88|删除服务端文件")
        return 0, "", ""

    def download_file(self, remote_path, local_path):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        source = local_path.parent / "world.sav"
        source.write_bytes(b"palworld-save")
        with tarfile.open(local_path, "w:gz") as archive:
            archive.add(source, arcname="Saved/SaveGames/world.sav")
        source.unlink()


def uploaded_script(command: str) -> str:
    match = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| bash", command)
    assert match
    return base64.b64decode(match.group(1)).decode("utf-8")


def test_local_uninstall_backs_up_before_removing_install(tmp_path):
    install_dir = tmp_path / "server"
    saved = install_dir / "Pal" / "Saved" / "SaveGames"
    saved.mkdir(parents=True)
    (install_dir / "PalServer.exe").write_bytes(b"exe")
    (saved / "world.sav").write_bytes(b"save")
    instance = ServerInstance(install_dir=str(install_dir))

    result = LocalServerLifecycle(instance).uninstall(tmp_path / "backups")

    assert not install_dir.exists()
    assert result.had_saved_data is True
    assert Path(result.backup_path).exists()


def test_local_uninstall_keeps_files_when_backup_fails(tmp_path, monkeypatch):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    (install_dir / "PalServer.exe").write_bytes(b"exe")
    (install_dir / "Pal" / "Saved").mkdir(parents=True)
    instance = ServerInstance(install_dir=str(install_dir))
    monkeypatch.setattr(BackupService, "create_local_if_present", lambda *_args: (_ for _ in ()).throw(RuntimeError("backup failed")))

    with pytest.raises(RuntimeError, match="backup failed"):
        LocalServerLifecycle(instance).uninstall(tmp_path / "backups")
    assert install_dir.exists()


def test_local_validation_rejects_home_directory():
    with pytest.raises(ValueError, match="危险本机目录"):
        _validate_local_palworld_install(Path.home())


def test_remote_uninstall_stops_backs_up_then_removes_service(tmp_path):
    client = RemoteUninstallClient()
    instance = ServerInstance(kind="remote", install_dir="~/palworld-server", remote_profile={"installed": True, "install_dir": "~/palworld-server", "service_name": "palworld"})

    result = RemoteServerLifecycle(instance, client).uninstall(tmp_path / "backups")

    assert result.had_saved_data is True
    assert Path(result.backup_path).exists()
    stop_index = next(i for i, command in enumerate(client.commands) if "systemctl stop" in command)
    tar_index = next(i for i, command in enumerate(client.commands) if command.startswith("tar -C"))
    script_command = next(command for command in client.commands if "base64 -d | bash" in command)
    assert stop_index < tar_index < client.commands.index(script_command)
    script = uploaded_script(script_command)
    assert script.index("systemctl disable") < script.index("rm -f --") < script.index("daemon-reload") < script.index("rm -rf --")


def test_remote_uninstall_does_not_delete_when_backup_download_fails(tmp_path):
    client = RemoteUninstallClient()
    client.download_file = lambda *_args: (_ for _ in ()).throw(RuntimeError("SFTP disconnected"))
    instance = ServerInstance(kind="remote", install_dir="/home/pal/palworld-server", remote_profile={"installed": True, "install_dir": "/home/pal/palworld-server", "service_name": "palworld"})

    with pytest.raises(RuntimeError, match="SFTP disconnected"):
        RemoteServerLifecycle(instance, client).uninstall(tmp_path / "backups")
    assert not any("base64 -d | bash" in command for command in client.commands)
    assert any("systemctl start" in command for command in client.commands)


def test_remote_uninstall_allows_missing_saved_directory(tmp_path):
    client = RemoteUninstallClient()
    client.has_saved_data = False
    instance = ServerInstance(kind="remote", install_dir="/home/pal/palworld-server", remote_profile={"installed": True, "install_dir": "/home/pal/palworld-server"})

    result = RemoteServerLifecycle(instance, client).uninstall(tmp_path / "backups")

    assert result.had_saved_data is False
    assert result.backup_path == ""
