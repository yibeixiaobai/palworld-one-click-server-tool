from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any


BUILTIN_ZH_CN: dict[str, dict[str, str]] = {
    "pals": {
        "SheepBall": "棉悠悠", "PinkCat": "捣蛋猫", "ChickenPal": "皮皮鸡",
        "Carbunclo": "翠叶鼠", "Kitsunebi": "火绒狐", "Penguin": "企丸丸",
        "Monkey": "新叶猿", "MopBaby": "米露菲", "Ganesha": "叶泥泥",
        "ElecCat": "伏特喵", "NegativeKoala": "瞅什魔", "FoxMage": "焰巫狐",
        "GrassPanda": "叶胖达", "JetDragon": "空涡龙", "BlackGriffon": "异构格里芬",
    },
    "items": {
        "Wood": "木材", "Stone": "石头", "Fiber": "纤维", "Ingot": "金属铸块",
        "PalSphere": "帕鲁球", "MegaSphere": "高级帕鲁球", "GigaSphere": "特级帕鲁球",
        "HyperSphere": "大师帕鲁球", "UltraSphere": "传奇帕鲁球", "LegendSphere": "究极帕鲁球",
        "PalCrystal_S": "帕鲁矿碎块", "Coal": "石炭", "Sulfur": "硫磺",
        "Leather": "皮革", "Wool": "羊毛", "Money": "金币", "TechnologyBook_G1": "技术书",
    },
    "skills": {
        "Unique_SheepBall_Roll": "滚滚毛球", "FireBall": "火球", "FlareArrow": "烈焰箭",
        "WaterGun": "水枪", "WindCutter": "风刃", "StoneShotgun": "岩石爆击",
    },
    "passives": {
        "Legend": "传说", "Rare": "稀有", "Lucky": "幸运", "Runner": "神速",
        "Artisan": "工匠精神", "Serious": "认真", "Workaholic": "工作狂",
    },
    "containers": {
        "inventory": "普通背包", "common": "普通背包", "key_items": "重要物品",
        "equipment": "装备栏", "weapon": "武器栏", "food": "食物栏", "palbox": "帕鲁终端",
    },
    "gender": {"Male": "雄性", "Female": "雌性", "Unknown": "未知"},
    "rarity": {"true": "幸运/稀有", "false": "普通"},
}


@dataclass(frozen=True)
class LocalizedValue:
    category: str
    key: str
    text: str
    source: str
    known: bool = True


@dataclass
class LocalizationCatalog:
    build_id: str = "builtin"
    language: str = "zh-CN"
    generated_at: str = ""
    source_path: str = ""
    source_hash: str = ""
    entries: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LocalizationCatalog":
        return cls(**{key: value for key, value in payload.items() if key in cls.__dataclass_fields__})


