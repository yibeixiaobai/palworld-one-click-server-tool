import json
from pathlib import Path
from typing import Iterable

try:
    import keyring
except ImportError:  # optional during headless tests
    keyring = None

from .models import ServerInstance


class AppStorage:
    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".palworld-console"
        self.root.mkdir(parents=True, exist_ok=True)
        self.instances_file = self.root / "instances.json"

    def load_instances(self) -> list[ServerInstance]:
        if not self.instances_file.exists():
            return []
        try:
            raw = json.loads(self.instances_file.read_text(encoding="utf-8"))
            return [ServerInstance.from_dict(item) for item in raw]
        except (OSError, ValueError, TypeError):
            return []

    def save_instances(self, instances: Iterable[ServerInstance]) -> None:
        tmp = self.instances_file.with_suffix(".tmp")
        tmp.write_text(json.dumps([i.to_dict() for i in instances], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.instances_file)

    def set_secret(self, ref: str, value: str) -> str:
        if not ref:
            raise ValueError("凭据引用不能为空")
        if keyring is None:
            raise RuntimeError("未安装 keyring，无法保存系统凭据")
        keyring.set_password("palworld-console", ref, value)
        return ref

    def get_secret(self, ref: str) -> str:
        if not ref or keyring is None:
            return ""
        return keyring.get_password("palworld-console", ref) or ""

    def delete_secret(self, ref: str) -> None:
        if ref and keyring is not None:
            try:
                keyring.delete_password("palworld-console", ref)
            except Exception:
                pass
