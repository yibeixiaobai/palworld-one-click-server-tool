from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any, Protocol
import zipfile
import shlex


@dataclass
class ModManifest:
    package_name: str
    display_name: str = ""
    version: str = "未知"
    source: str = "local"
    workshop_id: str = ""
    install_rules: tuple[str, ...] = ()
    server_supported: bool = False
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    sha256: str = ""
    install_path: str = ""
    enabled: bool = False
    metadata_complete: bool = True
    archive_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("install_rules", "dependencies", "conflicts"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModManifest":
        values = dict(data)
        for key in ("install_rules", "dependencies", "conflicts"):
            values[key] = tuple(values.get(key) or ())
        return cls(**{key: value for key, value in values.items() if key in cls.__dataclass_fields__})


@dataclass
class ModEnvironment:
    host_system: str
    server_type: str
    wine_path: str = ""
    wine_version: str = ""
    workshop_root: str = ""
    mods_dir: str = ""
    settings_path: str = ""
    palserver_exe: str = ""
    supported: bool = False
    experimental: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModProvider(Protocol):
    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_workshop_id(value: str) -> str:
    text = value.strip()
    if text.isdigit():
        return text
    match = re.search(r"(?:[?&]id=|/filedetails/)(\d+)", text)
    if not match:
        raise ValueError("请输入有效的 Steam Workshop ID 或链接")
    return match.group(1)


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, dict):
        return tuple(str(key) for key in value)
    return tuple(str(item) for item in value)


def parse_info_json(payload: dict[str, Any], source: str, digest: str = "", archive_path: str = "") -> ModManifest:
    package = str(payload.get("PackageName") or payload.get("packageName") or "").strip()
    if not package:
        raise ValueError("Info.json 缺少 PackageName")
    rules = _as_list(payload.get("InstallRules") or payload.get("InstallRule"))
    normalized_rules = tuple(rule.lower() for rule in rules)
    server_supported = any("server" in rule or "dedicated" in rule for rule in normalized_rules)
    return ModManifest(
        package_name=package,
        display_name=str(payload.get("Name") or payload.get("DisplayName") or package),
        version=str(payload.get("Version") or payload.get("version") or "未知"),
        source=source,
        workshop_id=str(payload.get("WorkshopId") or payload.get("WorkshopID") or ""),
        install_rules=rules,
        server_supported=server_supported,
        dependencies=_as_list(payload.get("Dependencies") or payload.get("RequiredMods")),
        conflicts=_as_list(payload.get("Conflicts") or payload.get("ConflictMods")),
        sha256=digest,
        archive_path=archive_path,
    )


class LocalArchiveProvider:
    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest:
        archive = Path(source)
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            raise ValueError("请选择有效的 ZIP 模组包")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = sha256_file(archive)
        target = cache_dir / f"{digest}.zip"
        if not target.exists():
            shutil.copy2(archive, target)
        with zipfile.ZipFile(target) as bundle:
            unsafe = [name for name in bundle.namelist() if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts]
            if unsafe:
                raise ValueError("ZIP 包含不安全的路径，已拒绝导入")
            candidates = [name for name in bundle.namelist() if PurePosixPath(name).name.lower() == "info.json"]
            if not candidates:
                raise ValueError("ZIP 中未找到 Info.json")
            if len(candidates) > 1:
                candidates.sort(key=lambda name: (len(PurePosixPath(name).parts), name))
            payload = json.loads(bundle.read(candidates[0]).decode("utf-8-sig"))
        return parse_info_json(payload, "local-zip", digest, str(target))


class LocalPakProvider:
    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest:
        pak = Path(source)
        if not pak.is_file() or pak.suffix.lower() != ".pak":
            raise ValueError("请选择有效的 PAK 文件")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = sha256_file(pak)
        target = cache_dir / f"{digest}-{pak.name}"
        if not target.exists():
            shutil.copy2(pak, target)
        return ModManifest(
            package_name=pak.stem, display_name=pak.stem, source="local-pak", sha256=digest,
            server_supported=False, metadata_complete=False, archive_path=str(target),
        )


