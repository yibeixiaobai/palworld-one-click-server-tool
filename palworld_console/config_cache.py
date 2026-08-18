from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from .models import ConfigSyncResult


SECRET_KEYS = {"AdminPassword", "ServerPassword"}


@dataclass
class ConfigCacheRecord:
    instance_id: str
    values: dict[str, Any] = field(default_factory=dict)
    config_path: str = ""
    source: str = ""
    synced_at: str = ""
    content_hash: str = ""
    secret_presence: dict[str, bool] = field(default_factory=dict)


@dataclass
class ConfigDraft:
    instance_id: str
    values: dict[str, Any] = field(default_factory=dict)
    base_hash: str = ""
    saved_at: str = ""
    secret_presence: dict[str, bool] = field(default_factory=dict)


class ConfigCacheRepository:
    def __init__(self, root: Path):
        self.root = Path(root) / "config-cache"

    def load_snapshot(self, instance_id: str) -> ConfigCacheRecord | None:
        payload = self._read(self._snapshot_path(instance_id))
        return ConfigCacheRecord(**payload) if payload else None

    def save_snapshot(self, instance_id: str, result: ConfigSyncResult) -> ConfigCacheRecord:
        values, presence = self.sanitize(result.values)
        record = ConfigCacheRecord(
            instance_id=instance_id, values=values, config_path=result.config_path,
            source=result.source, synced_at=result.synced_at or self._now(),
            content_hash=self.hash_values(values), secret_presence=presence,
        )
        self._write(self._snapshot_path(instance_id), asdict(record))
        return record

    def load_draft(self, instance_id: str) -> ConfigDraft | None:
        payload = self._read(self._draft_path(instance_id))
        return ConfigDraft(**payload) if payload else None

    def save_draft(self, instance_id: str, values: dict[str, Any], base_hash: str = "") -> ConfigDraft:
        clean, presence = self.sanitize(values)
        draft = ConfigDraft(instance_id, clean, base_hash, self._now(), presence)
        self._write(self._draft_path(instance_id), asdict(draft))
        return draft

    def clear_draft(self, instance_id: str) -> None:
        self._draft_path(instance_id).unlink(missing_ok=True)

    def remove_instance(self, instance_id: str) -> None:
        directory = self.root / instance_id
        if not directory.exists():
            return
        for child in directory.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass

    @staticmethod
    def sanitize(values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
        clean = {key: value for key, value in values.items() if key not in SECRET_KEYS}
        presence = {key: bool(values.get(key)) for key in SECRET_KEYS}
        return clean, presence

    @staticmethod
    def hash_values(values: dict[str, Any]) -> str:
        clean = {key: value for key, value in values.items() if key not in SECRET_KEYS}
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _snapshot_path(self, instance_id: str) -> Path:
        return self.root / instance_id / "snapshot.json"

    def _draft_path(self, instance_id: str) -> Path:
        return self.root / instance_id / "draft.json"

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)
