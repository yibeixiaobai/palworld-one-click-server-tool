from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable


PALWORLD_PACKAGE = "PocketpairInc.Palworld_ad4psfrxyesvt"
WGS_USER_PATTERN = re.compile(r"^[0-9A-Fa-f]{16}_[0-9A-Fa-f]{32}$")
CONTAINER_INDEX_VERSION = 0xE
CONTAINER_FILE_VERSION = 4


class GamePassSaveError(ValueError):
    pass


@dataclass(frozen=True)
class GamePassContainer:
    name: str
    sequence: int
    directory_id: str
    modified_filetime: int
    size: int


@dataclass(frozen=True)
class GamePassWorld:
    user_container: str
    save_id: str
    files: tuple[str, ...]
    player_count: int
    total_bytes: int
    modified_at: str
    warnings: tuple[str, ...] = ()


def default_wgs_root(local_app_data: str | Path | None = None) -> Path:
    root = Path(local_app_data or os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return root / "Packages" / PALWORLD_PACKAGE / "SystemAppData" / "wgs"


def _read_exact(stream: BinaryIO, length: int, label: str) -> bytes:
    value = stream.read(length)
    if len(value) != length:
        raise GamePassSaveError(f"Game Pass 容器在读取 {label} 时意外结束")
    return value


def _read_u8(stream: BinaryIO, label: str) -> int:
    return int.from_bytes(_read_exact(stream, 1, label), "little")


def _read_u32(stream: BinaryIO, label: str) -> int:
    return int.from_bytes(_read_exact(stream, 4, label), "little")


def _read_u64(stream: BinaryIO, label: str) -> int:
    return int.from_bytes(_read_exact(stream, 8, label), "little")


def _read_utf16(stream: BinaryIO, label: str, fixed_chars: int | None = None) -> str:
    length = fixed_chars if fixed_chars is not None else _read_u32(stream, f"{label}长度")
    if length < 0 or length > 32768:
        raise GamePassSaveError(f"Game Pass 容器中的 {label} 长度无效：{length}")
    raw = _read_exact(stream, length * 2, label)
    try:
        return raw.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError as exc:
        raise GamePassSaveError(f"Game Pass 容器中的 {label} 不是有效 UTF-16") from exc


def find_user_containers(wgs_path: Path | None = None) -> tuple[Path, ...]:
    root = Path(wgs_path or default_wgs_root()).expanduser().resolve()
    if (root / "containers.index").is_file():
        return (root,)
    if not root.is_dir():
        return ()
    return tuple(sorted(path for path in root.iterdir() if path.is_dir() and WGS_USER_PATTERN.fullmatch(path.name)))


def read_container_index(user_container: Path) -> tuple[GamePassContainer, ...]:
    path = Path(user_container).expanduser().resolve() / "containers.index"
    if not path.is_file():
        raise FileNotFoundError(f"未找到 Game Pass containers.index：{path}")
    with path.open("rb") as stream:
        version = _read_u32(stream, "索引版本")
        if version != CONTAINER_INDEX_VERSION:
            raise GamePassSaveError(f"不支持的 Game Pass 容器索引版本：{version}（需要 {CONTAINER_INDEX_VERSION}）")
        count = _read_u32(stream, "容器数量")
        if count > 100000:
            raise GamePassSaveError(f"Game Pass 容器数量超过安全上限：{count}")
        _read_u32(stream, "索引标志")
        _read_utf16(stream, "包名称")
        _read_u64(stream, "索引时间")
        _read_u32(stream, "索引标志")
        _read_utf16(stream, "索引 ID")
        _read_u64(stream, "索引保留字段")
        entries: list[GamePassContainer] = []
        for number in range(count):
            name = _read_utf16(stream, f"容器 {number + 1} 名称")
            repeated = _read_utf16(stream, f"容器 {number + 1} 重复名称")
            if name != repeated:
                raise GamePassSaveError(f"Game Pass 容器名称不一致：{name} / {repeated}")
            cloud_id = _read_utf16(stream, f"容器 {number + 1} 云 ID")
            sequence = _read_u8(stream, f"容器 {number + 1} 序号")
            flag = _read_u32(stream, f"容器 {number + 1} 标志")
            if (not cloud_id and not flag & 4) or (cloud_id and flag & 4):
                raise GamePassSaveError(f"Game Pass 容器 {name} 的云状态标志不一致")
            raw_uuid = _read_exact(stream, 16, f"容器 {number + 1} UUID")
            directory_id = uuid.UUID(bytes=raw_uuid).bytes_le.hex().upper()
            modified = _read_u64(stream, f"容器 {number + 1} 时间")
            _read_u64(stream, f"容器 {number + 1} 保留字段")
            size = _read_u64(stream, f"容器 {number + 1} 大小")
            entries.append(GamePassContainer(name, sequence, directory_id, modified, size))
    return tuple(entries)


def _logical_suffix(container_name: str, save_id: str) -> str | None:
    prefix = f"{save_id}-"
    if not container_name.startswith(prefix):
        return None
    suffix = container_name[len(prefix):]
    if suffix.startswith("Players-"):
        player_id = suffix[len("Players-"):]
        return f"Players/{player_id}.sav" if player_id else None
    names = {
        "Level": "Level.sav",
        "Level-01": "Level.sav",
        "LevelMeta": "LevelMeta.sav",
        "LocalData": "LocalData.sav",
        "WorldOption": "WorldOption.sav",
    }
    return names.get(suffix)


def _filetime_iso(value: int) -> str:
    try:
        timestamp = (value - 116444736000000000) / 10_000_000
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def discover_worlds(wgs_path: Path | None = None) -> tuple[GamePassWorld, ...]:
    worlds: list[GamePassWorld] = []
    for user in find_user_containers(wgs_path):
        entries = read_container_index(user)
        save_ids = sorted({entry.name.split("-", 1)[0] for entry in entries if "-" in entry.name})
        for save_id in save_ids:
            latest: dict[str, GamePassContainer] = {}
            for entry in entries:
                logical = _logical_suffix(entry.name, save_id)
                if logical is None:
                    continue
                current = latest.get(logical)
                if current is None or (entry.sequence, entry.modified_filetime) > (current.sequence, current.modified_filetime):
                    latest[logical] = entry
            if "Level.sav" not in latest:
                continue
            warnings: list[str] = []
            if not any(name.startswith("Players/") for name in latest):
                warnings.append("未检测到玩家容器")
            missing = [name for name, entry in latest.items() if not (user / entry.directory_id).is_dir()]
            if missing:
                warnings.append(f"{len(missing)} 个容器数据目录尚未同步到本机")
            modified = max((entry.modified_filetime for entry in latest.values()), default=0)
            worlds.append(GamePassWorld(str(user), save_id, tuple(sorted(latest)), sum(name.startswith("Players/") for name in latest), sum(entry.size for entry in latest.values()), _filetime_iso(modified), tuple(warnings)))
    return tuple(sorted(worlds, key=lambda item: item.modified_at, reverse=True))


def _read_container_payload(user: Path, entry: GamePassContainer) -> bytes:
    directory = user / entry.directory_id
    if not directory.is_dir():
        raise FileNotFoundError(f"Game Pass 容器数据目录不存在：{directory}")
    lists = sorted(directory.glob("container.*"), key=lambda p: (p.name != f"container.{entry.sequence}", p.name))
    if not lists:
        raise FileNotFoundError(f"Game Pass 容器缺少 container.*：{directory}")
    for list_path in lists:
        with list_path.open("rb") as stream:
            version = _read_u32(stream, "数据容器版本")
            if version != CONTAINER_FILE_VERSION:
                continue
            count = _read_u32(stream, "数据文件数量")
            if count > 10000:
                raise GamePassSaveError(f"Game Pass 数据文件数量超过安全上限：{count}")
            for index in range(count):
                _read_utf16(stream, f"数据文件 {index + 1} 名称", 64)
                _read_exact(stream, 16, f"数据文件 {index + 1} 保留 UUID")
                file_uuid = uuid.UUID(bytes=_read_exact(stream, 16, f"数据文件 {index + 1} UUID"))
                payload_path = directory / file_uuid.bytes_le.hex().upper()
                if payload_path.is_file():
                    return payload_path.read_bytes()
    raise FileNotFoundError(f"Game Pass 容器 {entry.name} 的数据尚未同步到本机")


def extract_world(
    user_container: Path,
    save_id: str,
    destination: Path,
    on_progress: Callable[[int, str], None] | None = None,
) -> dict:
    user = Path(user_container).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    entries = read_container_index(user)
    selected: dict[str, GamePassContainer] = {}
    for entry in entries:
        logical = _logical_suffix(entry.name, save_id)
        if logical is None:
            continue
        current = selected.get(logical)
        if current is None or (entry.sequence, entry.modified_filetime) > (current.sequence, current.modified_filetime):
            selected[logical] = entry
    if "Level.sav" not in selected:
        raise GamePassSaveError(f"Game Pass 世界 {save_id} 缺少 Level 容器")
    staging = destination.with_name(destination.name + f".xgp-{uuid.uuid4().hex}.tmp")
    backup = destination.with_name(destination.name + f".{datetime.now():%Y%m%d-%H%M%S}.bak")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for index, (relative, entry) in enumerate(sorted(selected.items()), 1):
            payload = _read_container_payload(user, entry)
            if not payload:
                raise GamePassSaveError(f"Game Pass 容器 {entry.name} 内容为空")
            target = staging / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            if on_progress:
                on_progress(10 + round(index / len(selected) * 75), f"正在提取 {relative}")
        if not (staging / "Level.sav").is_file() or not (staging / "Level.sav").stat().st_size:
            raise GamePassSaveError("Game Pass 转换候选缺少有效 Level.sav")
        if destination.exists():
            if backup.exists():
                raise FileExistsError(f"目标备份路径已存在：{backup}")
            destination.rename(backup)
        try:
            staging.rename(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if on_progress:
        on_progress(100, "Game Pass 世界已转换为 Steam 目录布局")
    files = tuple(sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()))
    return {
        "source": str(user),
        "save_id": save_id,
        "destination": str(destination),
        "backup": str(backup) if backup.exists() else "",
        "files": files,
        "player_count": sum(name.startswith("Players/") for name in files),
        "read_only_source": True,
    }