class GameLocalizationService:
    """Display-only localization. Stable game IDs are never rewritten."""

    CATEGORY_LABELS = {"pals": "帕鲁", "items": "物品", "skills": "技能", "passives": "被动技能", "containers": "容器", "gender": "性别", "rarity": "稀有度"}

    def __init__(self, root: Path):
        self.root = Path(root)
        self.catalog_root = self.root / "localization"
        self.override_file = self.catalog_root / "overrides.json"
        self.catalog = LocalizationCatalog(entries={key: dict(value) for key, value in BUILTIN_ZH_CN.items()})
        self._load_latest()

    @staticmethod
    def _normalized(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _lookup(mapping: dict[str, str], key: str) -> str:
        if key in mapping:
            return mapping[key]
        folded = key.casefold()
        return next((value for raw, value in mapping.items() if raw.casefold() == folded), "")

    def resolve(self, category: str, key: object) -> LocalizedValue:
        raw = self._normalized(key)
        if not raw:
            return LocalizedValue(category, raw, f"未知{self.CATEGORY_LABELS.get(category, '内容')}", "fallback", False)
        overrides = self._read_json(self.override_file).get(category, {})
        value = self._lookup(overrides, raw)
        if value:
            return LocalizedValue(category, raw, value, "user", True)
        value = self._lookup(self.catalog.entries.get(category, {}), raw)
        if value:
            source = "game" if self.catalog.build_id != "builtin" else "builtin"
            return LocalizedValue(category, raw, value, source, True)
        label = self.CATEGORY_LABELS.get(category, "内容")
        return LocalizedValue(category, raw, f"未知{label}（{raw}）", "fallback", False)

    def display(self, category: str, key: object) -> str:
        return self.resolve(category, key).text

    def save_overrides(self, entries: dict[str, dict[str, str]]) -> None:
        self._write_json(self.override_file, entries)

    def import_catalog(self, source: Path, build_id: str = "") -> LocalizationCatalog:
        source = Path(source)
        payload = self._read_json(source)
        entries = payload.get("entries", payload)
        if not isinstance(entries, dict):
            raise ValueError("中文资源文件格式无效")
        normalized: dict[str, dict[str, str]] = {}
        for category, values in entries.items():
            if isinstance(values, dict):
                normalized[str(category)] = {str(key): str(value) for key, value in values.items() if str(value).strip()}
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        catalog = LocalizationCatalog(
            build_id=build_id or payload.get("build_id") or digest[:12],
            generated_at=datetime.now().isoformat(timespec="seconds"),
            source_path=str(source), source_hash=digest, entries=normalized,
        )
        target = self.catalog_root / catalog.build_id / "zh-CN.json"
        self._write_json(target, catalog.to_dict())
        self.catalog = self._merged_with_builtin(catalog)
        return catalog

    def import_asset_directory(self, source: Path, build_id: str = "") -> LocalizationCatalog:
        """Import common extracted/localization JSON layouts without copying upstream files."""
        source = Path(source).resolve()
        if not source.is_dir():
            raise ValueError("中文资源目录不存在")
        entries: dict[str, dict[str, str]] = {}
        pal_file = next(iter(source.rglob("pal.json")), None)
        if pal_file:
            payload = self._read_json(pal_file); chinese = payload.get("zh") or payload.get("zh-CN") or payload.get("zh-Hans") or {}
            if isinstance(chinese, dict):
                entries["pals"] = {str(key): str(value) for key, value in chinese.items() if str(value).strip()}
        item_file = next(iter(source.rglob("items.json")), None)
        if item_file:
            payload = self._read_json(item_file); chinese = payload.get("zh") or payload.get("zh-CN") or payload.get("zh-Hans") or []
            if isinstance(chinese, list):
                entries["items"] = {str(item.get("key") or item.get("id")): str(item.get("name")) for item in chinese if isinstance(item, dict) and (item.get("key") or item.get("id")) and str(item.get("name") or "").strip()}
            elif isinstance(chinese, dict):
                entries["items"] = {str(key): str(value) for key, value in chinese.items() if str(value).strip()}
        for filename, category in (("skills.json", "skills"), ("passives.json", "passives"), ("passive_skills.json", "passives")):
            candidate = next(iter(source.rglob(filename)), None)
            if not candidate:
                continue
            payload = self._read_json(candidate); chinese = payload.get("zh") or payload.get("zh-CN") or payload.get("zh-Hans") or payload
            if isinstance(chinese, dict):
                entries.setdefault(category, {}).update({str(key): str(value) for key, value in chinese.items() if isinstance(value, str) and value.strip()})
        if not entries:
            raise ValueError("目录中未找到可识别的 pal.json、items.json、skills.json 或 passives.json 中文数据")
        digest = hashlib.sha256()
        for path in sorted(item for item in source.rglob("*.json") if item.is_file()):
            digest.update(path.relative_to(source).as_posix().encode()); digest.update(path.read_bytes())
        catalog = LocalizationCatalog(build_id or digest.hexdigest()[:12], "zh-CN", datetime.now().isoformat(timespec="seconds"), str(source), digest.hexdigest(), entries)
        target = self.catalog_root / catalog.build_id / "zh-CN.json"
        self._write_json(target, catalog.to_dict()); self.catalog = self._merged_with_builtin(catalog)
        return catalog

    def detect_palworld_client(self) -> Path | None:
        candidates: list[Path] = []
        try:
            import winreg
            for key_name in (r"SOFTWARE\WOW6432Node\Valve\Steam", r"SOFTWARE\Valve\Steam"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_name) as key:
                        steam = Path(winreg.QueryValueEx(key, "InstallPath")[0])
                        candidates.extend(self._steam_library_candidates(steam))
                except OSError:
                    continue
        except ImportError:
            pass
        for candidate in candidates:
            if (candidate / "Palworld.exe").is_file():
                return candidate
        return None

    @staticmethod
    def _steam_library_candidates(steam_root: Path) -> list[Path]:
        libraries = [steam_root]
        vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            libraries.extend(Path(value.replace("\\\\", "\\")) for value in re.findall(r'"path"\s+"([^"]+)"', text))
        return [root / "steamapps" / "common" / "Palworld" for root in libraries]

    def _load_latest(self) -> None:
        files = sorted(self.catalog_root.glob("*/zh-CN.json"), key=lambda path: path.stat().st_mtime, reverse=True) if self.catalog_root.exists() else []
        if not files:
            return
        try:
            self.catalog = self._merged_with_builtin(LocalizationCatalog.from_dict(self._read_json(files[0])))
        except (OSError, ValueError, TypeError):
            return

    @staticmethod
    def _merged_with_builtin(catalog: LocalizationCatalog) -> LocalizationCatalog:
        merged = {key: dict(value) for key, value in BUILTIN_ZH_CN.items()}
        for category, values in catalog.entries.items():
            merged.setdefault(category, {}).update(values)
        catalog.entries = merged
        return catalog

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not Path(path).is_file():
            return {}
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
