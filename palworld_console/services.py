from __future__ import annotations

import json
import base64
import os
import posixpath
import re
import secrets
import select
import shlex
import sys
import uuid
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
import tarfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable

from .config_ini import PalWorldSettings, default_settings_path, settings_path
from .models import ConfigSyncResult, EndpointStatus, GuildSummary, PlayerRecord, ServerHealthSnapshot, ServerInstance, TaskProgress, UninstallResult


class _LineBuffer:
    def __init__(self):
        self._buffer = ""

    def feed(self, chunk: bytes | str) -> list[str]:
        self._buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        parts = self._buffer.split("\n")
        self._buffer = parts.pop()
        return [line.rstrip("\r") for line in parts]

    def finish(self) -> list[str]:
        if not self._buffer:
            return []
        line, self._buffer = self._buffer.rstrip("\r"), ""
        return [line]


class SteamCmdInstaller:
    """Runs a user-supplied SteamCMD executable; downloading it stays an explicit UI action."""
    APP_ID = "2394010"

    @staticmethod
    def parse_progress(line: str, start: int = 20, end: int = 95) -> TaskProgress | None:
        match = re.search(r"progress:\s*([0-9]+(?:\.[0-9]+)?)", line, re.IGNORECASE)
        if not match:
            return None
        raw = max(0.0, min(100.0, float(match.group(1))))
        percent = round(start + (end - start) * raw / 100)
        return TaskProgress(percent, "下载并校验服务端", f"SteamCMD 进度 {raw:g}%")

    def install_or_update(self, steamcmd: Path, install_dir: Path, on_log: Callable[[str], None] | None = None, on_progress: Callable[[TaskProgress], None] | None = None) -> None:
        progress = on_progress or (lambda _progress: None)
        progress(TaskProgress(5, "检测环境", "正在检查 SteamCMD 和安装目录"))
        if not steamcmd.exists():
            raise FileNotFoundError(f"找不到 SteamCMD: {steamcmd}")
        install_dir.mkdir(parents=True, exist_ok=True)
        progress(TaskProgress(15, "准备安装", "正在启动 SteamCMD", True))
        command = [str(steamcmd), "+force_install_dir", str(install_dir), "+login", "anonymous", "+app_update", self.APP_ID, "validate", "+quit"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout
        for line in process.stdout:
            line = line.rstrip()
            if on_log:
                on_log(line)
            parsed = self.parse_progress(line)
            if parsed:
                progress(parsed)
            elif "Update state" in line or "Downloading" in line:
                progress(TaskProgress(20, "下载并校验服务端", line, True))
        if process.wait() != 0:
            raise RuntimeError(f"SteamCMD 安装/更新失败，退出码 {process.returncode}")
        progress(TaskProgress(98, "完成检查", "SteamCMD 安装结果验证通过"))


class RemoteHostClient:
    """SSH/SFTP transport for Linux deployment and file operations."""
    def __init__(self, host: str, username: str, password: str = "", port: int = 22, key_path: str = "", passphrase: str = ""):
        self.host, self.username, self.password, self.port = host, username, password, port
        self.key_path, self.passphrase = key_path, passphrase

    def _connect(self):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("未安装 paramiko，无法使用 SSH/SFTP") from exc
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.host, port=self.port, username=self.username, password=self.password or None, key_filename=self.key_path or None, passphrase=self.passphrase or None, timeout=12, auth_timeout=12, look_for_keys=not bool(self.password or self.key_path))
        return client

    def run(self, command: str) -> tuple[int, str, str]:
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(command)
            output, errors = stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")
            return stdout.channel.recv_exit_status(), output, errors
        finally:
            client.close()

    def run_stream(self, command: str, on_output: Callable[[str], None]) -> tuple[int, str, str]:
        client = self._connect()
        lines: list[str] = []
        buffer = _LineBuffer()
        try:
            _, stdout, _ = client.exec_command(command)
            channel = stdout.channel
            channel.set_combine_stderr(True)
            while not channel.exit_status_ready() or channel.recv_ready():
                if not channel.recv_ready():
                    time.sleep(0.05)
                    continue
                for line in buffer.feed(channel.recv(32768)):
                    lines.append(line)
                    on_output(line)
            for line in buffer.finish():
                lines.append(line)
                on_output(line)
            return channel.recv_exit_status(), "\n".join(lines), ""
        finally:
            client.close()

    def validate_linux_host(self) -> dict[str, str]:
        code, output, error = self.run("set -e; uname -s; df -h . | tail -1; command -v steamcmd || true; systemctl --version | head -1")
        if code:
            raise RuntimeError(error or "远程主机检查失败")
        if not output.startswith("Linux"):
            raise RuntimeError("远程自动部署仅支持 Linux 主机")
        return {"report": output}

    def upload_text(self, remote_path: str, content: str) -> None:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            with sftp.file(remote_path, "w") as stream:
                stream.write(content)
            sftp.close()
        finally:
            client.close()

    def download_file(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client = self._connect()
        try:
            sftp = client.open_sftp()
            try:
                sftp.get(remote_path, str(local_path))
            finally:
                sftp.close()
        finally:
            client.close()

    def upload_file_atomic(self, local_path: Path, remote_path: str, backup: bool = True) -> str:
        client = self._connect()
        temporary = f"{remote_path}.upload-{uuid.uuid4().hex}"
        backup_path = f"{remote_path}.{datetime.now():%Y%m%d-%H%M%S}.bak" if backup else ""
        try:
            sftp = client.open_sftp()
            try:
                sftp.put(str(local_path), temporary)
                sftp.chmod(temporary, 0o600)
                if backup:
                    try:
                        sftp.stat(remote_path)
                        sftp.posix_rename(remote_path, backup_path)
                    except OSError:
                        backup_path = ""
                sftp.posix_rename(temporary, remote_path)
            except Exception:
                try: sftp.remove(temporary)
                except OSError: pass
                if backup_path:
                    try: sftp.posix_rename(backup_path, remote_path)
                    except OSError: pass
                raise
            finally:
                sftp.close()
        finally:
            client.close()
        return backup_path

    @staticmethod
    def normalize_path_candidate(candidate: str, home_dir: str) -> str:
        home = posixpath.normpath(home_dir.strip().replace("\\", "/"))
        if not home.startswith("/"):
            raise ValueError("远程用户目录不是绝对路径")
        raw = candidate.strip().replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", raw):
            raise ValueError(f"远程安装目录不能使用 Windows 盘符: {candidate}")
        if not raw:
            normalized = f"{home}/palworld-server"
        elif raw == "~" or raw == "$HOME":
            normalized = home
        elif raw.startswith("~/"):
            normalized = f"{home}/{raw[2:]}"
        elif raw.startswith("$HOME/"):
            normalized = f"{home}/{raw[6:]}"
        elif raw.startswith("/"):
            normalized = raw
        else:
            normalized = f"{home}/{raw}"
        normalized = posixpath.normpath(normalized)
        duplicated_home = f"{home}/{home.lstrip('/')}"
        while normalized == duplicated_home or normalized.startswith(f"{duplicated_home}/"):
            normalized = home + normalized[len(duplicated_home):]
            normalized = posixpath.normpath(normalized)
        return normalized

    def resolve_path(self, candidate: str = "", require_writable_parent: bool = False) -> str:
        code, output, error = self.run("printf '%s' \"$HOME\"")
        home_dir = output.strip()
        if code or not home_dir.startswith("/"):
            raise RuntimeError(error.strip() or output.strip() or "无法读取远程用户目录")
        normalized = self.normalize_path_candidate(candidate, home_dir)
        encoded = base64.b64encode(normalized.encode("utf-8")).decode("ascii")
        writable_check = r'''
probe="$resolved"
while [ ! -e "$probe" ] && [ "$probe" != "/" ]; do probe="${probe%/*}"; [ -n "$probe" ] || probe="/"; done
test -d "$probe" && test -w "$probe" || { echo "安装目录的现有父目录不可写: $probe" >&2; exit 42; }
''' if require_writable_parent else ""
        command = rf'''set -e
candidate="$(printf %s {shlex.quote(encoded)} | base64 -d)"
if command -v realpath >/dev/null 2>&1; then
  resolved="$(realpath -m -- "$candidate")"
elif command -v readlink >/dev/null 2>&1; then
  resolved="$(readlink -m -- "$candidate")"
else
  echo "远程主机缺少 realpath/readlink，无法安全解析安装目录" >&2
  exit 41
fi
{writable_check}printf '%s' "$resolved"'''
        code, output, error = self.run(command)
        resolved = output.strip()
        if code or not resolved.startswith("/"):
            raise RuntimeError(error.strip() or output.strip() or "无法解析远程安装目录")
        return resolved

    def validate_install_target(self, install_dir: str) -> str:
        install_dir = self.resolve_path(install_dir)
        home_dir = self.resolve_path("~")
        dangerous = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/opt", "/proc", "/root", "/run", "/srv", "/sys", "/tmp", "/usr", "/var", home_dir}
        if install_dir in dangerous or len(PurePosixPath(install_dir).parts) < 3:
            raise ValueError(f"拒绝操作危险远程目录: {install_dir}")
        return install_dir

    def validate_palworld_install(self, install_dir: str) -> None:
        install_dir = self.validate_install_target(install_dir)
        marker = f"{install_dir}/PalServer.sh"
        code, _, error = self.run(f"test -f {shlex.quote(marker)} && test ! -L {shlex.quote(install_dir)}")
        if code:
            raise ValueError(error.strip() or f"目录不是可信的 Palworld 安装目录: {install_dir}")

    def read_text(self, remote_path: str, missing_ok: bool = False) -> str:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            try:
                with sftp.file(remote_path, "r") as stream:
                    data = stream.read()
                    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
            except FileNotFoundError:
                if missing_ok:
                    return ""
                raise
            finally:
                sftp.close()
        finally:
            client.close()

    def write_text_atomic(self, remote_path: str, content: str, backup: bool = True) -> str:
        directory = str(Path(remote_path).parent).replace("\\", "/")
        code, _, error = self.run(f"mkdir -p {shlex.quote(directory)}")
        if code:
            raise RuntimeError(f"创建远程配置目录失败：{error.strip()}")
        temp_path = f"{remote_path}.tmp-{uuid.uuid4().hex}"
        backup_path = ""
        client = self._connect()
        try:
            sftp = client.open_sftp()
            try:
                exists = True
                try:
                    sftp.stat(remote_path)
                except FileNotFoundError:
                    exists = False
                if exists and backup:
                    backup_path = f"{remote_path}.{datetime.now():%Y%m%d-%H%M%S}.bak"
                    with sftp.file(remote_path, "rb") as source, sftp.file(backup_path, "wb") as target:
                        target.write(source.read())
                    sftp.chmod(backup_path, 0o600)
                with sftp.file(temp_path, "w") as stream:
                    stream.write(content)
                sftp.chmod(temp_path, 0o600)
                try:
                    sftp.posix_rename(temp_path, remote_path)
                except (AttributeError, OSError):
                    if exists:
                        sftp.remove(remote_path)
                    sftp.rename(temp_path, remote_path)
                sftp.chmod(remote_path, 0o600)
            finally:
                try:
                    sftp.remove(temp_path)
                except OSError:
                    pass
                sftp.close()
        finally:
            client.close()
        return backup_path
    @staticmethod
    def steamcmd_discovery_command() -> str:
        return r'''for candidate in "$(command -v steamcmd 2>/dev/null || true)" \
    "$HOME/.local/share/SteamCMD/steamcmd.sh" \
    "$HOME/.local/share/SteamCMD/steamcmd" \
    "$HOME/steamcmd/steamcmd.sh" \
    "$HOME/steamcmd/steamcmd" \
    "/usr/games/steamcmd" \
    "/opt/steamcmd/steamcmd.sh" \
    "/opt/steamcmd/steamcmd"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        readlink -f "$candidate" 2>/dev/null || printf '%s\n' "$candidate"
        break
    fi
done'''

    @staticmethod
    def steamcmd_bootstrap_script() -> str:
        """Return an idempotent user-local SteamCMD bootstrap fragment."""
        return r'''echo "PAL_PROGRESS|10|检测 SteamCMD"
steamcmd_path=""
for candidate in "$(command -v steamcmd 2>/dev/null || true)" \
    "$HOME/.local/share/SteamCMD/steamcmd.sh" \
    "$HOME/.local/share/SteamCMD/steamcmd" \
    "$HOME/steamcmd/steamcmd.sh" \
    "$HOME/steamcmd/steamcmd" \
    "/usr/games/steamcmd" \
    "/opt/steamcmd/steamcmd.sh" \
    "/opt/steamcmd/steamcmd"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        steamcmd_path="$candidate"
        break
    fi
done

if [ -z "$steamcmd_path" ]; then
    echo "PAL_PROGRESS|20|安装 SteamCMD 依赖"
    echo "SteamCMD 未找到，开始自动安装到 $HOME/.local/share/SteamCMD"
    if ! command -v tar >/dev/null 2>&1 || \
        { ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; }; then
        echo "缺少下载或解压工具，尝试自动安装基础依赖"
        if sudo -n true >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
            sudo -n apt-get update
            sudo -n apt-get install -y tar ca-certificates curl
        elif sudo -n true >/dev/null 2>&1 && command -v dnf >/dev/null 2>&1; then
            sudo -n dnf install -y tar ca-certificates curl
        elif sudo -n true >/dev/null 2>&1 && command -v yum >/dev/null 2>&1; then
            sudo -n yum install -y tar ca-certificates curl
        else
            echo "缺少 curl/wget 或 tar，且当前 SSH 用户没有可用的免交互 sudo" >&2
            exit 20
        fi
    fi
    if ! command -v tar >/dev/null 2>&1; then
        echo "自动安装后仍找不到 tar" >&2
        exit 20
    fi
    mkdir -p "$HOME/.local/share/SteamCMD"
    echo "PAL_PROGRESS|30|下载 SteamCMD"
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT
    archive="$tmp_dir/steamcmd_linux.tar.gz"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 15 \
            "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" \
            -o "$archive"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$archive" \
            "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
    else
        echo "缺少 curl 或 wget，无法下载 SteamCMD" >&2
        exit 21
    fi
    test -s "$archive"
    echo "PAL_PROGRESS|35|解压 SteamCMD"
    tar -xzf "$archive" -C "$HOME/.local/share/SteamCMD"
    chmod +x "$HOME/.local/share/SteamCMD/steamcmd.sh" 2>/dev/null || true
    chmod +x "$HOME/.local/share/SteamCMD/steamcmd" 2>/dev/null || true
    steamcmd_path="$HOME/.local/share/SteamCMD/steamcmd.sh"
    if [ ! -x "$steamcmd_path" ]; then
        steamcmd_path="$HOME/.local/share/SteamCMD/steamcmd"
    fi
fi

if [ ! -x "$steamcmd_path" ]; then
    echo "SteamCMD 安装后仍不可执行: $steamcmd_path" >&2
    exit 22
fi
echo "SteamCMD: $steamcmd_path"
echo "PAL_PROGRESS|40|初始化 SteamCMD"
if ! "$steamcmd_path" +login anonymous +quit; then
    echo "SteamCMD 初始化失败，尝试安装 32 位运行库"
    if sudo -n true >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        sudo -n dpkg --add-architecture i386 2>/dev/null || true
        sudo -n apt-get update
        sudo -n apt-get install -y lib32gcc-s1 libc6-i386 ca-certificates || \
            sudo -n apt-get install -y lib32gcc1 libc6-i386 ca-certificates
    elif sudo -n true >/dev/null 2>&1 && command -v dnf >/dev/null 2>&1; then
        sudo -n dnf install -y glibc.i686 libstdc++.i686 ca-certificates
    elif sudo -n true >/dev/null 2>&1 && command -v yum >/dev/null 2>&1; then
        sudo -n yum install -y glibc.i686 libstdc++.i686 ca-certificates
    else
        echo "SteamCMD 初始化失败，且无法自动安装 32 位运行库；请为 SSH 用户配置免交互 sudo" >&2
        exit 23
    fi
    "$steamcmd_path" +login anonymous +quit
fi
'''

    def deployment_script(self, install_dir: str, service_name: str, game_port: int, rest_port: int) -> str:
        # Explicitly non-root game user is supplied by the SSH login.
        if not install_dir.startswith("/"):
            raise ValueError("远程安装目录必须是绝对路径")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", service_name):
            raise ValueError("systemd 服务名包含不安全字符")
        install_q = shlex.quote(install_dir)
        return f"""#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR={install_q}
{self.steamcmd_bootstrap_script()}
mkdir -p "$INSTALL_DIR"
echo "PAL_PROGRESS|45|下载并校验服务端"
"$steamcmd_path" +force_install_dir "$INSTALL_DIR" +login anonymous +app_update 2394010 validate +quit
echo "PAL_PROGRESS|85|服务端文件安装完成"
"""

    def systemd_script(self, install_dir: str, service_name: str, game_port: int, rest_port: int, service_user: str, home_dir: str, service_group: str = "") -> str:
        if not install_dir.startswith("/"):
            raise ValueError("远程安装目录必须是绝对路径")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", service_name):
            raise ValueError("systemd 服务名包含不安全字符")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_user):
            raise ValueError("远程服务用户包含不安全字符")
        service_group = service_group or service_user
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_group):
            raise ValueError("远程服务用户组包含不安全字符")
        if not home_dir.startswith("/"):
            raise ValueError("远程用户目录必须是绝对路径")
        install_q = shlex.quote(install_dir)
        palserver_q = shlex.quote(f"{install_dir}/PalServer.sh")
        return f"""#!/usr/bin/env bash
set -euo pipefail
echo "PAL_PROGRESS|90|配置 systemd 服务"
sudo -n tee /etc/systemd/system/{service_name}.service >/dev/null <<'UNIT'
[Unit]
Description=Palworld Dedicated Server ({service_name})
After=network.target
[Service]
Type=simple
User={service_user}
Group={service_group}
Environment=HOME={shlex.quote(home_dir)}
WorkingDirectory={install_q}
ExecStart={palserver_q} -port={int(game_port)} -RESTAPIEnabled -RESTAPIPort={int(rest_port)} -enable-gamedata-api
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl enable {service_name}
"""

    def update_script(self, install_dir: str) -> str:
        if not install_dir.startswith("/"):
            raise ValueError("远程安装目录必须是绝对路径")
        return f"""#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR={shlex.quote(install_dir)}
{self.steamcmd_bootstrap_script()}
echo "PAL_PROGRESS|45|下载并校验服务端"
"$steamcmd_path" +force_install_dir "$INSTALL_DIR" +login anonymous +app_update 2394010 validate +quit
echo "PAL_PROGRESS|95|服务端更新完成"
"""

    def uninstall_script(self, install_dir: str, service_name: str = "") -> str:
        if not install_dir.startswith("/"):
            raise ValueError("远程安装目录必须是绝对路径")
        if service_name and not re.fullmatch(r"[A-Za-z0-9_.@-]+", service_name):
            raise ValueError("systemd 服务名包含不安全字符")
        service_commands = f'''sudo -n systemctl disable "$SERVICE" >/dev/null 2>&1 || true
sudo -n rm -f -- "/etc/systemd/system/$SERVICE.service"
sudo -n systemctl daemon-reload''' if service_name else 'echo "未检测到 systemd 服务，跳过服务清理"'
        return f'''#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR={shlex.quote(install_dir)}
SERVICE={shlex.quote(service_name)}
echo "PAL_PROGRESS|75|删除 systemd 服务"
{service_commands}
echo "PAL_PROGRESS|88|删除服务端文件"
rm -rf -- "$INSTALL_DIR" || sudo -n rm -rf -- "$INSTALL_DIR"
echo "PAL_PROGRESS|95|服务端卸载完成"
'''


