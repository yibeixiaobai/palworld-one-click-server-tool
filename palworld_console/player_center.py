from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .player_edit import PlayerEditSession


@dataclass
class PlayerCenterSnapshot:
    instance_id: str = ""
    synced: bool = False
    stale: bool = False
    synced_at: str = ""
    players: list[dict[str, Any]] = field(default_factory=list)
    online: list[dict[str, Any]] = field(default_factory=list)
    save_path: str = ""
    plugin_ready: bool = False
    error: str = ""


class PlayerCenterController:
    """Single source of truth for the player-center workflow and draft lifecycle."""

    def __init__(self):
        self.snapshot = PlayerCenterSnapshot()
        self.selected_uid = ""
        self.sessions: dict[tuple[str, str], PlayerEditSession] = {}
        self.last_failure = ""
        self.retry_available = False

    def begin_sync(self, instance_id: str) -> None:
        if self.snapshot.instance_id != instance_id:
            self.sessions = {}
            self.selected_uid = ""
        self.snapshot = PlayerCenterSnapshot(instance_id=instance_id)
        self.last_failure = ""
        self.retry_available = False

    def complete_sync(self, instance_id: str, players: list[dict[str, Any]], online: list[dict[str, Any]], save_path: str, plugin_ready: bool) -> None:
        self.snapshot = PlayerCenterSnapshot(instance_id, True, False, datetime.now().isoformat(timespec="seconds"), players, online, save_path, plugin_ready)
        self.last_failure = ""
        self.retry_available = False

    def fail_sync(self, message: str) -> None:
        self.snapshot.stale = self.snapshot.synced
        self.snapshot.error = message
        self.last_failure = message

    def select(self, uid: str) -> None:
        if not self.snapshot.synced:
            raise RuntimeError("请先同步玩家数据")
        self.selected_uid = str(uid or "")

    def session(self, instance_id: str, uid: str, create: bool = True) -> PlayerEditSession | None:
        if not uid: return None
        key = (instance_id, str(uid))
        if create and key not in self.sessions: self.sessions[key] = PlayerEditSession(*key)
        return self.sessions.get(key)

    def pending_count(self, instance_id: str, uid: str) -> int:
        session = self.session(instance_id, uid, False)
        return len(session.changes) if session else 0

    def mark_save_failure(self, message: str) -> None:
        self.last_failure = message
        self.retry_available = True

    def mark_saved(self, instance_id: str, uid: str) -> None:
        self.sessions.pop((instance_id, uid), None)
        self.last_failure = ""
        self.retry_available = False

    def discard(self, instance_id: str, uid: str) -> None:
        session = self.session(instance_id, uid, False)
        if session: session.discard()

    def has_pending(self, instance_id: str, uid: str) -> bool:
        return self.pending_count(instance_id, uid) > 0
