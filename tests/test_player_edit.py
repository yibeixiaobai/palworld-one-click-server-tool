import pytest

from palworld_console.player_edit import PlayerEditSession


def test_player_edit_sessions_are_isolated_by_role_uid():
    first = PlayerEditSession("instance-1", "uid-a")
    second = PlayerEditSession("instance-1", "uid-b")
    first.stage("players[0].level", 10, "12", "等级", "player", "uid-a")
    assert first.preview() == ["等级：10 -> 12"]
    assert second.preview() == []
    assert first.value_for("players[0].level", 10) == 12
    assert second.value_for("players[0].level", 10) == 10


def test_player_edit_requires_stable_identity_in_callers_but_tracks_object_id():
    session = PlayerEditSession("instance-1", "uid-a")
    session.stage("players[0].pals[0].level", 4, 7, "帕鲁等级", "pal", "guid-1", "高")
    change = session.changes["players[0].pals[0].level"]
    assert change.object_id == "guid-1"
    assert change.object_type == "pal"
    assert change.risk == "高"


def test_player_edit_rejects_unknown_or_invalid_fields():
    session = PlayerEditSession("instance-1", "uid-a")
    with pytest.raises(ValueError):
        session.stage("players[0].unknown", 1, 2, "未知字段", "player", "uid-a")
