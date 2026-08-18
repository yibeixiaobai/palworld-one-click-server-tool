from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile
import time
import uuid
import zipfile
import re
import shlex
from dataclasses import dataclass
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import SaveChangeSet, SaveValidationResult, ScheduleDefinition, WhitelistEntry
from .save_codec import PlmCodecPlugin, PluginParsedSave


@dataclass
class ParsedSave:
    gvas: Any
    save_type: int

    @property
    def properties(self):
        return self.gvas.properties


class RconClient:
    """Small Source RCON client; callers tunnel remote endpoints over SSH."""

    AUTH = 3
    EXEC = 2

    def __init__(self, host: str, port: int, password: str, timeout: float = 8.0):
        self.host, self.port, self.password, self.timeout = host, int(port), password, timeout

    @staticmethod
    def _packet(request_id: int, packet_type: int, body: str) -> bytes:
        payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\0\0"
        return struct.pack("<i", len(payload)) + payload

    @staticmethod
    def _receive(sock: socket.socket) -> tuple[int, int, str]:
        size_data = RconClient._read_exact(sock, 4)
        size = struct.unpack("<i", size_data)[0]
        if size < 10 or size > 16 * 1024 * 1024:
            raise RuntimeError("RCON 返回了无效数据包")
        payload = RconClient._read_exact(sock, size)
        request_id, packet_type = struct.unpack("<ii", payload[:8])
        return request_id, packet_type, payload[8:-2].decode("utf-8", errors="replace")

    @staticmethod
    def _read_exact(sock: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            block = sock.recv(size - len(chunks))
            if not block:
                raise RuntimeError("RCON 连接意外关闭")
            chunks.extend(block)
        return bytes(chunks)

    def command(self, command: str) -> str:
        if not command.strip():
            raise ValueError("RCON 命令不能为空")
        request_id = int(time.time() * 1000) & 0x7FFFFFFF
        with socket.create_connection((self.host, self.port), self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(self._packet(request_id, self.AUTH, self.password))
            auth_id, _, _ = self._receive(sock)
            if auth_id == -1:
                raise RuntimeError("RCON 认证失败")
            sock.sendall(self._packet(request_id + 1, self.EXEC, command))
            response_id, _, body = self._receive(sock)
            if response_id not in {request_id + 1, request_id}:
                raise RuntimeError("RCON 响应与请求不匹配")
            return body.strip()


class WhitelistService:
    POLICIES = {"log", "warn", "kick"}

    @classmethod
    def normalize(cls, entries: list[dict[str, Any]]) -> list[WhitelistEntry]:
        seen, result = set(), []
        for raw in entries:
            uid = str(raw.get("player_uid") or "").strip()
            if not uid or uid in seen:
                continue
            seen.add(uid)
            result.append(WhitelistEntry(uid, str(raw.get("player_name") or ""), str(raw.get("platform") or "Unknown"), str(raw.get("note") or ""), bool(raw.get("enabled", True))))
        return result

    @classmethod
    def unauthorized(cls, entries: list[dict[str, Any]], online_uids: list[str]) -> list[str]:
        allowed = {item.player_uid for item in cls.normalize(entries) if item.enabled}
        return [uid for uid in online_uids if uid and uid not in allowed]


class AuditService:
    @staticmethod
    def record(instance, action: str, target: str = "", result: str = "success", detail: str = "") -> dict[str, str]:
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "target": target,
            "result": result,
            "detail": detail,
        }
        instance.operation_history.append(event)
        instance.operation_history = instance.operation_history[-1000:]
        return event


class AutomationService:
    DEFAULT_BACKUP = ScheduleDefinition(name="每日备份", action="backup", schedule="04:00", enabled=False, retention=14)
    ACTIONS = {"backup", "save", "broadcast", "restart", "update", "health", "whitelist"}

    @classmethod
    def validate(cls, raw: dict[str, Any]) -> ScheduleDefinition:
        task = ScheduleDefinition(**{key: value for key, value in raw.items() if key in ScheduleDefinition.__dataclass_fields__})
        if task.action not in cls.ACTIONS:
            raise ValueError(f"不支持的计划任务动作: {task.action}")
        hour, minute = task.schedule.split(":", 1)
        if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError("计划时间必须为 HH:MM")
        if task.retention < 1 or task.retention > 365:
            raise ValueError("保留数量必须在 1 到 365 之间")
        return task


class HostTaskDeployer:
    @staticmethod
    def systemd_units(instance_id: str, task: ScheduleDefinition, executable: str) -> tuple[str, str]:
        safe_id = "".join(ch for ch in instance_id if ch.isalnum() or ch in "-_")
        if not safe_id:
            raise ValueError("实例 ID 无效")
        hour, minute = task.schedule.split(":", 1)
        service = f"""[Unit]\nDescription=Palworld Console task {task.name}\n\n[Service]\nType=oneshot\nExecStart={executable}\nNoNewPrivileges=true\nPrivateTmp=true\n"""
        timer = f"""[Unit]\nDescription=Schedule Palworld task {task.name}\n\n[Timer]\nOnCalendar=*-*-* {int(hour):02d}:{int(minute):02d}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"""
        return service, timer

    @staticmethod
    def windows_task_arguments(instance_id: str, task: ScheduleDefinition, python: str, run_script: str) -> list[str]:
        return ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", task.schedule, "/TN", f"PalworldConsole-{instance_id}-{task.id}", "/TR", f'"{python}" "{run_script}" task-run --instance {instance_id} --task {task.id}']

    @staticmethod
    def remote_helper() -> str:
        return r'''#!/usr/bin/env python3
import base64, json, re, shutil, subprocess, sys, tarfile, urllib.request
from datetime import datetime
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
action, install, service = cfg["action"], Path(cfg["install_dir"]), cfg["service"]
saved, config = install / "Pal" / "Saved", Path(cfg["config_path"])

def rest(endpoint, payload=None):
    text = config.read_text(encoding="utf-8", errors="replace")
    password = re.search(r'AdminPassword="((?:\\.|[^"\\])*)"', text)
    if not password: raise RuntimeError("配置中未找到管理员密码")
    token = base64.b64encode(("admin:" + password.group(1).replace('\\"', '"')).encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request("http://127.0.0.1:%s/v1/api/%s" % (cfg["rest_port"], endpoint), data=data, method="POST" if data is not None else "GET", headers={"Authorization":"Basic " + token, "Content-Type":"application/json"})
    with urllib.request.urlopen(request, timeout=15) as response: return json.loads(response.read() or b"{}")

if action == "backup":
    try: rest("save", {})
    except Exception: pass
    target = Path(cfg["backup_dir"]); target.mkdir(parents=True, exist_ok=True)
    archive = target / ("saved-%s.tar.gz" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    with tarfile.open(archive, "w:gz") as tar: tar.add(saved, arcname="Saved")
    files = sorted(target.glob("saved-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[int(cfg.get("retention", 14)):]: old.unlink()
elif action == "save": rest("save", {})
elif action == "broadcast": rest("announce", {"message": cfg.get("message", "服务器计划通知")})
elif action == "restart": subprocess.run(["systemctl", "restart", service], check=True)
elif action == "health": subprocess.run(["systemctl", "is-active", "--quiet", service], check=True)
elif action == "update":
    subprocess.run(["systemctl", "stop", service], check=True)
    try: subprocess.run([cfg["steamcmd"], "+force_install_dir", str(install), "+login", "anonymous", "+app_update", "2394010", "validate", "+quit"], check=True)
    finally: subprocess.run(["systemctl", "start", service], check=True)
elif action == "whitelist":
    players = rest("players").get("players", [])
    allowed = set(cfg.get("allowed", []))
    for player in players:
        uid = str(player.get("userId") or player.get("userid") or "")
        if uid and uid not in allowed and cfg.get("policy") == "kick": rest("kick", {"userid": uid, "message": "未加入服务器白名单"})
'''


class SaveGameService:
    """Parser adapter with scalar-tree editing and strict round-trip validation."""

    def __init__(self, plm_plugin: PlmCodecPlugin | None = None):
        self._adapter_error = ""
        self.plm_plugin = plm_plugin or PlmCodecPlugin()

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        if isinstance(value, dict):
            for key, child in value.items():
                result.update(SaveGameService.flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                result.update(SaveGameService.flatten(child, f"{prefix}[{index}]"))
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[prefix] = value
        return result

    @staticmethod
    def set_path(root: Any, path: str, value: Any) -> None:
        parts = re.findall(r"(?:^|\.)([^.\[]+)|\[(\d+)\]", path)
        tokens: list[str | int] = [int(index) if index else key for key, index in parts]
        if not tokens:
            raise ValueError("存档字段路径为空")
        current = root
        for token in tokens[:-1]:
            current = current[token]
        current[tokens[-1]] = value

    def load(self, path: Path):
        raw = path.read_bytes()
        marker = raw[8:11] if len(raw) >= 12 else b""
        if marker == b"PlM":
            ready, detail = self.plm_plugin.probe()
            if not ready:
                raise RuntimeError(
                    "当前存档使用 PlM/Oodle 压缩；PlM 编辑插件尚不可用，已安全切换为只读状态。"
                    f"\n插件状态：{detail}"
                )
            return PluginParsedSave.create(self.plm_plugin.decode(path), path, self.plm_plugin)
        try:
            from palworld_save_tools.gvas import GvasFile
            from palworld_save_tools.palsav import decompress_sav_to_gvas
            from palworld_save_tools.paltypes import PALWORLD_CUSTOM_PROPERTIES, PALWORLD_TYPE_HINTS
        except ImportError as exc:
            raise RuntimeError("未安装 palworld-save-tools，存档编辑已切换为只读不可用状态") from exc
        gvas_data, save_type = decompress_sav_to_gvas(raw)
        return ParsedSave(GvasFile.read(gvas_data, PALWORLD_TYPE_HINTS, PALWORLD_CUSTOM_PROPERTIES), save_type)

    @staticmethod
    def _write_document(document, path: Path) -> None:
        if isinstance(document, PluginParsedSave):
            patch = document.patch_manifest()
            if not any(patch.get(section) for section in ("players", "pals", "inventory", "guilds", "bases")):
                raise ValueError("没有可写回的受支持存档字段")
            document.plugin.apply_patch(document.source_path, patch, path)
            document.plugin.verify_roundtrip(path, patch)
            return
        from palworld_save_tools.palsav import compress_gvas_to_sav
        from palworld_save_tools.paltypes import PALWORLD_CUSTOM_PROPERTIES
        gvas_data = document.gvas.write(PALWORLD_CUSTOM_PROPERTIES)
        path.write_bytes(compress_gvas_to_sav(gvas_data, document.save_type))

    def validate(self, path: Path) -> SaveValidationResult:
        try:
            self.load(path)
            return SaveValidationResult(True)
        except Exception as exc:
            return SaveValidationResult(False, (str(exc),))


class SaveTransaction:
    def __init__(self, service: SaveGameService | None = None):
        self.service = service or SaveGameService()

    def execute_local(
        self,
        save_path: Path,
        backup_dir: Path,
        mutate: Callable[[Any], None],
        changes: list[SaveChangeSet],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
        full_backup: Callable[[], Any] | None = None,
    ) -> Path:
        if not save_path.is_file():
            raise FileNotFoundError(f"找不到存档: {save_path}")
        free = shutil.disk_usage(save_path.parent).free
        if free < save_path.stat().st_size * 3:
            raise RuntimeError("可用磁盘空间不足，存档编辑至少需要存档体积的 3 倍")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"Level.sav.{stamp}.bak"
        failed = backup_dir / f"Level.sav.{stamp}.failed"
        stop()
        if full_backup:
            full_backup()
        shutil.copy2(save_path, backup)
        original_hash = self.service.sha256(backup)
        try:
            with tempfile.TemporaryDirectory(prefix="palworld-save-") as temp:
                candidate = Path(temp) / "Level.sav"
                document = self.service.load(backup)
                mutate(document)
                self.service._write_document(document, candidate)
                validation = self.service.validate(candidate)
                if not validation.valid:
                    raise RuntimeError("存档二次解析失败: " + "; ".join(validation.errors))
                os.replace(candidate, save_path)
            start()
            if not health():
                raise RuntimeError("写回后服务器健康检查失败")
            return backup
        except Exception:
            if save_path.exists():
                shutil.copy2(save_path, failed)
            shutil.copy2(backup, save_path)
            if self.service.sha256(save_path) != original_hash:
                raise RuntimeError("存档恢复校验失败，服务器保持停止状态")
            try:
                start()
            finally:
                raise

    def execute_remote(
        self,
        client,
        remote_save_path: str,
        local_backup_dir: Path,
        mutate: Callable[[Any], None],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
        full_backup: Callable[[], Any] | None = None,
    ) -> Path:
        local_backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        original = local_backup_dir / f"Level.sav.{stamp}.bak"
        candidate = local_backup_dir / f"Level.sav.{stamp}.candidate"
        stop()
        if full_backup:
            full_backup()
        client.download_file(remote_save_path, original)
        if shutil.disk_usage(local_backup_dir).free < original.stat().st_size * 3:
            start()
            raise RuntimeError("本机备份目录空间不足，至少需要存档体积的 3 倍")
        original_hash = self.service.sha256(original)
        remote_backup = ""
        try:
            document = self.service.load(original)
            mutate(document)
            self.service._write_document(document, candidate)
            validation = self.service.validate(candidate)
            if not validation.valid:
                raise RuntimeError("存档二次解析失败: " + "; ".join(validation.errors))
            remote_backup = client.upload_file_atomic(candidate, remote_save_path, backup=True)
            start()
            if not health():
                raise RuntimeError("写回后服务器健康检查失败")
            return original
        except Exception:
            try:
                client.upload_file_atomic(original, remote_save_path, backup=False)
                code, output, error = client.run(f"sha256sum {shlex.quote(remote_save_path)} | awk '{{print $1}}'")
                if code or output.strip() != original_hash:
                    raise RuntimeError(error.strip() or "远程回滚后的 SHA-256 不匹配")
                start()
            finally:
                candidate.unlink(missing_ok=True)
            raise
        finally:
            candidate.unlink(missing_ok=True)
