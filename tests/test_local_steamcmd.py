from pathlib import Path
from types import SimpleNamespace

import pytest

from palworld_console.services import LocalSteamCmdManager


def test_local_steamcmd_allows_a_normal_drive_child_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("palworld_console.services.shutil.disk_usage", lambda _path: SimpleNamespace(free=8 * 1024**3))
    result = LocalSteamCmdManager.validate_install_dir(tmp_path / "PalworldServer")
    assert result.name == "PalworldServer"


def test_local_steamcmd_rejects_root_and_project_directory(monkeypatch):
    with pytest.raises(ValueError):
        LocalSteamCmdManager.validate_install_dir(Path(Path.cwd().anchor))
    with pytest.raises(ValueError):
        LocalSteamCmdManager.validate_install_dir(Path(__file__).resolve().parents[1])


def test_local_steamcmd_prepare_extracts_archive_and_self_updates(tmp_path, monkeypatch):
    install = tmp_path / "PalworldServer"
    manager = LocalSteamCmdManager()
    monkeypatch.setattr("palworld_console.services.shutil.disk_usage", lambda _path: SimpleNamespace(free=8 * 1024**3))

    class Response:
        headers = {"Content-Length": "4"}

        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size=-1):
            if getattr(self, "done", False): return b""
            self.done = True
            return b"PK\x03\x04"

    # The ZIP extractor is patched separately; this test focuses on manager state and retry flow.
    monkeypatch.setattr("palworld_console.services.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("palworld_console.services.zipfile.ZipFile", lambda *_args, **_kwargs: FakeZip())
    monkeypatch.setattr("palworld_console.services.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))

    class FakeZip:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def infolist(self): return [SimpleNamespace(filename="steamcmd.exe")]
        def extractall(self, root):
            (Path(root) / "steamcmd.exe").write_bytes(b"x" * 100_000)

    state = manager.prepare(install)
    assert state.ready is True and state.downloaded is True
    assert Path(state.executable).parent == install / "_tools" / "steamcmd"


def test_local_steamcmd_rejects_zip_traversal(tmp_path, monkeypatch):
    install = tmp_path / "PalworldServer"
    monkeypatch.setattr("palworld_console.services.shutil.disk_usage", lambda _path: SimpleNamespace(free=8 * 1024**3))

    class BadZip:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def infolist(self): return [SimpleNamespace(filename="../../evil.exe")]
        def extractall(self, _root): raise AssertionError("must reject before extraction")

    class Response:
        headers = {"Content-Length": "1"}
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size=-1):
            if hasattr(self, "done"): return b""
            self.done = True
            return b"x"

    monkeypatch.setattr("palworld_console.services.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("palworld_console.services.zipfile.ZipFile", lambda *_args, **_kwargs: BadZip())
    with pytest.raises(RuntimeError, match="不安全路径"):
        LocalSteamCmdManager().prepare(install)
