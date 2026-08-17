from pathlib import Path

from palworld_console.config_ini import coerce_setting_value
from palworld_console.models import ServerInstance
from palworld_console.services import PalworldRestClient, ServerDiagnostics, SSHTunnelManager
from palworld_console.settings_schema import CATEGORIES, PRESETS, SETTING_BY_KEY, SETTING_DEFINITIONS


def test_setting_registry_has_unique_keys_categories_and_typed_values():
    assert len(SETTING_BY_KEY) == len(SETTING_DEFINITIONS)
    assert {item.category for item in SETTING_DEFINITIONS} == set(CATEGORIES)
    assert coerce_setting_value("GuildPlayerMaxNum", "30") == 30
    assert coerce_setting_value("PalCaptureRate", "2.5") == 2.5
    assert coerce_setting_value("bIsPvP", "true") is True
    assert PRESETS["高倍率"]["ExpRate"] > 1


def test_player_records_tolerate_official_and_legacy_field_names():
    players = PalworldRestClient.player_records({"players": [{
        "name": "Builder", "accountName": "steam-user", "userId": "uid-1", "playerUId": "puid-1",
        "level": 42, "ping": 18.5, "ip": "203.0.113.8", "location": {"x": 10, "y": 20},
        "buildingCount": 7, "guildId": "guild-1",
    }]})
    assert len(players) == 1
    assert players[0].name == "Builder"
    assert players[0].user_id == "uid-1"
    assert players[0].location_x == 10
    assert players[0].guild_id == "guild-1"


def test_game_data_is_aggregated_into_read_only_guild_summaries():
    players = PalworldRestClient.player_records({"players": [
        {"name": "A", "userId": "1", "level": 20, "guildId": "g1"},
        {"name": "B", "userId": "2", "level": 40, "guildId": "g1"},
    ]})
    payload = {"actors": [
        {"GuildID": "g1", "GuildName": "Builders", "Type": "BaseCamp"},
        {"GuildID": "g1", "GuildName": "Builders", "Type": "PalCharacter"},
    ]}
    guilds = PalworldRestClient.guild_summaries(payload, players)
    assert guilds[0].name == "Builders"
    assert guilds[0].member_count == 2
    assert guilds[0].average_level == 30
    assert guilds[0].base_count == 1
    assert guilds[0].pal_count == 1


class DiagnosticClient:
    def run(self, command):
        if command.startswith("systemctl is-active"):
            return 0, "active\n4321\n", ""
        if command.startswith("ps -p"):
            return 0, "ubuntu 12.5 25.0 524288\n", ""
        if command.startswith("ss -lun"):
            return 0, "0.0.0.0:8211\n__TCP__\n0.0.0.0:8212\n", ""
        if command.startswith("df -h"):
            return 0, "/dev/vda 40G 20G 20G 50% /\n", ""
        if command.startswith("sudo -n ufw"):
            return 0, "Status: active\n8211/udp ALLOW Anywhere\n", ""
        if command.startswith("journalctl"):
            return 0, "Running Palworld dedicated server on :8211\n", ""
        return 0, "", ""


class DiagnosticRest:
    def health(self): return {"version": "v1", "worldguid": "world-1", "maxplayernum": 32}
    def metrics(self): return {"serverfps": 60, "serverframetime": 16.6, "currentplayernum": 2, "uptime": 3600}


def test_diagnostics_requires_non_root_process_and_real_listener():
    instance = ServerInstance(kind="remote", host="example.com", game_port=8211, remote_profile={"service_name": "palworld", "game_port": 8211, "rest_port": 8212})
    snapshot = ServerDiagnostics.collect_remote(DiagnosticClient(), instance, DiagnosticRest())
    assert snapshot.healthy is True
    assert snapshot.process_user == "ubuntu"
    assert snapshot.game_endpoint.listening is True
    assert snapshot.rest_ok is True
    assert snapshot.player_count == 2


def test_ssh_tunnel_allocates_local_port_and_closes():
    class Transport:
        def is_active(self): return True
    class Ssh:
        def get_transport(self): return Transport()
        def close(self): pass
    class Client:
        def _connect(self): return Ssh()
    tunnel = SSHTunnelManager(Client())
    port = tunnel.start(remote_port=8212)
    assert port > 0
    assert tunnel.base_url.endswith(str(port))
    tunnel.close()
    assert tunnel.local_port == 0
