import base64
import posixpath
import re

import pytest

from palworld_console.models import ServerInstance
from palworld_console.models import TaskProgress
from palworld_console.services import RemoteHostClient, RemoteServerInspector, RemoteServerLifecycle, SteamCmdInstaller, _LineBuffer


class DiscoveryClient:
    host = "server.example"

    def __init__(self, steamcmd_path: str, download_tool: str = "/usr/bin/curl", sudo: bool = True):
        self.steamcmd_path = steamcmd_path
        self.download_tool = download_tool
        self.sudo = sudo

    @staticmethod
    def steamcmd_discovery_command() -> str:
        return RemoteHostClient.steamcmd_discovery_command()

    def run(self, command: str):
        if command.startswith("uname -s"):
            return 0, 'Linux\nx86_64\nPRETTY_NAME="Ubuntu 24.04 LTS"\n', ""
        if command.startswith("printf '%s' \"$HOME\""):
            return 0, "/home/pal", ""
        if command.startswith("df -Pk"):
            return 0, "/dev/vda1 100000 20000 80000 20% /home", ""
        if command.startswith("sudo -n true"):
            return 0, "yes\n" if self.sudo else "no\n", ""
        if "command -v curl" in command:
            return 0, f"{self.download_tool}\n" if self.download_tool else "", ""
        if "command -v tar" in command:
            return 0, "yes\n", ""
        if "for candidate" in command:
            return 0, f"{self.steamcmd_path}\n" if self.steamcmd_path else "", ""
        if "find $HOME /opt /srv" in command:
            return 0, "", ""
        if "list-unit-files" in command:
            return 0, "", ""
        if command.startswith("test -f "):
            return 1, "", ""
        return 0, "", ""

    def resolve_path(self, candidate="", require_writable_parent=False):
        value = candidate or "$HOME/palworld-server"
        value = value.replace("$HOME", "/home/pal")
        if value == "~": value = "/home/pal"
        elif value.startswith("~/"): value = "/home/pal/" + value[2:]
        elif not value.startswith("/"): value = "/home/pal/" + value
        return posixpath.normpath(value)

    def validate_install_target(self, install_dir):
        return install_dir


class RecordingClient(RemoteHostClient):
    def __init__(self):
        super().__init__("server.example", "pal")
        self.commands = []

    def run(self, command: str):
        self.commands.append(command)
        return 0, "ok", ""

    def run_stream(self, command: str, on_output):
        self.commands.append(command)
        on_output("ok")
        return 0, "ok", ""

    def resolve_path(self, candidate="", require_writable_parent=False):
        value = candidate or "$HOME/palworld-server"
        value = value.replace("$HOME", "/home/pal")
        if value == "~": value = "/home/pal"
        elif value.startswith("~/"): value = "/home/pal/" + value[2:]
        elif not value.startswith("/"): value = "/home/pal/" + value
        return posixpath.normpath(value)

    def validate_install_target(self, install_dir):
        return install_dir

    def validate_palworld_install(self, install_dir):
        return None


def _uploaded_script(command: str) -> str:
    match = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| bash", command)
    assert match, command
    return base64.b64decode(match.group(1)).decode("utf-8")


@pytest.mark.parametrize(
    "path, source",
    [
        ("/usr/games/steamcmd", "系统或已有安装"),
        ("/home/pal/.local/share/SteamCMD/steamcmd.sh", "用户目录自动安装"),
    ],
)
def test_discovery_accepts_path_and_common_install_locations(path, source):
    profile = RemoteServerInspector(DiscoveryClient(path)).discover()
    assert profile["steamcmd_available"] is True
    assert profile["steamcmd_path"] == path
    assert profile["steamcmd_source"] == source


def test_discovery_marks_missing_steamcmd_as_auto_installable():
    profile = RemoteServerInspector(DiscoveryClient("")).discover()
    assert profile["steamcmd_available"] is False
    assert profile["steamcmd_installable"] is True
    assert profile["steamcmd_source"] == "未安装，可自动安装"


def test_discovery_allows_dependency_install_when_only_sudo_is_available():
    profile = RemoteServerInspector(DiscoveryClient("", download_tool="", sudo=True)).discover()
    assert profile["steamcmd_installable"] is True


def test_install_bootstraps_steamcmd_and_uses_resolved_absolute_path():
    client = RecordingClient()
    instance = ServerInstance(
        kind="remote",
        remote_username="pal",
        remote_profile={"installed": False, "home_dir": "/home/pal"},
    )
    RemoteServerLifecycle(instance, client).install()

    script = _uploaded_script(client.commands[-1])
    assert "$HOME/.local/share/SteamCMD/steamcmd.sh" in script
    assert "steamcmd_linux.tar.gz" in script
    assert "apt-get install -y tar ca-certificates curl" in script
    assert '"$steamcmd_path" +force_install_dir' in script
    assert "\nsteamcmd +force_install_dir" not in script


