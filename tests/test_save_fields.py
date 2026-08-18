import pytest

from palworld_console.save_fields import display_field, resolve_path, validate_value


def test_context_registry_distinguishes_player_and_pal_level():
    player = display_field("players[0].level", 10)
    pal = display_field("players[0].pals[0].level", 12)
    assert player["label"] == "玩家等级"
    assert pal["label"] == "帕鲁等级"
    assert "players[0].level" in player["tooltip"]
    assert player["status"] == "可编辑"


def test_unknown_field_is_read_only_and_cannot_be_validated():
    field = resolve_path("players[0].UnknownGameValue")
    assert field.label == "未知字段"
    assert field.writable is False
    with pytest.raises(ValueError, match="只读字段"):
        validate_value(field, "1")


def test_inventory_quantity_has_chinese_meaning_and_range():
    field = resolve_path("players[0].items.CommonContainerId[0].StackCount")
    assert field.label == "物品数量"
    assert validate_value(field, "99") == 99
    with pytest.raises(ValueError):
        validate_value(field, "1000000")
