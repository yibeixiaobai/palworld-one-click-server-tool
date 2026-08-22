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


def test_unique_save_name_links_platform_placeholder_without_deleting_history(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    repository.overlay_online("server-1", [PlayerRecord(name="江小白", account_name="江小白", user_id="steam_765", player_uid="")])
    repository.upsert_save_snapshot("server-1", {"players": [{"player_uid": "1050661243", "nickname": "江小白", "level": 9}], "guilds": []})
    groups = repository.list_identity_groups("server-1")
    assert len(groups) == 1
    assert set(groups[0].aliases) == {"steam_765", "1050661243"}
    assert groups[0].role_uids == ("1050661243",)
    assert len(repository.list_players("server-1")) == 2
    repository.close()


def test_first_combined_sync_overlays_online_state_on_real_save_uid(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    repository.upsert_save_snapshot("server-1", {"players": [{"player_uid": "1050661243", "nickname": "江小白", "level": 9}], "guilds": []})
    repository.overlay_online("server-1", [PlayerRecord(name="江小白", user_id="steam_765", level=10)])
    groups = repository.list_identity_groups("server-1")
    assert len(groups) == 1
    assert groups[0].primary.player_uid == "1050661243"
    assert groups[0].primary.online is True
    assert groups[0].primary.user_id == "steam_765"
    assert groups[0].role_uids == ("1050661243",)
    repository.close()


def test_repository_startup_migrates_existing_duplicate_aliases(tmp_path):
    path = tmp_path / "players.db"
    repository = PlayerRepository(path)
    now = repository._now()
    repository.connection.execute("INSERT INTO players(instance_id,player_uid,nickname,first_seen,last_seen,save_status) VALUES(?,?,?,?,?,?)", ("server-1", "1050661243", "江小白", now, now, "active"))
    repository.connection.execute("INSERT INTO players(instance_id,player_uid,user_id,account_name,nickname,first_seen,last_seen,save_status) VALUES(?,?,?,?,?,?,?,?)", ("server-1", "steam_765", "steam_765", "江小白", "江小白", now, now, "missing"))
    repository.connection.execute("INSERT INTO player_aliases(instance_id,canonical_key,player_uid,user_id,first_seen,last_seen) VALUES(?,?,?,?,?,?)", ("server-1", "user:steam_765", "steam_765", "steam_765", now, now))
    repository.connection.commit(); repository.close()
    reopened = PlayerRepository(path)
    groups = reopened.list_identity_groups("server-1")
    assert len(groups) == 1
    assert set(groups[0].aliases) == {"1050661243", "steam_765"}
    assert groups[0].role_uids == ("1050661243",)
    reopened.close()


def test_ambiguous_duplicate_names_are_not_permanently_linked(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    repository.overlay_online("server-1", [PlayerRecord(name="Same", user_id="steam_1"), PlayerRecord(name="Same", user_id="steam_2")])
    repository.upsert_save_snapshot("server-1", {"players": [{"player_uid": "100", "nickname": "Same"}], "guilds": []})
    assert len(repository.list_identity_groups("server-1")) == 3
    repository.close()


def test_player_detail_persists_complete_guild_base_pal_and_inventory_snapshot(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    containers = {
        key: ([{"ContainerId": f"container-{index}", "SlotIndex": 0, "ItemId": "wood", "StackCount": index + 1, "data_status": "complete"}] if index % 2 == 0 else [])
        for index, key in enumerate(("CommonContainerId", "DropSlotContainerId", "EssentialContainerId", "FoodEquipContainerId", "PlayerEquipArmorContainerId", "WeaponLoadOutContainerId"))
    }
    payload = {
        "players": [{
            "player_uid": "200", "nickname": "Alice", "level": 20, "exp": 3000,
            "inventory_status": "complete", "inventory_containers": [{"key": key, "count": len(items), "data_status": "complete"} for key, items in containers.items()],
            "pals": [{"individual_id": "pal-1", "type": "SheepBall", "level": 10, "active_skills": ["FireBall"], "passive_skills": ["Lucky"], "data_status": "complete", "stable_id_valid": True}],
            "items": containers,
        }],
        "guilds": [{"guild_id": "guild-1", "name": "Builders", "admin_player_uid": "200", "base_camp_level": 8, "players": [{"player_uid": "200", "nickname": "Alice"}, {"player_uid": "201", "nickname": "Bob"}], "data_status": "complete"}],
        "bases": [{"base_id": "base-1", "name": "主基地", "guild_id": "guild-1", "position": {"x": 1, "y": 2, "z": 3}, "worker_container_id": "workers-1", "worker_pal_ids": ["pal-1"], "worker_pals": [{"individual_id": "pal-1", "type": "SheepBall"}], "container_ids": ["items-1"], "data_status": "complete"}],
    }
    assert repository.upsert_save_snapshot("server-1", payload) == 1
    assert repository.upsert_save_snapshot("server-1", payload) == 1
    detail = repository.player_detail("server-1", "200")
    assert len(detail["pals"]) == 1
    assert len(detail["items"]) == 3
    assert detail["guild"]["name"] == "Builders"
    assert [member["player_uid"] for member in detail["guild_members"]] == ["200", "201"]
    assert detail["bases"][0]["worker_pal_ids"] == ["pal-1"]
    assert len(detail["inventory_containers"]) == 6
    assert detail["completeness"] == {"pals": "complete", "inventory": "complete", "guild": "complete", "bases": "complete"}
    repository.close()


def test_player_repository_migrates_legacy_pals_table_without_losing_rows(tmp_path):
    path = tmp_path / "players.db"
    connection = __import__("sqlite3").connect(path)
    connection.execute("CREATE TABLE pals(instance_id TEXT NOT NULL,player_uid TEXT NOT NULL,pal_index INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(instance_id,player_uid,pal_index))")
    connection.execute("INSERT INTO pals VALUES(?,?,?,?)", ("server-1", "200", 0, json.dumps({"individual_id": "legacy-pal"})))
    connection.commit(); connection.close()
    repository = PlayerRepository(path)
    columns = {row["name"] for row in repository.connection.execute("PRAGMA table_info(pals)")}
    assert "individual_id" in columns
    assert repository.connection.execute("SELECT COUNT(*) FROM pals").fetchone()[0] == 1
    repository.close()


def test_player_repository_purge_player_removes_all_role_details(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    payload = {"players": [{"player_uid": "200", "nickname": "Alice", "pals": [{"individual_id": "pal-1"}], "items": {"CommonContainerId": [{"SlotIndex": 0, "ItemId": "wood"}]}}], "guilds": [{"guild_id": "g1", "players": [{"player_uid": "200"}]}], "bases": []}
    repository.upsert_save_snapshot("server-1", payload)
    repository.audit_player("server-1", "200", "测试")
    counts = repository.purge_player("server-1", "200")
    assert counts["players"] == 1
    assert repository.player_detail("server-1", "200") == {}
    assert repository.connection.execute("SELECT COUNT(*) FROM player_audit_events WHERE player_uid='200'").fetchone()[0] == 0
    repository.close()


def test_player_repository_purge_instance_clears_world_cache(tmp_path):
    repository = PlayerRepository(tmp_path / "players.db")
    repository.upsert_save_snapshot("server-1", {"players": [{"player_uid": "200"}], "guilds": [{"guild_id": "g1", "players": []}], "bases": [{"base_id": "b1", "guild_id": "g1"}]})
    repository.purge_instance("server-1")
    assert repository.list_players("server-1") == []
    assert repository.connection.execute("SELECT COUNT(*) FROM guilds WHERE instance_id='server-1'").fetchone()[0] == 0
    repository.close()