def test_update_rechecks_and_bootstraps_steamcmd_instead_of_using_bare_command():
    client = RecordingClient()
    instance = ServerInstance(
        kind="remote",
        remote_profile={
            "installed": True,
            "install_dir": "/home/pal/palworld-server",
            "service_name": "palworld",
            "steamcmd_path": "",
        },
    )
    RemoteServerLifecycle(instance, client).update()

    update_command = next(command for command in client.commands if "base64 -d | bash" in command)
    script = _uploaded_script(update_command)
    assert "for candidate in" in script
    assert '"$steamcmd_path" +force_install_dir' in script
    assert "\nsteamcmd +force_install_dir" not in script
    assert client.commands[-1] == "sudo -n systemctl start palworld"


def test_deployment_rejects_unsafe_systemd_service_name():
    client = RemoteHostClient("server.example", "pal")
    with pytest.raises(ValueError, match="systemd"):
        client.deployment_script("/home/pal/server", "palworld; rm -rf", 8211, 8212)


def test_systemd_service_runs_as_ssh_user_and_enables_game_data_api():
    script = RemoteHostClient("server.example", "ubuntu").systemd_script(
        "/home/ubuntu/palworld-server", "palworld", 8211, 8212, "ubuntu", "/home/ubuntu"
    )
    assert "User=ubuntu" in script
    assert "Group=ubuntu" in script
    assert "Environment=HOME=/home/ubuntu" in script
    assert "-port=8211" in script
    assert "-RESTAPIPort=8212" in script
    assert "-enable-gamedata-api" in script


@pytest.mark.parametrize(
    "candidate, expected",
    [
        ("", "/home/pal/palworld-server"),
        ("palworld-server", "/home/pal/palworld-server"),
        ("~/games/palworld", "/home/pal/games/palworld"),
        ("$HOME/games/palworld", "/home/pal/games/palworld"),
        ("/srv/games/palworld", "/srv/games/palworld"),
    ],
)
def test_lifecycle_normalizes_remote_install_path(candidate, expected):
    client = RecordingClient()
    instance = ServerInstance(kind="remote", install_dir=candidate, remote_profile={"installed": False})
    lifecycle = RemoteServerLifecycle(instance, client)
    assert lifecycle._install_dir() == expected
    assert instance.install_dir == expected
    assert instance.remote_profile["install_dir"] == expected


@pytest.mark.parametrize(
    "candidate, expected",
    [
        (r"\home\ubuntu\palworld-server", "/home/ubuntu/palworld-server"),
        (r"/home/ubuntu/\home\ubuntu\palworld-server", "/home/ubuntu/palworld-server"),
        (r"~\palworld-server", "/home/ubuntu/palworld-server"),
        (r"$HOME\games\palworld", "/home/ubuntu/games/palworld"),
    ],
)
def test_path_candidate_repairs_mixed_and_duplicated_linux_paths(candidate, expected):
    assert RemoteHostClient.normalize_path_candidate(candidate, "/home/ubuntu") == expected


def test_path_candidate_rejects_windows_drive_path():
    with pytest.raises(ValueError, match="Windows 盘符"):
        RemoteHostClient.normalize_path_candidate(r"C:\\palworld-server", "/home/ubuntu")


@pytest.mark.parametrize("path", ["/", "/home/pal", "/opt"])
def test_remote_target_validation_rejects_dangerous_paths(path):
    client = RemoteHostClient("server.example", "pal")
    client.resolve_path = lambda candidate="", require_writable_parent=False: "/home/pal" if candidate == "~" else candidate
    with pytest.raises(ValueError, match="危险远程目录"):
        client.validate_install_target(path)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Update state downloading, progress: 0", 20),
        ("Update state downloading, progress: 50.0", 58),
        ("Update state downloading, progress: 100.00", 95),
        ("Update state downloading, progress: 999", 95),
    ],
)
def test_steamcmd_progress_parser_maps_download_range(line, expected):
    progress = SteamCmdInstaller.parse_progress(line)
    assert progress == TaskProgress(expected, "下载并校验服务端", progress.message)


def test_steamcmd_progress_parser_ignores_unrelated_output():
    assert SteamCmdInstaller.parse_progress("Success! App fully installed.") is None


def test_remote_progress_marker_is_clamped_and_parsed():
    assert RemoteServerLifecycle.parse_progress("PAL_PROGRESS|140|启动服务") == TaskProgress(100, "启动服务", "启动服务")
    assert RemoteServerLifecycle.parse_progress("PAL_PROGRESS|-5|检测环境") == TaskProgress(0, "检测环境", "检测环境")


def test_line_buffer_reassembles_chunked_ssh_output():
    buffer = _LineBuffer()
    assert buffer.feed(b"PAL_PROG") == []
    assert buffer.feed(b"RESS|10|check\r\nnext") == ["PAL_PROGRESS|10|check"]
    assert buffer.feed(" line\nlast") == ["next line"]
    assert buffer.finish() == ["last"]
