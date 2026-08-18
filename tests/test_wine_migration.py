import base64
import shlex

from palworld_console.models import ServerInstance
from palworld_console.wine_migration import WineMigrationService


class FakeClient:
    def __init__(self, output=""):
        self.output = output
        self.commands = []

    def resolve_path(self, value, require_writable_parent=False):
        if value in {"$HOME/palworld-wine-server", "~/palworld-wine-server", ""}:
            return "/home/pal/palworld-wine-server"
        return value

    def run(self, command):
        self.commands.append(command)
        if "DIST=" in command:
            return 0, self.output, ""
        return 0, "", ""


def test_wine_preflight_reports_missing_requirements():
    client = FakeClient("DIST=Ubuntu\nARCH=x86_64\nWINE=\nSTEAMCMD=/usr/games/steamcmd\nSUDO=yes\nSYSTEMD=yes\nFREE=99999999")
    instance = ServerInstance(kind="remote", install_dir="/home/pal/palworld-server", remote_username="pal", remote_profile={"install_dir": "/home/pal/palworld-server", "service_name": "palworld"})
    result = WineMigrationService(client, instance).inspect()
    assert result.ready is False and "wine64" in result.missing
    assert result.target_dir != result.source_dir


def test_wine_prepare_uses_windows_steamcmd_and_isolated_service():
    client = FakeClient("DIST=Ubuntu\nARCH=x86_64\nWINE=/usr/bin/wine64\nSTEAMCMD=/usr/games/steamcmd\nSUDO=yes\nSYSTEMD=yes\nFREE=99999999")
    instance = ServerInstance(kind="remote", install_dir="/home/pal/palworld-server", remote_username="pal", remote_profile={"install_dir": "/home/pal/palworld-server", "service_name": "palworld", "home_dir": "/home/pal", "primary_group": "pal"})
    service = WineMigrationService(client, instance)
    migration = service.prepare(service.inspect())
    encoded = shlex.split(client.commands[-1])[2]
    script = base64.b64decode(encoded).decode()
    assert "+@sSteamCmdForcePlatformType windows" in script
    assert "/home/pal/palworld-wine-server" in script
    assert "Config/WindowsServer/PalWorldSettings.ini" in script
    assert migration["source_dir"] == "/home/pal/palworld-server"


def test_wine_activation_script_restores_native_on_failure():
    client = FakeClient()
    instance = ServerInstance(kind="remote", remote_username="pal", game_port=8211, remote_profile={"service_name": "palworld", "rest_port": 8212})
    migration = {"wine_service": "palworld-wine-12345678", "source_service": "palworld", "target_dir": "/home/pal/palworld-wine-server", "wine_path": "/usr/bin/wine64"}
    WineMigrationService(client, instance).activate(migration)
    encoded = shlex.split(client.commands[-1])[2]
    script = base64.b64decode(encoded).decode()
    assert "systemctl start palworld" in script
    assert "systemctl stop palworld-wine-12345678" in script
