# 幻兽帕鲁服务器控制台

作者：江小白Cresent

基于 Python 与 PySide6 的 Windows 桌面服务器管理工具，支持本机 Windows 专用服和远程 Linux SSH/systemd 实例。

## 主要功能

- 浅色现代桌面界面，提供仪表盘、连接部署、游戏配置、玩家、帕鲁与背包、公会与基地、RCON 自动化、备份、日志审计和关于我们十个页面
- SteamCMD 安装更新、启动停止、重启、卸载、实时进度、服务修复和端口诊断
- 远程 SSH 自动探测安装目录、SteamCMD、systemd、配置、存档和日志路径
- 60 余项常用游戏配置，支持分类、搜索、预设、差异、范围校验、原子写入和自动备份
- REST 玩家、公会、性能与管理操作；远程 REST 和 RCON 默认仅通过 SSH 隧道访问
- SQLite 永久玩家档案，合并完整存档与 REST 在线状态；支持离线/存档缺失状态、等级经验、备注、脱敏 IP、帕鲁、背包和公会关联
- Source RCON 命令控制台、高风险命令确认和操作审计
- 主机级计划任务：远程使用 systemd timer，本机使用 Windows 任务计划
- 本机与远程存档备份、恢复、校验和保留策略
- 旧格式继续使用 `palworld-save-tools==0.24.0`；`PlM1` 存档使用按需构建、与主程序隔离的固定提交插件，支持结构化玩家字段修改、二次解析、原子替换和失败回滚

## 运行

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python run.py
```

也可以运行 `create_desktop_shortcut.ps1` 创建桌面快捷方式。

## 网络说明

- 游戏客户端连接游戏 UDP 端口，默认格式为 `服务器公网地址:8211`。
- `8212/TCP` 默认是 REST 管理端口，不能用于游戏客户端连接。
- 云服务器需要在云厂商安全组或网络访问控制中放行游戏 UDP 端口；本机还可能需要配置主机防火墙或路由器端口映射。
- 程序不会自动操作云厂商控制台，也不会把 REST 或 RCON 管理端口直接开放到公网。

## 安全说明

- SSH 密码、私钥口令和管理密码保存在 Windows Credential Manager，实例 JSON 仅保存引用。
- 高级存档编辑禁止修改运行中的世界；任何写回都必须先停服并完成本地与服务器侧备份。
- 远程计划任务配置权限为 `600`，管理密码从服务器配置文件读取，不写入命令行或任务日志。
- 公会、基地、玩家或帕鲁的删除和迁移属于高风险操作，应先在备份副本上验证。
- PlM 插件不可用、构建失败或格式校验不通过时，存档功能自动保持只读；程序不会猜测未知字段。

## 开源致谢

- PySide6
- Paramiko
- keyring
- requests
- palworld-save-tools（MIT，旧格式适配）
- PalworldSaveTools 固定提交插件中的 `palsav-flex` / `palooz`（GPL-3.0-or-later；按需本机构建，不随主程序分发）

部分存档结构化流程参考 `palworld-server-tool` 的固定提交（Apache-2.0）。上游 `palooz` 所含部分 Oodle 压缩源码存在额外授权警告，因此插件与主程序隔离且不随安装包再分发。本项目不包含地图功能，也不复制参考项目界面或素材。
