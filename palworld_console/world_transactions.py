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

    @staticmethod
    def _local_savegames_root(world: Path) -> Path:
        for candidate in (world, *world.parents):
            if candidate.name.casefold() == "savegames":
                return candidate
        raise ValueError(f"活动世界不在 SaveGames 目录内：{world}")

    @staticmethod
    def _remote_savegames_root(remote_world: str, platform_name: str) -> str:
        if platform_name == "windows":
            candidate = ntpath.normpath(remote_world)
            while candidate:
                if ntpath.basename(candidate).casefold() == "savegames":
                    return candidate
                parent = ntpath.dirname(candidate)
                if parent == candidate:
                    break
                candidate = parent
        else:
            candidate = remote_world.rstrip("/") or "/"
            while candidate:
                if candidate.rsplit("/", 1)[-1].casefold() == "savegames":
                    return candidate
                parent = candidate.rsplit("/", 1)[0] or "/"
                if parent == candidate:
                    break
                candidate = parent
        raise ValueError(f"活动世界不在 SaveGames 目录内：{remote_world}")

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
        savegames = self._local_savegames_root(world)
        rollback = savegames.parent / f".{savegames.name}.pwc-reset-{uuid.uuid4().hex}"
        stop()
        try:
            package = self._validated_backup(create_backup)
        except Exception:
            start()
            raise
        savegames.rename(rollback)
        try:
            start(); deadline = time.monotonic() + timeout
            generated: Path | None = None
            while time.monotonic() < deadline:
                generated = next(savegames.rglob("Level.sav"), None) if savegames.is_dir() else None
                if generated is not None:
                    break
                time.sleep(1)
            if generated is None: raise RuntimeError("服务器未在期限内生成新 Level.sav")
            if not health(): raise RuntimeError("新世界服务器健康检查失败")
            shutil.rmtree(rollback)
            return WorldMutationResult(str(package), str(generated.parent), "reset")
        except Exception as exc:
            try:
                try: stop()
                except Exception: pass
                shutil.rmtree(savegames, ignore_errors=True); rollback.rename(savegames); start()
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
        savegames = self._remote_savegames_root(remote_world, platform_name)
        parent = ntpath.dirname(savegames) if platform_name == "windows" else savegames.rsplit("/", 1)[0]
        rollback = join(parent, f".SaveGames.pwc-reset-{token}")
        stop()
        try:
            package = self._validated_backup(create_backup)
        except Exception:
            start()
            raise
        if platform_name == "windows":
            from .services import RemoteHostClient
            q = RemoteHostClient._ps_literal; code, output, error = client.run_powershell(f"Move-Item -LiteralPath {q(savegames)} -Destination {q(rollback)}")
        else: code, output, error = client.run(f"mv -- {shlex.quote(savegames)} {shlex.quote(rollback)}")
        if code:
            start(); raise RuntimeError(error.strip() or output.strip() or "无法暂存原世界")
        try:
            start(); deadline = time.monotonic() + timeout; generated_world = ""
            while time.monotonic() < deadline:
                if platform_name == "windows":
                    code, output, _ = client.run_powershell(f"$p=Get-ChildItem -LiteralPath {q(savegames)} -Filter 'Level.sav' -File -Recurse -ErrorAction SilentlyContinue|Select-Object -First 1 -ExpandProperty DirectoryName;if($p){{$p}}")
                else:
                    code, output, _ = client.run(f"p=$(find {shlex.quote(savegames)} -type f -name Level.sav -print -quit 2>/dev/null); test -n \"$p\" && dirname \"$p\"")
                if not code and output.strip(): generated_world = output.strip().splitlines()[-1].strip(); break
                time.sleep(1)
            if not generated_world: raise RuntimeError("服务器未在期限内生成新 Level.sav")
            if not health(): raise RuntimeError("新世界服务器健康检查失败")
            self._remote_cleanup(client, platform_name, rollback)
            return WorldMutationResult(str(package), generated_world, "reset")
        except Exception as exc:
            try:
                try: stop()
                except Exception: pass
                if platform_name == "windows":
                    code, output, error = client.run_powershell(f"$ErrorActionPreference='Stop';Remove-Item -LiteralPath {q(savegames)} -Recurse -Force -ErrorAction SilentlyContinue;Move-Item -LiteralPath {q(rollback)} -Destination {q(savegames)}")
                else:
                    code, output, error = client.run(f"set -e; rm -rf -- {shlex.quote(savegames)}; mv -- {shlex.quote(rollback)} {shlex.quote(savegames)}")
                if code: raise RuntimeError(error.strip() or output.strip() or "远程 SaveGames 目录恢复失败")
                start()
            except Exception as rollback_exc: raise RuntimeError(f"世界重置回滚失败，服务器已保持停止：{rollback_exc}；原错误：{exc}") from exc
            raise RuntimeError(f"新世界验证失败，已恢复原世界：{exc}") from exc