class SSHTunnelManager:
    """Local TCP forwarder backed by the instance's authenticated SSH connection."""

    def __init__(self, client: RemoteHostClient):
        self.client_config = client
        self.ssh = None
        self.listener: socket.socket | None = None
        self.local_port = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self, remote_host: str = "127.0.0.1", remote_port: int = 8212) -> int:
        self.close()
        self.ssh = self.client_config._connect()
        transport = self.ssh.get_transport()
        if not transport or not transport.is_active():
            self.close()
            raise RuntimeError("SSH 隧道建立失败：SSH transport 不可用")
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.5)
        self.local_port = int(self.listener.getsockname()[1])
        self._stop.clear()

        def accept_loop():
            while not self._stop.is_set() and self.listener:
                try:
                    local, address = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    channel = transport.open_channel("direct-tcpip", (remote_host, int(remote_port)), address)
                except Exception:
                    local.close()
                    continue
                worker = threading.Thread(target=self._bridge, args=(local, channel), daemon=True)
                self._threads.append(worker)
                worker.start()

        thread = threading.Thread(target=accept_loop, daemon=True)
        self._threads.append(thread)
        thread.start()
        return self.local_port

    def _bridge(self, local, channel) -> None:
        try:
            try:
                while not self._stop.is_set():
                    ready, _, _ = select.select([local, channel], [], [], 0.5)
                    if local in ready:
                        data = local.recv(32768)
                        if not data: break
                        channel.sendall(data)
                    if channel in ready:
                        data = channel.recv(32768)
                        if not data: break
                        local.sendall(data)
            except (OSError, EOFError):
                pass
        finally:
            try: channel.close()
            except OSError: pass
            try: local.close()
            except OSError: pass

    @property
    def base_url(self) -> str:
        if not self.local_port:
            raise RuntimeError("SSH REST 隧道尚未启动")
        return f"http://127.0.0.1:{self.local_port}"

    def close(self) -> None:
        self._stop.set()
        if self.listener:
            try: self.listener.close()
            except OSError: pass
        self.listener = None
        self.local_port = 0
        if self.ssh:
            self.ssh.close()
        self.ssh = None
        self._threads = []


