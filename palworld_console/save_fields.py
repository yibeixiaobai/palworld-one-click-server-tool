from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SaveFieldDefinition:
    key: str
    label: str
    group: str
    value_type: str
    minimum: float | None = None
    maximum: float | None = None
    source: str = "Level.sav"
    writable: bool = False
    requires_stop: bool = True
    description: str = ""
    risk: str = "低"


FIELDS = (
    SaveFieldDefinition("player_uid", "玩家 UID", "玩家身份", "string", source="Level.sav / REST", description="玩家在当前世界中的稳定标识"),
    SaveFieldDefinition("nickname", "玩家昵称", "玩家基础", "string", writable=True, description="游戏内显示的角色名称", risk="中"),
    SaveFieldDefinition("level", "玩家等级", "玩家基础", "int", 1, 65, writable=True, description="角色当前等级；修改时同时校验经验值", risk="中"),
    SaveFieldDefinition("exp", "玩家经验值", "玩家基础", "int", 0, 2147483647, writable=True, description="角色累计经验值", risk="中"),
    SaveFieldDefinition("hp", "当前生命值", "玩家基础", "int", 0, 2147483647, writable=True),
    SaveFieldDefinition("shield_hp", "当前护盾值", "玩家基础", "int", 0, 2147483647, writable=True),
    SaveFieldDefinition("full_stomach", "饱食度", "玩家基础", "float", 0, 1000, writable=True),
    SaveFieldDefinition("status_point", "属性点", "玩家基础", "mapping", 0, 9999, writable=True, description="游戏存档中已存在的属性点项目", risk="高"),
    SaveFieldDefinition("type", "帕鲁种类", "帕鲁属性", "string", description="帕鲁内部种类标识"),
    SaveFieldDefinition("nickname", "帕鲁昵称", "帕鲁属性", "string", writable=True, risk="中"),
    SaveFieldDefinition("level", "帕鲁等级", "帕鲁属性", "int", 1, 65, writable=True, risk="中"),
    SaveFieldDefinition("exp", "帕鲁经验值", "帕鲁属性", "int", 0, 2147483647, writable=True, risk="中"),
    SaveFieldDefinition("gender", "性别", "帕鲁属性", "enum"),
    SaveFieldDefinition("is_lucky", "幸运帕鲁", "帕鲁属性", "bool"),
    SaveFieldDefinition("workspeed", "工作速度", "帕鲁属性", "int", 0, 255, writable=True, risk="高"),
    SaveFieldDefinition("melee", "生命个体值", "帕鲁属性", "int", 0, 100, writable=True, risk="高"),
    SaveFieldDefinition("ranged", "攻击个体值", "帕鲁属性", "int", 0, 100, writable=True, risk="高"),
    SaveFieldDefinition("defense", "防御个体值", "帕鲁属性", "int", 0, 100, writable=True, risk="高"),
    SaveFieldDefinition("rank", "星级", "帕鲁属性", "int", 1, 5, writable=True, risk="高"),
    SaveFieldDefinition("skills", "被动技能", "帕鲁属性", "list", writable=True, description="只允许修改存档中可识别的技能标识", risk="高"),
    SaveFieldDefinition("ItemId", "物品", "背包与装备", "string", source="Players/*.sav + Level.sav"),
    SaveFieldDefinition("SlotIndex", "槽位", "背包与装备", "int", source="Players/*.sav + Level.sav"),
    SaveFieldDefinition("StackCount", "物品数量", "背包与装备", "int", 0, 999999, source="Players/*.sav + Level.sav", writable=True, risk="高"),
    SaveFieldDefinition("name", "公会名称", "公会", "string", writable=True, risk="高"),
    SaveFieldDefinition("admin_player_uid", "会长 UID", "公会", "string", writable=True, risk="高"),
    SaveFieldDefinition("base_camp_level", "基地等级", "基地", "int", 1, 30, writable=True, risk="高"),
)


FIELD_BY_GROUP_KEY = {(field.group, field.key): field for field in FIELDS}
CONTAINER_LABELS = {
    "CommonContainerId": "普通背包",
    "DropSlotContainerId": "掉落槽",
    "EssentialContainerId": "重要物品",
    "FoodEquipContainerId": "食物袋",
    "PlayerEquipArmorContainerId": "装备栏",
    "WeaponLoadOutContainerId": "武器栏",
}


def definition(group: str, key: str) -> SaveFieldDefinition | None:
    return FIELD_BY_GROUP_KEY.get((group, key))


def validate_value(field: SaveFieldDefinition, value: Any) -> None:
    if not field.writable:
        raise ValueError(f"{field.label}为只读字段")
    if field.value_type == "int":
        value = int(value)
    elif field.value_type == "float":
        value = float(value)
    if isinstance(value, (int, float)):
        if field.minimum is not None and value < field.minimum:
            raise ValueError(f"{field.label}不能小于 {field.minimum:g}")
        if field.maximum is not None and value > field.maximum:
            raise ValueError(f"{field.label}不能大于 {field.maximum:g}")
