from pathlib import Path
import socket

import pytest

from palworld_console.management import AutomationService, HostTaskDeployer, RconClient, SaveGameService, SaveTransaction, WhitelistService
from palworld_console.models import ScheduleDefinition


def test_rcon_packet_and_partial_receive():
    packet = RconClient._packet(7, RconClient.EXEC, "Info")
    assert int.from_bytes(packet[:4], "little", signed=True) == len(packet) - 4

    left, right = socket.socketpair()
    try:
        right.sendall(packet[:3]); right.sendall(packet[3:])
        request_id, packet_type, body = RconClient._receive(left)
        assert (request_id, packet_type, body) == (7, RconClient.EXEC, "Info")
    finally:
        left.close(); right.close()


def test_schedule_validation_and_systemd_timer():
    task = ScheduleDefinition(name="每日备份", action="backup", schedule="04:00", retention=14)
    assert AutomationService.validate(task.__dict__).schedule == "04:00"
    service, timer = HostTaskDeployer.systemd_units("instance-1", task, "/opt/palworld/task.sh")
    assert "NoNewPrivileges=true" in service
    assert "OnCalendar=*-*-* 04:00:00" in timer
    with pytest.raises(ValueError):
        AutomationService.validate({**task.__dict__, "schedule": "25:00"})


def test_whitelist_is_deduplicated_and_detects_unknown_players():
    entries = [
        {"player_uid": "u1", "player_name": "One"},
        {"player_uid": "u1", "player_name": "Duplicate"},
        {"player_uid": "u2", "enabled": False},
    ]
    assert [item.player_uid for item in WhitelistService.normalize(entries)] == ["u1", "u2"]
    assert WhitelistService.unauthorized(entries, ["u1", "u2", "u3"]) == ["u2", "u3"]


def test_scalar_tree_editing():
    document = {"players": [{"level": 3, "name": "Pal"}]}
    flattened = SaveGameService.flatten(document)
    assert flattened["players[0].level"] == 3
    SaveGameService.set_path(document, "players[0].level", 10)
    assert document["players"][0]["level"] == 10


def test_plm_save_is_rejected_as_read_only_when_plugin_missing(tmp_path: Path):
    save = tmp_path / "Level.sav"
    save.write_bytes(b"\0" * 8 + b"PlM" + b"\x31" + b"payload")
    class MissingPlugin:
        def probe(self): return False, "not installed"
    with pytest.raises(RuntimeError, match="只读状态"):
        SaveGameService(MissingPlugin()).load(save)


def test_local_save_transaction_rolls_back_when_health_fails(tmp_path: Path):
    save = tmp_path / "Level.sav"; save.write_bytes(b"original")
    events = []

    class FakeService(SaveGameService):
        def load(self, path): return {"data": path.read_bytes()}
        @staticmethod
        def _write_document(document, path): path.write_bytes(document["data"])
        def validate(self, path):
            from palworld_console.models import SaveValidationResult
            return SaveValidationResult(True)

    def mutate(document): document["data"] = b"changed"
    with pytest.raises(RuntimeError, match="健康检查失败"):
        SaveTransaction(FakeService()).execute_local(save, tmp_path / "backups", mutate, [], lambda: events.append("stop"), lambda: events.append("start"), lambda: False)
    assert save.read_bytes() == b"original"
    assert events == ["stop", "start", "start"]