class ServerConfigBootstrap:
    DEFAULT_REST_PORT = 8212

    @staticmethod
    def generate_admin_password() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def _managed_values(instance: ServerInstance, admin_password: str) -> dict[str, object]:
        profile = instance.remote_profile
        return {
            "ServerName": instance.name or "Palworld Server",
            "ServerDescription": "幻兽帕鲁服务器",
            "ServerPassword": "",
            "AdminPassword": admin_password,
            "PublicPort": int(profile.get("game_port") or instance.game_port or 8211),
            "ServerPlayerMaxNum": 32,
            "RESTAPIEnabled": True,
            "RESTAPIPort": int(profile.get("rest_port") or ServerConfigBootstrap.DEFAULT_REST_PORT),
        }

    @classmethod
    def _apply_defaults(cls, settings: PalWorldSettings, instance: ServerInstance, admin_password: str) -> None:
        settings.values.update(cls._managed_values(instance, admin_password))

    @staticmethod
    def _result(settings: PalWorldSettings, path: str, source: str, created: bool) -> ConfigSyncResult:
        return ConfigSyncResult(dict(settings.values), path, source, created, datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def ensure_local(cls, instance: ServerInstance, admin_password: str) -> ConfigSyncResult:
        install_dir = Path(instance.install_dir)
        target = settings_path(install_dir)
        if target.exists():
            return cls._result(PalWorldSettings.load(target), str(target), "服务器读取", False)
        template = default_settings_path(install_dir)
        if not template.exists():
            raise FileNotFoundError(f"找不到默认配置模板: {template}")
        settings = PalWorldSettings.load(template)
        cls._apply_defaults(settings, instance, admin_password)
        settings.save(target)
        return cls._result(settings, str(target), "自动生成", True)

    @classmethod
    def read_local(cls, instance: ServerInstance) -> ConfigSyncResult:
        target = settings_path(Path(instance.install_dir))
        if not target.exists():
            raise FileNotFoundError(f"找不到服务器配置: {target}")
        return cls._result(PalWorldSettings.load(target), str(target), "服务器读取", False)

    @classmethod
    def update_local(cls, instance: ServerInstance, values: dict[str, object]) -> ConfigSyncResult:
        target = settings_path(Path(instance.install_dir))
        settings = PalWorldSettings.load(target)
        settings.values.update(values)
        settings.save(target)
        return cls._result(settings, str(target), "用户修改", False)

    @classmethod
    def ensure_remote(cls, client: RemoteHostClient, instance: ServerInstance, admin_password: str) -> ConfigSyncResult:
        install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir)
        if not install_dir:
            raise ValueError("远程安装目录尚未确定")
        target = f"{install_dir}/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
        existing = client.read_text(target, missing_ok=True)
        if existing:
            return cls._result(PalWorldSettings.from_text(existing), target, "服务器读取", False)
        template_path = f"{install_dir}/DefaultPalWorldSettings.ini"
        template = client.read_text(template_path, missing_ok=True)
        if not template:
            raise FileNotFoundError(f"远程默认配置模板不存在: {template_path}")
        settings = PalWorldSettings.from_text(template)
        cls._apply_defaults(settings, instance, admin_password)
        client.write_text_atomic(target, settings.render_document(), backup=False)
        return cls._result(settings, target, "自动生成", True)

    @classmethod
    def read_remote(cls, client: RemoteHostClient, instance: ServerInstance) -> ConfigSyncResult:
        install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir)
        target = str(instance.remote_profile.get("config_path") or f"{install_dir}/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini")
        text = client.read_text(target, missing_ok=True)
        if not text:
            raise FileNotFoundError(f"远程服务器配置不存在: {target}")
        return cls._result(PalWorldSettings.from_text(text), target, "服务器读取", False)

    @classmethod
    def update_remote(cls, client: RemoteHostClient, instance: ServerInstance, values: dict[str, object]) -> ConfigSyncResult:
        current = cls.read_remote(client, instance)
        settings = PalWorldSettings.from_text(client.read_text(current.config_path))
        settings.values.update(values)
        client.write_text_atomic(current.config_path, settings.render_document(), backup=True)
        return cls._result(settings, current.config_path, "用户修改", False)


