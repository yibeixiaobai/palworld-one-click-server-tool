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
    role_uids: tuple[str, ...] = ()


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
    def group(records: list[PlayerRecord], canonical_by_uid: dict[str, str] | None = None) -> list[PlayerIdentityGroup]:
        canonical_by_uid = canonical_by_uid or {}
        buckets: dict[str, list[PlayerRecord]] = {}
        order: list[str] = []
        for player in records:
            key = canonical_by_uid.get(player.player_uid) or (f"user:{player.user_id}" if player.user_id else f"uid:{player.player_uid}")
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(player)
        groups = []
        for key in order:
            items = buckets[key]
            primary = sorted(items, key=lambda p: (p.save_status == "active", p.online, p.save_status != "missing", p.last_seen, p.level), reverse=True)[0]
            aliases = tuple(dict.fromkeys(p.player_uid for p in items if p.player_uid))
            role_uids = tuple(dict.fromkeys(p.player_uid for p in items if p.player_uid and p.save_status == "active"))
            groups.append(PlayerIdentityGroup(primary, aliases, role_uids))
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
        CREATE TABLE IF NOT EXISTS guilds (
            instance_id TEXT NOT NULL, guild_id TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(instance_id, guild_id)
        );
        CREATE TABLE IF NOT EXISTS bases (
            instance_id TEXT NOT NULL, base_id TEXT NOT NULL, guild_id TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL, PRIMARY KEY(instance_id, base_id)
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
        CREATE INDEX IF NOT EXISTS idx_bases_guild ON bases(instance_id, guild_id);
        """)
        pal_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(pals)")}
        if "individual_id" not in pal_columns:
            self.connection.execute("ALTER TABLE pals ADD COLUMN individual_id TEXT NOT NULL DEFAULT ''")
        self.connection.execute("DROP INDEX IF EXISTS idx_pals_stable")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_pals_stable_lookup ON pals(instance_id,player_uid,individual_id)"
        )
        instances = self.connection.execute("SELECT DISTINCT instance_id FROM players").fetchall()
        for row in instances:
            self._reconcile_identity_aliases(row["instance_id"])
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
        now = self._now(); players = list(payload.get("players") or []); guilds = list(payload.get("guilds") or []); bases = list(payload.get("bases") or [])
        seen: set[str] = set(); guild_map: dict[str, tuple[str, str, int]] = {}
        for guild in guilds:
            gid = str(guild.get("guild_id") or guild.get("name") or "")
            admin = str(guild.get("admin_player_uid") or "")
            for member in guild.get("players") or []:
                uid = str(member.get("player_uid") or "")
                if uid: guild_map[uid] = (gid, str(guild.get("name") or ""), int(uid == admin))
        try:
            self.connection.execute("BEGIN")
            self.connection.execute("DELETE FROM guild_memberships WHERE instance_id=?", (instance_id,))
            self.connection.execute("DELETE FROM guilds WHERE instance_id=?", (instance_id,))
            self.connection.execute("DELETE FROM bases WHERE instance_id=?", (instance_id,))
            for guild in guilds:
                gid = str(guild.get("guild_id") or guild.get("name") or "")
                if gid:
                    self.connection.execute("INSERT INTO guilds VALUES(?,?,?)", (instance_id, gid, json.dumps(guild, ensure_ascii=False)))
            for base in bases:
                base_id = str(base.get("base_id") or "")
                if base_id:
                    self.connection.execute("INSERT INTO bases VALUES(?,?,?,?)", (instance_id, base_id, str(base.get("guild_id") or ""), json.dumps(base, ensure_ascii=False)))
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
                self.connection.execute("""
                    INSERT INTO player_aliases(instance_id,canonical_key,player_uid,user_id,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET last_seen=excluded.last_seen
                """, (instance_id, f"uid:{uid}", uid, "", now, now))
                self.connection.execute("DELETE FROM pals WHERE instance_id=? AND player_uid=?", (instance_id, uid))
                for index, pal in enumerate(raw.get("pals") or []):
                    self.connection.execute(
                        "INSERT INTO pals(instance_id,player_uid,pal_index,payload,individual_id) VALUES(?,?,?,?,?)",
                        (instance_id, uid, index, json.dumps(pal, ensure_ascii=False), str(pal.get("individual_id") or "")),
                    )
                self.connection.execute("DELETE FROM inventory_items WHERE instance_id=? AND player_uid=?", (instance_id, uid))
                for container, items in (raw.get("items") or {}).items():
                    for item in items or []:
                        self.connection.execute("INSERT INTO inventory_items VALUES(?,?,?,?,?)", (instance_id, uid, str(container), int(item.get("SlotIndex") or 0), json.dumps(item, ensure_ascii=False)))
                if uid in guild_map:
                    gid, name, admin = guild_map[uid]
                    self.connection.execute("INSERT INTO guild_memberships VALUES(?,?,?,?,?)", (instance_id, uid, gid, name, admin))
            rows = self.connection.execute("SELECT player_uid FROM players WHERE instance_id=?", (instance_id,)).fetchall()
            for row in rows:
                if row["player_uid"] not in seen:
                    self.connection.execute("UPDATE players SET online=0,save_status='missing' WHERE instance_id=? AND player_uid=?", (instance_id, row["player_uid"]))
            self._reconcile_identity_aliases(instance_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return len(seen)

    @staticmethod
    def _identity_name(*values: str) -> str:
        return " ".join(" ".join(str(value or "").strip().casefold().split()) for value in values if str(value or "").strip())

    def _reconcile_identity_aliases(self, instance_id: str) -> None:
        rows = self.connection.execute("SELECT player_uid,user_id,account_name,nickname,save_status FROM players WHERE instance_id=?", (instance_id,)).fetchall()
        active_by_name: dict[str, list[sqlite3.Row]] = {}
        platform_by_name: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            name = self._identity_name(row["nickname"] or row["account_name"])
            if not name:
                continue
            if row["save_status"] == "active":
                active_by_name.setdefault(name, []).append(row)
            if row["user_id"] and row["player_uid"] != row["user_id"]:
                continue
            if row["user_id"] or str(row["player_uid"]).lower().startswith(("steam_", "xbox_", "epic_")):
                platform_by_name.setdefault(name, []).append(row)
        now = self._now()
        for name, save_rows in active_by_name.items():
            platform_rows = platform_by_name.get(name, [])
            if len(save_rows) != 1 or len(platform_rows) != 1:
                continue
            save_row, platform_row = save_rows[0], platform_rows[0]
            if save_row["player_uid"] == platform_row["player_uid"]:
                continue
            user_id = str(platform_row["user_id"] or platform_row["player_uid"])
            canonical = f"user:{user_id}"
            for row in (save_row, platform_row):
                self.connection.execute("""
                    INSERT INTO player_aliases(instance_id,canonical_key,player_uid,user_id,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET
                    canonical_key=excluded.canonical_key,user_id=excluded.user_id,last_seen=excluded.last_seen
                """, (instance_id, canonical, row["player_uid"], user_id, now, now))
            self.connection.execute("UPDATE players SET user_id=?,account_name=CASE WHEN account_name='' THEN ? ELSE account_name END WHERE instance_id=? AND player_uid=?", (user_id, platform_row["account_name"] or platform_row["nickname"], instance_id, save_row["player_uid"]))

    def overlay_online(self, instance_id: str, players: list[PlayerRecord]) -> None:
        now = self._now(); online_uids = set()
        for player in PlayerIdentityService.deduplicate_online(players):
            uid = player.player_uid or player.user_id
            if not uid: continue
            resolved_uid = uid
            if player.user_id:
                alias = self.connection.execute("SELECT player_uid FROM player_aliases WHERE instance_id=? AND canonical_key=? ORDER BY player_uid", (instance_id, f"user:{player.user_id}")).fetchall()
                active = []
                for row in alias:
                    saved = self.connection.execute("SELECT save_status FROM players WHERE instance_id=? AND player_uid=?", (instance_id, row["player_uid"])).fetchone()
                    if saved and saved["save_status"] == "active": active.append(row["player_uid"])
                if len(active) == 1: resolved_uid = active[0]
            if resolved_uid == uid:
                online_name = self._identity_name(player.name or player.account_name)
                candidates = self.connection.execute("SELECT player_uid,nickname,account_name FROM players WHERE instance_id=? AND save_status='active'", (instance_id,)).fetchall()
                matching = [row["player_uid"] for row in candidates if online_name and self._identity_name(row["nickname"] or row["account_name"]) == online_name]
                if len(matching) == 1: resolved_uid = matching[0]
            online_uids.add(resolved_uid); existing = self.connection.execute("SELECT first_seen,masked_ips,note,tags FROM players WHERE instance_id=? AND player_uid=?", (instance_id, resolved_uid)).fetchone()
            masked = json.loads(existing["masked_ips"] or "[]") if existing else []
            value = self._masked_ip(player.ip)
            if value: masked = list(dict.fromkeys([*masked, value]))[-10:]
            self.connection.execute("""
                INSERT INTO players(instance_id,player_uid,user_id,account_name,nickname,level,first_seen,last_seen,online,save_status,masked_ips,note,tags)
                VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET
                user_id=excluded.user_id,account_name=excluded.account_name,nickname=CASE WHEN excluded.nickname<>'' THEN excluded.nickname ELSE players.nickname END,
                level=CASE WHEN excluded.level>0 THEN excluded.level ELSE players.level END,last_seen=excluded.last_seen,online=1,masked_ips=excluded.masked_ips
            """, (instance_id, resolved_uid, player.user_id, player.account_name, player.name, player.level, existing["first_seen"] if existing else now, now, "online", json.dumps(masked, ensure_ascii=False), existing["note"] if existing else "", existing["tags"] if existing else "[]"))
            canonical = f"user:{player.user_id}" if player.user_id else f"uid:{uid}"
            for alias_uid in dict.fromkeys((resolved_uid, uid)):
                self.connection.execute("""
                    INSERT INTO player_aliases(instance_id,canonical_key,player_uid,user_id,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(instance_id,player_uid) DO UPDATE SET
                    canonical_key=excluded.canonical_key,user_id=excluded.user_id,last_seen=excluded.last_seen
                """, (instance_id, canonical, alias_uid, player.user_id, now, now))
        self._reconcile_identity_aliases(instance_id)
        self.connection.execute("UPDATE players SET online=0 WHERE instance_id=?", (instance_id,))
        if online_uids:
            self.connection.executemany("UPDATE players SET online=1 WHERE instance_id=? AND player_uid=?", [(instance_id, uid) for uid in online_uids])
        self.connection.commit()

    def list_players(self, instance_id: str) -> list[PlayerRecord]:
        rows = self.connection.execute("SELECT * FROM players WHERE instance_id=? ORDER BY online DESC,last_seen DESC,level DESC", (instance_id,)).fetchall()
        return [PlayerRecord(name=row["nickname"], account_name=row["account_name"], user_id=row["user_id"], player_uid=row["player_uid"], level=row["level"], guild_id=row["guild_id"], experience=row["experience"], online=bool(row["online"]), first_seen=row["first_seen"], last_seen=row["last_seen"], save_status=row["save_status"], note=row["note"]) for row in rows]

    def list_identity_groups(self, instance_id: str) -> list[PlayerIdentityGroup]:
        aliases = self.connection.execute("SELECT player_uid,canonical_key FROM player_aliases WHERE instance_id=?", (instance_id,)).fetchall()
        return PlayerIdentityService.group(self.list_players(instance_id), {row["player_uid"]: row["canonical_key"] for row in aliases})

    def audit_player(self, instance_id: str, uid: str, action: str, reason: str = "", detail: str = "") -> None:
        self.connection.execute(
            "INSERT INTO player_audit_events(instance_id,player_uid,created_at,action,reason,detail) VALUES(?,?,?,?,?,?)",
            (instance_id, uid, self._now(), action, reason, detail),
        )
        self.connection.commit()

    def player_detail(self, instance_id: str, uid: str) -> dict[str, Any]:
        player = self.connection.execute("SELECT * FROM players WHERE instance_id=? AND player_uid=?", (instance_id, uid)).fetchone()
        if not player: return {}
        snapshot_row = self.connection.execute(
            "SELECT payload FROM player_snapshots WHERE instance_id=? AND player_uid=? ORDER BY id DESC LIMIT 1", (instance_id, uid)
        ).fetchone()
        snapshot = json.loads(snapshot_row["payload"]) if snapshot_row else {}
        pals = [json.loads(row["payload"]) for row in self.connection.execute("SELECT payload FROM pals WHERE instance_id=? AND player_uid=? ORDER BY pal_index", (instance_id, uid))]
        items = [json.loads(row["payload"]) | {"container": row["container"]} for row in self.connection.execute("SELECT container,payload FROM inventory_items WHERE instance_id=? AND player_uid=? ORDER BY container,slot_index", (instance_id, uid))]
        membership = self.connection.execute("SELECT * FROM guild_memberships WHERE instance_id=? AND player_uid=?", (instance_id, uid)).fetchone()
        guild_payload: dict[str, Any] = {}
        guild_id = str(membership["guild_id"] if membership else "")
        if guild_id:
            guild_row = self.connection.execute("SELECT payload FROM guilds WHERE instance_id=? AND guild_id=?", (instance_id, guild_id)).fetchone()
            if guild_row: guild_payload = json.loads(guild_row["payload"])
        guild = (dict(membership) if membership else {}) | guild_payload
        bases = [json.loads(row["payload"]) for row in self.connection.execute("SELECT payload FROM bases WHERE instance_id=? AND guild_id=? ORDER BY base_id", (instance_id, guild_id))] if guild_id else []
        members = list(guild_payload.get("players") or [])
        completeness = {
            "pals": "complete" if all(pal.get("data_status", "complete") == "complete" for pal in pals) else "partial",
            "inventory": snapshot.get("inventory_status", "complete" if all(item.get("data_status", "complete") == "complete" for item in items) else "partial"),
            "guild": guild_payload.get("data_status", "complete" if guild_payload else "empty"),
            "bases": "complete" if guild_payload and all(base.get("data_status") == "complete" for base in bases) else "empty" if not bases else "partial",
        }
        return {
            "player": dict(player), "pals": pals, "items": items, "guild": guild, "guild_members": members, "bases": bases,
            "completeness": completeness, "inventory_containers": list(snapshot.get("inventory_containers") or []),
            "inventory_read_only_reason": str(snapshot.get("inventory_read_only_reason") or ""),
        }

    def set_note(self, instance_id: str, uid: str, note: str) -> None:
        self.connection.execute("UPDATE players SET note=? WHERE instance_id=? AND player_uid=?", (note, instance_id, uid)); self.connection.commit()

    def close(self) -> None:
        self.connection.close()
