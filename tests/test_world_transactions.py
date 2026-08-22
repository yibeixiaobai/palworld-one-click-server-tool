from pathlib import Path

import pytest

from palworld_console.world_transactions import WorldDirectoryTransaction


def test_local_world_cleanup_replaces_directory_and_keeps_backup(tmp_path, monkeypatch):
    world = tmp_path / "WORLD"
    world.mkdir(); (world / "Level.sav").write_bytes(b"old"); (world / "Players").mkdir(); (world / "Players" / "A.sav").write_bytes(b"a")
    backup = tmp_path / "world.pwcbackup"; backup.write_bytes(b"backup")
    monkeypatch.setattr(WorldDirectoryTransaction, "_validated_backup", staticmethod(lambda _callback: backup))
    events = []

    def build(source, output):
        import shutil
        shutil.copytree(source, output)
        (output / "Players" / "A.sav").unlink()
        (output / "Level.sav").write_bytes(b"new")
        return {"counts": {"players": 1}}

    result = WorldDirectoryTransaction().execute_local(world, build, lambda: backup, lambda: events.append("stop"), lambda: events.append("start"), lambda: True)
    assert (world / "Level.sav").read_bytes() == b"new"
    assert not (world / "Players" / "A.sav").exists()
    assert result.backup_path == str(backup)
    assert events == ["stop", "start"]


def test_local_world_cleanup_restores_original_when_health_fails(tmp_path, monkeypatch):
    world = tmp_path / "WORLD"
    world.mkdir(); (world / "Level.sav").write_bytes(b"old")
    backup = tmp_path / "world.pwcbackup"; backup.write_bytes(b"backup")
    monkeypatch.setattr(WorldDirectoryTransaction, "_validated_backup", staticmethod(lambda _callback: backup))

    def build(source, output):
        import shutil
        shutil.copytree(source, output); (output / "Level.sav").write_bytes(b"new"); return {}

    with pytest.raises(RuntimeError, match="已恢复原世界"):
        WorldDirectoryTransaction().execute_local(world, build, lambda: backup, lambda: None, lambda: None, lambda: False)
    assert (world / "Level.sav").read_bytes() == b"old"
