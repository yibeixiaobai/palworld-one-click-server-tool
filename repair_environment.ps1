$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$systemPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $systemPython)) { $systemPython = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $systemPython) { throw '未找到可用的 Python 3.10+。请先安装 Python。' }
$venv = Join-Path $root '.venv'
if (Test-Path $venv) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Rename-Item -LiteralPath $venv -NewName ".venv.broken-$stamp"
}
& $systemPython -m venv $venv
& (Join-Path $venv 'Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $venv 'Scripts\python.exe') -m pip install -e $root
Write-Host '虚拟环境修复完成。现在可以运行 create_desktop_shortcut.ps1 重新生成快捷方式。'
