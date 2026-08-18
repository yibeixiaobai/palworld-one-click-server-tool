from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import html
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import tarfile
from typing import Any, Protocol
import urllib.parse
import urllib.request
import zipfile
import shlex


@dataclass
class ModManifest:
    package_name: str
    display_name: str = ""
    version: str = "未知"
    source: str = "local"
    workshop_id: str = ""
    install_rules: tuple[str, ...] = ()
    server_supported: bool = False
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    sha256: str = ""
    install_path: str = ""
    enabled: bool = False
    metadata_complete: bool = True
    archive_path: str = ""
    mod_type: str = "unknown"
    runtime: str = "windows"
    author: str = ""
    source_url: str = ""
    requires_ue4ss: bool = False
    validation_status: str = "unverified"
    last_operation: str = ""
    last_operation_at: str = ""
    risk: str = "中"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("install_rules", "dependencies", "conflicts"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModManifest":
        values = dict(data)
        for key in ("install_rules", "dependencies", "conflicts"):
            values[key] = tuple(values.get(key) or ())
        return cls(**{key: value for key, value in values.items() if key in cls.__dataclass_fields__})


@dataclass
class ModEnvironment:
    host_system: str
    server_type: str
    wine_path: str = ""
    wine_version: str = ""
    workshop_root: str = ""
    mods_dir: str = ""
    settings_path: str = ""
    palserver_exe: str = ""
    supported: bool = False
    experimental: bool = False
    reason: str = ""
    managed_mods_dir: str = ""
    ue4ss_root: str = ""
    ue4ss_mods_dir: str = ""
    native_mods_dir: str = ""
    paks_dir: str = ""
    ue4ss_config_path: str = ""
    writable_paths: tuple[str, ...] = ()
    detected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModInstallPlan:
    mod_type: str
    target: str
    target_root: str
    requires_ue4ss: bool
    changes_settings: bool
    read_only: bool = False
    reason: str = ""


@dataclass(frozen=True)
class WorkshopCatalogItem:
    workshop_id: str
    title: str
    author: str = ""
    preview_url: str = ""
    detail_url: str = ""
    description: str = ""
    updated_at: str = ""
    installed: bool = False


@dataclass(frozen=True)
class WorkshopCatalogPage:
    items: tuple[WorkshopCatalogItem, ...]
    page: int
    query: str = ""
    sort: str = "trend"
    from_cache: bool = False
    fetched_at: str = ""


class WorkshopCatalogService:
    APP_ID = "1623730"
    BASE_URL = "https://steamcommunity.com/workshop/browse/"

    def __init__(self, cache_dir: Path, ttl_seconds: int = 86400, timeout_seconds: int = 8):
        self.cache_dir = cache_dir; self.ttl_seconds = ttl_seconds; self.timeout_seconds = timeout_seconds

    def fetch(self, query: str = "", sort: str = "trend", page: int = 1, force: bool = False) -> WorkshopCatalogPage:
        page = max(1, int(page)); sort = sort if sort in {"trend", "mostrecent", "totaluniquesubscribers"} else "trend"
        cache = self.cache_dir / f"catalog-{sort}-{page}-{hashlib.sha256(query.encode()).hexdigest()[:12]}.json"; cached = self._read_cache(cache)
        if cached and not force and time.time() - cache.stat().st_mtime < self.ttl_seconds: return self._page_from_payload(cached, True)
        params = {"appid": self.APP_ID, "browsesort": sort, "section": "readytouseitems", "actualsort": sort, "p": str(page)}
        if query.strip(): params["searchtext"] = query.strip()
        try:
            request = urllib.request.Request(self.BASE_URL + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "Mozilla/5.0 PalworldConsole/0.3", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"})
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response: source = response.read().decode("utf-8", errors="replace")
            items = self.parse_catalog(source)
            if not items: raise RuntimeError("Steam Workshop 页面未返回可识别的模组条目")
            payload = {"items": [asdict(item) for item in items], "page": page, "query": query, "sort": sort, "fetched_at": datetime.now().isoformat(timespec="seconds")}
            cache.parent.mkdir(parents=True, exist_ok=True); temporary = cache.with_suffix(".tmp"); temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"); temporary.replace(cache)
            return self._page_from_payload(payload, False)
        except Exception:
            if cached: return self._page_from_payload(cached, True)
            raise

    def fetch_detail(self, item: WorkshopCatalogItem) -> WorkshopCatalogItem:
        cache = self.cache_dir / f"detail-{item.workshop_id}.json"; cached = self._read_cache(cache)
        if cached and time.time() - cache.stat().st_mtime < self.ttl_seconds: return WorkshopCatalogItem(**cached)
        request = urllib.request.Request(item.detail_url, headers={"User-Agent": "Mozilla/5.0 PalworldConsole/0.3", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response: source = response.read().decode("utf-8", errors="replace")
            description_match = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', source, re.I)
            updated_match = re.search(r'Updated[^<]{0,80}</div>\s*<div[^>]*>([^<]+)', source, re.I)
            detailed = WorkshopCatalogItem(item.workshop_id, item.title, item.author, item.preview_url, item.detail_url, html.unescape(description_match.group(1).strip()) if description_match else "", html.unescape(updated_match.group(1).strip()) if updated_match else "", item.installed)
            cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(json.dumps(asdict(detailed), ensure_ascii=False, indent=2), encoding="utf-8"); return detailed
        except Exception:
            if cached: return WorkshopCatalogItem(**cached)
            return item

    @staticmethod
    def parse_catalog(source: str) -> tuple[WorkshopCatalogItem, ...]:
        ids = list(dict.fromkeys(re.findall(r"sharedfiles/filedetails/\?id=(\d+)", source))); items = []
        for workshop_id in ids:
            marker = f"sharedfiles/filedetails/?id={workshop_id}"; index = source.find(marker)
            if index < 0: continue
            block_start = max(source.rfind('<div class="workshopItem"', 0, index), index - 600)
            next_item = source.find('<div class="workshopItem"', index + len(marker))
            block_end = next_item if next_item >= 0 else min(len(source), index + 3200)
            snippet = source[block_start:block_end]
            title_match = re.search(r'class="workshopItemTitle[^"]*"[^>]*>(.*?)</div>', snippet, re.I | re.S)
            if not title_match:
                title_match = re.search(rf'href="[^"]*sharedfiles/filedetails/\?id={workshop_id}"[^>]*>([^<]+)</a>', snippet, re.I)
            image_match = re.search(r'class="workshopItemPreviewImage[^"]*"[^>]+src="([^"]+)"', snippet, re.I)
            if not image_match:
                image_match = re.search(r'<img[^>]+src="([^"]+)"', snippet, re.I)
            author_match = re.search(r'class="workshopItemAuthorName[^"]*"[^>]*>(.*?)</div>', snippet, re.I | re.S)
            title = WorkshopCatalogService._plain_text(title_match.group(1)) if title_match else f"Workshop {workshop_id}"
            if title.lower().startswith("http"): title = f"Workshop {workshop_id}"
            author = WorkshopCatalogService._plain_text(author_match.group(1)) if author_match else ""
            author = re.sub(r"^By\s+", "", author, flags=re.I)
            items.append(WorkshopCatalogItem(workshop_id, title, author, html.unescape(image_match.group(1)) if image_match else "", f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"))
        return tuple(items)

    @staticmethod
    def _plain_text(fragment: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()

    @staticmethod
    def _read_cache(path: Path) -> dict[str, Any] | None:
        try: return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, ValueError): return None

    @staticmethod
    def _page_from_payload(payload: dict[str, Any], from_cache: bool) -> WorkshopCatalogPage:
        return WorkshopCatalogPage(tuple(WorkshopCatalogItem(**item) for item in payload.get("items", [])), int(payload.get("page", 1)), str(payload.get("query", "")), str(payload.get("sort", "trend")), from_cache, str(payload.get("fetched_at", "")))


class ModProvider(Protocol):
    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".palworld-console-write-{uuid_token()}"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def parse_workshop_id(value: str) -> str:
    text = value.strip()
    if text.isdigit():
        return text
    match = re.search(r"(?:[?&]id=|/filedetails/)(\d+)", text)
    if not match:
        raise ValueError("请输入有效的 Steam Workshop ID 或链接")
    return match.group(1)


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, dict):
        return tuple(str(key) for key in value)
    return tuple(str(item) for item in value)


def infer_mod_type(install_rules: tuple[str, ...] = (), files: tuple[str, ...] = (), source: str = "") -> tuple[str, str, bool]:
    """Return (type, runtime, requires_ue4ss) without executing package content."""
    rules = " ".join(install_rules).lower()
    names = " ".join(files).lower()
    if "nativemod" in rules or "native" in rules or "nativemods" in names:
        return "native", "windows", True
    if "ue4ss" in rules or "/mods/" in names or "\\mods\\" in names:
        return "ue4ss", "windows", True
    if any(name.endswith(".pak") for name in files) or "pak" in rules or source == "local-pak":
        return "pak", "windows", False
    if "workshop" in source or "dedicated" in rules or "server" in rules:
        return "official", "windows", False
    return "unknown", "windows", False


def parse_info_json(payload: dict[str, Any], source: str, digest: str = "", archive_path: str = "", files: tuple[str, ...] = ()) -> ModManifest:
    package = str(payload.get("PackageName") or payload.get("packageName") or "").strip()
    if not package:
        raise ValueError("Info.json 缺少 PackageName")
    rules = _as_list(payload.get("InstallRules") or payload.get("InstallRule"))
    normalized_rules = tuple(rule.lower() for rule in rules)
    server_supported = any("server" in rule or "dedicated" in rule for rule in normalized_rules)
    declared_type = str(payload.get("ModType") or payload.get("Type") or "").strip().lower()
    mod_type, runtime, requires_ue4ss = infer_mod_type(rules, files, source)
    if declared_type in {"official", "ue4ss", "native", "pak", "unknown"}:
        mod_type = declared_type
    requires_ue4ss = bool(payload.get("RequiresUE4SS", requires_ue4ss))
    return ModManifest(
        package_name=package,
        display_name=str(payload.get("Name") or payload.get("DisplayName") or package),
        version=str(payload.get("Version") or payload.get("version") or "未知"),
        source=source,
        workshop_id=str(payload.get("WorkshopId") or payload.get("WorkshopID") or ""),
        install_rules=rules,
        server_supported=server_supported,
        dependencies=_as_list(payload.get("Dependencies") or payload.get("RequiredMods")),
        conflicts=_as_list(payload.get("Conflicts") or payload.get("ConflictMods")),
        sha256=digest,
        archive_path=archive_path,
        mod_type=mod_type,
        runtime=str(payload.get("Runtime") or runtime),
        author=str(payload.get("Author") or payload.get("作者") or ""),
        source_url=str(payload.get("SourceUrl") or payload.get("URL") or ""),
        requires_ue4ss=requires_ue4ss,
        risk="高" if mod_type in {"native", "pak", "unknown"} else "中",
    )


class LocalArchiveProvider:
    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest:
        archive = Path(source)
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            raise ValueError("请选择有效的 ZIP 模组包")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = sha256_file(archive)
        target = cache_dir / f"{digest}.zip"
        if not target.exists():
            shutil.copy2(archive, target)
        with zipfile.ZipFile(target) as bundle:
            names = tuple(bundle.namelist())
            unsafe = [name for name in names if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts]
            if unsafe:
                raise ValueError("ZIP 包含不安全的路径，已拒绝导入")
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("ZIP 包含重复或大小写冲突的文件路径，已拒绝导入")
            if any((member.external_attr >> 16) & 0o170000 == 0o120000 for member in bundle.infolist()):
                raise ValueError("ZIP 包含符号链接，已拒绝导入")
            candidates = [name for name in names if PurePosixPath(name).name.lower() == "info.json"]
            if not candidates:
                raise ValueError("ZIP 中未找到 Info.json")
            if len(candidates) > 1:
                candidates.sort(key=lambda name: (len(PurePosixPath(name).parts), name))
            payload = json.loads(bundle.read(candidates[0]).decode("utf-8-sig"))
        return parse_info_json(payload, "local-zip", digest, str(target), names)


class LocalPakProvider:
    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest:
        pak = Path(source)
        if not pak.is_file() or pak.suffix.lower() != ".pak":
            raise ValueError("请选择有效的 PAK 文件")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = sha256_file(pak)
        target = cache_dir / f"{digest}-{pak.name}"
        if not target.exists():
            shutil.copy2(pak, target)
        return ModManifest(
            package_name=pak.stem, display_name=pak.stem, source="local-pak", sha256=digest,
            server_supported=False, metadata_complete=False, archive_path=str(target), mod_type="pak", runtime="windows", risk="高",
        )


class ModPackageService:
    """Imports server-mod packages without executing package-provided code."""

    MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

    def __init__(self, cache_dir: Path, timeout_seconds: int = 30):
        self.cache_dir = cache_dir
        self.timeout_seconds = timeout_seconds

    def prepare_directory(self, source: str | Path) -> ModManifest:
        root = Path(source).resolve()
        if not root.is_dir():
            raise ValueError("模组目录不存在")
        files = tuple(str(path.relative_to(root).as_posix()) for path in root.rglob("*") if path.is_file())
        info = next((path for path in root.rglob("Info.json") if path.is_file()), None)
        if not info:
            return ModManifest(root.name, root.name, source="local-directory", archive_path=str(root), metadata_complete=False, mod_type="unknown", risk="高")
        payload = json.loads(info.read_text(encoding="utf-8-sig"))
        digest = self._directory_hash(root)
        return parse_info_json(payload, "local-directory", digest, str(root), files)

    def prepare_url(self, url: str) -> ModManifest:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("请输入有效的 HTTP/HTTPS 模组地址")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url.strip(), headers={"User-Agent": "PalworldConsole/0.3"})
        suffix = Path(parsed.path).suffix.lower()
        temporary = self.cache_dir / f"download-{uuid_token()}{suffix or '.download'}"
        received = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                disposition = str(response.headers.get("Content-Disposition") or "")
                if not suffix:
                    filename_match = re.search(r"filename\s*=\s*\"?([^\";]+)", disposition, re.I)
                    filename = filename_match.group(1).strip() if filename_match else ""
                    suffix = Path(filename).suffix.lower()
                    if not suffix:
                        suffix = {"application/zip": ".zip", "application/x-zip-compressed": ".zip", "application/gzip": ".tgz", "application/x-tar": ".tar", "application/octet-stream": ".pak"}.get(content_type, "")
                    if suffix:
                        temporary = temporary.with_suffix(suffix)
                with temporary.open("wb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block: break
                        received += len(block)
                        if received > self.MAX_DOWNLOAD_BYTES: raise RuntimeError("模组下载超过 1 GB，已中止")
                        output.write(block)
            if temporary.suffix.lower() == ".pak":
                manifest = LocalPakProvider().prepare(temporary, self.cache_dir)
            elif temporary.suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"}:
                manifest = self._prepare_archive(temporary)
            else:
                raise ValueError("仅支持 ZIP、TAR、TGZ 或 PAK 模组包")
            manifest.source = "github-release" if "github.com" in parsed.netloc.lower() else "url"
            manifest.source_url = url.strip()
            manifest.validation_status = "awaiting_confirmation"
            return manifest
        finally:
            temporary.unlink(missing_ok=True)

    def _prepare_archive(self, archive: Path) -> ModManifest:
        if archive.suffix.lower() == ".zip":
            return LocalArchiveProvider().prepare(archive, self.cache_dir)
        extract_root = self.cache_dir / f"archive-{uuid_token()}"
        extract_root.mkdir(parents=True)
        try:
            with tarfile.open(archive, "r:*") as bundle:
                root = extract_root.resolve()
                for member in bundle.getmembers():
                    target = (extract_root / member.name).resolve()
                    if root not in target.parents and target != root:
                        raise ValueError("TAR 包含不安全路径，已拒绝导入")
                    if member.issym() or member.islnk():
                        raise ValueError("TAR 包含符号链接，已拒绝导入")
                bundle.extractall(extract_root, filter="data")
            return self.prepare_directory(extract_root)
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)

    @staticmethod
    def _directory_hash(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()


def uuid_token() -> str:
    return hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:20]


class WorkshopProvider:
    APP_ID = "1623730"

    def __init__(self, steamcmd: Path):
        self.steamcmd = steamcmd

    def prepare(self, source: str | Path, cache_dir: Path) -> ModManifest:
        workshop_id = parse_workshop_id(str(source))
        if not self.steamcmd.is_file():
            raise FileNotFoundError("未找到 SteamCMD，无法下载 Workshop 模组")
        download_root = cache_dir / "workshop"
        download_root.mkdir(parents=True, exist_ok=True)
        command = [str(self.steamcmd), "+force_install_dir", str(download_root), "+login", "anonymous", "+workshop_download_item", self.APP_ID, workshop_id, "validate", "+quit"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        if result.returncode:
            raise RuntimeError("Workshop 下载失败：" + (result.stderr.strip() or result.stdout.strip())[-800:])
        candidates = list(download_root.rglob("Info.json"))
        if not candidates:
            raise RuntimeError("Workshop 下载完成，但未找到 Info.json")
        info = sorted(candidates, key=lambda p: len(p.parts))[0]
        manifest = parse_info_json(json.loads(info.read_text(encoding="utf-8-sig")), "workshop")
        manifest.workshop_id = workshop_id
        manifest.archive_path = str(info.parent)
        manifest.sha256 = self._directory_hash(info.parent)
        return manifest

    def prepare_remote(self, client, source: str | Path, cache_dir: Path, remote_steamcmd: str, remote_root: str = "/tmp/palworld-console-workshop") -> ModManifest:
        """Download a Workshop item on a remote Wine host, then fetch a verified copy."""
        workshop_id = parse_workshop_id(str(source))
        if not remote_steamcmd.strip():
            raise FileNotFoundError("远程检测未找到 SteamCMD，无法下载 Workshop 模组")
        remote_root = f"{remote_root.rstrip('/')}-{workshop_id}"
        remote_archive = f"{remote_root}.tar.gz"
        qsteam, qroot, qarchive = (shlex.quote(value) for value in (remote_steamcmd, remote_root, remote_archive))
        command = (
            f"set -e; rm -rf {qroot} {qarchive}; mkdir -p {qroot}; "
            f"{qsteam} +force_install_dir {qroot} +login anonymous +workshop_download_item {self.APP_ID} {workshop_id} validate +quit; "
            f"info=$(find {qroot} -type f -name Info.json -print -quit); test -n \"$info\"; "
            f"tar -czf {qarchive} -C \"$(dirname \"$info\")\" ."
        )
        code, _output, error = client.run(command)
        if code:
            raise RuntimeError("远程 Workshop 下载失败：" + (error.strip() or "SteamCMD 返回非零退出码")[-800:])
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        local_archive = cache_dir / "workshop" / f"{workshop_id}.tar.gz"; local_archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            client.download_file(remote_archive, local_archive)
            extract_root = local_archive.parent / workshop_id
            if extract_root.exists(): shutil.rmtree(extract_root)
            extract_root.mkdir(parents=True)
            with tarfile.open(local_archive, "r:gz") as bundle:
                root = extract_root.resolve()
                for member in bundle.getmembers():
                    target = (extract_root / member.name).resolve()
                    if root not in target.parents and target != root:
                        raise RuntimeError("远程 Workshop 压缩包包含不安全路径")
                bundle.extractall(extract_root, filter="data")
            candidates = list(extract_root.rglob("Info.json"))
            if not candidates:
                raise RuntimeError("远程 Workshop 下载完成，但未找到 Info.json")
            info = sorted(candidates, key=lambda path: len(path.parts))[0]
            manifest = parse_info_json(json.loads(info.read_text(encoding="utf-8-sig")), "workshop", archive_path=str(info.parent))
            manifest.workshop_id = workshop_id
            manifest.sha256 = self._directory_hash(info.parent)
            return manifest
        finally:
            try: client.run(f"rm -rf {qroot} {qarchive}")
            except Exception: pass

    @staticmethod
    def _directory_hash(root: Path) -> str:
        digest = hashlib.sha256()
        for file in sorted(path for path in root.rglob("*") if path.is_file()):
            digest.update(file.relative_to(root).as_posix().encode())
            digest.update(file.read_bytes())
        return digest.hexdigest()


class ModManager:
    """Validates mod state and performs rollback-capable local deployments."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    @staticmethod
    def build_install_plan(manifest: ModManifest, environment: ModEnvironment, allow_unverified: bool = False) -> ModInstallPlan:
        if not environment.supported:
            return ModInstallPlan(manifest.mod_type, "", "", manifest.requires_ue4ss, False, True, environment.reason or "当前服务端环境不支持模组")
        mod_type = manifest.mod_type or "unknown"
        if mod_type == "official":
            root = Path(environment.mods_dir); target = root / (manifest.workshop_id or manifest.package_name)
            return ModInstallPlan(mod_type, str(target), str(root), False, True)
        if mod_type == "ue4ss":
            if not environment.ue4ss_mods_dir:
                return ModInstallPlan(mod_type, "", "", True, False, True, "未检测到 UE4SS/Mods 目录")
            root = Path(environment.ue4ss_mods_dir); return ModInstallPlan(mod_type, str(root / manifest.package_name), str(root), True, False)
        if mod_type == "native":
            if not environment.native_mods_dir:
                return ModInstallPlan(mod_type, "", "", True, False, True, "未检测到 UE4SS/NativeMods 目录")
            root = Path(environment.native_mods_dir); return ModInstallPlan(mod_type, str(root / manifest.package_name), str(root), True, False)
        if mod_type == "pak":
            if not environment.paks_dir:
                return ModInstallPlan(mod_type, "", "", False, False, True, "未检测到 Pal/Content/Paks 目录")
            filename = Path(manifest.archive_path).name or f"{manifest.package_name}.pak"
            root = Path(environment.paks_dir); return ModInstallPlan(mod_type, str(root / filename), str(root), False, False)
        if allow_unverified:
            return ModInstallPlan("unknown", "", "", False, False, True, "未知模组类型，只能诊断，不能自动部署")
        return ModInstallPlan(mod_type, "", "", manifest.requires_ue4ss, False, True, "无法确认模组类型或服务器安装规则")

    @staticmethod
    def _extract_zip_safe(source: Path, target: Path) -> None:
        with zipfile.ZipFile(source) as bundle:
            root = target.resolve()
            for member in bundle.infolist():
                destination = (target / member.filename).resolve()
                if root not in destination.parents and destination != root:
                    raise ValueError("模组 ZIP 包含不安全路径")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("模组 ZIP 包含符号链接")
            bundle.extractall(target)

    @staticmethod
    def detect_local(install_dir: Path, system_name: str = "Windows") -> ModEnvironment:
        root = install_dir.resolve() if install_dir.exists() else install_dir.absolute()
        exe = root / "PalServer.exe"
        if system_name.lower().startswith("win"):
            supported = exe.is_file()
            ue4ss = next((candidate for candidate in (root / "UE4SS", root / "ue4ss") if candidate.exists()), root / "UE4SS")
            mods = root / "Mods" / "Workshop"
            paks = root / "Pal" / "Content" / "Paks"
            writable = tuple(str(path) for path in (root, mods, paks, ue4ss) if path.exists() and _is_writable(path))
            return ModEnvironment(system_name, "windows", workshop_root=str(mods), mods_dir=str(mods), settings_path=str(root / "Mods" / "PalModSettings.ini"), palserver_exe=str(exe), supported=supported, reason="" if supported else "未找到 PalServer.exe", managed_mods_dir=str(root / "Mods" / "ManagedMods"), ue4ss_root=str(ue4ss), ue4ss_mods_dir=str(ue4ss / "Mods"), native_mods_dir=str(ue4ss / "NativeMods"), paks_dir=str(paks), ue4ss_config_path=str(ue4ss / "UE4SS-settings.ini"), writable_paths=writable, detected_at=datetime.now().isoformat(timespec="seconds"))
        return ModEnvironment(system_name, "linux-native", supported=False, reason="官方服务端模组不支持原生 Linux Dedicated Server")

    @staticmethod
    def detect_remote(profile: dict[str, Any]) -> ModEnvironment:
        system_name = str(profile.get("os") or profile.get("system") or "Linux")
        command = str(profile.get("service_exec") or profile.get("exec_start") or "")
        wine = str(profile.get("wine_path") or "")
        exe = str(profile.get("palserver_exe") or "")
        writable = bool(profile.get("mods_writable") and profile.get("settings_writable"))
        wine_mode = bool(wine and exe and re.search(r"\bwine(?:64)?\b", command, re.I))
        if wine_mode:
            supported = writable
            return ModEnvironment(system_name, "linux-wine", wine_path=wine, wine_version=str(profile.get("wine_version") or ""), workshop_root=str(profile.get("workshop_root") or ""), mods_dir=str(profile.get("mods_dir") or ""), settings_path=str(profile.get("mod_settings_path") or ""), palserver_exe=exe, supported=supported, experimental=True, reason="实验性 Wine 模式" if supported else "Wine 服务已检测到，但模组目录或配置不可写", managed_mods_dir=str(profile.get("managed_mods_dir") or ""), ue4ss_root=str(profile.get("ue4ss_root") or ""), ue4ss_mods_dir=str(profile.get("ue4ss_mods_dir") or ""), native_mods_dir=str(profile.get("native_mods_dir") or ""), paks_dir=str(profile.get("paks_dir") or ""), ue4ss_config_path=str(profile.get("ue4ss_config_path") or ""), writable_paths=tuple(profile.get("writable_paths") or ()), detected_at=datetime.now().isoformat(timespec="seconds"))
        return ModEnvironment(system_name, "linux-native", supported=False, reason="官方服务端模组不支持原生 Linux Dedicated Server")

    @staticmethod
    def validate_enable(manifest: ModManifest, installed: list[ModManifest], allow_unverified: bool = False) -> None:
        if not manifest.metadata_complete and not (allow_unverified and manifest.mod_type == "pak"):
            raise ValueError("PAK 缺少 Info.json，默认禁用；需明确选择目标目录并确认风险")
        if not manifest.server_supported:
            raise ValueError("该模组的 Info.json 未声明服务器安装规则，不能在服务端启用")
        enabled = {mod.package_name: mod for mod in installed if mod.enabled}
        missing = [name for name in manifest.dependencies if name not in enabled and name != manifest.package_name]
        if missing:
            raise ValueError("缺少依赖：" + "、".join(missing))
        conflicts = [name for name in manifest.conflicts if name in enabled]
        reverse = [mod.package_name for mod in enabled.values() if manifest.package_name in mod.conflicts]
        if conflicts or reverse:
            raise ValueError("模组冲突：" + "、".join(dict.fromkeys([*conflicts, *reverse])))

    def install_local(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], stop, start, health, allow_unverified: bool = False) -> ModManifest:
        if not environment.supported:
            raise RuntimeError(environment.reason or "当前服务端环境不支持模组")
        self.validate_enable(manifest, installed, allow_unverified)
        plan = self.build_install_plan(manifest, environment)
        if plan.read_only: raise RuntimeError(plan.reason or "模组未通过安装计划校验")
        mods_dir = Path(environment.mods_dir); settings = Path(environment.settings_path); mods_root = settings.parent; server_root = Path(environment.palserver_exe).parent; paks = Path(environment.paks_dir or (server_root / "Pal" / "Content" / "Paks")); ue4ss_root = Path(environment.ue4ss_root) if environment.ue4ss_root else server_root / "UE4SS"
        transaction_root = self.cache_dir / "transactions" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        transaction_root.mkdir(parents=True, exist_ok=True)
        backup_mods = transaction_root / "Mods"
        backup_paks = transaction_root / "Paks"
        backup_ue4ss = transaction_root / "UE4SS"
        backup_settings = transaction_root / "PalModSettings.ini"
        existed = {"mods": mods_root.exists(), "paks": paks.exists(), "ue4ss": ue4ss_root.exists(), "settings": settings.exists()}
        (transaction_root / "transaction.json").write_text(json.dumps({"action": "install", "package": manifest.package_name, "states": {mod.package_name: mod.enabled for mod in installed}, "existed": existed}, ensure_ascii=False, indent=2), encoding="utf-8")
        stop()
        try:
            if mods_root.exists():
                shutil.copytree(mods_root, backup_mods)
            if paks.exists(): shutil.copytree(paks, backup_paks)
            if ue4ss_root.exists(): shutil.copytree(ue4ss_root, backup_ue4ss)
            if settings.exists():
                shutil.copy2(settings, backup_settings)
            target = Path(plan.target)
            if plan.mod_type == "pak":
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
            source = Path(manifest.archive_path)
            if manifest.source == "local-zip":
                self._extract_zip_safe(source, target)
            elif source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif plan.mod_type == "pak":
                shutil.copy2(source, target)
            else:
                shutil.copy2(source, target / source.name)
            manifest.install_path = str(target)
            manifest.enabled = True
            if plan.changes_settings:
                settings.parent.mkdir(parents=True, exist_ok=True)
                enabled = [mod.package_name for mod in installed if mod.enabled and mod.package_name != manifest.package_name] + [manifest.package_name]
                settings.write_text(self._settings_text(enabled), encoding="utf-8")
            start()
            if not health():
                raise RuntimeError("模组部署后服务器健康检查失败")
            if manifest.source == "workshop" and plan.changes_settings:
                install_manifest = mods_root / "ManagedMods" / manifest.package_name / "InstallManifest.json"
                for _attempt in range(30):
                    if install_manifest.is_file(): break
                    time.sleep(1)
                else: raise RuntimeError(f"服务器已启动，但未生成模组部署清单: {install_manifest}")
            return manifest
        except Exception:
            if mods_root.exists():
                shutil.rmtree(mods_root)
            if backup_mods.exists():
                shutil.copytree(backup_mods, mods_root)
            if backup_paks.exists():
                if paks.exists(): shutil.rmtree(paks)
                shutil.copytree(backup_paks, paks)
            elif not existed["paks"] and paks.exists():
                shutil.rmtree(paks)
            if ue4ss_root.exists() and not existed["ue4ss"]:
                shutil.rmtree(ue4ss_root)
            if backup_ue4ss.exists():
                if ue4ss_root.exists(): shutil.rmtree(ue4ss_root)
                shutil.copytree(backup_ue4ss, ue4ss_root)
            if backup_settings.exists():
                settings.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_settings, settings)
            elif settings.exists():
                settings.unlink()
            try:
                start()
            finally:
                raise

    def install_remote(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], client, stop, start, health, allow_unverified: bool = False) -> ModManifest:
        if not environment.supported or environment.server_type != "linux-wine":
            raise RuntimeError(environment.reason or "远程主机未通过实验性 Wine 模组环境检测")
        self.validate_enable(manifest, installed, allow_unverified=allow_unverified)
        plan = self.build_install_plan(manifest, environment, allow_unverified=allow_unverified)
        if plan.read_only:
            raise RuntimeError(plan.reason or "模组未通过远程安装计划校验")
        mods_dir = environment.mods_dir
        settings = environment.settings_path
        if not mods_dir.startswith("/") or not settings.startswith("/"):
            raise ValueError("远程模组目录和配置路径必须是探测到的绝对路径")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"/tmp/palworld-mod-backup-{stamp}.tar.gz"
        upload = f"/tmp/palworld-mod-{stamp}.zip"
        source = Path(manifest.archive_path)
        temporary: tempfile.TemporaryDirectory | None = None
        if source.is_dir():
            temporary = tempfile.TemporaryDirectory(prefix="palworld-mod-")
            archive = Path(temporary.name) / "mod.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for file in source.rglob("*"):
                    if file.is_file(): bundle.write(file, file.relative_to(source))
            source = archive
        if source.suffix.lower() == ".pak":
            temporary = tempfile.TemporaryDirectory(prefix="palworld-mod-")
            archive = Path(temporary.name) / "mod.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle: bundle.write(source, source.name)
            source = archive
        server_root = str(PurePosixPath(environment.palserver_exe).parent); paks = environment.paks_dir or str(PurePosixPath(server_root) / "Pal" / "Content" / "Paks"); ue4ss_root = environment.ue4ss_root or str(PurePosixPath(server_root) / "UE4SS"); mods_root = str(PurePosixPath(settings).parent); backup_list = f"{backup}.list"; qmods, qsettings, qpaks, que4ss, qupload, qbackup, qlist = map(shlex.quote, (mods_root, settings, paks, ue4ss_root, upload, backup, backup_list))
        stop()
        try:
            backup_command = f"set -e; : > {qlist}; for p in {qmods} {qsettings} {qpaks} {que4ss}; do if [ -e \"$p\" ]; then printf '%s\\n' \"${{p#/}}\" >> {qlist}; fi; done; tar -C / -czf {qbackup} -T {qlist}; rm -f {qlist}"
            code, _out, error = client.run(backup_command)
            if code: raise RuntimeError(error.strip() or "创建远程模组回滚包失败")
            client.upload_file(source, upload)
            target = plan.target
            if plan.mod_type == "pak":
                command = f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))} && unzip -oq {qupload} -d {shlex.quote(str(PurePosixPath(target).parent))} && rm -f {qupload}"
            elif plan.mod_type in {"ue4ss", "native"}:
                command = f"mkdir -p {shlex.quote(target)} && unzip -oq {qupload} -d {shlex.quote(target)} && rm -f {qupload}"
            else:
                command = f"mkdir -p {shlex.quote(target)} && unzip -oq {qupload} -d {shlex.quote(target)} && rm -f {qupload}"
            code, _out, error = client.run(command)
            if code: raise RuntimeError(error.strip() or "远程模组文件部署失败")
            if plan.changes_settings:
                enabled = [mod.package_name for mod in installed if mod.enabled and mod.package_name != manifest.package_name] + [manifest.package_name]
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ini", delete=False) as handle:
                    handle.write(self._settings_text(enabled)); local_settings = Path(handle.name)
                remote_tmp = f"{settings}.palworld-console.tmp"
                client.upload_file(local_settings, remote_tmp); local_settings.unlink(missing_ok=True)
                code, _out, error = client.run(f"mkdir -p {shlex.quote(str(PurePosixPath(settings).parent))} && mv {shlex.quote(remote_tmp)} {qsettings}")
                if code: raise RuntimeError(error.strip() or "更新远程 PalModSettings.ini 失败")
            start()
            if not health(): raise RuntimeError("模组部署后服务器健康检查失败")
            if manifest.source == "workshop" and plan.changes_settings:
                managed = str(PurePosixPath(mods_root) / "ManagedMods" / manifest.package_name / "InstallManifest.json")
                code, _out, _error = client.run(f"for i in $(seq 1 30); do test -f {shlex.quote(managed)} && exit 0; sleep 1; done; exit 1")
                if code: raise RuntimeError(f"服务器已启动，但未生成模组部署清单: {managed}")
            manifest.install_path = target; manifest.enabled = True
            return manifest
        except Exception as exc:
            restore_code, _out, restore_error = client.run(f"rm -rf {qmods} {qsettings}; test -f {qbackup}; tar -xzf {qbackup} -C /; rm -f {qupload} {qbackup}")
            if restore_code:
                raise RuntimeError(f"远程模组部署失败，且回滚失败：{restore_error.strip() or '无法恢复远程备份'}；原错误：{exc}") from exc
            try: start()
            finally: raise
        finally:
            if temporary: temporary.cleanup()

    @staticmethod
    def _enabled_text(installed: list[ModManifest], excluded: str = "") -> str:
        enabled = [mod.package_name for mod in installed if mod.enabled and mod.package_name != excluded]
        return ModManager._settings_text(enabled)

    @staticmethod
    def _settings_text(enabled: list[str]) -> str:
        lines = ["[PalModSettings]", f"bGlobalEnableMod={'true' if enabled else 'false'}"]
        lines.extend(f"ActiveModList={package}" for package in dict.fromkeys(enabled))
        return "\n".join(lines) + "\n"

    def change_local(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], remove: bool, stop, start, health) -> None:
        if not environment.supported: raise RuntimeError(environment.reason or "当前环境不支持模组")
        settings = Path(environment.settings_path); target = Path(manifest.install_path) if manifest.install_path else Path(environment.mods_dir) / (manifest.workshop_id or manifest.package_name)
        transaction = self.cache_dir / "transactions" / datetime.now().strftime("%Y%m%d-%H%M%S-%f"); transaction.mkdir(parents=True, exist_ok=True)
        backup_settings = transaction / "PalModSettings.ini"; backup_target = transaction / "mod"
        (transaction / "transaction.json").write_text(json.dumps({"action": "remove" if remove else "disable", "package": manifest.package_name, "target": str(target), "states": {mod.package_name: mod.enabled for mod in installed}}, ensure_ascii=False, indent=2), encoding="utf-8")
        stop()
        try:
            if settings.exists(): shutil.copy2(settings, backup_settings)
            if remove and target.exists(): shutil.copytree(target, backup_target)
            settings.parent.mkdir(parents=True, exist_ok=True); settings.write_text(self._enabled_text(installed, manifest.package_name), encoding="utf-8")
            if remove and target.exists(): shutil.rmtree(target)
            start()
            if not health(): raise RuntimeError("模组变更后服务器健康检查失败")
        except Exception:
            if backup_settings.exists(): shutil.copy2(backup_settings, settings)
            if remove and backup_target.exists():
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(backup_target, target)
            try: start()
            finally: raise

    def rollback_latest_local(self, environment: ModEnvironment, stop, start, health) -> dict[str, bool]:
        root = self.cache_dir / "transactions"
        candidates = sorted((path for path in root.glob("*") if (path / "transaction.json").is_file() and not (path / "rolled-back").exists()), reverse=True) if root.exists() else []
        if not candidates: raise FileNotFoundError("没有可回滚的本机模组事务")
        transaction = candidates[0]; metadata = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        mods_dir = Path(environment.mods_dir); settings = Path(environment.settings_path); mods_root = settings.parent; paks = Path(environment.palserver_exe).parent / "Pal" / "Content" / "Paks"; backup_mods = transaction / "Mods"; backup_target = transaction / "mod"; backup_paks = transaction / "Paks"; existed = dict(metadata.get("existed") or {})
        stop()
        try:
            if backup_mods.exists():
                if mods_root.exists(): shutil.rmtree(mods_root)
                shutil.copytree(backup_mods, mods_root)
                if backup_paks.exists():
                    if paks.exists(): shutil.rmtree(paks)
                    shutil.copytree(backup_paks, paks)
                elif existed.get("paks") is False and paks.exists():
                    shutil.rmtree(paks)
            elif backup_target.exists():
                target = Path(str(metadata.get("target") or ""))
                if not target.is_absolute(): raise RuntimeError("回滚记录缺少可信的模组绝对路径")
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(backup_target, target)
            backup_settings = transaction / "PalModSettings.ini"
            if backup_settings.exists():
                settings.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(backup_settings, settings)
            start()
            if not health(): raise RuntimeError("回滚后服务器健康检查失败")
            (transaction / "rolled-back").write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            return {str(key): bool(value) for key, value in dict(metadata.get("states") or {}).items()}
        except Exception:
            try: start()
            finally: raise

    def change_remote(self, manifest: ModManifest, environment: ModEnvironment, installed: list[ModManifest], client, remove: bool, stop, start, health) -> None:
        if not environment.supported or environment.server_type != "linux-wine": raise RuntimeError(environment.reason or "远程 Wine 模组环境不可用")
        settings = environment.settings_path; target = manifest.install_path or str(PurePosixPath(environment.mods_dir) / (manifest.workshop_id or manifest.package_name))
        backup = f"/tmp/palworld-mod-change-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
        stop()
        try:
            client.run(f"tar -czf {shlex.quote(backup)} {shlex.quote(settings)} {shlex.quote(target)} 2>/dev/null || true")
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ini", delete=False) as handle:
                handle.write(self._enabled_text(installed, manifest.package_name)); local = Path(handle.name)
            remote_tmp = settings + ".palworld-console.tmp"; client.upload_file(local, remote_tmp); local.unlink(missing_ok=True)
            command = f"mkdir -p {shlex.quote(str(PurePosixPath(settings).parent))} && mv {shlex.quote(remote_tmp)} {shlex.quote(settings)}"
            if remove: command += f" && rm -rf {shlex.quote(target)}"
            code, _out, error = client.run(command)
            if code: raise RuntimeError(error.strip() or "远程模组变更失败")
            start()
            if not health(): raise RuntimeError("模组变更后服务器健康检查失败")
        except Exception:
            client.run(f"tar -xzf {shlex.quote(backup)} -C / 2>/dev/null || true")
            try: start()
            finally: raise
