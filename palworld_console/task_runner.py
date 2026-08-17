from __future__ import annotations

import time
from pathlib import Path

from .management import AutomationService, AuditService, WhitelistService
from .services import BackupService, LocalServerLifecycle, PalworldRestClient, PlayerAdminService
from .storage import AppStorage


def run_scheduled_task(instance_id: str, task_id: str) -> int:
    storage = AppStorage()
    instances = storage.load_instances()
    instance = next((item for item in instances if item.id == instance_id), None)
    if not instance:
        raise RuntimeError(f"找不到实例: {instance_id}")
    raw = next((item for item in instance.schedules if item.get("id") == task_id), None)
    if not raw:
        raise RuntimeError(f"找不到计划任务: {task_id}")
    task = AutomationService.validate(raw)
    if not task.enabled:
        return 0
    if instance.kind != "local":
        raise RuntimeError("远程实例计划任务应由远程 systemd timer 执行")
    rest = PalworldRestClient(instance.rest_url or "http://127.0.0.1:8212", storage.get_secret(instance.admin_secret_ref))
    lifecycle = LocalServerLifecycle(instance)
    if task.action == "backup":
        try: rest.save()
        except Exception: pass
        root = storage.root / "backups" / instance.id
        BackupService().create_local(instance, root)
        files = sorted(root.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in files[task.retention:]: old.unlink()
    elif task.action == "save": rest.save()
    elif task.action == "broadcast": rest.announce(str(task.payload.get("message") or "服务器计划通知"))
    elif task.action == "restart":
        rest.shutdown(10); time.sleep(15); lifecycle.start()
    elif task.action == "health": rest.health()
    elif task.action == "whitelist":
        players = PlayerAdminService(rest).list_players()
        unauthorized = WhitelistService.unauthorized(instance.whitelist, [p.user_id or p.player_uid for p in players])
        if instance.whitelist_policy == "kick":
            for uid in unauthorized: rest.kick(uid, "未加入服务器白名单")
    elif task.action == "update":
        raise RuntimeError("本机自动更新需要在 GUI 中确认 SteamCMD 路径后执行")
    AuditService.record(instance, f"计划任务:{task.action}", task.id)
    storage.save_instances(instances)
    return 0