class RemoteServerInspector:
    """Read-only Linux host inspection for remote Palworld instances."""
    def __init__(self, client: RemoteHostClient, on_log: Callable[[str], None] | None = None, preferred_install_dir: str = ""):
        self.client, self.on_log = client, on_log or (lambda _line: None)
        self.preferred_install_dir = preferred_install_dir

    def _run(self, stage: str, command: str, required: bool = True) -> str:
        self.on_log(f"SSH 检测：{stage}")
        code, output, error = self.client.run(command)
        if code and required:
            raise RuntimeError(f"{stage}失败：{error.strip() or output.strip() or '远程命令返回非零状态'}")
        return output.strip()

    @staticmethod
    def _setting(text: str, name: str) -> str:
        match = re.search(rf"(?:^|[,(]){re.escape(name)}=(?:\"([^\"]*)\"|([^,)]*))", text)
        return (match.group(1) if match and match.group(1) is not None else (match.group(2) if match else "")).strip()

    def discover(self) -> dict[str, object]:
        system = self._run("系统信息", "uname -s; uname -m; cat /etc/os-release 2>/dev/null | grep '^PRETTY_NAME=' | head -1 || true")
        if not system.startswith("Linux"):
            raise RuntimeError("远程自动部署仅支持 Linux 主机")
        home_dir = self._run("用户目录", "printf '%s' \"$HOME\"")
        primary_group = self._run("用户主组", "id -gn", required=False)
        disk = self._run("磁盘空间", "df -Pk $HOME | tail -1")
        sudo = self._run("sudo 权限", "sudo -n true >/dev/null 2>&1 && echo yes || echo no", required=False) == "yes"
        download_tool = self._run("下载工具", "command -v curl 2>/dev/null || command -v wget 2>/dev/null || true", required=False)
        tar_available = self._run("解压工具", "command -v tar >/dev/null 2>&1 && echo yes || echo no", required=False) == "yes"
        steamcmd = self._run("SteamCMD", self.client.steamcmd_discovery_command(), required=False).splitlines()
        steamcmd = steamcmd[0] if steamcmd else ""
        if steamcmd.startswith(f"{home_dir}/.local/share/SteamCMD/"):
            steamcmd_source = "用户目录自动安装"
        elif steamcmd:
            steamcmd_source = "系统或已有安装"
        else:
            steamcmd_source = "未安装，可自动安装"
        services = self._run("systemd 服务", "systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '/pal(world)?/ {print $1}' | head -1", required=False)
        service_name = services.removesuffix(".service")
        service_dir = self._run("systemd 工作目录", f"systemctl show {shlex.quote(service_name)} -p WorkingDirectory --value 2>/dev/null || true", required=False) if service_name else ""
        palserver = self._run("Palworld 服务端目录", "find $HOME /opt /srv -type f -name PalServer.sh 2>/dev/null | head -1", required=False)
        found_dir = str(PurePosixPath(palserver).parent) if palserver else ""
        candidate = service_dir or found_dir or self.preferred_install_dir or "$HOME/palworld-server"
        install_dir = self.client.resolve_path(candidate, require_writable_parent=not bool(service_dir or found_dir))
        self.client.validate_install_target(install_dir)
        marker_code, _, _ = self.client.run(f"test -f {shlex.quote(f'{install_dir}/PalServer.sh')}")
        installed = marker_code == 0
        palserver = f"{install_dir}/PalServer.sh" if installed else ""
        service_state = self._run("服务状态", f"systemctl is-active {shlex.quote(service_name)} 2>/dev/null || true", required=False) if service_name else "not_found"
        config_path = f"{install_dir}/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini" if install_dir else ""
        config = self._run("服务器配置", f"test -f {shlex.quote(config_path)} && cat {shlex.quote(config_path)} || true", required=False) if config_path else ""
        game_port = self._setting(config, "PublicPort") or self._setting(config, "Port")
        rest_enabled = self._setting(config, "RESTAPIEnabled").lower() == "true"
        rest_port = self._setting(config, "RESTAPIPort")
        saves = f"{install_dir}/Pal/Saved" if install_dir else ""
        logs = f"{saves}/Logs" if saves else ""
        return {"os": system.splitlines()[-1].replace('PRETTY_NAME=', '').strip('"'), "architecture": system.splitlines()[1] if len(system.splitlines()) > 1 else "", "home_dir": home_dir, "primary_group": primary_group, "disk": disk, "sudo": sudo, "download_tool": download_tool, "tar_available": tar_available, "steamcmd_path": steamcmd, "steamcmd_source": steamcmd_source, "steamcmd_available": bool(steamcmd), "steamcmd_installable": bool(steamcmd or sudo or (download_tool and tar_available)), "palserver_path": palserver, "install_dir": install_dir, "service_name": service_name, "service_state": service_state, "config_path": config_path if config else "", "save_dir": saves if config else "", "log_dir": logs if config else "", "game_port": int(game_port) if game_port.isdigit() else 8211, "rest_enabled": rest_enabled, "rest_port": int(rest_port) if rest_port.isdigit() else 8212, "rest_url": f"http://{self.client.host}:{rest_port}" if rest_enabled and rest_port else "", "installed": installed}


