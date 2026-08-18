from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class SaveFieldDefinition:
    key: str
    label: str
    group: str
    value_type: str
    object_type: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    source: str = "Level.sav"
    writable: bool = False
    read_only_reason: str = "该字段尚未通过真实存档写回验证"
    requires_stop: bool = True
    description: str = ""
    risk: str = "低"

    @property
    def range_text(self) -> str:
        if self.minimum is None and self.maximum is None:
            return self.unit
        low = "-" if self.minimum is None else f"{self.minimum:g}"
        high = "-" if self.maximum is None else f"{self.maximum:g}"
        return f"{low} - {high}{self.unit}"


def _f(object_type: str, key: str, label: str, group: str, value_type: str, **kwargs) -> SaveFieldDefinition:
    return SaveFieldDefinition(key, label, group, value_type, object_type, **kwargs)


FIELDS = (
    _f("player", "player_uid", "玩家 UID", "玩家身份", "string", source="Level.sav / REST", description="当前世界中角色的稳定标识", read_only_reason="身份标识不可修改"),
    _f("player", "nickname", "玩家昵称", "玩家属性", "string", writable=True, description="游戏中显示的角色名称", risk="中"),
    _f("player", "level", "玩家等级", "玩家属性", "int", minimum=1, maximum=65, unit="级", writable=True, description="角色当前等级；与累计经验联动校验", risk="中"),
    _f("player", "exp", "累计经验", "玩家属性", "int", minimum=0, maximum=2147483647, writable=True, description="达到当前等级所需的累计经验", risk="中"),
    _f("player", "hp", "当前生命值", "玩家属性", "int", minimum=0, maximum=2147483647, unit="点", writable=True, risk="中"),
    _f("player", "shield_hp", "当前护盾值", "玩家属性", "int", minimum=0, maximum=2147483647, unit="点", writable=True, risk="中"),
    _f("player", "full_stomach", "当前饱食度", "玩家属性", "float", minimum=0, maximum=1000, unit="点", writable=True, risk="中"),
    _f("player", "status_point", "角色属性点", "玩家属性", "mapping", minimum=0, maximum=9999, unit="点", writable=True, description="仅修改存档内已经存在的属性点项目", risk="高"),
    _f("pal", "individual_id", "帕鲁个体 ID", "帕鲁", "string", description="帕鲁稳定对象 GUID", read_only_reason="稳定身份标识不可修改"),
    _f("pal", "type", "帕鲁种类", "帕鲁", "string", description="游戏内部帕鲁种类标识", read_only_reason="更换种类可能破坏对象关系"),
    _f("pal", "nickname", "帕鲁昵称", "帕鲁", "string", writable=True, risk="中"),
    _f("pal", "level", "帕鲁等级", "帕鲁", "int", minimum=1, maximum=65, unit="级", writable=True, risk="中"),
    _f("pal", "exp", "帕鲁经验", "帕鲁", "int", minimum=0, maximum=2147483647, writable=True, risk="中"),
    _f("pal", "gender", "帕鲁性别", "帕鲁", "enum", description="游戏记录的性别", read_only_reason="性别写回尚未通过真实存档验证"),
    _f("pal", "is_lucky", "幸运帕鲁", "帕鲁", "bool", description="是否为幸运帕鲁", read_only_reason="幸运状态写回尚未通过真实存档验证"),
    _f("pal", "melee", "帕鲁生命个体值", "帕鲁", "int", minimum=0, maximum=100, writable=True, risk="高"),
    _f("pal", "ranged", "帕鲁攻击个体值", "帕鲁", "int", minimum=0, maximum=100, writable=True, risk="高"),
    _f("pal", "defense", "帕鲁防御个体值", "帕鲁", "int", minimum=0, maximum=100, writable=True, risk="高"),
    _f("pal", "workspeed", "帕鲁工作速度", "帕鲁", "int", minimum=0, maximum=255, writable=True, risk="高"),
    _f("pal", "rank", "帕鲁星级", "帕鲁", "int", minimum=1, maximum=5, unit="星", writable=True, risk="高"),
    _f("pal", "skills", "帕鲁被动技能", "帕鲁", "list", writable=True, description="只允许使用存档中已识别的技能 ID", risk="高"),
    _f("inventory", "container_id", "背包容器 ID", "背包", "string", source="Players/*.sav + Level.sav", read_only_reason="容器标识不可修改"),
    _f("inventory", "SlotIndex", "槽位", "背包", "int", source="Players/*.sav + Level.sav", read_only_reason="槽位移动尚未开放"),
    _f("inventory", "ItemId", "物品名称 / ID", "背包", "string", source="Players/*.sav + Level.sav", read_only_reason="首版不创建或替换未知物品"),
    _f("inventory", "StackCount", "物品数量", "背包", "int", minimum=0, maximum=999999, unit="个", source="Players/*.sav + Level.sav", writable=True, risk="高"),
    _f("guild", "guild_id", "公会 ID", "公会与基地", "string", read_only_reason="稳定身份标识不可修改"),
    _f("guild", "name", "公会名称", "公会与基地", "string", read_only_reason="公会改名需先通过完整成员和基地关系写回验证", risk="高"),
    _f("guild", "admin_player_uid", "公会会长", "公会与基地", "string", read_only_reason="会长转移需要完整成员和基地关系校验", risk="高"),
    _f("guild", "players", "公会成员", "公会与基地", "list", read_only_reason="成员变更需要完整关系校验", risk="高"),
    _f("base", "base_id", "基地 ID", "公会与基地", "string", read_only_reason="稳定身份标识不可修改"),
    _f("base", "base_camp_level", "基地等级", "公会与基地", "int", minimum=1, maximum=30, unit="级", read_only_reason="基地写回需先验证容器和工作帕鲁归属", risk="高"),
    _f("base", "guild_id", "基地所属公会", "公会与基地", "string", read_only_reason="归属转移需先验证公会、容器和帕鲁关系", risk="高"),
)

