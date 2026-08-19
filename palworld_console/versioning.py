from __future__ import annotations

import argparse
from pathlib import Path

from packaging.version import InvalidVersion, Version


def bump_version(value: str, bump_type: str = "patch") -> str:
    try:
        current = Version(value.strip().removeprefix("v"))
    except InvalidVersion as exc:
        raise ValueError(f"无效版本号：{value}") from exc
    if current.is_prerelease or current.is_devrelease or current.is_postrelease or len(current.release) != 3:
        raise ValueError("VERSION 必须是 X.Y.Z 格式的稳定版本")
    major, minor, patch = current.release
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    if bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"不支持的版本递增类型：{bump_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Increment Palworld Console VERSION")
    parser.add_argument("--file", type=Path, default=Path(__file__).with_name("VERSION"))
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    args = parser.parse_args()
    next_version = bump_version(args.file.read_text(encoding="utf-8"), args.bump)
    args.file.write_text(next_version + "\n", encoding="utf-8")
    print(next_version)


if __name__ == "__main__":
    main()