class RemoteServerLifecycle:
    def __init__(self, instance: ServerInstance, client: RemoteHostClient, on_log: Callable[[str], None] | None = None, on_progress: Callable[[TaskProgress], None] | None = None):
        self.instance, self.client = instance, client
        self.on_log = on_log or (lambda _line: None)
        self.on_progress = on_progress or (lambda _progress: None)

    @staticmethod
    def parse_progress(line: str) -> TaskProgress | None:
        marker = re.fullmatch(r"PAL_PROGRESS\|(-?\d+)\|(.+)", line.strip())
        if marker:
            message = marker.group(2).strip()
            return TaskProgress(int(marker.group(1)), message, message)
        return SteamCmdInstaller.parse_progress(line, 40, 85)

    def _service(self) -> str:
        return str(self.instance.remote_profile.get("service_name") or "palworld")

    def _install_dir(self) -> str:
        profile = self.instance.remote_profile
        candidate = str(profile.get("install_dir") or self.instance.install_dir or "$HOME/palworld-server")
        install_dir = self.client.resolve_path(candidate, require_writable_parent=not bool(profile.get("installed")))
        self.client.validate_install_target(install_dir)
        profile["install_dir"] = install_dir
        profile["palserver_path"] = f"{install_dir}/PalServer.sh"
        self.instance.install_dir = install_dir
        return install_dir

    def _run(self, label: str, command: str) -> None:
        self.on_log(f"SSH：{label}")
        emitted = False

        def handle_output(line: str):
            nonlocal emitted
            emitted = True
            parsed = self.parse_progress(line)
            if parsed:
                self.on_progress(parsed)
            if not line.startswith("PAL_PROGRESS|"):
                self.on_log(line)

        code, output, error = self.client.run_stream(command, handle_output)
        if not emitted:
            for line in (output + "\n" + error).splitlines():
                handle_output(line)
        if code:
            raise RuntimeError(f"{label}失败：{error.strip() or output.strip()}")

    def _run_script(self, label: str, script: str) -> None:
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        self._run(label, f"printf %s {shlex.quote(encoded)} | base64 -d | bash")

    def install(self) -> None:
        self.on_progress(TaskProgress(5, "检测环境", "准备远程安装"))
        profile = self.instance.remote_profile
        install_dir = self._install_dir()
        service = str(profile.get("service_name") or "palworld")
        script = self.client.deployment_script(install_dir, service, int(profile.get("game_port", self.instance.game_port)), int(profile.get("rest_port", 8212)))
        self._run_script("上传并执行部署脚本", script)

    def configure_service(self) -> None:
        profile = self.instance.remote_profile
        install_dir = self._install_dir()
        service = str(profile.get("service_name") or "palworld")
        home_dir = str(profile.get("home_dir") or self.client.resolve_path("~"))
        script = self.client.systemd_script(install_dir, service, int(profile.get("game_port", self.instance.game_port)), int(profile.get("rest_port", 8212)), self.instance.remote_username, home_dir, str(profile.get("primary_group") or self.instance.remote_username))
        self._run_script("配置 systemd 服务", script)

    def allow_game_firewall(self) -> None:
        game_port = int(self.instance.remote_profile.get("game_port") or self.instance.game_port)
        code, output, error = self.client.run("command -v ufw >/dev/null 2>&1 && sudo -n ufw status | head -1 || true")
        if code:
            raise RuntimeError(error.strip() or output.strip() or "无法检查 UFW")
        if "active" in output.lower():
            self._run("放行游戏 UDP 端口", f"sudo -n ufw allow {game_port}/udp")

    def wait_for_game_listener(self, timeout: int = 35) -> None:
        game_port = int(self.instance.remote_profile.get("game_port") or self.instance.game_port)
        deadline = time.time() + timeout
        last_log = ""
        while time.time() < deadline:
            code, output, _ = self.client.run(f"ss -lun 2>/dev/null | awk '{{print $4}}' | grep -Eq ':{game_port}$'")
            if code == 0:
                return
            _, last_log, _ = self.client.run(f"journalctl -u {shlex.quote(self._service())} -n 5 --no-pager 2>/dev/null || true")
            if "Refusing to run with the root privileges" in last_log:
                raise RuntimeError("PalServer 拒绝以 root 身份运行，请重新生成 systemd 服务")
            time.sleep(1)
        raise RuntimeError(f"服务已启动但 UDP {game_port} 未监听。最近日志：{last_log.strip()}")

    def repair_runtime(self) -> None:
        self.configure_service()
        self.allow_game_firewall()
        self.restart()
        self.wait_for_game_listener()

    def update(self, restart: bool = True) -> None:
        profile = self.instance.remote_profile
        if not profile.get("installed"):
            return self.install()
        service, install_dir = self._service(), self._install_dir()
        self.client.validate_palworld_install(install_dir)
        self.on_progress(TaskProgress(5, "停止服务器", "正在停止现有服务"))
        self._run("停止服务", f"sudo -n systemctl stop {shlex.quote(service)}")
        try:
            self._run_script("SteamCMD 更新", self.client.update_script(install_dir))
        finally:
            if restart:
                self.on_progress(TaskProgress(95, "启动服务器", "正在启动更新后的服务"))
                self._run("启动服务", f"sudo -n systemctl start {shlex.quote(service)}")

    def start(self): self._run("启动服务", f"sudo -n systemctl start {shlex.quote(self._service())}")
    def stop(self): self._run("停止服务", f"sudo -n systemctl stop {shlex.quote(self._service())}")
    def restart(self): self._run("重启服务", f"sudo -n systemctl restart {shlex.quote(self._service())}")
    def status(self) -> str:
        _, output, _ = self.client.run(f"systemctl is-active {shlex.quote(self._service())} 2>/dev/null || true")
        return output.strip() or "unknown"

    def uninstall(self, backup_destination: Path) -> UninstallResult:
        install_dir = self._install_dir()
        self.client.validate_palworld_install(install_dir)
        service = str(self.instance.remote_profile.get("service_name") or "")
        if service and not re.fullmatch(r"[A-Za-z0-9_.@-]+", service):
            raise ValueError("systemd 服务名包含不安全字符")
        was_running = False
        if service:
            _, state, _ = self.client.run(f"systemctl is-active {shlex.quote(service)} 2>/dev/null || true")
            was_running = state.strip() == "active"
            self.on_progress(TaskProgress(15, "停止服务器", "正在停止远程 Palworld 服务", True))
            code, output, error = self.client.run(f"sudo -n systemctl stop {shlex.quote(service)}")
            if code:
                raise RuntimeError(f"停止远程服务失败：{error.strip() or output.strip()}")
        else:
            self.on_progress(TaskProgress(15, "停止服务器", "未检测到 systemd 服务，继续备份"))
        self.on_progress(TaskProgress(35, "创建备份", "正在打包并下载远程存档", True))
        try:
            backup = BackupService().create_remote(self.client, self.instance, backup_destination, install_dir)
        except Exception:
            if service and was_running:
                code, output, error = self.client.run(f"sudo -n systemctl start {shlex.quote(service)}")
                if code:
                    self.on_log(f"备份失败后恢复服务也失败：{error.strip() or output.strip()}")
                else:
                    self.on_log("备份失败，原远程服务已重新启动")
            raise
        self.on_progress(TaskProgress(70, "校验备份", "远程存档备份已下载并校验" if backup else "未发现存档，跳过备份"))
        self._run_script("卸载远程服务端", self.client.uninstall_script(install_dir, service))
        return UninstallResult(install_dir, str(backup) if backup else "", bool(backup))