FIELD_BY_OBJECT_KEY = {(field.object_type, field.key.lower()): field for field in FIELDS}
CONTAINER_LABELS = {
    "CommonContainerId": "普通背包", "DropSlotContainerId": "掉落槽", "EssentialContainerId": "重要物品",
    "FoodEquipContainerId": "食物栏", "PlayerEquipArmorContainerId": "装备栏", "WeaponLoadOutContainerId": "武器栏",
}


def definition(object_type: str, key: str) -> SaveFieldDefinition | None:
    return FIELD_BY_OBJECT_KEY.get((object_type, key.lower()))


def path_context(path: str) -> tuple[str, str, str]:
    leaf = re.split(r"\.|\[", path)[-1].rstrip("]")
    if path.startswith("players["):
        player_match = re.match(r"players\[(\d+)\]", path)
        owner = f"玩家 {int(player_match.group(1)) + 1}" if player_match else "玩家"
        if ".pals[" in path:
            pal_match = re.search(r"\.pals\[(\d+)\]", path)
            return "pal", leaf, f"{owner}的帕鲁 {int(pal_match.group(1)) + 1 if pal_match else ''}".strip()
        if ".items." in path:
            container = path.split(".items.", 1)[1].split("[", 1)[0]
            return "inventory", leaf, f"{owner} · {CONTAINER_LABELS.get(container, container)}"
        if ".status_point." in path:
            point_name = path.split(".status_point.", 1)[1]
            return "player", "status_point", f"{owner} · 属性点 {point_name}"
        return "player", leaf, owner
    if path.startswith("guilds["):
        return "guild", leaf, "公会"
    if path.startswith("bases["):
        return "base", leaf, "基地"
    return "unknown", leaf, "未知对象"


def resolve_path(path: str) -> SaveFieldDefinition:
    object_type, key, _label = path_context(path)
    resolved = definition(object_type, key)
    if resolved:
        return resolved
    return SaveFieldDefinition(key, "未知字段", "未识别", "unknown", object_type, source="存档原始数据", writable=False,
        read_only_reason="未知字段仅供查看，不能通过通用路径编辑器写回", description="程序尚未识别该游戏字段；内部路径仅用于诊断。", risk="高")


def display_field(path: str, value: Any) -> dict[str, Any]:
    object_type, _key, object_label = path_context(path)
    field = resolve_path(path)
    return {"object_type": object_type, "object": object_label, "label": field.label, "current": value,
        "source": field.source, "status": "可编辑" if field.writable else f"只读：{field.read_only_reason}", "risk": field.risk,
        "tooltip": f"{field.description or field.label}\n内部路径：{path}\n有效范围：{field.range_text or '按游戏存档格式'}", "definition": field}


def validate_value(field: SaveFieldDefinition, value: Any) -> Any:
    if not field.writable:
        raise ValueError(f"{field.label}为只读字段：{field.read_only_reason}")
    converted = int(value) if field.value_type in {"int", "mapping"} else float(value) if field.value_type == "float" else value
    if isinstance(converted, (int, float)):
        if field.minimum is not None and converted < field.minimum:
            raise ValueError(f"{field.label}不能小于 {field.minimum:g}")
        if field.maximum is not None and converted > field.maximum:
            raise ValueError(f"{field.label}不能大于 {field.maximum:g}")
    return converted
