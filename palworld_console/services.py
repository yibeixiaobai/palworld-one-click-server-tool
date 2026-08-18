from __future__ import annotations

import json
import base64
import html
import ntpath
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
from .models import ConfigSyncResult, EndpointStatus, GuildSummary, LocalSteamCmdState, PlayerRecord, PrerequisiteStatus, RemotePlatformProfile, RemoteVolume, ServerHealthSnapshot, ServerInstance, TaskProgress, UninstallResult


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
    """Runs a prepared SteamCMD executable and streams install progress."""
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


class LocalSteamCmdManager:
    DOWNLOAD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"

    @staticmethod
    def validate_install_dir(install_dir: Path) -> Path:
        if not str(install_dir).strip():
            raise ValueError("请先选择本机服务端安装目录")
        resolved = install_dir.expanduser().resolve()
        project_root = Path(__file__).resolve().parent.parent
        dangerous = {Path(resolved.anchor), Path.home().resolve(), project_root.resolve(), Path(os.environ.get("WINDIR", "C:/Windows")).resolve()}
        if resolved in dangerous:
            raise ValueError(f"拒绝使用危险安装目录: {resolved}")
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / f".palworld-write-{uuid.uuid4().hex}"
        try:
            probe.write_text("ok", encoding="ascii")
        except OSError as exc:
            raise PermissionError(f"安装目录不可写: {resolved}") from exc
        finally:
            probe.unlink(missing_ok=True)
        if shutil.disk_usage(resolved).free < 6 * 1024**3:
            raise RuntimeError("安装目录可用空间不足 6 GB")
        return resolved

    @staticmethod
    def tool_root(install_dir: Path) -> Path:
        return install_dir / "_tools" / "steamcmd"

    def prepare(self, install_dir: Path, on_log: Callable[[str], None] | None = None, on_progress: Callable[[TaskProgress], None] | None = None) -> LocalSteamCmdState:
        log = on_log or (lambda _line: None); progress = on_progress or (lambda _progress: None)
        install_dir = self.validate_install_dir(install_dir); root = self.tool_root(install_dir); executable = root / "steamcmd.exe"
        downloaded = False; repaired = False
        progress(TaskProgress(2, "检测 SteamCMD", f"工具目录：{root}"))
        if not executable.is_file() or executable.stat().st_size < 100_000:
            repaired = executable.exists(); root.mkdir(parents=True, exist_ok=True); archive = root / "steamcmd.zip.download"
            progress(TaskProgress(5, "下载 SteamCMD", "正在从 Steam 官方地址下载", True))
            try:
                request = urllib.request.Request(self.DOWNLOAD_URL, headers={"User-Agent": "PalworldConsole/0.3"})
                with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
                    total = int(response.headers.get("Content-Length") or 0); received = 0
                    while True:
                        block = response.read(1024 * 256)
                        if not block: break
                        output.write(block); received += len(block)
                        if total: progress(TaskProgress(5 + round(received / total * 8), "下载 SteamCMD", f"已下载 {received / 1024 / 1024:.1f} MB"))
                with zipfile.ZipFile(archive) as bundle:
                    root_resolved = root.resolve()
                    for entry in bundle.infolist():
                        target = (root / entry.filename).resolve()
                        if root_resolved not in target.parents and target != root_resolved:
                            raise RuntimeError("SteamCMD ZIP 包含不安全路径")
                    bundle.extractall(root)
                downloaded = True; log(f"SteamCMD 已下载到 {root}")
            except Exception:
                executable.unlink(missing_ok=True)
                raise
            finally:
                archive.unlink(missing_ok=True)
        progress(TaskProgress(14, "初始化 SteamCMD", "正在执行 SteamCMD 自更新", True))
        process = subprocess.run([str(executable), "+quit"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        for line in (process.stdout + "\n" + process.stderr).splitlines():
            if line.strip(): log(line.strip())
        if process.returncode != 0 or not executable.is_file():
            raise RuntimeError(f"SteamCMD 初始化失败，退出码 {process.returncode}")
        progress(TaskProgress(18, "初始化 SteamCMD", "SteamCMD 已就绪"))
        return LocalSteamCmdState(str(root), str(executable), True, downloaded, repaired, "SteamCMD 自动管理")


class RemoteHostClient:
    """SSH/SFTP transport shared by Linux and Windows remote hosts."""
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

    @staticmethod
    def _sftp_path(remote_path: str) -> str:
        normalized = str(remote_path).replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", normalized):
            return "/" + normalized
        return normalized

    def run(self, command: str) -> tuple[int, str, str]:
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(command)
            output, errors = stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")
            return stdout.channel.recv_exit_status(), output, errors
        finally:
            client.close()

    @staticmethod
    def powershell_command(script: str) -> str:
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return f"powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"

    def run_powershell(self, script: str) -> tuple[int, str, str]:
        return self.run(self.powershell_command(script))

    def run_powershell_stream(self, script: str, on_output: Callable[[str], None]) -> tuple[int, str, str]:
        return self.run_stream(self.powershell_command(script), on_output)

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

    @staticmethod
    def windows_openssh_bootstrap_script() -> str:
        return """$ErrorActionPreference = 'Stop'
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
if (-not (Get-NetFirewallRule -Name OpenSSH-Server-In-TCP -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name OpenSSH-Server-In-TCP -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
}
Get-Service sshd
"""

    def upload_text(self, remote_path: str, content: str) -> None:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            with sftp.file(self._sftp_path(remote_path), "w") as stream:
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
                sftp.get(self._sftp_path(remote_path), str(local_path))
            finally:
                sftp.close()
        finally:
            client.close()

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            try:
                sftp.put(str(local_path), self._sftp_path(remote_path))
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
                with sftp.file(self._sftp_path(remote_path), "r") as stream:
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

    def write_text_atomic_windows(self, remote_path: str, content: str, backup: bool = True) -> str:
        """Upload through SFTP, then perform the replace on-host with PowerShell."""
        normalized = ntpath.normpath(remote_path)
        if not re.fullmatch(r"[A-Za-z]:\\.+", normalized):
            raise ValueError(f"Windows 远程路径必须包含盘符: {remote_path}")
        temporary = f"{normalized}.tmp-{uuid.uuid4().hex}"
        backup_path = f"{normalized}.{datetime.now():%Y%m%d-%H%M%S}.bak" if backup else ""
        directory = ntpath.dirname(normalized)
        self.run_powershell(f"New-Item -ItemType Directory -Force -LiteralPath {self._ps_literal(directory)} | Out-Null")
        client = self._connect()
        try:
            sftp = client.open_sftp()
            try:
                with sftp.file(self._sftp_path(temporary), "w") as stream:
                    stream.write(content)
            finally:
                sftp.close()
        finally:
            client.close()
        script = f"""
$ErrorActionPreference = 'Stop'
$target = {self._ps_literal(normalized)}
$temporary = {self._ps_literal(temporary)}
$backup = {self._ps_literal(backup_path)}
try {{
  if ({'$backup' if backup else '$false'} -and (Test-Path -LiteralPath $target)) {{ Copy-Item -LiteralPath $target -Destination $backup -Force }}
  Move-Item -LiteralPath $temporary -Destination $target -Force
}} catch {{
  Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
  throw
}}
"""
        code, output, error = self.run_powershell(script)
        if code:
            raise RuntimeError(error.strip() or output.strip() or "Windows 远程配置原子替换失败")
        return backup_path if backup else ""

    @staticmethod
    def _ps_literal(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"
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
        windows = str(instance.remote_profile.get("platform") or "linux").lower() == "windows" or bool(instance.remote_profile.get("wine_path"))
        separator = "\\" if str(instance.remote_profile.get("platform")).lower() == "windows" else "/"
        config_dir = "WindowsServer" if windows else "LinuxServer"
        target = separator.join((install_dir.rstrip("/\\"), "Pal", "Saved", "Config", config_dir, "PalWorldSettings.ini"))
        existing = client.read_text(target, missing_ok=True)
        if existing:
            return cls._result(PalWorldSettings.from_text(existing), target, "服务器读取", False)
        template_path = separator.join((install_dir.rstrip("/\\"), "DefaultPalWorldSettings.ini"))
        template = client.read_text(template_path, missing_ok=True)
        if not template:
            raise FileNotFoundError(f"远程默认配置模板不存在: {template_path}")
        settings = PalWorldSettings.from_text(template)
        cls._apply_defaults(settings, instance, admin_password)
        if str(instance.remote_profile.get("platform")).lower() == "windows":
            client.write_text_atomic_windows(target, settings.render_document(), backup=False)
        else:
            client.write_text_atomic(target, settings.render_document(), backup=False)
        return cls._result(settings, target, "自动生成", True)

    @classmethod
    def read_remote(cls, client: RemoteHostClient, instance: ServerInstance) -> ConfigSyncResult:
        install_dir = str(instance.remote_profile.get("install_dir") or instance.install_dir)
        windows = str(instance.remote_profile.get("platform") or "linux").lower() == "windows" or bool(instance.remote_profile.get("wine_path"))
        separator = "\\" if str(instance.remote_profile.get("platform")).lower() == "windows" else "/"
        config_dir = "WindowsServer" if windows else "LinuxServer"
        fallback = separator.join((install_dir.rstrip("/\\"), "Pal", "Saved", "Config", config_dir, "PalWorldSettings.ini"))
        target = str(instance.remote_profile.get("config_path") or fallback)
        text = client.read_text(target, missing_ok=True)
        if not text:
            raise FileNotFoundError(f"远程服务器配置不存在: {target}")
        return cls._result(PalWorldSettings.from_text(text), target, "服务器读取", False)

    @classmethod
    def update_remote(cls, client: RemoteHostClient, instance: ServerInstance, values: dict[str, object]) -> ConfigSyncResult:
        current = cls.read_remote(client, instance)
        settings = PalWorldSettings.from_text(client.read_text(current.config_path))
        settings.values.update(values)
        if str(instance.remote_profile.get("platform")).lower() == "windows":
            client.write_text_atomic_windows(current.config_path, settings.render_document(), backup=True)
        else:
            client.write_text_atomic(current.config_path, settings.render_document(), backup=True)
        return cls._result(settings, current.config_path, "用户修改", False)


class LinuxRemoteServerInspector:
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
        volume_parts = disk.split()
        free_bytes = int(volume_parts[3]) * 1024 if len(volume_parts) >= 4 and volume_parts[3].isdigit() else 0
        return {"platform": "linux", "path_style": "posix", "service_manager": "systemd", "shell": "bash", "os": system.splitlines()[-1].replace('PRETTY_NAME=', '').strip('"'), "architecture": system.splitlines()[1] if len(system.splitlines()) > 1 else "", "home_dir": home_dir, "primary_group": primary_group, "disk": disk, "volumes": [{"root": volume_parts[-1] if volume_parts else "/", "total_bytes": int(volume_parts[1]) * 1024 if len(volume_parts) >= 2 and volume_parts[1].isdigit() else 0, "free_bytes": free_bytes, "writable": True, "recommended": True}], "sudo": sudo, "download_tool": download_tool, "tar_available": tar_available, "steamcmd_path": steamcmd, "steamcmd_source": steamcmd_source, "steamcmd_available": bool(steamcmd), "steamcmd_installable": bool(steamcmd or sudo or (download_tool and tar_available)), "prerequisites": {"steamcmd": bool(steamcmd), "powershell": False, "systemd": True, "winsw": False, "elevated": sudo, "download_tool": bool(download_tool), "archive_tool": tar_available, "missing": [], "repair_actions": []}, "palserver_path": palserver, "install_dir": install_dir, "service_name": service_name, "service_state": service_state, "config_path": config_path if config else "", "save_dir": saves if config else "", "log_dir": logs if config else "", "game_port": int(game_port) if game_port.isdigit() else 8211, "rest_enabled": rest_enabled, "rest_port": int(rest_port) if rest_port.isdigit() else 8212, "rest_url": f"http://{self.client.host}:{rest_port}" if rest_enabled and rest_port else "", "installed": installed}


class WindowsRemotePath:
    DANGEROUS_SUFFIXES = ("\\windows", "\\windows\\system32", "\\program files", "\\program files (x86)", "\\users")

    @classmethod
    def normalize(cls, candidate: str) -> str:
        raw = str(candidate or "").strip().replace("/", "\\")
        if raw.startswith("\\\\"):
            raise ValueError("首版不支持 UNC 远程安装目录")
        normalized = ntpath.normpath(raw)
        drive, tail = ntpath.splitdrive(normalized)
        if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
            raise ValueError(f"Windows 安装目录必须是绝对盘符路径: {candidate}")
        lowered = normalized.rstrip("\\").lower()
        if lowered == drive.lower() or any(lowered == drive.lower() + suffix for suffix in cls.DANGEROUS_SUFFIXES):
            raise ValueError(f"拒绝操作危险 Windows 目录: {normalized}")
        if len([part for part in tail.split("\\") if part]) < 2:
            raise ValueError(f"Windows 安装目录层级过浅: {normalized}")
        return drive.upper() + normalized[len(drive):]


class WindowsRemoteServerInspector:
    MARKER = "PALWORLD_CONSOLE_WINDOWS_PROFILE:"

    def __init__(self, client: RemoteHostClient, on_log: Callable[[str], None] | None = None, preferred_install_dir: str = "", instance_id: str = "default"):
        self.client, self.on_log = client, on_log or (lambda _line: None)
        self.preferred_install_dir, self.instance_id = preferred_install_dir, instance_id

    def _probe_script(self) -> str:
        preferred = RemoteHostClient._ps_literal(self.preferred_install_dir)
        suffix = re.sub(r"[^A-Za-z0-9]", "", self.instance_id)[:8] or "default"
        return rf"""
$ErrorActionPreference = 'Stop'
$preferred = {preferred}
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$os = Get-CimInstance Win32_OperatingSystem
$volumes = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {{
  $writable = $false
  try {{ $p = Join-Path $_.DeviceID '.palworld-console-write-test'; Set-Content -LiteralPath $p -Value 'ok' -ErrorAction Stop; Remove-Item -LiteralPath $p -Force; $writable = $true }} catch {{}}
  [pscustomobject]@{{ root = $_.DeviceID + '\\'; label = $_.VolumeName; total_bytes = [int64]$_.Size; free_bytes = [int64]$_.FreeSpace; writable = $writable; recommended = $false }}
}})
$service = Get-CimInstance Win32_Service -Filter "Name LIKE 'PalworldConsole-%'" | Select-Object -First 1
$serviceName = if ($service) {{ $service.Name }} else {{ '' }}
$install = ''
if ($service -and $service.PathName) {{
  $wrapper = [regex]::Match($service.PathName, '(?:^\"([^\"]+)\"|^(\S+))').Groups | Where-Object {{ $_.Value }} | Select-Object -Last 1 -ExpandProperty Value
  if ($wrapper) {{
    $xmlPath = [IO.Path]::ChangeExtension($wrapper, '.xml')
    if (Test-Path -LiteralPath $xmlPath) {{ try {{ [xml]$xml = Get-Content -LiteralPath $xmlPath -Raw; $install = [string]$xml.service.workingdirectory }} catch {{}} }}
  }}
}}
if (-not $install -and $preferred -and (Test-Path -LiteralPath (Join-Path $preferred 'PalServer.exe'))) {{ $install = $preferred }}
if (-not $install) {{
  foreach ($volume in ($volumes | Sort-Object free_bytes -Descending)) {{
    $candidate = Join-Path $volume.root 'PalworldServer'
    $found = Get-ChildItem -LiteralPath $candidate -Filter PalServer.exe -File -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {{ $install = $found.DirectoryName; break }}
  }}
}}
if (-not $install) {{
  $best = $volumes | Where-Object writable | Sort-Object free_bytes -Descending | Select-Object -First 1
  if ($best) {{ $install = Join-Path $best.root ('PalworldServer\\{suffix}') }}
}}
$installed = [bool]($install -and (Test-Path -LiteralPath (Join-Path $install 'PalServer.exe')))
$config = if ($install) {{ Join-Path $install 'Pal\\Saved\\Config\\WindowsServer\\PalWorldSettings.ini' }} else {{ '' }}
$steamcmd = if ($install) {{ Join-Path $install '_tools\\steamcmd\\steamcmd.exe' }} else {{ '' }}
$winsw = if ($install) {{ Join-Path $install '_tools\\winsw\\PalworldConsole.exe' }} else {{ '' }}
$profile = [ordered]@{{
  platform='windows'; os=$os.Caption; version=$os.Version; architecture=$env:PROCESSOR_ARCHITECTURE; shell='powershell'; path_style='windows'; service_manager='winsw'; home_dir=$env:USERPROFILE
  powershell_version=$PSVersionTable.PSVersion.ToString(); elevated=$isAdmin; volumes=$volumes; install_dir=$install; palserver_path=if($installed){{Join-Path $install 'PalServer.exe'}}else{{''}}; installed=$installed
  steamcmd_path=if(Test-Path -LiteralPath $steamcmd){{$steamcmd}}else{{''}}; winsw_path=if(Test-Path -LiteralPath $winsw){{$winsw}}else{{''}}; service_name=$serviceName; service_state=if($service){{$service.State}}else{{'not_found'}}
  config_path=if(Test-Path -LiteralPath $config){{$config}}else{{''}}; save_dir=if($install){{Join-Path $install 'Pal\\Saved'}}else{{''}}; log_dir=if($install){{Join-Path $install 'logs'}}else{{''}}
}}
Write-Output ('{self.MARKER}' + ($profile | ConvertTo-Json -Compress -Depth 6))
"""

    @classmethod
    def parse_probe(cls, output: str) -> dict[str, object] | None:
        marker_line = next((line for line in output.splitlines() if line.startswith(cls.MARKER)), "")
        if not marker_line:
            return None
        payload = json.loads(marker_line[len(cls.MARKER):])
        payload["platform"] = "windows"
        payload.setdefault("path_style", "windows")
        payload.setdefault("shell", "powershell")
        payload.setdefault("service_manager", "winsw")
        payload["game_port"] = int(payload.get("game_port") or 8211)
        payload["rest_port"] = int(payload.get("rest_port") or 8212)
        payload["rest_url"] = ""
        volumes = payload.get("volumes") or []
        if isinstance(volumes, dict):
            volumes = [volumes]
        writable = [volume for volume in volumes if volume.get("writable")]
        if writable:
            best = max(writable, key=lambda item: int(item.get("free_bytes") or 0))
            best["recommended"] = True
        payload["volumes"] = volumes
        missing = []
        if not payload.get("elevated"): missing.append("管理员权限")
        if not payload.get("steamcmd_path"): missing.append("SteamCMD")
        if not payload.get("winsw_path"): missing.append("WinSW")
        required_bytes = 6 * 1024**3
        if not writable or max(int(item.get("free_bytes") or 0) for item in writable) < required_bytes: missing.append("磁盘空间")
        repair_actions = []
        if not payload.get("steamcmd_path"): repair_actions.append("自动准备实例专用 SteamCMD")
        if not payload.get("winsw_path"): repair_actions.append("下载并校验 WinSW 2.12.0")
        if not payload.get("elevated"): repair_actions.append("改用具备管理员令牌的 SSH 会话")
        if "磁盘空间" in missing: repair_actions.extend(("选择其他固定磁盘", "清理应用创建的缓存和失败事务"))
        payload["required_bytes"] = required_bytes
        payload["prerequisites"] = {"steamcmd": bool(payload.get("steamcmd_path")), "powershell": True, "systemd": False, "winsw": bool(payload.get("winsw_path")), "elevated": bool(payload.get("elevated")), "download_tool": True, "archive_tool": True, "missing": missing, "repair_actions": repair_actions}
        payload["steamcmd_available"] = bool(payload.get("steamcmd_path"))
        payload["steamcmd_installable"] = bool(payload.get("elevated"))
        payload["disk"] = "\n".join(f"{v.get('root')} 可用 {int(v.get('free_bytes') or 0) // 1024 // 1024} MB" for v in volumes)
        if payload.get("install_dir"):
            payload["install_dir"] = WindowsRemotePath.normalize(str(payload["install_dir"]))
        return payload

    def discover(self) -> dict[str, object]:
        self.on_log("SSH 检测：Windows PowerShell、磁盘和服务")
        code, output, error = self.client.run_powershell(self._probe_script())
        profile = self.parse_probe(output)
        if code or not profile:
            raise RuntimeError(error.strip() or output.strip() or "Windows PowerShell 探针未返回有效结果")
        return profile


class RemoteServerInspector:
    """Detect the remote OS first, then dispatch to a platform inspector."""
    def __init__(self, client: RemoteHostClient, on_log: Callable[[str], None] | None = None, preferred_install_dir: str = "", instance_id: str = "default"):
        self.client, self.on_log = client, on_log or (lambda _line: None)
        self.preferred_install_dir, self.instance_id = preferred_install_dir, instance_id

    @staticmethod
    def _setting(text: str, name: str) -> str:
        return LinuxRemoteServerInspector._setting(text, name)

    def discover(self) -> dict[str, object]:
        windows = WindowsRemoteServerInspector(self.client, self.on_log, self.preferred_install_dir, self.instance_id)
        try:
            code, output, _ = self.client.run_powershell(windows._probe_script())
            profile = windows.parse_probe(output)
            if code == 0 and profile:
                self.on_log("已识别远程系统：Windows Server")
                return profile
        except Exception as exc:
            self.on_log(f"Windows 探针未命中：{exc}")
        try:
            profile = LinuxRemoteServerInspector(self.client, self.on_log, self.preferred_install_dir).discover()
            self.on_log("已识别远程系统：Linux")
            return profile
        except Exception as exc:
            return {"platform": "unknown", "os": "未知系统", "architecture": "", "path_style": "unknown", "service_manager": "unknown", "installed": False, "capabilities": [], "detection_error": str(exc), "prerequisites": {"missing": ["无法识别远程操作系统"], "repair_actions": ["retry_detection", "export_diagnostics"]}}


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


class WindowsRemoteServerLifecycle:
    STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
    WINSW_VERSION = "2.12.0"
    WINSW_URL = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
    WINSW_SHA256 = "05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA"

    def __init__(self, instance: ServerInstance, client: RemoteHostClient, on_log: Callable[[str], None] | None = None, on_progress: Callable[[TaskProgress], None] | None = None):
        self.instance, self.client = instance, client
        self.on_log = on_log or (lambda _line: None)
        self.on_progress = on_progress or (lambda _progress: None)

    @staticmethod
    def parse_progress(line: str) -> TaskProgress | None:
        return RemoteServerLifecycle.parse_progress(line)

    def _service(self) -> str:
        service = str(self.instance.remote_profile.get("service_name") or f"PalworldConsole-{self.instance.id.replace('-', '')[:8]}")
        if not re.fullmatch(r"PalworldConsole-[A-Za-z0-9]{1,32}", service):
            raise ValueError("WinSW 服务名不符合 PalworldConsole 实例命名规则")
        return service

    def _install_dir(self) -> str:
        candidate = str(self.instance.remote_profile.get("install_dir") or self.instance.install_dir)
        install_dir = WindowsRemotePath.normalize(candidate)
        script = f"""
$ErrorActionPreference='Stop'
$path={RemoteHostClient._ps_literal(install_dir)}
$root=[IO.Path]::GetPathRoot($path)
if ($path.TrimEnd('\\') -eq $root.TrimEnd('\\')) {{ throw '拒绝使用磁盘根目录' }}
$probe=$path
while (-not (Test-Path -LiteralPath $probe)) {{ $probe=Split-Path -Parent $probe; if (-not $probe) {{ throw '找不到可写父目录' }} }}
$cursor=Get-Item -LiteralPath $probe -Force
while ($cursor) {{
  if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{ throw ('安装路径包含重解析点: '+$cursor.FullName) }}
  if ($cursor.FullName.TrimEnd('\\') -eq $root.TrimEnd('\\')) {{ break }}
  $parent=Split-Path -Parent $cursor.FullName; if (-not $parent) {{ break }}; $cursor=Get-Item -LiteralPath $parent -Force
}}
"""
        code, output, error = self.client.run_powershell(script)
        if code:
            raise ValueError(error.strip() or output.strip() or f"Windows 安装路径不安全: {install_dir}")
        self.instance.install_dir = install_dir
        self.instance.remote_profile.update({"platform": "windows", "install_dir": install_dir, "palserver_path": ntpath.join(install_dir, "PalServer.exe")})
        return install_dir

    def _run_ps(self, label: str, script: str) -> None:
        self.on_log(f"SSH/PowerShell：{label}")
        emitted = False
        def handle(line: str):
            nonlocal emitted
            emitted = True
            parsed = self.parse_progress(line)
            if parsed: self.on_progress(parsed)
            if line and not line.startswith("PAL_PROGRESS|"): self.on_log(line)
        code, output, error = self.client.run_powershell_stream(script, handle)
        if not emitted:
            for line in (output + "\n" + error).splitlines(): handle(line)
        if code:
            raise RuntimeError(f"{label}失败：{error.strip() or output.strip() or 'PowerShell 返回非零状态'}")

    @classmethod
    def service_xml(cls, instance: ServerInstance, install_dir: str, service_name: str, game_port: int, rest_port: int) -> str:
        exe = ntpath.join(install_dir, "PalServer.exe")
        log_dir = ntpath.join(install_dir, "logs")
        values = {"id": service_name, "name": f"Palworld Server ({instance.name})", "exe": exe, "cwd": install_dir, "logs": log_dir}
        escaped = {key: html.escape(str(value), quote=True) for key, value in values.items()}
        return f"""<service>
  <id>{escaped['id']}</id>
  <name>{escaped['name']}</name>
  <description>Palworld dedicated server managed by Palworld Console</description>
  <executable>{escaped['exe']}</executable>
  <arguments>-port={int(game_port)} -RESTAPIEnabled -RESTAPIPort={int(rest_port)} -enable-gamedata-api</arguments>
  <workingdirectory>{escaped['cwd']}</workingdirectory>
  <logpath>{escaped['logs']}</logpath>
  <log mode="roll-by-size"><sizeThreshold>10485760</sizeThreshold><keepFiles>8</keepFiles></log>
  <serviceaccount><username>NT AUTHORITY\\LocalService</username></serviceaccount>
  <onfailure action="restart" delay="10 sec"/><onfailure action="restart" delay="30 sec"/><onfailure action="none"/>
  <stoptimeout>30 sec</stoptimeout>
</service>
"""

    def _prepare_tools_script(self, install_dir: str) -> str:
        q = RemoteHostClient._ps_literal
        steam_root, steam_exe = ntpath.join(install_dir, "_tools", "steamcmd"), ntpath.join(install_dir, "_tools", "steamcmd", "steamcmd.exe")
        winsw_root, winsw_exe = ntpath.join(install_dir, "_tools", "winsw"), ntpath.join(install_dir, "_tools", "winsw", "PalworldConsole.exe")
        return f"""
$ErrorActionPreference='Stop'
$install={q(install_dir)}; $steamRoot={q(steam_root)}; $steamExe={q(steam_exe)}; $winswRoot={q(winsw_root)}; $winswExe={q(winsw_exe)}
Write-Output 'PAL_PROGRESS|8|检查 Windows 权限与磁盘'
$admin=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {{ throw 'Windows 自动部署需要管理员令牌' }}
New-Item -ItemType Directory -Force -Path $install,$steamRoot,$winswRoot,(Join-Path $install 'logs') | Out-Null
$drive=Get-PSDrive -Name ([IO.Path]::GetPathRoot($install).Substring(0,1)); if ($drive.Free -lt 6442450944) {{ throw ('安装磁盘空间不足，当前可用 '+[math]::Round($drive.Free/1MB)+' MB') }}
if (-not (Test-Path -LiteralPath $steamExe)) {{
  Write-Output 'PAL_PROGRESS|15|下载 Windows SteamCMD'
  $zip=Join-Path $steamRoot ('steamcmd-'+[guid]::NewGuid().ToString('N')+'.zip')
  try {{ Invoke-WebRequest -UseBasicParsing -Uri {q(self.STEAMCMD_URL)} -OutFile $zip; Expand-Archive -LiteralPath $zip -DestinationPath $steamRoot -Force }} finally {{ Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }}
}}
Write-Output 'PAL_PROGRESS|25|初始化 SteamCMD'
& $steamExe +quit; if ($LASTEXITCODE -ne 0) {{ throw ('SteamCMD 初始化失败: '+$LASTEXITCODE) }}
Write-Output 'PAL_PROGRESS|35|下载并校验 WinSW'
if (-not (Test-Path -LiteralPath $winswExe) -or (Get-FileHash -LiteralPath $winswExe -Algorithm SHA256).Hash -ne {q(self.WINSW_SHA256)}) {{ Invoke-WebRequest -UseBasicParsing -Uri {q(self.WINSW_URL)} -OutFile $winswExe }}
$actual=(Get-FileHash -LiteralPath $winswExe -Algorithm SHA256).Hash; if ($actual -ne {q(self.WINSW_SHA256)}) {{ Remove-Item -LiteralPath $winswExe -Force; throw ('WinSW SHA-256 校验失败: '+$actual) }}
Write-Output 'PAL_PROGRESS|45|下载并校验 Palworld 服务端'
& $steamExe +force_install_dir $install +login anonymous +app_update 2394010 validate +quit; if ($LASTEXITCODE -ne 0) {{ throw ('SteamCMD 安装失败: '+$LASTEXITCODE) }}
if (-not (Test-Path -LiteralPath (Join-Path $install 'PalServer.exe'))) {{ throw '安装结束后未找到 PalServer.exe' }}
Write-Output 'PAL_PROGRESS|84|Windows 服务端文件已就绪'
"""

    def install(self) -> None:
        self.on_progress(TaskProgress(3, "检测环境", "准备 Windows Server 远程安装"))
        self._run_ps("准备工具并安装服务端", self._prepare_tools_script(self._install_dir()))

    def configure_service(self) -> None:
        install_dir, service = self._install_dir(), self._service()
        wrapper = ntpath.join(install_dir, "_tools", "winsw", "PalworldConsole.exe")
        xml_path = ntpath.join(install_dir, "_tools", "winsw", "PalworldConsole.xml")
        xml = self.service_xml(self.instance, install_dir, service, int(self.instance.remote_profile.get("game_port") or self.instance.game_port), int(self.instance.remote_profile.get("rest_port") or 8212))
        self.client.write_text_atomic_windows(xml_path, xml, backup=True)
        script = f"""
$ErrorActionPreference='Stop'; $wrapper={RemoteHostClient._ps_literal(wrapper)}; $install={RemoteHostClient._ps_literal(install_dir)}
Write-Output 'PAL_PROGRESS|88|配置 WinSW 服务'
& icacls.exe $install /grant '*S-1-5-19:(OI)(CI)M' /T /C | Out-Null
& $wrapper stop 2>$null; & $wrapper uninstall 2>$null; & $wrapper install
if ($LASTEXITCODE -ne 0) {{ throw ('WinSW 服务安装失败: '+$LASTEXITCODE) }}
"""
        self._run_ps("配置 WinSW 服务", script)
        self.instance.remote_profile.update({"service_name": service, "winsw_path": wrapper, "service_manager": "winsw"})

    def allow_game_firewall(self) -> None:
        port, name = int(self.instance.remote_profile.get("game_port") or self.instance.game_port), f"PalworldConsole-{self.instance.id.replace('-', '')[:8]}-UDP"
        script = f"Get-NetFirewallRule -DisplayName {RemoteHostClient._ps_literal(name)} -ErrorAction SilentlyContinue | Remove-NetFirewallRule; New-NetFirewallRule -DisplayName {RemoteHostClient._ps_literal(name)} -Direction Inbound -Action Allow -Protocol UDP -LocalPort {port} | Out-Null"
        self._run_ps("配置 Windows 防火墙游戏端口", script)

    def update(self, restart: bool = True) -> None:
        if not self.instance.remote_profile.get("installed"):
            return self.install()
        was_running = self.status() == "running"
        self.stop()
        try: self._run_ps("更新 Windows 服务端", self._prepare_tools_script(self._install_dir()))
        finally:
            if restart and was_running: self.start()

    def start(self): self._run_ps("启动 WinSW 服务", f"Start-Service -Name {RemoteHostClient._ps_literal(self._service())} -ErrorAction Stop")
    def stop(self): self._run_ps("停止 WinSW 服务", f"Stop-Service -Name {RemoteHostClient._ps_literal(self._service())} -Force -ErrorAction Stop")
    def restart(self): self._run_ps("重启 WinSW 服务", f"Restart-Service -Name {RemoteHostClient._ps_literal(self._service())} -Force -ErrorAction Stop")
    def status(self) -> str:
        code, output, _ = self.client.run_powershell(f"$s=Get-Service -Name {RemoteHostClient._ps_literal(self._service())} -ErrorAction SilentlyContinue; if($s){{$s.Status}}else{{'not_found'}}")
        if code: return "unknown"
        return "running" if output.strip().lower() == "running" else output.strip().lower() or "unknown"

    def wait_for_game_listener(self, timeout: int = 45) -> None:
        port, deadline = int(self.instance.remote_profile.get("game_port") or self.instance.game_port), time.time() + timeout
        while time.time() < deadline:
            script = f"$p=Get-Process PalServer-Win64-Shipping,PalServer -ErrorAction SilentlyContinue; $u=Get-NetUDPEndpoint -LocalPort {port} -ErrorAction SilentlyContinue; if($p -and $u){{'READY'}}"
            code, output, _ = self.client.run_powershell(script)
            if code == 0 and output.strip() == "READY": return
            time.sleep(1)
        raise RuntimeError(f"WinSW 服务已启动，但 PalServer 进程或 UDP {port} 监听未就绪")

    def repair_runtime(self) -> None:
        self.configure_service(); self.allow_game_firewall(); self.restart(); self.wait_for_game_listener()

    def uninstall(self, backup_destination: Path) -> UninstallResult:
        install_dir = self._install_dir()
        marker = ntpath.join(install_dir, "PalServer.exe")
        code, output, error = self.client.run_powershell(f"$p=Get-Item -LiteralPath {RemoteHostClient._ps_literal(install_dir)} -Force; if(($p.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){{throw '拒绝删除重解析点目录'}}; if(-not(Test-Path -LiteralPath {RemoteHostClient._ps_literal(marker)})){{throw '目录缺少 PalServer.exe 标记'}}")
        if code: raise ValueError(error.strip() or output.strip() or "Windows 安装目录验证失败")
        was_running = self.status() == "running"
        if was_running: self.stop()
        try: backup = BackupService().create_remote(self.client, self.instance, backup_destination, install_dir)
        except Exception:
            if was_running:
                try: self.start()
                except Exception as exc: self.on_log(f"备份失败后恢复 Windows 服务也失败：{exc}")
            raise
        wrapper = ntpath.join(install_dir, "_tools", "winsw", "PalworldConsole.exe")
        script = f"""
$ErrorActionPreference='Stop'; $wrapper={RemoteHostClient._ps_literal(wrapper)}; $install={RemoteHostClient._ps_literal(install_dir)}
Write-Output 'PAL_PROGRESS|75|删除 WinSW 服务'
if(Test-Path -LiteralPath $wrapper){{& $wrapper stop 2>$null; & $wrapper uninstall}}
Write-Output 'PAL_PROGRESS|88|删除 Windows 服务端文件'
Remove-Item -LiteralPath $install -Recurse -Force
Write-Output 'PAL_PROGRESS|95|服务端卸载完成'
"""
        self._run_ps("卸载 Windows 服务端", script)
        return UninstallResult(install_dir, str(backup) if backup else "", bool(backup))


def remote_lifecycle_for(instance: ServerInstance, client: RemoteHostClient, on_log: Callable[[str], None] | None = None, on_progress: Callable[[TaskProgress], None] | None = None):
    platform = str(instance.remote_profile.get("platform") or "linux").lower()
    if platform == "windows":
        return WindowsRemoteServerLifecycle(instance, client, on_log, on_progress)
    if platform == "unknown":
        raise RuntimeError("远程操作系统尚未识别，禁止部署")
    return RemoteServerLifecycle(instance, client, on_log, on_progress)


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
        if str(profile.get("platform") or "linux").lower() == "windows":
            return cls._collect_remote_windows(client, instance, rest_client)
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

    @classmethod
    def _collect_remote_windows(cls, client: RemoteHostClient, instance: ServerInstance, rest_client: PalworldRestClient | None = None) -> ServerHealthSnapshot:
        profile = instance.remote_profile
        service = str(profile.get("service_name") or f"PalworldConsole-{instance.id.replace('-', '')[:8]}")
        game_port, rest_port = int(profile.get("game_port") or instance.game_port or 8211), int(profile.get("rest_port") or 8212)
        script = f"""
$s=Get-CimInstance Win32_Service -Filter "Name='{service.replace("'", "''")}'" -ErrorAction SilentlyContinue
$p=Get-Process PalServer-Win64-Shipping,PalServer -ErrorAction SilentlyContinue | Select-Object -First 1
$udp=[bool](Get-NetUDPEndpoint -LocalPort {game_port} -ErrorAction SilentlyContinue)
$tcp=[bool](Get-NetTCPConnection -State Listen -LocalPort {rest_port} -ErrorAction SilentlyContinue)
$fw=[bool](Get-NetFirewallPortFilter -Protocol UDP -ErrorAction SilentlyContinue | Where-Object LocalPort -eq '{game_port}')
$drive=if({RemoteHostClient._ps_literal(str(profile.get('install_dir') or instance.install_dir))}){{Get-PSDrive -Name ([IO.Path]::GetPathRoot({RemoteHostClient._ps_literal(str(profile.get('install_dir') or instance.install_dir))}).Substring(0,1)) -ErrorAction SilentlyContinue}}
$o=[ordered]@{{service_state=if($s){{$s.State}}else{{'not_found'}};pid=if($p){{$p.Id}}else{{0}};user=if($s){{$s.StartName}}else{{''}};cpu=if($p){{$p.CPU}}else{{0}};memory_mb=if($p){{[math]::Round($p.WorkingSet64/1MB,1)}}else{{0}};game=$udp;rest=$tcp;firewall=$fw;disk=if($drive){{($drive.Name+': '+[math]::Round($drive.Free/1MB)+' MB free')}}else{{''}}}}
'PALWORLD_CONSOLE_HEALTH:'+($o|ConvertTo-Json -Compress)
"""
        code, output, error = client.run_powershell(script)
        line = next((item for item in output.splitlines() if item.startswith("PALWORLD_CONSOLE_HEALTH:")), "")
        data = json.loads(line.partition(":")[2]) if line else {}
        state = str(data.get("service_state") or "unknown")
        pid, user = int(data.get("pid") or 0), str(data.get("user") or "")
        game_listening, rest_listening = bool(data.get("game")), bool(data.get("rest"))
        rest_ok, info, metrics, recent_log = False, {}, {}, ""
        if rest_client:
            try: info, metrics, rest_ok = rest_client.health() or {}, rest_client.metrics() or {}, True
            except Exception as exc: recent_log = f"REST 检查失败：{exc}"
        issues = []
        if code: issues.append(f"Windows 服务检查失败：{error.strip() or output.strip()}")
        if state.lower() != "running": issues.append(f"WinSW 服务状态为 {state}")
        if pid <= 0: issues.append("未检测到 PalServer 主进程")
        if not game_listening: issues.append(f"游戏 UDP {game_port} 未监听")
        if not rest_listening: issues.append(f"REST TCP {rest_port} 未监听")
        if not data.get("firewall"): issues.append(f"Windows 防火墙未检测到 UDP {game_port} 放行规则")
        if rest_client and not rest_ok: issues.append("REST API 通过 SSH 隧道不可用")
        return ServerHealthSnapshot(
            healthy=state.lower() == "running" and pid > 0 and game_listening and (not rest_client or rest_ok), service_state=state, pid=pid, process_user=user,
            cpu_percent=float(data.get("cpu") or 0), memory_mb=float(data.get("memory_mb") or 0), disk=str(data.get("disk") or ""),
            uptime_seconds=int(cls._number(metrics, "uptime", "uptime_seconds")), fps=cls._number(metrics, "serverfps", "server_fps", "fps"), frame_time_ms=cls._number(metrics, "serverframetime", "server_frame_time", "frame_time"),
            player_count=int(cls._number(metrics, "currentplayernum", "current_players", "player_count", default=cls._number(info, "currentplayernum", "current_players"))), player_limit=int(cls._number(metrics, "maxplayernum", "max_players", default=cls._number(info, "maxplayernum", "max_players"))),
            game_days=int(cls._number(metrics, "days", "game_days")), version=str(info.get("version") or ""), world_guid=str(info.get("worldguid") or info.get("world_guid") or ""),
            game_endpoint=EndpointStatus("游戏", "UDP", game_port, game_listening, None, f"{instance.host}:{game_port}"), rest_endpoint=EndpointStatus("REST", "TCP/SSH", rest_port, rest_listening, rest_ok if rest_client else None, "仅通过 SSH 隧道访问"),
            ssh_ok=True, rest_ok=rest_ok, ufw_active=True, ufw_game_allowed=bool(data.get("firewall")), issues=tuple(issues), recent_log=recent_log, checked_at=datetime.now().isoformat(timespec="seconds"),
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
            normalized = {str(key).replace("_", "").lower(): value for key, value in row.items()}
            def pick(*keys, default=""):
                for key in keys:
                    value = normalized.get(str(key).replace("_", "").lower())
                    if value not in (None, ""):
                        return value
                return default
            location = row.get("location") or normalized.get("location") or {}
            result.append(PlayerRecord(
                name=str(pick("name", "playerName")),
                account_name=str(pick("accountName", "account_name", "platformAccount")),
                user_id=str(pick("userId", "userid", "user_id")),
                player_uid=str(pick("playerUId", "playerUid", "playeruid", "player_uid")),
                level=int(pick("level", default=0) or 0), ping=float(pick("ping", default=0) or 0), ip=str(pick("ip")),
                location_x=float(location.get("x") or pick("location_x", default=0) or 0), location_y=float(location.get("y") or pick("location_y", default=0) or 0),
                building_count=int(pick("buildingCount", "building_count", default=0) or 0), guild_id=str(pick("guildId", "GuildID", "guild_id")),
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
        if str(instance.remote_profile.get("platform") or "linux").lower() == "windows":
            return self._create_remote_windows(client, instance, destination, install_dir)
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

    def _create_remote_windows(self, client: RemoteHostClient, instance: ServerInstance, destination: Path, install_dir: str) -> Path | None:
        install_dir = WindowsRemotePath.normalize(install_dir)
        saved_dir = ntpath.join(install_dir, "Pal", "Saved")
        remote_archive = ntpath.join(install_dir, "_tools", f"backup-{uuid.uuid4().hex}.zip")
        q = RemoteHostClient._ps_literal
        check = f"if(-not(Test-Path -LiteralPath {q(saved_dir)})){{'MISSING'}}elseif(-not((Get-Item -LiteralPath {q(saved_dir)}).PSIsContainer)){{throw 'Saved 不是目录'}}else{{'READY'}}"
        code, output, error = client.run_powershell(check)
        if output.strip() == "MISSING": return None
        if code or output.strip() != "READY": raise RuntimeError(f"无法读取 Windows 远程存档目录：{error.strip() or output.strip()}")
        destination.mkdir(parents=True, exist_ok=True)
        local_archive = destination / f"{instance.id}-{datetime.now():%Y%m%d-%H%M%S}.zip"
        try:
            script = f"New-Item -ItemType Directory -Force -Path {q(ntpath.dirname(remote_archive))}|Out-Null; Compress-Archive -LiteralPath {q(saved_dir)} -DestinationPath {q(remote_archive)} -CompressionLevel Optimal -Force"
            code, output, error = client.run_powershell(script)
            if code: raise RuntimeError(f"Windows 远程存档打包失败：{error.strip() or output.strip()}")
            client.download_file(remote_archive, local_archive)
            self.validate_zip(local_archive)
            with zipfile.ZipFile(local_archive) as archive:
                if not archive.namelist(): raise RuntimeError("Windows 远程备份结构无效")
            return local_archive
        except Exception:
            local_archive.unlink(missing_ok=True)
            raise
        finally:
            client.run_powershell(f"Remove-Item -LiteralPath {q(remote_archive)} -Force -ErrorAction SilentlyContinue")

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
