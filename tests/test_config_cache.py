from palworld_console.config_cache import ConfigCacheRepository
from palworld_console.models import ConfigSyncResult


def test_config_snapshot_and_draft_are_persistent_and_redacted(tmp_path):
    repository = ConfigCacheRepository(tmp_path)
    result = ConfigSyncResult({"ServerName": "测试服", "PublicPort": 8211, "AdminPassword": "admin-secret", "ServerPassword": "player-secret"}, "/srv/config.ini", "服务器读取", False, "2026-08-18T17:00:00")
    snapshot = repository.save_snapshot("one", result)
    draft = repository.save_draft("one", {**result.values, "PublicPort": 9001}, snapshot.content_hash)
    stored = (tmp_path / "config-cache" / "one" / "snapshot.json").read_text(encoding="utf-8") + (tmp_path / "config-cache" / "one" / "draft.json").read_text(encoding="utf-8")
    assert "admin-secret" not in stored and "player-secret" not in stored
    assert repository.load_snapshot("one").values["ServerName"] == "测试服"
    assert repository.load_draft("one").values["PublicPort"] == 9001
    assert draft.base_hash == snapshot.content_hash


def test_config_hash_ignores_secrets_but_detects_server_changes(tmp_path):
    repository = ConfigCacheRepository(tmp_path)
    first = {"ServerName": "A", "AdminPassword": "one"}
    secret_only = {"ServerName": "A", "AdminPassword": "two"}
    changed = {"ServerName": "B", "AdminPassword": "two"}
    assert repository.hash_values(first) == repository.hash_values(secret_only)
    assert repository.hash_values(first) != repository.hash_values(changed)