class WorkshopProvider:
    APP_ID = "1623730"

    def __init__(self, steamcmd: Path):
        self.steamcmd = steamcmd

    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest:
        workshop_id = parse_workshop_id(str(source))
        if not self.steamcmd.is_file():
            raise FileNotFoundError("未找到 SteamCMD，无法下载 Workshop 模组")
        download_root = cache_dir / "workshop"
        download_root.mkdir(parents=True, exist_ok=True)
        command = [str(self.steamcmd), "+force_install_dir", str(download_root), "+login", "anonymous", "+workshop_download_item", self.APP_ID, workshop_id, "validate", "+quit"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        if result.returncode:
            raise RuntimeError("Workshop 下载失败：" + (result.stderr.strip() or result.stdout.strip())[-800:])
        candidates = list(download_root.rglob("Info.json"))
        if not candidates:
            raise RuntimeError("Workshop 下载完成，但未找到 Info.json")
        info = sorted(candidates, key=lambda p: len(p.parts))[0]
        manifest = parse_info_json(json.loads(info.read_text(encoding="utf-8-sig")), "workshop")
        manifest.workshop_id = workshop_id
        manifest.archive_path = str(info.parent)
        manifest.sha256 = self._directory_hash(info.parent)
        return manifest

    @staticmethod
    def _directory_hash(root: Path) -> str:
        digest = hashlib.sha256()
        for file in sorted(path for path in root.rglob("*") if path.is_file()):
            digest.update(file.relative_to(root).as_posix().encode())
            digest.update(file.read_bytes())
        return digest.hexdigest()


class ModManager:
    """Validates mod state and performs rollback-capable local deployments."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    @staticmethod
    def detect_local(install_dir: Path, system_name: str = "Windows") -> ModEnvironment:
        root = install_dir.resolve() if install_dir.exists() else install_dir.absolute()
        exe = root / "PalServer.exe"
        if system_name.lower().startswith("win"):
            supported = exe.is_file()
            return ModEnvironment(system_name, "windows", workshop_root=str(root / "steamapps" / "workshop"), mods_dir=str(root / "Pal" / "Content" / "Mods"), settings_path=str(root / "Pal" / "Saved" / "Config" / "WindowsServer" / "PalModSettings.ini"), palserver_exe=str(exe), supported=supported, reason="" if supported else "未找到 PalServer.exe")
        return ModEnvironment(system_name, "linux-native", supported=False, reason="官方服务端模组不支持原生 Linux Dedicated Server")

    @staticmethod
    def detect_remote(profile: dict[str, Any]) -> ModEnvironment:
        system_name = str(profile.get("os") or profile.get("system") or "Linux")
        command = str(profile.get("service_exec") or profile.get("exec_start") or "")
        wine = str(profile.get("wine_path") or "")
        exe = str(profile.get("palserver_exe") or "")
        writable = bool(profile.get("mods_writable") and profile.get("settings_writable"))
        wine_mode = bool(wine and exe and re.search(r"\bwine(?:64)?\b", command, re.I))
        if wine_mode:
            supported = writable
            return ModEnvironment(system_name, "linux-wine", wine_path=wine, wine_version=str(profile.get("wine_version") or ""), workshop_root=str(profile.get("workshop_root") or ""), mods_dir=str(profile.get("mods_dir") or ""), settings_path=str(profile.get("mod_settings_path") or ""), palserver_exe=exe, supported=supported, experimental=True, reason="实验性 Wine 模式" if supported else "Wine 服务已检测到，但模组目录或配置不可写")
        return ModEnvironment(system_name, "linux-native", supported=False, reason="官方服务端模组不支持原生 Linux Dedicated Server")

    @staticmethod
    def validate_enable(manifest: ModManifest, installed: list[ModManifest]) -> None:
        if not manifest.metadata_complete:
            raise ValueError("PAK 缺少 Info.json，默认禁用；需明确选择目标目录并确认风险")
        if not manifest.server_supported:
            raise ValueError("该模组的 Info.json 未声明服务器安装规则，不能在服务端启用")
        enabled = {mod.package_name: mod for mod in installed if mod.enabled}
        missing = [name for name in manifest.dependencies if name not in enabled and name != manifest.package_name]
        if missing:
            raise ValueError("缺少依赖：" + "、".join(missing))
        conflicts = [name for name in manifest.conflicts if name in enabled]
        reverse = [mod.package_name for mod in enabled.values() if manifest.package_name in mod.conflicts]
        if conflicts or reverse:
            raise ValueError("模组冲突：" + "、".join(dict.fromkeys([*conflicts, *reverse])))

    def install_local(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], stop, start, health) -> ModManifest:
        if not environment.supported:
            raise RuntimeError(environment.reason or "当前服务端环境不支持模组")
        self.validate_enable(manifest, installed)
        mods_dir = Path(environment.mods_dir)
        settings = Path(environment.settings_path)
        transaction_root = self.cache_dir / "transactions" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        transaction_root.mkdir(parents=True, exist_ok=True)
        backup_mods = transaction_root / "Mods"
        backup_settings = transaction_root / "PalModSettings.ini"
        (transaction_root / "transaction.json").write_text(json.dumps({"action": "install", "package": manifest.package_name, "states": {mod.package_name: mod.enabled for mod in installed}}, ensure_ascii=False, indent=2), encoding="utf-8")
        stop()
        try:
            if mods_dir.exists():
                shutil.copytree(mods_dir, backup_mods)
            if settings.exists():
                shutil.copy2(settings, backup_settings)
            target = mods_dir / manifest.package_name
            target.mkdir(parents=True, exist_ok=True)
            source = Path(manifest.archive_path)
            if manifest.source == "local-zip":
                with zipfile.ZipFile(source) as bundle:
                    bundle.extractall(target)
            elif source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target / source.name)
            manifest.install_path = str(target)
            manifest.enabled = True
            settings.parent.mkdir(parents=True, exist_ok=True)
            enabled = [mod.package_name for mod in installed if mod.enabled and mod.package_name != manifest.package_name] + [manifest.package_name]
            settings.write_text("[PalModSettings]\nEnabledMods=" + ",".join(dict.fromkeys(enabled)) + "\n", encoding="utf-8")
            start()
            if not health():
                raise RuntimeError("模组部署后服务器健康检查失败")
            return manifest
        except Exception:
            if mods_dir.exists():
                shutil.rmtree(mods_dir)
            if backup_mods.exists():
                shutil.copytree(backup_mods, mods_dir)
            if backup_settings.exists():
                settings.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_settings, settings)
            elif settings.exists():
                settings.unlink()
            try:
                start()
            finally:
                raise

    def install_remote(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], client, stop, start, health) -> ModManifest:
        if not environment.supported or environment.server_type != "linux-wine":
            raise RuntimeError(environment.reason or "远程主机未通过实验性 Wine 模组环境检测")
        self.validate_enable(manifest, installed)
        mods_dir = environment.mods_dir
        settings = environment.settings_path
        if not mods_dir.startswith("/") or not settings.startswith("/"):
            raise ValueError("远程模组目录和配置路径必须是探测到的绝对路径")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"/tmp/palworld-mod-backup-{stamp}.tar.gz"
        upload = f"/tmp/palworld-mod-{stamp}.zip"
        source = Path(manifest.archive_path)
        temporary: tempfile.TemporaryDirectory | None = None
        if source.is_dir():
            temporary = tempfile.TemporaryDirectory(prefix="palworld-mod-")
            archive = Path(temporary.name) / "mod.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for file in source.rglob("*"):
                    if file.is_file(): bundle.write(file, file.relative_to(source))
            source = archive
        if source.suffix.lower() == ".pak":
            temporary = tempfile.TemporaryDirectory(prefix="palworld-mod-")
            archive = Path(temporary.name) / "mod.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle: bundle.write(source, source.name)
            source = archive
        qmods, qsettings, qupload, qbackup = map(shlex.quote, (mods_dir, settings, upload, backup))
        stop()
        try:
            code, _out, error = client.run(f"tar -czf {qbackup} {qmods} {qsettings} 2>/dev/null || true")
            if code: raise RuntimeError(error.strip() or "创建远程模组回滚包失败")
            client.upload_file(source, upload)
            target = str(PurePosixPath(mods_dir) / manifest.package_name)
            command = f"mkdir -p {shlex.quote(target)} && unzip -oq {qupload} -d {shlex.quote(target)} && rm -f {qupload}"
            code, _out, error = client.run(command)
            if code: raise RuntimeError(error.strip() or "远程模组文件部署失败")
            enabled = [mod.package_name for mod in installed if mod.enabled and mod.package_name != manifest.package_name] + [manifest.package_name]
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ini", delete=False) as handle:
                handle.write("[PalModSettings]\nEnabledMods=" + ",".join(dict.fromkeys(enabled)) + "\n")
                local_settings = Path(handle.name)
            remote_tmp = f"{settings}.palworld-console.tmp"
            client.upload_file(local_settings, remote_tmp); local_settings.unlink(missing_ok=True)
            code, _out, error = client.run(f"mkdir -p {shlex.quote(str(PurePosixPath(settings).parent))} && mv {shlex.quote(remote_tmp)} {qsettings}")
            if code: raise RuntimeError(error.strip() or "更新远程 PalModSettings.ini 失败")
            start()
            if not health(): raise RuntimeError("模组部署后服务器健康检查失败")
            manifest.install_path = target; manifest.enabled = True
            return manifest
        except Exception:
            client.run(f"rm -rf {qmods} {qsettings}; tar -xzf {qbackup} -C / 2>/dev/null || true; rm -f {qupload}")
            try: start()
            finally: raise
        finally:
            if temporary: temporary.cleanup()

    @staticmethod
    def _enabled_text(installed: list[ModManifest], excluded: str = "") -> str:
        enabled = [mod.package_name for mod in installed if mod.enabled and mod.package_name != excluded]
        return "[PalModSettings]\nEnabledMods=" + ",".join(dict.fromkeys(enabled)) + "\n"

    def change_local(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], remove: bool, stop, start, health) -> None:
        if not environment.supported: raise RuntimeError(environment.reason or "当前环境不支持模组")
        settings = Path(environment.settings_path); target = Path(manifest.install_path) if manifest.install_path else Path(environment.mods_dir) / manifest.package_name
        transaction = self.cache_dir / "transactions" / datetime.now().strftime("%Y%m%d-%H%M%S-%f"); transaction.mkdir(parents=True, exist_ok=True)
        backup_settings = transaction / "PalModSettings.ini"; backup_target = transaction / "mod"
        (transaction / "transaction.json").write_text(json.dumps({"action": "remove" if remove else "disable", "package": manifest.package_name, "target": str(target), "states": {mod.package_name: mod.enabled for mod in installed}}, ensure_ascii=False, indent=2), encoding="utf-8")
        stop()
        try:
            if settings.exists(): shutil.copy2(settings, backup_settings)
            if remove and target.exists(): shutil.copytree(target, backup_target)
            settings.parent.mkdir(parents=True, exist_ok=True); settings.write_text(self._enabled_text(installed, manifest.package_name), encoding="utf-8")
            if remove and target.exists(): shutil.rmtree(target)
            start()
            if not health(): raise RuntimeError("模组变更后服务器健康检查失败")
        except Exception:
            if backup_settings.exists(): shutil.copy2(backup_settings, settings)
            if remove and backup_target.exists():
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(backup_target, target)
            try: start()
            finally: raise

    def rollback_latest_local(self, environment: ModEnvironment, stop, start, health) -> dict[str, bool]:
        root = self.cache_dir / "transactions"
        candidates = sorted((path for path in root.glob("*") if (path / "transaction.json").is_file() and not (path / "rolled-back").exists()), reverse=True) if root.exists() else []
        if not candidates: raise FileNotFoundError("没有可回滚的本机模组事务")
        transaction = candidates[0]; metadata = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        mods_dir = Path(environment.mods_dir); settings = Path(environment.settings_path); backup_mods = transaction / "Mods"; backup_target = transaction / "mod"
        stop()
        try:
            if backup_mods.exists():
                if mods_dir.exists(): shutil.rmtree(mods_dir)
                shutil.copytree(backup_mods, mods_dir)
            elif backup_target.exists():
                target = Path(str(metadata.get("target") or ""))
                if not target.is_absolute(): raise RuntimeError("回滚记录缺少可信的模组绝对路径")
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(backup_target, target)
            backup_settings = transaction / "PalModSettings.ini"
            if backup_settings.exists():
                settings.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(backup_settings, settings)
            start()
            if not health(): raise RuntimeError("回滚后服务器健康检查失败")
            (transaction / "rolled-back").write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            return {str(key): bool(value) for key, value in dict(metadata.get("states") or {}).items()}
        except Exception:
            try: start()
            finally: raise

    def change_remote(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], client, remove: bool, stop, start, health) -> None:
        if not environment.supported or environment.server_type != "linux-wine": raise RuntimeError(environment.reason or "远程 Wine 模组环境不可用")
        settings = environment.settings_path; target = manifest.install_path or str(PurePosixPath(environment.mods_dir) / manifest.package_name)
        backup = f"/tmp/palworld-mod-change-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
        stop()
        try:
            client.run(f"tar -czf {shlex.quote(backup)} {shlex.quote(settings)} {shlex.quote(target)} 2>/dev/null || true")
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ini", delete=False) as handle:
                handle.write(self._enabled_text(installed, manifest.package_name)); local = Path(handle.name)
            remote_tmp = settings + ".palworld-console.tmp"; client.upload_file(local, remote_tmp); local.unlink(missing_ok=True)
            command = f"mkdir -p {shlex.quote(str(PurePosixPath(settings).parent))} && mv {shlex.quote(remote_tmp)} {shlex.quote(settings)}"
            if remove: command += f" && rm -rf {shlex.quote(target)}"
            code, _out, error = client.run(command)
            if code: raise RuntimeError(error.strip() or "远程模组变更失败")
            start()
            if not health(): raise RuntimeError("模组变更后服务器健康检查失败")
        except Exception:
            client.run(f"tar -xzf {shlex.quote(backup)} -C / 2>/dev/null || true")
            try: start()
            finally: raise
