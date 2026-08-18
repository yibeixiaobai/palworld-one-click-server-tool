# 幻兽帕鲁服务器控制台

作者：江小白Cresent

基于 Python 与 PySide6 的 Windows 桌面服务器管理工具，支持本机 Windows 专用服和远程 Linux SSH/systemd 实例。

## 主要功能

- 浅色现代桌面界面，提供仪表盘、连接部署、游戏配置、玩家中心、公会与基地、模组管理、RCON 自动化、备份、日志审计和关于我们十个页面
- SteamCMD 安装更新、启动停止、重启、卸载、实时进度、服务修复和端口诊断；本机只需选择服务端目录，工具会自动维护 `<安装目录>\_tools\steamcmd\steamcmd.exe`
- 远程 SSH 自动探测安装目录、SteamCMD、systemd、配置、存档和日志路径
- 60 余项常用游戏配置，支持分类、搜索、预设、差异、范围校验、原子写入和自动备份
- REST 玩家、公会、性能与管理操作；远程 REST 和 RCON 默认仅通过 SSH 隧道访问
- SQLite 永久玩家档案通过 UID 与平台账号安全聚合，REST 和存档重复记录只显示一次；玩家属性、帕鲁、背包、公会与管理操作集中在玩家中心
- 中文存档字段注册表隐藏内部技术路径，展示游戏含义、来源、有效范围、可写状态、只读原因和风险等级
- 模组管理默认加载 Steam Workshop 公共目录，支持搜索、排序、分页、24 小时缓存、封面和详情；Workshop、ZIP、PAK 均校验 `Info.json`、服务器安装规则、依赖、冲突与 SHA-256。Windows 服务端使用官方 `Mods\Workshop`、`Mods\ManagedMods` 和 `Mods\PalModSettings.ini` 目录；原生 Linux 明确禁用，Linux Wine 为实验模式
- 玩家中心采用“选择玩家 -> 角色 UID -> 关联内容”的主从布局，同一平台账号的多个角色只聚合展示、不混写存档；帕鲁和背包编辑绑定稳定 GUID、容器 ID 与槽位，并支持草稿预览、撤销和统一写回
- Source RCON 命令控制台、高风险命令确认和操作审计
- 主机级计划任务：远程使用 systemd timer，本机使用 Windows 任务计划
- 本机与远程存档备份、恢复、校验和保留策略
- 旧格式继续使用 `palworld-save-tools==0.24.0`；`PlM1` 存档使用按需构建、与主程序隔离的固定提交插件，使用 `save-patch-v2` 对玩家 UID、帕鲁 GUID 和背包容器槽位执行结构化修改、二次解析、原子替换和失败回滚

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
- 模组部署需要停服、完整备份、重启和健康检查；原生 Linux Dedicated Server 不会被伪装成官方支持模组的环境。
- PlM 插件不可用、构建失败或格式校验不通过时，存档功能自动保持只读；程序不会猜测未知字段。

## 本机安装目录

选择一个用于 Palworld Dedicated Server 的普通可写目录即可，例如 `D:\PalworldServer` 或 `E:\Games\PalworldServer`。程序会在该目录下创建 `_tools\steamcmd`，自动下载、校验和首次初始化 SteamCMD，再使用 AppID `2394010` 安装或更新服务端。SteamCMD 不再要求单独选择路径；完整卸载会连同该实例目录内的 `_tools\steamcmd` 一并移除。

## Workshop 安装目标

模组页面的安装目标始终跟随当前选中的服务器实例。本机 Windows 直接部署到服务端目录；远程 Linux Wine 先通过 SSH 在主机上下载，再通过 SFTP 取回校验并部署。原生 Linux Dedicated Server 页面会明确显示官方不支持并禁用安装按钮。

## 开源致谢

- PySide6
- Paramiko
- keyring
- requests
- palworld-save-tools（MIT，旧格式适配）
- PalworldSaveTools 固定提交插件中的 `palsav-flex` / `palooz`（GPL-3.0-or-later；按需本机构建，不随主程序分发）

部分存档结构化流程参考 `palworld-server-tool` 的固定提交（Apache-2.0）。上游 `palooz` 所含部分 Oodle 压缩源码存在额外授权警告，因此插件与主程序隔离且不随安装包再分发。本项目不包含地图功能，也不复制参考项目界面或素材。