class ServerDiagnostics:
    @staticmethod
    def _number(payload: dict, *keys, default=0.0):
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                try: return float(payload[key])
                except (TypeError, ValueError): pass
        return default

    @classmethod
    def collect_remote(cls, client: RemoteHostClient, instance: ServerInstance, rest_client: PalworldRestClient | None = None) -> ServerHealthSnapshot:
        profile = instance.remote_profile
        service = str(profile.get("service_name") or "palworld")
        game_port = int(profile.get("game_port") or instance.game_port or 8211)
        rest_port = int(profile.get("rest_port") or 8212)
        code, service_output, service_error = client.run(f"systemctl is-active {shlex.quote(service)} 2>/dev/null || true; systemctl show {shlex.quote(service)} -p MainPID --value 2>/dev/null || true")
        lines = [line.strip() for line in service_output.splitlines() if line.strip()]
        service_state = lines[0] if lines else "unknown"
        pid = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else 0
        process_user, cpu, memory_percent, memory_mb = "", 0.0, 0.0, 0.0
        if pid:
            _, process_output, _ = client.run(f"ps -p {pid} -o user=,%cpu=,%mem=,rss= 2>/dev/null || true")
            parts = process_output.split()
            if len(parts) >= 4:
                process_user = parts[0]
                try: cpu, memory_percent, memory_mb = float(parts[1]), float(parts[2]), round(float(parts[3]) / 1024, 1)
                except ValueError: pass
        _, listeners, _ = client.run(f"ss -lun 2>/dev/null | awk '{{print $4}}'; echo __TCP__; ss -ltn 2>/dev/null | awk '{{print $4}}'")
        udp_text, _, tcp_text = listeners.partition("__TCP__")
        game_listening = bool(re.search(rf":{game_port}(?:\s|$)", udp_text))
        rest_listening = bool(re.search(rf":{rest_port}(?:\s|$)", tcp_text))
        _, disk, _ = client.run('df -h "$HOME" 2>/dev/null | tail -1')
        _, ufw, _ = client.run("sudo -n ufw status 2>/dev/null || true")
        ufw_active = "status: active" in ufw.lower()
        ufw_allowed = bool(re.search(rf"^{game_port}/udp\s+ALLOW", ufw, re.MULTILINE | re.IGNORECASE)) if ufw_active else True
        _, recent_log, _ = client.run(f"journalctl -u {shlex.quote(service)} -n 12 --no-pager 2>/dev/null || true")
        rest_ok, info, metrics = False, {}, {}
        if rest_client:
            try:
                info = rest_client.health() or {}
                metrics = rest_client.metrics() or {}
                rest_ok = True
            except Exception as exc:
                recent_log = (recent_log + f"\nREST 检查失败：{exc}").strip()
        issues = []
        if code and service_error.strip(): issues.append(f"systemd 检查失败：{service_error.strip()}")
        if service_state != "active": issues.append(f"systemd 状态为 {service_state}")
        if pid <= 0: issues.append("未检测到 PalServer 主进程")
        if process_user == "root" or "Refusing to run with the root privileges" in recent_log: issues.append("PalServer 正在或曾尝试以 root 身份运行")
        if not game_listening: issues.append(f"游戏 UDP {game_port} 未监听")
        if not rest_listening: issues.append(f"REST TCP {rest_port} 未监听")
        if ufw_active and not ufw_allowed: issues.append(f"UFW 未放行 {game_port}/udp")
        if rest_client and not rest_ok: issues.append("REST API 通过 SSH 隧道不可用")
        return ServerHealthSnapshot(
            healthy=service_state == "active" and pid > 0 and process_user not in {"", "root"} and game_listening and (not rest_client or rest_ok),
            service_state=service_state, pid=pid, process_user=process_user, cpu_percent=cpu, memory_percent=memory_percent, memory_mb=memory_mb,
            disk=disk.strip(), uptime_seconds=int(cls._number(metrics, "uptime", "uptime_seconds")), fps=cls._number(metrics, "serverfps", "server_fps", "fps"),
            frame_time_ms=cls._number(metrics, "serverframetime", "server_frame_time", "frame_time"), player_count=int(cls._number(metrics, "currentplayernum", "current_players", "player_count", default=cls._number(info, "currentplayernum", "current_players"))),
            player_limit=int(cls._number(metrics, "maxplayernum", "max_players", default=cls._number(info, "maxplayernum", "max_players"))), game_days=int(cls._number(metrics, "days", "game_days")),
            version=str(info.get("version") or ""), world_guid=str(info.get("worldguid") or info.get("world_guid") or ""),
            game_endpoint=EndpointStatus("游戏", "UDP", game_port, game_listening, None, f"{instance.host}:{game_port}"),
            rest_endpoint=EndpointStatus("REST", "TCP/SSH", rest_port, rest_listening, rest_ok if rest_client else None, "仅通过 SSH 隧道访问"),
            ssh_ok=True, rest_ok=rest_ok, ufw_active=ufw_active, ufw_game_allowed=ufw_allowed, issues=tuple(issues), recent_log=recent_log.strip(), checked_at=datetime.now().isoformat(timespec="seconds"),
        )


