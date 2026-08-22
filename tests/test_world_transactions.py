from pathlib import Path

import pytest

from palworld_console.world_transactions import WorldDirectoryTransaction


@pytest.mark.parametrize(
    ("world", "platform", "expected"),
    (
        ("/home/ubuntu/palworld-server/Pal/Saved/SaveGames/imported-world", "linux", "/home/ubuntu/palworld-server/Pal/Saved/SaveGames"),
        ("/home/ubuntu/palworld-server/Pal/Saved/SaveGames/0/WORLD", "linux", "/home/ubuntu/palworld-server/Pal/Saved/SaveGames"),
        (r"D:\PalServer\Pal\Saved\SaveGames\imported-world", "windows", r"D:\PalServer\Pal\Saved\SaveGames"),
        (r"D:\PalServer\Pal\Saved\SaveGames\0\WORLD", "windows", r"D:\PalServer\Pal\Saved\SaveGames"),
    ),
)
def test_remote_savegames_root_supports_direct_and_nested_world_layouts(world, platform, expected):
    assert WorldDirectoryTransaction._remote_savegames_root(world, platform) == expected


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


def test_local_world_reset_replaces_entire_savegames_directory(tmp_path, monkeypatch):
    savegames = tmp_path / "Saved" / "SaveGames"
    world = savegames / "0" / "WORLD"
    other_world = savegames / "0" / "OTHER"
    world.mkdir(parents=True); other_world.mkdir()
    (world / "Level.sav").write_bytes(b"old")
    (other_world / "Level.sav").write_bytes(b"other")
    (savegames / "UserOption.sav").write_bytes(b"options")
    backup = tmp_path / "world.pwcbackup"; backup.write_bytes(b"backup")
    monkeypatch.setattr(WorldDirectoryTransaction, "_validated_backup", staticmethod(lambda _callback: backup))
    events = []

    def start():
        events.append("start")
        generated = savegames / "0" / "NEW"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "Level.sav").write_bytes(b"new")

    result = WorldDirectoryTransaction().reset_local(world, lambda: backup, lambda: events.append("stop"), start, lambda: True, timeout=0.1)

    assert result.world_path == str(savegames / "0" / "NEW")
    assert (savegames / "0" / "NEW" / "Level.sav").read_bytes() == b"new"
    assert not (savegames / "0" / "WORLD").exists()
    assert not (savegames / "0" / "OTHER").exists()
    assert not (savegames / "UserOption.sav").exists()
    assert events == ["stop", "start"]


def test_local_world_reset_restores_all_savegames_when_new_world_fails_health_check(tmp_path, monkeypatch):
    savegames = tmp_path / "Saved" / "SaveGames"
    world = savegames / "0" / "WORLD"
    world.mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"old")
    (savegames / "WorldOption.sav").write_bytes(b"old-options")
    backup = tmp_path / "world.pwcbackup"; backup.write_bytes(b"backup")
    monkeypatch.setattr(WorldDirectoryTransaction, "_validated_backup", staticmethod(lambda _callback: backup))

    def start():
        if not savegames.exists():
            generated = savegames / "0" / "NEW"
            generated.mkdir(parents=True)
            (generated / "Level.sav").write_bytes(b"new")

    with pytest.raises(RuntimeError, match="已恢复原世界"):
        WorldDirectoryTransaction().reset_local(world, lambda: backup, lambda: None, start, lambda: False, timeout=0.1)

    assert (world / "Level.sav").read_bytes() == b"old"
    assert (savegames / "WorldOption.sav").read_bytes() == b"old-options"
    assert not (savegames / "0" / "NEW").exists()
