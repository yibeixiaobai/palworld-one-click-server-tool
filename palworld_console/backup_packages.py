from __future__ import annotations

import hashlib
import copy
import json
import ntpath
import os
import re
import shlex
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .config_ini import PalWorldSettings
from .models import BackupEntry, BackupManifest, RestorePlan, RestoreResult, ServerInstance


SCHEMA = "palworld-console-backup-v1"
MAX_ENTRIES = 200_000
MAX_UNCOMPRESSED = 50 * 1024**3
MAX_COMPRESSION_RATIO = 250
SECRET_FIELDS = ("AdminPassword", "ServerPassword")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise ValueError(f"备份包含不安全路径: {name}")
    return path


def _manifest_from_dict(payload: dict) -> BackupManifest:
    entries = tuple(BackupEntry(**entry) for entry in payload.get("entries", []))
    fields = {name for name in BackupManifest.__dataclass_fields__}
    values = {key: value for key, value in payload.items() if key in fields and key != "entries"}
    for key in ("components", "redacted_fields"):
        values[key] = tuple(values.get(key) or ())
    values["entries"] = entries
    return BackupManifest(**values)


class BackupPackageService:
    """Creates and validates portable, credential-free Palworld backup packages."""

    def create(
        self,
        instance: ServerInstance,
        saved_dir: Path,
        destination: Path,
        backup_type: str = "world",
        note: str = "",
        incomplete: bool = False,
    ) -> Path:
        if backup_type not in {"world", "disaster", "restore-point"}:
            raise ValueError(f"未知备份类型: {backup_type}")
        saved_dir = saved_dir.resolve()
        savegames = saved_dir / "SaveGames"
        if not savegames.is_dir():
            raise FileNotFoundError(f"找不到 SaveGames: {savegames}")
        level_files = sorted(savegames.rglob("Level.sav"))
        if not level_files:
            incomplete = True
        package_id = str(uuid.uuid4())
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{instance.id}-{datetime.now():%Y%m%d-%H%M%S}-{backup_type}-{package_id[:8]}.pwcbackup"
        temporary = target.with_suffix(target.suffix + ".tmp")
        entries: list[BackupEntry] = []
        components = ["world"]
        payloads: list[tuple[Path, str, str, bool]] = []
        for source in sorted(savegames.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"存档包含符号链接，拒绝打包: {source}")
            if source.is_file():
                relative = source.relative_to(savegames).as_posix()
                payloads.append((source, f"payload/savegames/{relative}", "world", source.name == "Level.sav"))

        config_text = ""
        if backup_type in {"disaster", "restore-point"}:
            candidates = sorted((saved_dir / "Config").glob("*Server/PalWorldSettings.ini"))
            if candidates:
                settings = PalWorldSettings.load(candidates[0])
                for field in SECRET_FIELDS:
                    settings.values[field] = ""
                config_text = settings.render_document()
                components.append("config")

        player_ids = sorted({path.stem for path in savegames.rglob("Players/*.sav")})
        metadata = json.dumps({"player_uids": player_ids, "player_count": len(player_ids)}, ensure_ascii=False, indent=2).encode("utf-8")
        world_id = self._world_id(savegames, level_files)
        save_format = self._save_format(level_files[0]) if level_files else "unknown"
        platform_name = str(instance.remote_profile.get("platform") or ("windows" if instance.kind == "local" else "linux"))
        game_version = str(instance.last_diagnostic.get("version") or instance.remote_profile.get("game_version") or "")
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for source, archive_name, component, required in payloads:
                    archive.write(source, archive_name)
                    entries.append(BackupEntry(archive_name, _sha256(source), source.stat().st_size, component, required))
                if config_text:
                    encoded = config_text.encode("utf-8")
                    archive.writestr("payload/config/PalWorldSettings.ini", encoded)
                    entries.append(BackupEntry("payload/config/PalWorldSettings.ini", hashlib.sha256(encoded).hexdigest(), len(encoded), "config", False))
                archive.writestr("metadata/players.json", metadata)
                entries.append(BackupEntry("metadata/players.json", hashlib.sha256(metadata).hexdigest(), len(metadata), "metadata", False))
                manifest = BackupManifest(
                    schema=SCHEMA, package_id=package_id, backup_type=backup_type,
                    source_instance_id=instance.id, source_instance_name=instance.name,
                    source_platform=platform_name, created_at=_utc_now(), world_id=world_id,
                    game_version=game_version, save_format=save_format, components=tuple(components),
                    entries=tuple(entries), redacted_fields=SECRET_FIELDS if config_text else (),
                    player_count=len(player_ids), incomplete=incomplete, note=note,
                )
                manifest_bytes = json.dumps(asdict(manifest), ensure_ascii=False, indent=2).encode("utf-8")
                archive.writestr("manifest.json", manifest_bytes)
                checksums = "".join(f"{entry.sha256}  {entry.path}\n" for entry in entries).encode("utf-8")
                archive.writestr("checksums.sha256", checksums)
            self.validate(temporary)
            os.replace(temporary, target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _world_id(savegames: Path, level_files: list[Path]) -> str:
        if not level_files:
            return ""
        relative = level_files[0].relative_to(savegames)
        return relative.parent.name if relative.parent != Path(".") else ""

    @staticmethod
    def _save_format(level: Path) -> str:
        raw = level.read_bytes()[:16]
        return "PlM1/Oodle" if len(raw) >= 12 and raw[8:12].startswith(b"PlM") else "legacy"

    def read_manifest(self, package: Path, validate: bool = True) -> BackupManifest:
        if validate:
            self.validate(package)
        with zipfile.ZipFile(package) as archive:
            return _manifest_from_dict(json.loads(archive.read("manifest.json").decode("utf-8")))

    def read_player_uids(self, package: Path, structured: bool = False) -> tuple[str, ...]:
        self.validate(package)
        if structured:
            with tempfile.TemporaryDirectory(prefix="palworld-backup-players-") as temp_name:
                temp = Path(temp_name)
                self.extract(package, temp, ("world",))
                level = next(iter((temp / "payload" / "savegames").rglob("Level.sav")), None)
                if level is None:
                    raise ValueError("备份缺少 Level.sav，无法读取结构化玩家 UID")
                from .management import SaveGameService
                from .save_codec import PluginParsedSave
                document = SaveGameService().load(level)
                if not isinstance(document, PluginParsedSave):
                    raise RuntimeError("单玩家恢复仅支持经过 PlM 插件完整解析的存档")
                return tuple(sorted({str(item.get("player_uid")) for item in document.properties.get("players", []) if item.get("player_uid")}))
        with zipfile.ZipFile(package) as archive:
            payload = json.loads(archive.read("metadata/players.json").decode("utf-8"))
        return tuple(str(uid) for uid in payload.get("player_uids", []) if str(uid))

    def validate(self, package: Path) -> BackupManifest:
        if not package.is_file() or not zipfile.is_zipfile(package):
            raise ValueError(f"不是有效的 .pwcbackup 文件: {package}")
        with zipfile.ZipFile(package) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ENTRIES:
                raise ValueError("备份条目为空或超过安全上限")
            seen: set[str] = set()
            total = 0
            for info in infos:
                path = _safe_relative(info.filename)
                folded = path.as_posix().casefold()
                if folded in seen:
                    raise ValueError(f"备份包含重复或大小写冲突路径: {path}")
                seen.add(folded)
                total += info.file_size
                if total > MAX_UNCOMPRESSED:
                    raise ValueError("备份解压后大小超过安全上限")
                if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    raise ValueError(f"备份条目压缩比异常: {path}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f"备份包含符号链接: {path}")
            if archive.testzip() is not None:
                raise ValueError("备份 CRC 校验失败")
            required_names = {"manifest.json", "checksums.sha256", "metadata/players.json"}
            if not required_names.issubset({item.filename for item in infos}):
                raise ValueError("备份缺少 manifest、校验清单或玩家元数据")
            manifest = _manifest_from_dict(json.loads(archive.read("manifest.json").decode("utf-8")))
            if manifest.schema != SCHEMA:
                raise ValueError(f"不支持的备份格式: {manifest.schema}")
            checksum_lines = archive.read("checksums.sha256").decode("utf-8").splitlines()
            checksum_map = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in checksum_lines if "  " in line}
            entry_paths = {entry.path for entry in manifest.entries}
            if entry_paths != set(checksum_map):
                raise ValueError("manifest 与 SHA-256 清单不一致")
            for entry in manifest.entries:
                _safe_relative(entry.path)
                if entry.path.casefold() not in seen:
                    raise ValueError(f"manifest 引用了不存在的文件: {entry.path}")
                data = archive.read(entry.path)
                if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise ValueError(f"备份文件校验失败: {entry.path}")
                if checksum_map[entry.path] != entry.sha256:
                    raise ValueError(f"校验清单不匹配: {entry.path}")
            if not manifest.incomplete and not any(entry.required and entry.path.endswith("/Level.sav") for entry in manifest.entries):
                raise ValueError("完整备份缺少 Level.sav")
            return manifest

    def extract(self, package: Path, destination: Path, components: Iterable[str] | None = None) -> BackupManifest:
        manifest = self.validate(package)
        allowed = set(components or manifest.components) | {"metadata"}
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with zipfile.ZipFile(package) as archive:
            for entry in manifest.entries:
                if entry.component not in allowed:
                    continue
                relative = _safe_relative(entry.path)
                target = (destination / Path(*relative.parts)).resolve()
                if root != target and root not in target.parents:
                    raise ValueError(f"解压目标逃逸: {entry.path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry.path))
        return manifest

    def import_source(self, source: Path, instance: ServerInstance, destination: Path, backup_type: str = "world") -> Path:
        source = source.resolve()
        if source.is_file() and source.suffix.lower() == ".pwcbackup":
            self.validate(source)
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / source.name
            if target.exists():
                target = destination / f"{source.stem}-{uuid.uuid4().hex[:8]}.pwcbackup"
            shutil.copy2(source, target)
            self.validate(target)
            return target
        with tempfile.TemporaryDirectory(prefix="palworld-import-") as temp_name:
            temp = Path(temp_name)
            incomplete = False
            if source.is_dir():
                self._copy_directory_safe(source, temp)
            elif source.name.lower() == "level.sav":
                world = temp / "SaveGames" / "0" / f"imported-{uuid.uuid4().hex[:8]}"
                world.mkdir(parents=True)
                shutil.copy2(source, world / "Level.sav")
                incomplete = True
            elif zipfile.is_zipfile(source):
                self._extract_legacy_zip(source, temp)
            elif tarfile.is_tarfile(source):
                self._extract_legacy_tar(source, temp)
            else:
                raise ValueError("仅支持 .pwcbackup、ZIP、TAR.GZ、Saved/SaveGames 目录或 Level.sav")
            saved = self._locate_saved_root(temp)
            players = list((saved / "SaveGames").rglob("Players/*.sav")) if (saved / "SaveGames").exists() else []
            if not players:
                incomplete = True
            return self.create(instance, saved, destination, backup_type, "从旧备份或外部存档导入", incomplete)

    @staticmethod
    def _copy_directory_safe(source: Path, destination: Path) -> None:
        for path in (source, *source.rglob("*")):
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError(f"拒绝导入符号链接或重解析点: {path}")
        target = destination / source.name
        shutil.copytree(source, target, symlinks=False)

    @staticmethod
    def _extract_legacy_zip(source: Path, destination: Path) -> None:
        with zipfile.ZipFile(source) as archive:
            total = 0; seen = set()
            for info in archive.infolist():
                relative = _safe_relative(info.filename)
                folded = relative.as_posix().casefold()
                if folded in seen: raise ValueError(f"ZIP 包含重复或大小写冲突路径: {relative}")
                seen.add(folded)
                total += info.file_size
                if total > MAX_UNCOMPRESSED or (info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
                    raise ValueError("ZIP 大小或压缩比超过安全限制")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("ZIP 包含符号链接")
                target = destination / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not info.is_dir():
                    target.write_bytes(archive.read(info))

    @staticmethod
    def _extract_legacy_tar(source: Path, destination: Path) -> None:
        with tarfile.open(source) as archive:
            members = archive.getmembers()
            if len(members) > MAX_ENTRIES or sum(item.size for item in members) > MAX_UNCOMPRESSED:
                raise ValueError("TAR 大小或条目数超过安全限制")
            seen = set()
            for member in members:
                relative = _safe_relative(member.name)
                folded = relative.as_posix().casefold()
                if folded in seen: raise ValueError(f"TAR 包含重复或大小写冲突路径: {relative}")
                seen.add(folded)
                if member.issym() or member.islnk():
                    raise ValueError("TAR 包含符号链接")
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError(f"无法读取 TAR 条目: {member.name}")
                    target = destination / Path(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(stream.read())

    @staticmethod
    def _locate_saved_root(root: Path) -> Path:
        candidates = [root, root / "Saved"]
        candidates.extend(path.parent for path in root.rglob("SaveGames") if path.is_dir())
        for candidate in candidates:
            if (candidate / "SaveGames").is_dir():
                return candidate
        raise ValueError("导入内容中未找到 SaveGames 目录")

    def export(self, package: Path, destination: Path, world_only: bool = False, overwrite: bool = False) -> Path:
        manifest = self.validate(package)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"导出目标已存在: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.export-{uuid.uuid4().hex}.tmp")
        try:
            if not world_only or manifest.backup_type == "world":
                shutil.copy2(package, temporary)
            else:
                with tempfile.TemporaryDirectory(prefix="palworld-export-") as temp_name:
                    temp = Path(temp_name)
                    self.extract(package, temp, ("world",))
                    saved = temp / "payload"
                    normalized = temp / "normalized"
                    (normalized / "SaveGames").parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(saved / "savegames", normalized / "SaveGames")
                    generated = self.create(
                        ServerInstance(id=manifest.source_instance_id, name=manifest.source_instance_name, kind="remote", remote_profile={"platform": manifest.source_platform}),
                        normalized, temp / "out", "world", manifest.note, manifest.incomplete,
                    )
                    shutil.copy2(generated, temporary)
            self.validate(temporary)
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def export_report(self, package: Path, destination: Path, overwrite: bool = False) -> Path:
        manifest = self.validate(package)
        if destination.exists() and not overwrite: raise FileExistsError(f"导出目标已存在: {destination}")
        lines = [
            "Palworld Console 备份校验报告", f"文件: {package.name}", f"包 SHA-256: {_sha256(package)}",
            f"Schema: {manifest.schema}", f"包 ID: {manifest.package_id}", f"类型: {manifest.backup_type}",
            f"来源实例: {manifest.source_instance_name} ({manifest.source_instance_id})", f"世界 ID: {manifest.world_id or '未知'}",
            f"游戏版本: {manifest.game_version or '未知'}", f"存档格式: {manifest.save_format}", f"组件: {', '.join(manifest.components)}",
            f"配置脱敏字段: {', '.join(manifest.redacted_fields) or '无配置'}", "", "文件 SHA-256:",
        ]
        lines.extend(f"{entry.sha256}  {entry.path}  {entry.size_bytes} bytes" for entry in manifest.entries)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.report-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)


class BackupRepository:
    def __init__(self, root: Path, instance_id: str):
        self.root = root / instance_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.service = BackupPackageService()

    def list(self) -> list[dict]:
        records = []
        for path in sorted(self.root.glob("*.pwcbackup"), key=lambda item: item.stat().st_mtime, reverse=True):
            sidecar = self._sidecar(path)
            metadata = self._read_sidecar(sidecar)
            try:
                manifest = self.service.validate(path)
                status, error = "通过", ""
            except Exception as exc:
                manifest, status, error = None, "失败", str(exc)
            records.append({"path": str(path), "manifest": manifest, "status": status, "error": error, "size_bytes": path.stat().st_size, "note": metadata.get("note", manifest.note if manifest else ""), "protected": bool(metadata.get("protected")), "verified_at": metadata.get("verified_at", "")})
        known = {Path(record["path"]).resolve() for record in records}
        legacy_paths = list(self.root.glob("*.zip")) + list(self.root.glob("*.tar.gz")) + list(self.root.glob("*.tgz"))
        for path in sorted(legacy_paths, key=lambda item: item.stat().st_mtime, reverse=True):
            if path.resolve() in known: continue
            records.append({"path": str(path), "manifest": None, "status": "旧格式，待转换", "error": "", "size_bytes": path.stat().st_size, "note": "", "protected": False, "verified_at": ""})
        self._write_index(records)
        return records

    def import_source(self, source: Path, instance: ServerInstance, backup_type: str = "world") -> Path:
        path = self.service.import_source(source, instance, self.root, backup_type)
        self.set_metadata(path, verified_at=_utc_now())
        return path

    def import_remote_scheduled(self, client, instance: ServerInstance, known_names: Iterable[str] = ()) -> tuple[str, ...]:
        """Download newly discovered host-side scheduled archives into the verified local repository."""
        platform_name = str(instance.remote_profile.get("platform") or "linux").lower()
        install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir).rstrip("/\\")
        if not install_dir:
            return ()
        if platform_name == "windows":
            from .services import RemoteHostClient
            remote_dir = str(instance.remote_profile.get("scheduled_backup_dir") or ntpath.join(install_dir, "_backups", "palworld-console"))
            script = f"$p={RemoteHostClient._ps_literal(remote_dir)}; if(Test-Path -LiteralPath $p){{@(Get-ChildItem -LiteralPath $p -File | Where-Object {{$_.Name -match '^saved-[0-9]{{8}}-[0-9]{{6}}\\.(zip|tar\\.gz)$'}} | Select-Object Name,FullName,Length)|ConvertTo-Json -Compress}}else{{'[]'}}"
            code, output, error = client.run_powershell(script)
            if code: raise RuntimeError(error.strip() or output.strip() or "无法扫描远程计划备份")
            try:
                payload = json.loads(output.strip() or "[]")
                rows = payload if isinstance(payload, list) else [payload]
                candidates = [(str(row.get("Name") or ""), str(row.get("FullName") or "")) for row in rows]
            except (ValueError, TypeError, AttributeError) as exc:
                raise RuntimeError("Windows 计划备份扫描返回无效结果") from exc
        else:
            if not install_dir.startswith("/"): raise ValueError("Linux 远程安装目录必须是绝对路径")
            remote_dir = str(instance.remote_profile.get("scheduled_backup_dir") or f"{install_dir}/_backups/palworld-console")
            code, output, error = client.run(f"if [ -d {shlex.quote(remote_dir)} ]; then find {shlex.quote(remote_dir)} -maxdepth 1 -type f -printf '%f\\n'; fi")
            if code: raise RuntimeError(error.strip() or output.strip() or "无法扫描远程计划备份")
            candidates = [(name.strip(), f"{remote_dir}/{name.strip()}") for name in output.splitlines() if name.strip()]
        known = set(known_names)
        imported: list[str] = []
        pattern = re.compile(r"^saved-\d{8}-\d{6}\.(?:zip|tar\.gz)$", re.IGNORECASE)
        for name, remote_path in candidates:
            if name in known or not pattern.fullmatch(name) or Path(remote_path.replace("\\", "/")).name != name:
                continue
            temporary = self.root / f".incoming-{uuid.uuid4().hex}-{name}"
            try:
                client.download_file(remote_path, temporary)
                package = self.import_source(temporary, instance, "world")
                self.set_metadata(package, note=f"服务器计划备份：{name}", verified_at=_utc_now())
                imported.append(name)
            finally:
                temporary.unlink(missing_ok=True)
        return tuple(imported)

    def mark_latest(self, package: Path) -> None:
        for record in self.list():
            path = Path(record["path"])
            metadata = self._read_sidecar(self._sidecar(path))
            if metadata.get("latest"):
                metadata["latest"] = False
                manifest = record.get("manifest")
                if not manifest or manifest.backup_type != "restore-point": metadata["protected"] = False
                self.set_metadata(path, **metadata)
        self.set_metadata(package, latest=True, protected=True, verified_at=_utc_now())

    def set_metadata(self, package: Path, **values) -> None:
        sidecar = self._sidecar(package)
        payload = self._read_sidecar(sidecar)
        payload.update(values)
        temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, sidecar)

    def delete(self, package: Path) -> None:
        metadata = self._read_sidecar(self._sidecar(package))
        if metadata.get("protected"):
            raise PermissionError("该备份受保护，请先解除保护")
        package.unlink()
        self._sidecar(package).unlink(missing_ok=True)

    def enforce_retention(self, keep: int) -> None:
        candidates = [item for item in self.list() if item["status"] == "通过" and not item["protected"]]
        for record in candidates[max(1, int(keep)):]:
            self.delete(Path(record["path"]))

    def _write_index(self, records: list[dict]) -> None:
        serializable = []
        for record in records:
            item = {key: value for key, value in record.items() if key != "manifest"}
            item["manifest"] = asdict(record["manifest"]) if record["manifest"] else None
            serializable.append(item)
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.index_path)

    @staticmethod
    def _sidecar(package: Path) -> Path:
        return package.with_suffix(package.suffix + ".meta.json")

    @staticmethod
    def _read_sidecar(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError, TypeError):
            return {}


