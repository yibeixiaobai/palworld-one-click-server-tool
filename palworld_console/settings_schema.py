from __future__ import annotations

from .models import SettingDefinition


def _s(key, label, category, value_type, default, minimum=None, maximum=None, description=""):
    return SettingDefinition(key, label, category, value_type, default, minimum, maximum, True, description)


SETTING_DEFINITIONS = (
    _s("ServerName", "服务器名称", "基础信息", "string", "Palworld Server"),
    _s("ServerDescription", "服务器描述", "基础信息", "string", ""),
    _s("ServerPassword", "玩家密码", "基础信息", "password", ""),
    _s("ServerPlayerMaxNum", "最大玩家数", "基础信息", "int", 32, 1, 32),
    _s("CoopPlayerMaxNum", "合作队伍人数", "基础信息", "int", 4, 1, 32),
    _s("PublicPort", "游戏端口", "基础信息", "int", 8211, 1, 65535),
    _s("RESTAPIEnabled", "启用 REST API", "管理接口", "bool", True),
    _s("RESTAPIPort", "REST API 端口", "管理接口", "int", 8212, 1, 65535),
    _s("Difficulty", "难度模式", "难度与世界", "string", "None"),
    _s("DayTimeSpeedRate", "白天速度", "难度与世界", "float", 1.0, 0.1, 5.0),
    _s("NightTimeSpeedRate", "夜晚速度", "难度与世界", "float", 1.0, 0.1, 5.0),
    _s("ExpRate", "经验倍率", "经验与捕获", "float", 1.0, 0.1, 20.0),
    _s("PalCaptureRate", "帕鲁捕获倍率", "经验与捕获", "float", 1.0, 0.1, 20.0),
    _s("PalSpawnNumRate", "帕鲁刷新倍率", "经验与捕获", "float", 1.0, 0.1, 10.0),
    _s("PalDamageRateAttack", "帕鲁攻击伤害倍率", "战斗", "float", 1.0, 0.1, 10.0),
    _s("PalDamageRateDefense", "帕鲁承伤倍率", "战斗", "float", 1.0, 0.1, 10.0),
    _s("PlayerDamageRateAttack", "玩家攻击伤害倍率", "战斗", "float", 1.0, 0.1, 10.0),
    _s("PlayerDamageRateDefense", "玩家承伤倍率", "战斗", "float", 1.0, 0.1, 10.0),
    _s("PlayerStomachDecreaceRate", "玩家饱食消耗倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PlayerStaminaDecreaceRate", "玩家耐力消耗倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PalStomachDecreaceRate", "帕鲁饱食消耗倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PalStaminaDecreaceRate", "帕鲁耐力消耗倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PalAutoHPRegeneRate", "帕鲁生命恢复倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PlayerAutoHPRegeneRate", "玩家生命恢复倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PlayerAutoHpRegeneRateInSleep", "玩家睡眠生命恢复倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("PalAutoHpRegeneRateInSleep", "帕鲁睡眠生命恢复倍率", "生存", "float", 1.0, 0.0, 10.0),
    _s("BuildObjectDamageRate", "建筑承伤倍率", "建筑与基地", "float", 1.0, 0.0, 10.0),
    _s("BuildObjectDeteriorationDamageRate", "建筑劣化倍率", "建筑与基地", "float", 1.0, 0.0, 10.0),
    _s("WorkSpeedRate", "工作速度倍率", "建筑与基地", "float", 1.0, 0.1, 10.0),
    _s("BaseCampMaxNumInGuild", "每公会基地上限", "建筑与基地", "int", 4, 1, 64),
    _s("MaxBuildingLimitNum", "最大建筑数量", "建筑与基地", "int", 0, 0, 100000),
    _s("CollectionDropRate", "采集掉落倍率", "采集与掉落", "float", 1.0, 0.1, 20.0),
    _s("CollectionObjectHpRate", "采集物生命倍率", "采集与掉落", "float", 1.0, 0.1, 10.0),
    _s("CollectionObjectRespawnSpeedRate", "采集物刷新速度", "采集与掉落", "float", 1.0, 0.1, 10.0),
    _s("EnemyDropItemRate", "敌人掉落倍率", "采集与掉落", "float", 1.0, 0.1, 20.0),
    _s("DropItemMaxNum", "世界掉落物上限", "采集与掉落", "int", 3000, 0, 100000),
    _s("DropItemAliveMaxHours", "掉落物保留时间（小时）", "采集与掉落", "float", 1.0, 0.0, 240.0),
    _s("ItemWeightRate", "物品重量倍率", "采集与掉落", "float", 1.0, 0.0, 10.0),
    _s("PalEggDefaultHatchingTime", "巨大蛋孵化时间（小时）", "生育与孵化", "float", 72.0, 0.0, 240.0),
    _s("DeathPenalty", "死亡惩罚", "玩家规则", "string", "All"),
    _s("bEnablePlayerToPlayerDamage", "启用玩家 PvP", "玩家规则", "bool", False),
    _s("bEnableFriendlyFire", "启用友军伤害", "玩家规则", "bool", False),
    _s("bEnableFastTravel", "启用快速传送", "玩家规则", "bool", True),
    _s("bExistPlayerAfterLogout", "离线后保留玩家", "玩家规则", "bool", False),
    _s("bEnableNonLoginPenalty", "启用离线惩罚", "玩家规则", "bool", True),
    _s("bCanPickupOtherGuildDeathPenaltyDrop", "可拾取其他公会死亡掉落", "玩家规则", "bool", False),
    _s("bEnableInvaderEnemy", "启用入侵事件", "世界事件", "bool", True),
    _s("bActiveUNKO", "启用随机帕鲁竞技场", "世界事件", "bool", False),
    _s("GuildPlayerMaxNum", "公会最大成员数", "公会", "int", 20, 1, 100),
    _s("BaseCampMaxNum", "世界基地总上限", "公会", "int", 128, 1, 1024),
    _s("BaseCampWorkerMaxNum", "单基地工作帕鲁上限", "公会", "int", 15, 1, 100),
    _s("AutoResetGuildNoOnlinePlayers", "自动清理离线公会", "公会", "bool", False),
    _s("AutoResetGuildTimeNoOnlinePlayers", "公会离线清理时间（小时）", "公会", "float", 72.0, 1.0, 720.0),
    _s("GuildJoinRestrictionTimeDays", "退出公会后重入限制（天）", "公会", "float", 0.0, 0.0, 30.0),
    _s("CrossplayPlatforms", "跨平台", "网络与平台", "raw", "(Steam,Xbox,PS5,Mac)"),
    _s("bIsMultiplay", "启用多人游戏", "网络与平台", "bool", True),
    _s("bIsPvP", "启用 PvP 世界", "网络与平台", "bool", False),
    _s("bShowPlayerList", "显示玩家列表", "网络与平台", "bool", False),
    _s("ChatPostLimitPerMinute", "每分钟聊天消息上限", "网络与平台", "int", 10, 1, 120),
    _s("RCONEnabled", "启用 RCON", "管理接口", "bool", False),
    _s("RCONPort", "RCON 端口", "管理接口", "int", 25575, 1, 65535),
    _s("bAllowGlobalPalboxExport", "允许全局帕鲁终端导出", "管理接口", "bool", True),
    _s("bAllowGlobalPalboxImport", "允许全局帕鲁终端导入", "管理接口", "bool", False),
)

SETTING_BY_KEY = {item.key: item for item in SETTING_DEFINITIONS}
CATEGORIES = tuple(dict.fromkeys(item.category for item in SETTING_DEFINITIONS))

PRESETS = {
    "官方默认": {item.key: item.default for item in SETTING_DEFINITIONS},
    "休闲": {"ExpRate": 2.0, "PalCaptureRate": 1.5, "CollectionDropRate": 2.0, "PalEggDefaultHatchingTime": 2.0, "DeathPenalty": "None"},
    "高倍率": {"ExpRate": 5.0, "PalCaptureRate": 3.0, "CollectionDropRate": 5.0, "EnemyDropItemRate": 3.0, "PalEggDefaultHatchingTime": 0.5},
    "硬核 PvP": {"bIsPvP": True, "bEnablePlayerToPlayerDamage": True, "DeathPenalty": "All", "ExpRate": 0.75, "CollectionDropRate": 0.75},
}
