from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import platform
import re
import sys
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QMainWindow, QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QLineEdit, QVBoxLayout, QWidget, QInputDialog, QAbstractItemView, QStackedWidget)
from PySide6.QtCore import QSettings

from .config_ini import coerce_setting_value
from .models import ConfigSyncResult, GuildSummary, PlayerRecord, ServerHealthSnapshot, ServerInstance, TaskProgress, UninstallResult, ScheduleDefinition
from .services import BackupService, FirewallService, GuildSnapshotService, LocalServerLifecycle, NetworkDiagnostics, PalworldRestClient, PlayerAdminService, RemoteHostClient, RemoteServerInspector, RemoteServerLifecycle, ServerConfigBootstrap, ServerDiagnostics, SSHTunnelManager, SteamCmdInstaller, WindowsShortcutService
from .management import AuditService, AutomationService, HostTaskDeployer, RconClient, SaveGameService, WhitelistService
from .player_store import PlayerRepository
from .save_codec import PALWORLD_SAVE_TOOLS_COMMIT, PlmCodecPlugin, PluginParsedSave
from .settings_schema import CATEGORIES, PRESETS, SETTING_BY_KEY, SETTING_DEFINITIONS
from .storage import AppStorage


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    log = Signal(str)


class UiSignals(QObject):
    log = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn, with_signals=False):
        super().__init__(); self.fn = fn; self.with_signals = with_signals; self.signals = WorkerSignals()
    @Slot()
    def run(self):
        try: self.signals.finished.emit(self.fn(self.signals) if self.with_signals else self.fn())
        except Exception as exc: self.signals.error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("幻兽帕鲁服务器控制台")
        self.resize(1180, 760)
        self.storage = AppStorage()
        self.instances = self.storage.load_instances()
        if not self.instances:
            self.instances = [ServerInstance()]
            self.storage.save_instances(self.instances)
        self.player_repository = PlayerRepository(self.storage.root / "players.db")
        migrated = sum(self.player_repository.migrate_instance_history(instance) for instance in self.instances)
        if migrated:
            for instance in self.instances:
                instance.player_history = {}
            self.storage.save_instances(self.instances)
        self.selected: ServerInstance | None = None
        self.lifecycle: LocalServerLifecycle | RemoteServerLifecycle | None = None
        self.install_task_active = False
        self.active_task_kind = ""
        self.install_progress_value = 0
        self.rest_tunnel: SSHTunnelManager | None = None
        self.rcon_tunnel: SSHTunnelManager | None = None
        self.current_players: list[PlayerRecord] = []
        self.current_guilds: list[GuildSummary] = []
        self.config_original: dict[str, object] = {}
        self.ui_signals = UiSignals()
        self.ui_signals.log.connect(self.append_log)
        self.pool = QThreadPool.globalInstance()
        self._build_ui()
        self._refresh_instances()
        self._restore_ui_state()

    def _build_ui(self):
        menu = self.menuBar().addMenu("文件")
        add_action = QAction("新增实例", self); add_action.triggered.connect(self.add_instance); menu.addAction(add_action)
        root = QWidget(); layout = QHBoxLayout(root); layout.setContentsMargins(12, 12, 12, 12); layout.setSpacing(12)
        splitter = QSplitter(); layout.addWidget(splitter)
        left = QWidget(); left.setObjectName("instancePanel"); left_layout = QVBoxLayout(left); left_layout.setContentsMargins(12, 12, 12, 12)
        brand = QLabel("PALWORLD\nSERVER CONSOLE"); brand.setObjectName("brandLabel"); left_layout.addWidget(brand)
        left_layout.addWidget(QLabel("服务器实例"))
        self.instance_list = QListWidget(); self.instance_list.currentRowChanged.connect(self.select_instance); left_layout.addWidget(self.instance_list)
        self.add_button = QPushButton("＋ 新增实例"); self.add_button.clicked.connect(self.add_instance); left_layout.addWidget(self.add_button)
        self.delete_button = QPushButton("删除实例"); self.delete_button.clicked.connect(self.delete_instance); left_layout.addWidget(self.delete_button)
        splitter.addWidget(left)

        right = QWidget(); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(0, 0, 0, 0)
        header = QWidget(); header.setObjectName("topHeader"); header_layout = QHBoxLayout(header); header_layout.setContentsMargins(16, 10, 16, 10)
        self.title = QLabel("未选择实例"); self.title.setObjectName("pageTitle"); header_layout.addWidget(self.title)
        self.header_health = QLabel("未检测"); self.header_health.setObjectName("statusPill"); header_layout.addWidget(self.header_health)
        header_layout.addStretch(); self.header_address = QLabel("游戏地址：-"); header_layout.addWidget(self.header_address)
        right_layout.addWidget(header)
        page_area = QWidget(); page_layout = QHBoxLayout(page_area); page_layout.setContentsMargins(0, 0, 0, 0); page_layout.setSpacing(8)
        self.navigation = QListWidget(); self.navigation.setObjectName("pageNavigation"); self.navigation.setFixedWidth(172); page_layout.addWidget(self.navigation)
        self.page_stack = QStackedWidget(); page_layout.addWidget(self.page_stack, 1); right_layout.addWidget(page_area)
        self.tabs = QTabWidget(); self.tabs.hide()
        self.dashboard = self._dashboard_tab(); self.connection = self._connection_tab(); self.config = self._game_config_tab(); self.players_page = self._players_tab(); self.pals_page = self._pals_tab(); self.guilds_page = self._guilds_tab(); self.automation_page = self._automation_tab(); self.backups_page = self._backup_tab(); self.ops = self._ops_tab(); self.about_page = self._about_tab()
        pages = ((self.dashboard, "仪表盘"), (self.connection, "连接与部署"), (self.config, "游戏配置"), (self.players_page, "玩家管理"), (self.pals_page, "帕鲁与背包"), (self.guilds_page, "公会与基地"), (self.automation_page, "RCON 与自动化"), (self.backups_page, "备份与恢复"), (self.ops, "日志与审计"), (self.about_page, "关于我们"))
        for page, title in pages:
            self.tabs.addTab(QWidget(), title); self.navigation.addItem(title); self.page_stack.addWidget(page)
        self.navigation.currentRowChanged.connect(self.page_stack.setCurrentIndex); self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex); self.navigation.setCurrentRow(0)
        splitter.addWidget(right); splitter.setSizes([235, 945]); self.main_splitter = splitter
        self.setCentralWidget(root)
        self.setMinimumSize(1180, 720)
        self._apply_light_theme()

    def _apply_light_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f4f6f8; color: #17212b; font-size: 13px; }
            #instancePanel, #topHeader, QGroupBox, QTabWidget::pane { background: #ffffff; border: 1px solid #dce2e8; }
            #instancePanel, #topHeader { border-radius: 6px; }
            #brandLabel { color: #087f5b; font-size: 15px; font-weight: 700; padding: 4px 2px 12px 2px; }
            #pageTitle { font-size: 20px; font-weight: 650; }
            #statusPill { background: #eef2f5; border-radius: 8px; padding: 4px 10px; font-weight: 600; }
            #pageNavigation { background: #ffffff; border: 1px solid #dce2e8; border-radius: 6px; padding: 6px; outline: none; }
            #pageNavigation::item { min-height: 34px; padding: 3px 10px; border-radius: 4px; color: #44515e; }
            #pageNavigation::item:selected { background: #e6f4ef; color: #087f5b; font-weight: 650; border-left: 3px solid #099268; }
            QGroupBox { border-radius: 6px; margin-top: 12px; padding-top: 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QPushButton { background: #ffffff; border: 1px solid #cbd4dc; border-radius: 5px; padding: 6px 12px; min-height: 22px; }
            QPushButton:hover { border-color: #099268; color: #087f5b; }
            QPushButton:pressed { background: #e6f4ef; }
            QPushButton:disabled { background: #f0f2f4; color: #9aa5ae; }
            QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTableWidget { background: #ffffff; border: 1px solid #cbd4dc; border-radius: 4px; padding: 4px; selection-background-color: #bfe6d8; }
            QHeaderView::section { background: #f0f3f5; border: 0; border-bottom: 1px solid #dce2e8; padding: 7px; font-weight: 600; }
            QTableWidget { gridline-color: #e8ecef; alternate-background-color: #f8fafb; }
            QProgressBar { border: 1px solid #cbd4dc; border-radius: 4px; background: #eef2f5; height: 10px; }
            QProgressBar::chunk { background: #099268; border-radius: 3px; }
        """)

    def _dashboard_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        self.status = QLabel("状态：-"); self.connect_addr = QLabel("连接地址：-"); l.addWidget(self.status); l.addWidget(self.connect_addr)
        row = QHBoxLayout()
        for text, handler in (("刷新状态", self.refresh_health), ("诊断并修复", self.diagnose_and_repair), ("启动", self.start_server), ("停止", self.stop_server), ("重启", self.restart_server), ("安装/更新", self.update_server), ("卸载服务器", self.uninstall_server), ("复制游戏地址", self.copy_address)):
            b = QPushButton(text); b.clicked.connect(handler); row.addWidget(b)
            if text == "安装/更新": self.install_button = b
            if text == "卸载服务器":
                self.uninstall_button = b
                b.setStyleSheet("color: #b42318;")
        l.addLayout(row)
        progress_group = QGroupBox("服务器任务")
        progress_layout = QVBoxLayout(progress_group)
        progress_header = QHBoxLayout()
        self.install_stage = QLabel("暂无安装任务")
        self.install_percent = QLabel("0%")
        progress_header.addWidget(self.install_stage); progress_header.addStretch(); progress_header.addWidget(self.install_percent)
        self.install_progress = QProgressBar(); self.install_progress.setRange(0, 100); self.install_progress.setValue(0); self.install_progress.setTextVisible(False)
        self.install_message = QLabel("等待安装、更新或卸载操作"); self.install_message.setWordWrap(True)
        progress_layout.addLayout(progress_header); progress_layout.addWidget(self.install_progress); progress_layout.addWidget(self.install_message)
        l.addWidget(progress_group)
        health = QGroupBox("服务器状态（手动刷新）"); grid = QGridLayout(health)
        self.health_labels = {}
        for index, (key, label) in enumerate((("service", "systemd"), ("process", "进程"), ("game", "游戏端口"), ("rest", "REST 隧道"), ("players", "在线玩家"), ("performance", "性能"), ("resources", "资源"), ("backup", "最近备份"), ("checked", "刷新时间"))):
            title = QLabel(label); value = QLabel("-"); value.setWordWrap(True); self.health_labels[key] = value; grid.addWidget(title, index // 2, (index % 2) * 2); grid.addWidget(value, index // 2, (index % 2) * 2 + 1)
        l.addWidget(health)
        self.health_issues = QPlainTextEdit(); self.health_issues.setReadOnly(True); self.health_issues.setMaximumHeight(130); self.health_issues.setPlaceholderText("点击“刷新状态”查看诊断结果"); l.addWidget(self.health_issues)
        self.rest_status = QLabel("REST：未测试（远程实例通过 SSH 隧道访问）"); l.addWidget(self.rest_status)
        l.addStretch(); return w

    def _connection_tab(self):
        outer = QWidget(); outer_layout = QVBoxLayout(outer); outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); outer_layout.addWidget(scroll)
        w = QWidget(); scroll.setWidget(w); l = QVBoxLayout(w); form = QFormLayout()
        self.name_edit = QLineEdit(); self.kind_combo = QComboBox(); self.kind_combo.addItem("本机", "local"); self.kind_combo.addItem("远程 SSH", "remote")
        self.path_edit = QLineEdit(); self.host_edit = QLineEdit(); self.user_edit = QLineEdit(); self.ssh_port_spin = QSpinBox(); self.ssh_port_spin.setRange(1, 65535); self.ssh_port_spin.setValue(22)
        self.auth_combo = QComboBox(); self.auth_combo.addItem("密码", "password"); self.auth_combo.addItem("私钥", "key"); self.ssh_password_edit = QLineEdit(); self.ssh_password_edit.setEchoMode(QLineEdit.Password); self.key_path_edit = QLineEdit(); self.key_passphrase_edit = QLineEdit(); self.key_passphrase_edit.setEchoMode(QLineEdit.Password)
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535); self.rest_edit = QLineEdit(); self.rest_user_edit = QLineEdit("admin"); self.rest_password_edit = QLineEdit(); self.rest_password_edit.setEchoMode(QLineEdit.Password); self.public_edit = QLineEdit()
        self.admin_password_box = QWidget(); password_layout = QHBoxLayout(self.admin_password_box); password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.addWidget(self.rest_password_edit)
        self.show_admin_password_button = QPushButton("显示"); self.show_admin_password_button.clicked.connect(self.toggle_admin_password); password_layout.addWidget(self.show_admin_password_button)
        copy_password = QPushButton("复制"); copy_password.clicked.connect(self.copy_admin_password); password_layout.addWidget(copy_password)
        for label, widget in (("名称", self.name_edit), ("类型", self.kind_combo), ("本地安装目录", self.path_edit), ("主机地址", self.host_edit), ("SSH 用户", self.user_edit), ("SSH 端口", self.ssh_port_spin), ("认证方式", self.auth_combo), ("SSH 密码", self.ssh_password_edit), ("私钥文件", self.key_path_edit), ("私钥口令", self.key_passphrase_edit), ("游戏端口（UDP）", self.port_spin), ("REST 远程端点（经 SSH 隧道）", self.rest_edit), ("REST 用户", self.rest_user_edit), ("REST 管理员密码", self.admin_password_box), ("公网/局域网游戏地址", self.public_edit)): form.addRow(label, widget)
        self.kind_combo.currentIndexChanged.connect(self._toggle_remote_fields)
        self.auth_combo.currentIndexChanged.connect(self._toggle_remote_fields)
        l.addLayout(form)
        row = QHBoxLayout(); save = QPushButton("保存实例设置"); save.clicked.connect(self.save_instance); row.addWidget(save); self.discover_btn = QPushButton("连接并检测 SSH"); self.discover_btn.clicked.connect(self.discover_remote); row.addWidget(self.discover_btn); l.addLayout(row)
        l.addWidget(QLabel("远程检测结果")); self.discovery_result = QPlainTextEdit(); self.discovery_result.setReadOnly(True); self.discovery_result.setMaximumHeight(150); l.addWidget(self.discovery_result)
        l.addStretch(); return outer

    def _game_config_tab(self):
        outer = QWidget(); layout = QVBoxLayout(outer)
        controls = QHBoxLayout(); self.config_search = QLineEdit(); self.config_search.setPlaceholderText("搜索配置项"); self.config_search.textChanged.connect(self._filter_config_fields); controls.addWidget(self.config_search)
        self.modified_only = QCheckBox("仅显示已修改"); self.modified_only.toggled.connect(self._filter_config_fields); controls.addWidget(self.modified_only)
        self.preset_combo = QComboBox(); self.preset_combo.addItems(PRESETS.keys()); controls.addWidget(self.preset_combo)
        preset_btn = QPushButton("应用预设"); preset_btn.clicked.connect(self.apply_config_preset); controls.addWidget(preset_btn); layout.addLayout(controls)
        self.config_source_label = QLabel("配置状态：尚未同步"); layout.addWidget(self.config_source_label)
        self.config_categories = QTabWidget(); layout.addWidget(self.config_categories)
        self.ini_fields = {}; self.config_forms = {}
        for category in CATEGORIES:
            scroll = QScrollArea(); scroll.setWidgetResizable(True); body = QWidget(); form = QFormLayout(body); scroll.setWidget(body); self.config_categories.addTab(scroll, category); self.config_forms[category] = form
        for definition in SETTING_DEFINITIONS:
            if definition.value_type == "bool":
                widget = QComboBox(); widget.addItems(["True", "False"]); widget.setCurrentText("True" if definition.default else "False"); widget.currentTextChanged.connect(self._config_changed)
            else:
                widget = QLineEdit(str(definition.default)); widget.textChanged.connect(self._config_changed)
                if definition.value_type == "password": widget.setEchoMode(QLineEdit.Password)
            widget.setProperty("setting_key", definition.key); self.ini_fields[definition.key] = widget
            range_text = f" [{definition.minimum:g}–{definition.maximum:g}]" if definition.minimum is not None and definition.maximum is not None else ""
            self.config_forms[definition.category].addRow(definition.label + range_text, widget)
        actions = QHBoxLayout(); load_btn = QPushButton("从服务器读取"); load_btn.clicked.connect(self.load_ini); save_btn = QPushButton("保存配置（自动备份）"); save_btn.clicked.connect(self.save_ini); reset_btn = QPushButton("恢复当前分组默认值"); reset_btn.clicked.connect(self.reset_config_category)
        actions.addWidget(load_btn); actions.addWidget(save_btn); actions.addWidget(reset_btn); actions.addStretch(); layout.addLayout(actions)
        self.config_diff_label = QLabel("尚无修改"); self.config_diff_label.setWordWrap(True); layout.addWidget(self.config_diff_label); return outer

    def _players_tab(self):
        w = QWidget(); l = QVBoxLayout(w); controls = QHBoxLayout(); self.player_search = QLineEdit(); self.player_search.setPlaceholderText("搜索玩家、平台账号或 UID"); self.player_search.textChanged.connect(self._render_players); controls.addWidget(self.player_search)
        self.player_state_filter = QComboBox(); self.player_state_filter.addItem("全部状态", "all"); self.player_state_filter.addItem("在线", "online"); self.player_state_filter.addItem("离线", "offline"); self.player_state_filter.addItem("存档缺失", "missing"); self.player_state_filter.currentIndexChanged.connect(self._render_players); controls.addWidget(self.player_state_filter)
        for text, handler in (("刷新在线状态", self.refresh_players), ("同步完整存档", self.load_save_snapshot), ("广播", self.broadcast), ("踢出", self.kick_player), ("封禁", self.ban_player), ("按 ID 解封", self.unban_player)): b=QPushButton(text); b.clicked.connect(handler); controls.addWidget(b)
        l.addLayout(controls); split = QSplitter(Qt.Horizontal)
        self.players_table = QTableWidget(0, 9); self.players_table.setHorizontalHeaderLabels(["状态", "玩家", "账号", "用户 ID", "玩家 UID", "等级", "经验", "最后出现", "存档"]); self.players_table.setAlternatingRowColors(True); self.players_table.setSortingEnabled(True); self.players_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.players_table.horizontalHeader().setStretchLastSection(True); self.players_table.currentCellChanged.connect(self._show_player_detail); split.addWidget(self.players_table)
        detail = QWidget(); dl = QVBoxLayout(detail); self.player_detail_title = QLabel("选择玩家查看永久档案"); self.player_detail_title.setStyleSheet("font-size:16px;font-weight:650;"); dl.addWidget(self.player_detail_title); self.player_detail_text = QPlainTextEdit(); self.player_detail_text.setReadOnly(True); dl.addWidget(self.player_detail_text)
        note_row = QHBoxLayout(); self.player_note = QLineEdit(); self.player_note.setPlaceholderText("玩家备注"); note_row.addWidget(self.player_note); note_button = QPushButton("保存备注"); note_button.clicked.connect(self.save_player_note); note_row.addWidget(note_button); edit_button = QPushButton("编辑玩家等级/属性"); edit_button.clicked.connect(self.edit_selected_player); note_row.addWidget(edit_button); dl.addLayout(note_row); split.addWidget(detail); split.setSizes([760, 360]); l.addWidget(split); return w

    def _pals_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        plugin = QGroupBox("PlM/Oodle 存档插件"); pl = QHBoxLayout(plugin); self.plm_plugin_status = QLabel("正在检测插件状态"); self.plm_plugin_status.setWordWrap(True); pl.addWidget(self.plm_plugin_status, 1); install_plugin = QPushButton("安装/修复 PlM 插件"); install_plugin.clicked.connect(self.install_plm_plugin); pl.addWidget(install_plugin); l.addWidget(plugin)
        row = QHBoxLayout(); self.save_path_label = QLabel("尚未载入 Level.sav"); row.addWidget(self.save_path_label); row.addStretch()
        load = QPushButton("载入存档副本"); load.clicked.connect(self.load_save_snapshot); row.addWidget(load)
        validate = QPushButton("验证存档"); validate.clicked.connect(self.validate_save_snapshot); row.addWidget(validate)
        apply_btn = QPushButton("应用高级修改"); apply_btn.clicked.connect(self.apply_save_changes); apply_btn.setStyleSheet("color:#b42318;"); row.addWidget(apply_btn); l.addLayout(row)
        notice = QLabel("PlM1 使用结构化玩家补丁，只允许已验证字段。写回必须停服、完整备份、二次解析并通过健康检查；插件不可用时自动保持只读。")
        notice.setWordWrap(True); l.addWidget(notice)
        search_row = QHBoxLayout(); self.save_scope = QComboBox(); self.save_scope.addItem("全部对象", "all"); self.save_scope.addItem("玩家字段", "player"); self.save_scope.addItem("帕鲁字段", "pal"); self.save_scope.addItem("背包与容器", "inventory"); self.save_scope.addItem("公会与基地", "guild"); self.save_scope.currentIndexChanged.connect(self._render_save_fields); search_row.addWidget(self.save_scope)
        self.save_search = QLineEdit(); self.save_search.setPlaceholderText("搜索对象路径、玩家 UID、物品或帕鲁字段"); self.save_search.textChanged.connect(self._render_save_fields); search_row.addWidget(self.save_search)
        self.save_changed_only = QCheckBox("仅看已修改"); self.save_changed_only.toggled.connect(self._render_save_fields); search_row.addWidget(self.save_changed_only); l.addLayout(search_row)
        self.save_fields_table = QTableWidget(0, 3); self.save_fields_table.setHorizontalHeaderLabels(["对象路径", "原始值", "修改值"]); self.save_fields_table.setAlternatingRowColors(True); self.save_fields_table.setSortingEnabled(False); self.save_fields_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch); self.save_fields_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents); self.save_fields_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents); l.addWidget(self.save_fields_table)
        self.save_document = None; self.save_scalar_values = {}; self.save_working_path = None
        self._refresh_plm_plugin_status()
        return w

    def _guilds_tab(self):
        w = QWidget(); l = QVBoxLayout(w); row = QHBoxLayout(); refresh = QPushButton("刷新公会与基地"); refresh.clicked.connect(self.refresh_guilds); row.addWidget(refresh); row.addWidget(QLabel("在线数据来自官方接口；改名、转移、合并和删除通过停服存档事务执行。")); row.addStretch(); l.addLayout(row)
        self.guilds_table = QTableWidget(0, 7); self.guilds_table.setHorizontalHeaderLabels(["公会", "公会 ID", "成员", "在线", "平均等级", "基地", "帕鲁"]); self.guilds_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.guilds_table.horizontalHeader().setStretchLastSection(True); l.addWidget(self.guilds_table); self.guild_members = QPlainTextEdit(); self.guild_members.setReadOnly(True); self.guild_members.setMaximumHeight(100); l.addWidget(self.guild_members); self.guilds_table.currentCellChanged.connect(self._show_guild_members); return w

    def _automation_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        rcon = QGroupBox("RCON 命令控制台（远程连接自动使用 SSH 隧道）"); rl = QVBoxLayout(rcon); row = QHBoxLayout()
        self.rcon_status = QLabel("未连接"); row.addWidget(self.rcon_status); self.rcon_command = QComboBox(); self.rcon_command.setEditable(True); self.rcon_command.addItems(["Info", "ShowPlayers", "Save", "Broadcast "]); row.addWidget(self.rcon_command, 1)
        execute = QPushButton("执行命令"); execute.clicked.connect(self.execute_rcon); row.addWidget(execute); rl.addLayout(row)
        self.rcon_output = QPlainTextEdit(); self.rcon_output.setReadOnly(True); self.rcon_output.setMaximumHeight(150); rl.addWidget(self.rcon_output); l.addWidget(rcon)
        tasks = QGroupBox("主机级计划任务"); tl = QVBoxLayout(tasks); task_row = QHBoxLayout()
        self.task_action = QComboBox(); self.task_action.addItems(["backup", "save", "broadcast", "restart", "update", "health", "whitelist"]); task_row.addWidget(self.task_action)
        self.task_time = QLineEdit("04:00"); self.task_time.setMaximumWidth(90); task_row.addWidget(self.task_time)
        self.task_retention = QSpinBox(); self.task_retention.setRange(1, 365); self.task_retention.setValue(14); task_row.addWidget(self.task_retention)
        add_task = QPushButton("添加计划"); add_task.clicked.connect(self.add_schedule); task_row.addWidget(add_task)
        toggle_task = QPushButton("启用/停用所选"); toggle_task.clicked.connect(self.toggle_schedule); task_row.addWidget(toggle_task)
        deploy = QPushButton("部署到主机"); deploy.clicked.connect(self.deploy_schedules); task_row.addWidget(deploy); task_row.addStretch(); tl.addLayout(task_row)
        self.schedule_table = QTableWidget(0, 5); self.schedule_table.setHorizontalHeaderLabels(["启用", "名称", "动作", "时间", "保留"]); self.schedule_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); tl.addWidget(self.schedule_table); l.addWidget(tasks)
        whitelist = QGroupBox("白名单策略"); wl = QVBoxLayout(whitelist); wr = QHBoxLayout(); self.whitelist_uid = QLineEdit(); self.whitelist_uid.setPlaceholderText("玩家 UID"); wr.addWidget(self.whitelist_uid); self.whitelist_name = QLineEdit(); self.whitelist_name.setPlaceholderText("玩家名称/备注"); wr.addWidget(self.whitelist_name); self.whitelist_policy = QComboBox(); self.whitelist_policy.addItem("仅记录", "log"); self.whitelist_policy.addItem("广播警告", "warn"); self.whitelist_policy.addItem("自动踢出", "kick"); wr.addWidget(self.whitelist_policy); add = QPushButton("加入白名单"); add.clicked.connect(self.add_whitelist); wr.addWidget(add); wl.addLayout(wr)
        self.whitelist_table = QTableWidget(0, 4); self.whitelist_table.setHorizontalHeaderLabels(["玩家 UID", "名称", "平台", "备注"]); self.whitelist_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); wl.addWidget(self.whitelist_table); l.addWidget(whitelist); l.addStretch(); return w

    def _backup_tab(self):
        w = QWidget(); l = QVBoxLayout(w); row = QHBoxLayout()
        for text, handler in (("立即备份", self.backup), ("恢复所选备份", self.restore), ("刷新列表", self.refresh_backup_list)):
            b = QPushButton(text); b.clicked.connect(handler); row.addWidget(b)
        row.addStretch(); l.addLayout(row)
        self.backup_summary = QLabel("计划备份默认关闭；启用后默认每天 04:00，保留最近 14 份。")
        self.backup_summary.setWordWrap(True); l.addWidget(self.backup_summary)
        self.backup_table = QTableWidget(0, 4); self.backup_table.setHorizontalHeaderLabels(["备份文件", "大小", "修改时间", "校验"]); self.backup_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch); self.backup_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents); self.backup_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents); self.backup_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents); l.addWidget(self.backup_table); return w

    def _ops_tab(self):
        w = QWidget(); l = QVBoxLayout(w); row = QHBoxLayout(); self.log_filter = QLineEdit(); self.log_filter.setPlaceholderText("筛选日志和审计记录"); row.addWidget(self.log_filter)
        for text, handler in (("保存世界", self.rest_save), ("广播", self.broadcast), ("导出日志", self.export_logs), ("清空日志", lambda: self.log.clear())):
            b = QPushButton(text); b.clicked.connect(handler); row.addWidget(b)
        l.addLayout(row); split = QSplitter(Qt.Vertical); self.log = QPlainTextEdit(); self.log.setReadOnly(True); split.addWidget(self.log); self.audit_table = QTableWidget(0, 5); self.audit_table.setHorizontalHeaderLabels(["时间", "操作", "目标", "结果", "详情"]); self.audit_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch); split.addWidget(self.audit_table); split.setSizes([360, 220]); l.addWidget(split); return w

    def _about_tab(self):
        from . import __version__
        w = QWidget(); l = QVBoxLayout(w); l.setContentsMargins(36, 30, 36, 30)
        name = QLabel("幻兽帕鲁服务器控制台"); name.setStyleSheet("font-size:26px;font-weight:700;color:#087f5b;"); l.addWidget(name)
        version = QLabel(f"版本 {__version__}  ·  作者：江小白Cresent"); version.setStyleSheet("font-size:15px;font-weight:600;"); l.addWidget(version)
        info = QGroupBox("运行环境"); form = QFormLayout(info); form.addRow("Python", QLabel(platform.python_version()));
        try:
            from PySide6 import __version__ as qt_version
        except ImportError:
            qt_version = "未知"
        form.addRow("PySide6", QLabel(qt_version)); form.addRow("数据目录", QLabel(str(self.storage.root))); form.addRow("备份目录", QLabel(str(self.storage.root / "backups"))); l.addWidget(info)
        privacy = QLabel("隐私与安全：SSH 密码、私钥口令和管理密码保存在 Windows Credential Manager；实例 JSON、日志和计划任务不保存凭据明文。远程 REST 与 RCON 默认仅通过 SSH 隧道访问。")
        privacy.setWordWrap(True); l.addWidget(privacy)
        credits = QGroupBox("开源致谢与许可证"); cl = QVBoxLayout(credits); credit_text = QLabel("PySide6 · Paramiko · keyring · requests\nPlM 插件按需从固定提交构建，与主程序隔离。上游组件包含 Apache-2.0、GPL-3.0-or-later 及 Oodle 压缩源码授权警告，程序不随安装包再分发相关源码或二进制。\n功能流程参考 palworld-server-tool；本程序不包含地图功能，也不复制其界面或素材。"); credit_text.setWordWrap(True); cl.addWidget(credit_text); l.addWidget(credits); l.addStretch(); return w

    def _refresh_instances(self):
        self.instance_list.clear(); self.instance_list.addItems([f"{i.name}  ({'本机' if i.kind == 'local' else '远程'})" for i in self.instances])
        if self.instances: self.instance_list.setCurrentRow(0)

    def select_instance(self, row: int):
        if row < 0 or row >= len(self.instances): return
        self._close_rest_tunnel()
        self.current_players = self.player_repository.list_players(self.instances[row].id); self.current_guilds = []
        self.selected = self.instances[row]; self.title.setText(self.selected.name); self.name_edit.setText(self.selected.name); self.kind_combo.setCurrentIndex(0 if self.selected.kind == "local" else 1); self.path_edit.setText(self.selected.install_dir); self.host_edit.setText(self.selected.host); self.user_edit.setText(self.selected.remote_username); self.ssh_port_spin.setValue(self.selected.ssh_port); self.auth_combo.setCurrentIndex(0 if self.selected.ssh_auth_type == "password" else 1); self.key_path_edit.setText(self.selected.ssh_key_path); self.port_spin.setValue(self.selected.game_port); self.rest_edit.setText(self.selected.rest_url); self.rest_password_edit.setText(self.storage.get_secret(self.selected.admin_secret_ref)); self.public_edit.setText(self.selected.public_address); self.config_source_label.setText(f"配置状态：{self.selected.config_source or '尚未同步'}" + ("，需要重启" if self.selected.config_restart_required else "")); self.lifecycle = LocalServerLifecycle(self.selected, self.ui_signals.log.emit) if self.selected.kind == "local" else (self._remote_lifecycle() if self.selected.discovery_status == "ready" else None); self._toggle_remote_fields(); self._show_discovery(); self.refresh_status()
        self._render_players(); self._render_guilds()
        self._render_schedules(); self._render_whitelist(); self._render_audit(); self.refresh_backup_list()
        self.header_address.setText(f"游戏地址：{self.selected.public_address or self.selected.host}:{self.selected.game_port}")

    def add_instance(self):
        self.instances.append(ServerInstance(name=f"服务器 {len(self.instances)+1}")); self.storage.save_instances(self.instances); self._refresh_instances()

    def delete_instance(self):
        if not self.selected: return
        if self.selected.kind == "local" and self.lifecycle and self.lifecycle.status() == "running": return QMessageBox.warning(self, "无法删除", "请先停止正在运行的本机服务器。")
        if QMessageBox.question(self, "确认删除", f"删除“{self.selected.name}”的控制台记录和保存的凭据？\n不会删除服务器文件。") != QMessageBox.Yes: return
        for ref in (self.selected.ssh_secret_ref, self.selected.ssh_key_passphrase_ref, self.selected.admin_secret_ref): self.storage.delete_secret(ref)
        index = self.instances.index(self.selected); self.instances.pop(index)
        if not self.instances: self.instances.append(ServerInstance())
        self.storage.save_instances(self.instances); self._refresh_instances(); self.instance_list.setCurrentRow(max(0, index - 1)); self.append_log("实例已删除")

    def save_instance(self):
        if not self.selected: return
        self.selected.name = self.name_edit.text().strip() or "未命名服务器"; self.selected.kind = self.kind_combo.currentData(); self.selected.install_dir = self.path_edit.text().strip(); self.selected.host = self.host_edit.text().strip() or "127.0.0.1"; self.selected.remote_username = self.user_edit.text().strip(); self.selected.ssh_port = self.ssh_port_spin.value(); self.selected.ssh_auth_type = self.auth_combo.currentData(); self.selected.ssh_key_path = self.key_path_edit.text().strip(); self.selected.game_port = self.port_spin.value(); self.selected.rest_url = self.rest_edit.text().strip(); self.selected.public_address = self.public_edit.text().strip(); self.selected.admin_secret_ref = self.selected.admin_secret_ref or f"rest-{self.selected.id}"; password = self.rest_password_edit.text();
        if password:
            self.storage.set_secret(self.selected.admin_secret_ref, password)
        if self.selected.kind == "remote":
            if not self.selected.host or not self.selected.remote_username: return QMessageBox.warning(self, "SSH 信息不完整", "远程实例需要主机地址和 SSH 用户。")
            self.selected.ssh_secret_ref = self.selected.ssh_secret_ref or f"ssh-{self.selected.id}"
            if self.selected.ssh_auth_type == "password" and self.ssh_password_edit.text(): self.storage.set_secret(self.selected.ssh_secret_ref, self.ssh_password_edit.text())
            if self.selected.ssh_auth_type == "key" and not self.selected.ssh_key_path: return QMessageBox.warning(self, "SSH 信息不完整", "请选择私钥文件。")
        self.storage.save_instances(self.instances); self._refresh_instances(); self.append_log("实例设置已保存")

    def toggle_admin_password(self):
        visible = self.rest_password_edit.echoMode() == QLineEdit.Normal
        self.rest_password_edit.setEchoMode(QLineEdit.Password if visible else QLineEdit.Normal)
        self.show_admin_password_button.setText("显示" if visible else "隐藏")

    def copy_admin_password(self):
        password = self.rest_password_edit.text()
        if password:
            QApplication.clipboard().setText(password)
            self.append_log("管理员密码已复制")

    def _ensure_admin_password(self) -> str:
        if not self.selected:
            raise RuntimeError("未选择服务器实例")
        self.selected.admin_secret_ref = self.selected.admin_secret_ref or f"rest-{self.selected.id}"
        password = self.rest_password_edit.text() or self.storage.get_secret(self.selected.admin_secret_ref)
        if not password:
            password = ServerConfigBootstrap.generate_admin_password()
        self.storage.set_secret(self.selected.admin_secret_ref, password)
        self.rest_password_edit.setText(password)
        self.storage.save_instances(self.instances)
        return password

    def refresh_status(self):
        if not self.selected: return
        if self.selected.kind == "remote" and self.selected.discovery_status == "ready" and not self.selected.remote_profile.get("installed"):
            state = "not_installed"
        elif self.selected.kind == "remote":
            state = str(self.selected.last_diagnostic.get("service_state") or self.selected.remote_profile.get("service_state") or self.selected.discovery_status)
        else:
            state = self.lifecycle.status() if self.lifecycle else "stopped"
        self.status.setText(f"状态：{state}"); addr = self.selected.public_address or f"{self.selected.host}:{self.selected.game_port}"; self.connect_addr.setText(f"游戏地址（UDP）：{addr}"); self.header_address.setText(f"游戏地址：{addr}"); self.header_health.setText(state)

    def refresh_health(self):
        if not self.selected: return
        if self.selected.kind != "remote":
            state = self.lifecycle.status() if self.lifecycle else "stopped"
            self.health_labels["service"].setText(state); self.health_labels["process"].setText("本机进程由当前控制台管理"); self.health_labels["game"].setText(f"UDP {self.selected.game_port}"); self.health_labels["checked"].setText("刚刚"); return
        selected, client = self.selected, self._remote_client()
        password = self.storage.get_secret(selected.admin_secret_ref); username = self.rest_user_edit.text().strip() or "admin"
        worker = Worker(lambda signals: self._collect_remote_health(selected, client, password, username), with_signals=True)
        worker.signals.finished.connect(self._health_done); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "状态刷新失败", e)); self.pool.start(worker)

    @staticmethod
    def _collect_remote_health(selected, client, password, username):
        tunnel = SSHTunnelManager(client)
        rest_client = None
        try:
            tunnel.start("127.0.0.1", int(selected.remote_profile.get("rest_port") or 8212))
            rest_client = PalworldRestClient(tunnel.base_url, password, username)
            snapshot = ServerDiagnostics.collect_remote(client, selected, rest_client)
            return snapshot, tunnel
        except Exception:
            snapshot = ServerDiagnostics.collect_remote(client, selected, None)
            return snapshot, tunnel

    def _health_done(self, payload):
        snapshot, tunnel = payload
        self._close_rest_tunnel(); self.rest_tunnel = tunnel
        if self.selected:
            self.selected.last_diagnostic = asdict(snapshot); self.selected.last_status = "healthy" if snapshot.healthy else "unhealthy"; self.storage.save_instances(self.instances)
        self._apply_health(snapshot)

    def _apply_health(self, snapshot: ServerHealthSnapshot):
        self.status.setText("状态：健康" if snapshot.healthy else "状态：异常")
        self.header_health.setText("健康" if snapshot.healthy else "异常")
        self.header_health.setStyleSheet("background:#dff5e8;color:#087f5b;border-radius:8px;padding:4px 10px;font-weight:600;" if snapshot.healthy else "background:#fff0e6;color:#b42318;border-radius:8px;padding:4px 10px;font-weight:600;")
        self.health_labels["service"].setText(snapshot.service_state)
        self.health_labels["process"].setText(f"PID {snapshot.pid or '-'} / 用户 {snapshot.process_user or '-'}")
        self.health_labels["game"].setText(f"UDP {snapshot.game_endpoint.port if snapshot.game_endpoint else '-'}：{'监听中' if snapshot.game_endpoint and snapshot.game_endpoint.listening else '未监听'}")
        self.health_labels["rest"].setText("SSH 隧道可用" if snapshot.rest_ok else "不可用")
        self.health_labels["players"].setText(f"{snapshot.player_count}/{snapshot.player_limit or '-'}")
        self.health_labels["performance"].setText(f"FPS {snapshot.fps:g} / {snapshot.frame_time_ms:g} ms")
        self.health_labels["resources"].setText(f"CPU {snapshot.cpu_percent:g}% / 内存 {snapshot.memory_mb:g} MB\n{snapshot.disk}")
        self.health_labels["backup"].setText(self.selected.last_backup if self.selected and self.selected.last_backup else "暂无")
        self.health_labels["checked"].setText(snapshot.checked_at)
        issues = list(snapshot.issues)
        if snapshot.game_endpoint and snapshot.game_endpoint.listening: issues.append(f"仍需确认云厂商安全组、网络访问控制或路由器已放行入站 UDP {snapshot.game_endpoint.port}。")
        self.health_issues.setPlainText("\n".join(issues) if issues else "未发现服务器侧异常。")
        self.rest_status.setText("REST：SSH 隧道连接成功" if snapshot.rest_ok else "REST：不可用")

    def diagnose_and_repair(self):
        if not self.selected or self.selected.kind != "remote": return QMessageBox.information(self, "提示", "诊断修复当前仅用于远程 Linux 实例。")
        if QMessageBox.question(self, "诊断并修复", "将重建非 root systemd 服务、放行游戏 UDP 端口并重启服务器，继续吗？") != QMessageBox.Yes: return
        selected, client = self.selected, self._remote_client(); password = self.storage.get_secret(selected.admin_secret_ref); username = self.rest_user_edit.text().strip() or "admin"
        worker = Worker(lambda signals: self._repair_and_collect(signals, selected, client, password, username), with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.finished.connect(lambda payload: (self.append_log("服务器诊断修复完成"), self._health_done(payload))); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "诊断修复失败", e)); self.pool.start(worker)

    @staticmethod
    def _repair_and_collect(signals, selected, client, password, username):
        RemoteServerLifecycle(selected, client, signals.log.emit).repair_runtime()
        return MainWindow._collect_remote_health(selected, client, password, username)

    def run_async(self, fn, done=lambda _result: None):
        worker = Worker(fn); worker.signals.finished.connect(done); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "操作失败", e)); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); self.pool.start(worker)

    def start_server(self):
        if self.lifecycle and self.selected:
            selected = self.selected
            client = self._remote_client() if selected.kind == "remote" else None
            worker = Worker(lambda signals: self._run_start_and_sync(signals, selected, client), with_signals=True)
            worker.signals.log.connect(self.append_log)
            worker.signals.finished.connect(self._server_started)
            worker.signals.error.connect(lambda e: QMessageBox.critical(self, "启动失败", e))
            self.pool.start(worker)
            return
        self._rest_action("启动", "stop", {})

    @staticmethod
    def _run_start_and_sync(signals, selected, client):
        lifecycle = None
        if selected.kind == "remote":
            remote_lifecycle = RemoteServerLifecycle(selected, client, signals.log.emit); remote_lifecycle.start(); remote_lifecycle.wait_for_game_listener()
        else:
            lifecycle = LocalServerLifecycle(selected, signals.log.emit)
            lifecycle.start()
        try:
            result = ServerConfigBootstrap.read_remote(client, selected) if selected.kind == "remote" else ServerConfigBootstrap.read_local(selected)
            return result, "", lifecycle
        except Exception as exc:
            return None, str(exc), lifecycle

    def _server_started(self, payload):
        result, sync_error, lifecycle = payload
        if lifecycle:
            self.lifecycle = lifecycle
        self.append_log("服务器启动命令已执行")
        if result:
            self._apply_config_result(result)
            self.append_log("服务器配置已自动同步")
        elif sync_error:
            self.append_log(f"服务器已启动，配置同步失败：{sync_error}")
            QMessageBox.warning(self, "配置同步失败", f"服务器已启动，但无法读取配置：\n{sync_error}")
        self.refresh_status()
    def stop_server(self):
        if self.lifecycle: self.run_async(self.lifecycle.stop, lambda _: (self.append_log("服务器已停止"), self.refresh_status()))
        else: self._rest_action("停止", "stop", {})
    def restart_server(self):
        if self.lifecycle and self.selected:
            selected, lifecycle = self.selected, self.lifecycle
            client = self._remote_client() if selected.kind == "remote" else None
            worker = Worker(lambda signals: self._run_restart_and_sync(signals, selected, lifecycle, client), with_signals=True)
            worker.signals.log.connect(self.append_log)
            worker.signals.finished.connect(self._server_restarted)
            worker.signals.error.connect(lambda e: QMessageBox.critical(self, "重启失败", e))
            self.pool.start(worker)
        else: self._rest_action("重启", "shutdown", {"waittime": 15})

    @staticmethod
    def _run_restart_and_sync(signals, selected, lifecycle, client):
        if selected.kind == "remote": lifecycle.configure_service()
        lifecycle.restart()
        if selected.kind == "remote": lifecycle.wait_for_game_listener()
        try:
            result = ServerConfigBootstrap.read_remote(client, selected) if selected.kind == "remote" else ServerConfigBootstrap.read_local(selected)
            return result, ""
        except Exception as exc:
            return None, str(exc)

    def _server_restarted(self, payload):
        result, sync_error = payload
        self.append_log("服务器已重启")
        if result:
            self._apply_config_result(result)
            self.append_log("重启后的服务器配置已自动同步")
        elif sync_error:
            self.append_log(f"服务器已重启，配置同步失败：{sync_error}")
            QMessageBox.warning(self, "配置同步失败", f"服务器已重启，但无法读取配置：\n{sync_error}")
        self.refresh_status()
    def update_server(self):
        if self.install_task_active:
            return QMessageBox.information(self, "任务进行中", "当前安装或更新任务尚未结束。")
        if self.selected and self.selected.kind == "remote":
            if self.selected.discovery_status != "ready": return QMessageBox.information(self, "需要检测", "请先连接并检测 SSH。")
            try: admin_password = self._ensure_admin_password()
            except Exception as exc: return QMessageBox.critical(self, "凭据错误", str(exc))
            self._begin_install_task("正在安装…" if not self.selected.remote_profile.get("installed") else "正在更新…")
            selected = self.selected
            client = self._remote_client()
            worker = Worker(lambda signals: self._run_remote_install(signals, selected, client, admin_password), with_signals=True)
            self._connect_install_worker(worker, self._remote_install_done)
            self.pool.start(worker)
            return
        if not self.selected: return
        steamcmd, _ = QFileDialog.getOpenFileName(self, "选择 steamcmd.exe", "", "SteamCMD (steamcmd.exe)")
        if not steamcmd: return
        if not self.selected.install_dir: return QMessageBox.warning(self, "提示", "请先保存本地安装目录")
        try: admin_password = self._ensure_admin_password()
        except Exception as exc: return QMessageBox.critical(self, "凭据错误", str(exc))
        self._begin_install_task("正在安装…")
        install_dir = Path(self.selected.install_dir)
        selected = self.selected
        worker = Worker(lambda signals: self._run_local_install(signals, selected, Path(steamcmd), install_dir, admin_password), with_signals=True)
        self._connect_install_worker(worker, self._local_install_done)
        self.pool.start(worker)

    def uninstall_server(self):
        if self.install_task_active:
            return QMessageBox.information(self, "任务进行中", "当前服务器任务尚未结束。")
        if not self.selected or not self.selected.install_dir:
            return QMessageBox.warning(self, "无法卸载", "尚未检测到服务器安装目录。")
        if self.selected.kind == "remote" and not self.selected.remote_profile.get("installed"):
            return QMessageBox.information(self, "无需卸载", "远程主机上未检测到 Palworld 服务端。")
        backup_dir = self._backup_destination(self.selected)
        summary = (
            f"服务器：{self.selected.name}\n"
            f"安装目录：{self.selected.install_dir}\n"
            f"备份目录：{backup_dir}\n\n"
            "程序、配置和存档将在备份校验通过后被完整删除。\n"
            "实例记录、SSH 凭据和 SteamCMD 会保留。"
        )
        if QMessageBox.warning(self, "确认卸载服务器", summary, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        entered, ok = QInputDialog.getText(self, "再次确认", f"请输入实例名称“{self.selected.name}”以继续：")
        if not ok or entered != self.selected.name:
            if ok:
                QMessageBox.warning(self, "名称不匹配", "实例名称不匹配，已取消卸载。")
            return
        selected = self.selected
        self._begin_server_task("uninstall", "正在卸载…", "准备卸载", "正在验证服务器和备份目录")
        if selected.kind == "remote":
            client = self._remote_client()
            worker = Worker(lambda signals: self._run_remote_uninstall(signals, selected, client, backup_dir, self.storage.get_secret(selected.admin_secret_ref)), with_signals=True)
        else:
            lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, self.ui_signals.log.emit)
            worker = Worker(lambda signals: lifecycle.uninstall(backup_dir, signals.progress.emit), with_signals=True)
        worker.signals.log.connect(self.append_log)
        worker.signals.progress.connect(self._set_install_progress)
        worker.signals.finished.connect(self._uninstall_succeeded)
        worker.signals.error.connect(self._server_task_failed)
        self.pool.start(worker)

    @staticmethod
    def _run_remote_uninstall(signals, selected, client, backup_dir, admin_password):
        if selected.rest_url and admin_password:
            signals.progress.emit(TaskProgress(5, "保存世界", "正在请求服务器保存世界", True))
            try:
                PalworldRestClient(selected.rest_url, admin_password).save()
                signals.log.emit("卸载前已请求保存世界")
            except Exception as exc:
                signals.log.emit(f"保存世界请求失败，将通过停止服务保证备份一致性：{exc}")
        lifecycle = RemoteServerLifecycle(selected, client, signals.log.emit, signals.progress.emit)
        result = lifecycle.uninstall(backup_dir)
        signals.progress.emit(TaskProgress(97, "重新检测", "正在确认远程服务端已移除", True))
        profile = RemoteServerInspector(client, signals.log.emit, result.install_dir).discover()
        if profile.get("installed"):
            raise RuntimeError("卸载命令已执行，但重新检测仍发现 Palworld 服务端")
        return result, profile

    def _backup_destination(self, instance: ServerInstance) -> Path:
        root = Path(getattr(self.storage, "root", Path.home() / ".palworld-console"))
        return root / "backups" / instance.id

    @staticmethod
    def _run_remote_install(signals, selected, client, admin_password):
        lifecycle = RemoteServerLifecycle(selected, client, signals.log.emit, signals.progress.emit)
        if selected.remote_profile.get("installed"):
            lifecycle.update(restart=False)
        else:
            lifecycle.install()
        signals.progress.emit(TaskProgress(87, "生成服务器配置", "正在创建或读取 PalWorldSettings.ini", True))
        config = ServerConfigBootstrap.ensure_remote(client, selected, admin_password)
        lifecycle.configure_service()
        signals.progress.emit(TaskProgress(95, "启动服务器", "正在启动 Palworld 服务", True))
        lifecycle.start()
        lifecycle.wait_for_game_listener()
        signals.progress.emit(TaskProgress(97, "重新检测", "正在确认服务端安装状态", True))
        try:
            profile = RemoteServerInspector(client, signals.log.emit, selected.install_dir).discover()
            return profile, config
        except Exception as exc:
            raise RuntimeError(f"服务端安装/更新已执行，但状态复检失败：{exc}") from exc

    @staticmethod
    def _run_local_install(signals, selected, steamcmd, install_dir, admin_password):
        SteamCmdInstaller().install_or_update(steamcmd, install_dir, signals.log.emit, signals.progress.emit)
        signals.progress.emit(TaskProgress(87, "生成服务器配置", "正在创建或读取 PalWorldSettings.ini", True))
        config = ServerConfigBootstrap.ensure_local(selected, admin_password)
        signals.progress.emit(TaskProgress(95, "启动服务器", "正在启动本机 Palworld 服务", True))
        lifecycle = LocalServerLifecycle(selected, signals.log.emit)
        lifecycle.start()
        return config, lifecycle

    def _connect_install_worker(self, worker: Worker, done):
        worker.signals.log.connect(self.append_log)
        worker.signals.progress.connect(self._set_install_progress)
        worker.signals.finished.connect(done)
        worker.signals.error.connect(self._install_failed)

    def _begin_install_task(self, button_text: str):
        self._begin_server_task("install", button_text, "准备任务", "正在初始化安装任务")

    def _begin_server_task(self, kind: str, button_text: str, stage: str, message: str):
        self.install_task_active = True
        self.active_task_kind = kind
        self.install_progress_value = 0
        (self.uninstall_button if kind == "uninstall" else self.install_button).setText(button_text)
        self.install_stage.setStyleSheet("")
        self.install_message.setStyleSheet("")
        self._set_install_progress(TaskProgress(0, stage, message, True))
        self._set_install_controls_enabled(False)

    def _set_install_controls_enabled(self, enabled: bool):
        self.install_button.setEnabled(enabled)
        self.uninstall_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self.add_button.setEnabled(enabled)
        self.instance_list.setEnabled(enabled)

    def _set_install_progress(self, progress: TaskProgress):
        if not isinstance(progress, TaskProgress):
            return
        self.install_progress_value = progress.percent
        self.install_stage.setText(progress.stage)
        self.install_message.setText(progress.message or progress.stage)
        if progress.indeterminate:
            self.install_progress.setRange(0, 0)
            self.install_percent.setText("处理中")
        else:
            self.install_progress.setRange(0, 100)
            self.install_progress.setValue(progress.percent)
            self.install_percent.setText(f"{progress.percent}%")

    def _install_succeeded(self, message: str):
        self._set_install_progress(TaskProgress(100, "安装完成", message))
        self.install_stage.setStyleSheet("color: #18794e; font-weight: 600;")
        self.install_message.setStyleSheet("color: #18794e;")
        self.append_log(message)
        self.install_task_active = False
        self.active_task_kind = ""
        self.install_button.setText("安装/更新")
        self.uninstall_button.setText("卸载服务器")
        self._set_install_controls_enabled(True)
        self.refresh_status()

    def _install_failed(self, error: str):
        self.install_progress.setRange(0, 100)
        self.install_progress.setValue(self.install_progress_value)
        self.install_percent.setText(f"{self.install_progress_value}%")
        self.install_stage.setText("安装失败")
        self.install_message.setText(error)
        self.install_stage.setStyleSheet("color: #b42318; font-weight: 600;")
        self.install_message.setStyleSheet("color: #b42318;")
        self.append_log(f"安装/更新失败：{error}")
        self.install_task_active = False
        self.active_task_kind = ""
        self.install_button.setText("安装/更新")
        self.uninstall_button.setText("卸载服务器")
        self._set_install_controls_enabled(True)
        QMessageBox.critical(self, "安装/更新失败", error)

    def _server_task_failed(self, error: str):
        if self.active_task_kind != "uninstall":
            return self._install_failed(error)
        self.install_progress.setRange(0, 100)
        self.install_progress.setValue(self.install_progress_value)
        self.install_percent.setText(f"{self.install_progress_value}%")
        self.install_stage.setText("卸载失败")
        self.install_message.setText(error)
        self.install_stage.setStyleSheet("color: #b42318; font-weight: 600;")
        self.install_message.setStyleSheet("color: #b42318;")
        self.append_log(f"卸载失败：{error}")
        self.install_task_active = False
        self.active_task_kind = ""
        self.install_button.setText("安装/更新")
        self.uninstall_button.setText("卸载服务器")
        self._set_install_controls_enabled(True)
        QMessageBox.critical(self, "卸载失败", error)

    def _uninstall_succeeded(self, payload):
        result, profile = payload if isinstance(payload, tuple) else (payload, None)
        if not isinstance(result, UninstallResult) or not self.selected:
            return self._server_task_failed("卸载任务返回了无效结果")
        self.selected.last_backup = result.backup_path
        if self.selected.admin_secret_ref:
            self.storage.delete_secret(self.selected.admin_secret_ref)
        self.selected.admin_secret_ref = ""
        self.selected.rest_url = ""
        self.selected.config_source = ""
        self.selected.config_synced_at = ""
        self.selected.config_restart_required = False
        self.rest_edit.clear(); self.rest_password_edit.clear()
        if profile is not None:
            self.selected.remote_profile = profile
            self.selected.discovery_status = "ready"
            self.selected.install_dir = str(profile.get("install_dir") or result.install_dir)
            self.path_edit.setText(self.selected.install_dir)
            self._show_discovery()
        self.lifecycle = LocalServerLifecycle(self.selected, self.ui_signals.log.emit) if self.selected.kind == "local" else self._remote_lifecycle()
        self.storage.save_instances(self.instances)
        message = f"服务器已卸载。备份：{result.backup_path}" if result.backup_path else "服务器已卸载，未发现需要备份的存档。"
        self._set_install_progress(TaskProgress(100, "卸载完成", message))
        self.install_stage.setStyleSheet("color: #18794e; font-weight: 600;")
        self.install_message.setStyleSheet("color: #18794e;")
        self.append_log(message)
        self.install_task_active = False
        self.active_task_kind = ""
        self.install_button.setText("安装/更新")
        self.uninstall_button.setText("卸载服务器")
        self._set_install_controls_enabled(True)
        self.refresh_status()

    def _remote_install_done(self, payload):
        profile, config = payload
        self._apply_discovery_profile(profile)
        self._apply_config_result(config)
        self._install_succeeded("远程安装/更新完成，状态复检通过")

    def _local_install_done(self, payload):
        config, lifecycle = payload
        self.lifecycle = lifecycle
        self._apply_config_result(config)
        self._install_succeeded("本机安装/更新完成，服务器已启动")
    def copy_address(self): QApplication.clipboard().setText(self.connect_addr.text().replace("连接地址：", "")); self.append_log("连接地址已复制")
    def check_port(self):
        if self.selected:
            in_use = NetworkDiagnostics.local_udp_in_use(self.selected.game_port) if self.selected.kind == "local" else NetworkDiagnostics.port_available(self.selected.host, self.selected.game_port)
            self.append_log(("本地 UDP 端口已被占用" if in_use else "端口检查通过") if self.selected.kind == "local" else ("远程端口可连接" if in_use else "远程端口不可连接"))
    def add_firewall_rule(self):
        if not self.selected or self.selected.kind != "local": return
        self.run_async(lambda: FirewallService.add_windows_udp_rule(f"Palworld UDP {self.selected.game_port}", self.selected.game_port), lambda _: self.append_log("Windows 防火墙规则已添加"))
    def create_shortcut(self):
        self.run_async(WindowsShortcutService.create_desktop_shortcut, lambda p: self.append_log(f"桌面快捷方式已创建：{p}"))
    def test_rest(self):
        if not self.selected: return
        self.run_async(lambda: self._rest_client().health(), lambda _: self.rest_status.setText("REST：连接成功"))
    def rest_save(self):
        if self.selected and self.selected.rest_url: self._rest_action("保存世界", "save", {})
    def broadcast(self):
        if not self.selected: return
        message, ok = QInputDialog.getText(self, "广播", "消息")
        if ok and message: self.run_async(lambda: self._rest_client().announce(message), lambda _: self.append_log("广播已发送"))
    def backup(self):
        if not self.selected: return
        selected = self.selected; destination = self._backup_destination(selected)
        if selected.kind == "remote":
            client = self._remote_client(); admin = self.storage.get_secret(selected.admin_secret_ref); rest_user = self.rest_user_edit.text().strip() or "admin"; worker = Worker(lambda signals: self._run_remote_backup(signals, selected, client, destination, admin, rest_user), with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.finished.connect(self._backup_done); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "备份失败", e)); self.pool.start(worker)
        else:
            self.run_async(lambda: BackupService().create_local(selected, destination), self._backup_done)

    @staticmethod
    def _run_remote_backup(signals, selected, client, destination, admin_password, rest_user):
        lifecycle = RemoteServerLifecycle(selected, client, signals.log.emit)
        was_running = lifecycle.status() == "active"
        tunnel = SSHTunnelManager(client)
        try:
            try:
                tunnel.start("127.0.0.1", int(selected.remote_profile.get("rest_port") or 8212)); PalworldRestClient(tunnel.base_url, admin_password, rest_user).save(); signals.log.emit("备份前已保存世界")
            except Exception as exc: signals.log.emit(f"保存世界请求失败，将通过停服保证备份一致性：{exc}")
            if was_running: lifecycle.stop()
            return BackupService().create_remote(client, selected, destination, selected.install_dir)
        finally:
            tunnel.close()
            if was_running: lifecycle.start()

    def _backup_done(self, path):
        if self.selected and path:
            self.selected.last_backup = str(path); self.storage.save_instances(self.instances)
            if hasattr(self, "health_labels"): self.health_labels["backup"].setText(str(path))
        self.append_log(f"备份完成：{path}" if path else "未发现需要备份的存档")
    def restore(self):
        if not self.selected or self.selected.kind != "local": return
        file, _ = QFileDialog.getOpenFileName(self, "选择备份", str(self._backup_destination(self.selected)), "Zip (*.zip)")
        if file and QMessageBox.question(self, "确认恢复", "恢复前必须确保服务器已停止，继续吗？") == QMessageBox.Yes: self.run_async(lambda: BackupService().restore_local(self.selected, Path(file)), lambda _: self.append_log("恢复完成"))
    def save_ini(self):
        if not self.selected or not self.selected.install_dir: return QMessageBox.warning(self, "提示", "请先填写本地安装目录")
        try:
            values = {key: coerce_setting_value(key, self._setting_text(edit)) for key, edit in self.ini_fields.items() if self._setting_text(edit) != ""}
            for key, value in values.items():
                definition = SETTING_BY_KEY.get(key)
                if definition and definition.minimum is not None and isinstance(value, (int, float)) and not (definition.minimum <= value <= definition.maximum):
                    raise ValueError(f"{definition.label} 必须在 {definition.minimum:g} 到 {definition.maximum:g} 之间")
            if self.rest_password_edit.text():
                values["AdminPassword"] = self.rest_password_edit.text()
                self._ensure_admin_password()
        except Exception as exc:
            return QMessageBox.critical(self, "配置错误", str(exc))
        if self.selected.kind == "remote":
            client = self._remote_client(); selected = self.selected
            worker = Worker(lambda signals: ServerConfigBootstrap.update_remote(client, selected, values), with_signals=True)
            worker.signals.finished.connect(self._config_saved); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "配置错误", e)); self.pool.start(worker)
        else:
            try: self._config_saved(ServerConfigBootstrap.update_local(self.selected, values))
            except Exception as exc: QMessageBox.critical(self, "配置错误", str(exc))

    def _config_saved(self, result):
        self._apply_config_result(result)
        self.selected.config_source = "用户修改"
        self.selected.config_restart_required = True
        self.config_source_label.setText("配置状态：用户修改，需要重启")
        self.storage.save_instances(self.instances)
        self.append_log("配置已保存并创建备份，需要重启服务器后生效")

    def load_ini(self):
        if not self.selected or not self.selected.install_dir: return QMessageBox.warning(self, "提示", "请先填写本地安装目录")
        if self.selected.kind == "remote":
            client = self._remote_client(); selected = self.selected
            worker = Worker(lambda signals: ServerConfigBootstrap.read_remote(client, selected), with_signals=True)
            worker.signals.finished.connect(lambda result: (self._apply_config_result(result), self.append_log("已读取远程配置文件"))); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "配置错误", e)); self.pool.start(worker)
        else:
            try:
                self._apply_config_result(ServerConfigBootstrap.read_local(self.selected)); self.append_log("已读取配置文件")
            except Exception as exc: QMessageBox.critical(self, "配置错误", str(exc))

    def _apply_config_result(self, result: ConfigSyncResult):
        if not self.selected:
            return
        for key, edit in self.ini_fields.items():
            if key in result.values:
                self._set_setting_widget(edit, result.values[key])
        self.config_original = {key: result.values.get(key, SETTING_BY_KEY[key].default) for key in self.ini_fields}
        admin_password = str(result.values.get("AdminPassword") or "")
        if admin_password:
            self.selected.admin_secret_ref = self.selected.admin_secret_ref or f"rest-{self.selected.id}"
            self.storage.set_secret(self.selected.admin_secret_ref, admin_password)
            self.rest_password_edit.setText(admin_password)
        game_port = result.values.get("PublicPort")
        if isinstance(game_port, (int, float)) or str(game_port).isdigit():
            self.selected.game_port = int(game_port); self.port_spin.setValue(int(game_port))
            if self.selected.kind == "remote": self.selected.remote_profile["game_port"] = int(game_port)
        rest_enabled = result.values.get("RESTAPIEnabled") is True
        rest_port = result.values.get("RESTAPIPort")
        if rest_enabled and (isinstance(rest_port, (int, float)) or str(rest_port).isdigit()):
            self.selected.rest_url = f"http://{self.selected.host}:{int(rest_port)}"; self.rest_edit.setText(self.selected.rest_url)
            if self.selected.kind == "remote": self.selected.remote_profile["rest_port"] = int(rest_port)
        self.selected.rcon_enabled = result.values.get("RCONEnabled") is True
        rcon_port = result.values.get("RCONPort")
        if isinstance(rcon_port, (int, float)) or str(rcon_port).isdigit(): self.selected.rcon_port = int(rcon_port)
        if admin_password and not self.selected.rcon_secret_ref: self.selected.rcon_secret_ref = self.selected.admin_secret_ref
        self.selected.config_source = result.source
        self.selected.config_synced_at = result.synced_at
        self.selected.config_restart_required = False
        self.config_source_label.setText(f"配置状态：{result.source}，同步于 {result.synced_at}")
        if self.selected.kind == "remote":
            self.selected.remote_profile["config_path"] = result.config_path
            self.selected.remote_profile["config_synced_at"] = result.synced_at
            self.selected.remote_profile["config_status"] = result.source
        self.storage.save_instances(self.instances)
        self._config_changed()

    @staticmethod
    def _setting_text(widget) -> str:
        return widget.currentText() if isinstance(widget, QComboBox) else widget.text()

    @staticmethod
    def _set_setting_widget(widget, value) -> None:
        text = "True" if value is True else "False" if value is False else str(value)
        if isinstance(widget, QComboBox): widget.setCurrentText(text)
        else: widget.setText(text)

    def _config_changed(self, *_args):
        if not hasattr(self, "config_diff_label"): return
        changed = [SETTING_BY_KEY[key].label for key, widget in self.ini_fields.items() if self._setting_text(widget) != str(self.config_original.get(key, SETTING_BY_KEY[key].default))]
        self.config_diff_label.setText("尚无修改" if not changed else f"已修改 {len(changed)} 项：" + "、".join(changed[:8]) + ("…" if len(changed) > 8 else ""))
        self._filter_config_fields()

    def _filter_config_fields(self, *_args):
        if not hasattr(self, "config_search"): return
        query = self.config_search.text().strip().lower(); modified_only = self.modified_only.isChecked()
        for definition in SETTING_DEFINITIONS:
            widget = self.ini_fields[definition.key]; changed = self._setting_text(widget) != str(self.config_original.get(definition.key, definition.default)); visible = (not query or query in definition.label.lower() or query in definition.key.lower()) and (not modified_only or changed)
            widget.setVisible(visible); label = self.config_forms[definition.category].labelForField(widget)
            if label: label.setVisible(visible)

    def apply_config_preset(self):
        preset = PRESETS[self.preset_combo.currentText()]
        for key, value in preset.items():
            if key in self.ini_fields: self._set_setting_widget(self.ini_fields[key], value)
        self._config_changed(); self.append_log(f"已应用配置预设：{self.preset_combo.currentText()}，尚未保存")

    def reset_config_category(self):
        category = self.config_categories.tabText(self.config_categories.currentIndex())
        for definition in SETTING_DEFINITIONS:
            if definition.category == category: self._set_setting_widget(self.ini_fields[definition.key], definition.default)
        self._config_changed()

    def refresh_players(self):
        if not self.selected: return
        worker = Worker(lambda signals: PlayerAdminService(self._rest_client()).list_players(), with_signals=True); worker.signals.finished.connect(self._players_loaded); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "玩家刷新失败", e)); self.pool.start(worker)

    def _players_loaded(self, players):
        if not self.selected: return
        self.player_repository.overlay_online(self.selected.id, players)
        self.current_players = self.player_repository.list_players(self.selected.id)
        self._render_players(); self._enforce_whitelist(players); self.append_log(f"玩家列表已刷新：{len(players)} 人在线，永久档案 {len(self.current_players)} 人")

    def _enforce_whitelist(self, players):
        if not self.selected or not self.selected.whitelist: return
        unauthorized = WhitelistService.unauthorized(self.selected.whitelist, [p.user_id or p.player_uid for p in players])
        if not unauthorized: return
        self.append_log("白名单检测到未授权玩家：" + "、".join(unauthorized))
        if self.selected.whitelist_policy == "warn": self.run_async(lambda: self._rest_client().announce("你不在服务器白名单中，请联系管理员。"))
        elif self.selected.whitelist_policy == "kick":
            for uid in unauthorized: self.run_async(lambda uid=uid: PlayerAdminService(self._rest_client()).kick(uid, "未加入服务器白名单"), lambda _, uid=uid: self._admin_action_done("白名单自动踢出", uid))

    def _render_players(self, *_args):
        if not hasattr(self, "players_table"): return
        query = self.player_search.text().strip().lower() if hasattr(self, "player_search") else ""; state = self.player_state_filter.currentData() if hasattr(self, "player_state_filter") else "all"
        rows = [p for p in self.current_players if (not query or query in p.name.lower() or query in p.user_id.lower() or query in p.player_uid.lower() or query in p.account_name.lower()) and (state == "all" or (state == "online" and p.online) or (state == "offline" and not p.online and p.save_status != "missing") or (state == "missing" and p.save_status == "missing"))]
        self.players_table.setRowCount(len(rows))
        for row, player in enumerate(rows):
            values = ("在线" if player.online else "离线", player.name, player.account_name, player.user_id, player.player_uid, player.level, player.experience, player.last_seen or "-", "缺失" if player.save_status == "missing" else "存在")
            for column, value in enumerate(values): self.players_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _selected_player_id(self) -> str:
        row = self.players_table.currentRow(); item = self.players_table.item(row, 3) if row >= 0 else None
        return item.text() if item else ""

    def _selected_player_uid(self) -> str:
        row = self.players_table.currentRow(); item = self.players_table.item(row, 4) if row >= 0 else None
        return item.text() if item else ""

    def _show_player_detail(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        if not self.selected or row < 0: return
        uid_item = self.players_table.item(row, 4)
        if not uid_item: return
        detail = self.player_repository.player_detail(self.selected.id, uid_item.text())
        player = detail.get("player", {}); pals = detail.get("pals", []); items = detail.get("items", []); guild = detail.get("guild", {})
        self.player_detail_title.setText(player.get("nickname") or player.get("account_name") or uid_item.text())
        self.player_note.setText(player.get("note") or "")
        masked_ips = ", ".join(__import__("json").loads(player.get("masked_ips") or "[]")) or "无"
        self.player_detail_text.setPlainText(
            f"玩家 UID：{uid_item.text()}\n平台用户 ID：{player.get('user_id') or '-'}\n"
            f"状态：{'在线' if player.get('online') else '离线'} / 存档 {player.get('save_status')}\n"
            f"等级 / 经验：{player.get('level', 0)} / {player.get('experience', 0)}\n"
            f"首次 / 最后出现：{player.get('first_seen') or '-'} / {player.get('last_seen') or '-'}\n"
            f"历史 IP（脱敏）：{masked_ips}\n公会：{guild.get('guild_name') or '-'}\n"
            f"关联帕鲁：{len(pals)} 只\n背包记录：{len(items)} 项"
        )

    def save_player_note(self):
        if not self.selected or not self._selected_player_uid(): return
        self.player_repository.set_note(self.selected.id, self._selected_player_uid(), self.player_note.text().strip())
        self.current_players = self.player_repository.list_players(self.selected.id); self._render_players(); self.append_log("玩家备注已保存")

    def edit_selected_player(self):
        uid = self._selected_player_uid()
        if not uid: return QMessageBox.information(self, "玩家编辑", "请先选择玩家。")
        if not isinstance(self.save_document, PluginParsedSave):
            return QMessageBox.information(self, "玩家编辑", "请先点击“同步完整存档”，并确保 PlM 插件可用。")
        players = self.save_document.properties.get("players", [])
        player = next((item for item in players if str(item.get("player_uid")) == uid), None)
        if not player: return QMessageBox.warning(self, "玩家编辑", "当前存档副本中找不到该玩家。")
        level, ok = QInputDialog.getInt(self, "编辑玩家等级", "目标等级（写回前仍会进行二次解析验证）：", int(player.get("level") or 1), 1, 100)
        if not ok: return
        experience, ok = QInputDialog.getInt(self, "编辑玩家经验", "目标经验（必须为非负整数）：", int(player.get("exp") or 0), 0, 2147483647)
        if not ok: return
        player_index = players.index(player)
        wanted = {f"players[{player_index}].level": str(level), f"players[{player_index}].exp": str(experience)}
        for row in range(self.save_fields_table.rowCount()):
            path_item = self.save_fields_table.item(row, 0)
            if path_item and path_item.text() in wanted:
                self.save_fields_table.item(row, 2).setText(wanted[path_item.text()])
        self.append_log(f"已暂存玩家 {uid} 的等级/经验修改，尚未写回服务器")

    def kick_player(self): self._confirm_player_action("踢出玩家", "kick")
    def ban_player(self): self._confirm_player_action("封禁玩家", "ban")

    def _confirm_player_action(self, label: str, method: str):
        user_id = self._selected_player_id()
        if not user_id: return QMessageBox.information(self, "提示", "请先选择玩家。")
        message, ok = QInputDialog.getText(self, label, "原因/提示消息（可留空）")
        if not ok or QMessageBox.question(self, "确认操作", f"确认{label} {user_id}？") != QMessageBox.Yes: return
        self.run_async(lambda: getattr(PlayerAdminService(self._rest_client()), method)(user_id, message), lambda _: self._admin_action_done(label, user_id))

    def unban_player(self):
        user_id, ok = QInputDialog.getText(self, "解除封禁", "用户 ID")
        if ok and user_id and QMessageBox.question(self, "确认操作", f"确认解除封禁 {user_id}？") == QMessageBox.Yes: self.run_async(lambda: PlayerAdminService(self._rest_client()).unban(user_id), lambda _: self._admin_action_done("解除封禁", user_id))

    def _admin_action_done(self, action: str, target: str):
        if self.selected:
            AuditService.record(self.selected, action, target); self.storage.save_instances(self.instances); self._render_audit()
        self.append_log(f"{action}已执行：{target}")

    def refresh_guilds(self):
        if not self.selected: return
        def load():
            client = self._rest_client(); players = PlayerAdminService(client).list_players(); return players, GuildSnapshotService(client).list_guilds(players)
        self.run_async(load, self._guilds_loaded)

    def _guilds_loaded(self, payload):
        self.current_players, self.current_guilds = payload; self._render_guilds(); self.append_log(f"公会快照已刷新：{len(self.current_guilds)} 个公会")

    def _render_guilds(self):
        if not hasattr(self, "guilds_table"): return
        self.guilds_table.setRowCount(len(self.current_guilds))
        for row, guild in enumerate(self.current_guilds):
            for column, value in enumerate((guild.name, guild.guild_id, guild.member_count, guild.online_count, guild.average_level, guild.base_count, guild.pal_count)): self.guilds_table.setItem(row, column, QTableWidgetItem(str(value)))
        self._show_guild_members(self.guilds_table.currentRow(), 0, -1, -1)

    def _show_guild_members(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        self.guild_members.setPlainText("\n".join(self.current_guilds[row].members) if 0 <= row < len(self.current_guilds) else "")

    def _find_save_path(self):
        if not self.selected or not self.selected.install_dir:
            raise RuntimeError("实例尚未安装或未完成路径检测")
        if self.selected.kind == "local":
            candidates = sorted((Path(self.selected.install_dir) / "Pal" / "Saved" / "SaveGames").glob("**/Level.sav"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                raise FileNotFoundError("未找到 Level.sav，请先启动服务器并创建世界")
            return str(candidates[0])
        save_dir = str(self.selected.remote_profile.get("save_dir") or f"{self.selected.install_dir}/Pal/Saved")
        code, output, error = self._remote_client().run(f"find {self._shell_quote(save_dir)} -type f -name Level.sav -printf '%T@ %p\\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-")
        if code or not output.strip():
            raise FileNotFoundError(error.strip() or "远程服务器未找到 Level.sav")
        return output.strip()

    @staticmethod
    def _shell_quote(value: str) -> str:
        import shlex
        return shlex.quote(value)

    def load_save_snapshot(self):
        if not self.selected: return
        selected = self.selected
        def load():
            path = self._find_save_path()
            local = Path(path)
            if selected.kind == "remote":
                local = self.storage.root / "cache" / selected.id / "world" / "Level.sav"
                self._download_remote_save_bundle(self._remote_client(), path, local)
            service = SaveGameService(); document = service.load(local); values = service.flatten(document.properties)
            return document, values, path, local
        self.run_async(load, self._save_snapshot_loaded)

    def _save_snapshot_loaded(self, payload):
        self.save_document, self.save_scalar_values, remote_path, local = payload
        self.save_remote_path, self.save_working_path = remote_path, local
        if isinstance(self.save_document, PluginParsedSave) and self.selected:
            run_id = self.player_repository.begin_sync(self.selected.id)
            try:
                count = self.player_repository.upsert_save_snapshot(self.selected.id, self.save_document.properties)
                self.player_repository.finish_sync(run_id, "success", count)
                self.current_players = self.player_repository.list_players(self.selected.id); self._render_players()
            except Exception as exc:
                self.player_repository.finish_sync(run_id, "failed", detail=str(exc)); raise
        self.save_path_label.setText(f"已载入：{remote_path} · {len(self.save_scalar_values)} 个结构化字段")
        self._render_save_fields(); self.append_log("存档副本已解析，尚未修改服务器文件")

    @staticmethod
    def _download_remote_save_bundle(client, remote_level: str, local_level: Path) -> None:
        from pathlib import PurePosixPath
        remote_world = str(PurePosixPath(remote_level).parent); local_level.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(remote_level, local_level)
        remote_players = str(PurePosixPath(remote_world) / "Players")
        code, output, _error = client.run(f"find {MainWindow._shell_quote(remote_players)} -maxdepth 1 -type f -name '*.sav' -printf '%f\\n' 2>/dev/null")
        if code: return
        local_players = local_level.parent / "Players"; local_players.mkdir(exist_ok=True)
        for name in output.splitlines():
            safe_name = PurePosixPath(name).name
            if safe_name == name and safe_name.endswith(".sav"):
                client.download_file(str(PurePosixPath(remote_players) / safe_name), local_players / safe_name)

    def _render_save_fields(self, *_args):
        if not hasattr(self, "save_fields_table"): return
        query = self.save_search.text().strip().lower() if hasattr(self, "save_search") else ""
        scope = self.save_scope.currentData() if hasattr(self, "save_scope") else "all"
        scope_terms = {"player": ("player", "individualid"), "pal": ("pal", "charactercontainer"), "inventory": ("item", "inventory", "container", "slot"), "guild": ("guild", "group", "basecamp")}
        old_edits = {}
        for row in range(self.save_fields_table.rowCount()):
            path = self.save_fields_table.item(row, 0); changed = self.save_fields_table.item(row, 2)
            if path and changed: old_edits[path.text()] = changed.text()
        rows = []
        for path, value in self.save_scalar_values.items():
            edit = old_edits.get(path, str(value))
            changed = edit != str(value)
            scoped = scope == "all" or any(term in path.lower() for term in scope_terms.get(scope, ()))
            if scoped and (not query or query in path.lower() or query in str(value).lower()) and (not self.save_changed_only.isChecked() or changed): rows.append((path, value, edit))
        self.save_fields_table.setRowCount(len(rows))
        for row, (path, value, edit) in enumerate(rows):
            path_item = QTableWidgetItem(path); path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable); original = QTableWidgetItem(str(value)); original.setFlags(original.flags() & ~Qt.ItemIsEditable)
            self.save_fields_table.setItem(row, 0, path_item); self.save_fields_table.setItem(row, 1, original); self.save_fields_table.setItem(row, 2, QTableWidgetItem(edit))

    def validate_save_snapshot(self):
        if not self.save_working_path: return QMessageBox.information(self, "存档", "请先载入存档副本")
        result = SaveGameService().validate(Path(self.save_working_path))
        QMessageBox.information(self, "存档验证", "存档可正常解析" if result.valid else "\n".join(result.errors))

    @staticmethod
    def _coerce_save_value(text: str, original):
        if original is None: return None if text.strip().lower() in {"", "none", "null"} else text
        if isinstance(original, bool):
            if text.lower() not in {"true", "false"}: raise ValueError("布尔值只能是 True 或 False")
            return text.lower() == "true"
        if isinstance(original, int): return int(text)
        if isinstance(original, float): return float(text)
        return text

    def apply_save_changes(self):
        if not self.selected or not self.save_document: return QMessageBox.information(self, "存档", "请先载入存档副本")
        edits = []
        for row in range(self.save_fields_table.rowCount()):
            path, original, changed = (self.save_fields_table.item(row, c) for c in range(3))
            if path and original and changed and original.text() != changed.text(): edits.append((path.text(), changed.text()))
        if not edits: return QMessageBox.information(self, "存档", "没有需要写回的修改")
        if isinstance(self.save_document, PluginParsedSave):
            allowed = re.compile(r"^players\[\d+\]\.(nickname|level|exp|hp|shield_hp|full_stomach|status_point(?:\.[^.\[]+|\[[0-9]+\])*)$")
            rejected = [path for path, _text in edits if not allowed.match(path)]
            if rejected:
                return QMessageBox.warning(self, "不支持的 PlM 修改", "为保护存档，以下字段当前只读：\n" + "\n".join(rejected[:12]))
        name, ok = QInputDialog.getText(self, "高风险存档操作", f"将修改 {len(edits)} 个字段。请输入实例名称“{self.selected.name}”确认：")
        if not ok or name != self.selected.name: return
        reason, ok = QInputDialog.getText(self, "操作原因", "请输入本次修改原因：")
        if not ok or not reason.strip(): return
        QMessageBox.information(self, "存档事务", "即将保存世界、停止服务、创建双重备份并验证写回。任务完成前请勿关闭程序。")
        selected = self.selected; service = SaveGameService()
        def mutate(document):
            for path, text in edits:
                original = self.save_scalar_values[path]; service.set_path(document.properties, path, self._coerce_save_value(text, original))
        def run(signals):
            from .management import SaveTransaction
            lifecycle = self._remote_lifecycle() if selected.kind == "remote" else self.lifecycle
            try:
                try: self._rest_client().save()
                except Exception: pass
                if selected.kind == "remote":
                    backup_root = self._backup_destination(selected)
                    return SaveTransaction(service).execute_remote(self._remote_client(), self.save_remote_path, backup_root, mutate, lifecycle.stop, lifecycle.start, lambda: self._remote_health_ok(selected), lambda: BackupService().create_remote(self._remote_client(), selected, backup_root, selected.install_dir))
                backup_root = self._backup_destination(selected)
                return SaveTransaction(service).execute_local(Path(self.save_remote_path), backup_root, mutate, [], lifecycle.stop, lifecycle.start, lambda: lifecycle.status() == "running", lambda: BackupService().create_local(selected, backup_root))
            finally:
                self._close_rest_tunnel()
        worker = Worker(run, with_signals=True); worker.signals.finished.connect(lambda backup: self._save_apply_done(backup, reason)); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "存档事务失败", e)); self.pool.start(worker)

    def _remote_health_ok(self, selected):
        snapshot = ServerDiagnostics.collect_remote(self._remote_client(), selected, None)
        return snapshot.service_state == "active" and snapshot.pid > 0 and bool(snapshot.game_endpoint and snapshot.game_endpoint.listening)

    def _save_apply_done(self, backup, reason):
        AuditService.record(self.selected, "高级存档修改", str(self.save_remote_path), detail=reason); self.storage.save_instances(self.instances); self._render_audit(); self.append_log(f"存档修改完成，回滚备份：{backup}"); self.load_save_snapshot()

    def _refresh_plm_plugin_status(self):
        if not hasattr(self, "plm_plugin_status"): return
        ready, detail = PlmCodecPlugin(self.storage.root).probe()
        self.plm_plugin_status.setText(("可用" if ready else "只读") + f" · 固定提交 {PALWORLD_SAVE_TOOLS_COMMIT[:12]} · {detail}")

    def install_plm_plugin(self):
        plugin = PlmCodecPlugin(self.storage.root)
        install_tools = False
        if not plugin.detect_msvc() and sys.platform == "win32":
            answer = QMessageBox.question(
                self,
                "安装 C++ 构建工具",
                "当前未检测到 Visual Studio C++ Build Tools。\n"
                "继续后会从微软官方下载已签名的安装程序，并请求 UAC 安装 C++ Build Tools 工作负载。\n"
                "插件与主程序隔离；取消后其他功能仍可使用，但 PlM 存档保持只读。",
            )
            if answer != QMessageBox.Yes: return
            install_tools = True
        self.plm_plugin_status.setText("正在构建隔离的 PlM 插件，请查看日志与 UAC 提示…")
        worker = Worker(lambda signals: plugin.build(signals.log.emit, install_tools=install_tools), with_signals=True)
        worker.signals.log.connect(self.append_log)
        worker.signals.finished.connect(lambda result: (self._refresh_plm_plugin_status(), self.append_log(f"PlM 插件安装完成：{result.commit}")))
        worker.signals.error.connect(lambda error: (self._refresh_plm_plugin_status(), QMessageBox.critical(self, "PlM 插件安装失败", error)))
        self.pool.start(worker)

    def _rcon_client(self):
        if not self.selected: raise RuntimeError("未选择实例")
        host, port = "127.0.0.1", int(self.selected.rcon_port or 25575)
        if self.selected.kind == "remote":
            if not self.rcon_tunnel or not self.rcon_tunnel.local_port:
                self.rcon_tunnel = SSHTunnelManager(self._remote_client()); port = self.rcon_tunnel.start("127.0.0.1", port)
            else: port = self.rcon_tunnel.local_port
        password = self.storage.get_secret(self.selected.rcon_secret_ref or self.selected.admin_secret_ref)
        return RconClient(host, port, password)

    def execute_rcon(self):
        command = self.rcon_command.currentText().strip()
        if not command: return
        dangerous = command.split()[0].lower() in {"shutdown", "doexit", "kickplayer", "banplayer"}
        if dangerous and QMessageBox.question(self, "确认 RCON 命令", f"确认执行高风险命令？\n{command}") != QMessageBox.Yes: return
        self.run_async(lambda: self._rcon_client().command(command), lambda output: self._rcon_done(command, output))

    def _rcon_done(self, command, output):
        self.rcon_status.setText("已连接"); self.rcon_output.appendPlainText(f"> {command}\n{output or '(无输出)'}"); AuditService.record(self.selected, "RCON", command); self.storage.save_instances(self.instances); self._render_audit()

    def add_schedule(self):
        if not self.selected: return
        task = ScheduleDefinition(name=f"{self.task_action.currentText()} {self.task_time.text()}", action=self.task_action.currentText(), schedule=self.task_time.text().strip(), retention=self.task_retention.value())
        try: AutomationService.validate(asdict(task))
        except Exception as exc: return QMessageBox.warning(self, "计划任务", str(exc))
        self.selected.schedules.append(asdict(task)); self.storage.save_instances(self.instances); self._render_schedules()

    def _render_schedules(self):
        if not hasattr(self, "schedule_table") or not self.selected: return
        self.schedule_table.setRowCount(len(self.selected.schedules))
        for row, raw in enumerate(self.selected.schedules):
            values = ("是" if raw.get("enabled") else "否", raw.get("name", ""), raw.get("action", ""), raw.get("schedule", ""), raw.get("retention", 14))
            for column, value in enumerate(values): self.schedule_table.setItem(row, column, QTableWidgetItem(str(value)))

    def toggle_schedule(self):
        if not self.selected: return
        row = self.schedule_table.currentRow()
        if row < 0 or row >= len(self.selected.schedules): return QMessageBox.information(self, "计划任务", "请先选择计划任务")
        self.selected.schedules[row]["enabled"] = not bool(self.selected.schedules[row].get("enabled")); self.storage.save_instances(self.instances); self._render_schedules()

    def deploy_schedules(self):
        if not self.selected: return
        if not self.selected.schedules: return QMessageBox.information(self, "计划任务", "请先添加计划任务")
        selected = self.selected
        def deploy():
            import json, subprocess
            enabled = [AutomationService.validate(raw) for raw in selected.schedules if raw.get("enabled")]
            if not enabled: raise RuntimeError("没有已启用的计划任务")
            if selected.kind == "local":
                for task in enabled:
                    args = HostTaskDeployer.windows_task_arguments(selected.id, task, sys.executable, str(Path(__file__).resolve().parent.parent / "run.py"))
                    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
                    if result.returncode: raise RuntimeError(result.stderr or result.stdout or "创建 Windows 任务计划失败")
                return len(enabled)
            client = self._remote_client(); helper_tmp = f"/tmp/palworld-task-{selected.id}.py"; client.upload_text(helper_tmp, HostTaskDeployer.remote_helper())
            code, output, error = client.run(f"sudo -n install -d -m 700 /usr/local/lib/palworld-console /etc/palworld-console && sudo -n install -m 700 {self._shell_quote(helper_tmp)} /usr/local/lib/palworld-console/task.py && rm -f {self._shell_quote(helper_tmp)}")
            if code: raise RuntimeError(error.strip() or output.strip())
            for task in enabled:
                config = {"action": task.action, "install_dir": selected.install_dir, "service": selected.remote_profile.get("service_name") or "palworld", "config_path": selected.remote_profile.get("config_path") or "", "rest_port": selected.remote_profile.get("rest_port") or 8212, "steamcmd": selected.remote_profile.get("steamcmd_path") or "steamcmd", "backup_dir": f"{selected.install_dir}/Pal/Saved/Backups", "retention": task.retention, "message": task.payload.get("message", "服务器计划通知"), "allowed": [item.player_uid for item in WhitelistService.normalize(selected.whitelist) if item.enabled], "policy": selected.whitelist_policy}
                config_tmp = f"/tmp/palworld-task-{task.id}.json"; client.upload_text(config_tmp, json.dumps(config, ensure_ascii=False))
                service_name = f"palworld-console-{selected.id[:8]}-{task.id[:8]}"; config_path = f"/etc/palworld-console/{service_name}.json"
                command = f"/usr/bin/python3 /usr/local/lib/palworld-console/task.py {config_path}"; service_text, timer_text = HostTaskDeployer.systemd_units(selected.id, task, command)
                service_tmp, timer_tmp = f"/tmp/{service_name}.service", f"/tmp/{service_name}.timer"; client.upload_text(service_tmp, service_text); client.upload_text(timer_tmp, timer_text)
                script = f"sudo -n install -m 600 {self._shell_quote(config_tmp)} {self._shell_quote(config_path)} && sudo -n install -m 644 {self._shell_quote(service_tmp)} /etc/systemd/system/{service_name}.service && sudo -n install -m 644 {self._shell_quote(timer_tmp)} /etc/systemd/system/{service_name}.timer && rm -f {self._shell_quote(config_tmp)} {self._shell_quote(service_tmp)} {self._shell_quote(timer_tmp)} && sudo -n systemctl daemon-reload && sudo -n systemctl enable --now {service_name}.timer"
                code, output, error = client.run(script)
                if code: raise RuntimeError(error.strip() or output.strip())
            return len(enabled)
        self.run_async(deploy, lambda count: self._schedule_deployed(count))

    def _schedule_deployed(self, count):
        AuditService.record(self.selected, "部署计划任务", str(count)); self.storage.save_instances(self.instances); self._render_audit(); QMessageBox.information(self, "计划任务", f"已部署 {count} 个主机级计划任务")

    def add_whitelist(self):
        if not self.selected: return
        uid = self.whitelist_uid.text().strip()
        if not uid: return QMessageBox.warning(self, "白名单", "玩家 UID 不能为空")
        self.selected.whitelist.append({"player_uid": uid, "player_name": self.whitelist_name.text().strip(), "platform": "Unknown", "note": self.whitelist_name.text().strip(), "enabled": True})
        self.selected.whitelist_policy = self.whitelist_policy.currentData(); self.selected.whitelist = [asdict(item) for item in WhitelistService.normalize(self.selected.whitelist)]; self.storage.save_instances(self.instances); self._render_whitelist()

    def _render_whitelist(self):
        if not hasattr(self, "whitelist_table") or not self.selected: return
        rows = WhitelistService.normalize(self.selected.whitelist); self.whitelist_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            for column, value in enumerate((item.player_uid, item.player_name, item.platform, item.note)): self.whitelist_table.setItem(row, column, QTableWidgetItem(str(value)))

    def refresh_backup_list(self):
        if not hasattr(self, "backup_table") or not self.selected: return
        root = self._backup_destination(self.selected); files = sorted([p for p in root.glob("*") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
        self.backup_table.setRowCount(len(files))
        from datetime import datetime
        for row, path in enumerate(files):
            valid = "待校验"
            if path.suffix.lower() == ".zip":
                try: BackupService.validate_zip(path); valid = "通过"
                except Exception: valid = "失败"
            values = (path.name, f"{path.stat().st_size / 1024 / 1024:.1f} MB", datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"), valid)
            for column, value in enumerate(values): self.backup_table.setItem(row, column, QTableWidgetItem(value))

    def export_logs(self):
        target, _ = QFileDialog.getSaveFileName(self, "导出日志", str(self.storage.root / "palworld-console.log"), "日志 (*.log);;文本 (*.txt)")
        if target: Path(target).write_text(self.log.toPlainText(), encoding="utf-8")

    def _render_audit(self):
        if not hasattr(self, "audit_table") or not self.selected: return
        events = list(reversed(self.selected.operation_history)); self.audit_table.setRowCount(len(events))
        for row, event in enumerate(events):
            for column, key in enumerate(("time", "action", "target", "result", "detail")): self.audit_table.setItem(row, column, QTableWidgetItem(str(event.get(key, ""))))

    def _restore_ui_state(self):
        settings = QSettings("JiangXiaobaiCresent", "PalworldConsole")
        geometry = settings.value("geometry")
        if geometry: self.restoreGeometry(geometry)
        sizes = settings.value("splitter")
        if sizes: self.main_splitter.restoreState(sizes)
        self.navigation.setCurrentRow(int(settings.value("page", 0)))
    def _toggle_remote_fields(self):
        remote = self.kind_combo.currentData() == "remote"
        self.path_edit.setEnabled(not remote)
        self.user_edit.setEnabled(remote); self.ssh_port_spin.setEnabled(remote); self.auth_combo.setEnabled(remote); self.ssh_password_edit.setEnabled(remote and self.auth_combo.currentData() == "password"); self.key_path_edit.setEnabled(remote and self.auth_combo.currentData() == "key"); self.key_passphrase_edit.setEnabled(remote and self.auth_combo.currentData() == "key")
        detected = bool(self.selected and self.selected.discovery_status == "ready")
        for widget in (self.port_spin, self.rest_edit, self.rest_user_edit, self.admin_password_box, self.public_edit): widget.setVisible(not remote or detected)
        self.discover_btn.setVisible(remote)

    def _remote_client(self):
        password = self.storage.get_secret(self.selected.ssh_secret_ref) if self.selected.ssh_auth_type == "password" else ""
        phrase = self.storage.get_secret(self.selected.ssh_key_passphrase_ref)
        return RemoteHostClient(self.selected.host, self.selected.remote_username, password, self.selected.ssh_port, self.selected.ssh_key_path if self.selected.ssh_auth_type == "key" else "", phrase)

    def _remote_lifecycle(self): return RemoteServerLifecycle(self.selected, self._remote_client(), self.ui_signals.log.emit)

    def discover_remote(self):
        self.save_instance()
        if not self.selected or self.selected.kind != "remote": return
        client = self._remote_client()
        selected = self.selected
        worker = Worker(lambda signals: RemoteServerInspector(client, signals.log.emit, selected.install_dir).discover(), with_signals=True)
        worker.signals.log.connect(self.append_log)
        worker.signals.finished.connect(self._discovery_done)
        worker.signals.error.connect(self._discovery_failed)
        self.pool.start(worker)

    def _discovery_done(self, profile):
        self._apply_discovery_profile(profile)
        self.append_log("SSH 连接和服务器检测完成")
        self.refresh_status()
        if profile.get("config_path"):
            client = self._remote_client(); selected = self.selected
            worker = Worker(lambda signals: ServerConfigBootstrap.read_remote(client, selected), with_signals=True)
            worker.signals.finished.connect(lambda result: (self._apply_config_result(result), self.append_log("游戏配置已自动回填")))
            worker.signals.error.connect(lambda e: self.append_log(f"配置自动回填失败：{e}"))
            self.pool.start(worker)

    def _apply_discovery_profile(self, profile):
        self.selected.remote_profile = profile; self.selected.discovery_status = "ready"
        from datetime import datetime
        self.selected.discovered_at = datetime.now().isoformat(timespec="seconds")
        self.selected.install_dir = str(profile.get("install_dir", self.selected.install_dir)); self.selected.game_port = int(profile.get("game_port", self.selected.game_port)); self.selected.rest_url = str(profile.get("rest_url", self.selected.rest_url)); self.lifecycle = self._remote_lifecycle(); self.storage.save_instances(self.instances); self.path_edit.setText(self.selected.install_dir); self.port_spin.setValue(self.selected.game_port); self.rest_edit.setText(self.selected.rest_url); self._show_discovery(); self._toggle_remote_fields()

    def _discovery_failed(self, error):
        self.selected.discovery_status = "failed"; self.storage.save_instances(self.instances); self._show_discovery(error); QMessageBox.critical(self, "SSH 检测失败", error)

    def _show_discovery(self, error=""):
        if not self.selected or self.selected.kind != "remote": self.discovery_result.setPlainText(""); return
        if error: self.discovery_result.setPlainText(f"检测失败\n{error}"); return
        profile = self.selected.remote_profile
        if not profile: self.discovery_result.setPlainText("尚未检测。请保存 SSH 信息后点击“连接并检测 SSH”。"); return
        steamcmd = profile.get("steamcmd_path") or ("未安装，部署时将自动安装" if profile.get("steamcmd_installable") else "未安装，缺少下载或解压条件")
        fields = (("系统", profile.get("os")), ("架构", profile.get("architecture")), ("磁盘", profile.get("disk")), ("sudo", "可用" if profile.get("sudo") else "不可免交互使用"), ("SteamCMD", steamcmd), ("SteamCMD 来源", profile.get("steamcmd_source") or "未知"), ("服务端", profile.get("install_dir") or "未部署"), ("systemd", f"{profile.get('service_name') or '未找到'} / {profile.get('service_state')}"), ("配置", profile.get("config_path") or "未找到"), ("REST", profile.get("rest_url") or "未启用"))
        self.discovery_result.setPlainText("\n".join(f"{key}: {value}" for key, value in fields))
    def _rest_client(self):
        if not self.selected: raise RuntimeError("未选择服务器实例")
        base_url = self.selected.rest_url
        if self.selected.kind == "remote":
            if not self.rest_tunnel or not self.rest_tunnel.local_port:
                self.rest_tunnel = SSHTunnelManager(self._remote_client()); self.rest_tunnel.start("127.0.0.1", int(self.selected.remote_profile.get("rest_port") or 8212))
            base_url = self.rest_tunnel.base_url
        if not base_url: raise RuntimeError("REST API 尚未配置")
        return PalworldRestClient(base_url, self.storage.get_secret(self.selected.admin_secret_ref), self.rest_user_edit.text().strip() or "admin")

    def _close_rest_tunnel(self):
        if self.rest_tunnel: self.rest_tunnel.close()
        self.rest_tunnel = None

    def _rest_action(self, label: str, method: str, payload: dict):
        if not self.selected: return QMessageBox.warning(self, "提示", "请先选择服务器实例")
        self.run_async(lambda: getattr(self._rest_client(), method)(**payload) if payload else getattr(self._rest_client(), method)(), lambda _: self.append_log(f"{label}请求已发送"))

    def closeEvent(self, event):
        self._close_rest_tunnel()
        if self.rcon_tunnel: self.rcon_tunnel.close()
        self.player_repository.close()
        settings = QSettings("JiangXiaobaiCresent", "PalworldConsole"); settings.setValue("geometry", self.saveGeometry()); settings.setValue("splitter", self.main_splitter.saveState()); settings.setValue("page", self.navigation.currentRow())
        super().closeEvent(event)

    def append_log(self, text: str): self.log.appendPlainText(text)
