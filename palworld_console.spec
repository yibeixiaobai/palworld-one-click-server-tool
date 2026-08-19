# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("keyring.backends") + collect_submodules("palworld_save_tools")
version_text = Path("palworld_console/VERSION").read_text(encoding="utf-8").strip()
version_parts = tuple(int(part) for part in version_text.split(".")) + (0,)
version_info = Path("build/version_info.txt")
version_info.parent.mkdir(parents=True, exist_ok=True)
version_info.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_parts}, prodvers={version_parts}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('080404b0', [
    StringStruct('CompanyName', 'JiangXiaobaiCresent'),
    StringStruct('FileDescription', 'Palworld Server Console'),
    StringStruct('FileVersion', '{version_text}'),
    StringStruct('InternalName', 'PalworldConsole'),
    StringStruct('OriginalFilename', 'PalworldConsole.exe'),
    StringStruct('ProductName', 'Palworld Server Console'),
    StringStruct('ProductVersion', '{version_text}')
  ])]), VarFileInfo([VarStruct('Translation', [2052, 1200])])]
)\n""", encoding="utf-8")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("palworld_console/VERSION", "palworld_console")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PalworldConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    version=str(version_info),
)
