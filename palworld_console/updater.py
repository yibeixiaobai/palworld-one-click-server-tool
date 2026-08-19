from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Callable

import requests
from packaging.version import InvalidVersion, Version


DEFAULT_REPOSITORY = "yibeixiaobai/palworld-one-click-server-tool"
LATEST_RELEASE_URL = "https://api.github.com/repos/{repository}/releases/latest"
INSTALLER_TEMPLATE = "PalworldConsole-Setup-v{version}.exe"
CHECKSUM_ASSET = "SHA256SUMS.txt"
_CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$")


class UpdateError(RuntimeError):
    """Raised when a release cannot be trusted or downloaded."""


class DownloadCancelled(UpdateError):
    """Raised when the user cancels an installer download."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: Version
    tag_name: str
    name: str
    body: str
    installer_name: str
    installer_url: str
    installer_size: int | None
    sha256: str

    @property
    def version_text(self) -> str:
        return str(self.version)


ProgressCallback = Callable[[int, int | None], None]


def _parse_version(value: str) -> Version:
    text = str(value or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    try:
        return Version(text)
    except InvalidVersion as exc:
        raise UpdateError(f"无效版本号：{value}") from exc


class UpdateService:
    def __init__(self, repository: str = DEFAULT_REPOSITORY, storage_root: Path | None = None, session=None):
        self.repository = repository
        self.storage_root = Path(storage_root or (Path.home() / ".palworld-console"))
        self.session = session or requests.Session()

    def check_latest(self, current_version: str) -> ReleaseInfo | None:
        current = _parse_version(current_version)
        url = LATEST_RELEASE_URL.format(repository=self.repository)
        try:
            response = self.session.get(
                url,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "PalworldConsole-Updater"},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise UpdateError(f"检查更新失败：{exc}") from exc
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise UpdateError(f"读取 Release 失败：{exc}") from exc
        if payload.get("draft") or payload.get("prerelease"):
            return None
        tag_name = str(payload.get("tag_name") or "").strip()
        target = _parse_version(tag_name)
        if target <= current:
            return None
        assets = {str(asset.get("name")): asset for asset in payload.get("assets") or ()}
        installer_name = INSTALLER_TEMPLATE.format(version=target)
        installer = assets.get(installer_name)
        checksum = assets.get(CHECKSUM_ASSET)
        if not installer or not checksum:
            raise UpdateError(f"Release {tag_name} 缺少安装器或 SHA256SUMS.txt")
        installer_url = str(installer.get("browser_download_url") or "")
        checksum_url = str(checksum.get("browser_download_url") or "")
        if not installer_url.startswith("https://") or not checksum_url.startswith("https://"):
            raise UpdateError("Release 资产 URL 不安全")
        try:
            checksum_response = self.session.get(
                checksum_url,
                headers={"User-Agent": "PalworldConsole-Updater"},
                timeout=15,
            )
            checksum_response.raise_for_status()
            checksum_text = checksum_response.text
        except requests.RequestException as exc:
            raise UpdateError(f"读取安装包校验和失败：{exc}") from exc
        digest = None
        for line in checksum_text.splitlines():
            match = _CHECKSUM_LINE.match(line)
            if match and Path(match.group(2)).name == installer_name:
                digest = match.group(1).lower()
                break
        if not digest:
            raise UpdateError(f"校验文件中没有 {installer_name} 的 SHA-256")
        size = installer.get("size")
        return ReleaseInfo(
            version=target,
            tag_name=tag_name,
            name=str(payload.get("name") or tag_name),
            body=str(payload.get("body") or "").strip(),
            installer_name=installer_name,
            installer_url=installer_url,
            installer_size=int(size) if isinstance(size, int) else None,
            sha256=digest,
        )

    def download_installer(self, release: ReleaseInfo, progress: ProgressCallback | None = None, cancel=None) -> Path:
        destination_dir = self.storage_root / "updates"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / release.installer_name
        partial = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            response = self.session.get(
                release.installer_url,
                headers={"Accept": "application/octet-stream", "User-Agent": "PalworldConsole-Updater"},
                stream=True,
                timeout=60,
            )
            response.raise_for_status()
            total_header = response.headers.get("Content-Length")
            total = int(total_header) if total_header and total_header.isdigit() else release.installer_size
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled("用户取消了更新下载")
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total)
            if total is not None and received != total:
                raise UpdateError(f"安装包长度不符：期望 {total} 字节，实际 {received} 字节")
            actual = digest.hexdigest().lower()
            if actual != release.sha256.lower():
                raise UpdateError(f"安装包 SHA-256 校验失败：期望 {release.sha256}，实际 {actual}")
            partial.replace(destination)
            for old in destination_dir.glob("PalworldConsole-Setup-v*.exe"):
                if old != destination:
                    old.unlink(missing_ok=True)
            return destination
        except requests.RequestException as exc:
            raise UpdateError(f"下载安装包失败：{exc}") from exc
        finally:
            partial.unlink(missing_ok=True)