class PlayerAdminService:
    def __init__(self, client: PalworldRestClient):
        self.client = client

    def list_players(self) -> list[PlayerRecord]:
        return self.client.player_records(self.client.players())

    def kick(self, user_id: str, message: str = ""):
        return self.client.kick(user_id, message)

    def ban(self, user_id: str, message: str = ""):
        return self.client.ban(user_id, message)

    def unban(self, user_id: str):
        return self.client.unban(user_id)


class GuildSnapshotService:
    def __init__(self, client: PalworldRestClient):
        self.client = client

    def list_guilds(self, players: list[PlayerRecord] | None = None) -> list[GuildSummary]:
        return self.client.guild_summaries(self.client.game_data(), players)


class FirewallService:
    @staticmethod
    def add_windows_udp_rule(name: str, port: int) -> None:
        command = ["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow", "protocol=UDP", f"localport={port}"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout or "创建防火墙规则失败，请以管理员身份运行")


class WindowsShortcutService:
    @staticmethod
    def create_desktop_shortcut(name: str = "幻兽帕鲁服务器控制台") -> Path:
        if os.name != "nt":
            raise RuntimeError("桌面快捷方式仅支持 Windows")
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        desktop.mkdir(parents=True, exist_ok=True)
        shortcut = desktop / f"{name}.lnk"
        target = Path(sys.executable).resolve()
        project_root = Path(__file__).resolve().parent.parent
        script = project_root / "run.py"
        if not script.exists():
            raise FileNotFoundError(f"找不到启动文件: {script}")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("找不到 PowerShell，无法创建快捷方式")
        command = (
            "$s=New-Object -ComObject WScript.Shell;"
            f"$l=$s.CreateShortcut('{str(shortcut).replace(chr(39), chr(39)*2)}');"
            f"$l.TargetPath='{str(target).replace(chr(39), chr(39)*2)}';"
            f"$l.Arguments='\"{str(script).replace(chr(39), chr(39)*2)}\"';"
            f"$l.WorkingDirectory='{str(project_root).replace(chr(39), chr(39)*2)}';"
            "$l.Description='幻兽帕鲁服务器控制台';$l.Save()"
        )
        result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout or "创建桌面快捷方式失败")
        return shortcut


def _validate_local_palworld_install(install_dir: Path) -> Path:
    if not str(install_dir).strip():
        raise ValueError("本机安装目录为空")
    if install_dir.is_symlink():
        raise ValueError(f"拒绝卸载符号链接目录: {install_dir}")
    resolved = install_dir.resolve(strict=True)
    project_root = Path(__file__).resolve().parent.parent
    dangerous = {Path(resolved.anchor), Path.home().resolve(), project_root}
    if resolved in dangerous or len(resolved.parts) < 3:
        raise ValueError(f"拒绝操作危险本机目录: {resolved}")
    if not (resolved / "PalServer.exe").is_file():
        raise ValueError(f"目录不是可信的 Palworld 安装目录: {resolved}")
    return resolved


