from __future__ import annotations

import ntpath
import shutil
import shlex
import tarfile
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backup_packages import BackupPackageService


@dataclass(frozen=True)
class WorldMutationResult:
    backup_path: str
    world_path: str
    action: str
    counts: dict[str, int] = field(default_factory=dict)
    restarted: bool = True
    rolled_back: bool = False


class WorldDirectoryTransaction:
    """Atomic world-directory replacement for mutations that include player files."""

    @staticmethod
    def _validated_backup(create_backup: Callable[[], Any]) -> Path:
        package = Path(create_backup())
        BackupPackageService().validate(package)
        return package

    @staticmethod
    def _validate_candidate(path: Path) -> None:
        level = path / "Level.sav"
        if not level.is_file() or level.stat().st_size == 0:
            raise RuntimeError("清理候选目录缺少有效 Level.sav")
        if any(item.is_symlink() for item in path.rglob("*")):
            raise RuntimeError("清理候选目录包含符号链接")

    def execute_local(
        self,
        world_path: Path,
        build_candidate: Callable[[Path, Path], dict[str, Any]],
        create_backup: Callable[[], Any],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
    ) -> WorldMutationResult:
        world = Path(world_path).resolve()
        if not (world / "Level.sav").is_file(): raise FileNotFoundError(f"活动世界无效：{world}")
        token = uuid.uuid4().hex; staging = world.parent / f".{world.name}.pwc-stage-{token}"; rollback = world.parent / f".{world.name}.pwc-rollback-{token}"; failed = world.parent / f".{world.name}.pwc-failed-{token}"
        stop(); package: Path | None = None; deployed = False
        try:
            package = self._validated_backup(create_backup)
            details = build_candidate(world, staging) or {}
            self._validate_candidate(staging)
            world.rename(rollback); staging.rename(world); deployed = True
            start()
            if not health(): raise RuntimeError("清理后服务器健康检查失败")
            shutil.rmtree(rollback)
            return WorldMutationResult(str(package), str(world), "cleanup", {str(k): int(v) for k, v in details.get("counts", {}).items()})
        except Exception as exc:
            if not deployed:
                shutil.rmtree(staging, ignore_errors=True)
                try: start()
                except Exception as start_exc: raise RuntimeError(f"候选构建失败且服务器无法恢复启动：{start_exc}；原错误：{exc}") from exc
                raise RuntimeError(f"候选世界未部署：{exc}") from exc
            try:
                try: stop()
                except Exception: pass
                if world.exists(): world.rename(failed)
                rollback.rename(world)
                start()
                shutil.rmtree(failed, ignore_errors=True)
            except Exception as rollback_exc:
                raise RuntimeError(f"世界目录回滚失败，服务器已保持停止：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"清理后验证失败，已恢复原世界：{exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def reset_local(
        self,
        world_path: Path,
        create_backup: Callable[[], Any],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
        timeout: float = 120,
    ) -> WorldMutationResult:
        world = Path(world_path).resolve()
        if not (world / "Level.sav").is_file(): raise FileNotFoundError(f"活动世界无效：{world}")
        rollback = world.parent / f".{world.name}.pwc-reset-{uuid.uuid4().hex}"
        stop(); package = self._validated_backup(create_backup); world.rename(rollback)
        try:
            start(); deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not (world / "Level.sav").is_file(): time.sleep(1)
            if not (world / "Level.sav").is_file(): raise RuntimeError("服务器未在期限内生成新 Level.sav")
            if not health(): raise RuntimeError("新世界服务器健康检查失败")
            shutil.rmtree(rollback)
            return WorldMutationResult(str(package), str(world), "reset")
        except Exception as exc:
            try:
                try: stop()
                except Exception: pass
                shutil.rmtree(world, ignore_errors=True); rollback.rename(world); start()
            except Exception as rollback_exc:
                raise RuntimeError(f"世界重置回滚失败，服务器已保持停止：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"新世界验证失败，已恢复原世界：{exc}") from exc

    def execute_remote(
        self,
        client,
        remote_world: str,
        platform_name: str,
        build_candidate: Callable[[Path, Path], dict[str, Any]],
        create_backup: Callable[[], Any],
        stop: Callable[[], None],
        start: Callable[[], None],
        health: Callable[[], bool],
    ) -> WorldMutationResult:
        platform_name = platform_name.casefold(); package: Path | None = None; deployed = False
        token = uuid.uuid4().hex
        join = ntpath.join if platform_name == "windows" else lambda a, b: f"{a.rstrip('/')}/{b}"
        parent = ntpath.dirname(remote_world) if platform_name == "windows" else remote_world.rsplit("/", 1)[0]
        name = ntpath.basename(remote_world) if platform_name == "windows" else remote_world.rstrip("/").rsplit("/", 1)[-1]
        staging = join(parent, f".{name}.pwc-stage-{token}"); rollback = join(parent, f".{name}.pwc-rollback-{token}"); archive_remote = join(parent, f".pwc-upload-{token}.zip" if platform_name == "windows" else f".pwc-upload-{token}.tar.gz")
        stop()
        try:
            package = self._validated_backup(create_backup)
            with tempfile.TemporaryDirectory(prefix="palworld-cleanup-") as temp_name:
                temp = Path(temp_name); extracted = temp / "backup"; BackupPackageService().extract(package, extracted, ("world",))
                candidates = [path.parent for path in (extracted / "payload" / "savegames").rglob("Level.sav") if path.parent.name == name]
                if len(candidates) != 1: raise RuntimeError(f"备份中无法唯一定位活动世界 {name}")
                candidate = temp / "candidate"; details = build_candidate(candidates[0], candidate) or {}; self._validate_candidate(candidate)
                archive = temp / ("candidate.zip" if platform_name == "windows" else "candidate.tar.gz")
                if platform_name == "windows":
                    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                        for path in candidate.rglob("*"):
                            if path.is_file(): bundle.write(path, path.relative_to(candidate))
                else:
                    with tarfile.open(archive, "w:gz") as bundle:
                        for path in candidate.iterdir(): bundle.add(path, arcname=path.name, recursive=True)
                client.upload_file(archive, archive_remote)
            if platform_name == "windows":
                from .services import RemoteHostClient
                q = RemoteHostClient._ps_literal
                script = f"$ErrorActionPreference='Stop';Remove-Item -LiteralPath {q(staging)},{q(rollback)} -Recurse -Force -ErrorAction SilentlyContinue;New-Item -ItemType Directory -Path {q(staging)}|Out-Null;Expand-Archive -LiteralPath {q(archive_remote)} -DestinationPath {q(staging)} -Force;Move-Item -LiteralPath {q(remote_world)} -Destination {q(rollback)};Move-Item -LiteralPath {q(staging)} -Destination {q(remote_world)}"
                code, output, error = client.run_powershell(script)
            else:
                command = f"set -e; rm -rf -- {shlex.quote(staging)} {shlex.quote(rollback)}; mkdir -p -- {shlex.quote(staging)}; tar -xzf {shlex.quote(archive_remote)} -C {shlex.quote(staging)}; mv -- {shlex.quote(remote_world)} {shlex.quote(rollback)}; mv -- {shlex.quote(staging)} {shlex.quote(remote_world)}"
                code, output, error = client.run(command)
            if code: raise RuntimeError(error.strip() or output.strip() or "远程世界替换失败")
            deployed = True; start()
            if not health(): raise RuntimeError("清理后服务器健康检查失败")
            self._remote_cleanup(client, platform_name, rollback, staging, archive_remote)
            return WorldMutationResult(str(package), remote_world, "cleanup", {str(k): int(v) for k, v in details.get("counts", {}).items()})
        except Exception as exc:
            if deployed:
                try:
                    try: stop()
                    except Exception: pass
                    self._remote_rollback(client, platform_name, remote_world, rollback, staging, archive_remote); start()
                except Exception as rollback_exc: raise RuntimeError(f"远程世界回滚失败，服务器已保持停止：{rollback_exc}；原错误：{exc}") from exc
                raise RuntimeError(f"清理后验证失败，已恢复原世界：{exc}") from exc
            self._remote_cleanup(client, platform_name, rollback, staging, archive_remote)
            try: start()
            except Exception as start_exc: raise RuntimeError(f"候选构建失败且服务器无法恢复启动：{start_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"候选世界未部署：{exc}") from exc

    @staticmethod
    def _remote_cleanup(client, platform_name: str, *paths: str) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            quoted = ",".join(RemoteHostClient._ps_literal(path) for path in paths)
            client.run_powershell(f"Remove-Item -LiteralPath {quoted} -Recurse -Force -ErrorAction SilentlyContinue")
        else: client.run("rm -rf -- " + " ".join(shlex.quote(path) for path in paths))

    @classmethod
    def _remote_rollback(cls, client, platform_name: str, world: str, rollback: str, staging: str, archive: str) -> None:
        if platform_name == "windows":
            from .services import RemoteHostClient
            q = RemoteHostClient._ps_literal; code, output, error = client.run_powershell(f"$ErrorActionPreference='Stop';Remove-Item -LiteralPath {q(world)} -Recurse -Force -ErrorAction SilentlyContinue;Move-Item -LiteralPath {q(rollback)} -Destination {q(world)};Remove-Item -LiteralPath {q(staging)},{q(archive)} -Recurse -Force -ErrorAction SilentlyContinue")
        else: code, output, error = client.run(f"set -e; rm -rf -- {shlex.quote(world)}; mv -- {shlex.quote(rollback)} {shlex.quote(world)}; rm -rf -- {shlex.quote(staging)} {shlex.quote(archive)}")
        if code: raise RuntimeError(error.strip() or output.strip() or "远程世界目录恢复失败")

    def reset_remote(self, client, remote_world: str, platform_name: str, create_backup: Callable[[], Any], stop: Callable[[], None], start: Callable[[], None], health: Callable[[], bool], timeout: float = 120) -> WorldMutationResult:
        platform_name = platform_name.casefold(); token = uuid.uuid4().hex
        join = ntpath.join if platform_name == "windows" else lambda a, b: f"{a.rstrip('/')}/{b}"
        parent = ntpath.dirname(remote_world) if platform_name == "windows" else remote_world.rsplit("/", 1)[0]; name = ntpath.basename(remote_world) if platform_name == "windows" else remote_world.rstrip("/").rsplit("/", 1)[-1]; rollback = join(parent, f".{name}.pwc-reset-{token}")
        stop(); package = self._validated_backup(create_backup)
        if platform_name == "windows":
            from .services import RemoteHostClient
            q = RemoteHostClient._ps_literal; code, output, error = client.run_powershell(f"Move-Item -LiteralPath {q(remote_world)} -Destination {q(rollback)}")
        else: code, output, error = client.run(f"mv -- {shlex.quote(remote_world)} {shlex.quote(rollback)}")
        if code:
            start(); raise RuntimeError(error.strip() or output.strip() or "无法暂存原世界")
        try:
            start(); deadline = time.monotonic() + timeout; exists = False
            while time.monotonic() < deadline:
                if platform_name == "windows":
                    q = RemoteHostClient._ps_literal; code, output, _ = client.run_powershell(f"if(Test-Path -LiteralPath {q(join(remote_world, 'Level.sav'))} -PathType Leaf){{'yes'}}")
                else: code, output, _ = client.run(f"test -f {shlex.quote(join(remote_world, 'Level.sav'))} && printf yes")
                if not code and output.strip().endswith("yes"): exists = True; break
                time.sleep(1)
            if not exists: raise RuntimeError("服务器未在期限内生成新 Level.sav")
            if not health(): raise RuntimeError("新世界服务器健康检查失败")
            self._remote_cleanup(client, platform_name, rollback)
            return WorldMutationResult(str(package), remote_world, "reset")
        except Exception as exc:
            try:
                try: stop()
                except Exception: pass
                self._remote_rollback(client, platform_name, remote_world, rollback, "", ""); start()
            except Exception as rollback_exc: raise RuntimeError(f"世界重置回滚失败，服务器已保持停止：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"新世界验证失败，已恢复原世界：{exc}") from exc
