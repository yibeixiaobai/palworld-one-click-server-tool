import json
from pathlib import Path

from palworld_console.config_ini import PalWorldSettings, settings_path
from palworld_console.models import ServerInstance
from palworld_console.services import RemoteHostClient, ServerConfigBootstrap


TEMPLATE = '''[/Script/Pal.PalGameWorldSettings]
OptionSettings=(ServerName="Default",UnknownFlag=True,ExpRate=1.0,CrossplayPlatforms=(Steam,Xbox,PS5,Mac));
'''


class MemoryRemoteClient:
    def __init__(self, files):
        self.files = dict(files)
        self.writes = []
        self.commands = []

    def read_text(self, path, missing_ok=False):
        if path in self.files:
            return self.files[path]
        if missing_ok:
            return ""
        raise FileNotFoundError(path)

    def write_text_atomic(self, path, content, backup=True):
        self.writes.append((path, content, backup))
        self.files[path] = content
        return ""


def test_local_bootstrap_creates_config_and_preserves_template_values(tmp_path: Path):
    (tmp_path / "DefaultPalWorldSettings.ini").write_text(TEMPLATE, encoding="utf-8")
    instance = ServerInstance(name="测试服务器", install_dir=str(tmp_path), game_port=9000)

    result = ServerConfigBootstrap.ensure_local(instance, "generated-secret")

    assert result.created is True
    assert result.source == "自动生成"
    settings = PalWorldSettings.load(settings_path(tmp_path))
    assert settings.values["ServerName"] == "测试服务器"
    assert settings.values["AdminPassword"] == "generated-secret"
    assert settings.values["PublicPort"] == 9000
    assert settings.values["RESTAPIEnabled"] is True
    assert settings.values["UnknownFlag"] is True
    assert str(settings.values["CrossplayPlatforms"]) == "(Steam,Xbox,PS5,Mac)"


def test_existing_local_config_is_read_without_overwrite(tmp_path: Path):
    target = settings_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text('OptionSettings=(ServerName="Hand Edited",AdminPassword="keep-me",PublicPort=7777);', encoding="utf-8")
    instance = ServerInstance(name="Ignored", install_dir=str(tmp_path))

    result = ServerConfigBootstrap.ensure_local(instance, "new-secret")

    assert result.created is False
    assert result.values["ServerName"] == "Hand Edited"
    assert result.values["AdminPassword"] == "keep-me"


def test_local_user_update_creates_backup(tmp_path: Path):
    target = settings_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text('OptionSettings=(ServerName="Before",UnknownFlag=True);', encoding="utf-8")
    instance = ServerInstance(install_dir=str(tmp_path))

    ServerConfigBootstrap.update_local(instance, {"ServerName": "After"})

    assert PalWorldSettings.load(target).values["ServerName"] == "After"
    assert list((target.parent / "backups").glob("*.bak"))


def test_remote_bootstrap_uses_sftp_content_and_does_not_put_secret_in_commands():
    install_dir = "/home/pal/palworld-server"
    template_path = f"{install_dir}/DefaultPalWorldSettings.ini"
    target = f"{install_dir}/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
    client = MemoryRemoteClient({template_path: TEMPLATE})
    instance = ServerInstance(name="Cloud", kind="remote", install_dir=install_dir, remote_profile={"install_dir": install_dir})

    result = ServerConfigBootstrap.ensure_remote(client, instance, "remote-secret")

    assert result.created is True
    assert client.writes[0][0] == target
    assert "remote-secret" in client.writes[0][1]
    assert all("remote-secret" not in command for command in client.commands)
    serialized = json.dumps(instance.to_dict(), ensure_ascii=False)
    assert "remote-secret" not in serialized


def test_remote_existing_config_is_not_written():
    install_dir = "/home/pal/palworld-server"
    target = f"{install_dir}/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
    existing = 'OptionSettings=(ServerName="Existing",AdminPassword="existing-secret");'
    client = MemoryRemoteClient({target: existing})
    instance = ServerInstance(kind="remote", install_dir=install_dir, remote_profile={"install_dir": install_dir})

    result = ServerConfigBootstrap.ensure_remote(client, instance, "new-secret")

    assert result.values["AdminPassword"] == "existing-secret"
    assert client.writes == []


def test_generated_admin_password_is_random_and_nontrivial():
    first = ServerConfigBootstrap.generate_admin_password()
    second = ServerConfigBootstrap.generate_admin_password()
    assert first != second
    assert len(first) >= 24


def test_remote_atomic_write_creates_backup_and_sets_private_permissions():
    class Stream:
        def __init__(self, sftp, path, mode):
            self.sftp, self.path, self.mode = sftp, path, mode
            self.data = sftp.files.get(path, b"") if "r" in mode else b""

        def __enter__(self): return self
        def __exit__(self, *_args):
            if "w" in self.mode: self.sftp.files[self.path] = self.data

        def read(self): return self.data
        def write(self, data): self.data += data.encode() if isinstance(data, str) else data

    class Sftp:
        def __init__(self):
            self.files = {"/srv/config.ini": b"old"}
            self.modes = {}

        def file(self, path, mode): return Stream(self, path, mode)
        def stat(self, path):
            if path not in self.files: raise FileNotFoundError(path)
            return object()
        def chmod(self, path, mode): self.modes[path] = mode
        def posix_rename(self, source, target): self.files[target] = self.files.pop(source)
        def remove(self, path):
            if path not in self.files: raise FileNotFoundError(path)
            del self.files[path]
        def close(self): pass

    class Ssh:
        def __init__(self, sftp): self.sftp = sftp
        def open_sftp(self): return self.sftp
        def close(self): pass

    class Client(RemoteHostClient):
        def __init__(self, sftp):
            super().__init__("host", "user")
            self.sftp = sftp
        def _connect(self): return Ssh(self.sftp)
        def run(self, command): return 0, "", ""

    sftp = Sftp()
    backup = Client(sftp).write_text_atomic("/srv/config.ini", "new-secret-config")

    assert sftp.files["/srv/config.ini"] == b"new-secret-config"
    assert sftp.files[backup] == b"old"
    assert sftp.modes["/srv/config.ini"] == 0o600
    assert sftp.modes[backup] == 0o600
