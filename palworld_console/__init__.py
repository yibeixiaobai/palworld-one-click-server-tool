from importlib.resources import files


def _read_version() -> str:
    value = files(__package__).joinpath("VERSION").read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("VERSION 文件为空")
    return value


__version__ = _read_version()
