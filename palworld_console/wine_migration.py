from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import base64
import re
import shlex
from typing import Any, Callable

from .models import ServerInstance, TaskProgress


@dataclass
class WineMigrationPreflight:
    ready: bool
    target_dir: str
    source_dir: str
    source_service: str
    wine_path: str = ""
    steamcmd_path: str = ""
    architecture: str = ""
    distribution: str = ""
    free_kb: int = 0
    missing: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WineMigrationService:
    MIN_FREE_KB = 12 * 1024 * 1024

    def __init__(self, client, instance: ServerInstance, on_log: Callable[[str], None] = lambda _line: None, on_progress: Callable[[TaskProgress], None] = lambda _progress: None):
        self.client = client
        self.instance = instance
        self.on_log = on_log
        self.on_progress = on_progress

    def inspect(self, target_dir: str = "") -> WineMigrationPreflight:
        profile = self.instance.remote_profile
        source = self.client.resolve_path(str(profile.get("install_dir") or self.instance.install_dir))
        target = self.client.resolve_path(target_dir or "$HOME/palworld-wine-server", require_writable_parent=True)
        if target == source or target.startswith(source.rstrip("/") + "/") or source.startswith(target.rstrip("/") + "/"):
            raise ValueError("Wine 安装目录必须与原生 Linux 服务端目录完全隔离")
        command = """set -e
printf 'DIST='; . /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-${ID:-Linux}}" || printf Linux
printf '\nARCH='; uname -m
printf '\nWINE='; command -v wine64 || command -v wine || true
printf '\nSTEAMCMD='; for p in "$(command -v steamcmd 2>/dev/null || true)" "$HOME/steamcmd/steamcmd.sh" "$HOME/.local/share/SteamCMD/steamcmd.sh" /usr/games/steamcmd /opt/steamcmd/steamcmd.sh; do [ -n "$p" ] && [ -x "$p" ] && { readlink -f "$p" 2>/dev/null || printf '%s' "$p"; break; }; done
printf '\nSUDO='; sudo -n true >/dev/null 2>&1 && printf yes || printf no
printf '\nSYSTEMD='; command -v systemctl >/dev/null 2>&1 && printf yes || printf no
printf '\nFREE='; df -Pk "$HOME" | awk 'NR==2 {print $4}'
"""
        code, output, error = self.client.run(command)
        if code:
            raise RuntimeError(error.strip() or "Wine 迁移预检失败")
        values = {}
        for line in output.splitlines():
            if "=" in line:
                key, value = line.split("=", 1); values[key] = value.strip()
        missing, suggestions = [], []
        wine = values.get("WINE", ""); steamcmd = values.get("STEAMCMD", "")
        free_kb = int(values.get("FREE") or 0)
        if not wine:
            missing.append("wine64"); suggestions.append("请先使用发行版软件包管理器安装 64 位 Wine")
        if not steamcmd:
            missing.append("SteamCMD"); suggestions.append("请先在远程主机安装 SteamCMD，或重新运行服务器环境检测")
        if values.get("SUDO") != "yes":
            missing.append("免交互 sudo"); suggestions.append("需要允许当前 SSH 用户管理本实例的 systemd 服务")
        if values.get("SYSTEMD") != "yes":
            missing.append("systemd")
        if free_kb < self.MIN_FREE_KB:
            missing.append("磁盘空间"); suggestions.append("Wine 隔离目录至少预留 12 GB 可用空间")
        return WineMigrationPreflight(not missing, target, source, str(profile.get("service_name") or "palworld"), wine, steamcmd, values.get("ARCH", ""), values.get("DIST", "Linux"), free_kb, tuple(missing), tuple(suggestions), datetime.now().isoformat(timespec="seconds"))

    def prepare(self, preflight: WineMigrationPreflight) -> dict[str, Any]:
        if not preflight.ready:
            raise RuntimeError("Wine 迁移条件不完整：" + "、".join(preflight.missing))
        self.on_progress(TaskProgress(10, "准备 Wine 实例", "正在创建隔离目录"))
        target, source = preflight.target_dir, preflight.source_dir
        staging_game = min(65535, int(self.instance.game_port) + 10000)
        rest_port = int(self.instance.remote_profile.get("rest_port") or 8212)
        staging_rest = min(65535, rest_port + 10000)
        service = self._wine_service_name()
        script = f'''set -euo pipefail
target={shlex.quote(target)}
source_dir={shlex.quote(source)}
steamcmd={shlex.quote(preflight.steamcmd_path)}
wine={shlex.quote(preflight.wine_path)}
mkdir -p "$target"
"$steamcmd" +@sSteamCmdForcePlatformType windows +force_install_dir "$target" +login anonymous +app_update 2394010 validate +quit
test -f "$target/PalServer.exe"
if [ -d "$source_dir/Pal/Saved" ]; then
  mkdir -p "$target/Pal"
  rm -rf "$target/Pal/Saved.palworld-console-staging"
  cp -a "$source_dir/Pal/Saved" "$target/Pal/Saved.palworld-console-staging"
  rm -rf "$target/Pal/Saved"
  mv "$target/Pal/Saved.palworld-console-staging" "$target/Pal/Saved"
  if [ -f "$target/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini" ]; then
    mkdir -p "$target/Pal/Saved/Config/WindowsServer"
    cp -a "$target/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini" "$target/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
  fi
fi
sudo -n tee /etc/systemd/system/{service}.service >/dev/null <<'UNIT'
[Unit]
Description=Palworld Wine migration staging service
After=network-online.target
[Service]
Type=simple
User={self.instance.remote_username}
Group={self.instance.remote_profile.get('primary_group') or self.instance.remote_username}
Environment=HOME={self.instance.remote_profile.get('home_dir') or target.rsplit('/', 1)[0]}
WorkingDirectory={target}
ExecStart={preflight.wine_path} {target}/PalServer.exe -port={staging_game} -RESTAPIPort={staging_rest} -useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS -enable-gamedata-api
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl start {service}.service
for i in $(seq 1 60); do ss -lun 2>/dev/null | awk '{{print $4}}' | grep -Eq ':{staging_game}$' && break; sleep 2; done
ss -lun 2>/dev/null | awk '{{print $4}}' | grep -Eq ':{staging_game}$'
sudo -n systemctl stop {service}.service
'''
        self._run_script(script, "准备隔离 Wine 服务端")
        self.on_progress(TaskProgress(85, "验证 Wine 实例", "隔离服务已通过临时端口检查"))
        return {"status": "prepared", "target_dir": target, "source_dir": source, "source_service": preflight.source_service, "wine_service": service, "wine_path": preflight.wine_path, "steamcmd_path": preflight.steamcmd_path, "prepared_at": datetime.now().isoformat(timespec="seconds"), "staging_game_port": staging_game, "staging_rest_port": staging_rest}

    def activate(self, migration: dict[str, Any]) -> dict[str, Any]:
        service = str(migration.get("wine_service") or self._wine_service_name())
        source_service = str(migration.get("source_service") or self.instance.remote_profile.get("service_name") or "palworld")
        target = str(migration.get("target_dir") or "")
        wine = str(migration.get("wine_path") or "")
        if not target.startswith("/") or not wine.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_.@-]+", source_service):
            raise ValueError("Wine 迁移状态无效，请重新执行预检")
        game_port = int(self.instance.game_port); rest_port = int(self.instance.remote_profile.get("rest_port") or 8212)
        script = f'''set -euo pipefail
sudo -n systemctl stop {shlex.quote(source_service)}
sudo -n sed -i -E 's/-port=[0-9]+/-port={game_port}/; s/-RESTAPIPort=[0-9]+/-RESTAPIPort={rest_port}/' /etc/systemd/system/{service}.service
sudo -n systemctl daemon-reload
if ! sudo -n systemctl start {service}.service; then sudo -n systemctl start {shlex.quote(source_service)}; exit 51; fi
for i in $(seq 1 60); do ss -lun 2>/dev/null | awk '{{print $4}}' | grep -Eq ':{game_port}$' && exit 0; sleep 2; done
sudo -n systemctl stop {service}.service || true
sudo -n systemctl start {shlex.quote(source_service)}
exit 52
'''
        self.on_progress(TaskProgress(90, "切换服务", "正在切换到隔离 Wine 实例"))
        self._run_script(script, "切换 Wine 服务")
        migration = dict(migration); migration.update({"status": "active", "activated_at": datetime.now().isoformat(timespec="seconds")})
        self.on_progress(TaskProgress(100, "迁移完成", "Wine 服务已接管游戏端口"))
        return migration

    def restore_native(self, migration: dict[str, Any]) -> dict[str, Any]:
        wine_service = str(migration.get("wine_service") or self._wine_service_name())
        source_service = str(migration.get("source_service") or self.instance.remote_profile.get("service_name") or "palworld")
        code, output, error = self.client.run(f"sudo -n systemctl stop {shlex.quote(wine_service)} || true; sudo -n systemctl start {shlex.quote(source_service)}")
        if code:
            raise RuntimeError(error.strip() or output.strip() or "恢复原生 Linux 服务失败")
        migration = dict(migration); migration.update({"status": "native-restored", "restored_at": datetime.now().isoformat(timespec="seconds")})
        return migration

    def _run_script(self, script: str, label: str) -> None:
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        code, output, error = self.client.run(f"printf %s {shlex.quote(encoded)} | base64 -d | bash")
        if code:
            raise RuntimeError(f"{label}失败：{error.strip() or output.strip()}")
        if output.strip():
            self.on_log(output.strip())

    def _wine_service_name(self) -> str:
        return f"palworld-wine-{self.instance.id[:8]}"
