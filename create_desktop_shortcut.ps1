$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw '未找到 Python。请先安装 Python 3.10+ 并运行 pip install -e .' }
$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop '幻兽帕鲁服务器控制台.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $python
$shortcut.Arguments = ('"' + (Join-Path $root 'run.py') + '"')
$shortcut.WorkingDirectory = $root
$shortcut.Description = '幻兽帕鲁服务器控制台'
$shortcut.Save()
Write-Host "已创建: $link"