class LocalServerLifecycle:
    def __init__(self, instance: ServerInstance, on_log: Callable[[str], None] | None = None):
        self.instance = instance
        self.on_log = on_log or (lambda _line: None)
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    @property
    def executable(self) -> Path:
        return Path(self.instance.install_dir) / "PalServer.exe"

    def start(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                return
            if not self.executable.exists():
                raise FileNotFoundError(f"找不到服务器程序: {self.executable}")
            rest_port = int(self.instance.remote_profile.get("rest_port") or 8212)
            command = [str(self.executable), f"-port={int(self.instance.game_port)}", "-RESTAPIEnabled", f"-RESTAPIPort={rest_port}", "-enable-gamedata-api", "-useperfthreads", "-NoAsyncLoadingThread", "-UseMultithreadForDS"]
            self.process = subprocess.Popen(command, cwd=self.executable.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            self.on_log(line.rstrip())

    def stop(self, timeout: int = 20) -> None:
        with self._lock:
            if not self.process or self.process.poll() is not None:
                return
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def restart(self) -> None:
        self.stop()
        self.start()

    def status(self) -> str:
        return "running" if self.process and self.process.poll() is None else "stopped"

    def uninstall(self, backup_destination: Path, on_progress: Callable[[TaskProgress], None] | None = None) -> UninstallResult:
        progress = on_progress or (lambda _progress: None)
        install_dir = _validate_local_palworld_install(Path(self.instance.install_dir))
        was_running = self.status() == "running"
        progress(TaskProgress(15, "停止服务器", "正在停止本机 Palworld 服务", True))
        self.stop()
        progress(TaskProgress(35, "创建备份", "正在备份本机存档", True))
        try:
            backup = BackupService().create_local_if_present(self.instance, backup_destination)
        except Exception:
            if was_running:
                try:
                    self.start()
                    self.on_log("备份失败，原本机服务已重新启动")
                except Exception as exc:
                    self.on_log(f"备份失败后恢复本机服务也失败：{exc}")
            raise
        progress(TaskProgress(70, "校验备份", "本机存档备份已校验" if backup else "未发现存档，跳过备份"))
        shutil.rmtree(install_dir)
        progress(TaskProgress(95, "删除服务端文件", "本机服务端已卸载"))
        return UninstallResult(str(install_dir), str(backup) if backup else "", bool(backup))


class PalworldRestClient:
    def __init__(self, base_url: str, password: str, username: str = "admin", timeout: float = 8):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.username = username
        self.timeout = timeout

    def request(self, method: str, endpoint: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(self.base_url + endpoint, data=data, method=method, headers={"Content-Type": "application/json", "Authorization": f"Basic {token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"REST 请求失败 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"REST 连接失败: {exc.reason}") from exc

    def health(self): return self.request("GET", "/v1/api/info")
    def players(self): return self.request("GET", "/v1/api/players")
    def save(self): return self.request("POST", "/v1/api/save")
    def announce(self, message: str): return self.request("POST", "/v1/api/announce", {"message": message})
    def shutdown(self, waittime: int = 30): return self.request("POST", "/v1/api/shutdown", {"waittime": waittime})
    def stop(self): return self.request("POST", "/v1/api/stop")
    def metrics(self): return self.request("GET", "/v1/api/metrics")
    def settings(self): return self.request("GET", "/v1/api/settings")
    def game_data(self): return self.request("GET", "/v1/api/game-data")
    def kick(self, userid: str, message: str = ""): return self.request("POST", "/v1/api/kick", {"userid": userid, "message": message})
    def ban(self, userid: str, message: str = ""): return self.request("POST", "/v1/api/ban", {"userid": userid, "message": message})
    def unban(self, userid: str): return self.request("POST", "/v1/api/unban", {"userid": userid})

    @staticmethod
    def player_records(payload: object) -> list[PlayerRecord]:
        rows = payload.get("players", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
        result = []
        for row in rows:
            if not isinstance(row, dict): continue
            location = row.get("location") or {}
            result.append(PlayerRecord(
                name=str(row.get("name") or row.get("playerName") or ""),
                account_name=str(row.get("accountName") or row.get("account_name") or ""),
                user_id=str(row.get("userId") or row.get("userid") or ""),
                player_uid=str(row.get("playerUId") or row.get("playerUid") or ""),
                level=int(row.get("level") or 0), ping=float(row.get("ping") or 0), ip=str(row.get("ip") or ""),
                location_x=float(location.get("x") or row.get("location_x") or 0), location_y=float(location.get("y") or row.get("location_y") or 0),
                building_count=int(row.get("buildingCount") or row.get("building_count") or 0), guild_id=str(row.get("guildId") or row.get("GuildID") or ""),
            ))
        return result

    @staticmethod
    def guild_summaries(payload: object, players: list[PlayerRecord] | None = None) -> list[GuildSummary]:
        players = players or []
        found: dict[str, dict[str, object]] = {}

        def visit(value):
            if isinstance(value, dict):
                guild_id = value.get("GuildID") or value.get("guildId") or value.get("guild_id")
                guild_name = value.get("GuildName") or value.get("guildName") or value.get("guild_name")
                if guild_id:
                    item = found.setdefault(str(guild_id), {"name": str(guild_name or guild_id), "bases": 0, "pals": 0})
                    if guild_name: item["name"] = str(guild_name)
                    type_name = str(value.get("Type") or value.get("type") or value.get("className") or "").lower()
                    if "basecamp" in type_name: item["bases"] = int(item["bases"]) + 1
                    if "pal" in type_name: item["pals"] = int(item["pals"]) + 1
                for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)

        visit(payload)
        result = []
        for guild_id, data in found.items():
            members = [p for p in players if p.guild_id == guild_id]
            result.append(GuildSummary(guild_id, str(data["name"]), len(members), len(members), round(sum(p.level for p in members) / len(members), 1) if members else 0.0, int(data["bases"]), int(data["pals"]), tuple(p.name for p in members)))
        return sorted(result, key=lambda item: item.name.lower())


class BackupService:
    @staticmethod
    def validate_zip(archive: Path) -> None:
        if not archive.exists() or archive.stat().st_size == 0 or not zipfile.is_zipfile(archive):
            raise RuntimeError(f"备份校验失败: {archive}")
        with zipfile.ZipFile(archive) as zf:
            if zf.testzip() is not None:
                raise RuntimeError(f"备份文件损坏: {archive}")

    def create_local(self, instance: ServerInstance, destination: Path) -> Path:
        source = Path(instance.install_dir) / "Pal" / "Saved"
        if not source.exists():
            raise FileNotFoundError(f"找不到存档目录: {source}")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{instance.id}-{datetime.now():%Y%m%d-%H%M%S}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in source.rglob("*"):
                if file.is_file():
                    archive.write(file, file.relative_to(source))
        self.validate_zip(target)
        return target

    def create_local_if_present(self, instance: ServerInstance, destination: Path) -> Path | None:
        source = Path(instance.install_dir) / "Pal" / "Saved"
        if not source.exists():
            return None
        return self.create_local(instance, destination)

    def create_remote(self, client: RemoteHostClient, instance: ServerInstance, destination: Path, install_dir: str) -> Path | None:
        saved_dir = f"{install_dir}/Pal/Saved"
        check = f"if [ ! -e {shlex.quote(saved_dir)} ]; then echo MISSING; elif [ -d {shlex.quote(saved_dir)} ] && [ -r {shlex.quote(saved_dir)} ]; then echo READY; else echo UNREADABLE; exit 2; fi"
        code, output, error = client.run(check)
        if output.strip() == "MISSING":
            return None
        if code or output.strip() != "READY":
            raise RuntimeError(f"无法读取远程存档目录：{error.strip() or output.strip()}")
        destination.mkdir(parents=True, exist_ok=True)
        remote_archive = f"/tmp/palworld-{instance.id}-{uuid.uuid4().hex}.tar.gz"
        local_archive = destination / f"{instance.id}-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
        try:
            code, output, error = client.run(f"tar -C {shlex.quote(f'{install_dir}/Pal')} -czf {shlex.quote(remote_archive)} Saved")
            if code:
                raise RuntimeError(f"远程存档打包失败：{error.strip() or output.strip()}")
            client.download_file(remote_archive, local_archive)
            if not local_archive.exists() or local_archive.stat().st_size == 0 or not tarfile.is_tarfile(local_archive):
                raise RuntimeError("远程备份下载后校验失败")
            with tarfile.open(local_archive, "r:gz") as archive:
                members = archive.getmembers()
                if not members or any(PurePosixPath(member.name).parts[0] != "Saved" for member in members if member.name):
                    raise RuntimeError("远程备份结构无效")
            return local_archive
        except Exception:
            local_archive.unlink(missing_ok=True)
            raise
        finally:
            client.run(f"rm -f -- {shlex.quote(remote_archive)}")

    def restore_local(self, instance: ServerInstance, archive: Path) -> None:
        target = Path(instance.install_dir) / "Pal" / "Saved"
        if not archive.exists() or not zipfile.is_zipfile(archive):
            raise ValueError("备份文件无效")
        staging = target.with_name(target.name + ".restore")
        if staging.exists(): shutil.rmtree(staging)
        with zipfile.ZipFile(archive) as zf: zf.extractall(staging)
        if target.exists(): shutil.move(str(target), str(target.with_name(target.name + ".before-restore")))
        staging.rename(target)


class NetworkDiagnostics:
    @staticmethod
    def port_available(host: str, port: int, timeout: float = 1.5) -> bool:
        with socket.socket() as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0

    @staticmethod
    def local_udp_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.bind(("0.0.0.0", port))
                return False
            except OSError:
                return True
