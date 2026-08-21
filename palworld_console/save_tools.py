from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .backup_packages import BackupPackageService
from .gamepass import GamePassWorld, default_wgs_root, discover_worlds, extract_world, find_user_containers
from .models import ConversionPackage, LocalSaveSource, SaveDiagnosticFinding, SaveDiagnosticReport, SaveToolOperation
from .save_codec import PlmCodecPlugin


CONVERSION_FORMAT = "palworld-console-conversion-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SaveToolsService:
    """Application-facing save tool workflow.

    The actual save codec remains behind the existing PlM/helper boundary. This
    service owns source discovery, conversion packages and operation metadata.
    """

    def __init__(self, storage_root: Path | None = None, codec=None):
        self.storage_root = Path(storage_root or Path.home() / ".palworld-console")
        self.package_root = self.storage_root / "conversion-packages"
        self.codec = codec or PlmCodecPlugin(self.storage_root)
        self.exclusions_path = self.storage_root / "save-tool-exclusions.json"

    def load_exclusions(self) -> dict[str, list[str]]:
        """Load persistent cleanup exclusions used by administrator workflows."""
        try:
            payload = json.loads(self.exclusions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        return {
            key: sorted({str(value).strip() for value in payload.get(key, []) if str(value).strip()})
            for key in ("players", "guilds", "bases")
        }

    def save_exclusions(self, exclusions: dict[str, list[str]]) -> dict[str, list[str]]:
        """Persist normalized player/guild/base IDs atomically."""
        normalized = {
            key: sorted({str(value).strip() for value in exclusions.get(key, []) if str(value).strip()})
            for key in ("players", "guilds", "bases")
        }
        self.exclusions_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.exclusions_path.with_name(self.exclusions_path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.exclusions_path)
        finally:
            temp.unlink(missing_ok=True)
        return normalized

    def export_base_blueprint(self, source: Path, base_id: str, output: Path) -> dict:
        """Export a portable, redacted base blueprint from a decoded world."""
        _level, payload = self.load_world_snapshot(source)
        wanted = str(base_id).strip()
        base = next((item for item in payload.get("bases", []) if str(item.get("base_id") or "") == wanted), None)
        if base is None:
            raise ValueError(f"找不到基地：{wanted}")
        blueprint = {
            "format": "palworld-console-base-blueprint-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "base": json.loads(json.dumps(base, ensure_ascii=False)),
        }
        output = Path(output).expanduser().resolve(); output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(output.name + f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        return {"format": blueprint["format"], "base_id": wanted, "output": str(output), "sha256": _sha256(output)}

    @staticmethod
    def inspect_base_blueprint(path: Path) -> dict:
        path = Path(path).expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"基地蓝图不是有效 JSON：{exc}") from exc
        if payload.get("format") != "palworld-console-base-blueprint-v1":
            raise ValueError("不支持的基地蓝图格式")
        base = payload.get("base")
        if not isinstance(base, dict) or not str(base.get("base_id") or ""):
            raise ValueError("基地蓝图缺少稳定 base_id")
        return {"base_id": str(base["base_id"]), "name": str(base.get("name") or ""), "guild_id": str(base.get("guild_id") or ""), "position": dict(base.get("position") or {}), "worker_count": len(base.get("worker_pal_ids") or []), "container_count": len(base.get("container_ids") or [])}

    def build_cleanup_plan(self, source: Path, exclusions: dict[str, list[str]] | None = None, inactive_days: int = 30) -> dict:
        """Produce a deterministic, reviewable cleanup plan without mutating a save."""
        _level, payload = self.load_world_snapshot(source)
        excluded = exclusions if exclusions is not None else self.load_exclusions()
        excluded = {key: {str(value) for value in excluded.get(key, [])} for key in ("players", "guilds", "bases")}
        cutoff = datetime.now(timezone.utc).timestamp() - max(0, int(inactive_days)) * 86400
        def old(item: dict) -> bool:
            for key in ("last_seen", "last_login", "last_online"):
                value = item.get(key)
                if isinstance(value, (int, float)):
                    return float(value) < cutoff
            return False
        players = list(payload.get("players") or []); guilds = list(payload.get("guilds") or []); bases = list(payload.get("bases") or [])
        player_ids = [self._player_guid(item) for item in players]
        duplicate_players = sorted({uid for uid in player_ids if uid and player_ids.count(uid) > 1})
        empty_guilds = sorted(str(item.get("guild_id") or "") for item in guilds if item.get("guild_id") and not (item.get("players") or item.get("member_uids")))
        orphan_bases = sorted(str(item.get("base_id") or "") for item in bases if item.get("base_id") and not str(item.get("guild_id") or ""))
        inactive_players = sorted(self._player_guid(item) for item in players if self._player_guid(item) and old(item))
        return {"source": str(Path(source).expanduser().resolve()), "inactive_days": int(inactive_days), "excluded": {key: sorted(values) for key, values in excluded.items()}, "candidates": {"duplicate_players": [uid for uid in duplicate_players if uid not in excluded["players"]], "inactive_players": [uid for uid in inactive_players if uid not in excluded["players"]], "empty_guilds": [gid for gid in empty_guilds if gid not in excluded["guilds"]], "orphan_bases": [bid for bid in orphan_bases if bid not in excluded["bases"]]}, "mutations": 0, "read_only": True}

    def detect_sources(self) -> tuple[LocalSaveSource, ...]:
        sources = list(BackupPackageService().detect_local_save_sources())
        for world in self.detect_gamepass_worlds():
            sources.append(LocalSaveSource(
                source_path=world.user_container,
                source_kind="gamepass",
                savegames_root=world.user_container,
                world_relative_path=world.save_id,
                world_id=world.save_id,
                file_count=len(world.files),
                total_bytes=world.total_bytes,
                has_level="Level.sav" in world.files,
                has_players=world.player_count > 0,
                save_format="Xbox/Game Pass WGS",
                modified_at=world.modified_at,
                warnings=world.warnings,
            ))
        return tuple(sources)

    def detect_gamepass_worlds(self, wgs_path: Path | None = None) -> tuple[GamePassWorld, ...]:
        return discover_worlds(wgs_path or default_wgs_root())

    def gamepass_user_containers(self, wgs_path: Path | None = None) -> tuple[Path, ...]:
        return find_user_containers(wgs_path or default_wgs_root())

    def convert_gamepass_to_steam(
        self,
        user_container: Path,
        save_id: str,
        destination: Path,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        report = extract_world(user_container, save_id, destination, on_progress)
        inspection = BackupPackageService().inspect_save_source(Path(report["destination"]))
        if not inspection.has_level:
            raise RuntimeError("Game Pass 转换后的 Steam 世界未通过 Level.sav 复检")
        return {**report, "inspection": asdict(inspection)}

    def inspect(self, source: Path) -> LocalSaveSource:
        source = Path(source).expanduser().resolve()
        if (source / "containers.index").is_file():
            worlds = self.detect_gamepass_worlds(source)
            if not worlds:
                raise ValueError("Game Pass 容器中未检测到可读取的 Palworld 世界")
            world = worlds[0]
            return LocalSaveSource(str(source), "gamepass", str(source), world.save_id, world.save_id, len(world.files), world.total_bytes, True, world.player_count > 0, "Xbox/Game Pass WGS", world.modified_at, world.warnings)
        return BackupPackageService().inspect_save_source(Path(source))

    def plan(self, operation: str, source: Path, target: Path | None = None) -> SaveToolOperation:
        inspection = self.inspect(source)
        return SaveToolOperation(
            operation_id=uuid.uuid4().hex,
            operation=operation,
            source_path=str(Path(source).expanduser().resolve()),
            target_path=str(Path(target).expanduser().resolve()) if target else "",
            source_kind=inspection.source_kind,
            source_format=inspection.save_format,
            target_kind="server" if target else "package",
        )

    def create_conversion_package(
        self,
        source: Path,
        operation: str = "convert",
        output: Path | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        world_id: str = "",
    ) -> ConversionPackage:
        source = Path(source).expanduser().resolve()
        inspection = self.inspect(source)
        if inspection.source_kind == "gamepass" and world_id and world_id != inspection.world_id:
            world = next((item for item in self.detect_gamepass_worlds(source) if item.save_id == world_id), None)
            if world is None:
                raise ValueError(f"Game Pass 容器中不存在世界：{world_id}")
            inspection = LocalSaveSource(str(source), "gamepass", str(source), world.save_id, world.save_id, len(world.files), world.total_bytes, True, world.player_count > 0, "Xbox/Game Pass WGS", world.modified_at, world.warnings)
        output = Path(output).expanduser().resolve() if output else self.package_root / f"{inspection.world_id}-{uuid.uuid4().hex[:8]}.pwc-conversion"
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="palworld-conversion-") as temp_name:
            temp = Path(temp_name)
            input_source = source
            if inspection.source_kind == "gamepass":
                input_source = temp / "gamepass-world"
                extract_world(source, inspection.world_id, input_source, lambda p, m: on_progress and on_progress(round(p * .4), m))
            normalized_root = temp / "normalized"
            BackupPackageService().normalize_local_save(input_source, normalized_root, lambda p, m: on_progress and on_progress(40 + round(p * .4), m))
            normalized = normalized_root / "SaveGames" / "imported-world"
            files = [path for path in normalized.rglob("*") if path.is_file()]
            total_bytes = sum(path.stat().st_size for path in files)
            manifest = {
                "format": CONVERSION_FORMAT,
                "operation_id": uuid.uuid4().hex,
                "operation": operation,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": asdict(inspection),
                "world_id": inspection.world_id,
                "entries": [
                    {"path": path.relative_to(normalized).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)}
                    for path in files
                ],
            }
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if output.exists():
                output.unlink()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(manifest_path, "manifest.json")
                for index, path in enumerate(files, 1):
                    archive.write(path, f"Saved/{path.relative_to(normalized).as_posix()}")
                    if on_progress:
                        on_progress(80 + round(index / max(1, len(files)) * 15), f"正在写入转换包 {index}/{len(files)}")
        return ConversionPackage(
            path=str(output), manifest_path="manifest.json", operation_id=manifest["operation_id"],
            source_path=str(source), source_kind=inspection.source_kind, world_id=inspection.world_id,
            file_count=len(files), total_bytes=total_bytes,
            sha256=_sha256(output), created_at=manifest["created_at"], warnings=inspection.warnings,
        )

    def verify_conversion_package(self, package: Path) -> dict:
        package = Path(package).expanduser().resolve()
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names:
                raise ValueError("转换包缺少 manifest.json")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != CONVERSION_FORMAT:
                raise ValueError("不是受支持的 Palworld 转换包")
            expected = {"manifest.json"}
            checked = 0
            for entry in manifest.get("entries", []):
                relative = self._safe_relative(str(entry.get("path") or ""))
                name = f"Saved/{relative.as_posix()}"
                expected.add(name)
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError(f"转换包校验失败：{entry['path']}")
                checked += 1
            unexpected = names - expected
            if unexpected:
                raise ValueError(f"转换包包含未登记文件：{sorted(unexpected)[0]}")
        return {"valid": True, "world_id": manifest.get("world_id", ""), "entries": checked, "sha256": _sha256(package)}

    @staticmethod
    def _safe_relative(value: str) -> Path:
        normalized = value.replace("\\", "/")
        relative = Path(normalized)
        if not normalized or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"转换包包含不安全路径：{value}")
        return relative

    def extract_conversion_package(self, package: Path, destination: Path | None = None) -> Path:
        package = Path(package).expanduser().resolve()
        report = self.verify_conversion_package(package)
        destination = Path(destination).expanduser().resolve() if destination else self.storage_root / "conversion-imports" / report["sha256"][:16]
        if destination.exists():
            shutil.rmtree(destination)
        world = destination / "SaveGames" / "imported-world"
        world.mkdir(parents=True)
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            for entry in manifest["entries"]:
                relative = self._safe_relative(entry["path"])
                target = world / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(f"Saved/{relative.as_posix()}"))
        return destination

    def materialize_source(self, source: Path, world_id: str = "") -> Path:
        source = Path(source).expanduser().resolve()
        if source.suffix.lower() == ".pwc-conversion":
            return self.extract_conversion_package(source)
        if source.is_dir() and (source / "containers.index").is_file():
            worlds = self.detect_gamepass_worlds(source)
            selected = next((item for item in worlds if item.save_id == world_id), worlds[0] if worlds else None)
            if selected is None:
                raise ValueError("Game Pass 容器中未检测到可读取的 Palworld 世界")
            token = hashlib.sha256(f"{source}|{selected.save_id}".encode("utf-8")).hexdigest()[:16]
            destination = self.storage_root / "gamepass-imports" / token / selected.save_id
            extract_world(source, selected.save_id, destination)
            return destination
        return source

    def convert_save_file(self, source: Path, output: Path) -> dict:
        source = Path(source).expanduser().resolve()
        output = Path(output).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"转换来源不存在：{source}")
        if source.suffix.lower() not in {".sav", ".json"}:
            raise ValueError("仅支持 .sav 与 .json 文件")
        expected_suffix = ".json" if source.suffix.lower() == ".sav" else ".sav"
        if output.suffix.lower() != expected_suffix:
            raise ValueError(f"输出文件必须使用 {expected_suffix} 扩展名")
        output.parent.mkdir(parents=True, exist_ok=True)
        backup = ""
        with tempfile.TemporaryDirectory(prefix="palworld-file-convert-") as temp_name:
            candidate = Path(temp_name) / ("candidate" + expected_suffix)
            report = self.codec.convert_file(source, candidate)
            if not candidate.is_file() or not candidate.stat().st_size:
                raise RuntimeError("转换 helper 没有生成有效候选文件")
            if expected_suffix == ".json":
                json.loads(candidate.read_text(encoding="utf-8"))
            else:
                verification = Path(temp_name) / "roundtrip.json"
                self.codec.convert_file(candidate, verification)
                json.loads(verification.read_text(encoding="utf-8"))
            if output.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_path = output.with_name(output.name + f".{stamp}.bak")
                shutil.copy2(output, backup_path); backup = str(backup_path)
            os.replace(candidate, output)
        return {**report, "output": str(output), "backup": backup, "sha256": _sha256(output)}

    def steam_id_to_uid(self, value: str) -> dict[str, str]:
        raw = str(value).strip()
        if not raw:
            raise ValueError("请输入 SteamID64 或 Steam 个人资料链接")
        return self.codec.steam_id_to_uid(raw)

    def restore_map_file(self, source: Path, output: Path | None = None) -> dict:
        source = Path(source).expanduser().resolve(); output = Path(output or source).expanduser().resolve()
        if not source.is_file() or source.name.casefold() != "localdata.sav":
            raise ValueError("地图恢复仅支持 LocalData.sav")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="palworld-map-restore-") as temp_name:
            candidate = Path(temp_name) / "LocalData.sav"
            report = self.codec.restore_map(source, candidate)
            if not candidate.is_file() or not candidate.stat().st_size:
                raise RuntimeError("地图恢复 helper 没有生成有效候选文件")
            verification = Path(temp_name) / "LocalData.json"
            self.codec.convert_file(candidate, verification)
            json.loads(verification.read_text(encoding="utf-8"))
            backup = ""
            if output.exists():
                backup_path = output.with_name(output.name + f".{datetime.now():%Y%m%d-%H%M%S}.bak")
                shutil.copy2(output, backup_path); backup = str(backup_path)
            os.replace(candidate, output)
        return {**report, "output": str(output), "backup": backup, "sha256": _sha256(output)}

    def resolve_level_path(self, source: Path) -> Path:
        source = self.materialize_source(Path(source))
        if source.is_file() and source.name.casefold() == "level.sav":
            return source
        if not source.exists():
            raise FileNotFoundError(f"存档来源不存在：{source}")
        if source.is_file():
            workspace = self.storage_root / "save-tool-workspaces" / (_sha256(source)[:16])
            if workspace.exists(): shutil.rmtree(workspace)
            BackupPackageService().normalize_local_save(source, workspace)
            source = workspace
        candidates = list(source.rglob("Level.sav"))
        if not candidates:
            raise FileNotFoundError("存档来源中未找到 Level.sav")
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def load_world_snapshot(self, source: Path) -> tuple[Path, dict]:
        level = self.resolve_level_path(source)
        return level, self.codec.decode(level)

    @staticmethod
    def _player_guid(player: dict) -> str:
        return str(player.get("player_guid") or player.get("player_uid") or "").replace("-", "").upper()

    def rebind_world_identity(self, source: Path, old_guid: str, new_guid: str, destination: Path) -> dict:
        level, payload = self.load_world_snapshot(source)
        world = level.parent
        if not (world / "Players").is_dir():
            raise ValueError("角色重绑定需要包含 Players 的完整世界目录")
        old_key = str(old_guid).replace("-", "").upper(); new_key = str(new_guid).replace("-", "").upper()
        if old_key == new_key:
            raise ValueError("原角色与占位角色不能相同")
        players = {self._player_guid(player): player for player in payload.get("players", []) if self._player_guid(player)}
        old = players.get(old_key); new = players.get(new_key)
        if old is None or new is None:
            raise ValueError("原角色或占位角色不在当前世界的结构化玩家列表中")
        old_instance = str(old.get("instance_id") or ""); new_instance = str(new.get("instance_id") or "")
        if not old_instance or not new_instance:
            raise ValueError("角色缺少稳定 InstanceId，禁止执行身份重绑定")
        destination = Path(destination).expanduser().resolve()
        if destination == world.resolve():
            raise ValueError("角色重绑定必须输出到新目录，不能原地覆盖来源世界")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + f".identity-{uuid.uuid4().hex}.tmp")
        backup = destination.with_name(destination.name + f".{datetime.now():%Y%m%d-%H%M%S}.bak")
        mapping = {"old_guid": old_key, "new_guid": new_key, "old_instance_id": old_instance, "new_instance_id": new_instance}
        try:
            report = self.codec.migrate_identities(world, [mapping], staging)
            decoded = self.codec.decode(staging / "Level.sav")
            active = [self._player_guid(player) for player in decoded.get("players", [])]
            if active.count(new_key) != 1 or old_key in active:
                raise RuntimeError("角色重绑定候选二次解析未通过唯一身份验证")
            if destination.exists():
                destination.rename(backup)
            try:
                staging.rename(destination)
            except Exception:
                if backup.exists() and not destination.exists(): backup.rename(destination)
                raise
        finally:
            if staging.exists(): shutil.rmtree(staging)
        return {**report, "source": str(world), "destination": str(destination), "backup": str(backup) if backup.exists() else "", "old_guid": old_key, "new_guid": new_key}

    def expand_palbox_world(self, source: Path, player_guid: str, slots: int, destination: Path) -> dict:
        level, payload = self.load_world_snapshot(source); world = level.parent
        player_key = str(player_guid).replace("-", "").upper()
        players = {self._player_guid(player): player for player in payload.get("players", []) if self._player_guid(player)}
        if player_key not in players:
            raise ValueError("目标玩家不在当前世界中")
        destination = Path(destination).expanduser().resolve()
        if destination == world.resolve(): raise ValueError("Palbox 扩容必须输出到新目录")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + f".palbox-{uuid.uuid4().hex}.tmp")
        backup = destination.with_name(destination.name + f".{datetime.now():%Y%m%d-%H%M%S}.bak")
        try:
            report = self.codec.expand_palbox(world, player_key, int(slots), staging)
            decoded = self.codec.decode(staging / "Level.sav")
            if [self._player_guid(player) for player in decoded.get("players", [])].count(player_key) != 1:
                raise RuntimeError("Palbox 扩容候选二次解析未找到唯一目标玩家")
            if destination.exists(): destination.rename(backup)
            try: staging.rename(destination)
            except Exception:
                if backup.exists() and not destination.exists(): backup.rename(destination)
                raise
        finally:
            if staging.exists(): shutil.rmtree(staging)
        return {**report, "destination": str(destination), "backup": str(backup) if backup.exists() else ""}

    def diagnose(self, source: Path) -> SaveDiagnosticReport:
        level, payload = self.load_world_snapshot(source)
        players = list(payload.get("players") or [])
        guilds = list(payload.get("guilds") or [])
        bases = list(payload.get("bases") or [])
        pals = [pal for player in players for pal in (player.get("pals") or [])]
        findings: list[SaveDiagnosticFinding] = []

        def duplicates(items, key):
            counts = {}
            for item in items:
                value = str(item.get(key) or "")
                if value: counts[value] = counts.get(value, 0) + 1
            return {value for value, count in counts.items() if count > 1}

        for uid in sorted(duplicates(players, "player_uid")):
            findings.append(SaveDiagnosticFinding("高", "重复身份", "player", uid, "玩家 UID 在 Level.sav 中重复出现"))
        for identity in sorted(duplicates(pals, "individual_id")):
            findings.append(SaveDiagnosticFinding("高", "重复身份", "pal", identity, "帕鲁 InstanceId 重复，禁止直接写回"))
        for guild in guilds:
            guild_id = str(guild.get("guild_id") or "")
            members = guild.get("players") or []
            if not guild_id:
                findings.append(SaveDiagnosticFinding("高", "关系缺失", "guild", "-", "公会缺少稳定 ID"))
            elif not members:
                findings.append(SaveDiagnosticFinding("中", "空公会", "guild", guild_id, "公会没有成员，可在完整清理事务中删除", True))
        guild_ids = {str(guild.get("guild_id") or "") for guild in guilds}
        for base in bases:
            base_id = str(base.get("base_id") or "-")
            guild_id = str(base.get("guild_id") or "")
            if not guild_id or guild_id not in guild_ids:
                findings.append(SaveDiagnosticFinding("高", "关系缺失", "base", base_id, "基地没有有效公会归属"))
            if base.get("data_status") != "complete":
                findings.append(SaveDiagnosticFinding("中", "数据不完整", "base", base_id, str(base.get("read_only_reason") or "基地关系不完整")))
        for pal in pals:
            identity = str(pal.get("individual_id") or "-")
            for field, maximum in (("level", 80), ("melee", 100), ("ranged", 100), ("defense", 100)):
                value = pal.get(field)
                if isinstance(value, (int, float)) and (value < 0 or value > maximum):
                    findings.append(SaveDiagnosticFinding("中", "非法数值", "pal", identity, f"{field}={value} 超出 0-{maximum}", True))
        return SaveDiagnosticReport(str(Path(source).resolve()), str(level), len(players), len(pals), len(guilds), len(bases), tuple(findings))
