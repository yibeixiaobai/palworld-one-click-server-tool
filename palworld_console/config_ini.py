from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import os
from datetime import datetime


OPTION_START_RE = __import__("re").compile(r"OptionSettings\s*=\s*\(")


class RawValue(str):
    pass


BOOL_SETTINGS = {"RESTAPIEnabled", "RCONEnabled", "bEnablePlayerToPlayerDamage", "bEnableFriendlyFire", "bEnableInvaderEnemy", "bActiveUNKO", "AutoResetGuildNoOnlinePlayers", "bIsMultiplay", "bIsPvP", "bEnableFastTravel", "bExistPlayerAfterLogout", "bEnableNonLoginPenalty", "bCanPickupOtherGuildDeathPenaltyDrop", "bShowPlayerList", "bAllowGlobalPalboxExport", "bAllowGlobalPalboxImport"}
INT_SETTINGS = {"PublicPort", "RESTAPIPort", "RCONPort", "ServerPlayerMaxNum", "CoopPlayerMaxNum", "GuildPlayerMaxNum", "BaseCampMaxNum", "BaseCampWorkerMaxNum", "BaseCampMaxNumInGuild", "MaxBuildingLimitNum", "DropItemMaxNum", "ChatPostLimitPerMinute"}
FLOAT_SETTINGS = {"ExpRate", "CollectionDropRate", "PalEggDefaultHatchingTime", "DayTimeSpeedRate", "NightTimeSpeedRate", "PalCaptureRate", "PalSpawnNumRate", "PalDamageRateAttack", "PalDamageRateDefense", "PlayerDamageRateAttack", "PlayerDamageRateDefense", "PlayerStomachDecreaceRate", "PlayerStaminaDecreaceRate", "PalStomachDecreaceRate", "PalStaminaDecreaceRate", "PalAutoHPRegeneRate", "PlayerAutoHPRegeneRate", "PlayerAutoHpRegeneRateInSleep", "PalAutoHpRegeneRateInSleep", "BuildObjectDamageRate", "BuildObjectDeteriorationDamageRate", "WorkSpeedRate", "CollectionObjectHpRate", "CollectionObjectRespawnSpeedRate", "EnemyDropItemRate", "DropItemAliveMaxHours", "ItemWeightRate", "AutoResetGuildTimeNoOnlinePlayers", "GuildJoinRestrictionTimeDays"}
RAW_SETTINGS = {"CrossplayPlatforms"}


def split_assignments(body: str) -> list[str]:
    result, buf, quote, depth = [], [], None, 0
    for char in body:
        if char in "\"'" and (not buf or buf[-1] != "\\"):
            quote = None if quote == char else (char if quote is None else quote)
        if quote is None and char in "([{":
            depth += 1
        elif quote is None and char in ")]}" and depth:
            depth -= 1
        if char == "," and quote is None and depth == 0:
            result.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    if "".join(buf).strip():
        result.append("".join(buf).strip())
    return result


def parse_value(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].replace("\\\"", "\"")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if len(value) >= 2 and value[0] in "([{":
        return RawValue(value)
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def format_value(value) -> str:
    if isinstance(value, RawValue):
        return str(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return '"' + value.replace('"', '\\"') + '"'
    return str(value)


def coerce_setting_value(name: str, value: str):
    text = value.strip()
    if name in BOOL_SETTINGS:
        if text.lower() not in {"true", "false"}:
            raise ValueError(f"{name} 必须是 True 或 False")
        return text.lower() == "true"
    if name in INT_SETTINGS:
        return int(text)
    if name in FLOAT_SETTINGS:
        return float(text)
    if name in RAW_SETTINGS:
        if not (text.startswith("(") and text.endswith(")")):
            raise ValueError(f"{name} 必须使用括号格式，例如 (Steam,Xbox,PS5,Mac)")
        return RawValue(text)
    return value


@dataclass
class PalWorldSettings:
    values: dict[str, object]
    prefix: str = "OptionSettings=("
    suffix: str = ");"
    document: str = ""
    option_span: tuple[int, int] | None = None

    @classmethod
    def load(cls, path: Path) -> "PalWorldSettings":
        return cls.from_text(path.read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, text: str) -> "PalWorldSettings":
        match = OPTION_START_RE.search(text)
        if not match:
            raise ValueError("未找到有效的 OptionSettings=(...) 配置")
        open_pos = text.find("(", match.start(), match.end())
        depth, quote, close_pos = 1, None, None
        escaped = False
        for index in range(open_pos + 1, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote:
                escaped = True
                continue
            if char in "\"'":
                quote = None if quote == char else (char if quote is None else quote)
            elif not quote:
                if char == "(": depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        close_pos = index
                        break
        if close_pos is None or quote:
            raise ValueError("OptionSettings 括号或引号未闭合")
        suffix_end = close_pos + 1
        while suffix_end < len(text) and text[suffix_end] in " \t;\r\n":
            suffix_end += 1
        values: dict[str, object] = {}
        for item in split_assignments(text[open_pos + 1:close_pos]):
            if "=" not in item:
                raise ValueError(f"无法解析配置项: {item}")
            key, value = item.split("=", 1)
            if not key.strip():
                raise ValueError("配置项名称不能为空")
            values[key.strip()] = parse_value(value)
        return cls(values, text[match.start():open_pos + 1], text[close_pos:suffix_end], text, (match.start(), suffix_end))

    def render(self) -> str:
        return self.prefix + ",".join(f"{k}={format_value(v)}" for k, v in self.values.items()) + self.suffix

    def render_document(self) -> str:
        if self.document and self.option_span:
            start, end = self.option_span
            return self.document[:start] + self.render() + self.document[end:]
        return self.render() + "\n"

    def save(self, path: Path, backup_dir: Path | None = None) -> Path | None:
        if not self.values:
            raise ValueError("配置为空，拒绝覆盖文件")
        backup = None
        if path.exists():
            backup_dir = backup_dir or path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.bak"
            shutil.copy2(path, backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(self.render_document(), encoding="utf-8")
        os.replace(temp, path)
        return backup


def settings_path(install_dir: Path, platform: str = "windows") -> Path:
    directory = "LinuxServer" if platform.lower() == "linux" else "WindowsServer"
    return install_dir / "Pal" / "Saved" / "Config" / directory / "PalWorldSettings.ini"


def default_settings_path(install_dir: Path) -> Path:
    return install_dir / "DefaultPalWorldSettings.ini"
