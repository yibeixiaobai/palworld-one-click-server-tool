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
HELPER_API_VERSION = 12
SAVE_PATCH_FORMAT = "palworld-console-save-patch-v2"
IDENTITY_MIGRATION_FORMAT = "palworld-console-identity-migration-v1"


class SaveCodecPlugin(Protocol):
    def probe(self) -> tuple[bool, str]: ...
    def decode(self, level_path: Path) -> dict[str, Any]: ...
    def decode_players(self, level_path: Path) -> dict[str, Any]: ...
    def apply_patch(self, level_path: Path, patch: dict[str, Any], output_path: Path) -> None: ...
    def verify_roundtrip(self, level_path: Path, expected_patch: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def migrate_identities(self, world_path: Path, mappings: list[dict[str, Any]], output_path: Path) -> dict[str, Any]: ...
    def convert_file(self, source_path: Path, output_path: Path) -> dict[str, Any]: ...
    def steam_id_to_uid(self, steam_id: str) -> dict[str, str]: ...
    def restore_map(self, source_path: Path, output_path: Path) -> dict[str, Any]: ...
    def expand_palbox(self, world_path: Path, player_guid: str, slots: int, output_path: Path) -> dict[str, Any]: ...
    def clean_world(self, world_path: Path, selection: dict[str, list[str]], output_path: Path) -> dict[str, Any]: ...


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

    @staticmethod
    def helper_sha256() -> str:
        return hashlib.sha256(PLM_HELPER.encode("utf-8")).hexdigest()

    def _ensure_helper_contract(self, manifest: dict[str, Any]) -> None:
        expected_hash = self.helper_sha256()
        current_hash = ""
        if self.helper.is_file():
            current_hash = hashlib.sha256(self.helper.read_bytes()).hexdigest()
        compatible = (
            manifest.get("helper_api_version") == HELPER_API_VERSION
            and manifest.get("patch_format") == SAVE_PATCH_FORMAT
            and manifest.get("helper_sha256") == expected_hash
            and current_hash == expected_hash
        )
        if compatible:
            return
        helper_tmp = self.root / f".plm-helper-{uuid.uuid4().hex}.tmp"
        manifest_tmp = self.root / f".manifest-{uuid.uuid4().hex}.tmp"
        try:
            helper_tmp.write_text(PLM_HELPER, encoding="utf-8")
            updated = dict(manifest)
            updated.update({
                "helper_api_version": HELPER_API_VERSION,
                "patch_format": SAVE_PATCH_FORMAT,
                "helper_sha256": expected_hash,
                "helper_updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            manifest_tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(helper_tmp, self.helper)
            os.replace(manifest_tmp, self.manifest)
            self._record("升级 helper 接口", "complete", f"API v{HELPER_API_VERSION} / {SAVE_PATCH_FORMAT}")
        except Exception as exc:
            raise RuntimeError(f"PlM helper 接口版本不匹配且自动升级失败：{exc}") from exc
        finally:
            helper_tmp.unlink(missing_ok=True)
            manifest_tmp.unlink(missing_ok=True)

    def probe(self) -> tuple[bool, str]:
        if not self.python.is_file() or not self.manifest.is_file():
            return False, "PlM 插件尚未安装"
        try:
            manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
            if manifest.get("source_commit") != PALWORLD_SAVE_TOOLS_COMMIT:
                return False, "PlM 插件版本与应用要求不一致"
            self._ensure_helper_contract(manifest)
            result = subprocess.run([str(self.python), str(self.helper), "probe"], capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
            detail = result.stdout.strip() or result.stderr.strip()
            if result.returncode:
                return False, detail
            return True, f"{detail or 'PlM codec ready'} / helper API v{HELPER_API_VERSION}"
        except Exception as exc:
            return False, str(exc)

    def _run(self, args: list[str], timeout: int = 600) -> str:
        ready, detail = self.probe()
        if not ready: raise RuntimeError(detail)
        try:
            result = subprocess.run([str(self.python), str(self.helper), *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            command = args[0] if args else "helper"
            raise RuntimeError(f"PlM helper 执行超时：{command} 超过 {timeout} 秒。请确认存档未被游戏占用，并检查插件构建日志") from exc
        if result.returncode: raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "PlM 插件执行失败")
        return result.stdout.strip()

    def decode(self, level_path: Path) -> dict[str, Any]:
        output = self.root / "work" / f"decode-{uuid.uuid4().hex}.json"; output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(["decode", "--level", str(level_path), "--output", str(output)])
            return json.loads(output.read_text(encoding="utf-8"))
        finally:
            output.unlink(missing_ok=True)

    def decode_players(self, level_path: Path) -> dict[str, Any]:
        output = self.root / "work" / f"players-{uuid.uuid4().hex}.json"; output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(["decode-players", "--level", str(level_path), "--output", str(output)], timeout=240)
            return json.loads(output.read_text(encoding="utf-8"))
        finally:
            output.unlink(missing_ok=True)

    def apply_patch(self, level_path: Path, patch: dict[str, Any], output_path: Path) -> None:
        work = self.root / "work"; work.mkdir(parents=True, exist_ok=True)
        patch_path = work / f"patch-{uuid.uuid4().hex}.json"; patch_path.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
        try: self._run(["patch", "--level", str(level_path), "--patch", str(patch_path), "--output", str(output_path)])
        finally: patch_path.unlink(missing_ok=True)

    def migrate_identities(self, world_path: Path, mappings: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
        work = self.root / "work"; work.mkdir(parents=True, exist_ok=True)
        mapping_path = work / f"identity-{uuid.uuid4().hex}.json"
        mapping_path.write_text(json.dumps({"format": IDENTITY_MIGRATION_FORMAT, "mappings": mappings}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            result = self._run(["migrate-identities", "--world", str(world_path), "--mapping", str(mapping_path), "--output", str(output_path)])
            return json.loads(result)
        finally:
            mapping_path.unlink(missing_ok=True)

    def migrate_identities_v2(self, base_world: Path, source_world: Path, mappings: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
        """Incremental migration using the immutable source and latest server snapshot."""
        work = self.root / "work"; work.mkdir(parents=True, exist_ok=True)
        mapping_path = work / f"identity-v2-{uuid.uuid4().hex}.json"
        mapping_path.write_text(json.dumps({"format": "palworld-console-identity-migration-v2", "source_world": str(source_world), "mappings": mappings}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            result = self._run(["migrate-identities-v2", "--base-world", str(base_world), "--source-world", str(source_world), "--mapping", str(mapping_path), "--output", str(output_path)], timeout=600)
            return json.loads(result)
        finally:
            mapping_path.unlink(missing_ok=True)

    def convert_file(self, source_path: Path, output_path: Path) -> dict[str, Any]:
        result = self._run(["convert", "--source", str(source_path), "--output", str(output_path)])
        return json.loads(result)

    def steam_id_to_uid(self, steam_id: str) -> dict[str, str]:
        result = self._run(["steam-uid", "--steam-id", str(steam_id)], timeout=60)
        return json.loads(result)

    def restore_map(self, source_path: Path, output_path: Path) -> dict[str, Any]:
        result = self._run(["restore-map", "--source", str(source_path), "--output", str(output_path)])
        return json.loads(result)

    def expand_palbox(self, world_path: Path, player_guid: str, slots: int, output_path: Path) -> dict[str, Any]:
        result = self._run(["expand-palbox", "--world", str(world_path), "--player-guid", str(player_guid), "--slots", str(slots), "--output", str(output_path)])
        return json.loads(result)

    def clean_world(self, world_path: Path, selection: dict[str, list[str]], output_path: Path) -> dict[str, Any]:
        work = self.root / "work"; work.mkdir(parents=True, exist_ok=True)
        selection_path = work / f"cleanup-{uuid.uuid4().hex}.json"
        selection_path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            result = self._run(["clean-world", "--world", str(world_path), "--selection", str(selection_path), "--output", str(output_path)], timeout=900)
            return json.loads(result)
        finally:
            selection_path.unlink(missing_ok=True)

    def verify_roundtrip(self, level_path: Path, expected_patch: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.decode(level_path)
        if expected_patch:
            if expected_patch.get("format") != SAVE_PATCH_FORMAT or "operations" in expected_patch:
                raise RuntimeError("仅支持 palworld-console-save-patch-v2 补丁验证")
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
            operations = expected_patch.get("players", [])
            for operation in operations:
                player = by_uid.get(str(operation.get("player_uid")))
                if not player: raise RuntimeError(f"写回后找不到玩家 {operation.get('player_uid')}")
                for key, expected in operation.get("fields", {}).items():
                    if player.get(key) != expected: raise RuntimeError(f"字段验证失败 {key}: {player.get(key)!r} != {expected!r}")
            decoded_pals = [pal for player in payload.get("players", []) for pal in player.get("pals", []) if pal.get("individual_id")]
            decoded_pal_ids = [str(pal.get("individual_id")) for pal in decoded_pals]
            if len(decoded_pal_ids) != len(set(decoded_pal_ids)):
                raise RuntimeError("写回后出现重复帕鲁 InstanceId")
            by_pal = {str(pal.get("individual_id")): pal for pal in decoded_pals}
            expected_pal_ids = {str(value) for value in invariants.get("pal_instance_ids", [])}
            if expected_pal_ids and set(by_pal) != expected_pal_ids:
                raise RuntimeError("写回后帕鲁 InstanceId 集合发生变化")
            for operation in expected_patch.get("pals", []):
                pal = by_pal.get(str(operation.get("individual_id")))
                if not pal: raise RuntimeError(f"写回后找不到帕鲁 {operation.get('individual_id')}")
                for key, expected in operation.get("fields", {}).items():
                    if pal.get(key) != expected: raise RuntimeError(f"帕鲁字段验证失败 {key}: {pal.get(key)!r} != {expected!r}")
            for individual_id, expected_fields in invariants.get("unchanged_pal_fields", {}).items():
                pal = by_pal.get(str(individual_id))
                if not pal:
                    raise RuntimeError(f"写回后找不到未修改帕鲁 {individual_id}")
                for key, expected in expected_fields.items():
                    if pal.get(key) != expected:
                        raise RuntimeError(f"未修改帕鲁字段发生变化 {individual_id}.{key}: {pal.get(key)!r} != {expected!r}")
            by_item = {}
            for player in payload.get("players", []):
                for container_name, items in (player.get("items") or {}).items():
                    for item in items or []:
                        identity = (str(player.get("player_uid")), str(item.get("ContainerId") or container_name), int(item.get("SlotIndex") or 0))
                        if identity in by_item:
                            raise RuntimeError(f"写回后出现重复背包槽位 {identity[1]}:{identity[2]}")
                        by_item[identity] = item
            for operation in expected_patch.get("inventory", []):
                identity = (str(operation.get("player_uid")), str(operation.get("container_id")), int(operation.get("slot_index") or 0))
                item = by_item.get(identity)
                if not item: raise RuntimeError(f"写回后找不到背包槽位 {identity[1]}:{identity[2]}")
                for key, expected in operation.get("fields", {}).items():
                    if item.get(key) != expected: raise RuntimeError(f"背包字段验证失败 {key}: {item.get(key)!r} != {expected!r}")
            by_guild = {str(item.get("guild_id")): item for item in payload.get("guilds", []) if item.get("guild_id")}
            for operation in expected_patch.get("guilds", []):
                guild = by_guild.get(str(operation.get("guild_id")))
                if not guild: raise RuntimeError(f"写回后找不到公会 {operation.get('guild_id')}")
                for key, expected in operation.get("fields", {}).items():
                    if guild.get(key) != expected: raise RuntimeError(f"公会字段验证失败 {key}: {guild.get(key)!r} != {expected!r}")
            by_base = {str(item.get("base_id")): item for item in payload.get("bases", []) if item.get("base_id")}
            for operation in expected_patch.get("bases", []):
                base = by_base.get(str(operation.get("base_id")))
                if not base: raise RuntimeError(f"写回后找不到基地 {operation.get('base_id')}")
                for key, expected in operation.get("fields", {}).items():
                    actual = base.get(key)
                    if key == "position":
                        for axis, value in expected.items():
                            if float((actual or {}).get(axis, 0)) != float(value): raise RuntimeError(f"基地坐标验证失败 {axis}")
                    elif actual != expected: raise RuntimeError(f"基地字段验证失败 {key}: {actual!r} != {expected!r}")
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
            self.manifest.write_text(json.dumps({"source_commit": PALWORLD_SAVE_TOOLS_COMMIT, "source_sha256": digest.hexdigest(), "reference_commit": REFERENCE_TOOL_COMMIT, "python": sys.version, "built_at": datetime.now().isoformat(timespec="seconds"), "helper_api_version": HELPER_API_VERSION, "patch_format": SAVE_PATCH_FORMAT, "helper_sha256": self.helper_sha256()}, ensure_ascii=False, indent=2), encoding="utf-8")
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
        pal_allowed = {"nickname", "level", "exp", "workspeed", "melee", "ranged", "defense", "rank", "skills", "active_skills", "learned_skills", "rank_attack", "rank_defence", "rank_craftspeed", "is_lucky"}
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
            old_pals = {str(pal.get("individual_id") or ""): pal for pal in before[uid].get("pals", []) if pal.get("individual_id") and pal.get("stable_id_valid", True)}
            for pal in player.get("pals", []):
                individual_id = str(pal.get("individual_id") or "")
                if not individual_id or not pal.get("stable_id_valid", True) or individual_id not in old_pals:
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
        changed_pal_ids = {str(operation["individual_id"]) for operation in pals}
        original_pals = [pal for player in original_players for pal in player.get("pals", []) if pal.get("individual_id") and pal.get("stable_id_valid", True)]
        invariants = {
            "player_uids": sorted(str(player.get("player_uid")) for player in original_players if player.get("player_uid")),
            "pal_count": sum(len(player.get("pals", [])) for player in original_players),
            "inventory_count": sum(len(items) for player in original_players for items in (player.get("items") or {}).values()),
            "pal_instance_ids": sorted(str(pal.get("individual_id")) for pal in original_pals),
            "unchanged_pal_fields": {
                str(pal.get("individual_id")): {key: pal.get(key) for key in pal_allowed}
                for pal in original_pals if str(pal.get("individual_id")) not in changed_pal_ids
            },
        }
        original_guilds = {str(item.get("guild_id") or ""): item for item in self.original.get("guilds", []) if item.get("guild_id")}
        guilds = []
        for guild in self.properties.get("guilds", []):
            guild_id = str(guild.get("guild_id") or ""); before_guild = original_guilds.get(guild_id)
            if not before_guild: continue
            fields = {key: guild.get(key) for key in ("name", "base_camp_level") if guild.get(key) != before_guild.get(key)}
            if fields: guilds.append({"guild_id": guild_id, "fields": fields})
        original_bases = {str(item.get("base_id") or ""): item for item in self.original.get("bases", []) if item.get("base_id")}
        bases = []
        for base in self.properties.get("bases", []):
            base_id = str(base.get("base_id") or ""); before_base = original_bases.get(base_id)
            if not before_base: continue
            fields = {}
            if base.get("name") != before_base.get("name"): fields["name"] = base.get("name")
            before_position = before_base.get("position") or {}; position = base.get("position") or {}
            changed_position = {axis: position.get(axis) for axis in ("x", "y", "z") if position.get(axis) != before_position.get(axis)}
            if changed_position: fields["position"] = changed_position
            if fields: bases.append({"base_id": base_id, "fields": fields})
        return {"format": SAVE_PATCH_FORMAT, "players": players, "pals": pals, "inventory": inventory, "guilds": guilds, "bases": bases, "invariants": invariants}


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
def list_value(prop):
    value=(prop or {}).get("value",{})
    if isinstance(value,dict):value=value.get("values",[])
    if not isinstance(value,(list,tuple)):return []
    return [item.get("value",item) if isinstance(item,dict) else item for item in value]
def container_id(entry):
    key=(entry or {}).get("key",{})
    if isinstance(key,str):return key
    for candidate in (key.get("ID"),key.get("Id"),key):
        if isinstance(candidate,dict) and candidate.get("value") is not None:return str(candidate.get("value"))
    return ""
def collect_container_ids(value,result=None,depth=0):
    result=result if result is not None else set()
    if depth>12:return result
    if isinstance(value,dict):
        for key,item in value.items():
            lowered=str(key).lower()
            if "container" in lowered and lowered.endswith(("id","ids")):
                candidate=item.get("value") if isinstance(item,dict) and "value" in item else item
                if isinstance(candidate,(str,int)) and str(candidate):result.add(str(candidate))
            collect_container_ids(item,result,depth+1)
    elif isinstance(value,list):
        for item in value:collect_container_ids(item,result,depth+1)
    return result
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
        cid=container_id(container)
        result[cid]=container["value"]["Slots"]["value"].get("values",[])
    return result
def character_container_index(world):
    result={}
    for container in world.get("CharacterContainerSaveData",{}).get("value",[]):
        cid=container_id(container)
        slots=[]
        for slot in container.get("value",{}).get("Slots",{}).get("value",{}).get("values",[]):
            raw=slot.get("RawData",{}).get("value") or {}
            instance_id=str(raw.get("instance_id") or "").lower()
            if instance_id:slots.append(instance_id)
        if cid:result[cid]=slots
    return result
def player_items(level_path,raw_uid,containers):
    result={key:[] for key in PLAYER_CONTAINER_KEYS}
    player_path=Path(level_path).parent/"Players"/(str(raw_uid).upper().replace("-","")+".sav")
    if not player_path.is_file():return result,"partial","未找到对应 Players 存档文件"
    try: save=read_gvas(player_path)[0].properties["SaveData"]["value"]
    except Exception as exc:return result,"partial","玩家背包存档解析失败："+str(exc)
    inventory=save.get("InventoryInfo",{}).get("value",{})
    for key in PLAYER_CONTAINER_KEYS:
        ref=inventory.get(key)
        if not ref:continue
        cid=str(ref["value"]["ID"]["value"])
        for slot in containers.get(cid,[]):
            raw=slot.get("RawData",{}).get("value")
            if not raw or not raw.get("item",{}).get("static_id"):continue
            result[key].append({"ContainerId":cid,"SlotIndex":int(raw.get("slot_index",0)),"ItemId":str(raw["item"]["static_id"]).lower(),"StackCount":int(raw.get("count",0)),"data_status":"complete","read_only_reason":""})
    return result,"complete",""
def decode(path):
    gvas,save_type,world=load(path); players=[]; pals=[]; containers=item_index(world); character_containers=character_container_index(world)
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        raw_uid=entry["key"].get("PlayerUId",{}).get("value"); uid=uid_text(raw_uid); sp=save_parameter(entry)
        if sp.get("IsPlayer",{}).get("value"):
            items,inventory_status,inventory_reason=player_items(path,raw_uid,containers)
            players.append({"player_uid":uid,"player_guid":str(raw_uid or "").replace("-","").upper(),"instance_id":str(entry["key"].get("InstanceId",{}).get("value","")).lower(),"nickname":sp.get("NickName",{}).get("value","") ,"level":byte_value(sp.get("Level"),1),"exp":int(sp.get("Exp",{}).get("value",0)),"hp":fixed_value(sp.get("Hp")),"shield_hp":fixed_value(sp.get("ShieldHP")),"full_stomach":round(float(sp.get("FullStomach",{}).get("value",0)),2),"status_point":{x["StatusName"]["value"]:x["StatusPoint"]["value"] for x in sp.get("GotStatusPointList",{}).get("value",{}).get("values",[])},"pals":[],"items":items,"inventory_status":inventory_status,"inventory_read_only_reason":inventory_reason,"inventory_containers":[{"key":key,"count":len(items.get(key,[])),"data_status":inventory_status} for key in PLAYER_CONTAINER_KEYS]})
        elif sp.get("OwnerPlayerUId"):
            passive=list_value(sp.get("PassiveSkillList"))
            pals.append({"individual_id":str(entry["key"].get("InstanceId",{}).get("value","")).lower(),"owner":uid_text(sp["OwnerPlayerUId"]["value"]),"nickname":sp.get("NickName",{}).get("value","") ,"level":byte_value(sp.get("Level"),1),"exp":int(sp.get("Exp",{}).get("value",0)),"type":sp.get("CharacterID",{}).get("value",""),"gender":str(sp.get("Gender",{}).get("value",{}).get("value","Unknown")).split("::")[-1],"is_lucky":bool(sp.get("IsRarePal",{}).get("value",False)),"workspeed":byte_value(sp.get("CraftSpeed"),0),"melee":byte_value(sp.get("Talent_HP"),0),"ranged":byte_value(sp.get("Talent_Shot"),0),"defense":byte_value(sp.get("Talent_Defense"),0),"rank":byte_value(sp.get("Rank"),1),"rank_attack":byte_value(sp.get("Rank_Attack"),0),"rank_defence":byte_value(sp.get("Rank_Defence"),0),"rank_craftspeed":byte_value(sp.get("Rank_CraftSpeed"),0),"skills":passive,"passive_skills":passive,"active_skills":list_value(sp.get("EquipWaza")),"learned_skills":list_value(sp.get("MasteredWaza")),"data_status":"complete"})
    pal_id_counts={}
    for pal in pals:
        identity=pal.get("individual_id","")
        if identity:pal_id_counts[identity]=pal_id_counts.get(identity,0)+1
    by_uid={p["player_uid"]:p for p in players}
    for pal in pals:
        identity=pal.get("individual_id","")
        pal["stable_id_valid"]=bool(identity) and pal_id_counts.get(identity,0)==1
        pal["read_only_reason"]="" if pal["stable_id_valid"] else ("帕鲁 InstanceId 重复，已禁止写回" if identity else "未检测到稳定帕鲁 InstanceId")
        if not pal["stable_id_valid"]:pal["data_status"]="partial"
        owner=pal.pop("owner","")
        if owner in by_uid: by_uid[owner]["pals"].append(pal)
    guilds=[]
    for group in world.get("GroupSaveDataMap",{}).get("value",[]):
        if enum_value(group.get("value",{}).get("GroupType"))!="EPalGroupType::Guild":continue
        raw=group["value"]["RawData"]["value"]
        guild_id=str(raw.get("group_id",""))
        guilds.append({"guild_id":guild_id,"name":raw.get("guild_name",""),"base_camp_level":raw.get("base_camp_level",0),"admin_player_uid":uid_text(raw.get("admin_player_uid")),"players":[{"player_uid":uid_text(x.get("player_uid")),"nickname":x.get("player_info",{}).get("player_name","")} for x in raw.get("players",[])],"base_ids":[],"data_status":"complete" if guild_id else "partial","read_only_reason":"" if guild_id else "公会缺少稳定 ID"})
    guild_by_id={str(guild.get("guild_id")):guild for guild in guilds}
    pal_by_id={str(pal.get("individual_id")):pal for pal in pals if pal.get("individual_id")}
    bases=[]
    for entry in world.get("BaseCampSaveData",{}).get("value",[]):
        base_id=str(entry.get("key") or "")
        value=entry.get("value",{});raw=value.get("RawData",{}).get("value") or {};worker_raw=value.get("WorkerDirector",{}).get("value",{}).get("RawData",{}).get("value") or {}
        guild_id=str(raw.get("group_id_belong_to") or "");worker_container_id=str(worker_raw.get("container_id") or "")
        worker_ids=list(character_containers.get(worker_container_id,[]));translation=(raw.get("transform") or {}).get("translation") or (worker_raw.get("spawn_transform") or {}).get("translation") or {}
        related=collect_container_ids(value);related.discard(worker_container_id)
        problems=[]
        if not base_id:problems.append("基地缺少稳定 ID")
        if not guild_id:problems.append("基地缺少公会归属")
        if worker_container_id and worker_container_id not in character_containers:problems.append("工作帕鲁容器未找到")
        unresolved_workers=[identity for identity in worker_ids if identity not in pal_by_id]
        if unresolved_workers:problems.append("部分工作帕鲁无法关联")
        base={"base_id":base_id,"name":raw.get("name") or "","guild_id":guild_id,"position":{"x":translation.get("x",0),"y":translation.get("y",0),"z":translation.get("z",0)},"worker_container_id":worker_container_id,"worker_pal_ids":worker_ids,"worker_pals":[{"individual_id":identity,"type":pal_by_id.get(identity,{}).get("type",""),"nickname":pal_by_id.get(identity,{}).get("nickname","")} for identity in worker_ids],"container_ids":sorted(related),"data_status":"complete" if not problems else "partial","read_only_reason":"；".join(problems)}
        bases.append(base)
        if guild_id in guild_by_id:guild_by_id[guild_id]["base_ids"].append(base_id)
    return {"format":"PlM1","save_type":save_type,"players":players,"guilds":guilds,"bases":bases,"data_status":{"players":"complete","pals":"complete" if all(p.get("data_status")=="complete" for p in pals) else "partial","guilds":"complete","bases":"complete" if all(b.get("data_status")=="complete" for b in bases) else "partial"}}
def clean_guid(value):
    clean=str(value or "").replace("-","").replace("{","").replace("}","").upper()
    return clean if len(clean)==32 and all(char in "0123456789ABCDEF" for char in clean) else ""
def decode_players(path):
    path=Path(path);_,save_type,world=load(path);by_guid={};warnings=[]
    entries=world.get("CharacterSaveParameterMap",{}).get("value",[])
    for index,entry in enumerate(entries):
        try:
            sp=save_parameter(entry)
            if not bool(sp.get("IsPlayer",{}).get("value")):continue
            raw_uid=entry.get("key",{}).get("PlayerUId",{}).get("value");player_guid=clean_guid(raw_uid)
            if not player_guid:
                warnings.append("Level.sav 玩家条目缺少有效 GUID："+str(index));continue
            by_guid[player_guid]={"player_uid":uid_text(raw_uid),"player_guid":player_guid,"instance_id":str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower(),"nickname":str(sp.get("NickName",{}).get("value","") or ""),"level":byte_value(sp.get("Level"),1),"source":"level"}
        except Exception as exc:warnings.append("Level.sav 玩家条目解析失败 "+str(index)+"："+str(exc))
    players_dir=path.parent/"Players";file_guids=[]
    if players_dir.is_dir():
        for player_path in sorted(players_dir.glob("*.sav")):
            file_guid=clean_guid(player_path.stem)
            if not file_guid:continue
            file_guids.append(file_guid)
            try:
                save=read_gvas(player_path)[0].properties.get("SaveData",{}).get("value",{})
                raw_uid=save.get("PlayerUId",{}).get("value");save_guid=clean_guid(raw_uid) or file_guid
                individual=save.get("IndividualId",{}).get("value",{})
                instance_id=str(individual.get("InstanceId",{}).get("value","")).lower()
                current=by_guid.pop(file_guid,None) or by_guid.get(save_guid) or {}
                current.update({"player_uid":current.get("player_uid") or uid_text(raw_uid or save_guid),"player_guid":save_guid,"instance_id":current.get("instance_id") or instance_id,"nickname":current.get("nickname") or "","level":current.get("level") or 1,"player_file":player_path.name,"source":"level+player" if current else "player"})
                by_guid[save_guid]=current
            except Exception as exc:
                warnings.append("玩家文件解析失败 "+player_path.name+"："+str(exc))
                by_guid.setdefault(file_guid,{"player_uid":uid_text(file_guid),"player_guid":file_guid,"instance_id":"","nickname":"","level":1,"player_file":player_path.name,"source":"filename"})
    players=sorted(by_guid.values(),key=lambda player:(player.get("player_guid")!="00000000000000000000000000000001",player.get("player_guid","")))
    return {"format":"PlM1-players-v1","save_type":save_type,"players":players,"player_files":file_guids,"warnings":warnings}
def set_byte(prop,value,field):
    nested=(prop or {}).get("value")
    if not isinstance(nested,dict) or "value" not in nested:
        raise RuntimeError(field+" 不是受支持的 ByteProperty.value.value 结构")
    nested["value"]=int(value)
def set_fixed(prop,value): prop["value"]["Value"]["value"]=int(value)
def patch(level,manifest,output):
    if manifest.get("format")!="palworld-console-save-patch-v2":raise RuntimeError("unsupported patch format")
    if "operations" in manifest:raise RuntimeError("legacy operations patch is not supported")
    gvas,save_type,world=load(level); operations={str(x["player_uid"]):x.get("fields",{}) for x in manifest.get("players",[])}
    pal_entries=list(manifest.get("pals",[]));pal_ids=[str(x["individual_id"]).lower() for x in pal_entries]
    if len(pal_ids)!=len(set(pal_ids)):raise RuntimeError("duplicate pal InstanceId in patch")
    pal_operations={str(x["individual_id"]).lower():x.get("fields",{}) for x in pal_entries}
    item_operations={(str(x["container_id"]),int(x["slot_index"])):x.get("fields",{}) for x in manifest.get("inventory",[])}
    guild_operations={str(x["guild_id"]):x.get("fields",{}) for x in manifest.get("guilds",[])}
    base_operations={str(x["base_id"]):x.get("fields",{}) for x in manifest.get("bases",[])}
    player_hits={key:0 for key in operations};pal_hits={key:0 for key in pal_operations};item_hits={key:0 for key in item_operations};guild_hits={key:0 for key in guild_operations};base_hits={key:0 for key in base_operations}
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        uid=uid_text(entry["key"].get("PlayerUId",{}).get("value")); sp=save_parameter(entry)
        if sp.get("IsPlayer",{}).get("value") and uid in operations:
            player_hits[uid]+=1; fields=operations[uid]
            if "nickname" in fields: sp["NickName"]["value"]=str(fields["nickname"])
            if "level" in fields: set_byte(sp.get("Level"),fields["level"],"Level")
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
            pal_hits[individual_id]+=1;fields=pal_operations[individual_id]
            if "nickname" in fields: sp["NickName"]["value"]=str(fields["nickname"])
            if "level" in fields:set_byte(sp.get("Level"),fields["level"],"Level")
            if "exp" in fields:sp["Exp"]["value"]=int(fields["exp"])
            for key,prop in (("workspeed","CraftSpeed"),("melee","Talent_HP"),("ranged","Talent_Shot"),("defense","Talent_Defense"),("rank","Rank")):
                if key in fields:set_byte(sp.get(prop),fields[key],prop)
            if "skills" in fields:sp["PassiveSkillList"]["value"]["values"]=list(fields["skills"])
            if "active_skills" in fields:sp["EquipWaza"]["value"]["values"]=list(fields["active_skills"])
            if "learned_skills" in fields:sp["MasteredWaza"]["value"]["values"]=list(fields["learned_skills"])
            for key,prop in (("rank_attack","Rank_Attack"),("rank_defence","Rank_Defence"),("rank_craftspeed","Rank_CraftSpeed")):
                if key in fields:set_byte(sp.get(prop),fields[key],prop)
            if "is_lucky" in fields:sp["IsRarePal"]["value"]=bool(fields["is_lucky"])
    for container in world.get("ItemContainerSaveData",{}).get("value",[]):
        cid=str(container["key"]["ID"]["value"])
        for slot in container["value"]["Slots"]["value"].get("values",[]):
            raw=slot.get("RawData",{}).get("value") or {}; identity=(cid,int(raw.get("slot_index",0)))
            if identity in item_operations and "StackCount" in item_operations[identity]:raw["count"]=int(item_operations[identity]["StackCount"]);item_hits[identity]+=1
    for group in world.get("GroupSaveDataMap",{}).get("value",[]):
        if enum_value(group.get("value",{}).get("GroupType"))!="EPalGroupType::Guild":continue
        raw=group.get("value",{}).get("RawData",{}).get("value") or {};guild_id=str(raw.get("group_id") or "")
        if guild_id in guild_operations:
            guild_hits[guild_id]+=1;fields=guild_operations[guild_id]
            if "name" in fields:raw["guild_name"]=str(fields["name"])
            if "base_camp_level" in fields:raw["base_camp_level"]=int(fields["base_camp_level"])
    for entry in world.get("BaseCampSaveData",{}).get("value",[]):
        base_id=str(entry.get("key") or "")
        if base_id in base_operations:
            base_hits[base_id]+=1;fields=base_operations[base_id];raw=entry.get("value",{}).get("RawData",{}).get("value") or {}
            if "name" in fields:raw["name"]=str(fields["name"])
            if "position" in fields:
                transform=raw.setdefault("transform",{});translation=transform.setdefault("translation",{})
                for axis,value in fields["position"].items():
                    if axis in ("x","y","z"):translation[axis]=float(value)
    invalid_players=[key for key,count in player_hits.items() if count!=1]
    invalid_pals=[key for key,count in pal_hits.items() if count!=1]
    invalid_items=[key for key,count in item_hits.items() if count!=1]
    invalid_guilds=[key for key,count in guild_hits.items() if count!=1];invalid_bases=[key for key,count in base_hits.items() if count!=1]
    if invalid_players:raise RuntimeError("player targets must match exactly once: "+",".join(sorted(invalid_players)))
    if invalid_pals:raise RuntimeError("pal InstanceId targets must match exactly once: "+",".join(sorted(invalid_pals)))
    if invalid_items:raise RuntimeError("inventory targets must match exactly once: "+",".join(f"{x[0]}:{x[1]}" for x in invalid_items))
    if invalid_guilds:raise RuntimeError("guild targets must match exactly once: "+",".join(sorted(invalid_guilds)))
    if invalid_bases:raise RuntimeError("base targets must match exactly once: "+",".join(sorted(invalid_bases)))
    output_data=compress_gvas_to_sav(gvas.write(PALWORLD_CUSTOM_PROPERTIES),save_type); Path(output).write_bytes(output_data)

def clean_world(world_path, selection_path, output_path):
    """Create and validate a world copy after conservative role/world cleanup."""
    import shutil
    world_path=Path(world_path); output_path=Path(output_path)
    if output_path.exists(): shutil.rmtree(output_path)
    shutil.copytree(world_path, output_path)
    level_path=output_path/"Level.sav"; players_path=output_path/"Players"
    if not level_path.is_file() or not players_path.is_dir(): raise RuntimeError("世界目录缺少 Level.sav 或 Players")
    selection=json.loads(Path(selection_path).read_text(encoding="utf-8"))
    wanted_players={str(value).replace("-","").upper() for value in selection.get("players",[]) if str(value).strip()}
    wanted_guilds={str(value) for value in selection.get("guilds",[]) if str(value).strip()}
    wanted_bases={str(value) for value in selection.get("bases",[]) if str(value).strip()}
    if not (wanted_players or wanted_guilds or wanted_bases): raise RuntimeError("清理选择为空")
    gvas,save_type,world=load(level_path)
    player_entries=[]; selected_entries=[]; instance_ids=set(); pal_ids=set(); player_uids={}
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        raw_uid=entry.get("key",{}).get("PlayerUId",{}).get("value")
        uid=clean_guid(raw_uid) or str(uid_text(raw_uid)).replace("-","").upper()
        sp=save_parameter(entry)
        if sp.get("IsPlayer",{}).get("value"):
            player_entries.append((uid,entry))
            if uid in wanted_players: selected_entries.append((uid,entry)); instance_ids.add(str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower())
    if len(selected_entries)!=len(wanted_players):
        missing=sorted(wanted_players-{uid for uid,_ in selected_entries}); raise RuntimeError("目标角色不存在或 UID 不唯一："+",".join(missing))
    guild_entries=[]; guild_by_player={}; selected_guild_entries=[]
    for group in world.get("GroupSaveDataMap",{}).get("value",[]):
        if enum_value(group.get("value",{}).get("GroupType"))!="EPalGroupType::Guild": continue
        raw=group.get("value",{}).get("RawData",{}).get("value") or {}; gid=str(raw.get("group_id") or "")
        guild_entries.append((gid,group,raw))
        members=[clean_guid(member.get("player_uid")) or str(uid_text(member.get("player_uid"))).replace("-","").upper() for member in raw.get("players",[]) or []]
        for member_uid in members: guild_by_player[member_uid]=(gid,raw,members)
        if gid in wanted_guilds: selected_guild_entries.append((gid,group,raw))
    if len(selected_guild_entries)!=len(wanted_guilds): raise RuntimeError("目标公会不存在或 ID 不唯一")
    base_entries=[]; selected_base_entries=[]
    for entry in world.get("BaseCampSaveData",{}).get("value",[]):
        bid=str(entry.get("key") or ""); raw=entry.get("value",{}).get("RawData",{}).get("value") or {}; base_entries.append((bid,entry,raw))
        if bid in wanted_bases: selected_base_entries.append((bid,entry,raw))
    if len(selected_base_entries)!=len(wanted_bases): raise RuntimeError("目标基地不存在或 ID 不唯一")
    for uid,_ in selected_entries:
        guild=guild_by_player.get(uid)
        if guild:
            gid,raw,members=guild; admin=clean_guid(raw.get("admin_player_uid")) or str(uid_text(raw.get("admin_player_uid"))).replace("-","").upper()
            if admin==uid: raise RuntimeError("角色是公会会长，拒绝清除："+uid)
            base_ids=[bid for bid,_,base_raw in base_entries if str(base_raw.get("group_id_belong_to") or "")==gid]
            if len(members)<=1 and base_ids: raise RuntimeError("清除角色后会留下孤立基地："+uid)
    for gid,_,raw in selected_guild_entries:
        members=raw.get("players",[]) or []
        if members: raise RuntimeError("只能清除空公会："+gid)
    container_ids=set(); removed_files=0
    for uid,_ in selected_entries:
        file_guid=uid
        player_path=players_path/(file_guid+".sav")
        if not player_path.is_file():
            candidates=list(players_path.glob("*.sav")); player_path=next((path for path in candidates if clean_guid(path.stem)==uid),player_path)
        if not player_path.is_file(): raise RuntimeError("角色文件不存在："+uid)
        player_gvas=read_gvas(player_path)[0]; save_data=player_gvas.properties.get("SaveData",{}).get("value",{}); inventory=save_data.get("InventoryInfo",{}).get("value",{})
        for prop in PLAYER_CONTAINER_KEYS:
            ref=inventory.get(prop,{}).get("value",{}).get("ID",{}).get("value") if isinstance(inventory.get(prop),dict) else None
            if ref: container_ids.add(str(ref))
        player_path.unlink(); removed_files+=1
        for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
            sp=save_parameter(entry); owner=sp.get("OwnerPlayerUId",{}).get("value")
            if (clean_guid(owner) or str(uid_text(owner)).replace("-","").upper())==uid: pal_ids.add(str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower())
    kept=[]
    for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
        instance=str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower(); sp=save_parameter(entry); owner=sp.get("OwnerPlayerUId",{}).get("value")
        owner_uid=clean_guid(owner) or str(uid_text(owner)).replace("-","").upper()
        if instance in instance_ids or instance in pal_ids or owner_uid in wanted_players: continue
        kept.append(entry)
    world.setdefault("CharacterSaveParameterMap", {"value": []})["value"]=kept
    kept_groups=[]
    for gid,group,raw in guild_entries:
        if gid in wanted_guilds: continue
        raw["players"]=[member for member in (raw.get("players",[]) or []) if (clean_guid(member.get("player_uid")) or str(uid_text(member.get("player_uid"))).replace("-","").upper()) not in wanted_players]
        kept_groups.append(group)
    world.setdefault("GroupSaveDataMap", {"value": []})["value"]=kept_groups
    world.setdefault("BaseCampSaveData", {"value": []})["value"]= [entry for bid,entry,_ in base_entries if bid not in wanted_bases]
    world.setdefault("ItemContainerSaveData", {"value": []})["value"]=[container for container in world.get("ItemContainerSaveData",{}).get("value",[]) if container_id(container) not in container_ids]
    level_path.write_bytes(compress_gvas_to_sav(gvas.write(PALWORLD_CUSTOM_PROPERTIES),save_type))
    decoded=decode(level_path); remaining={clean_guid(item.get("player_guid")) or str(item.get("player_uid","")).replace("-","").upper() for item in decoded.get("players",[])}
    if remaining & wanted_players: raise RuntimeError("清理后二次解析仍发现目标角色")
    if wanted_guilds and any(str(item.get("guild_id") or "") in wanted_guilds for item in decoded.get("guilds",[])): raise RuntimeError("清理后二次解析仍发现目标公会")
    if wanted_bases and any(str(item.get("base_id") or "") in wanted_bases for item in decoded.get("bases",[])): raise RuntimeError("清理后二次解析仍发现目标基地")
    return {"output":str(output_path),"counts":{"players":len(wanted_players),"guilds":len(wanted_guilds),"bases":len(wanted_bases),"player_files":removed_files,"containers":len(container_ids),"pals":len(pal_ids)}}
def guid(value):
    clean=str(value or "").replace("-","").lower()
    if len(clean)!=32 or any(c not in "0123456789abcdef" for c in clean):raise RuntimeError("GUID 必须是 32 位十六进制")
    return clean,"{}-{}-{}-{}-{}".format(clean[:8],clean[8:12],clean[12:16],clean[16:20],clean[20:])
def valid_uuid_text(value):
    import uuid
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return "00000000-0000-0000-0000-000000000000"
def normalize_guild_guids(world):
    """Keep raw guild GUID fields serializable across legacy numeric UID saves."""
    for group in world.get("GroupSaveDataMap",{}).get("value",[]):
        raw=group.get("value",{}).get("RawData",{}).get("value") or {}
        for key in ("admin_player_uid","last_guild_name_modifier_player_uid"):
            if key in raw: raw[key]=valid_uuid_text(raw.get(key))
        for member in raw.get("players",[]) or []:
            if isinstance(member,dict) and "player_uid" in member: member["player_uid"]=valid_uuid_text(member.get("player_uid"))
        for handle in raw.get("individual_character_handle_ids",[]) or []:
            if isinstance(handle,dict) and "guid" in handle: handle["guid"]=valid_uuid_text(handle.get("guid"))
def migrate_identities(world_path,mapping_path,output_path):
    import shutil
    world_path=Path(world_path);output_path=Path(output_path)
    manifest=json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    if manifest.get("format")!="palworld-console-identity-migration-v1":raise RuntimeError("unsupported identity migration format")
    mappings=manifest.get("mappings") or []
    if not mappings:raise RuntimeError("迁移映射为空")
    prepared=[];old_set=set();new_set=set();same_set=set()
    for item in mappings:
        old_clean,old_value=guid(item.get("old_guid"));new_clean,new_value=guid(item.get("new_guid"))
        if old_clean in old_set or new_clean in new_set:raise RuntimeError("迁移映射包含重复 GUID")
        if old_clean==new_clean:same_set.add(old_clean)
        old_set.add(old_clean);new_set.add(new_clean);prepared.append((item,old_clean,old_value,new_clean,new_value))
    if (old_set & new_set)-same_set:raise RuntimeError("迁移映射不能形成交叉覆盖")
    if output_path.exists():shutil.rmtree(output_path)
    shutil.copytree(world_path,output_path)
    level_path=output_path/"Level.sav";players_path=output_path/"Players"
    if not level_path.is_file() or not players_path.is_dir():raise RuntimeError("世界目录缺少 Level.sav 或 Players")
    level_gvas,level_type,world=load(level_path);normalize_guild_guids(world);reports=[]
    for item,old_clean,old_value,new_clean,new_value in prepared:
        old_path=players_path/(old_clean.upper()+".sav");new_path=players_path/(new_clean.upper()+".sav")
        if not old_path.is_file():raise RuntimeError("旧玩家文件不存在："+old_path.name)
        if not new_path.is_file():raise RuntimeError("专服临时玩家文件不存在："+new_path.name)
        if old_clean==new_clean:
            reports.append({"old_guid":old_clean.upper(),"new_guid":new_clean.upper(),"instance_id":str(item.get("old_instance_id") or "").lower(),"guild_updates":0,"placeholder_hits":0,"identity_preserved":True})
            continue
        player_gvas,player_type=read_gvas(old_path);save_data=player_gvas.properties["SaveData"]["value"]
        old_instance=str(save_data["IndividualId"]["value"]["InstanceId"]["value"]).lower()
        expected_instance=str(item.get("old_instance_id") or "").lower()
        if expected_instance and expected_instance!=old_instance:raise RuntimeError("旧角色 InstanceId 与确认映射不一致")
        save_data["PlayerUId"]["value"]=new_value
        save_data["IndividualId"]["value"]["PlayerUId"]["value"]=new_value
        player_hits=0; placeholder_hits=0; kept_entries=[]
        removed_instances={str(item.get("new_instance_id") or "").lower()} if str(item.get("new_instance_id") or "") else set()
        for entry in world.get("CharacterSaveParameterMap",{}).get("value",[]):
            if str(entry["key"].get("InstanceId",{}).get("value","")).lower()==old_instance:
                entry["key"]["PlayerUId"]["value"]=new_value;player_hits+=1
            if str(entry["key"].get("PlayerUId",{}).get("value","")).replace("-","").lower()==new_clean and str(item.get("new_instance_id") or "") and str(entry["key"].get("InstanceId",{}).get("value","")).lower()==str(item.get("new_instance_id")).lower():
                placeholder_hits+=1
            sp=save_parameter(entry)
            owner=sp.get("OwnerPlayerUId",{}).get("value")
            if str(owner or "").lower()==old_value:sp["OwnerPlayerUId"]["value"]=new_value
            elif str(owner or "").replace("-","").lower()==new_clean and str(item.get("new_instance_id") or ""):
                continue
            if str(entry["key"].get("InstanceId",{}).get("value","")).lower() not in removed_instances: kept_entries.append(entry)
        world["CharacterSaveParameterMap"]["value"] = kept_entries
        if player_hits!=1:raise RuntimeError("Level.sav 中原角色 InstanceId 必须恰好命中一次")
        guild_updates=0
        for group in world.get("GroupSaveDataMap",{}).get("value",[]):
            if enum_value(group.get("value",{}).get("GroupType"))!="EPalGroupType::Guild":continue
            raw=group["value"]["RawData"]["value"]
            if str(item.get("new_instance_id") or ""):
                raw["individual_character_handle_ids"] = [handle for handle in raw.get("individual_character_handle_ids",[]) if str(handle.get("instance_id","")).lower()!=str(item.get("new_instance_id")).lower()]
                if str(raw.get("admin_player_uid") or "").replace("-","").lower()==new_clean:raw["admin_player_uid"]="00000000-0000-0000-0000-000000000000"
                raw["players"] = [member for member in raw.get("players",[]) if str(member.get("player_uid") or "").replace("-","").lower()!=new_clean]
            for handle in raw.get("individual_character_handle_ids",[]):
                if str(handle.get("instance_id","")).lower()==old_instance:handle["guid"]=new_value;guild_updates+=1
            if str(raw.get("admin_player_uid") or "").lower()==old_value:raw["admin_player_uid"]=new_value;guild_updates+=1
            for member in raw.get("players",[]):
                if str(member.get("player_uid") or "").lower()==old_value:member["player_uid"]=new_value;guild_updates+=1
        new_path.unlink(missing_ok=True)
        new_path.write_bytes(compress_gvas_to_sav(player_gvas.write(PALWORLD_CUSTOM_PROPERTIES),player_type));old_path.unlink()
        reports.append({"old_guid":old_clean.upper(),"new_guid":new_clean.upper(),"instance_id":old_instance,"guild_updates":guild_updates,"placeholder_hits":placeholder_hits,"player_semantic_verified":True})
    level_path.write_bytes(compress_gvas_to_sav(level_gvas.write(PALWORLD_CUSTOM_PROPERTIES),level_type))
    decoded=decode(level_path);decoded_by_guid={str(p.get("player_guid","")).upper():p for p in decoded.get("players",[])}
    for report in reports:
        if report["new_guid"] not in decoded_by_guid:raise RuntimeError("迁移后二次解析未找到玩家："+report["new_guid"])
        if report["old_guid"]!=report["new_guid"] and (players_path/(report["old_guid"]+".sav")).exists():raise RuntimeError("迁移后旧玩家文件仍然存在")
        if not (players_path/(report["new_guid"]+".sav")).is_file():raise RuntimeError("迁移后新玩家文件不存在")
    return {"migrated":len(reports),"players":reports,"decoded_players":len(decoded.get("players",[])),"world_mode":"source-authoritative"}
def migrate_identities_v2(base_world,source_world,mapping_path,output_path):
    """Merge missing source role records into the latest server snapshot, then rebind identities."""
    import copy, shutil
    base_world=Path(base_world); source_world=Path(source_world); output_path=Path(output_path)
    manifest=json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    if manifest.get("format")!="palworld-console-identity-migration-v2":raise RuntimeError("unsupported identity migration v2 format")
    mappings=manifest.get("mappings") or []
    if not mappings:raise RuntimeError("迁移映射为空")
    staging=output_path.parent/(output_path.name+"-staging")
    if staging.exists():shutil.rmtree(staging)
    shutil.copytree(base_world,staging)
    try:
        base_level,base_type,base_data=load(staging/"Level.sav")
        _source_level,_,source_data=load(source_world/"Level.sav")
        base_entries=base_data.setdefault("CharacterSaveParameterMap",{}).setdefault("value",[])
        source_entries=source_data.get("CharacterSaveParameterMap",{}).get("value",[])
        def reference_id(prop):
            value=(prop or {}).get("value",{}) if isinstance(prop,dict) else {}
            identity=value.get("ID",{}) if isinstance(value,dict) else {}
            return str(identity.get("value") or "") if isinstance(identity,dict) else str(identity or "")
        def merge_containers(section,wanted):
            wanted={str(value).lower() for value in wanted if value}
            if not wanted:return 0
            source_rows=source_data.get(section,{}).get("value",[]); matches=[entry for entry in source_rows if container_id(entry).lower() in wanted]
            found={container_id(entry).lower() for entry in matches};missing=wanted-found
            if missing:raise RuntimeError("原始世界缺少玩家容器："+",".join(sorted(missing)))
            target_rows=base_data.setdefault(section,{}).setdefault("value",[])
            target_rows[:]=[entry for entry in target_rows if container_id(entry).lower() not in wanted]
            target_rows.extend(copy.deepcopy(entry) for entry in matches);return len(matches)
        base_instances={str(e.get("key",{}).get("InstanceId",{}).get("value","" )).lower() for e in base_entries};preserved=[];rebound=[]
        for item in mappings:
            old_clean,_=guid(item.get("old_guid")); old_value=guid(item.get("old_guid"))[1]; new_clean,_=guid(item.get("new_guid"))
            old_instance=str(item.get("old_instance_id") or "").lower()
            if not old_instance:
                for entry in source_entries:
                    if str(entry.get("key",{}).get("PlayerUId",{}).get("value","")).replace("-","").lower()==old_clean:
                        old_instance=str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower(); break
            source_entry=next((entry for entry in source_entries if str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower()==old_instance),None)
            new_instance=str(item.get("new_instance_id") or "").lower()
            source_player=source_world/"Players"/(old_clean.upper()+".sav")
            if not source_player.is_file():raise RuntimeError("原始玩家文件不存在："+source_player.name)
            source_save=read_gvas(source_player)[0].properties.get("SaveData",{}).get("value",{})
            inventory=source_save.get("InventoryInfo",{}).get("value",{});item_containers=set()
            for prop in inventory.values() if isinstance(inventory,dict) else []:
                identity=reference_id(prop)
                if identity:item_containers.add(identity)
            pal_container=reference_id(source_save.get("PalStorageContainerId"))
            item_container_count=merge_containers("ItemContainerSaveData",item_containers)
            character_container_count=merge_containers("CharacterContainerSaveData",{pal_container} if pal_container else set())
            source_pals=[entry for entry in source_entries if str(save_parameter(entry).get("OwnerPlayerUId",{}).get("value") or "").replace("-","").lower()==old_clean]
            source_pal_ids={str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower() for entry in source_pals}
            if old_clean==new_clean:
                base_entries[:]=[entry for entry in base_entries if not (str(save_parameter(entry).get("OwnerPlayerUId",{}).get("value") or "").replace("-","").lower()==old_clean and str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower() not in source_pal_ids)]
            for pal_entry in source_pals:
                pal_instance=str(pal_entry.get("key",{}).get("InstanceId",{}).get("value","")).lower()
                base_entries[:]=[entry for entry in base_entries if str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower()!=pal_instance]
                base_entries.append(copy.deepcopy(pal_entry));base_instances.add(pal_instance)
            if old_clean==new_clean:
                existing=next((entry for entry in base_entries if old_instance and str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower()==old_instance),None)
                template=next((entry for entry in base_entries if (new_instance and str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower()==new_instance) or str(entry.get("key",{}).get("PlayerUId",{}).get("value","")).replace("-","").lower()==new_clean),None)
                if source_entry is None:raise RuntimeError("原始 Level.sav 中找不到待迁移玩家记录："+old_clean.upper())
                if existing is None and template is None:raise RuntimeError("最新专服快照中找不到对应临时角色记录："+new_clean.upper())
                if existing is not None:
                    if template is not None and template is not existing:base_entries.remove(template)
                else:
                    template["key"]["PlayerUId"]["value"]=source_entry["key"]["PlayerUId"]["value"]
                    template["key"]["InstanceId"]["value"]=source_entry["key"]["InstanceId"]["value"]
                    template["value"]["RawData"]["value"]["object"]["SaveParameter"]["value"]=copy.deepcopy(save_parameter(source_entry))
                for group in base_data.get("GroupSaveDataMap",{}).get("value",[]):
                    if enum_value(group.get("value",{}).get("GroupType"))!="EPalGroupType::Guild":continue
                    raw=group["value"]["RawData"]["value"]
                    if new_instance and new_instance!=old_instance:raw["individual_character_handle_ids"]=[handle for handle in raw.get("individual_character_handle_ids",[]) if str(handle.get("instance_id","")).lower()!=new_instance]
                preserved.append({"old_guid":old_clean.upper(),"new_guid":new_clean.upper(),"instance_id":old_instance,"guild_updates":0,"placeholder_hits":1 if new_instance and new_instance!=old_instance else 0,"identity_preserved":True,"pals":len(source_pals),"item_containers":item_container_count,"character_containers":character_container_count})
                continue
            rebound.append(item)
            if old_instance and old_instance not in base_instances:
                template=next((entry for entry in base_entries if str(entry.get("key",{}).get("PlayerUId",{}).get("value","")).replace("-","").lower()==new_clean or (new_instance and str(entry.get("key",{}).get("InstanceId",{}).get("value","")).lower()==new_instance)),None)
                if source_entry is None:raise RuntimeError("原始 Level.sav 中找不到待迁移玩家记录："+old_clean.upper())
                if template is None:raise RuntimeError("最新专服快照中找不到对应临时角色记录："+new_clean.upper())
                candidate=copy.deepcopy(template)
                candidate["key"]["PlayerUId"]["value"]=source_entry["key"]["PlayerUId"]["value"]
                candidate["key"]["InstanceId"]["value"]=source_entry["key"]["InstanceId"]["value"]
                candidate["value"]["RawData"]["value"]["object"]["SaveParameter"]["value"]=copy.deepcopy(save_parameter(source_entry))
                base_entries.append(candidate);base_instances.add(old_instance)
        (staging/"Level.sav").write_bytes(compress_gvas_to_sav(base_level.write(PALWORLD_CUSTOM_PROPERTIES),base_type))
        # Source player files are authoritative for pending roles; placeholders remain from the base snapshot.
        players=staging/"Players"; source_players=source_world/"Players"; players.mkdir(exist_ok=True)
        for item in mappings:
            old_clean,_=guid(item.get("old_guid")); old_path=source_players/(old_clean.upper()+".sav")
            if old_path.is_file(): shutil.copy2(old_path,players/old_path.name)
        if rebound:
            legacy_mapping=staging.parent/(staging.name+"-mapping-v1.json")
            legacy_mapping.write_text(json.dumps({"format":"palworld-console-identity-migration-v1","mappings":rebound},ensure_ascii=False),encoding="utf-8")
            try:report=migrate_identities(staging,legacy_mapping,output_path)
            finally:legacy_mapping.unlink(missing_ok=True)
            report["migrated"]=int(report.get("migrated",0))+len(preserved);report["players"]=list(report.get("players") or [])+preserved;return report
        if output_path.exists():shutil.rmtree(output_path)
        shutil.copytree(staging,output_path);return {"migrated":len(preserved),"players":preserved,"decoded_players":len(decode(output_path/"Level.sav").get("players",[]))}
    finally:
        if staging.exists():shutil.rmtree(staging)
def convert_file(source,output):
    from palsav import json_tools
    from palsav.io import load_sav,save_sav
    source=Path(source);output=Path(output)
    if not source.is_file():raise RuntimeError("转换来源不存在")
    output.parent.mkdir(parents=True,exist_ok=True)
    if source.suffix.lower()==".sav":
        gvas=load_sav(str(source),custom_properties=PALWORLD_CUSTOM_PROPERTIES)
        json_tools.dump(gvas.dump(),str(output),minify=False,allow_nan=True)
        mode="sav-to-json"
    elif source.suffix.lower()==".json":
        gvas=GvasFile.load(json_tools.load(str(source)))
        save_sav(gvas,str(output))
        mode="json-to-sav"
    else:raise RuntimeError("仅支持 .sav 与 .json 文件")
    if not output.is_file() or output.stat().st_size==0:raise RuntimeError("转换没有生成有效输出")
    return {"mode":mode,"source":str(source),"output":str(output),"bytes":output.stat().st_size}
def restore_map(source,output):
    from palsav.io import load_sav,save_sav
    source=Path(source);output=Path(output)
    if not source.is_file():raise RuntimeError("LocalData.sav 不存在")
    gvas=load_sav(str(source),custom_properties=PALWORLD_CUSTOM_PROPERTIES);dump=gvas.dump()
    save_data=dump.get("properties",{}).get("SaveData",{}).get("value",{});masks=0;hidden=0
    def clear_values(mask):
        nonlocal masks
        values=mask.get("values")
        if isinstance(values,bytes):mask["values"]=b"\x00"*len(values);masks+=1
        elif isinstance(values,list):mask["values"]=[0]*len(values);masks+=1
    if "WorldMapUISaveDataMap" in save_data:
        for entry in save_data["WorldMapUISaveDataMap"].get("value",[]):clear_values(entry.get("value",{}).get("MaskTextureData",{}).get("value",{}))
    elif "WorldMapMaskTextureV4" in save_data:clear_values(save_data["WorldMapMaskTextureV4"].get("value",{}))
    for entry in save_data.get("Local_HiddenLocationFlagMap",{}).get("value",[]):
        if entry.get("value") is not False:entry["value"]=False;hidden+=1
    save_data["Local_ShowSkyIslandCloudOnWorldMapUI"]={"value":False,"id":None,"type":"BoolProperty"}
    output.parent.mkdir(parents=True,exist_ok=True);save_sav(GvasFile.load(dump),str(output),custom_properties=PALWORLD_CUSTOM_PROPERTIES)
    if not output.is_file() or output.stat().st_size==0:raise RuntimeError("地图恢复没有生成有效输出")
    return {"source":str(source),"output":str(output),"mask_textures":masks,"hidden_locations":hidden,"bytes":output.stat().st_size}
def expand_palbox(world_path,player_guid,slots,output_path):
    import shutil
    clean,_=guid(player_guid);slots=int(slots)
    if slots<1 or slots>99999:raise RuntimeError("Palbox 槽位必须在 1 到 99999 之间")
    world_path=Path(world_path);output_path=Path(output_path)
    player_path=world_path/"Players"/(clean.upper()+".sav")
    if not (world_path/"Level.sav").is_file() or not player_path.is_file():raise RuntimeError("世界目录缺少 Level.sav 或目标玩家文件")
    player_gvas,_=read_gvas(player_path);save_data=player_gvas.properties.get("SaveData",{}).get("value",{})
    palbox=str(save_data.get("PalStorageContainerId",{}).get("value",{}).get("ID",{}).get("value","")).lower()
    if not palbox:raise RuntimeError("目标玩家文件缺少 PalStorageContainerId")
    if output_path.exists():shutil.rmtree(output_path)
    shutil.copytree(world_path,output_path);level_path=output_path/"Level.sav";level_gvas,save_type,world=load(level_path)
    matches=[]
    for entry in world.get("CharacterContainerSaveData",{}).get("value",[]):
        if container_id(entry).lower()==palbox:matches.append(entry)
    if len(matches)!=1:raise RuntimeError("Palbox 容器必须恰好命中一次")
    value=matches[0].get("value",{});slot_values=value.get("Slots",{}).get("value",{}).get("values",[])
    used=max([int(slot.get("SlotIndex",{}).get("value",index)) for index,slot in enumerate(slot_values)]+[-1])+1
    if slots<used:raise RuntimeError("新槽位数不能小于当前已使用槽位")
    current=int(value.get("SlotNum",{}).get("value",0));value.setdefault("SlotNum",{"id":None,"type":"IntProperty"})["value"]=slots
    level_path.write_bytes(compress_gvas_to_sav(level_gvas.write(PALWORLD_CUSTOM_PROPERTIES),save_type))
    _,_,verified=load(level_path);verified_matches=[entry for entry in verified.get("CharacterContainerSaveData",{}).get("value",[]) if container_id(entry).lower()==palbox]
    actual=int(verified_matches[0].get("value",{}).get("SlotNum",{}).get("value",0)) if len(verified_matches)==1 else -1
    if actual!=slots:raise RuntimeError("Palbox 槽位写回验证失败")
    return {"player_guid":clean.upper(),"container_id":palbox,"old_slots":current,"new_slots":actual,"used_slots":used,"output":str(output_path)}
def u32(value):return int.from_bytes((value&4294967295).to_bytes(8,"little",signed=True),"little",signed=False)
def no_steam_uid(value):
    a=u32(u32(value<<8)^u32(2654435769-value));b=u32(a>>13^u32(-(value+a)));c=u32(b>>12^u32(value-a-b));d=u32(u32(c<<16)^u32(a-c-b));e=u32(d>>5^b-d-c);f=u32(e>>3^c-d-e)
    return "%08X"%u32(u32(u32(f<<10)^u32(d-f-e))>>15^e-(u32(f<<10)^u32(d-f-e))-f)
def steam_uid(value):
    from palsav.archive import UUID
    from palsav._cityhash import cityhash64
    raw=str(value).strip()
    if "steamcommunity.com/profiles/" in raw:raw=raw.split("steamcommunity.com/profiles/",1)[1].split("/",1)[0]
    if raw.lower().startswith("steam_"):raw=raw[6:]
    steam_id=int(raw)
    if steam_id<=0:raise RuntimeError("SteamID 必须是正整数")
    hashed=cityhash64(str(steam_id).encode("utf-16-le"));pal=UUID(int(u32(u32(hashed)+(hashed>>32)*23)).to_bytes(4,"little",signed=False)+b"\x00"*12)
    nosteam=no_steam_uid(int.from_bytes(pal.raw_bytes[:4],"little"))+"-0000-0000-0000-000000000000"
    return {"steam_id":str(steam_id),"palworld_uid":str(pal).upper(),"nosteam_uid":nosteam.upper()}
def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="cmd",required=True); sub.add_parser("probe")
    for name in ("decode","patch"):
        p=sub.add_parser(name);p.add_argument("--level",required=True);p.add_argument("--output",required=True)
        if name=="patch":p.add_argument("--patch",required=True)
    players=sub.add_parser("decode-players");players.add_argument("--level",required=True);players.add_argument("--output",required=True)
    migrate=sub.add_parser("migrate-identities");migrate.add_argument("--world",required=True);migrate.add_argument("--mapping",required=True);migrate.add_argument("--output",required=True)
    migrate_v2=sub.add_parser("migrate-identities-v2");migrate_v2.add_argument("--base-world",required=True);migrate_v2.add_argument("--source-world",required=True);migrate_v2.add_argument("--mapping",required=True);migrate_v2.add_argument("--output",required=True)
    convert=sub.add_parser("convert");convert.add_argument("--source",required=True);convert.add_argument("--output",required=True)
    map_restore=sub.add_parser("restore-map");map_restore.add_argument("--source",required=True);map_restore.add_argument("--output",required=True)
    palbox=sub.add_parser("expand-palbox");palbox.add_argument("--world",required=True);palbox.add_argument("--player-guid",required=True);palbox.add_argument("--slots",required=True,type=int);palbox.add_argument("--output",required=True)
    cleanup=sub.add_parser("clean-world");cleanup.add_argument("--world",required=True);cleanup.add_argument("--selection",required=True);cleanup.add_argument("--output",required=True)
    steam=sub.add_parser("steam-uid");steam.add_argument("--steam-id",required=True)
    args=parser.parse_args()
    if args.cmd=="probe":
        import palooz, palsav;print("PlM codec ready");return
    if args.cmd=="decode":Path(args.output).write_text(json.dumps(decode(args.level),ensure_ascii=False),encoding="utf-8")
    elif args.cmd=="decode-players":Path(args.output).write_text(json.dumps(decode_players(args.level),ensure_ascii=False),encoding="utf-8")
    elif args.cmd=="patch":patch(args.level,json.loads(Path(args.patch).read_text(encoding="utf-8")),args.output)
    elif args.cmd=="migrate-identities":print(json.dumps(migrate_identities(args.world,args.mapping,args.output),ensure_ascii=False))
    elif args.cmd=="migrate-identities-v2":print(json.dumps(migrate_identities_v2(args.base_world,args.source_world,args.mapping,args.output),ensure_ascii=False))
    elif args.cmd=="convert":print(json.dumps(convert_file(args.source,args.output),ensure_ascii=False))
    elif args.cmd=="restore-map":print(json.dumps(restore_map(args.source,args.output),ensure_ascii=False))
    elif args.cmd=="expand-palbox":print(json.dumps(expand_palbox(args.world,args.player_guid,args.slots,args.output),ensure_ascii=False))
    elif args.cmd=="clean-world":print(json.dumps(clean_world(args.world,args.selection,args.output),ensure_ascii=False))
    else:print(json.dumps(steam_uid(args.steam_id),ensure_ascii=False))
if __name__=="__main__":main()
'''
