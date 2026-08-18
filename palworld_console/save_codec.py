from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


PALWORLD_SAVE_TOOLS_COMMIT = "a0e350127dc570593e666f2177eafcee69f7cd5d"
REFERENCE_TOOL_COMMIT = "f45a48ef25ce08a5311a27e55b17062ba0bb4362"
SOURCE_URL = "https://github.com/deafdudecomputers/PalworldSaveTools.git"
VS_BOOTSTRAPPER_URL = "https://aka.ms/vs/17/release/vs_BuildTools.exe"


class SaveCodecPlugin(Protocol):
    def probe(self) -> tuple[bool, str]: ...
    def decode(self, level_path: Path) -> dict[str, Any]: ...
    def apply_patch(self, level_path: Path, patch: dict[str, Any], output_path: Path) -> None: ...
    def verify_roundtrip(self, level_path: Path, expected_patch: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass
class PluginBuildResult:
    ready: bool
    root: str
    commit: str
    message: str


class PlmCodecPlugin:
    def __init__(self, app_root: Path | None = None):
        self.app_root = app_root or Path.home() / ".palworld-console"
        self.root = self.app_root / "plugins" / "plm" / PALWORLD_SAVE_TOOLS_COMMIT
        self.venv = self.root / "venv"
        self.python = self.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.helper = self.root / "plm_helper.py"
        self.manifest = self.root / "manifest.json"
        self.build_log = self.root / "build.log"
        self.build_state = self.root / "build-state.json"

    @staticmethod
    def _redact(value: str) -> str:
        value = str(value)
        for marker in ("password=", "passphrase=", "authorization:"):
            start = value.lower().find(marker)
            if start >= 0:
                end = value.find(" ", start)
                value = value[: start + len(marker)] + "***" + (value[end:] if end >= 0 else "")
        return value

    def _record(self, stage: str, status: str, detail: str = "") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"stage": stage, "status": status, "detail": self._redact(detail), "updated_at": datetime.now().isoformat(timespec="seconds")}
        self.build_state.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.build_log.open("a", encoding="utf-8") as handle:
            handle.write(f"[{payload['updated_at']}] {stage} {status}: {payload['detail']}\n")

    def state(self) -> dict[str, Any]:
        try:
            return json.loads(self.build_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"stage": "尚未开始", "status": "idle", "detail": ""}

    def probe(self) -> tuple[bool, str]:
        if not self.python.is_file() or not self.helper.is_file() or not self.manifest.is_file():
            return False, "PlM 插件尚未安装"
        try:
            manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
            if manifest.get("source_commit") != PALWORLD_SAVE_TOOLS_COMMIT:
                return False, "PlM 插件版本与应用要求不一致"
            result = subprocess.run([str(self.python), str(self.helper), "probe"], capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
            return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
        except Exception as exc:
            return False, str(exc)

    def _run(self, args: list[str], timeout: int = 600) -> str:
        ready, detail = self.probe()
        if not ready: raise RuntimeError(detail)
        result = subprocess.run([str(self.python), str(self.helper), *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if result.returncode: raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "PlM 插件执行失败")
        return result.stdout.strip()

    def decode(self, level_path: Path) -> dict[str, Any]:
        output = self.root / "work" / f"decode-{uuid.uuid4().hex}.json"; output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(["decode", "--level", str(level_path), "--output", str(output)])
            return json.loads(output.read_text(encoding="utf-8"))
        finally:
            output.unlink(missing_ok=True)

    def apply_patch(self, level_path: Path, patch: dict[str, Any], output_path: Path) -> None:
        work = self.root / "work"; work.mkdir(parents=True, exist_ok=True)
        patch_path = work / f"patch-{uuid.uuid4().hex}.json"; patch_path.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
        try: self._run(["patch", "--level", str(level_path), "--patch", str(patch_path), "--output", str(output_path)])
        finally: patch_path.unlink(missing_ok=True)

    def verify_roundtrip(self, level_path: Path, expected_patch: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.decode(level_path)
        if expected_patch:
            by_uid = {str(item.get("player_uid")): item for item in payload.get("players", [])}
            invariants = expected_patch.get("invariants", {})
            expected_uids = {str(uid) for uid in invariants.get("player_uids", [])}
            if expected_uids and set(by_uid) != expected_uids:
                raise RuntimeError("写回后玩家 UID 集合发生变化")
            pal_count = sum(len(player.get("pals", [])) for player in payload.get("players", []))
            item_count = sum(len(items) for player in payload.get("players", []) for items in (player.get("items") or {}).values())
            if "pal_count" in invariants and pal_count != int(invariants["pal_count"]):
                raise RuntimeError("写回后帕鲁数量发生变化")
            if "inventory_count" in invariants and item_count != int(invariants["inventory_count"]):
                raise RuntimeError("写回后背包槽位数量发生变化")
            operations = expected_patch.get("players", expected_patch.get("operations", []))
            for operation in operations:
                player = by_uid.get(str(operation.get("player_uid")))
                if not player: raise RuntimeError(f"写回后找不到玩家 {operation.get('player_uid')}")
                for key, expected in operation.get("fields", {}).items():
                    if player.get(key) != expected: raise RuntimeError(f"字段验证失败 {key}: {player.get(key)!r} != {expected!r}")
            by_pal = {str(pal.get("individual_id")): pal for player in payload.get("players", []) for pal in player.get("pals", []) if pal.get("individual_id")}
            for operation in expected_patch.get("pals", []):
                pal = by_pal.get(str(operation.get("individual_id")))
                if not pal: raise RuntimeError(f"写回后找不到帕鲁 {operation.get('individual_id')}")
                for key, expected in operation.get("fields", {}).items():
                    if pal.get(key) != expected: raise RuntimeError(f"帕鲁字段验证失败 {key}: {pal.get(key)!r} != {expected!r}")
            by_item = {}
            for player in payload.get("players", []):
                for container_name, items in (player.get("items") or {}).items():
                    for item in items or []:
                        identity = (str(player.get("player_uid")), str(item.get("ContainerId") or container_name), int(item.get("SlotIndex") or 0))
                        by_item[identity] = item
            for operation in expected_patch.get("inventory", []):
                identity = (str(operation.get("player_uid")), str(operation.get("container_id")), int(operation.get("slot_index") or 0))
                item = by_item.get(identity)
                if not item: raise RuntimeError(f"写回后找不到背包槽位 {identity[1]}:{identity[2]}")
                for key, expected in operation.get("fields", {}).items():
                    if item.get(key) != expected: raise RuntimeError(f"背包字段验证失败 {key}: {item.get(key)!r} != {expected!r}")
        return payload

    def _run_checked(self, command: list[str], on_log: Callable[[str], None], stage: str) -> None:
        summary = self._redact(" ".join(command[:4]))
        self._record(stage, "running", summary); on_log(f"[{stage}] 执行：{summary}")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout is not None
        with self.build_log.open("a", encoding="utf-8") as handle:
            for line in process.stdout:
                safe = self._redact(line.rstrip()); on_log(safe); handle.write(safe + "\n")
        if process.wait():
            self._record(stage, "failed", f"退出码 {process.returncode}")
            raise RuntimeError(f"{stage}失败，退出码 {process.returncode}；日志：{self.build_log}")
        self._record(stage, "complete", summary)

    def install_prebuilt(self, archive: Path, on_log: Callable[[str], None] | None = None) -> PluginBuildResult:
        on_log = on_log or (lambda _line: None); self.root.mkdir(parents=True, exist_ok=True)
        self._record("校验预编译包", "running", str(archive))
        if not zipfile.is_zipfile(archive): raise RuntimeError("预编译插件包不是有效 ZIP")
        staging = self.root.parent / f".prebuilt-{uuid.uuid4().hex}"
        with zipfile.ZipFile(archive) as bundle:
            try: package_manifest = json.loads(bundle.read("plugin-manifest.json").decode("utf-8"))
            except Exception as exc: raise RuntimeError("预编译插件包缺少有效 plugin-manifest.json") from exc
            expected = package_manifest.get("archive_sha256")
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if expected and expected.lower() != actual.lower(): raise RuntimeError("预编译插件包 SHA-256 校验失败")
            if package_manifest.get("source_commit") != PALWORLD_SAVE_TOOLS_COMMIT: raise RuntimeError("预编译插件提交版本不匹配")
            if package_manifest.get("python_abi") != f"cp{sys.version_info.major}{sys.version_info.minor}": raise RuntimeError("预编译插件 Python ABI 不匹配")
            if str(package_manifest.get("architecture", "")).lower() != platform.machine().lower(): raise RuntimeError("预编译插件系统架构不匹配")
            try:
                bundle.extractall(staging)
                staged_root = staging / self.root.name
                if not staged_root.exists(): staged_root = staging
                old_root = self.root.parent / f"{self.root.name}.previous"
                if old_root.exists(): shutil.rmtree(old_root)
                if self.root.exists(): self.root.replace(old_root)
                staged_root.replace(self.root)
                ready, detail = self.probe()
                if not ready:
                    if self.root.exists(): shutil.rmtree(self.root)
                    if old_root.exists(): old_root.replace(self.root)
                    raise RuntimeError(f"预编译插件导入验证失败：{detail}")
                if old_root.exists(): shutil.rmtree(old_root)
            finally:
                if staging.exists(): shutil.rmtree(staging, ignore_errors=True)
        self._record("校验预编译包", "complete", detail); on_log(detail)
        return PluginBuildResult(True, str(self.root), PALWORLD_SAVE_TOOLS_COMMIT, detail)

    def clear_broken_cache(self) -> None:
        for path in (self.venv, self.helper, self.manifest):
            if path.is_dir(): shutil.rmtree(path)
            elif path.exists(): path.unlink()
        self._record("清理缓存", "complete", "保留已下载源码")

    @staticmethod
    def detect_msvc() -> str:
        direct = shutil.which("cl.exe")
        if direct: return direct
        vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not vswhere.is_file(): return ""
        result = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], capture_output=True, text=True)
        return result.stdout.strip()

    def install_build_tools(self, on_log: Callable[[str], None] | None = None) -> None:
        if os.name != "nt": return
        on_log = on_log or (lambda _line: None)
        downloads = self.app_root / "downloads"; downloads.mkdir(parents=True, exist_ok=True)
        installer = downloads / "vs_BuildTools.exe"
        on_log("正在从微软官方下载 Visual Studio Build Tools 引导程序")
        urllib.request.urlretrieve(VS_BOOTSTRAPPER_URL, installer)
        powershell = shutil.which("powershell.exe") or "powershell.exe"
        escaped_installer = str(installer).replace("'", "''")
        signature_script = (
            f"$s=Get-AuthenticodeSignature -LiteralPath '{escaped_installer}'; "
            "if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Microsoft'){exit 2}; "
            "$s.SignerCertificate.Subject"
        )
        check = subprocess.run(
            [powershell, "-NoProfile", "-Command", signature_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check.returncode: raise RuntimeError("Visual Studio Build Tools 安装程序签名验证失败")
        on_log("微软 Authenticode 签名验证通过，等待管理员授权安装 C++ Build Tools")
        args = "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
        result = subprocess.run([powershell, "-NoProfile", "-Command", f"$p=Start-Process -FilePath '{installer}' -ArgumentList '{args}' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"])
        if result.returncode not in {0, 3010}: raise RuntimeError(f"Visual Studio Build Tools 安装失败，退出码 {result.returncode}")

    def build(self, on_log: Callable[[str], None] | None = None, install_tools: bool = False) -> PluginBuildResult:
        on_log = on_log or (lambda _line: None)
        try:
            ready, detail = self.probe()
            if ready:
                self._record("插件检测", "complete", "复用已验证插件")
                return PluginBuildResult(True, str(self.root), PALWORLD_SAVE_TOOLS_COMMIT, detail)
            self._record("环境检测", "running", f"Python {platform.python_version()} / {platform.machine()}")
            if os.name == "nt" and not self.detect_msvc():
                if not install_tools:
                    raise RuntimeError("未检测到 Visual Studio C++ Build Tools")
                self.install_build_tools(on_log)
            self.root.mkdir(parents=True, exist_ok=True)
            source = self.root / "source"
            git = shutil.which("git")
            if not git:
                raise RuntimeError("未安装 Git，无法获取固定版本的 PlM 插件源码")
            if not (source / ".git").is_dir():
                if source.exists():
                    shutil.rmtree(source)
                self._run_checked([git, "clone", "--filter=blob:none", "--no-checkout", SOURCE_URL, str(source)], on_log, "下载固定源码")
            self._run_checked([git, "-C", str(source), "checkout", "--force", PALWORLD_SAVE_TOOLS_COMMIT], on_log, "校验源码提交")
            actual_commit = subprocess.run([git, "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            if actual_commit != PALWORLD_SAVE_TOOLS_COMMIT:
                raise RuntimeError("PlM 插件源码提交校验失败")
            setup = source / "src" / "palsav" / "palooz" / "setup.py"
            if os.name == "nt":
                source_text = setup.read_text(encoding="utf-8")
                import_old = "import os\nfrom setuptools import setup, Extension"
                flags_old = "extra_compile_args = ['-O3', '-flto', '-fno-exceptions', '-fno-rtti', '-ffast-math', '-fno-strict-aliasing']"
                flags_new = "if sys.platform == 'win32':\n    extra_compile_args = ['/O2', '/fp:fast', '/GR-']\nelse:\n    extra_compile_args = ['-O3', '-flto', '-fno-exceptions', '-fno-rtti', '-ffast-math', '-fno-strict-aliasing']"
                if flags_old in source_text:
                    source_text = source_text.replace(flags_old, flags_new, 1)
                if import_old in source_text:
                    source_text = source_text.replace(import_old, "import os\nimport sys\nfrom setuptools import setup, Extension", 1)
                setup.write_text(source_text, encoding="utf-8")
            if not self.python.is_file():
                self._run_checked([sys.executable, "-m", "venv", str(self.venv)], on_log, "创建隔离环境")
            self._run_checked([str(self.python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], on_log, "准备 Python 依赖")
            self._run_checked([str(self.python), "-m", "pip", "install", "orjson"], on_log, "安装运行依赖")
            self._run_checked([str(self.python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", str(source / "src" / "palsav" / "palooz")], on_log, "编译 Oodle 扩展")
            self._run_checked([str(self.python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", str(source / "src" / "palsav")], on_log, "安装 palsav-flex")
            self.helper.write_text(PLM_HELPER, encoding="utf-8")
            digest = hashlib.sha256()
            for file in sorted((source / "src" / "palsav").rglob("*")):
                if file.is_file() and ".git" not in file.parts:
                    digest.update(file.relative_to(source).as_posix().encode("utf-8"))
                    digest.update(file.read_bytes())
            self.manifest.write_text(json.dumps({"source_commit": PALWORLD_SAVE_TOOLS_COMMIT, "source_sha256": digest.hexdigest(), "reference_commit": REFERENCE_TOOL_COMMIT, "python": sys.version, "built_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
            ready, detail = self.probe()
            if not ready:
                raise RuntimeError(detail)
            self._record("插件导入验证", "complete", detail)
            return PluginBuildResult(True, str(self.root), PALWORLD_SAVE_TOOLS_COMMIT, detail)
        except Exception as exc:
            self._record(self.state().get("stage", "插件安装"), "failed", str(exc))
            raise


@dataclass
class PluginParsedSave:
    properties: dict[str, Any]
    source_path: Path
    original: dict[str, Any]
    plugin: PlmCodecPlugin

    @classmethod
    def create(cls, payload: dict[str, Any], source: Path, plugin: PlmCodecPlugin):
        return cls(payload, source, copy.deepcopy(payload), plugin)

    def patch_manifest(self) -> dict[str, Any]:
        """Build a stable-identity patch; unknown and index-only objects remain read-only."""
        allowed = {"nickname", "level", "exp", "hp", "shield_hp", "full_stomach", "status_point"}
        pal_allowed = {"nickname", "level", "exp", "workspeed", "melee", "ranged", "defense", "rank", "skills"}
        before = {str(item.get("player_uid")): item for item in self.original.get("players", [])}
        players = []
        pals = []
        inventory = []
        for player in self.properties.get("players", []):
            uid = str(player.get("player_uid") or "")
            if not uid or uid not in before:
                continue
            fields = {key: player.get(key) for key in allowed if player.get(key) != before[uid].get(key)}
            if fields:
                players.append({"player_uid": uid, "fields": fields})
            old_pals = {str(pal.get("individual_id") or ""): pal for pal in before[uid].get("pals", []) if pal.get("individual_id")}
            for pal in player.get("pals", []):
                individual_id = str(pal.get("individual_id") or "")
                if not individual_id or individual_id not in old_pals:
                    continue
                changed = {key: pal.get(key) for key in pal_allowed if pal.get(key) != old_pals[individual_id].get(key)}
                if changed:
                    pals.append({"individual_id": individual_id, "owner_uid": uid, "fields": changed})
            old_items = {}
            for container, items in (before[uid].get("items") or {}).items():
                for item in items or []:
                    stable_container = str(item.get("ContainerId") or container)
                    old_items[(stable_container, int(item.get("SlotIndex") or 0))] = item
            for container, items in (player.get("items") or {}).items():
                for item in items or []:
                    identity = (str(item.get("ContainerId") or container), int(item.get("SlotIndex") or 0))
                    old = old_items.get(identity)
                    if old and item.get("StackCount") != old.get("StackCount"):
                        inventory.append({"player_uid": uid, "container_id": identity[0], "slot_index": identity[1], "fields": {"StackCount": item.get("StackCount")}})
        original_players = self.original.get("players", [])
        invariants = {
            "player_uids": sorted(str(player.get("player_uid")) for player in original_players if player.get("player_uid")),
            "pal_count": sum(len(player.get("pals", [])) for player in original_players),
            "inventory_count": sum(len(items) for player in original_players for items in (player.get("items") or {}).values()),
        }
        return {"format": "palworld-console-save-patch-v2", "players": players, "pals": pals, "inventory": inventory, "guilds": [], "bases": [], "invariants": invariants}


PLM_HELPER = r'''from __future__ import annotations
import argparse, json
from pathlib import Path
from palsav.core import decompress_sav_to_gvas, compress_gvas_to_sav
from palsav.gvas import GvasFile
from palsav.paltypes import PALWORLD_TYPE_HINTS, PALWORLD_CUSTOM_PROPERTIES

def uid_text(uid):
    if uid is None: return ""
    return str(int(str(uid).split("-")[0], 16))
def byte_value(prop, default=0):
    if not prop: return default
    value=prop.get("value")
    return int(value.get("value",default) if isinstance(value,dict) else value)
def fixed_value(prop):
    try:return int(prop["value"]["Value"]["value"])
    except Exception:return 0
def save_parameter(entry): return entry["value"]["RawData"]["value"]["object"]["SaveParameter"]["value"]
def enum_value(prop, default=""):
    value=(prop or {}).get("value",default)
    if isinstance(value,dict): value=value.get("value",default)
    return str(value or default)
def read_gvas(path):
    raw,save_type=decompress_sav_to_gvas(Path(path).read_bytes())
    gvas=GvasFile.read(raw,PALWORLD_TYPE_HINTS,PALWORLD_CUSTOM_PROPERTIES)
    return gvas,save_type
def load(path):
    gvas,save_type=read_gvas(path)
    return gvas,save_type,gvas.properties["worldSaveData"]["value"]
PLAYER_CONTAINER_KEYS=("CommonContainerId","DropSlotContainerId","EssentialContainerId","FoodEquipContainerId","PlayerEquipArmorContainerId","WeaponLoadOutContainerId")
def item_index(world):
    result={}
    for container in world.get("ItemContainerSaveData",{}).get("value",[]):
        cid=str(container["key"]["ID"]["value"])
        result[cid]=container["value"]["Slots"]["value"].get("values",[])
    return result
def player_items(level_path,raw_uid,containers):
    result={key:[] for key in PLAYER_CONTAINER_KEYS}
    player_path=Path(level_path).parent/"Players"/(str(raw_uid).upper().replace("-","")+".sav")
    if not player_path.is_file():return result
    try: save=read_gvas(player_path)[0].properties["SaveData"]["value"]
    except Exception:return result
    inventory=save.get("InventoryInfo",{}).get("value",{})
    for key in PLAYER_CONTAINER_KEYS:
        ref=inventory.get(key)
        if not ref:continue
        cid=str(ref["value"]["ID"]["value"])
        for slot in containers.get(cid,[]):
            raw=slot.get("RawData",{}).get("value")
            if not raw or not raw.get("item",{}).get("static_id"):continue
            result[key].append({"ContainerId":cid,"SlotIndex":int(raw.get("slot_index",0)),"ItemId":str(raw["item"]["static_id"]).lower(),"StackCount":int(raw.get("count",0))})
    return result
def decode(path):
    gvas,save_type,world=load(path); players=[]; pals=[]; containers=item_index(world)
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        raw_uid=entry["key"].get("PlayerUId",{}).get("value"); uid=uid_text(raw_uid); sp=save_parameter(entry)
        if sp.get("IsPlayer",{}).get("value"):
            players.append({"player_uid":uid,"nickname":sp.get("NickName",{}).get("value","") ,"level":byte_value(sp.get("Level"),1),"exp":int(sp.get("Exp",{}).get("value",0)),"hp":fixed_value(sp.get("Hp")),"shield_hp":fixed_value(sp.get("ShieldHP")),"full_stomach":round(float(sp.get("FullStomach",{}).get("value",0)),2),"status_point":{x["StatusName"]["value"]:x["StatusPoint"]["value"] for x in sp.get("GotStatusPointList",{}).get("value",{}).get("values",[])},"pals":[],"items":player_items(path,raw_uid,containers)})
        elif sp.get("OwnerPlayerUId"):
            pals.append({"individual_id":str(entry["key"].get("InstanceId",{}).get("value","")).lower(),"owner":uid_text(sp["OwnerPlayerUId"]["value"]),"nickname":sp.get("NickName",{}).get("value","") ,"level":byte_value(sp.get("Level"),1),"exp":int(sp.get("Exp",{}).get("value",0)),"type":sp.get("CharacterID",{}).get("value",""),"gender":str(sp.get("Gender",{}).get("value",{}).get("value","Unknown")).split("::")[-1],"is_lucky":bool(sp.get("IsRarePal",{}).get("value",False)),"workspeed":byte_value(sp.get("CraftSpeed"),0),"melee":byte_value(sp.get("Talent_HP"),0),"ranged":byte_value(sp.get("Talent_Shot"),0),"defense":byte_value(sp.get("Talent_Defense"),0),"rank":byte_value(sp.get("Rank"),1),"rank_attack":byte_value(sp.get("Rank_Attack"),0),"rank_defence":byte_value(sp.get("Rank_Defence"),0),"rank_craftspeed":byte_value(sp.get("Rank_CraftSpeed"),0),"skills":sp.get("PassiveSkillList",{}).get("value",{}).get("values",[])})
    by_uid={p["player_uid"]:p for p in players}
    for pal in pals:
        owner=pal.pop("owner","")
        if owner in by_uid: by_uid[owner]["pals"].append(pal)
    guilds=[]
    for group in world.get("GroupSaveDataMap",{}).get("value",[]):
        if enum_value(group.get("value",{}).get("GroupType"))!="EPalGroupType::Guild":continue
        raw=group["value"]["RawData"]["value"]
        guilds.append({"guild_id":str(raw.get("group_id","")),"name":raw.get("guild_name",""),"base_camp_level":raw.get("base_camp_level",0),"admin_player_uid":uid_text(raw.get("admin_player_uid")),"players":[{"player_uid":uid_text(x.get("player_uid")),"nickname":x.get("player_info",{}).get("player_name","")} for x in raw.get("players",[])]})
    return {"format":"PlM1","save_type":save_type,"players":players,"guilds":guilds}
def set_byte(prop,value):
    if isinstance(prop.get("value"),dict): prop["value"]["value"]=int(value)
    else: prop["value"]=int(value)
def set_fixed(prop,value): prop["value"]["Value"]["value"]=int(value)
def patch(level,manifest,output):
    gvas,save_type,world=load(level); operations={str(x["player_uid"]):x.get("fields",{}) for x in manifest.get("players",manifest.get("operations",[]))}
    pal_operations={str(x["individual_id"]).lower():x.get("fields",{}) for x in manifest.get("pals",[])}
    item_operations={(str(x["container_id"]),int(x["slot_index"])):x.get("fields",{}) for x in manifest.get("inventory",[])}
    found=set()
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        uid=uid_text(entry["key"].get("PlayerUId",{}).get("value")); sp=save_parameter(entry)
        if sp.get("IsPlayer",{}).get("value") and uid in operations:
            found.add(uid); fields=operations[uid]
            if "nickname" in fields: sp["NickName"]["value"]=str(fields["nickname"])
            if "level" in fields: set_byte(sp["Level"],fields["level"])
            if "exp" in fields: sp["Exp"]["value"]=int(fields["exp"])
            if "hp" in fields: set_fixed(sp["Hp"],fields["hp"])
            if "shield_hp" in fields: set_fixed(sp["ShieldHP"],fields["shield_hp"])
            if "full_stomach" in fields: sp["FullStomach"]["value"]=float(fields["full_stomach"])
            if "status_point" in fields:
                wanted=fields["status_point"]
                for item in sp.get("GotStatusPointList",{}).get("value",{}).get("values",[]):
                    name=item["StatusName"]["value"]
                    if name in wanted:item["StatusPoint"]["value"]=int(wanted[name])
        individual_id=str(entry["key"].get("InstanceId",{}).get("value","")).lower()
        if individual_id in pal_operations:
            fields=pal_operations[individual_id]
            if "nickname" in fields: sp["NickName"]["value"]=str(fields["nickname"])
            if "level" in fields:set_byte(sp["Level"],fields["level"])
            if "exp" in fields:sp["Exp"]["value"]=int(fields["exp"])
            for key,prop in (("workspeed","CraftSpeed"),("melee","Talent_HP"),("ranged","Talent_Shot"),("defense","Talent_Defense"),("rank","Rank")):
                if key in fields:set_byte(sp[prop],fields[key])
            if "skills" in fields:sp["PassiveSkillList"]["value"]["values"]=list(fields["skills"])
    for container in world.get("ItemContainerSaveData",{}).get("value",[]):
        cid=str(container["key"]["ID"]["value"])
        for slot in container["value"]["Slots"]["value"].get("values",[]):
            raw=slot.get("RawData",{}).get("value") or {}; identity=(cid,int(raw.get("slot_index",0)))
            if identity in item_operations and "StackCount" in item_operations[identity]:raw["count"]=int(item_operations[identity]["StackCount"])
    missing=set(operations)-found
    if missing:raise RuntimeError("players not found: "+",".join(sorted(missing)))
    output_data=compress_gvas_to_sav(gvas.write(PALWORLD_CUSTOM_PROPERTIES),save_type); Path(output).write_bytes(output_data)
def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="cmd",required=True); sub.add_parser("probe")
    for name in ("decode","patch"):
        p=sub.add_parser(name);p.add_argument("--level",required=True);p.add_argument("--output",required=True)
        if name=="patch":p.add_argument("--patch",required=True)
    args=parser.parse_args()
    if args.cmd=="probe":
        import palooz, palsav;print("PlM codec ready");return
    if args.cmd=="decode":Path(args.output).write_text(json.dumps(decode(args.level),ensure_ascii=False),encoding="utf-8")
    else:patch(args.level,json.loads(Path(args.patch).read_text(encoding="utf-8")),args.output)
if __name__=="__main__":main()
'''
