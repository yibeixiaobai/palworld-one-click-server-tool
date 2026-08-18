import json

from palworld_console.models import PlayerRecord, ServerInstance
from palworld_console.player_store import PlayerIdentityService, PlayerRepository


def test_player_repository_migrates_history_and_retains_missing_players(tmp_path):
    instance = ServerInstance(id="server-1", player_history={"100": {"first_seen": "2026-01-01T00:00:00", "last_seen": "2026-01-02T00:00:00", "masked_ips": ["203.0.113.*"]}})
    repository = PlayerRepository(tmp_path / "players.db")
    assert repository.migrate_instance_history(instance) == 1

    payload = {"players": [{"player_uid": "200", "nickname": "Alice", "level": 10, "exp": 500, "pals": [{"type": "SheepBall"}], "items": {"CommonContainerId": [{"SlotIndex": 0, "ItemId": "wood", "StackCount": 12}]}}], "guilds": []}
    assert repository.upsert_save_snapshot(instance.id, payload) == 1
    players = {player.player_uid: player for player in repository.list_players(instance.id)}
    assert players["100"].save_status == "missing"
    assert players["200"].level == 10
    assert len(repository.player_detail(instance.id, "200")["pals"]) == 1
    repository.close()


def test_online_overlay_persists_only_masked_ip_and_is_idempotent(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    online = [PlayerRecord(name="Alice", user_id="steam-1", player_uid="200", level=11, ip="203.0.113.42")]
    repository.overlay_online("server-1", online)
    repository.overlay_online("server-1", online)
    detail = repository.player_detail("server-1", "200")["player"]
    assert json.loads(detail["masked_ips"]) == ["203.0.113.*"]
    assert "203.0.113.42" not in (tmp_path / "players.db").read_bytes().decode("latin1")
    assert repository.list_players("server-1")[0].online is True
    repository.close()


def test_identity_service_deduplicates_exact_uid_and_groups_shared_user_id(tmp_path):
    duplicate = [PlayerRecord(name="Alice", user_id="steam-1", player_uid="100", level=5), PlayerRecord(name="Alice", user_id="steam-1", player_uid="100", level=6)]
    assert len(PlayerIdentityService.deduplicate_online(duplicate)) == 1
    repository = PlayerRepository(tmp_path / "players.db")
    repository.overlay_online("server-1", [PlayerRecord(name="Alice A", user_id="steam-1", player_uid="100"), PlayerRecord(name="Alice B", user_id="steam-1", player_uid="200")])
    groups = repository.list_identity_groups("server-1")
    assert len(groups) == 1
    assert set(groups[0].aliases) == {"100", "200"}
    repository.close()
