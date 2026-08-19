from pathlib import Path
import threading

import pytest

from palworld_console import __version__
from palworld_console.updater import DownloadCancelled, UpdateError, UpdateService
from palworld_console.versioning import bump_version


class Response:
    def __init__(self, payload=None, text="", content=b"", status_code=200, content_length=None):
        self._payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(content) if content_length is None else content_length)}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def iter_content(self, chunk_size=1):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]


class Session:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


def release_payload(version="0.3.1", *, prerelease=False):
    installer = f"PalworldConsole-Setup-v{version}.exe"
    return {
        "tag_name": f"v{version}",
        "name": f"v{version}",
        "body": "修复问题",
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            {"name": installer, "browser_download_url": "https://github.com/example/installer.exe", "size": 4},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/example/SHA256SUMS.txt"},
        ],
    }


def test_version_is_read_from_single_source():
    assert __version__ == Path("palworld_console/VERSION").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize(("bump_type", "expected"), (("patch", "0.3.1"), ("minor", "0.4.0"), ("major", "1.0.0")))
def test_version_bump_types(bump_type, expected):
    assert bump_version("v0.3.0", bump_type) == expected


def test_version_bump_rejects_non_release_version():
    with pytest.raises(ValueError, match="X.Y.Z"):
        bump_version("0.3.0rc1")


def test_check_latest_requires_new_stable_release(tmp_path):
    payload = release_payload()
    checksum = "81f8aefd4e8c8c3c7a9f3f2e9b7e4c4d1bb8f15ec4b0d4c4e3b4d8d1cfe4b5aa  PalworldConsole-Setup-v0.3.1.exe\n"
    session = Session([Response(payload), Response(text=checksum), Response(payload)])
    service = UpdateService(storage_root=tmp_path, session=session)
    # The fixture's digest is intentionally invalid for the download, but release discovery succeeds.
    info = service.check_latest("0.3.0")
    assert info is not None
    assert info.version_text == "0.3.1"
    assert service.check_latest("0.3.1") is None


def test_check_latest_ignores_prerelease(tmp_path):
    payload = release_payload(prerelease=True)
    service = UpdateService(storage_root=tmp_path, session=Session([Response(payload)]))
    assert service.check_latest("0.3.0") is None


def test_download_verifies_sha256_and_cleans_old_installers(tmp_path):
    content = b"setup"
    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    payload = release_payload()
    checksum = f"{digest} *PalworldConsole-Setup-v0.3.1.exe\n"
    session = Session([Response(payload), Response(text=checksum), Response(content=content)])
    service = UpdateService(storage_root=tmp_path, session=session)
    info = service.check_latest("0.3.0")
    old = tmp_path / "updates" / "PalworldConsole-Setup-v0.3.0.exe"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    destination = service.download_installer(info)
    assert destination.read_bytes() == content
    assert not old.exists()
    assert not destination.with_suffix(".exe.part").exists()


def test_download_cancel_removes_partial_file(tmp_path):
    content = b"setup"
    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    from palworld_console.updater import ReleaseInfo
    from packaging.version import Version

    info = ReleaseInfo(Version("0.3.1"), "v0.3.1", "", "", "PalworldConsole-Setup-v0.3.1.exe", "https://example.test/setup", len(content), digest)
    cancel = threading.Event(); cancel.set()
    service = UpdateService(storage_root=tmp_path, session=Session([Response(content=content)]))
    with pytest.raises(DownloadCancelled):
        service.download_installer(info, cancel=cancel)
    assert not (tmp_path / "updates" / "PalworldConsole-Setup-v0.3.1.exe.part").exists()


def test_download_rejects_content_length_mismatch(tmp_path):
    import hashlib
    from packaging.version import Version
    from palworld_console.updater import ReleaseInfo

    content = b"short"
    info = ReleaseInfo(Version("0.3.1"), "v0.3.1", "", "", "PalworldConsole-Setup-v0.3.1.exe", "https://example.test/setup", 99, hashlib.sha256(content).hexdigest())
    service = UpdateService(storage_root=tmp_path, session=Session([Response(content=content, content_length=99)]))
    with pytest.raises(UpdateError, match="长度不符"):
        service.download_installer(info)
    assert not (tmp_path / "updates" / "PalworldConsole-Setup-v0.3.1.exe.part").exists()


def test_check_latest_rejects_missing_checksum(tmp_path):
    payload = release_payload()
    payload["assets"] = payload["assets"][:1]
    service = UpdateService(storage_root=tmp_path, session=Session([Response(payload)]))
    with pytest.raises(UpdateError, match="缺少安装器或 SHA256SUMS"):
        service.check_latest("0.3.0")
