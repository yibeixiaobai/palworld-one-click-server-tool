from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .management import SaveGameService
from .save_fields import resolve_path, validate_value


@dataclass(frozen=True)
class PlayerDraftChange:
    path: str
    label: str
    original: Any
    value: Any
    object_type: str
    object_id: str
    risk: str = "中"


@dataclass
class PlayerEditSession:
    instance_id: str
    player_uid: str
    changes: dict[str, PlayerDraftChange] = field(default_factory=dict)

    def stage(self, path: str, original: Any, value: Any, label: str, object_type: str, object_id: str, risk: str = "中") -> None:
        definition = resolve_path(path)
        converted = validate_value(definition, value)
        if converted == original:
            self.changes.pop(path, None)
            return
        self.changes[path] = PlayerDraftChange(path, label, original, converted, object_type, object_id, risk)

    def discard(self, path: str | None = None) -> None:
        if path is None: self.changes.clear()
        else: self.changes.pop(path, None)

    def value_for(self, path: str, original: Any) -> Any:
        change = self.changes.get(path)
        return change.value if change else original

    def preview(self) -> list[str]:
        return [f"{change.label}：{change.original} -> {change.value}" for change in self.changes.values()]

    def apply(self, document: Any) -> None:
        for change in self.changes.values():
            current = SaveGameService.get_path(document.properties, change.path)
            if current != change.original:
                raise RuntimeError(f"服务器存档字段已变化，请重新同步：{change.label} 当前为 {current!r}，同步时为 {change.original!r}")
            SaveGameService.set_path(document.properties, change.path, change.value)
