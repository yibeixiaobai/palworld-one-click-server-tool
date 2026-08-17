from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import uuid
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
            for operation in expected_patch.get("operations", []):
                player = by_uid.get(str(operation.get("player_uid")))
                if not player: raise RuntimeError(f"写回后找不到玩家 {operation.get('player_uid')}")
                for key, expected in operation.get("fields", {}).items():
                    if player.get(key) != expected: raise RuntimeError(f"字段验证失败 {key}: {player.get(key)!r} != {expected!r}")
        return payload

    @staticmethod
    def _run_checked(command: list[str], on_log: Callable[[str], None]) -> None:
        on_log("执行：" + " ".join(command[:3]))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout is not None
        for line in process.stdout: on_log(line.rstrip())
        if process.wait(): raise RuntimeError(f"命令执行失败，退出码 {process.returncode}")

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
        if os.name == "nt" and not self.detect_msvc():
            if not install_tools: raise RuntimeError("未检测到 Visual Studio C++ Build Tools")
            self.install_build_tools(on_log)
        self.root.mkdir(parents=True, exist_ok=True)
        source = self.root / "source"
        if source.exists(): shutil.rmtree(source)
        git = shutil.which("git")
        if not git: raise RuntimeError("未安装 Git，无法获取固定版本的 PlM 插件源码")
        self._run_checked([git, "clone", "--filter=blob:none", "--no-checkout", SOURCE_URL, str(source)], on_log)
        self._run_checked([git, "-C", str(source), "checkout", PALWORLD_SAVE_TOOLS_COMMIT], on_log)
        actual_commit = subprocess.run([git, "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        if actual_commit != PALWORLD_SAVE_TOOLS_COMMIT:
            raise RuntimeError("PlM 插件源码提交校验失败")
        setup = source / "src" / "palsav" / "palooz" / "setup.py"
        if os.name == "nt":
            source_text = setup.read_text(encoding="utf-8")
            import_old = "import os\nfrom setuptools import setup, Extension"
            flags_old = "extra_compile_args = ['-O3', '-flto', '-fno-exceptions', '-fno-rtti', '-ffast-math', '-fno-strict-aliasing']"
            flags_new = "if sys.platform == 'win32':\n    extra_compile_args = ['/O2', '/fp:fast', '/GR-']\nelse:\n    extra_compile_args = ['-O3', '-flto', '-fno-exceptions', '-fno-rtti', '-ffast-math', '-fno-strict-aliasing']"
            if import_old not in source_text or flags_old not in source_text:
                raise RuntimeError("固定提交的 palooz 构建脚本结构与预期不一致")
            setup.write_text(source_text.replace(import_old, "import os\nimport sys\nfrom setuptools import setup, Extension", 1).replace(flags_old, flags_new, 1), encoding="utf-8")
        if self.venv.exists(): shutil.rmtree(self.venv)
        self._run_checked([sys.executable, "-m", "venv", str(self.venv)], on_log)
        self._run_checked([str(self.python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], on_log)
        self._run_checked([str(self.python), "-m", "pip", "install", "orjson"], on_log)
        self._run_checked([str(self.python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", str(source / "src" / "palsav" / "palooz")], on_log)
        self._run_checked([str(self.python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", str(source / "src" / "palsav")], on_log)
        self.helper.write_text(PLM_HELPER, encoding="utf-8")
        digest = hashlib.sha256()
        for file in sorted((source / "src" / "palsav").rglob("*")):
            if file.is_file() and ".git" not in file.parts:
                digest.update(file.relative_to(source).as_posix().encode("utf-8")); digest.update(file.read_bytes())
        self.manifest.write_text(json.dumps({"source_commit": PALWORLD_SAVE_TOOLS_COMMIT, "source_sha256": digest.hexdigest(), "reference_commit": REFERENCE_TOOL_COMMIT, "python": sys.version, "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
        ready, detail = self.probe()
        if not ready: raise RuntimeError(detail)
        return PluginBuildResult(True, str(self.root), PALWORLD_SAVE_TOOLS_COMMIT, detail)


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
        """Return only supported player changes; reject arbitrary object writes."""
        allowed = {"nickname", "level", "exp", "hp", "shield_hp", "full_stomach", "status_point"}
        before = {str(item.get("player_uid")): item for item in self.original.get("players", [])}
        operations = []
        for player in self.properties.get("players", []):
            uid = str(player.get("player_uid") or "")
            if not uid or uid not in before:
                continue
            fields = {key: player.get(key) for key in allowed if player.get(key) != before[uid].get(key)}
            if fields:
                operations.append({"player_uid": uid, "fields": fields})
        return {"format": "palworld-console-player-patch-v1", "operations": operations}


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
            result[key].append({"SlotIndex":int(raw.get("slot_index",0)),"ItemId":str(raw["item"]["static_id"]).lower(),"StackCount":int(raw.get("count",0))})
    return result
def decode(path):
    gvas,save_type,world=load(path); players=[]; pals=[]; containers=item_index(world)
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        raw_uid=entry["key"].get("PlayerUId",{}).get("value"); uid=uid_text(raw_uid); sp=save_parameter(entry)
        if sp.get("IsPlayer",{}).get("value"):
            players.append({"player_uid":uid,"nickname":sp.get("NickName",{}).get("value","") ,"level":byte_value(sp.get("Level"),1),"exp":int(sp.get("Exp",{}).get("value",0)),"hp":fixed_value(sp.get("Hp")),"shield_hp":fixed_value(sp.get("ShieldHP")),"full_stomach":round(float(sp.get("FullStomach",{}).get("value",0)),2),"status_point":{x["StatusName"]["value"]:x["StatusPoint"]["value"] for x in sp.get("GotStatusPointList",{}).get("value",{}).get("values",[])},"pals":[],"items":player_items(path,raw_uid,containers)})
        elif sp.get("OwnerPlayerUId"):
            pals.append({"owner":uid_text(sp["OwnerPlayerUId"]["value"]),"nickname":sp.get("NickName",{}).get("value","") ,"level":byte_value(sp.get("Level"),1),"exp":int(sp.get("Exp",{}).get("value",0)),"type":sp.get("CharacterID",{}).get("value",""),"gender":str(sp.get("Gender",{}).get("value",{}).get("value","Unknown")).split("::")[-1],"is_lucky":bool(sp.get("IsRarePal",{}).get("value",False)),"workspeed":byte_value(sp.get("CraftSpeed"),0),"melee":byte_value(sp.get("Talent_HP"),0),"ranged":byte_value(sp.get("Talent_Shot"),0),"defense":byte_value(sp.get("Talent_Defense"),0),"rank":byte_value(sp.get("Rank"),1),"rank_attack":byte_value(sp.get("Rank_Attack"),0),"rank_defence":byte_value(sp.get("Rank_Defence"),0),"rank_craftspeed":byte_value(sp.get("Rank_CraftSpeed"),0),"skills":sp.get("PassiveSkillList",{}).get("value",{}).get("values",[])})
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
    gvas,save_type,world=load(level); operations={str(x["player_uid"]):x.get("fields",{}) for x in manifest.get("operations",[])}
    found=set()
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        uid=uid_text(entry["key"].get("PlayerUId",{}).get("value")); sp=save_parameter(entry)
        if not sp.get("IsPlayer",{}).get("value") or uid not in operations:continue
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
