$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$venvCfg = Join-Path $root '.venv\pyvenv.cfg'
$runPy = Join-Path $root 'run.py'
if ((Test-Path $python) -and -not (Test-Path $venvCfg)) {
    throw "项目虚拟环境不完整：缺少 $venvCfg。请先运行 .\repair_environment.ps1。"
}
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw '未找到 Python。请先安装 Python 3.10+ 并运行 pip install -e .' }
if (-not (Test-Path $runPy)) { throw "启动入口不存在：$runPy" }
$probe = & $python -c "import sys; print(sys.executable)" 2>&1
if ($LASTEXITCODE -ne 0) { throw "Python 无法执行：$probe" }
$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop '幻兽帕鲁服务器控制台.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $python
$shortcut.Arguments = ('"' + $runPy + '"')
$shortcut.WorkingDirectory = $root
$shortcut.Description = '幻兽帕鲁服务器控制台'
$shortcut.Save()
Write-Host "已创建: $link"