class RestoreTransaction:
    PRESERVE_CONFIG_KEYS = ("ServerName", "PublicPort", "RESTAPIPort", "RESTAPIEnabled", "PublicIP")

    def __init__(self, package_service: BackupPackageService | None = None):
        self.packages = package_service or BackupPackageService()

    def plan(self, package: Path, instance: ServerInstance, components: Iterable[str] | None = None) -> RestorePlan:
        manifest = self.packages.validate(package)
        selected = tuple(component for component in (components or manifest.components) if component in manifest.components or (component == "player" and "world" in manifest.components))
        cross = manifest.source_instance_id != instance.id
        current_version = str(instance.last_diagnostic.get("version") or instance.remote_profile.get("game_version") or "")
        version_mismatch = bool(manifest.game_version and current_version and manifest.game_version != current_version)
        current_world = str(instance.remote_profile.get("world_id") or "")
        if instance.kind == "local" and instance.install_dir:
            levels = sorted((Path(instance.install_dir) / "Pal" / "Saved" / "SaveGames").rglob("Level.sav"))
            if levels:
                savegames = Path(instance.install_dir) / "Pal" / "Saved" / "SaveGames"
                relative = levels[0].relative_to(savegames)
                current_world = relative.parent.name if relative.parent != Path(".") else ""
        world_mismatch = bool(manifest.world_id and current_world and manifest.world_id != current_world)
        summary = [f"来源实例：{manifest.source_instance_name}", f"目标实例：{instance.name}", f"世界 ID：{manifest.world_id or '未知'}", f"玩家：{manifest.player_count}", f"组件：{', '.join(selected)}"]
        if manifest.incomplete: summary.append("备份信息不完整，恢复风险较高")
        if cross: summary.append("这是跨实例迁移，将保留目标实例连接、端口和凭据")
        if version_mismatch: summary.append(f"游戏版本不一致：{manifest.game_version} -> {current_version}")
        if world_mismatch: summary.append(f"目标当前世界 ID：{current_world}")
        blocked_reason = ""
        if (version_mismatch or manifest.incomplete or "player" in selected) and ({"world", "player"} & set(selected)):
            with tempfile.TemporaryDirectory(prefix="palworld-restore-probe-") as temp_name:
                temp = Path(temp_name)
                self.packages.extract(package, temp, ("world",))
                levels = list((temp / "payload" / "savegames").rglob("Level.sav"))
                if not levels:
                    blocked_reason = "备份缺少 Level.sav，不能执行高级恢复"
                else:
                    from .management import SaveGameService
                    validation = SaveGameService().validate(levels[0])
                    if not validation.valid:
                        blocked_reason = "当前解析器无法完整验证该存档：" + "; ".join(validation.errors)
        if blocked_reason: summary.append(blocked_reason)
        estimated_components = set(selected) | ({"world"} if "player" in selected else set())
        estimated = sum(entry.size_bytes for entry in manifest.entries if entry.component in estimated_components) * 3
        return RestorePlan(str(package), manifest.source_instance_id, instance.id, selected, cross, version_mismatch, world_mismatch, version_mismatch or manifest.incomplete, estimated, tuple(summary), blocked_reason)

    def execute_local(
        self,
        package: Path,
        instance: ServerInstance,
        repository: BackupRepository,
        components: Iterable[str],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
        admin_password: str = "",
        server_password: str = "",
        player_uid: str = "",
    ) -> RestoreResult:
        plan = self.plan(package, instance, components)
        saved = Path(instance.install_dir) / "Pal" / "Saved"
        if not saved.is_dir():
            raise FileNotFoundError(f"找不到目标 Saved 目录: {saved}")
        current_size = sum(path.stat().st_size for path in saved.rglob("*") if path.is_file())
        required_space = current_size * 2 + max(1, plan.estimated_bytes // 3)
        if shutil.disk_usage(saved.parent).free < required_space:
            raise RuntimeError("恢复空间不足，需要当前存档、暂存副本和回滚副本的总空间")
        stop()
        try:
            restore_point = self.packages.create(instance, saved, repository.root, "restore-point", "恢复操作自动创建的恢复点")
            repository.set_metadata(restore_point, protected=True, verified_at=_utc_now())
        except Exception:
            start()
            raise
        rollback = saved.with_name(f"{saved.name}.rollback-{uuid.uuid4().hex}")
        staging = saved.with_name(f"{saved.name}.restore-{uuid.uuid4().hex}")
        try:
            shutil.copytree(saved, staging)
            self._apply_to_staging(package, staging, plan.components, instance, admin_password, server_password, player_uid)
            self._validate_staging(staging, plan.components)
            saved.rename(rollback)
            staging.rename(saved)
            start()
            if not health():
                raise RuntimeError("恢复后服务器健康检查失败")
            shutil.rmtree(rollback, ignore_errors=True)
            return RestoreResult(True, str(package), str(restore_point), plan.components, False, "恢复并完成健康检查")
        except Exception as exc:
            if saved.exists() and rollback.exists():
                shutil.rmtree(saved, ignore_errors=True)
            if rollback.exists():
                rollback.rename(saved)
            shutil.rmtree(staging, ignore_errors=True)
            try:
                start()
                if not health():
                    raise RuntimeError("回滚后健康检查失败")
            except Exception as rollback_exc:
                raise RuntimeError(f"恢复失败且自动回滚未通过健康检查：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"恢复失败，已自动回滚：{exc}") from exc

    def execute_remote(
        self,
        package: Path,
        instance: ServerInstance,
        client,
        repository: BackupRepository,
        components: Iterable[str],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
        admin_password: str = "",
        server_password: str = "",
        player_uid: str = "",
    ) -> RestoreResult:
        from .services import BackupService, RemoteHostClient, WindowsRemotePath

        plan = self.plan(package, instance, components)
        platform_name = str(instance.remote_profile.get("platform") or "linux").lower()
        install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir)
        if platform_name == "windows":
            install_dir = WindowsRemotePath.normalize(install_dir)
            saved_path = ntpath.join(install_dir, "Pal", "Saved")
            tool_dir = ntpath.join(install_dir, "_tools")
        else:
            if not install_dir.startswith("/"):
                raise ValueError("Linux 远程安装目录必须是绝对路径")
            saved_path = f"{install_dir}/Pal/Saved"
            tool_dir = f"{install_dir}/_tools"
        self._check_remote_space(client, platform_name, saved_path, max(1, plan.estimated_bytes // 3))
        stop()
        try:
            raw_restore_point = BackupService().create_remote(client, instance, repository.root, install_dir)
            if raw_restore_point is None: raise RuntimeError("无法创建恢复操作前的服务器恢复点")
            try:
                restore_point = repository.import_source(raw_restore_point, instance)
                repository.set_metadata(restore_point, protected=True, verified_at=_utc_now(), note="恢复操作自动创建的恢复点")
            finally:
                raw_restore_point.unlink(missing_ok=True)
        except Exception:
            start()
            raise

        token = uuid.uuid4().hex
        archive_suffix = ".zip" if platform_name == "windows" else ".tar.gz"
        remote_archive = ntpath.join(tool_dir, f"restore-{token}{archive_suffix}") if platform_name == "windows" else f"{tool_dir}/restore-{token}{archive_suffix}"
        rollback = f"{saved_path}.rollback-{token}"
        staging = f"{saved_path}.restore-{token}"
        with tempfile.TemporaryDirectory(prefix="palworld-remote-restore-") as temp_name:
            temp = Path(temp_name)
            payload = temp / "Saved"
            payload.mkdir()
            self._build_remote_payload(package, payload, plan.components, instance, client, admin_password, server_password, player_uid)
            self._validate_staging(payload, plan.components)
            local_archive = temp / f"restore{archive_suffix}"
            if platform_name == "windows":
                with zipfile.ZipFile(local_archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for path in sorted(payload.rglob("*")):
                        if path.is_file(): archive.write(path, path.relative_to(payload).as_posix())
            else:
                with tarfile.open(local_archive, "w:gz") as archive:
                    for path in sorted(payload.iterdir()): archive.add(path, arcname=path.name, recursive=True)
            local_hash = _sha256(local_archive)
            client.upload_file(local_archive, remote_archive)
            if platform_name == "windows":
                code, output, error = client.run_powershell(f"(Get-FileHash -LiteralPath {RemoteHostClient._ps_literal(remote_archive)} -Algorithm SHA256).Hash")
            else:
                code, output, error = client.run(f"sha256sum {shlex.quote(remote_archive)} | awk '{{print $1}}'")
            if code or output.strip().lower() != local_hash.lower():
                raise RuntimeError(error.strip() or "远程恢复归档 SHA-256 校验失败")
            try:
                if platform_name == "windows":
                    self._replace_remote_windows(client, saved_path, staging, rollback, remote_archive, "world" in plan.components)
                else:
                    self._replace_remote_linux(client, saved_path, staging, rollback, remote_archive, "world" in plan.components)
                start()
                if not health():
                    raise RuntimeError("恢复后服务器健康检查失败")
                self._cleanup_remote(client, platform_name, rollback, remote_archive)
                return RestoreResult(True, str(package), str(restore_point), plan.components, False, "远程恢复并完成健康检查")
            except Exception as exc:
                try:
                    try: stop()
                    except Exception: pass
                    self._rollback_remote(client, platform_name, saved_path, rollback, staging, remote_archive)
                    start()
                    if not health(): raise RuntimeError("回滚后健康检查失败")
                except Exception as rollback_exc:
                    raise RuntimeError(f"远程恢复失败且自动回滚失败：{rollback_exc}；原错误：{exc}") from exc
                raise RuntimeError(f"远程恢复失败，已自动回滚：{exc}") from exc

    @staticmethod
    def _check_remote_space(client, platform_name: str, saved_path: str, incoming_bytes: int) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            q = RemoteHostClient._ps_literal
            script = f"$s=(Get-ChildItem -LiteralPath {q(saved_path)} -File -Recurse -ErrorAction Stop|Measure-Object Length -Sum).Sum; $d=Get-PSDrive -Name ([IO.Path]::GetPathRoot({q(saved_path)}).Substring(0,1)); @{{saved=[int64]$s;free=[int64]$d.Free}}|ConvertTo-Json -Compress"
            code, output, error = client.run_powershell(script)
        else:
            code, output, error = client.run(f"saved=$(du -sb {shlex.quote(saved_path)} | awk '{{print $1}}'); free=$(df -PB1 {shlex.quote(saved_path)} | tail -1 | awk '{{print $4}}'); printf '{{\"saved\":%s,\"free\":%s}}' \"$saved\" \"$free\"")
        if code: raise RuntimeError(error.strip() or output.strip() or "无法检查远程恢复空间")
        try: payload = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc: raise RuntimeError("远程恢复空间探针返回无效结果") from exc
        required = int(payload.get("saved") or 0) * 2 + int(incoming_bytes)
        if int(payload.get("free") or 0) < required:
            raise RuntimeError(f"远程恢复空间不足：需要约 {required // 1024 // 1024} MB，可用 {int(payload.get('free') or 0) // 1024 // 1024} MB")

    def _build_remote_payload(self, package: Path, payload: Path, components: Iterable[str], instance: ServerInstance, client, admin_password: str, server_password: str, player_uid: str = "") -> None:
        selected = set(components)
        with tempfile.TemporaryDirectory(prefix="palworld-restore-content-") as extracted_name:
            extracted = Path(extracted_name)
            self.packages.extract(package, extracted, selected | ({"world"} if "player" in selected else set()))
            if "world" in selected:
                source = extracted / "payload" / "savegames"
                if not source.is_dir(): raise ValueError("备份缺少世界存档组件")
                shutil.copytree(source, payload / "SaveGames")
            if "config" in selected:
                imported_path = extracted / "payload" / "config" / "PalWorldSettings.ini"
                if not imported_path.is_file(): raise ValueError("备份缺少配置组件")
                windows = instance.remote_profile.get("platform") == "windows" or instance.remote_profile.get("wine_path")
                config_dir = "WindowsServer" if windows else "LinuxServer"
                separator = "\\" if instance.remote_profile.get("platform") == "windows" else "/"
                install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir).rstrip("/\\")
                remote_config = str(instance.remote_profile.get("config_path") or separator.join((install_dir, "Pal", "Saved", "Config", config_dir, "PalWorldSettings.ini")))
                current_text = client.read_text(remote_config, missing_ok=True)
                current = PalWorldSettings.from_text(current_text) if current_text else PalWorldSettings.from_text("OptionSettings=();")
                restored = PalWorldSettings.load(imported_path)
                for key in self.PRESERVE_CONFIG_KEYS:
                    if key in current.values: restored.values[key] = current.values[key]
                restored.values["AdminPassword"] = admin_password or str(current.values.get("AdminPassword") or "")
                restored.values["ServerPassword"] = server_password or str(current.values.get("ServerPassword") or "")
                target = payload / "Config" / config_dir / "PalWorldSettings.ini"
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(restored.render_document(), encoding="utf-8")
                os.replace(temporary, target)
            if "player" in selected:
                source_level = next(iter((extracted / "payload" / "savegames").rglob("Level.sav")), None)
                if source_level is None: raise ValueError("备份缺少用于单玩家恢复的 Level.sav")
                platform_name = str(instance.remote_profile.get("platform") or "linux").lower()
                install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir).rstrip("/\\")
                if platform_name == "windows":
                    from .services import RemoteHostClient
                    base = ntpath.join(install_dir, "Pal", "Saved", "SaveGames")
                    code, output, error = client.run_powershell(f"(Get-ChildItem -LiteralPath {RemoteHostClient._ps_literal(base)} -Filter Level.sav -File -Recurse | Select-Object -First 1 -ExpandProperty FullName)")
                    remote_level = output.strip()
                    marker = "\\SaveGames\\"
                    index = remote_level.lower().find(marker.lower())
                    relative = remote_level[index + len(marker):] if index >= 0 else ""
                else:
                    base = f"{install_dir}/Pal/Saved/SaveGames"
                    code, output, error = client.run(f"find {shlex.quote(base)} -name Level.sav -type f | head -1")
                    remote_level = output.strip(); relative = remote_level.partition("/SaveGames/")[2]
                if code or not remote_level or not relative: raise RuntimeError(error.strip() or "目标服务器未找到 Level.sav")
                target_level = Path(extracted_name) / "target-Level.sav"; candidate = Path(extracted_name) / "merged-Level.sav"
                client.download_file(remote_level, target_level)
                self._merge_single_player(source_level, target_level, candidate, player_uid)
                output_level = payload / "SaveGames" / Path(*PurePosixPath(relative.replace("\\", "/")).parts)
                output_level.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(candidate, output_level)

    @staticmethod
    def _replace_remote_linux(client, saved: str, staging: str, rollback: str, archive: str, world_selected: bool) -> None:
        commands = [
            "set -euo pipefail",
            f"test -d {shlex.quote(saved)}",
            f"rm -rf -- {shlex.quote(staging)} {shlex.quote(rollback)}",
            f"cp -a -- {shlex.quote(saved)} {shlex.quote(staging)}",
        ]
        if world_selected: commands.append(f"rm -rf -- {shlex.quote(staging + '/SaveGames')}")
        commands.extend((
            f"tar -xzf {shlex.quote(archive)} -C {shlex.quote(staging)}",
            f"test -s \"$(find {shlex.quote(staging + '/SaveGames')} -name Level.sav -type f | head -1)\"",
            f"mv -- {shlex.quote(saved)} {shlex.quote(rollback)}",
            f"mv -- {shlex.quote(staging)} {shlex.quote(saved)}",
        ))
        code, output, error = client.run("\n".join(commands))
        if code: raise RuntimeError(error.strip() or output.strip() or "Linux 远程存档原子替换失败")

    @staticmethod
    def _replace_remote_windows(client, saved: str, staging: str, rollback: str, archive: str, world_selected: bool) -> None:
        from .services import RemoteHostClient
        q = RemoteHostClient._ps_literal
        remove_world = "Remove-Item -LiteralPath (Join-Path $stage 'SaveGames') -Recurse -Force -ErrorAction SilentlyContinue" if world_selected else ""
        script = f"""
$ErrorActionPreference='Stop'; $saved={q(saved)}; $stage={q(staging)}; $rollback={q(rollback)}; $archive={q(archive)}
if(-not(Test-Path -LiteralPath $saved)){{throw '目标 Saved 目录不存在'}}
Remove-Item -LiteralPath $stage,$rollback -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath $saved -Destination $stage -Recurse -Force
{remove_world}
Expand-Archive -LiteralPath $archive -DestinationPath $stage -Force
if(-not(Get-ChildItem -LiteralPath (Join-Path $stage 'SaveGames') -Filter Level.sav -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)){{throw '暂存目录缺少 Level.sav'}}
Move-Item -LiteralPath $saved -Destination $rollback
Move-Item -LiteralPath $stage -Destination $saved
"""
        code, output, error = client.run_powershell(script)
        if code: raise RuntimeError(error.strip() or output.strip() or "Windows 远程存档原子替换失败")

    @staticmethod
    def _cleanup_remote(client, platform_name: str, rollback: str, archive: str) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            client.run_powershell(f"Remove-Item -LiteralPath {RemoteHostClient._ps_literal(rollback)},{RemoteHostClient._ps_literal(archive)} -Recurse -Force -ErrorAction SilentlyContinue")
        else:
            client.run(f"rm -rf -- {shlex.quote(rollback)} {shlex.quote(archive)}")

    @staticmethod
    def _rollback_remote(client, platform_name: str, saved: str, rollback: str, staging: str, archive: str) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            q = RemoteHostClient._ps_literal
            script = f"if(Test-Path -LiteralPath {q(rollback)}){{Remove-Item -LiteralPath {q(saved)} -Recurse -Force -ErrorAction SilentlyContinue; Move-Item -LiteralPath {q(rollback)} -Destination {q(saved)}}}; Remove-Item -LiteralPath {q(staging)},{q(archive)} -Recurse -Force -ErrorAction SilentlyContinue"
            code, output, error = client.run_powershell(script)
        else:
            script = f"set -e; if [ -d {shlex.quote(rollback)} ]; then rm -rf -- {shlex.quote(saved)}; mv -- {shlex.quote(rollback)} {shlex.quote(saved)}; fi; rm -rf -- {shlex.quote(staging)} {shlex.quote(archive)}"
            code, output, error = client.run(script)
        if code: raise RuntimeError(error.strip() or output.strip() or "远程回滚目录恢复失败")

    def _apply_to_staging(self, package: Path, staging: Path, components: Iterable[str], instance: ServerInstance, admin_password: str, server_password: str, player_uid: str = "") -> None:
        selected = set(components)
        with tempfile.TemporaryDirectory(prefix="palworld-restore-") as temp_name:
            extracted = Path(temp_name)
            self.packages.extract(package, extracted, selected | ({"world"} if "player" in selected else set()))
            if "world" in selected:
                source = extracted / "payload" / "savegames"
                if not source.is_dir():
                    raise ValueError("备份缺少世界存档组件")
                target = staging / "SaveGames"
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(source, target)
            if "config" in selected:
                imported = extracted / "payload" / "config" / "PalWorldSettings.ini"
                if not imported.is_file():
                    raise ValueError("备份缺少配置组件")
                config_dir = "WindowsServer" if instance.kind == "local" or instance.remote_profile.get("platform") == "windows" or instance.remote_profile.get("wine_path") else "LinuxServer"
                target = staging / "Config" / config_dir / "PalWorldSettings.ini"
                target.parent.mkdir(parents=True, exist_ok=True)
                current = PalWorldSettings.load(target) if target.exists() else PalWorldSettings.from_text("OptionSettings=();")
                restored = PalWorldSettings.load(imported)
                for key in self.PRESERVE_CONFIG_KEYS:
                    if key in current.values:
                        restored.values[key] = current.values[key]
                restored.values["AdminPassword"] = admin_password or str(current.values.get("AdminPassword") or "")
                restored.values["ServerPassword"] = server_password or str(current.values.get("ServerPassword") or "")
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(restored.render_document(), encoding="utf-8")
                os.replace(temporary, target)
            if "player" in selected:
                source_level = next(iter((extracted / "payload" / "savegames").rglob("Level.sav")), None)
                target_level = next(iter((staging / "SaveGames").rglob("Level.sav")), None)
                if source_level is None or target_level is None: raise ValueError("来源或目标缺少 Level.sav，无法恢复单个玩家")
                candidate = extracted / "merged-Level.sav"
                self._merge_single_player(source_level, target_level, candidate, player_uid)
                os.replace(candidate, target_level)

    @staticmethod
    def _merge_single_player(source_level: Path, target_level: Path, output: Path, player_uid: str) -> None:
        if not player_uid: raise ValueError("单玩家恢复必须选择稳定玩家 UID")
        from .management import SaveGameService
        from .save_codec import PluginParsedSave
        service = SaveGameService(); source = service.load(source_level); target = service.load(target_level)
        if not isinstance(source, PluginParsedSave) or not isinstance(target, PluginParsedSave):
            raise RuntimeError("单玩家恢复仅支持经过 PlM 插件完整解析的存档")
        source_matches = [item for item in source.properties.get("players", []) if str(item.get("player_uid")) == player_uid]
        target_matches = [item for item in target.properties.get("players", []) if str(item.get("player_uid")) == player_uid]
        if len(source_matches) != 1 or len(target_matches) != 1:
            raise ValueError(f"来源和目标存档必须各包含一个且仅一个玩家 UID {player_uid}")
        source_player, target_player = source_matches[0], target_matches[0]
        def guild_id(document):
            for guild in document.properties.get("guilds", []):
                if any(str(item.get("player_uid")) == player_uid for item in guild.get("players", [])): return str(guild.get("guild_id") or "")
            return ""
        if guild_id(source) != guild_id(target): raise ValueError("来源和目标玩家的公会关系不一致，拒绝单玩家恢复")
        for key in ("nickname", "level", "exp", "hp", "shield_hp", "full_stomach", "status_point"):
            if key in source_player: target_player[key] = copy.deepcopy(source_player[key])
        def pals(player, label):
            rows = [item for item in player.get("pals", []) if item.get("individual_id")]
            result = {str(item.get("individual_id")): item for item in rows}
            if len(result) != len(rows): raise ValueError(f"{label}玩家存在重复帕鲁 GUID")
            return result
        source_pals, target_pals = pals(source_player, "来源"), pals(target_player, "目标")
        if set(source_pals) - set(target_pals): raise ValueError("来源玩家包含目标存档不存在的帕鲁 GUID")
        for identity, source_pal in source_pals.items():
            for key in ("nickname", "level", "exp", "workspeed", "melee", "ranged", "defense", "rank", "skills"):
                if key in source_pal: target_pals[identity][key] = copy.deepcopy(source_pal[key])
        def items(player, label):
            rows = [(str(item.get("ContainerId") or container), int(item.get("SlotIndex") or 0), item) for container, values in (player.get("items") or {}).items() for item in values or []]
            result = {(container, slot): item for container, slot, item in rows}
            if len(result) != len(rows): raise ValueError(f"{label}玩家存在重复容器槽位")
            return result
        source_items, target_items = items(source_player, "来源"), items(target_player, "目标")
        if set(source_items) - set(target_items): raise ValueError("来源玩家包含目标存档不存在的容器或槽位")
        for identity, source_item in source_items.items(): target_items[identity]["StackCount"] = source_item.get("StackCount")
        service._write_document(target, output)
        validation = service.validate(output)
        if not validation.valid: raise RuntimeError("单玩家合并后二次解析失败：" + "; ".join(validation.errors))

    @staticmethod
    def _validate_staging(staging: Path, components: Iterable[str]) -> None:
        if {"world", "player"} & set(components):
            levels = list((staging / "SaveGames").rglob("Level.sav"))
            if not levels or any(path.stat().st_size == 0 for path in levels):
                raise ValueError("恢复暂存目录缺少有效 Level.sav")
            if any(path.is_symlink() for path in (staging / "SaveGames").rglob("*")):
                raise ValueError("恢复暂存目录包含符号链接")
