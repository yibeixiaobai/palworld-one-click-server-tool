from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import PlayerRecord, ServerInstance


@dataclass(frozen=True)
class PlayerIdentityGroup:
    primary: PlayerRecord
    aliases: tuple[str, ...] = ()


class PlayerIdentityService:
    """Groups identities without ever treating a nickname as a durable key."""

    @staticmethod
    def deduplicate_online(players: list[PlayerRecord]) -> list[PlayerRecord]:
        selected: dict[str, PlayerRecord] = {}
        order: list[str] = []
        for player in players:
            key = player.player_uid.strip() or player.user_id.strip()
            if not key:
                continue
            if key not in selected:
                order.append(key)
                selected[key] = player
                continue
            current = selected[key]
            selected[key] = PlayerRecord(
                name=player.name or current.name, account_name=player.account_name or current.account_name,
                user_id=player.user_id or current.user_id, player_uid=player.player_uid or current.player_uid,
                level=max(player.level, current.level), ping=player.ping or current.ping, ip=player.ip or current.ip,
                location_x=player.location_x or current.location_x, location_y=player.location_y or current.location_y,
                building_count=max(player.building_count, current.building_count), guild_id=player.guild_id or current.guild_id,
                experience=max(player.experience, current.experience), online=player.online or current.online,
                first_seen=current.first_seen or player.first_seen, last_seen=player.last_seen or current.last_seen,
                save_status=current.save_status if current.save_status != "unknown" else player.save_status, note=current.note or player.note,
            )
        return [selected[key] for key in order]

    @staticmethod
    def group(records: list[PlayerRecord]) -> list[PlayerIdentityGroup]:
        buckets: dict[str, list[PlayerRecord]] = {}
        order: list[str] = []
        for player in records:
            key = f"user:{player.user_id}" if player.user_id else f"uid:{player.player_uid}"
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(player)
        groups = []
        for key in order:
            items = buckets[key]
            primary = sorted(items, key=lambda p: (p.online, p.save_status != "missing", p.last_seen, p.level), reverse=True)[0]
            aliases = tuple(dict.fromkeys(p.player_uid for p in items if p.player_uid))
            groups.append(PlayerIdentityGroup(primary, aliases))
        return groups


