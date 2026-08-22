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
import time
import uuid
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .config_ini import PalWorldSettings
from .models import BackupEntry, BackupManifest, CoopMigrationSession, LocalSaveSource, PlayerIdentityMapping, RestorePlan, RestoreResult, ServerInstance, ServerWorldTarget
from .save_codec import PlmCodecPlugin


SCHEMA = "palworld-console-backup-v1"
MAX_ENTRIES = 200_000
MAX_UNCOMPRESSED = 50 * 1024**3
MAX_COMPRESSION_RATIO = 250
SECRET_FIELDS = ("AdminPassword", "ServerPassword")
WORLD_TRANSIENT_DIRS = {"backup", "backups", ".backup", ".backups"}


def _require_file(path: Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"{label}不存在：{resolved}")
    if not resolved.is_file():
        raise ValueError(f"{label}不是文件：{resolved}")
    try:
        with resolved.open("rb"):
            pass
    except OSError as exc:
        raise RuntimeError(f"无法读取{label}：{resolved}；{exc}") from exc
    return resolved


def _stage_error(stage: str, exc: Exception) -> RuntimeError:
    if isinstance(exc, FileNotFoundError):
        path = getattr(exc, "filename", None)
        detail = f"路径：{path}" if path else str(exc)
        return RuntimeError(f"{stage}失败：找不到所需文件或目录。{detail}")
    return RuntimeError(f"{stage}失败：{exc}")


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

        # 旧 disaster/restore-point 包保留脱敏配置，供历史 API 和灾备导出兼容；
        # 新的 world 备份和新的直接恢复流程只使用 SaveGames。
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

    @staticmethod
    def _world_candidates(root: Path) -> list[tuple[Path, Path, Path]]:
        """Return (savegames root, world dir, relative world path) candidates."""
        root = root.resolve(); candidates = []
        save_roots = []
        if root.name.casefold() == "savegames" and root.is_dir(): save_roots.append(root)
        for candidate in (root / "SaveGames", root / "savegames"):
            if candidate.is_dir(): save_roots.append(candidate)
        save_roots.extend(path for path in root.rglob("*") if path.is_dir() and path.name.casefold() == "savegames")
        if (root / "Level.sav").is_file():
            save_roots.append(root.parent); candidates.append((root.parent, root, Path(root.name)))
        for level in root.rglob("Level.sav"):
            world = level.parent
            relative_to_root = world.relative_to(root)
            if any(part.casefold() in WORLD_TRANSIENT_DIRS for part in relative_to_root.parts): continue
            if any(parent.name.casefold() == "savegames" for parent in world.parents if root == parent or root in parent.parents): continue
            relative = relative_to_root if relative_to_root != Path(".") else Path(world.name)
            candidates.append((root, world, relative))
        seen = set()
        for save_root in save_roots:
            save_root = save_root.resolve()
            if str(save_root).casefold() in seen: continue
            seen.add(str(save_root).casefold())
            for level in save_root.rglob("Level.sav"):
                world = level.parent
                try: relative = world.relative_to(save_root)
                except ValueError: continue
                if any(part.casefold() in WORLD_TRANSIENT_DIRS for part in relative.parts): continue
                if relative == Path("."): relative = Path(world.name)
                candidates.append((save_root, world, relative))
        unique = {}
        for item in candidates: unique[str(item[1]).casefold()] = item
        return list(unique.values())

    def inspect_save_source(self, source: Path) -> LocalSaveSource:
        source = Path(source).expanduser().resolve()
        if not source.exists(): raise FileNotFoundError(f"存档来源不存在：{source}")
        kind = "folder" if source.is_dir() else ("pwcbackup" if source.suffix.lower() == ".pwcbackup" else "level" if source.name.casefold() == "level.sav" else "zip" if zipfile.is_zipfile(source) else "tar" if tarfile.is_tarfile(source) else "file")
        warnings: list[str] = []
        if kind == "pwcbackup": self.validate(source)
        with tempfile.TemporaryDirectory(prefix="palworld-inspect-") as temp_name:
            temp = Path(temp_name)
            if kind == "pwcbackup": self.extract(source, temp, ("world",))
            elif kind == "folder": self._copy_directory_safe(source, temp)
            elif kind == "level":
                world = temp / "SaveGames" / "imported-world"; world.mkdir(parents=True); shutil.copy2(source, world / "Level.sav")
                warnings.append("仅包含 Level.sav，玩家和世界附属文件不会被恢复")
            elif kind == "zip": self._extract_legacy_zip(source, temp)
            elif kind == "tar": self._extract_legacy_tar(source, temp)
            else: raise ValueError("不支持的存档来源格式")
            candidates = self._world_candidates(temp)
            if not candidates: raise ValueError("导入内容中未找到包含 Level.sav 的世界目录")
            save_root, world, relative = max(candidates, key=lambda item: item[1].stat().st_mtime)
            files = [p for p in world.rglob("*") if p.is_file()]
            players = list(world.rglob("Players/*.sav"))
            if not players: warnings.append("未检测到 Players 文件，玩家数据可能不完整")
            modified = datetime.fromtimestamp(max((p.stat().st_mtime for p in files), default=world.stat().st_mtime), timezone.utc).isoformat()
            return LocalSaveSource(str(source), kind, str(save_root), relative.as_posix(), relative.name, len(files), sum(p.stat().st_size for p in files), True, bool(players), self._save_format(world / "Level.sav"), modified, tuple(warnings))

    def detect_local_save_sources(self) -> tuple[LocalSaveSource, ...]:
        paths = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths.extend([Path(local) / "Pal" / "Saved" / "SaveGames", Path(local) / "Pal" / "Saved"])
        paths.extend([Path.home() / "AppData" / "Local" / "Pal" / "Saved" / "SaveGames"])
        results = []; seen = set()
        for path in paths:
            try:
                key = str(path.expanduser().resolve()).casefold()
                if key in seen or not path.exists(): continue
                seen.add(key); results.append(self.inspect_save_source(path))
            except (OSError, ValueError):
                continue
        return tuple(results)

    def normalize_local_save(self, source: Path, destination: Path, on_progress: Callable[[int, str], None] | None = None) -> LocalSaveSource:
        inspection = self.inspect_save_source(source)
        source = Path(source).expanduser().resolve(); destination = Path(destination).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="palworld-normalize-") as temp_name:
            temp = Path(temp_name)
            kind = inspection.source_kind
            if kind == "pwcbackup": self.extract(source, temp, ("world",))
            elif kind == "folder": self._copy_directory_safe(source, temp)
            elif kind == "level":
                world = temp / "SaveGames" / "imported-world"; world.mkdir(parents=True); shutil.copy2(source, world / "Level.sav")
            elif kind == "zip": self._extract_legacy_zip(source, temp)
            else: self._extract_legacy_tar(source, temp)
            candidate = max(self._world_candidates(temp), key=lambda item: item[1].stat().st_mtime)
            target = destination / "SaveGames" / "imported-world"; target.mkdir(parents=True, exist_ok=True)
            files = [p for p in candidate[1].rglob("*") if p.is_file()]
            for index, path in enumerate(files, 1):
                relative = path.relative_to(candidate[1]); output = target / relative; output.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, output)
                if on_progress: on_progress(20 + round(index / max(1, len(files)) * 70), f"正在规范化 {index}/{len(files)} 个文件")
        return self.inspect_save_source(destination)

    @staticmethod
    def detect_server_world(target_savegames: Path) -> ServerWorldTarget:
        target_savegames = Path(target_savegames).expanduser().resolve()
        if not target_savegames.is_dir():
            raise FileNotFoundError(f"服务器尚未创建 SaveGames 目录：{target_savegames}")
        worlds = []
        for level in target_savegames.rglob("Level.sav"):
            world = level.parent
            files = [p for p in world.rglob("*") if p.is_file()]
            worlds.append((max((p.stat().st_mtime for p in files), default=world.stat().st_mtime), world, files))
        if not worlds: raise FileNotFoundError("服务器尚未创建世界，请先启动一次服务器并生成存档。")
        _, world, files = max(worlds, key=lambda item: item[0])
        modified = datetime.fromtimestamp(max((p.stat().st_mtime for p in files), default=world.stat().st_mtime), timezone.utc).isoformat()
        return ServerWorldTarget(str(target_savegames), str(world), world.name, len(files), modified)

    def import_local_save_to_server(self, source: Path, target_savegames: Path, stop: Callable[[], None], start: Callable[[], None], on_progress: Callable[[int, str, str], None] | None = None) -> RestoreResult:
        progress = lambda p, s, m: RestoreTransaction._emit_progress(on_progress, p, s, m)
        source = Path(source).expanduser().resolve(); progress(0, "检测本地存档", "正在识别文件夹或压缩包")
        inspection = self.inspect_save_source(source); progress(10, "安全解包", f"已识别 {inspection.file_count} 个存档文件")
        target = self.detect_server_world(target_savegames); progress(35, "检测服务器世界", f"目标世界：{target.world_id}")
        with tempfile.TemporaryDirectory(prefix="palworld-server-import-") as temp_name:
            normalized = Path(temp_name) / "normalized"; normalized.mkdir()
            self.normalize_local_save(source, normalized, lambda p, m: progress(20 + round(p * .15), "准备本地存档", m))
            source_world = normalized / "SaveGames" / "imported-world"
            files = sorted(p for p in source_world.rglob("*") if p.is_file())
            stop(); completed = 0
            try:
                for path in files:
                    relative = path.relative_to(source_world); destination = target.world_path and (Path(target.world_path) / relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(destination.name + f".import-{uuid.uuid4().hex}.tmp")
                    try: shutil.copy2(path, temporary); os.replace(temporary, destination)
                    finally: temporary.unlink(missing_ok=True)
                    completed += 1; progress(40 + round(completed / max(1, len(files)) * 50), "导入世界文件", f"已处理 {completed}/{len(files)} 个文件：{relative.as_posix()}")
                progress(92, "启动服务器", "正在启动服务器"); start(); progress(100, "导入完成", f"已写入 {completed}/{len(files)} 个文件到世界 {target.world_id}")
                return RestoreResult(True, str(source), "", ("world",), False, f"本地存档已导入到服务器世界 {target.world_id}：{completed}/{len(files)} 个文件")
            except Exception as exc:
                try: start(); restart = "服务器已重新启动"
                except Exception as start_exc: restart = f"服务器重新启动失败：{start_exc}"
                raise RuntimeError(f"本地存档导入失败，已处理 {completed}/{len(files)} 个文件；{restart}；错误：{exc}") from exc

    @staticmethod
    def _migration_path(root: Path, instance_id: str) -> Path:
        return Path(root) / "migrations" / f"{instance_id}.json"

    @staticmethod
    def save_migration_session(root: Path, session: CoopMigrationSession) -> Path:
        target = BackupPackageService._migration_path(root, session.instance_id); target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(session); payload["mappings"] = [asdict(item) for item in session.mappings]; payload["source_players"] = list(session.source_players); payload["placeholder_players"] = list(session.placeholder_players); payload["source_player_hashes"] = list(session.source_player_hashes)
        temporary = target.with_suffix(".tmp"); temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"); os.replace(temporary, target); return target

    @staticmethod
    def load_migration_session(root: Path, instance_id: str) -> CoopMigrationSession | None:
        path = BackupPackageService._migration_path(root, instance_id)
        if not path.is_file(): return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8")); payload["mappings"] = tuple(PlayerIdentityMapping(**item) for item in payload.get("mappings", [])); payload["source_players"] = tuple(payload.get("source_players", [])); payload["placeholder_players"] = tuple(payload.get("placeholder_players", [])); payload["source_player_hashes"] = tuple(payload.get("source_player_hashes", []))
            # v1 sessions used source_path for both the immutable source and the latest snapshot.
            if not payload.get("schema_version"):
                payload["schema_version"] = 2
                payload["original_source_path"] = payload.get("source_path", "")
                payload["original_source_hash"] = payload.get("source_world_hash", "")
                payload["latest_snapshot_path"] = payload.get("target_snapshot_path", "")
                payload["latest_snapshot_hash"] = payload.get("target_world_hash", "")
                payload["snapshot_generation"] = 0
            return CoopMigrationSession(**payload)
        except (OSError, ValueError, TypeError): return None

    @staticmethod
    def inspect_world_players(world: Path, app_root: Path | None = None) -> tuple[dict, ...]:
        level = Path(world) / "Level.sav"; players_dir = Path(world) / "Players"
        if not level.is_file() or not players_dir.is_dir(): raise FileNotFoundError("世界目录缺少 Level.sav 或 Players")
        plugin = PlmCodecPlugin(app_root); ready, detail = plugin.probe()
        if not ready: raise RuntimeError(f"PlM 插件不可用：{detail}")
        decoded = plugin.decode_players(level)
        players = tuple(decoded.get("players", ()))
        if not players:
            warnings = "；".join(str(item) for item in decoded.get("warnings", ())[:3])
            detail = f"；诊断：{warnings}" if warnings else ""
            raise RuntimeError(f"未能从 Level.sav 或 Players/*.sav 解析出玩家身份{detail}")
        invalid = [item for item in players if not BackupPackageService._player_guid(item) or not item.get("instance_id")]
        if invalid:
            names = ", ".join(str(item.get("player_file") or BackupPackageService._player_guid(item) or "未知玩家") for item in invalid[:4])
            raise RuntimeError(f"玩家身份缺少 GUID 或 InstanceId：{names}。请确认玩家文件与 Level.sav 来自同一次完整保存")
        return players

    def prepare_coop_migration(self, source_world: Path, instance_id: str, target_world: Path, storage_root: Path) -> CoopMigrationSession:
        players = self.inspect_world_players(source_world, storage_root)
        files = tuple(sorted(path.name for path in (Path(source_world) / "Players").glob("*.sav")))
        source = Path(source_world).resolve()
        hashes = tuple({"guid": self._player_guid(item), "hash": _sha256(source / "Players" / str(item.get("player_file") or (self._player_guid(item) + ".sav")))} for item in players if (source / "Players" / str(item.get("player_file") or (self._player_guid(item) + ".sav"))).is_file())
        session = CoopMigrationSession(instance_id=instance_id, source_path=str(source), original_source_path=str(source), target_world_path=str(Path(target_world).resolve()), phase="source_ready", source_players=players, baseline_player_files=files, source_world_hash=_sha256(source / "Level.sav"), original_source_hash=_sha256(source / "Level.sav"), source_player_hashes=hashes)
        self.save_migration_session(storage_root, session); return session

    def refresh_coop_placeholders(self, session: CoopMigrationSession, storage_root: Path) -> CoopMigrationSession:
        current = {path.name for path in (Path(session.target_world_path) / "Players").glob("*.sav")}
        added = sorted(current - set(session.baseline_player_files)); placeholders = []
        try: placeholders = list(self.inspect_world_players(Path(session.target_world_path), storage_root))
        except Exception as exc:
            updated = replace(session, phase="waiting_placeholders", placeholder_players=(), detail=f"临时角色身份解析失败：{exc}")
            self.save_migration_session(storage_root, updated)
            raise RuntimeError(updated.detail) from exc
        current_folded = {name.casefold() for name in current}; baseline_folded = {name.casefold() for name in session.baseline_player_files}
        placeholders = [item for item in placeholders if f"{self._player_guid(item)}.sav".casefold() in current_folded and f"{self._player_guid(item)}.sav".casefold() not in baseline_folded]
        updated = replace(session, phase="mapping_ready" if added else "waiting_placeholders", placeholder_players=tuple(placeholders), detail="检测到临时角色：" + ", ".join(added) if added else "尚未检测到新建角色")
        self.save_migration_session(storage_root, updated); return updated

    def build_identity_mappings(self, session: CoopMigrationSession, confirmations: dict[str, str], storage_root: Path) -> CoopMigrationSession:
        source_by_uid = {str(item.get("player_guid") or item.get("player_uid") or "").replace("-", "").upper(): item for item in session.source_players}; target_by_uid = {str(item.get("player_guid") or item.get("player_uid") or "").replace("-", "").upper(): item for item in session.placeholder_players}
        mappings = []; used = set()
        for old_guid, new_guid in confirmations.items():
            old_key = str(old_guid).replace("-", "").upper(); new_key = str(new_guid).replace("-", "").upper()
            if old_key not in source_by_uid or new_key not in target_by_uid: raise ValueError("玩家映射引用了不存在的角色")
            source = source_by_uid[old_key]; target = target_by_uid[new_key]
            if old_key == new_key: raise ValueError(f"玩家 {source.get('nickname') or old_key} 不能映射到自己的旧 GUID；请让该玩家进入专服创建新角色后选择新的 GUID")
            if new_key in used: raise ValueError(f"目标 GUID {new_key} 已分配给其他玩家，存在重复目标 GUID；每个专服临时角色只能使用一次")
            used.add(new_key)
            mappings.append(PlayerIdentityMapping(old_guid=old_key, new_guid=new_key, old_name=str(source.get("nickname") or ""), new_name=str(target.get("nickname") or ""), old_instance_id=str(source.get("instance_id") or ""), new_instance_id=str(target.get("instance_id") or ""), confirmed=True, status="confirmed"))
        if len(mappings) != len(source_by_uid): raise ValueError("必须确认全部本地角色的迁移映射")
        updated = replace(session, phase="mapping_ready", mappings=tuple(mappings), detail=f"已确认 {len(mappings)} 个玩家映射"); self.save_migration_session(storage_root, updated); return updated

    def apply_coop_migration(self, session: CoopMigrationSession, stop: Callable[[], None], start: Callable[[], None], health: Callable[[], bool] | None = None, storage_root: Path | None = None, on_progress: Callable[[int, str, str], None] | None = None) -> RestoreResult:
        if session.phase != "mapping_ready" or not session.mappings or not all(item.confirmed for item in session.mappings):
            raise ValueError("玩家映射尚未全部确认")
        world = Path(session.target_world_path).expanduser().resolve()
        source = Path(session.source_path).expanduser().resolve()
        if not world.is_dir() or not (world / "Level.sav").is_file(): raise FileNotFoundError("目标世界目录不存在或缺少 Level.sav")
        if not source.is_dir() or not (source / "Level.sav").is_file(): raise FileNotFoundError("本地世界目录不存在或缺少 Level.sav")
        progress = lambda p, s, m: RestoreTransaction._emit_progress(on_progress, p, s, m)
        plugin = PlmCodecPlugin(storage_root); ready, detail = plugin.probe()
        if not ready: raise RuntimeError(f"PlM 插件不可用：{detail}")
        with tempfile.TemporaryDirectory(prefix="palworld-coop-migration-") as temp_name:
            temp = Path(temp_name); candidate = temp / "world"; progress(5, "准备迁移", "正在校验本地和专服角色")
            # Build the candidate from the current server world so untouched players and world data remain intact.
            shutil.copytree(world, candidate)
            report = plugin.migrate_identities(candidate, [asdict(item) for item in session.mappings], temp / "migrated")
            migrated = temp / "migrated"
            if not migrated.is_dir(): raise RuntimeError("身份迁移未生成候选世界")
            progress(25, "候选验证", f"已完成 {report.get('migrated', 0)} 个玩家身份重绑定")
            backup = world.with_name(world.name + f".migration-backup-{uuid.uuid4().hex}")
            stop()
            try:
                progress(40, "创建迁移前备份", "正在保存当前专服世界")
                shutil.copytree(world, backup)
                if world.exists(): shutil.rmtree(world)
                shutil.copytree(migrated, world)
                progress(80, "写入迁移结果", "正在启动专服并检查迁移后的存档")
                start()
                if health and not RestoreTransaction._wait_for_health(health, on_wait=lambda elapsed, remaining: progress(min(98, 90 + int(elapsed / 6)), "等待服务器就绪", f"服务器启动检查中，已等待 {int(elapsed)} 秒，最多再等待 {int(remaining)} 秒")): raise RuntimeError("迁移后服务器健康检查失败")
                if storage_root:
                    updated = replace(session, phase="complete", backup_path=str(backup), detail=f"已迁移 {report.get('migrated', 0)} 个玩家")
                    self.save_migration_session(storage_root, updated)
                progress(100, "迁移完成", f"已完成 {report.get('migrated', 0)} 个玩家身份迁移")
                return RestoreResult(True, str(source), str(backup), ("world", "players"), False, f"已完成 {report.get('migrated', 0)} 个玩家身份迁移")
            except Exception as exc:
                try:
                    if world.exists(): shutil.rmtree(world)
                    if backup.exists(): backup.rename(world)
                    start()
                except Exception as rollback_exc:
                    raise RuntimeError(f"角色迁移失败且回滚失败：{rollback_exc}；原错误：{exc}") from exc
                raise RuntimeError(f"角色迁移失败，已恢复迁移前世界：{exc}") from exc

    def prepare_restore_migration(
        self,
        package: Path,
        instance: ServerInstance,
        target_saved_snapshot: Path,
        target_world_path: str,
        storage_root: Path,
        target_kind: str = "local",
        target_platform: str = "windows",
    ) -> CoopMigrationSession:
        package = _require_file(Path(package), "备份文件"); manifest = self.validate(package)
        root = Path(storage_root) / "migrations" / instance.id / "restore"
        root.mkdir(parents=True, exist_ok=True)
        extracted = root / "package"
        source_world = root / "source-world"
        target_snapshot_world = root / "target-before"
        for path in (extracted, source_world, target_snapshot_world, root / "candidate-input", root / "candidate", root / "rollback"):
            if path.exists(): shutil.rmtree(path)
        extracted.mkdir(parents=True)
        self.extract(package, extracted, ("world",))
        source_candidates = self._world_candidates(extracted / "payload")
        if not source_candidates: raise ValueError("备份中未找到包含 Level.sav 的世界目录")
        shutil.copytree(max(source_candidates, key=lambda item: item[1].stat().st_mtime)[1], source_world)
        source_player_files = tuple(sorted(path.name.upper() for path in (source_world / "Players").glob("*.sav"))) if (source_world / "Players").is_dir() else ()
        if not source_player_files:
            raise ValueError("备份不包含 Players 玩家文件，无需执行玩家迁移")
        ready, detail = PlmCodecPlugin(storage_root).probe()
        if not ready: raise RuntimeError(f"备份包含玩家，必须启用 PlM 插件后才能恢复：{detail}")
        source_players = self.inspect_world_players(source_world, storage_root)
        target_saved_snapshot = Path(target_saved_snapshot).resolve()
        target = self.detect_server_world(target_saved_snapshot / "SaveGames" if (target_saved_snapshot / "SaveGames").is_dir() else target_saved_snapshot)
        shutil.copytree(Path(target.world_path), target_snapshot_world)
        if (target_snapshot_world / "Players").is_dir():
            target_players = self.inspect_world_players(target_snapshot_world, storage_root)
        else:
            target_players = tuple(PlmCodecPlugin(storage_root).decode(target_snapshot_world / "Level.sav").get("players", ()))
        source_hash = _sha256(source_world / "Level.sav")
        source_hashes = tuple({"guid": self._player_guid(item), "hash": _sha256(source_world / "Players" / str(item.get("player_file") or (self._player_guid(item) + ".sav")))} for item in source_players if (source_world / "Players" / str(item.get("player_file") or (self._player_guid(item) + ".sav"))).is_file())
        snapshot_hash = _sha256(target_snapshot_world / "Level.sav")
        session = CoopMigrationSession(
            instance_id=instance.id, source_path=str(source_world), target_world_path=str(target_world_path), phase="mapping_ready",
            source_players=source_players, baseline_player_files=source_player_files, placeholder_players=target_players,
            package_path=str(package), package_hash=_sha256(package), source_world_hash=source_hash, original_source_path=str(source_world), original_source_hash=source_hash, source_player_hashes=source_hashes,
            target_snapshot_path=str(target_snapshot_world), target_world_hash=snapshot_hash, latest_snapshot_path=str(target_snapshot_world), latest_snapshot_hash=snapshot_hash, snapshot_generation=0,
            target_kind=target_kind, target_platform=target_platform, pending_player_guids=tuple(self._player_guid(item) for item in source_players),
            detail=f"源玩家 {len(source_players)}，目标身份 {len(target_players)}",
        )
        self.save_migration_session(storage_root, session); return session

    @staticmethod
    def _player_guid(player: dict) -> str:
        return str(player.get("player_guid") or player.get("player_uid") or "").replace("-", "").upper()

    @classmethod
    def available_identity_targets(cls, old_guid: str, targets: Iterable[dict], used_guids: Iterable[str] = ()) -> tuple[dict, ...]:
        old_key = str(old_guid).replace("-", "").upper()
        used = {str(value).replace("-", "").upper() for value in used_guids}
        result = []
        seen = set()
        for target in targets:
            target_guid = cls._player_guid(target)
            if not target_guid or target_guid == old_key or target_guid in used or target_guid in seen:
                continue
            seen.add(target_guid); result.append(target)
        return tuple(result)

    def confirm_restore_mappings(self, session: CoopMigrationSession, confirmations: dict[str, str], storage_root: Path) -> CoopMigrationSession:
        source = {self._player_guid(item): item for item in session.source_players}; targets = {self._player_guid(item): item for item in session.placeholder_players}
        completed = [item for item in session.mappings if item.status == "migrated"]
        mappings = list(completed); used = {item.new_guid for item in completed}
        allowed = set(session.pending_player_guids) if session.pending_player_guids else set(source)
        for old_guid, new_guid in confirmations.items():
            old_key = str(old_guid).replace("-", "").upper(); new_key = str(new_guid).replace("-", "").upper()
            if not new_key: continue
            if old_key not in source or old_key not in allowed or new_key not in targets: raise ValueError("玩家映射引用了不存在或已完成迁移的角色")
            old = source[old_key]; new = targets[new_key]
            if old_key == new_key: raise ValueError(f"玩家 {old.get('nickname') or old_key} 不能映射到自己的旧 GUID；请让该玩家进入专服创建新角色后选择新的 GUID")
            if new_key in used: raise ValueError(f"目标 GUID {new_key} 已分配给其他玩家，存在重复目标 GUID；每个专服临时角色只能使用一次")
            used.add(new_key)
            mappings.append(PlayerIdentityMapping(old_guid=old_key, new_guid=new_key, old_name=str(old.get("nickname") or ""), new_name=str(new.get("nickname") or ""), old_instance_id=str(old.get("instance_id") or ""), confirmed=True, new_instance_id=str(new.get("instance_id") or ""), status="confirmed"))
        pending = tuple(guid for guid in session.pending_player_guids if guid not in {item.old_guid for item in mappings}) if session.pending_player_guids else tuple(guid for guid in source if guid not in {item.old_guid for item in mappings})
        updated = replace(session, mappings=tuple(mappings), pending_player_guids=pending, phase="candidate_ready", detail=f"已确认 {len(mappings)} 个映射，待后续迁移 {len(pending)} 个")
        self.save_migration_session(storage_root, updated); return updated

    def refresh_restore_placeholders(self, session: CoopMigrationSession, current_saved_snapshot: Path, storage_root: Path) -> CoopMigrationSession:
        if session.phase != "waiting_placeholders": raise ValueError(f"当前会话阶段不能刷新临时角色：{session.phase}")
        current_saved_snapshot = Path(current_saved_snapshot).resolve()
        target = self.detect_server_world(current_saved_snapshot / "SaveGames" if (current_saved_snapshot / "SaveGames").is_dir() else current_saved_snapshot)
        current_world = Path(target.world_path)
        current_files = {path.name.upper() for path in (current_world / "Players").glob("*.sav")}
        added = current_files - set(session.baseline_player_files)
        players = self.inspect_world_players(current_world, storage_root)
        placeholders = tuple(item for item in players if f"{self._player_guid(item)}.SAV" in added)
        root = Path(storage_root) / "migrations" / session.instance_id / "restore"
        current_copy = root / "current-world"
        if current_copy.exists(): shutil.rmtree(current_copy)
        shutil.copytree(current_world, current_copy)
        phase = "mapping_ready" if placeholders else "waiting_placeholders"
        detail = f"检测到 {len(placeholders)} 个新建临时角色" if placeholders else "尚未检测到新增临时角色"
        snapshot_hash = _sha256(current_copy / "Level.sav")
        # Keep source_path as a legacy alias for callers that display the latest snapshot;
        # original_source_path remains immutable and is used for all future merges.
        updated = replace(session, source_path=str(current_copy), source_world_hash=snapshot_hash, target_snapshot_path=str(current_copy), target_world_hash=snapshot_hash, latest_snapshot_path=str(current_copy), latest_snapshot_hash=snapshot_hash, snapshot_generation=session.snapshot_generation + 1, placeholder_players=placeholders, phase=phase, detail=detail)
        self.save_migration_session(storage_root, updated); return updated

    def refresh_restore_target_snapshot(self, session: CoopMigrationSession, current_saved_snapshot: Path, storage_root: Path) -> CoopMigrationSession:
        """Pin the stopped server's latest world immediately before candidate construction."""
        current_saved_snapshot = Path(current_saved_snapshot).resolve()
        target = self.detect_server_world(current_saved_snapshot / "SaveGames" if (current_saved_snapshot / "SaveGames").is_dir() else current_saved_snapshot)
        current_world = Path(target.world_path)
        root = Path(storage_root) / "migrations" / session.instance_id / "restore"
        latest_copy = root / "deployment-world"
        if latest_copy.exists(): shutil.rmtree(latest_copy)
        shutil.copytree(current_world, latest_copy)
        snapshot_hash = _sha256(latest_copy / "Level.sav")
        updated = replace(
            session,
            target_snapshot_path=str(latest_copy),
            target_world_hash=snapshot_hash,
            latest_snapshot_path=str(latest_copy),
            latest_snapshot_hash=snapshot_hash,
            snapshot_generation=session.snapshot_generation + 1,
            detail="已锁定停服后的最新服务器世界，正在重建迁移候选",
        )
        self.save_migration_session(storage_root, updated)
        return updated

    def build_restore_candidate(self, session: CoopMigrationSession, storage_root: Path) -> tuple[CoopMigrationSession, dict]:
        original = Path(session.original_source_path or session.source_path).expanduser().resolve()
        latest = Path(session.latest_snapshot_path or session.target_snapshot_path or session.target_world_path).expanduser().resolve()
        if session.package_path and session.package_hash and _sha256(Path(session.package_path)) != session.package_hash: raise RuntimeError("恢复包在确认映射后发生变化，请重新关联原始转换包")
        if not original.is_dir() or not (original / "Level.sav").is_file(): raise RuntimeError("不可变原始联机存档不存在，请重新关联原始联机存档或转换包")
        if session.original_source_hash and _sha256(original / "Level.sav") != session.original_source_hash: raise RuntimeError("原始联机存档已变化，请重新关联原始联机存档")
        if not latest.is_dir() or not (latest / "Level.sav").is_file(): raise RuntimeError("最新专服快照不存在，请先刷新服务器快照")
        if session.latest_snapshot_hash and _sha256(latest / "Level.sav") != session.latest_snapshot_hash: raise RuntimeError("最新专服快照在映射确认后发生变化，请重新刷新服务器快照")
        root = Path(storage_root) / "migrations" / session.instance_id / "restore"; candidate_input = root / "candidate-input"; candidate = root / "candidate"
        for path in (candidate_input, candidate):
            if path.exists(): shutil.rmtree(path)
        shutil.copytree(latest, candidate_input)
        source_players = original / "Players"; target_players = latest / "Players"; candidate_players = candidate_input / "Players"; candidate_players.mkdir(exist_ok=True)
        active_mappings = tuple(item for item in session.mappings if item.status == "confirmed")
        for mapping in active_mappings:
            placeholder = target_players / f"{mapping.new_guid}.sav"
            if not placeholder.is_file(): raise FileNotFoundError(f"目标身份玩家文件不存在：{placeholder.name}")
            old_file = source_players / f"{mapping.old_guid}.sav"
            if not old_file.is_file(): raise FileNotFoundError(f"原始玩家文件不存在：{old_file.name}")
            # Keep the latest server Level/world state, while providing both source role data and the current placeholder.
            shutil.copy2(old_file, candidate_players / old_file.name)
            shutil.copy2(placeholder, candidate_players / placeholder.name)
        if active_mappings:
            report = PlmCodecPlugin(storage_root).migrate_identities_v2(latest, original, [asdict(item) for item in active_mappings], candidate)
        else:
            shutil.copytree(candidate_input, candidate); report = {"migrated": 0, "players": []}
        decoded = self.inspect_world_players(candidate, storage_root)
        active = [self._player_guid(item) for item in decoded]
        for mapping in session.mappings:
            if active.count(mapping.new_guid) != 1: raise RuntimeError(f"候选世界中的目标玩家 GUID 数量异常：{mapping.new_guid}")
            if mapping.old_guid in active: raise RuntimeError(f"候选世界仍引用旧玩家 GUID：{mapping.old_guid}")
        baseline = tuple(sorted(path.name.upper() for path in (candidate / "Players").glob("*.sav")))
        updated = replace(session, phase="deploying", baseline_player_files=baseline, detail=f"候选验证通过，已迁移 {report.get('migrated', 0)} 个玩家")
        self.save_migration_session(storage_root, updated); return updated, report

    def extract_saved_snapshot(self, archive: Path, destination: Path) -> Path:
        archive = _require_file(Path(archive), "服务器存档快照")
        if destination.exists(): shutil.rmtree(destination)
        destination.mkdir(parents=True)
        if zipfile.is_zipfile(archive): self._extract_legacy_zip(archive, destination)
        elif tarfile.is_tarfile(archive): self._extract_legacy_tar(archive, destination)
        else: raise ValueError("服务器存档快照不是受支持的 ZIP/TAR 格式")
        candidates = [path for path in (destination, *destination.rglob("*")) if path.is_dir() and (path / "SaveGames").is_dir()]
        if not candidates: raise ValueError("服务器快照中未找到 Saved/SaveGames")
        return min(candidates, key=lambda path: len(path.parts))

    def deploy_restore_candidate_local(self, session: CoopMigrationSession, stop: Callable[[], None], start: Callable[[], None], health: Callable[[], bool], storage_root: Path, restore_point: str = "", on_progress: Callable[[int, str, str], None] | None = None) -> RestoreResult:
        progress = lambda p, s, m: RestoreTransaction._emit_progress(on_progress, p, s, m)
        target = Path(session.target_world_path).resolve(); candidate = Path(storage_root) / "migrations" / session.instance_id / "restore" / "candidate"
        if not target.is_dir() or not candidate.is_dir(): raise FileNotFoundError("服务器目标世界或恢复候选不存在")
        if _sha256(target / "Level.sav") != session.target_world_hash: raise RuntimeError("服务器存档在映射确认后发生变化，请重新执行恢复预检")
        rollback = Path(storage_root) / "migrations" / session.instance_id / "restore" / "rollback"
        if rollback.exists(): shutil.rmtree(rollback)
        progress(35, "创建恢复点", "候选已验证，正在停止服务器")
        stop()
        try:
            shutil.copytree(target, rollback)
            replacement = target.with_name(target.name + f".restore-{uuid.uuid4().hex}"); shutil.copytree(candidate, replacement)
            shutil.rmtree(target); replacement.rename(target)
            progress(82, "启动服务器", "服务器世界已替换，正在等待健康检查")
            start()
            if not RestoreTransaction._wait_for_health(health, on_wait=lambda elapsed, remaining: progress(min(98, 82 + int(elapsed / 4)), "等待服务器就绪", f"服务器启动检查中，已等待 {int(elapsed)} 秒，最多再等待 {int(remaining)} 秒")): raise RuntimeError("恢复后服务器健康检查失败")
            players = self.inspect_world_players(target, storage_root); active = [self._player_guid(item) for item in players]
            for mapping in session.mappings:
                if active.count(mapping.new_guid) != 1: raise RuntimeError(f"服务器复查未找到唯一目标玩家：{mapping.new_guid}")
            phase = "waiting_placeholders" if session.pending_player_guids else "complete"
            migrated = tuple(replace(item, status="migrated") if item.status == "confirmed" else item for item in session.mappings)
            updated = replace(session, mappings=migrated, phase=phase, backup_path=restore_point or str(rollback), detail=f"世界恢复完成；已迁移 {len(migrated)} 个玩家；待迁移 {len(session.pending_player_guids)} 个")
            self.save_migration_session(storage_root, updated); progress(100, "恢复完成", updated.detail)
            return RestoreResult(True, session.package_path, updated.backup_path, ("world", "players"), False, updated.detail)
        except Exception as exc:
            try:
                if target.exists(): shutil.rmtree(target)
                if rollback.exists(): shutil.copytree(rollback, target)
                start()
            except Exception as rollback_exc: raise RuntimeError(f"恢复失败且回滚失败：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"恢复失败，已恢复原世界：{exc}") from exc

    def deploy_restore_candidate_remote(self, session: CoopMigrationSession, client, stop: Callable[[], None], start: Callable[[], None], health: Callable[[], bool], storage_root: Path, restore_point: str = "", on_progress: Callable[[int, str, str], None] | None = None) -> RestoreResult:
        from .services import RemoteHostClient
        progress = lambda p, s, m: RestoreTransaction._emit_progress(on_progress, p, s, m); platform_name = session.target_platform.lower()
        candidate = Path(storage_root) / "migrations" / session.instance_id / "restore" / "candidate"
        if not candidate.is_dir(): raise FileNotFoundError("恢复候选目录不存在")
        check = Path(storage_root) / "migrations" / session.instance_id / "restore" / "remote-current-Level.sav"; check.parent.mkdir(parents=True, exist_ok=True)
        remote_level = ntpath.join(session.target_world_path, "Level.sav") if platform_name == "windows" else f"{session.target_world_path}/Level.sav"
        client.download_file(remote_level, check)
        if _sha256(check) != session.target_world_hash: raise RuntimeError("远程服务器存档在映射确认后发生变化，请重新执行恢复预检")
        token = uuid.uuid4().hex; archive = check.parent / ("candidate.zip" if platform_name == "windows" else "candidate.tar.gz")
        if platform_name == "windows":
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                for path in candidate.rglob("*"):
                    if path.is_file(): bundle.write(path, path.relative_to(candidate).as_posix())
            remote_archive = ntpath.join(ntpath.dirname(session.target_world_path), f"restore-{token}.zip")
        else:
            with tarfile.open(archive, "w:gz") as bundle:
                for path in candidate.iterdir(): bundle.add(path, arcname=path.name, recursive=True)
            remote_archive = f"/tmp/palworld-restore-{token}.tar.gz"
        progress(48, "上传候选世界", f"正在上传候选归档：{archive.name}")
        client.upload_file(archive, remote_archive); rollback = session.target_world_path + f".rollback-{token}"; staging = session.target_world_path + f".restore-{token}"
        progress(55, "上传候选世界", "候选世界已上传，正在停止服务器")
        stop()
        try:
            if platform_name == "windows":
                q = RemoteHostClient._ps_literal
                script = f"$ErrorActionPreference='Stop';$target={q(session.target_world_path)};$stage={q(staging)};$rollback={q(rollback)};Remove-Item -LiteralPath $stage,$rollback -Recurse -Force -ErrorAction SilentlyContinue;New-Item -ItemType Directory -Force -Path $stage|Out-Null;Expand-Archive -LiteralPath {q(remote_archive)} -DestinationPath $stage -Force;if(-not(Test-Path -LiteralPath (Join-Path $stage 'Level.sav'))){{throw '候选世界缺少 Level.sav'}};Move-Item -LiteralPath $target -Destination $rollback;Move-Item -LiteralPath $stage -Destination $target"
                code, output, error = client.run_powershell(script)
            else:
                script = f"set -euo pipefail; rm -rf -- {shlex.quote(staging)} {shlex.quote(rollback)}; mkdir -p -- {shlex.quote(staging)}; tar -xzf {shlex.quote(remote_archive)} -C {shlex.quote(staging)}; test -s {shlex.quote(staging + '/Level.sav')}; mv -- {shlex.quote(session.target_world_path)} {shlex.quote(rollback)}; mv -- {shlex.quote(staging)} {shlex.quote(session.target_world_path)}"
                code, output, error = client.run(script)
            if code: raise RuntimeError(error.strip() or output.strip() or "远程候选世界替换失败")
            progress(82, "启动服务器", "远程世界已替换，正在等待健康检查"); start()
            if not RestoreTransaction._wait_for_health(health, on_wait=lambda elapsed, remaining: progress(min(96, 82 + int(elapsed / 4)), "等待远程服务器就绪", f"远程服务器启动检查中，已等待 {int(elapsed)} 秒，最多再等待 {int(remaining)} 秒")): raise RuntimeError("远程恢复后服务器健康检查失败")
            readback = check.parent / "remote-readback-Level.sav"; client.download_file(remote_level, readback)
            decoded = PlmCodecPlugin(storage_root).decode(readback); active = [self._player_guid(item) for item in decoded.get("players", [])]
            for mapping in session.mappings:
                if active.count(mapping.new_guid) != 1: raise RuntimeError(f"远程服务器复查未找到唯一目标玩家：{mapping.new_guid}")
            phase = "waiting_placeholders" if session.pending_player_guids else "complete"; migrated = tuple(replace(item, status="migrated") if item.status == "confirmed" else item for item in session.mappings); detail = f"世界恢复完成；已迁移 {len(migrated)} 个玩家；待迁移 {len(session.pending_player_guids)} 个"
            updated = replace(session, mappings=migrated, phase=phase, backup_path=restore_point, detail=detail); self.save_migration_session(storage_root, updated)
            RestoreTransaction._cleanup_remote(client, platform_name, rollback, remote_archive); progress(100, "恢复完成", detail)
            return RestoreResult(True, session.package_path, restore_point, ("world", "players"), False, detail)
        except Exception as exc:
            try:
                try: stop()
                except Exception: pass
                RestoreTransaction._rollback_remote(client, platform_name, session.target_world_path, rollback, staging, remote_archive); start()
            except Exception as rollback_exc: raise RuntimeError(f"远程恢复失败且回滚失败：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"远程恢复失败，已恢复原世界：{exc}") from exc

    def import_source(self, source: Path, instance: ServerInstance, destination: Path, backup_type: str = "world", on_progress: Callable[[int, str], None] | None = None) -> Path:
        source = Path(source).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"待导入文件或目录不存在：{source}")
        if on_progress:
            on_progress(10, "正在读取本地备份文件")
        if source.is_file() and source.suffix.lower() == ".pwcbackup":
            self.validate(source)
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / source.name
            if target.exists():
                target = destination / f"{source.stem}-{uuid.uuid4().hex[:8]}.pwcbackup"
            shutil.copy2(source, target)
            self.validate(target)
            if on_progress:
                on_progress(100, "备份包已校验并导入")
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
                if on_progress:
                    on_progress(30, "正在安全解包 ZIP 压缩包")
                self._extract_legacy_zip(source, temp)
            elif tarfile.is_tarfile(source):
                if on_progress:
                    on_progress(30, "正在安全解包 TAR 压缩包")
                self._extract_legacy_tar(source, temp)
            else:
                raise ValueError("仅支持 .pwcbackup、ZIP、TAR.GZ、Saved/SaveGames 目录或 Level.sav")
            if on_progress:
                on_progress(55, "正在定位 Saved/SaveGames 目录")
            saved = self._locate_saved_root(temp)
            players = list((saved / "SaveGames").rglob("Players/*.sav")) if (saved / "SaveGames").exists() else []
            if not players:
                incomplete = True
            if on_progress:
                on_progress(75, "正在生成统一 .pwcbackup 并校验 SHA-256")
            result = self.create(instance, saved, destination, backup_type, "从旧备份或外部存档导入", incomplete)
            if on_progress:
                on_progress(100, "压缩包已导入并完成校验")
            return result

    @staticmethod
    def _copy_directory_safe(source: Path, destination: Path) -> None:
        source = Path(source).resolve(); is_world = (source / "Level.sav").is_file()
        paths = [source]
        for path in source.rglob("*"):
            if is_world:
                relative = path.relative_to(source)
                if any(part.casefold() in WORLD_TRANSIENT_DIRS for part in relative.parts):
                    continue
            paths.append(path)
        for path in paths:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError(f"拒绝导入符号链接或重解析点: {path}")
        target = destination / source.name
        ignore = shutil.ignore_patterns(*WORLD_TRANSIENT_DIRS) if is_world else None
        shutil.copytree(source, target, symlinks=False, ignore=ignore)

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

    def import_source(self, source: Path, instance: ServerInstance, backup_type: str = "world", on_progress: Callable[[int, str], None] | None = None) -> Path:
        path = self.service.import_source(source, instance, self.root, backup_type, on_progress)
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
        package = _require_file(Path(package), "备份文件")
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

    def restore_savegames(
        self,
        package: Path,
        target_savegames: Path,
        stop: Callable[[], None],
        start: Callable[[], None],
        on_progress: Callable[[int, str, str], None] | None = None,
    ) -> RestoreResult:
        """直接校验并覆盖 SaveGames 文件，不创建恢复点或执行复杂合并。"""
        progress = lambda percent, stage, message: self._emit_progress(on_progress, percent, stage, message)
        package = _require_file(Path(package), "备份文件")
        progress(0, "读取备份", "正在读取备份文件")
        manifest = self.packages.validate(package)
        progress(10, "校验备份", "备份清单、CRC 和 SHA-256 校验通过")
        target = Path(target_savegames).expanduser().resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"目标服务器尚未创建 SaveGames 目录：{target}")
        with tempfile.TemporaryDirectory(prefix="palworld-savegames-restore-") as temp_name:
            extracted = Path(temp_name)
            self.packages.extract(package, extracted, ("world",))
            source_root = extracted / "payload" / "savegames"
            files = sorted(path for path in source_root.rglob("*") if path.is_file()) if source_root.is_dir() else []
            if not files:
                raise ValueError("备份中缺少可恢复的 SaveGames 文件")
            root = source_root.resolve()
            progress(20, "准备目标目录", f"已找到 {len(files)} 个待恢复文件")
            try:
                stop()
            except Exception as exc:
                raise RuntimeError(f"停止服务器失败，未修改存档：{exc}") from exc
            completed = 0
            try:
                progress(25, "停止服务器", "服务器已停止，开始逐文件替换")
                for source in files:
                    relative = source.resolve().relative_to(root)
                    destination = (target / relative).resolve()
                    if target != destination and target not in destination.parents:
                        raise ValueError(f"备份文件路径越界：{relative.as_posix()}")
                    if source.is_symlink():
                        raise ValueError(f"备份文件包含符号链接：{relative.as_posix()}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(destination.name + f".restore-{uuid.uuid4().hex}.tmp")
                    try:
                        shutil.copy2(source, temporary)
                        os.replace(temporary, destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                    completed += 1
                    percent = 30 + round(completed / len(files) * 60)
                    progress(percent, "替换 SaveGames", f"已处理 {completed}/{len(files)} 个文件：{relative.as_posix()}")
                progress(92, "启动服务器", "正在启动服务器")
                try:
                    start()
                except Exception as exc:
                    raise RuntimeError(f"存档文件已写入，但服务器启动失败：{exc}") from exc
                progress(100, "恢复完成", f"已覆盖 {completed}/{len(files)} 个 SaveGames 文件")
                return RestoreResult(True, str(package), "", ("world",), False, f"已覆盖 {completed}/{len(files)} 个 SaveGames 文件")
            except Exception as exc:
                try:
                    start()
                    restart = "服务器已重新启动"
                except Exception as start_exc:
                    restart = f"服务器重新启动失败：{start_exc}"
                raise RuntimeError(f"SaveGames 恢复失败，已处理 {completed}/{len(files)} 个文件；{restart}；错误：{exc}") from exc

    def restore_savegames_remote(
        self,
        package: Path,
        target_savegames: str,
        client,
        platform_name: str,
        stop: Callable[[], None],
        start: Callable[[], None],
        on_progress: Callable[[int, str, str], None] | None = None,
    ) -> RestoreResult:
        from .services import RemoteHostClient
        progress = lambda percent, stage, message: self._emit_progress(on_progress, percent, stage, message)
        package = _require_file(Path(package), "备份文件")
        self.packages.validate(package)
        with tempfile.TemporaryDirectory(prefix="palworld-savegames-remote-") as temp_name:
            extracted = Path(temp_name); self.packages.extract(package, extracted, ("world",))
            source_root = extracted / "payload" / "savegames"
            files = sorted(path for path in source_root.rglob("*") if path.is_file()) if source_root.is_dir() else []
            if not files: raise ValueError("备份中缺少可恢复的 SaveGames 文件")
            progress(20, "准备目标目录", f"已找到 {len(files)} 个待恢复文件")
            if platform_name == "windows":
                q = RemoteHostClient._ps_literal
                code, output, error = client.run_powershell(f"if(Test-Path -LiteralPath {q(target_savegames)} -PathType Container){{'ok'}}else{{'missing'}}")
            else:
                code, output, error = client.run(f"test -d {shlex.quote(target_savegames)} && printf ok || printf missing")
            if code or output.strip().splitlines()[-1:] != ["ok"]:
                raise FileNotFoundError(f"目标服务器尚未创建 SaveGames 目录：{target_savegames}")
            stop(); completed = 0; token = uuid.uuid4().hex; failed_relative = ""; remote_tmp = ""; failed_stage = ""
            try:
                progress(25, "停止服务器", "服务器已停止，开始逐文件上传替换")
                for source in files:
                    relative = source.relative_to(source_root).as_posix()
                    failed_relative = relative
                    failed_stage = "读取本地解包文件"
                    if not source.is_file(): raise FileNotFoundError(f"解包后的备份文件不存在：{source}")
                    remote = ntpath.join(target_savegames, *relative.split("/")) if platform_name == "windows" else f"{target_savegames.rstrip('/')}/{relative}"
                    remote_tmp = remote + f".restore-{token}-{completed}.tmp"
                    failed_stage = "创建远程目标父目录"
                    if platform_name == "windows":
                        q = RemoteHostClient._ps_literal; parent = ntpath.dirname(remote)
                        code, output, error = client.run_powershell(f"$ErrorActionPreference='Stop';New-Item -ItemType Directory -Force -Path {q(parent)}|Out-Null")
                    else:
                        parent = remote.rsplit("/", 1)[0]
                        code, output, error = client.run(f"mkdir -p -- {shlex.quote(parent)}")
                    if code: raise RuntimeError(error.strip() or output.strip() or f"无法创建远程目录：{parent}")
                    failed_stage = "上传远程临时文件"
                    progress(30 + round(completed / len(files) * 60), "上传 SaveGames 文件", f"正在上传 {completed + 1}/{len(files)}：{relative}")
                    client.upload_file(source, remote_tmp)
                    failed_stage = "原子替换目标文件"
                    if platform_name == "windows":
                        q = RemoteHostClient._ps_literal
                        script = f"$ErrorActionPreference='Stop';Move-Item -LiteralPath {q(remote_tmp)} -Destination {q(remote)} -Force"
                        code, output, error = client.run_powershell(script)
                    else:
                        code, output, error = client.run(f"mv -f -- {shlex.quote(remote_tmp)} {shlex.quote(remote)}")
                    if code: raise RuntimeError(error.strip() or output.strip() or f"写入失败：{relative}")
                    completed += 1; remote_tmp = ""; failed_relative = ""; failed_stage = ""
                    progress(30 + round(completed / len(files) * 60), "替换 SaveGames", f"已处理 {completed}/{len(files)} 个文件：{relative}")
                progress(92, "启动服务器", "正在启动服务器")
                start(); progress(100, "恢复完成", f"已覆盖 {completed}/{len(files)} 个 SaveGames 文件")
                return RestoreResult(True, str(package), "", ("world",), False, f"已覆盖 {completed}/{len(files)} 个 SaveGames 文件")
            except Exception as exc:
                if remote_tmp:
                    try:
                        if platform_name == "windows":
                            client.run_powershell(f"Remove-Item -LiteralPath {RemoteHostClient._ps_literal(remote_tmp)} -Force -ErrorAction SilentlyContinue")
                        else:
                            client.run(f"rm -f -- {shlex.quote(remote_tmp)}")
                    except Exception:
                        pass
                try: start(); restart = "服务器已重新启动"
                except Exception as start_exc: restart = f"服务器重新启动失败：{start_exc}"
                failure = f"；失败文件：{failed_relative}；失败阶段：{failed_stage}" if failed_relative else ""
                if isinstance(exc, OSError) and getattr(exc, "errno", None) == 2:
                    missing = getattr(exc, "filename", None) or remote_tmp or failed_relative
                    detail = f"远程路径不存在或 SFTP 无法访问：{missing}。程序已尝试创建目标父目录，请检查远程安装路径和 SSH 用户写入权限"
                else:
                    detail = str(exc)
                raise RuntimeError(f"远程 SaveGames 恢复失败，已处理 {completed}/{len(files)} 个文件{failure}；{restart}；错误：{detail}") from exc

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
        on_progress: Callable[[int, str, str], None] | None = None,
    ) -> RestoreResult:
        progress = lambda percent, stage, message: self._emit_progress(on_progress, percent, stage, message)
        package = _require_file(Path(package), "备份文件")
        progress(8, "恢复预检", "正在校验备份、安装目录和磁盘空间")
        plan = self.plan(package, instance, components)
        install_dir = Path(instance.install_dir).expanduser().resolve() if instance.install_dir else None
        if not install_dir or not install_dir.is_dir():
            raise FileNotFoundError(f"目标服务器安装目录不存在：{install_dir or instance.install_dir or '未设置'}")
        saved = install_dir / "Pal" / "Saved"
        if not saved.is_dir():
            raise FileNotFoundError(f"目标服务器尚未安装或未检测到 Saved 目录：{saved}")
        current_size = sum(path.stat().st_size for path in saved.rglob("*") if path.is_file())
        required_space = current_size * 2 + max(1, plan.estimated_bytes // 3)
        if shutil.disk_usage(saved.parent).free < required_space:
            raise RuntimeError("恢复空间不足，需要当前存档、暂存副本和回滚副本的总空间")
        progress(15, "停止服务器", "正在停止服务器以保证存档一致性")
        stop()
        try:
            progress(22, "创建恢复点", "正在创建恢复前的受保护恢复点")
            try:
                restore_point = self.packages.create(instance, saved, repository.root, "restore-point", "恢复操作自动创建的恢复点")
            except Exception as exc:
                raise _stage_error("创建恢复前恢复点", exc) from exc
            repository.set_metadata(restore_point, protected=True, verified_at=_utc_now())
        except Exception:
            start()
            raise
        rollback = saved.with_name(f"{saved.name}.rollback-{uuid.uuid4().hex}")
        staging = saved.with_name(f"{saved.name}.restore-{uuid.uuid4().hex}")
        try:
            try:
                progress(35, "准备暂存目录", "正在复制当前存档并创建回滚副本")
                staging.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(saved, staging)
            except Exception as exc:
                raise _stage_error("创建恢复暂存目录", exc) from exc
            try:
                progress(52, "写入恢复内容", "正在解包并写入所选恢复组件")
                self._apply_to_staging(package, staging, plan.components, instance, admin_password, server_password, player_uid)
                self._validate_staging(staging, plan.components)
            except Exception as exc:
                raise _stage_error("准备恢复内容", exc) from exc
            saved.rename(rollback)
            staging.rename(saved)
            progress(72, "替换存档", "暂存存档已通过校验，正在切换到目标目录")
            progress(80, "启动服务器", "正在启动服务器并等待服务就绪")
            start()
            progress(90, "健康检查", "正在等待服务器进程和游戏端口恢复")
            if not health():
                raise RuntimeError("恢复后服务器健康检查失败")
            progress(97, "清理恢复现场", "正在清理回滚目录并保留恢复点")
            shutil.rmtree(rollback, ignore_errors=True)
            progress(100, "恢复完成", "存档已恢复，服务器健康检查通过")
            return RestoreResult(True, str(package), str(restore_point), plan.components, False, "恢复并完成健康检查")
        except Exception as exc:
            progress(84, "自动回滚", "恢复失败，正在还原原存档并重启服务器")
            if saved.exists() and rollback.exists():
                shutil.rmtree(saved, ignore_errors=True)
            if rollback.exists():
                shutil.copytree(rollback, saved, dirs_exist_ok=True)
                shutil.rmtree(rollback, ignore_errors=True)
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
        on_progress: Callable[[int, str, str], None] | None = None,
    ) -> RestoreResult:
        from .services import BackupService, RemoteHostClient, WindowsRemotePath

        progress = lambda percent, stage, message: self._emit_progress(on_progress, percent, stage, message)
        progress(8, "恢复预检", "正在校验远程路径、备份包和磁盘空间")
        plan = self.plan(package, instance, components)
        platform_name = str(instance.remote_profile.get("platform") or "linux").lower()
        install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir)
        if platform_name == "windows":
            install_dir = WindowsRemotePath.normalize(install_dir)
            saved_path = ntpath.join(install_dir, "Pal", "Saved")
            tool_dir = str(instance.remote_profile.get("restore_temp_dir") or ntpath.join(install_dir, "_tools"))
        else:
            if not install_dir.startswith("/"):
                raise ValueError("Linux 远程安装目录必须是绝对路径")
            saved_path = f"{install_dir}/Pal/Saved"
            # SteamCMD may live in ~/.local/share/SteamCMD; restore archives only
            # need a writable temporary directory and must not assume install/_tools.
            tool_dir = str(instance.remote_profile.get("restore_temp_dir") or "/tmp")
        self._check_remote_paths(client, platform_name, saved_path)
        self._ensure_remote_temp_dir(client, platform_name, tool_dir)
        self._check_remote_space(client, platform_name, saved_path, max(1, plan.estimated_bytes // 3))
        progress(15, "停止服务器", "正在停止远程服务器以保证存档一致性")
        stop()
        try:
            progress(22, "创建恢复点", "正在下载当前服务器完整恢复点")
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
            try:
                progress(38, "准备恢复内容", "正在解包、合并并校验恢复组件")
                self._build_remote_payload(package, payload, plan.components, instance, client, admin_password, server_password, player_uid)
                self._validate_staging(payload, plan.components)
            except Exception as exc:
                raise _stage_error("准备远程恢复内容", exc) from exc
            local_archive = temp / f"restore{archive_suffix}"
            if platform_name == "windows":
                with zipfile.ZipFile(local_archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for path in sorted(payload.rglob("*")):
                        if path.is_file(): archive.write(path, path.relative_to(payload).as_posix())
            else:
                with tarfile.open(local_archive, "w:gz") as archive:
                    for path in sorted(payload.iterdir()): archive.add(path, arcname=path.name, recursive=True)
            if not local_archive.is_file():
                raise RuntimeError(f"远程恢复归档生成失败：{local_archive}")
            local_hash = _sha256(local_archive)
            try:
                progress(58, "上传恢复包", "正在上传恢复归档并校验 SHA-256")
                client.upload_file(local_archive, remote_archive)
            except Exception as exc:
                raise _stage_error("上传远程恢复压缩包", exc) from exc
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
                progress(76, "替换远程存档", "远程暂存目录已校验，正在原子切换")
                progress(82, "启动服务器", "正在启动远程服务器并等待服务就绪")
                start()
                progress(90, "健康检查", "正在等待远程进程和游戏端口恢复")
                if not self._wait_for_health(health, on_wait=lambda elapsed, remaining: progress(min(96, 90 + int(elapsed / 8)), "等待服务器就绪", f"远程服务器启动检查中，已等待 {int(elapsed)} 秒，最多再等待 {int(remaining)} 秒")):
                    raise RuntimeError("恢复后服务器健康检查失败")
                progress(97, "清理恢复现场", "正在清理远程临时归档和回滚目录")
                self._cleanup_remote(client, platform_name, rollback, remote_archive)
                progress(100, "恢复完成", "远程存档已恢复，健康检查通过")
                return RestoreResult(True, str(package), str(restore_point), plan.components, False, "远程恢复并完成健康检查")
            except Exception as exc:
                progress(84, "自动回滚", "远程恢复失败，正在还原原存档并重启服务器")
                try:
                    try: stop()
                    except Exception: pass
                    self._rollback_remote(client, platform_name, saved_path, rollback, staging, remote_archive)
                    start()
                    if not self._wait_for_health(health, on_wait=lambda elapsed, remaining: progress(88, "验证回滚", f"正在确认回滚后的服务器状态，已等待 {int(elapsed)} 秒")): raise RuntimeError("回滚后健康检查失败")
                except Exception as rollback_exc:
                    raise RuntimeError(f"远程恢复失败且自动回滚失败：{rollback_exc}；原错误：{exc}") from exc
                raise RuntimeError(f"远程恢复失败，已自动回滚：{exc}") from exc

    @staticmethod
    def _emit_progress(callback, percent: int, stage: str, message: str) -> None:
        if callback is None:
            return
        try:
            callback(max(0, min(100, int(percent))), stage, message)
        except Exception:
            # 进度显示不能影响恢复事务本身。
            pass

    @staticmethod
    def _wait_for_health(health: Callable[[], bool], timeout_seconds: float = 45.0, interval_seconds: float = 2.0, on_wait: Callable[[float, float], None] | None = None) -> bool:
        started = time.monotonic(); deadline = started + timeout_seconds
        while True:
            try:
                if health():
                    return True
            except Exception:
                pass
            now = time.monotonic()
            if on_wait:
                try: on_wait(now - started, max(0.0, deadline - now))
                except Exception: pass
            if now >= deadline:
                return False
            time.sleep(interval_seconds)

    @staticmethod
    def _check_remote_paths(client, platform_name: str, saved_path: str) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            q = RemoteHostClient._ps_literal
            script = f"$s=Test-Path -LiteralPath {q(saved_path)}; @{{saved=$s}}|ConvertTo-Json -Compress"
            code, output, error = client.run_powershell(script)
        else:
            script = f"printf '{{\"saved\":%s}}' \"$(test -d {shlex.quote(saved_path)} && echo true || echo false)\""
            code, output, error = client.run(script)
        if code:
            raise RuntimeError(error.strip() or output.strip() or "无法检查远程恢复目录")
        try:
            payload = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError("远程恢复目录探针返回无效结果") from exc
        if not payload.get("saved"):
            raise FileNotFoundError(f"远程目标 Saved 目录不存在：{saved_path}")

    @staticmethod
    def _ensure_remote_temp_dir(client, platform_name: str, temp_dir: str) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            code, output, error = client.run_powershell(f"New-Item -ItemType Directory -Force -Path {RemoteHostClient._ps_literal(temp_dir)} | Out-Null")
        else:
            code, output, error = client.run(f"mkdir -p -- {shlex.quote(temp_dir)}")
        if code:
            raise RuntimeError(error.strip() or output.strip() or f"无法准备远程临时目录：{temp_dir}")

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