class PlayerRepository:
    """Permanent per-instance player archive backed by SQLite."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            instance_id TEXT NOT NULL,
            player_uid TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            account_name TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT 'Unknown',
            nickname TEXT NOT NULL DEFAULT '',
            level INTEGER NOT NULL DEFAULT 0,
            experience INTEGER NOT NULL DEFAULT 0,
            guild_id TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            save_last_seen TEXT NOT NULL DEFAULT '',
            online INTEGER NOT NULL DEFAULT 0,
            save_status TEXT NOT NULL DEFAULT 'unknown',
            masked_ips TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            deleted_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(instance_id, player_uid)
        );
        CREATE TABLE IF NOT EXISTS player_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            player_uid TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pals (
            instance_id TEXT NOT NULL, player_uid TEXT NOT NULL, pal_index INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(instance_id, player_uid, pal_index)
        );
        CREATE TABLE IF NOT EXISTS inventory_items (
            instance_id TEXT NOT NULL, player_uid TEXT NOT NULL, container TEXT NOT NULL,
            slot_index INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(instance_id, player_uid, container, slot_index)
        );
        CREATE TABLE IF NOT EXISTS guild_memberships (
            instance_id TEXT NOT NULL, player_uid TEXT NOT NULL, guild_id TEXT NOT NULL,
            guild_name TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(instance_id, player_uid)
        );
        CREATE TABLE IF NOT EXISTS save_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            player_count INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS player_aliases (
            instance_id TEXT NOT NULL, canonical_key TEXT NOT NULL, player_uid TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            PRIMARY KEY(instance_id, player_uid)
        );
        CREATE TABLE IF NOT EXISTS player_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL,
            player_uid TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_players_last_seen ON players(instance_id, last_seen DESC);
        """)
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _masked_ip(ip: str) -> str:
        if not ip: return ""
        if "." in ip: return ip.rsplit(".", 1)[0] + ".*"
        if ":" in ip: return ":".join(ip.split(":")[:4]) + "::*"
        return "***"

    def migrate_instance_history(self, instance: ServerInstance) -> int:
        migrated = 0
        for uid, raw in dict(instance.player_history or {}).items():
            now = self._now(); first = str(raw.get("first_seen") or now); last = str(raw.get("last_seen") or first)
            self.connection.execute("""
                INSERT INTO players(instance_id,player_uid,first_seen,last_seen,masked_ips,save_status)
                VALUES(?,?,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO NOTHING
            """, (instance.id, uid, first, last, json.dumps(raw.get("masked_ips", []), ensure_ascii=False), "history"))
            migrated += 1
        self.connection.commit()
        return migrated

    def begin_sync(self, instance_id: str) -> int:
        cur = self.connection.execute("INSERT INTO save_sync_runs(instance_id,started_at,status) VALUES(?,?,?)", (instance_id, self._now(), "running")); self.connection.commit(); return int(cur.lastrowid)

    def finish_sync(self, run_id: int, status: str, count: int = 0, detail: str = "") -> None:
        self.connection.execute("UPDATE save_sync_runs SET finished_at=?,status=?,player_count=?,detail=? WHERE id=?", (self._now(), status, count, detail, run_id)); self.connection.commit()

    def upsert_save_snapshot(self, instance_id: str, payload: dict[str, Any]) -> int:
        now = self._now(); players = list(payload.get("players") or []); guilds = list(payload.get("guilds") or [])
        seen: set[str] = set()
        guild_map: dict[str, tuple[str, str, int]] = {}
        for guild in guilds:
            gid = str(guild.get("guild_id") or guild.get("name") or "")
            admin = str(guild.get("admin_player_uid") or "")
            for member in guild.get("players") or []:
                uid = str(member.get("player_uid") or "")
                if uid: guild_map[uid] = (gid, str(guild.get("name") or ""), int(uid == admin))
        for raw in players:
            uid = str(raw.get("player_uid") or "").strip()
            if not uid: continue
            seen.add(uid); guild_id = guild_map.get(uid, ("", "", 0))[0]
            existing = self.connection.execute("SELECT first_seen,note,tags,masked_ips FROM players WHERE instance_id=? AND player_uid=?", (instance_id, uid)).fetchone()
            first = existing["first_seen"] if existing else now
            self.connection.execute("""
                INSERT INTO players(instance_id,player_uid,nickname,level,experience,guild_id,first_seen,last_seen,save_last_seen,save_status,note,tags,masked_ips)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instance_id,player_uid) DO UPDATE SET nickname=excluded.nickname,level=excluded.level,
                experience=excluded.experience,guild_id=excluded.guild_id,last_seen=excluded.last_seen,
                save_last_seen=excluded.save_last_seen,save_status='active',deleted_at=''
            """, (instance_id, uid, str(raw.get("nickname") or ""), int(raw.get("level") or 0), int(raw.get("exp") or 0), guild_id, first, now, str(raw.get("save_last_online") or now), "active", existing["note"] if existing else "", existing["tags"] if existing else "[]", existing["masked_ips"] if existing else "[]"))
            self.connection.execute("INSERT INTO player_snapshots(instance_id,player_uid,captured_at,payload) VALUES(?,?,?,?)", (instance_id, uid, now, json.dumps(raw, ensure_ascii=False)))
            self.connection.execute("DELETE FROM pals WHERE instance_id=? AND player_uid=?", (instance_id, uid))
            for index, pal in enumerate(raw.get("pals") or []): self.connection.execute("INSERT INTO pals VALUES(?,?,?,?)", (instance_id, uid, index, json.dumps(pal, ensure_ascii=False)))
            self.connection.execute("DELETE FROM inventory_items WHERE instance_id=? AND player_uid=?", (instance_id, uid))
            for container, items in (raw.get("items") or {}).items():
                for item in items or []: self.connection.execute("INSERT INTO inventory_items VALUES(?,?,?,?,?)", (instance_id, uid, str(container), int(item.get("SlotIndex") or 0), json.dumps(item, ensure_ascii=False)))
            if uid in guild_map:
                gid, name, admin = guild_map[uid]; self.connection.execute("INSERT INTO guild_memberships VALUES(?,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET guild_id=excluded.guild_id,guild_name=excluded.guild_name,is_admin=excluded.is_admin", (instance_id, uid, gid, name, admin))
        rows = self.connection.execute("SELECT player_uid FROM players WHERE instance_id=?", (instance_id,)).fetchall()
        for row in rows:
            if row["player_uid"] not in seen: self.connection.execute("UPDATE players SET online=0,save_status='missing' WHERE instance_id=? AND player_uid=?", (instance_id, row["player_uid"]))
        self.connection.commit(); return len(seen)

    def overlay_online(self, instance_id: str, players: list[PlayerRecord]) -> None:
        now = self._now(); online_uids = set()
        for player in PlayerIdentityService.deduplicate_online(players):
            uid = player.player_uid or player.user_id
            if not uid: continue
            online_uids.add(uid); existing = self.connection.execute("SELECT first_seen,masked_ips,note,tags FROM players WHERE instance_id=? AND player_uid=?", (instance_id, uid)).fetchone()
            masked = json.loads(existing["masked_ips"] or "[]") if existing else []
            value = self._masked_ip(player.ip)
            if value: masked = list(dict.fromkeys([*masked, value]))[-10:]
            self.connection.execute("""
                INSERT INTO players(instance_id,player_uid,user_id,account_name,nickname,level,first_seen,last_seen,online,save_status,masked_ips,note,tags)
                VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET
                user_id=excluded.user_id,account_name=excluded.account_name,nickname=CASE WHEN excluded.nickname<>'' THEN excluded.nickname ELSE players.nickname END,
                level=CASE WHEN excluded.level>0 THEN excluded.level ELSE players.level END,last_seen=excluded.last_seen,online=1,masked_ips=excluded.masked_ips
            """, (instance_id, uid, player.user_id, player.account_name, player.name, player.level, existing["first_seen"] if existing else now, now, "online", json.dumps(masked, ensure_ascii=False), existing["note"] if existing else "", existing["tags"] if existing else "[]"))
            canonical = f"user:{player.user_id}" if player.user_id else f"uid:{uid}"
            self.connection.execute("""
                INSERT INTO player_aliases(instance_id,canonical_key,player_uid,user_id,first_seen,last_seen)
                VALUES(?,?,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET
                canonical_key=excluded.canonical_key,user_id=excluded.user_id,last_seen=excluded.last_seen
            """, (instance_id, canonical, uid, player.user_id, now, now))
        self.connection.execute("UPDATE players SET online=0 WHERE instance_id=?", (instance_id,))
        if online_uids:
            self.connection.executemany("UPDATE players SET online=1 WHERE instance_id=? AND player_uid=?", [(instance_id, uid) for uid in online_uids])
        self.connection.commit()

    def list_players(self, instance_id: str) -> list[PlayerRecord]:
        rows = self.connection.execute("SELECT * FROM players WHERE instance_id=? ORDER BY online DESC,last_seen DESC,level DESC", (instance_id,)).fetchall()
        return [PlayerRecord(name=row["nickname"], account_name=row["account_name"], user_id=row["user_id"], player_uid=row["player_uid"], level=row["level"], guild_id=row["guild_id"], experience=row["experience"], online=bool(row["online"]), first_seen=row["first_seen"], last_seen=row["last_seen"], save_status=row["save_status"], note=row["note"]) for row in rows]

    def list_identity_groups(self, instance_id: str) -> list[PlayerIdentityGroup]:
        return PlayerIdentityService.group(self.list_players(instance_id))

    def audit_player(self, instance_id: str, uid: str, action: str, reason: str = "", detail: str = "") -> None:
        self.connection.execute(
            "INSERT INTO player_audit_events(instance_id,player_uid,created_at,action,reason,detail) VALUES(?,?,?,?,?,?)",
            (instance_id, uid, self._now(), action, reason, detail),
        )
        self.connection.commit()

    def player_detail(self, instance_id: str, uid: str) -> dict[str, Any]:
        player = self.connection.execute("SELECT * FROM players WHERE instance_id=? AND player_uid=?", (instance_id, uid)).fetchone()
        if not player: return {}
        pals = [json.loads(row["payload"]) for row in self.connection.execute("SELECT payload FROM pals WHERE instance_id=? AND player_uid=? ORDER BY pal_index", (instance_id, uid))]
        items = [json.loads(row["payload"]) | {"container": row["container"]} for row in self.connection.execute("SELECT container,payload FROM inventory_items WHERE instance_id=? AND player_uid=? ORDER BY container,slot_index", (instance_id, uid))]
        guild = self.connection.execute("SELECT * FROM guild_memberships WHERE instance_id=? AND player_uid=?", (instance_id, uid)).fetchone()
        return {"player": dict(player), "pals": pals, "items": items, "guild": dict(guild) if guild else {}}

    def set_note(self, instance_id: str, uid: str, note: str) -> None:
        self.connection.execute("UPDATE players SET note=? WHERE instance_id=? AND player_uid=?", (note, instance_id, uid)); self.connection.commit()

    def close(self) -> None:
        self.connection.close()
