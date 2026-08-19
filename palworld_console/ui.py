from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
import ntpath
import platform
import re
import sys
import threading
import time
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt, QProcess, QTimer
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QProgressDialog, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QLineEdit, QVBoxLayout, QWidget, QInputDialog, QAbstractItemView, QStackedWidget)
from PySide6.QtCore import QSettings

from .config_ini import coerce_setting_value
from .config_cache import ConfigCacheRepository
from .localization import GameLocalizationService
from .models import ConfigSyncResult, GuildSummary, PlayerRecord, ServerHealthSnapshot, ServerInstance, TaskProgress, UninstallResult, ScheduleDefinition
from .services import BackupService, FirewallService, GuildSnapshotService, LocalServerLifecycle, LocalSteamCmdManager, NetworkDiagnostics, PalworldRestClient, PlayerAdminService, RemoteHostClient, RemoteServerInspector, RemoteServerLifecycle, WindowsRemoteServerLifecycle, ServerConfigBootstrap, ServerDiagnostics, SSHTunnelManager, SteamCmdInstaller, WindowsShortcutService
from .management import AuditService, AutomationService, HostTaskDeployer, RconClient, SaveGameService, WhitelistService
from .player_store import PlayerIdentityGroup, PlayerIdentityService, PlayerRepository
from .player_edit import PlayerEditSession
from .player_center import PlayerCenterController
from .save_codec import PALWORLD_SAVE_TOOLS_COMMIT, PlmCodecPlugin, PluginParsedSave
from .save_fields import CONTAINER_LABELS, display_field, resolve_path, validate_value
from .mod_manager import LocalArchiveProvider, LocalPakProvider, ModEnvironment, ModManager, ModManifest, ModPackageService, WorkshopCatalogPage, WorkshopCatalogService, WorkshopProvider
from .wine_migration import WineMigrationPreflight, WineMigrationService
from .settings_schema import CATEGORIES, PRESETS, SETTING_BY_KEY, SETTING_DEFINITIONS
from .storage import AppStorage
from .backup_packages import BackupPackageService, BackupRepository, RestoreTransaction
from .save_tools import SaveToolsService
from .updater import ReleaseInfo, UpdateService


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    log = Signal(str)


def _remote_lifecycle_for(instance, client, on_log=None, on_progress=None):
    platform_name = str(instance.remote_profile.get("platform") or "linux").lower()
    if platform_name == "windows":
        return WindowsRemoteServerLifecycle(instance, client, on_log, on_progress)
    if platform_name == "unknown":
        raise RuntimeError("远程操作系统尚未识别，只能重新检测或导出诊断")
    return RemoteServerLifecycle(instance, client, on_log, on_progress)


class UiSignals(QObject):
    log = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn, with_signals=False):
        super().__init__(); self.fn = fn; self.with_signals = with_signals; self.signals = WorkerSignals()
    @Slot()
    def run(self):
        try:
            result = self.fn(self.signals) if self.with_signals else self.fn()
        except Exception as exc:
            try:
                self.signals.error.emit(str(exc))
            except RuntimeError:
                pass
            return
        try:
            self.signals.finished.emit(result)
        except RuntimeError:
            pass


class RestoreOptionsDialog(QDialog):
    def __init__(self, plan, manifest, parent=None, player_uids=(), plugin_status=(False, "PlM 插件尚未检测")):
        super().__init__(parent)
        self.setWindowTitle("恢复向导：组件与风险确认")
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        title = QLabel("恢复预检结果")
        title.setStyleSheet("font-size:18px;font-weight:650;")
        layout.addWidget(title)
        summary = QPlainTextEdit("\n".join(plan.summary))
        summary.setReadOnly(True); summary.setMaximumHeight(180); layout.addWidget(summary)
        components = QGroupBox("选择恢复组件")
        component_layout = QVBoxLayout(components)
        self.world = QCheckBox("世界与全部角色（Level.sav、Players 和世界必要文件）")
        self.world.setChecked("world" in manifest.components); self.world.setEnabled("world" in manifest.components)
        self.config = QCheckBox("服务器配置（密码将从目标实例重新注入）")
        self.config.setChecked(False); self.config.setEnabled("config" in manifest.components)
        self.player = QCheckBox("单个玩家角色（结构化合并玩家、帕鲁和现有背包槽位）")
        plugin_ready, plugin_detail = plugin_status
        self.player.setEnabled(bool(player_uids) and plugin_ready and "world" in manifest.components)
        self.player_uid = QComboBox(); self.player_uid.addItems(tuple(player_uids)); self.player_uid.setEnabled(False)
        self.player_hint = QLabel("选择后只合并稳定 UID 对应的已验证字段，不直接复制单个玩家文件。")
        self.player_hint.setWordWrap(True)
        if not self.player.isEnabled():
            self.player_hint.setText(f"单玩家恢复不可用：{plugin_detail if not plugin_ready else '备份中没有可识别玩家'}")
            self.player_hint.setStyleSheet("color:#8a4b08;")
        component_layout.addWidget(self.world); component_layout.addWidget(self.player); component_layout.addWidget(self.player_uid); component_layout.addWidget(self.player_hint); component_layout.addWidget(self.config); layout.addWidget(components)
        self.advanced = QCheckBox("我理解版本不一致或备份不完整的风险，并允许高级恢复")
        self.advanced.setVisible(plan.requires_advanced_confirmation)
        layout.addWidget(self.advanced)
        warning = QLabel("恢复前会保存世界、停止服务并创建受保护恢复点。失败时自动回滚；目标实例的 SSH、安装目录、服务名、端口和凭据不会被来源包覆盖。")
        warning.setWordWrap(True); warning.setStyleSheet("color:#8a4b08;"); layout.addWidget(warning)
        self.blocked_reason = plan.blocked_reason
        if self.blocked_reason:
            blocked = QLabel("已阻止恢复：" + self.blocked_reason); blocked.setWordWrap(True); blocked.setStyleSheet("color:#b42318;font-weight:600;"); layout.addWidget(blocked)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("继续恢复")
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject); layout.addWidget(self.buttons)
        self.world.toggled.connect(self._world_toggled); self.player.toggled.connect(self._player_toggled); self.config.toggled.connect(self._update_enabled); self.advanced.toggled.connect(self._update_enabled)
        self._update_enabled()

    def _world_toggled(self, checked):
        if checked and self.player.isChecked(): self.player.setChecked(False)
        self._update_enabled()

    def _player_toggled(self, checked):
        if checked and self.world.isChecked(): self.world.setChecked(False)
        self.player_uid.setEnabled(checked)
        self._update_enabled()

    def _update_enabled(self):
        selected = self.world.isChecked() or self.player.isChecked() or self.config.isChecked()
        world_selected = self.world.isChecked() or self.player.isChecked()
        risk_ok = not world_selected or not self.advanced.isVisible() or self.advanced.isChecked()
        blocked = bool(self.blocked_reason and world_selected)
        player_ok = not self.player.isChecked() or bool(self.player_uid.currentText())
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(selected and risk_ok and player_ok and not blocked)

    def selected_components(self) -> tuple[str, ...]:
        result = []
        if self.world.isChecked(): result.append("world")
        if self.player.isChecked(): result.append("player")
        if self.config.isChecked(): result.append("config")
        return tuple(result)

    def selected_player_uid(self) -> str:
        return self.player_uid.currentText().strip() if self.player.isChecked() else ""


class SaveDiagnosticsDialog(QDialog):
    def __init__(self, report, payload, parent=None):
        super().__init__(parent); self.setWindowTitle("存档地图与诊断"); self.resize(920, 660)
        layout = QVBoxLayout(self)
        summary = QLabel(f"玩家 {report.players} · 帕鲁 {report.pals} · 公会 {report.guilds} · 基地 {report.bases} · 风险 {len(report.findings)}")
        summary.setStyleSheet("font-size:16px;font-weight:650;"); layout.addWidget(summary)
        split = QSplitter(Qt.Vertical); layout.addWidget(split, 1)
        scene = QGraphicsScene(self); view = QGraphicsView(scene); view.setMinimumHeight(300); split.addWidget(view)
        bases = list(payload.get("bases") or []); points = []
        for base in bases:
            position = base.get("position") or {}; x = float(position.get("x") or 0); y = float(position.get("y") or 0); points.append((x, y, base))
        if points:
            xs = [item[0] for item in points]; ys = [item[1] for item in points]; min_x, max_x = min(xs), max(xs); min_y, max_y = min(ys), max(ys)
            span_x = max(1.0, max_x - min_x); span_y = max(1.0, max_y - min_y)
            for x, y, base in points:
                px = (x - min_x) / span_x * 760; py = (max_y - y) / span_y * 260
                marker = scene.addEllipse(px - 6, py - 6, 12, 12); marker.setToolTip(f"{base.get('name') or base.get('base_id')}\nX={x:g} Y={y:g}\n公会：{base.get('guild_id') or '-'}")
                label = scene.addText(str(base.get("name") or base.get("base_id") or "基地")); label.setPos(px + 8, py - 12)
            scene.setSceneRect(-20, -20, 840, 320); view.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)
        else:
            scene.addText("当前解析结果没有可显示的基地坐标")
        table = QTableWidget(len(report.findings), 6); table.setHorizontalHeaderLabels(["风险", "类别", "对象", "ID", "说明", "可修复"]); table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for row, finding in enumerate(report.findings):
            for column, value in enumerate((finding.severity, finding.category, finding.object_type, finding.object_id, finding.message, "是" if finding.repairable else "否")): table.setItem(row, column, QTableWidgetItem(str(value)))
        split.addWidget(table); split.setSizes([360, 240])
        buttons = QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject); layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("幻兽帕鲁服务器控制台")
        self.resize(1180, 760)
        self.storage = AppStorage()
        self.config_cache = ConfigCacheRepository(self.storage.root)
        self.localization = GameLocalizationService(self.storage.root)
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
        self.backup_task_started_at = 0.0
        self.backup_task_last_progress_at = 0.0
        self.backup_task_message = ""
        self.rest_tunnel: SSHTunnelManager | None = None
        self.rcon_tunnel: SSHTunnelManager | None = None
        self.current_players: list[PlayerRecord] = []
        self.current_player_groups: list[PlayerIdentityGroup] = []
        self.player_edit_sessions: dict[tuple[str, str], PlayerEditSession] = {}
        self.player_center = PlayerCenterController()
        self.player_edit_sessions = self.player_center.sessions
        self.world_edit_session: PlayerEditSession | None = None
        self.active_player_uid = ""
        self.current_guilds: list[GuildSummary] = []
        self.config_original: dict[str, object] = {}
        self.config_secret_presence: dict[str, bool] = {}
        self.ui_signals = UiSignals()
        self.ui_signals.log.connect(self.append_log)
        self.pool = QThreadPool.globalInstance()
        self.update_service = UpdateService(storage_root=self.storage.root)
        self.update_check_active = False
        self.update_cancel: threading.Event | None = None
        self.update_progress_dialog: QProgressDialog | None = None
        self._build_ui()
        self.backup_task_timer = QTimer(self); self.backup_task_timer.setInterval(1000); self.backup_task_timer.timeout.connect(self._backup_task_heartbeat)
        self._refresh_instances()
        self._restore_ui_state()
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            QTimer.singleShot(1500, lambda: self.check_for_updates(True))

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
        self.dashboard = self._dashboard_tab(); self.connection = self._connection_tab(); self.config = self._game_config_tab(); self.players_page = self._players_tab(); self.guilds_page = self._guilds_tab(); self.mods_page = self._mods_tab(); self.automation_page = self._automation_tab(); self.save_tools_page = self._save_tools_tab(); self.backups_page = self._backup_tab(); self.ops = self._ops_tab(); self.about_page = self._about_tab()
        pages = ((self.dashboard, "仪表盘"), (self.connection, "连接与部署"), (self.config, "游戏配置"), (self.players_page, "玩家中心"), (self.guilds_page, "公会与基地"), (self.mods_page, "模组管理"), (self.automation_page, "RCON 与自动化"), (self.save_tools_page, "存档工具"), (self.backups_page, "备份与恢复"), (self.ops, "日志与审计"), (self.about_page, "关于我们"))
        for page, title in pages:
            self.tabs.addTab(QWidget(), title); self.navigation.addItem(title); self.page_stack.addWidget(page)
        self.navigation.currentRowChanged.connect(self.page_stack.setCurrentIndex); self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex); self.navigation.currentRowChanged.connect(self._navigation_page_changed); self.navigation.setCurrentRow(0)
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
        self.path_edit = QLineEdit(); self.path_box = QWidget(); path_layout = QHBoxLayout(self.path_box); path_layout.setContentsMargins(0, 0, 0, 0); path_layout.addWidget(self.path_edit); browse_path = QPushButton("选择目录"); browse_path.clicked.connect(self.choose_local_install_dir); path_layout.addWidget(browse_path); self.host_edit = QLineEdit(); self.user_edit = QLineEdit(); self.ssh_port_spin = QSpinBox(); self.ssh_port_spin.setRange(1, 65535); self.ssh_port_spin.setValue(22)
        self.auth_combo = QComboBox(); self.auth_combo.addItem("密码", "password"); self.auth_combo.addItem("私钥", "key"); self.ssh_password_edit = QLineEdit(); self.ssh_password_edit.setEchoMode(QLineEdit.Password); self.key_path_edit = QLineEdit(); self.key_passphrase_edit = QLineEdit(); self.key_passphrase_edit.setEchoMode(QLineEdit.Password)
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535); self.rest_edit = QLineEdit(); self.rest_user_edit = QLineEdit("admin"); self.rest_password_edit = QLineEdit(); self.rest_password_edit.setEchoMode(QLineEdit.Password); self.public_edit = QLineEdit()
        self.admin_password_box = QWidget(); password_layout = QHBoxLayout(self.admin_password_box); password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.addWidget(self.rest_password_edit)
        self.show_admin_password_button = QPushButton("显示"); self.show_admin_password_button.clicked.connect(self.toggle_admin_password); password_layout.addWidget(self.show_admin_password_button)
        copy_password = QPushButton("复制"); copy_password.clicked.connect(self.copy_admin_password); password_layout.addWidget(copy_password)
        for label, widget in (("名称", self.name_edit), ("类型", self.kind_combo), ("本地安装目录", self.path_box), ("主机地址", self.host_edit), ("SSH 用户", self.user_edit), ("SSH 端口", self.ssh_port_spin), ("认证方式", self.auth_combo), ("SSH 密码", self.ssh_password_edit), ("私钥文件", self.key_path_edit), ("私钥口令", self.key_passphrase_edit), ("游戏端口（UDP）", self.port_spin), ("REST 远程端点（经 SSH 隧道）", self.rest_edit), ("REST 用户", self.rest_user_edit), ("REST 管理员密码", self.admin_password_box), ("公网/局域网游戏地址", self.public_edit)): form.addRow(label, widget)
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
        actions = QHBoxLayout(); load_btn = QPushButton("从服务器读取"); load_btn.clicked.connect(self.load_ini); draft_btn = QPushButton("保存离线草稿"); draft_btn.clicked.connect(self.save_config_draft); save_btn = QPushButton("推送配置（自动备份）"); save_btn.clicked.connect(self.save_ini); reset_btn = QPushButton("恢复当前分组默认值"); reset_btn.clicked.connect(self.reset_config_category)
        actions.addWidget(load_btn); actions.addWidget(draft_btn); actions.addWidget(save_btn); actions.addWidget(reset_btn); actions.addStretch(); layout.addLayout(actions)
        self.config_diff_label = QLabel("尚无修改"); self.config_diff_label.setWordWrap(True); layout.addWidget(self.config_diff_label); return outer

    def _players_tab(self):
        w = QWidget(); root = QVBoxLayout(w); self.player_view_stack = QStackedWidget(); root.addWidget(self.player_view_stack, 1)
        self.player_list_page = QWidget(); l = QVBoxLayout(self.player_list_page); controls = QHBoxLayout(); self.player_search = QLineEdit(); self.player_search.setPlaceholderText("搜索玩家、平台账号或关联 UID"); self.player_search.textChanged.connect(self._render_players); controls.addWidget(self.player_search)
        self.player_state_filter = QComboBox(); self.player_state_filter.addItem("全部状态", "all"); self.player_state_filter.addItem("在线", "online"); self.player_state_filter.addItem("离线", "offline"); self.player_state_filter.addItem("存档缺失", "missing"); self.player_state_filter.currentIndexChanged.connect(self._render_players); controls.addWidget(self.player_state_filter)
        self.player_sync_button = QPushButton("同步玩家数据"); self.player_sync_button.clicked.connect(self.sync_player_center); controls.addWidget(self.player_sync_button)
        self.player_online_refresh = QPushButton("刷新在线状态"); self.player_online_refresh.clicked.connect(self.refresh_players); self.player_online_refresh.setVisible(False); controls.addWidget(self.player_online_refresh)
        self.player_save_sync_legacy = QPushButton("同步完整存档"); self.player_save_sync_legacy.clicked.connect(self.load_save_snapshot); self.player_save_sync_legacy.setVisible(False); controls.addWidget(self.player_save_sync_legacy)
        self.player_sync_label = QLabel("尚未同步存档"); controls.addWidget(self.player_sync_label); l.addLayout(controls)
        self.player_list_hint = QLabel("同步完成后选择玩家，进入该玩家的存档详情和修改页面。"); self.player_list_hint.setWordWrap(True); l.addWidget(self.player_list_hint)
        self.players_table = QTableWidget(0, 7); self.players_table.setHorizontalHeaderLabels(["状态", "玩家", "平台账号", "等级", "最后出现", "存档", "草稿"]); self.players_table.setAlternatingRowColors(True); self.players_table.setSortingEnabled(True); self.players_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.players_table.setSelectionMode(QAbstractItemView.SingleSelection); self.players_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.players_table.horizontalHeader().setStretchLastSection(True); self.players_table.cellClicked.connect(self._show_player_detail); self.players_table.cellActivated.connect(self._show_player_detail); l.addWidget(self.players_table, 1); self.player_view_stack.addWidget(self.player_list_page)
        self.player_detail_page = QWidget(); dl = QVBoxLayout(self.player_detail_page)
        title_row = QHBoxLayout(); back = QPushButton("返回玩家列表"); back.clicked.connect(self._return_to_player_list); title_row.addWidget(back); self.player_detail_title = QLabel("玩家详情"); self.player_detail_title.setStyleSheet("font-size:16px;font-weight:650;"); title_row.addWidget(self.player_detail_title); title_row.addWidget(QLabel("角色")); self.player_role_combo = QComboBox(); self.player_role_combo.setMinimumWidth(210); self.player_role_combo.currentIndexChanged.connect(self._role_uid_changed); title_row.addWidget(self.player_role_combo); title_row.addStretch(); self.player_detail_sync_label = QLabel("尚未同步"); title_row.addWidget(self.player_detail_sync_label); self.pending_save_label = QLabel("未保存修改 0 项"); title_row.addWidget(self.pending_save_label); dl.addLayout(title_row)
        plugin_row = QHBoxLayout(); self.plm_plugin_status = QLabel("正在检测 PlM1/Oodle 插件"); self.plm_plugin_status.setWordWrap(True); plugin_row.addWidget(self.plm_plugin_status, 1); install_plugin = QPushButton("安装/修复插件"); install_plugin.clicked.connect(self.install_plm_plugin); plugin_row.addWidget(install_plugin); dl.addLayout(plugin_row)
        localization_row = QHBoxLayout(); self.localization_status = QLabel("中文资源：内置词典"); self.localization_status.setWordWrap(True); localization_row.addWidget(self.localization_status, 1); detect_client = QPushButton("检测游戏资源"); detect_client.clicked.connect(self.detect_localization_source); localization_row.addWidget(detect_client); import_names = QPushButton("导入中文资源"); import_names.clicked.connect(self.import_localization_source); localization_row.addWidget(import_names); dl.addLayout(localization_row)
        self.player_detail_tabs = QTabWidget(); dl.addWidget(self.player_detail_tabs, 1)
        overview = QWidget(); overview_l = QVBoxLayout(overview); self.player_detail_text = QPlainTextEdit(); self.player_detail_text.setReadOnly(True); overview_l.addWidget(self.player_detail_text); note_row = QHBoxLayout(); self.player_note = QLineEdit(); self.player_note.setPlaceholderText("玩家备注"); note_row.addWidget(self.player_note); note_button = QPushButton("保存备注"); note_button.clicked.connect(self.save_player_note); note_row.addWidget(note_button); overview_l.addLayout(note_row); self.player_detail_tabs.addTab(overview, "概览")
        attributes = QWidget(); al = QVBoxLayout(attributes); attr_tools = QHBoxLayout(); self.save_path_label = QLabel("尚未同步 Level.sav"); attr_tools.addWidget(self.save_path_label, 1); validate = QPushButton("验证存档"); validate.clicked.connect(self.validate_save_snapshot); attr_tools.addWidget(validate); al.addLayout(attr_tools)
        self.save_scope = QComboBox(); self.save_scope.addItem("玩家属性", "player"); self.save_scope.addItem("全部可识别字段", "all"); self.save_scope.currentIndexChanged.connect(self._render_save_fields); self.save_search = QLineEdit(); self.save_search.setPlaceholderText("搜索中文字段或当前值"); self.save_search.textChanged.connect(self._render_save_fields); self.save_changed_only = QCheckBox("仅看已修改"); self.save_changed_only.toggled.connect(self._render_save_fields); filter_row = QHBoxLayout(); filter_row.addWidget(self.save_scope); filter_row.addWidget(self.save_search, 1); filter_row.addWidget(self.save_changed_only); al.addLayout(filter_row)
        self.save_fields_table = QTableWidget(0, 7); self.save_fields_table.setHorizontalHeaderLabels(["对象", "中文字段", "当前值", "修改值", "来源", "状态", "风险"]); self.save_fields_table.setAlternatingRowColors(True); self.save_fields_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents); self.save_fields_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents); self.save_fields_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents); self.save_fields_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents); self.save_fields_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch); self.save_fields_table.itemChanged.connect(self._save_edit_changed); al.addWidget(self.save_fields_table); self.player_detail_tabs.addTab(attributes, "玩家属性")
        pals = QWidget(); pal_l = QVBoxLayout(pals); pal_split = QSplitter(Qt.Horizontal); self.player_pals_table = QTableWidget(0, 8); self.player_pals_table.setHorizontalHeaderLabels(["帕鲁", "昵称", "等级", "性别", "幸运", "星级", "个体值", "状态"]); self.player_pals_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.player_pals_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.player_pals_table.horizontalHeader().setStretchLastSection(True); self.player_pals_table.currentCellChanged.connect(self._show_pal_editor); pal_split.addWidget(self.player_pals_table)
        pal_detail = QWidget(); pal_detail_l = QVBoxLayout(pal_detail); self.pal_detail_text = QPlainTextEdit(); self.pal_detail_text.setReadOnly(True); self.pal_detail_text.setMinimumWidth(320); pal_detail_l.addWidget(self.pal_detail_text, 1); pal_editor = QGroupBox("所选帕鲁修改草稿"); pal_form = QGridLayout(pal_editor); self.pal_editors = {}
        pal_fields = (("nickname", "昵称"), ("level", "等级"), ("exp", "经验"), ("workspeed", "工作速度"), ("melee", "生命个体值"), ("ranged", "攻击个体值"), ("defense", "防御个体值"), ("rank", "星级"), ("rank_attack", "攻击强化"), ("rank_defence", "防御强化"), ("rank_craftspeed", "工作强化"), ("is_lucky", "幸运 是/否"), ("skills", "被动技能 ID"), ("active_skills", "装备主动技能 ID"), ("learned_skills", "已掌握技能 ID"))
        for index, (key, label) in enumerate(pal_fields):
            edit = QLineEdit(); self.pal_editors[key] = edit; pal_form.addWidget(QLabel(label), index // 4 * 2, index % 4); pal_form.addWidget(edit, index // 4 * 2 + 1, index % 4)
        pal_action_row = ((len(pal_fields) - 1) // 4 + 1) * 2
        fix_pals = QPushButton("生成非法数值修复草稿"); fix_pals.clicked.connect(self.stage_legal_pal_repairs); pal_form.addWidget(fix_pals, pal_action_row, 2)
        self.stage_pal_button = QPushButton("加入修改草稿"); self.stage_pal_button.clicked.connect(self.stage_selected_pal); pal_form.addWidget(self.stage_pal_button, pal_action_row, 3); pal_detail_l.addWidget(pal_editor); pal_split.addWidget(pal_detail); pal_split.setSizes([650, 390]); pal_l.addWidget(pal_split); self.player_pals_tab_index = self.player_detail_tabs.addTab(pals, "帕鲁 0")
        inventory = QWidget(); inv_l = QVBoxLayout(inventory); inv_filter = QHBoxLayout(); inv_filter.addWidget(QLabel("容器")); self.inventory_container_filter = QComboBox(); self.inventory_container_filter.addItem("全部", "all"); [self.inventory_container_filter.addItem(label, key) for key, label in CONTAINER_LABELS.items()]; self.inventory_container_filter.currentIndexChanged.connect(self._render_inventory_for_active_player); inv_filter.addWidget(self.inventory_container_filter); self.inventory_status_label = QLabel("尚未载入背包"); inv_filter.addWidget(self.inventory_status_label); inv_filter.addStretch(); inv_l.addLayout(inv_filter); self.player_inventory_table = QTableWidget(0, 5); self.player_inventory_table.setHorizontalHeaderLabels(["容器", "槽位", "物品", "物品 ID", "数量"]); self.player_inventory_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.player_inventory_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.player_inventory_table.horizontalHeader().setStretchLastSection(True); self.player_inventory_table.currentCellChanged.connect(self._show_inventory_editor); inv_l.addWidget(self.player_inventory_table)
        inventory_editor = QHBoxLayout(); self.inventory_selected_label = QLabel("选择背包槽位后可修改数量"); inventory_editor.addWidget(self.inventory_selected_label, 1); self.inventory_quantity = QSpinBox(); self.inventory_quantity.setRange(0, 999999); inventory_editor.addWidget(self.inventory_quantity); self.stage_inventory_button = QPushButton("加入修改草稿"); self.stage_inventory_button.clicked.connect(self.stage_selected_inventory); inventory_editor.addWidget(self.stage_inventory_button); inv_l.addLayout(inventory_editor); self.player_inventory_tab_index = self.player_detail_tabs.addTab(inventory, "背包 0")
        relations = QWidget(); rel_l = QVBoxLayout(relations); self.player_relations_summary = QLabel("尚未载入公会与基地关系"); self.player_relations_summary.setWordWrap(True); rel_l.addWidget(self.player_relations_summary); rel_split = QSplitter(Qt.Vertical); self.player_guild_members_table = QTableWidget(0, 3); self.player_guild_members_table.setHorizontalHeaderLabels(["公会成员", "角色 UID", "身份"]); self.player_guild_members_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.player_guild_members_table.horizontalHeader().setStretchLastSection(True); rel_split.addWidget(self.player_guild_members_table); self.player_bases_table = QTableWidget(0, 7); self.player_bases_table.setHorizontalHeaderLabels(["基地", "基地 ID", "坐标", "工作帕鲁", "工作容器", "关联容器", "状态"]); self.player_bases_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.player_bases_table.horizontalHeader().setStretchLastSection(True); rel_split.addWidget(self.player_bases_table); rel_split.setSizes([220, 320]); rel_l.addWidget(rel_split, 1); self.player_relations_tab_index = self.player_detail_tabs.addTab(relations, "公会与基地 0")
        operations = QWidget(); op_l = QVBoxLayout(operations); operation_row = QHBoxLayout();
        for text, handler in (("广播", self.broadcast), ("踢出", self.kick_player), ("封禁", self.ban_player), ("按 ID 解封", self.unban_player), ("保存世界", self.rest_save)): b=QPushButton(text); b.clicked.connect(handler); operation_row.addWidget(b)
        operation_row.addStretch(); op_l.addLayout(operation_row); op_l.addWidget(QLabel("踢出、封禁和存档写回会记录审计；高风险写回必须输入实例名称与操作原因。")); op_l.addStretch(); self.player_detail_tabs.addTab(operations, "管理操作")
        save_actions = QHBoxLayout(); self.preview_save_button = QPushButton("预览差异"); self.preview_save_button.clicked.connect(self.preview_save_changes); save_actions.addWidget(self.preview_save_button); revert = QPushButton("撤销全部修改"); revert.clicked.connect(self.revert_save_changes); save_actions.addWidget(revert); self.apply_save_button = QPushButton("保存到服务器"); self.apply_save_button.clicked.connect(self.apply_save_changes); self.apply_save_button.setStyleSheet("color:#b42318;"); save_actions.addWidget(self.apply_save_button); self.retry_save_button = QPushButton("重试上次保存"); self.retry_save_button.clicked.connect(self.apply_save_changes); self.retry_save_button.setVisible(False); save_actions.addWidget(self.retry_save_button); save_actions.addStretch(); dl.addLayout(save_actions)
        self.player_view_stack.addWidget(self.player_detail_page); self.player_view_stack.setCurrentWidget(self.player_list_page); self.save_document = None; self.save_scalar_values = {}; self.save_working_path = None; self._refresh_plm_plugin_status(); self._refresh_localization_status(); self.player_detail_tabs.setEnabled(False); return w

    def _guilds_tab(self):
        w = QWidget(); l = QVBoxLayout(w); row = QHBoxLayout(); refresh = QPushButton("刷新在线数据"); refresh.clicked.connect(self.refresh_guilds); row.addWidget(refresh); sync = QPushButton("同步完整存档"); sync.clicked.connect(self.load_save_snapshot); row.addWidget(sync); row.addWidget(QLabel("在线数据用于监控；结构化修改来自完整存档并通过停服事务写回。")); row.addStretch(); l.addLayout(row)
        self.guilds_table = QTableWidget(0, 7); self.guilds_table.setHorizontalHeaderLabels(["公会", "公会 ID", "成员", "在线", "平均等级", "基地", "帕鲁"]); self.guilds_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.guilds_table.horizontalHeader().setStretchLastSection(True); l.addWidget(self.guilds_table); self.guild_members = QPlainTextEdit(); self.guild_members.setReadOnly(True); self.guild_members.setMaximumHeight(100); l.addWidget(self.guild_members); self.guilds_table.currentCellChanged.connect(self._show_guild_members)
        editors = QGroupBox("结构化公会与基地修改草稿"); form = QGridLayout(editors)
        self.world_guild_combo = QComboBox(); self.world_guild_combo.currentIndexChanged.connect(self._show_world_guild_editor); form.addWidget(QLabel("公会"), 0, 0); form.addWidget(self.world_guild_combo, 0, 1)
        self.world_guild_name = QLineEdit(); form.addWidget(QLabel("名称"), 0, 2); form.addWidget(self.world_guild_name, 0, 3)
        self.world_guild_level = QSpinBox(); self.world_guild_level.setRange(1, 30); form.addWidget(QLabel("基地等级"), 0, 4); form.addWidget(self.world_guild_level, 0, 5)
        stage_guild = QPushButton("暂存公会修改"); stage_guild.clicked.connect(self.stage_world_guild); form.addWidget(stage_guild, 0, 6)
        self.world_base_combo = QComboBox(); self.world_base_combo.currentIndexChanged.connect(self._show_world_base_editor); form.addWidget(QLabel("基地"), 1, 0); form.addWidget(self.world_base_combo, 1, 1)
        self.world_base_name = QLineEdit(); form.addWidget(QLabel("名称"), 1, 2); form.addWidget(self.world_base_name, 1, 3)
        self.world_base_x = QLineEdit(); self.world_base_y = QLineEdit(); self.world_base_z = QLineEdit()
        coordinates = QHBoxLayout(); coordinates.addWidget(QLabel("X")); coordinates.addWidget(self.world_base_x); coordinates.addWidget(QLabel("Y")); coordinates.addWidget(self.world_base_y); coordinates.addWidget(QLabel("Z")); coordinates.addWidget(self.world_base_z); form.addLayout(coordinates, 1, 4, 1, 2)
        stage_base = QPushButton("暂存基地修改"); stage_base.clicked.connect(self.stage_world_base); form.addWidget(stage_base, 1, 6)
        actions = QHBoxLayout(); self.world_edit_status = QLabel("尚未同步结构化存档"); actions.addWidget(self.world_edit_status, 1); preview = QPushButton("预览世界修改"); preview.clicked.connect(self.preview_world_changes); actions.addWidget(preview); revert = QPushButton("撤销世界草稿"); revert.clicked.connect(self.revert_world_changes); actions.addWidget(revert); save = QPushButton("保存世界修改"); save.clicked.connect(self.apply_world_changes); save.setStyleSheet("color:#b42318;"); actions.addWidget(save); form.addLayout(actions, 2, 0, 1, 7)
        l.addWidget(editors); return w

    def _mods_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        environment = QGroupBox("服务端模组环境"); el = QGridLayout(environment)
        self.mod_environment_status = QLabel("尚未检测"); self.mod_environment_status.setWordWrap(True); el.addWidget(QLabel("环境"), 0, 0); el.addWidget(self.mod_environment_status, 0, 1)
        self.mod_paths_status = QLabel("Workshop / Mods / UE4SS / Paks 路径：-"); self.mod_paths_status.setWordWrap(True); el.addWidget(QLabel("路径"), 1, 0); el.addWidget(self.mod_paths_status, 1, 1)
        detect = QPushButton("检测环境"); detect.clicked.connect(self.detect_mod_environment); el.addWidget(detect, 0, 2)
        migrate = QPushButton("迁移到隔离 Wine 实例"); migrate.clicked.connect(self.start_wine_migration); el.addWidget(migrate, 1, 2)
        migrate_legacy = QPushButton("生成旧模组迁移包"); migrate_legacy.clicked.connect(self.generate_ue4ss_migration_package); el.addWidget(migrate_legacy, 2, 2)
        restore_native = QPushButton("恢复原生 Linux 服务"); restore_native.clicked.connect(self.restore_native_server); el.addWidget(restore_native, 3, 2)
        l.addWidget(environment)
        self.mod_views = QTabWidget(); l.addWidget(self.mod_views, 1)
        catalog = QWidget(); catalog_l = QVBoxLayout(catalog); catalog_tools = QHBoxLayout(); self.workshop_search = QLineEdit(); self.workshop_search.setPlaceholderText("搜索 Steam Workshop 模组"); self.workshop_search.returnPressed.connect(self.search_workshop_catalog); catalog_tools.addWidget(self.workshop_search, 1); self.workshop_sort = QComboBox(); self.workshop_sort.addItem("热门", "trend"); self.workshop_sort.addItem("最新更新", "mostrecent"); self.workshop_sort.addItem("订阅最多", "totaluniquesubscribers"); catalog_tools.addWidget(self.workshop_sort); search = QPushButton("搜索"); search.clicked.connect(self.search_workshop_catalog); catalog_tools.addWidget(search); refresh = QPushButton("刷新"); refresh.clicked.connect(lambda: self.load_workshop_catalog(force=True)); catalog_tools.addWidget(refresh); catalog_l.addLayout(catalog_tools)
        catalog_split = QSplitter(Qt.Horizontal); self.workshop_table = QTableWidget(0, 5); self.workshop_table.setHorizontalHeaderLabels(["模组", "作者", "Workshop ID", "安装状态", "服务器兼容"]); self.workshop_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.workshop_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.workshop_table.horizontalHeader().setStretchLastSection(True); self.workshop_table.currentCellChanged.connect(self._show_workshop_detail); catalog_split.addWidget(self.workshop_table)
        catalog_detail = QWidget(); cdl = QVBoxLayout(catalog_detail); self.workshop_preview = QLabel("选择模组查看详情"); self.workshop_preview.setAlignment(Qt.AlignCenter); self.workshop_preview.setMinimumHeight(140); cdl.addWidget(self.workshop_preview); self.workshop_detail = QPlainTextEdit(); self.workshop_detail.setReadOnly(True); cdl.addWidget(self.workshop_detail); self.install_catalog_button = QPushButton("安装到当前服务器"); self.install_catalog_button.clicked.connect(self.install_selected_workshop); cdl.addWidget(self.install_catalog_button); catalog_split.addWidget(catalog_detail); catalog_split.setSizes([760, 340]); catalog_l.addWidget(catalog_split, 1)
        paging = QHBoxLayout(); previous = QPushButton("上一页"); previous.clicked.connect(lambda: self.change_workshop_page(-1)); paging.addWidget(previous); self.workshop_page_label = QLabel("第 1 页"); paging.addWidget(self.workshop_page_label); next_page = QPushButton("下一页"); next_page.clicked.connect(lambda: self.change_workshop_page(1)); paging.addWidget(next_page); paging.addStretch(); self.workshop_cache_label = QLabel("尚未加载目录"); paging.addWidget(self.workshop_cache_label); catalog_l.addLayout(paging); self.mod_views.addTab(catalog, "创意工坊")
        installed = QWidget(); installed_l = QVBoxLayout(installed); actions = QGridLayout()
        self.mod_action_buttons = {}
        for index, (text, handler) in enumerate((("按 ID 安装", self.import_workshop_mod), ("导入本地 ZIP", self.import_zip_mod), ("导入 PAK", self.import_pak_mod), ("导入 URL/GitHub", self.import_url_mod), ("导入目录", self.import_directory_mod), ("启用", self.enable_selected_mod), ("禁用", self.disable_selected_mod), ("更新/修复", self.repair_selected_mod), ("移除", self.remove_selected_mod), ("回滚上次变更", self.rollback_last_mod_change), ("导出清单", self.export_mod_manifest))):
            button = QPushButton(text); button.clicked.connect(handler); self.mod_action_buttons[text] = button; actions.addWidget(button, index // 5, index % 5)
        actions.setColumnStretch(4, 1); installed_l.addLayout(actions)
        filter_row = QHBoxLayout(); filter_row.addWidget(QLabel("类型筛选")); self.mod_type_filter = QComboBox(); self.mod_type_filter.addItem("全部", "all"); self.mod_type_filter.addItem("官方 Mods", "official"); self.mod_type_filter.addItem("UE4SS", "ue4ss"); self.mod_type_filter.addItem("NativeMods", "native"); self.mod_type_filter.addItem("PAK", "pak"); self.mod_type_filter.addItem("未知/只读", "unknown"); self.mod_type_filter.currentIndexChanged.connect(self._render_mods); filter_row.addWidget(self.mod_type_filter); filter_row.addStretch(); installed_l.addLayout(filter_row)
        split = QSplitter(Qt.Horizontal)
        self.mods_table = QTableWidget(0, 10); self.mods_table.setHorizontalHeaderLabels(["状态", "名称", "PackageName", "版本", "类型", "运行时", "服务器兼容", "依赖", "冲突", "校验"]); self.mods_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.mods_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setStretchLastSection(True); self.mods_table.currentCellChanged.connect(self._show_mod_detail); split.addWidget(self.mods_table)
        detail = QWidget(); dl = QVBoxLayout(detail); self.mod_detail_title = QLabel("选择模组查看详情"); self.mod_detail_title.setStyleSheet("font-size:16px;font-weight:650;"); dl.addWidget(self.mod_detail_title); self.mod_detail_text = QPlainTextEdit(); self.mod_detail_text.setReadOnly(True); dl.addWidget(self.mod_detail_text); self.mod_config_preview = QPlainTextEdit(); self.mod_config_preview.setReadOnly(True); self.mod_config_preview.setPlaceholderText("UE4SS 模组启用状态预览"); self.mod_config_preview.setMaximumHeight(120); dl.addWidget(self.mod_config_preview); split.addWidget(detail); split.setSizes([760, 360]); installed_l.addWidget(split, 1); self.mod_views.addTab(installed, "已安装")
        warning = QLabel("模组统一部署到 UE4SS/Mods 或 UE4SS/NativeMods，不修改玩家客户端，也不写入 PalModSettings.ini。原生 Linux 仅可浏览和下载；Windows 与验证通过的 Linux Wine 才能部署。")
        warning.setWordWrap(True); l.addWidget(warning); return w

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

    def _save_tools_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        source_group = QGroupBox("存档来源"); source_layout = QVBoxLayout(source_group)
        source_row = QHBoxLayout(); self.save_tool_source = QLineEdit(); self.save_tool_source.setPlaceholderText("选择 Steam、Xbox/Game Pass、专用服务器世界、存档文件或转换包"); source_row.addWidget(self.save_tool_source, 1)
        for text, handler in (("扫描本地存档", self.refresh_save_tool_sources), ("选择文件", self.choose_save_tool_file), ("选择文件夹", self.choose_save_tool_directory)):
            button = QPushButton(text); button.clicked.connect(handler); source_row.addWidget(button)
        source_layout.addLayout(source_row)
        self.save_tool_sources = QTableWidget(0, 6); self.save_tool_sources.setHorizontalHeaderLabels(["来源", "世界 ID", "格式", "玩家文件", "文件数", "修改时间"]); self.save_tool_sources.setSelectionBehavior(QAbstractItemView.SelectRows); self.save_tool_sources.setSelectionMode(QAbstractItemView.SingleSelection); self.save_tool_sources.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch); self.save_tool_sources.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch); self.save_tool_sources.cellClicked.connect(self._select_save_tool_source); source_layout.addWidget(self.save_tool_sources)
        l.addWidget(source_group)

        migration = QGroupBox("转换与迁移"); ml = QGridLayout(migration)
        actions = (
            ("联机世界转专用服务器", self.start_selected_coop_migration, "迁移 1 至 4 名玩家，并在部署后完成临时角色身份映射。"),
            ("继续玩家身份迁移", self.finish_coop_migration, "重新扫描服务器临时角色并继续未完成的映射。"),
            ("Game Pass 转 Steam", self.convert_gamepass_world, "只读提取 WGS 容器，生成标准 Steam 世界目录；不会修改 Xbox 云存档。"),
            ("Host Swap / 专服转联机", self.rebind_save_identity, "选择原角色与已登录生成的占位角色，离线生成重绑定后的新世界。"),
            ("直接导入当前服务器", self.import_selected_save_source, "将所选世界导入当前本地或远程服务器。"),
            ("导出转换包", self.export_save_conversion_package, "生成带 manifest 和逐文件 SHA-256 的 .pwc-conversion 包。"),
            ("校验转换包", self.verify_save_conversion_package, "验证转换包结构、清单和所有文件摘要。"),
        )
        for index, (text, handler, tip) in enumerate(actions):
            column = index % 2; row = index // 2
            button = QPushButton(text); button.setToolTip(tip); button.clicked.connect(handler); button.setMinimumHeight(34); ml.addWidget(button, row, column)
        ml.setColumnStretch(0, 1); ml.setColumnStretch(1, 1)
        l.addWidget(migration)

        conversion = QGroupBox("格式与标识"); cl = QHBoxLayout(conversion)
        for text, handler, tip in (
            ("SAV 转 JSON", self.convert_sav_to_json, "导出完整结构化 JSON，不修改原 SAV。"),
            ("JSON 转 SAV", self.convert_json_to_sav, "候选文件通过二次转换验证后再原子写入。"),
            ("SteamID 转 UID", self.convert_steam_id, "支持 SteamID64、steam_ 前缀和个人资料链接。"),
            ("恢复地图迷雾", self.restore_save_map, "备份并清除 LocalData.sav 的迷雾与隐藏地点标志，保留地图标记。"),
            ("扩容 Palbox", self.expand_save_palbox, "按稳定玩家身份扩展 Palbox 槽位，生成经过二次解析的新世界目录。"),
            ("地图与存档诊断", self.open_save_diagnostics, "解析玩家、公会、基地和帕鲁关系并显示基地坐标。"),
        ):
            button = QPushButton(text); button.setToolTip(tip); button.clicked.connect(handler); cl.addWidget(button)
        cl.addStretch(); l.addWidget(conversion)

        management = QGroupBox("存档管理"); gl = QHBoxLayout(management)
        for text, page_name in (("玩家、帕鲁与背包", "players_page"), ("公会与基地", "guilds_page"), ("备份与恢复", "backups_page")):
            button = QPushButton(text); button.clicked.connect(lambda _checked=False, name=page_name: self._navigate_to_page(name)); gl.addWidget(button)
        gl.addStretch(); l.addWidget(management)

        status = QGroupBox("任务与能力状态"); sl = QVBoxLayout(status); header = QHBoxLayout(); self.save_tool_stage = QLabel("等待选择存档"); self.save_tool_percent = QLabel("0%"); header.addWidget(self.save_tool_stage); header.addStretch(); header.addWidget(self.save_tool_percent); sl.addLayout(header)
        self.save_tool_progress = QProgressBar(); self.save_tool_progress.setRange(0, 100); self.save_tool_progress.setValue(0); self.save_tool_progress.setTextVisible(False); sl.addWidget(self.save_tool_progress)
        ready, detail = PlmCodecPlugin(self.storage.root).probe(); self.save_tool_engine_status = QLabel(("存档 helper 可用：" if ready else "存档 helper 只读降级：") + detail); self.save_tool_engine_status.setWordWrap(True); self.save_tool_engine_status.setStyleSheet("color:#087f5b;" if ready else "color:#8a4b08;"); sl.addWidget(self.save_tool_engine_status)
        self.save_tool_result = QPlainTextEdit(); self.save_tool_result.setReadOnly(True); self.save_tool_result.setMaximumHeight(120); self.save_tool_result.setPlaceholderText("转换、迁移和校验结果将在这里显示"); sl.addWidget(self.save_tool_result); l.addWidget(status)
        return w

    def _backup_tab(self):
        w = QWidget(); l = QVBoxLayout(w); row = QHBoxLayout()
        self.backup_action_buttons = []
        create = QPushButton("创建备份"); create_menu = QMenu(create)
        create_menu.addAction("世界导出包", lambda: self.create_backup_package("world"))
        create_menu.addAction("完整灾备包（配置脱敏）", lambda: self.create_backup_package("disaster"))
        create.setMenu(create_menu); row.addWidget(create); self.backup_action_buttons.append(create)
        import_button = QPushButton("导入"); import_menu = QMenu(import_button)
        import_menu.addAction("导入文件", self.import_backup_file); import_menu.addAction("导入 Saved/SaveGames 目录", self.import_backup_directory)
        import_button.setMenu(import_menu); row.addWidget(import_button); self.backup_action_buttons.append(import_button)
        for text, handler in (("导出", self.export_selected_backup), ("校验报告", self.export_selected_backup_report), ("恢复", self.restore), ("校验", self.verify_selected_backup), ("添加备注", self.note_selected_backup), ("保护/解锁", self.toggle_selected_backup_protection), ("删除", self.delete_selected_backup), ("刷新", self.refresh_backup_list)):
            b = QPushButton(text); b.clicked.connect(handler); row.addWidget(b); self.backup_action_buttons.append(b)
        row.addStretch(); l.addLayout(row)
        self.backup_summary = QLabel("默认创建仅含 SaveGames 的世界导出包；完整灾备包会包含脱敏配置。计划备份默认关闭，启用后每天 04:00，保留最近 14 份。")
        self.backup_summary.setWordWrap(True); l.addWidget(self.backup_summary)
        task_group = QGroupBox("备份与恢复进度"); task_layout = QVBoxLayout(task_group)
        task_header = QHBoxLayout(); self.backup_task_stage = QLabel("暂无任务"); self.backup_task_percent = QLabel("0%")
        task_header.addWidget(self.backup_task_stage); task_header.addStretch(); task_header.addWidget(self.backup_task_percent)
        self.backup_task_progress = QProgressBar(); self.backup_task_progress.setRange(0, 100); self.backup_task_progress.setValue(0); self.backup_task_progress.setTextVisible(False)
        self.backup_task_message_label = QLabel("等待创建备份、恢复存档或迁移玩家"); self.backup_task_message_label.setWordWrap(True)
        self.backup_task_elapsed = QLabel("未运行"); self.backup_task_elapsed.setStyleSheet("color:#66727d;")
        task_layout.addLayout(task_header); task_layout.addWidget(self.backup_task_progress); task_layout.addWidget(self.backup_task_message_label); task_layout.addWidget(self.backup_task_elapsed)
        l.addWidget(task_group)
        split = QSplitter(Qt.Vertical)
        self.backup_table = QTableWidget(0, 10); self.backup_table.setHorizontalHeaderLabels(["状态", "类型", "来源实例", "世界 ID", "游戏版本", "组件", "大小", "创建时间", "校验", "备注"])
        self.backup_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.backup_table.setSelectionMode(QAbstractItemView.SingleSelection); self.backup_table.setSortingEnabled(True)
        self.backup_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch); self.backup_table.horizontalHeader().setSectionResizeMode(9, QHeaderView.Stretch)
        self.backup_table.itemSelectionChanged.connect(self.show_backup_details); split.addWidget(self.backup_table)
        self.backup_details = QPlainTextEdit(); self.backup_details.setReadOnly(True); self.backup_details.setPlaceholderText("选择备份查看文件、校验和可恢复组件"); split.addWidget(self.backup_details); split.setSizes([420, 220]); l.addWidget(split); return w

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
        update_group = QGroupBox("软件更新"); update_layout = QHBoxLayout(update_group)
        self.update_status = QLabel("尚未检查更新"); self.update_status.setWordWrap(True); update_layout.addWidget(self.update_status, 1)
        self.update_button = QPushButton("检查更新"); self.update_button.clicked.connect(lambda: self.check_for_updates(False)); update_layout.addWidget(self.update_button)
        l.addWidget(update_group)
        info = QGroupBox("运行环境"); form = QFormLayout(info); form.addRow("Python", QLabel(platform.python_version()));
        try:
            from PySide6 import __version__ as qt_version
        except ImportError:
            qt_version = "未知"
        form.addRow("PySide6", QLabel(qt_version)); form.addRow("数据目录", QLabel(str(self.storage.root))); form.addRow("备份目录", QLabel(str(self.storage.root / "backups"))); l.addWidget(info)
        privacy = QLabel("隐私与安全：SSH 密码、私钥口令和管理密码保存在 Windows Credential Manager；实例 JSON、日志和计划任务不保存凭据明文。远程 REST 与 RCON 默认仅通过 SSH 隧道访问。")
        privacy.setWordWrap(True); l.addWidget(privacy)
        credits = QGroupBox("开源致谢与许可证"); cl = QVBoxLayout(credits); credit_text = QLabel("PySide6 · Paramiko · keyring · requests\nPlM 插件按需从固定提交构建，与主程序隔离。上游组件包含 Apache-2.0、GPL-3.0-or-later 及 Oodle 压缩源码授权警告，程序不随安装包再分发相关源码或二进制。\n功能流程参考 palworld-server-tool；本程序不包含地图功能，也不复制其界面或素材。\n模组系统遵循官方 Windows Dedicated Server 安装规则；原生 Linux 不标记为支持，Linux Wine 功能属于实验模式，变更前必须备份并通过健康检查。"); credit_text.setWordWrap(True); cl.addWidget(credit_text); l.addWidget(credits); l.addStretch(); return w

    def check_for_updates(self, automatic: bool = False):
        if self.update_check_active:
            return
        self.update_check_active = True
        self.update_button.setEnabled(False)
        self.update_status.setText("正在检查最新稳定版本…")
        from . import __version__
        worker = Worker(lambda: self.update_service.check_latest(__version__))
        worker.signals.finished.connect(lambda info: self._update_check_done(info, automatic))
        worker.signals.error.connect(lambda error: self._update_check_failed(error, automatic))
        self.pool.start(worker)

    def _update_check_done(self, info: ReleaseInfo | None, automatic: bool):
        self.update_check_active = False; self.update_button.setEnabled(True)
        if info is None:
            self.update_status.setText("当前已是最新稳定版本")
            if not automatic:
                QMessageBox.information(self, "检查更新", "当前已是最新稳定版本。")
            return
        self.update_status.setText(f"发现新版本 v{info.version_text}")
        body = info.body[:1200] if info.body else "该版本没有发布说明。"
        answer = QMessageBox.question(self, "发现新版本", f"发现 v{info.version_text}，是否下载更新？\n\n{body}")
        if answer == QMessageBox.Yes:
            self._download_update(info)

    def _update_check_failed(self, error: str, automatic: bool):
        self.update_check_active = False; self.update_button.setEnabled(True)
        self.update_status.setText("更新检查失败")
        if automatic:
            self.append_log(f"自动更新检查失败：{error}")
        else:
            QMessageBox.warning(self, "检查更新失败", error)

    def _download_update(self, info: ReleaseInfo):
        self.update_cancel = threading.Event()
        dialog = QProgressDialog("正在下载更新…", "取消", 0, 100, self)
        dialog.setWindowTitle(f"下载 v{info.version_text}"); dialog.setAutoClose(False); dialog.setAutoReset(False); dialog.setMinimumDuration(0)
        dialog.canceled.connect(self.update_cancel.set); dialog.show(); self.update_progress_dialog = dialog
        worker = Worker(lambda signals: self.update_service.download_installer(info, lambda received, total: signals.progress.emit((received, total)), self.update_cancel), with_signals=True)
        worker.signals.progress.connect(self._update_download_progress)
        worker.signals.finished.connect(self._update_download_done)
        worker.signals.error.connect(self._update_download_failed)
        self.pool.start(worker)

    def _update_download_progress(self, payload):
        if not self.update_progress_dialog:
            return
        received, total = payload
        if total:
            self.update_progress_dialog.setRange(0, 100); self.update_progress_dialog.setValue(min(100, round(received * 100 / total)))
            self.update_progress_dialog.setLabelText(f"正在下载更新… {received / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB")
        else:
            self.update_progress_dialog.setRange(0, 0); self.update_progress_dialog.setLabelText(f"正在下载更新… {received / 1024 / 1024:.1f} MB")

    def _update_download_done(self, installer: Path):
        if self.update_progress_dialog:
            self.update_progress_dialog.close(); self.update_progress_dialog = None
        self.update_cancel = None
        answer = QMessageBox.question(self, "下载完成", f"安装包已校验完成：\n{installer}\n\n退出程序并启动安装器吗？")
        if answer != QMessageBox.Yes:
            self.update_status.setText(f"更新已下载：{installer}")
            return
        started = QProcess.startDetached(str(installer), [])
        started_ok = started[0] if isinstance(started, tuple) else started
        if not started_ok:
            QMessageBox.warning(self, "启动安装器失败", "安装器无法启动，请在更新目录中手动运行。")
            return
        QApplication.quit()

    def _update_download_failed(self, error: str):
        if self.update_progress_dialog:
            self.update_progress_dialog.close(); self.update_progress_dialog = None
        self.update_cancel = None
        if "用户取消" in error:
            self.update_status.setText("已取消更新下载")
        else:
            self.update_status.setText("更新下载失败")
            QMessageBox.warning(self, "更新下载失败", error)

    def _refresh_instances(self):
        self.instance_list.clear(); self.instance_list.addItems([f"{i.name}  ({'本机' if i.kind == 'local' else '远程'})" for i in self.instances])
        if self.instances: self.instance_list.setCurrentRow(0)

    def select_instance(self, row: int):
        if row < 0 or row >= len(self.instances): return
        if getattr(self, "player_save_busy", False): return QMessageBox.information(self, "任务进行中", "玩家存档正在保存，请等待任务完成后再切换实例。")
        self._close_rest_tunnel()
        self.active_player_uid = ""
        self.selected_pal_edit = {}; self.selected_inventory_edit = {}
        self.player_center.begin_sync(self.instances[row].id)
        if hasattr(self, "player_detail_tabs"): self.player_detail_tabs.setEnabled(False)
        if hasattr(self, "player_view_stack"): self.player_view_stack.setCurrentWidget(self.player_list_page)
        if hasattr(self, "player_sync_label"):
            self.player_sync_label.setText("尚未同步存档")
        if hasattr(self, "player_detail_sync_label"): self.player_detail_sync_label.setText("尚未同步")
        self.current_players = self.player_repository.list_players(self.instances[row].id); self.current_player_groups = self.player_repository.list_identity_groups(self.instances[row].id); self.current_guilds = []
        self.selected = self.instances[row]; self.title.setText(self.selected.name); self.name_edit.setText(self.selected.name); self.kind_combo.setCurrentIndex(0 if self.selected.kind == "local" else 1); self.path_edit.setText(self.selected.install_dir); self.host_edit.setText(self.selected.host); self.user_edit.setText(self.selected.remote_username); self.ssh_port_spin.setValue(self.selected.ssh_port); self.auth_combo.setCurrentIndex(0 if self.selected.ssh_auth_type == "password" else 1); self.key_path_edit.setText(self.selected.ssh_key_path); self.port_spin.setValue(self.selected.game_port); self.rest_edit.setText(self.selected.rest_url); self.rest_password_edit.setText(self.storage.get_secret(self.selected.admin_secret_ref)); self.public_edit.setText(self.selected.public_address); self.config_source_label.setText(f"配置状态：{self.selected.config_source or '尚未同步'}" + ("，需要重启" if self.selected.config_restart_required else "")); self.lifecycle = LocalServerLifecycle(self.selected, self.ui_signals.log.emit) if self.selected.kind == "local" else (self._remote_lifecycle() if self.selected.discovery_status == "ready" else None); self._toggle_remote_fields(); self._show_discovery(); self.refresh_status()
        self.world_edit_session = PlayerEditSession(self.selected.id, "__world__")
        self._load_cached_config()
        self._render_players(); self._render_guilds()
        self._render_schedules(); self._render_whitelist(); self._render_audit(); self.refresh_backup_list(); self._render_mods(); self._show_mod_environment()
        self.header_address.setText(f"游戏地址：{self.selected.public_address or self.selected.host}:{self.selected.game_port}")

    def add_instance(self):
        self.instances.append(ServerInstance(name=f"服务器 {len(self.instances)+1}")); self.storage.save_instances(self.instances); self._refresh_instances()

    def delete_instance(self):
        if not self.selected: return
        if self.selected.kind == "local" and self.lifecycle and self.lifecycle.status() == "running": return QMessageBox.warning(self, "无法删除", "请先停止正在运行的本机服务器。")
        if QMessageBox.question(self, "确认删除", f"删除“{self.selected.name}”的控制台记录和保存的凭据？\n不会删除服务器文件。") != QMessageBox.Yes: return
        for ref in (self.selected.ssh_secret_ref, self.selected.ssh_key_passphrase_ref, self.selected.admin_secret_ref, self.selected.server_password_secret_ref): self.storage.delete_secret(ref)
        self.config_cache.remove_instance(self.selected.id)
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
        _remote_lifecycle_for(selected, client, signals.log.emit).repair_runtime()
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
            remote_lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit); remote_lifecycle.start(); remote_lifecycle.wait_for_game_listener()
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
            if self.selected.remote_profile.get("platform") == "unknown": return QMessageBox.warning(self, "未知系统", "无法识别远程操作系统，已禁止部署。请重新检测或导出诊断。")
            prerequisites = self.selected.remote_profile.get("prerequisites") or {}
            missing = list(prerequisites.get("missing") or [])
            if missing:
                actions = list(prerequisites.get("repair_actions") or [])
                volume_text = "\n".join(f"{item.get('root')}: {int(item.get('free_bytes') or 0) // 1024 // 1024} MB 可用" for item in self.selected.remote_profile.get("volumes") or [])
                detail = f"检测到缺少：{', '.join(missing)}\n\n准备执行：\n" + "\n".join(f"- {item}" for item in actions)
                if volume_text: detail += f"\n\n可用磁盘：\n{volume_text}"
                if "管理员权限" in missing:
                    return QMessageBox.warning(self, "需要管理员 SSH 会话", detail + "\n\nWinSW 服务和防火墙配置需要管理员权限。请使用管理员账户重新连接，程序不会尝试绕过权限。")
                if "磁盘空间" in missing:
                    return QMessageBox.warning(self, "磁盘空间不足", detail + "\n\n请选择有足够空间的固定磁盘，或先清理本应用创建的缓存、失败事务和过期已验证备份。")
                if QMessageBox.question(self, "确认自动准备依赖", detail + "\n\n继续后将按上述清单自动准备，且不会重启操作系统。", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
            try: admin_password = self._ensure_admin_password()
            except Exception as exc: return QMessageBox.critical(self, "凭据错误", str(exc))
            self._begin_install_task("正在安装…" if not self.selected.remote_profile.get("installed") else "正在更新…")
            selected = self.selected
            client = self._remote_client()
            backup_root = self._backup_destination(selected)
            worker = Worker(lambda signals: self._run_remote_install(signals, selected, client, admin_password, backup_root), with_signals=True)
            self._connect_install_worker(worker, self._remote_install_done)
            self.pool.start(worker)
            return
        if not self.selected: return
        if not self.selected.install_dir: return QMessageBox.warning(self, "提示", "请先保存本地安装目录")
        try: admin_password = self._ensure_admin_password()
        except Exception as exc: return QMessageBox.critical(self, "凭据错误", str(exc))
        self._begin_install_task("正在安装…")
        install_dir = Path(self.selected.install_dir)
        selected = self.selected; existing = (install_dir / "PalServer.exe").is_file(); lifecycle = self.lifecycle; was_running = bool(lifecycle and lifecycle.status() == "running"); backup_root = self._backup_destination(selected)
        worker = Worker(lambda signals: self._run_local_install(signals, selected, install_dir, admin_password, existing, lifecycle, was_running, backup_root), with_signals=True)
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
            "实例记录和 SSH 凭据会保留；本机实例目录内的 _tools/steamcmd 会随服务端一并删除。"
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
        lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit, signals.progress.emit)
        result = lifecycle.uninstall(backup_dir)
        signals.progress.emit(TaskProgress(97, "重新检测", "正在确认远程服务端已移除", True))
        profile = RemoteServerInspector(client, signals.log.emit, result.install_dir, selected.id).discover()
        if profile.get("installed"):
            raise RuntimeError("卸载命令已执行，但重新检测仍发现 Palworld 服务端")
        return result, profile

    def _backup_destination(self, instance: ServerInstance) -> Path:
        root = Path(getattr(self.storage, "root", Path.home() / ".palworld-console"))
        return root / "backups" / instance.id

    @staticmethod
    def _run_remote_install(signals, selected, client, admin_password, backup_root=None):
        lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit, signals.progress.emit)
        if selected.remote_profile.get("installed"):
            was_running = lifecycle.status() in {"active", "running"}
            if was_running: lifecycle.stop()
            try:
                signals.progress.emit(TaskProgress(12, "更新前备份", "正在下载并校验配置与存档备份", True))
                if backup_root is not None: BackupService().create_remote(client, selected, Path(backup_root), selected.install_dir)
            except Exception:
                if was_running: lifecycle.start()
                raise
            lifecycle.update(restart=False)
        else:
            lifecycle.install()
        signals.progress.emit(TaskProgress(87, "生成服务器配置", "正在创建或读取 PalWorldSettings.ini", True))
        config = ServerConfigBootstrap.ensure_remote(client, selected, admin_password)
        lifecycle.configure_service()
        if hasattr(lifecycle, "allow_game_firewall"): lifecycle.allow_game_firewall()
        signals.progress.emit(TaskProgress(95, "启动服务器", "正在启动 Palworld 服务", True))
        lifecycle.start()
        lifecycle.wait_for_game_listener()
        signals.progress.emit(TaskProgress(97, "重新检测", "正在确认服务端安装状态", True))
        try:
            profile = RemoteServerInspector(client, signals.log.emit, selected.install_dir, selected.id).discover()
            return profile, config
        except Exception as exc:
            raise RuntimeError(f"服务端安装/更新已执行，但状态复检失败：{exc}") from exc

    @staticmethod
    def _run_local_install(signals, selected, install_dir, admin_password, existing=False, current_lifecycle=None, was_running=False, backup_root=None):
        if existing:
            signals.progress.emit(TaskProgress(1, "准备更新", "正在停止服务器并备份配置与存档", True))
            if current_lifecycle: current_lifecycle.stop()
            try: BackupService().create_local(selected, backup_root)
            except Exception:
                if was_running and current_lifecycle: current_lifecycle.start()
                raise
        try:
            state = LocalSteamCmdManager().prepare(install_dir, signals.log.emit, signals.progress.emit); selected.local_steamcmd_state = asdict(state)
            SteamCmdInstaller().install_or_update(Path(state.executable), install_dir, signals.log.emit, signals.progress.emit)
            if not (install_dir / "PalServer.exe").is_file(): raise RuntimeError("SteamCMD 已结束，但未找到 PalServer.exe")
            signals.progress.emit(TaskProgress(87, "生成服务器配置", "正在创建或读取 PalWorldSettings.ini", True)); config = ServerConfigBootstrap.ensure_local(selected, admin_password)
            lifecycle = LocalServerLifecycle(selected, signals.log.emit); should_start = not existing or was_running
            if should_start:
                signals.progress.emit(TaskProgress(95, "启动服务器", "正在启动本机 Palworld 服务", True)); lifecycle.start()
            return config, lifecycle, should_start
        except Exception:
            if was_running and current_lifecycle:
                try: current_lifecycle.start()
                except Exception as exc: signals.log.emit(f"更新失败后恢复服务器启动也失败：{exc}")
            raise

    def choose_local_install_dir(self):
        current = self.path_edit.text().strip() or str(Path.home() / "PalworldServer")
        selected = QFileDialog.getExistingDirectory(self, "选择 Palworld 服务端安装目录", current)
        if selected: self.path_edit.setText(selected)

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
        if hasattr(self, "backup_task_progress") and self.active_task_kind == "backup":
            self.backup_task_last_progress_at = time.monotonic()
            self.backup_task_message = progress.message or progress.stage
            self.backup_task_stage.setText(progress.stage)
            self.backup_task_message_label.setText(self.backup_task_message)
            if progress.indeterminate:
                self.backup_task_progress.setRange(0, 0); self.backup_task_percent.setText("处理中")
            else:
                self.backup_task_progress.setRange(0, 100); self.backup_task_progress.setValue(progress.percent); self.backup_task_percent.setText(f"{progress.percent}%")
            if getattr(self, "save_tool_task_active", False):
                self._set_save_tool_progress(progress)

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
        config, lifecycle, started = payload
        self.lifecycle = lifecycle
        self._apply_config_result(config)
        self._install_succeeded("本机安装/更新完成，服务器已启动" if started else "本机更新完成，服务器保持停止状态")
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
    def _backup_repository(self, instance=None):
        selected = instance or self.selected
        if not selected: raise RuntimeError("未选择服务器实例")
        return BackupRepository(Path(self.storage.root) / "backups", selected.id)

    def backup(self): self.create_backup_package("world")

    def create_backup_package(self, backup_type="world"):
        if not self.selected: return
        if self.install_task_active: return QMessageBox.information(self, "任务进行中", "当前服务器任务尚未结束。")
        selected = self.selected; repository = self._backup_repository(selected)
        self._begin_backup_task("创建世界导出包" if backup_type == "world" else "创建完整灾备包")
        if selected.kind == "remote":
            client = self._remote_client(); admin = self.storage.get_secret(selected.admin_secret_ref); rest_user = self.rest_user_edit.text().strip() or "admin"
            worker = Worker(lambda signals: self._run_remote_backup(signals, selected, client, repository, admin, rest_user, backup_type), with_signals=True)
        else:
            current_lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, self.ui_signals.log.emit)
            worker = Worker(lambda signals: self._run_local_package_backup(signals, selected, repository, backup_type, current_lifecycle, lambda: self._rest_client().save()), with_signals=True)
        worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._backup_done); worker.signals.error.connect(self._backup_task_failed); self.pool.start(worker)

    @staticmethod
    def _run_local_package_backup(signals, selected, repository, backup_type, lifecycle, save_world=None):
        was_running = lifecycle.status() == "running"
        signals.progress.emit(TaskProgress(10, "保存并停止", "正在创建一致性存档快照", True))
        if was_running and save_world:
            try: save_world(); signals.log.emit("备份前已请求服务器保存世界")
            except Exception as exc: signals.log.emit(f"保存世界请求失败，将通过停服保证备份一致性：{exc}")
        if was_running: lifecycle.stop()
        try:
            saved = Path(selected.install_dir) / "Pal" / "Saved"
            signals.progress.emit(TaskProgress(45, "创建备份包", "正在计算文件 SHA-256 并脱敏配置", True))
            package = BackupPackageService().create(selected, saved, repository.root, backup_type)
            signals.progress.emit(TaskProgress(85, "校验备份", "正在重新读取 CRC、清单和 SHA-256", True))
            BackupPackageService().validate(package)
            return package
        finally:
            if was_running: lifecycle.start()

    @staticmethod
    def _run_remote_backup(signals, selected, client, repository, admin_password, rest_user, backup_type="world"):
        lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit)
        was_running = lifecycle.status() in {"active", "running"}
        tunnel = SSHTunnelManager(client)
        raw = None
        try:
            try:
                tunnel.start("127.0.0.1", int(selected.remote_profile.get("rest_port") or 8212)); PalworldRestClient(tunnel.base_url, admin_password, rest_user).save(); signals.log.emit("备份前已保存世界")
            except Exception as exc: signals.log.emit(f"保存世界请求失败，将通过停服保证备份一致性：{exc}")
            if was_running: lifecycle.stop()
            signals.progress.emit(TaskProgress(35, "下载快照", "正在远程打包并通过 SFTP 下载", True))
            raw = BackupService().create_remote(client, selected, repository.root, selected.install_dir)
            if raw is None: raise RuntimeError("远程服务器没有可备份的 Saved 数据")
            signals.progress.emit(TaskProgress(65, "转换备份包", "正在生成统一清单并脱敏配置", True))
            package = repository.import_source(raw, selected, backup_type)
            BackupPackageService().validate(package)
            return package
        finally:
            if raw: raw.unlink(missing_ok=True)
            tunnel.close()
            if was_running: lifecycle.start()

    def _backup_done(self, path):
        if self.selected and path:
            self._backup_repository().mark_latest(Path(path))
            self.selected.last_backup = str(path); self.storage.save_instances(self.instances)
            if hasattr(self, "health_labels"): self.health_labels["backup"].setText(str(path))
            AuditService.record(self.selected, "创建备份", str(path), detail="统一 .pwcbackup 已完成 CRC 与 SHA-256 校验")
            self.storage.save_instances(self.instances); self._render_audit()
        self.append_log(f"备份完成：{path}" if path else "未发现需要备份的存档")
        self._finish_backup_task(); self.refresh_backup_list()

    def _begin_backup_task(self, label):
        self.install_task_active = True; self.active_task_kind = "backup"; self._set_install_controls_enabled(False)
        self.save_tool_task_active = hasattr(self, "save_tools_page") and self.page_stack.currentWidget() is self.save_tools_page
        self.navigation.setEnabled(False)
        for button in getattr(self, "backup_action_buttons", []): button.setEnabled(False)
        self.backup_task_started_at = time.monotonic(); self.backup_task_last_progress_at = self.backup_task_started_at; self.backup_task_message = "正在准备备份任务"
        self.backup_task_stage.setStyleSheet(""); self.backup_task_message_label.setStyleSheet(""); self.backup_task_elapsed.setText("已运行 0 秒")
        self.backup_task_timer.start()
        self._set_install_progress(TaskProgress(0, label, "正在准备备份任务", True))

    def _finish_backup_task(self, success=True):
        self.backup_task_timer.stop()
        self.install_task_active = False; self.active_task_kind = ""; self._set_install_controls_enabled(True)
        self.navigation.setEnabled(True)
        for button in getattr(self, "backup_action_buttons", []): button.setEnabled(True)
        self.install_progress.setRange(0, 100)
        if success:
            self.install_progress.setValue(100); self.install_percent.setText("100%")
            self.backup_task_progress.setRange(0, 100); self.backup_task_progress.setValue(100); self.backup_task_percent.setText("100%")
            self.backup_task_stage.setText("任务完成"); self.backup_task_stage.setStyleSheet("color:#18794e;font-weight:600;")
            if getattr(self, "save_tool_task_active", False): self._set_save_tool_progress(TaskProgress(100, "任务完成", self.backup_task_message or "存档工具任务已完成"))
        else:
            self.backup_task_progress.setRange(0, 100); self.backup_task_progress.setValue(self.install_progress_value); self.backup_task_percent.setText(f"{self.install_progress_value}%")
            self.backup_task_stage.setStyleSheet("color:#b42318;font-weight:600;")
            if getattr(self, "save_tool_task_active", False): self.save_tool_stage.setText("任务失败"); self.save_tool_percent.setText("失败")
        if self.backup_task_started_at:
            self.backup_task_elapsed.setText(f"总耗时 {max(0, int(time.monotonic() - self.backup_task_started_at))} 秒")
        self.save_tool_task_active = False

    def _backup_task_heartbeat(self):
        if self.active_task_kind != "backup" or not self.backup_task_started_at: return
        now = time.monotonic(); elapsed = max(0, int(now - self.backup_task_started_at)); stale = max(0, int(now - self.backup_task_last_progress_at))
        self.backup_task_elapsed.setText(f"已运行 {elapsed} 秒 · 最近进度更新 {stale} 秒前")
        if stale >= 8:
            self.backup_task_message_label.setText(f"{self.backup_task_message} · 远程操作仍在执行，请勿关闭程序")

    def _backup_task_failed(self, error):
        message = self._friendly_backup_error(error)
        self.append_log(f"备份任务失败：{message}"); self.install_stage.setText("备份失败"); self.install_message.setText(message)
        self.backup_task_stage.setText("任务失败"); self.backup_task_message_label.setText(message); self.backup_task_message = message
        self._finish_backup_task(False); QMessageBox.critical(self, "备份失败", message)

    def _selected_backup_path(self):
        if not hasattr(self, "backup_table") or self.backup_table.currentRow() < 0: return None
        item = self.backup_table.item(self.backup_table.currentRow(), 0)
        return Path(str(item.data(Qt.UserRole))) if item and item.data(Qt.UserRole) else None

    def restore(self):
        if not self.selected: return
        package = self._selected_backup_path()
        if not package: return QMessageBox.information(self, "恢复", "请先在备份列表中选择一个备份。")
        package = Path(package).expanduser().resolve()
        if not package.exists():
            return QMessageBox.critical(self, "恢复前预检失败", f"备份文件已被移动或删除，请重新导入：\n{package}")
        if not package.is_file():
            return QMessageBox.critical(self, "恢复前预检失败", f"所选备份不是文件：\n{package}")
        repository = self._backup_repository()
        if package.suffix.lower() != ".pwcbackup":
            if QMessageBox.question(self, "转换旧备份", "所选文件是旧格式，必须先转换并校验为 .pwcbackup。继续吗？") != QMessageBox.Yes: return
            try: package = repository.import_source(package, self.selected)
            except Exception as exc: return QMessageBox.critical(self, "旧备份转换失败", str(exc))
            package = Path(package).resolve()
            self.refresh_backup_list()
        try:
            manifest = BackupPackageService().validate(package)
            if "world" not in manifest.components:
                return QMessageBox.critical(self, "恢复前预检失败", "备份中没有可恢复的 SaveGames 文件。")
        except Exception as exc: return QMessageBox.critical(self, "备份校验失败", str(exc))
        name, ok = QInputDialog.getText(self, "确认恢复", f"请输入目标实例名称“{self.selected.name}”：")
        if not ok or name != self.selected.name: return
        reason, ok = QInputDialog.getText(self, "恢复原因", "请输入恢复或迁服原因：")
        if not ok or not reason.strip(): return
        selected = self.selected
        if selected.kind == "local":
            savegames = Path(selected.install_dir).expanduser().resolve() / "Pal" / "Saved" / "SaveGames"
            if not savegames.is_dir():
                return QMessageBox.critical(self, "恢复前预检失败", f"目标服务器尚未创建 SaveGames 目录：\n{savegames}")
        self._begin_backup_task("恢复服务器存档")
        has_players = any("/players/" in entry.path.casefold() and entry.path.casefold().endswith(".sav") for entry in manifest.entries)
        if has_players:
            ready, detail = PlmCodecPlugin(self.storage.root).probe()
            if not ready:
                self._finish_backup_task(False)
                return QMessageBox.critical(self, "无法迁移备份玩家", f"备份包含玩家角色，必须先启用 PlM 插件。\n{detail}")
            worker = Worker(lambda signals: self._prepare_restore_migration_task(signals, selected, package), with_signals=True)
            worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress)
            worker.signals.finished.connect(lambda payload: self._restore_migration_prepared(payload, reason)); worker.signals.error.connect(self._restore_failed); self.pool.start(worker); return
        worker = Worker(lambda signals: self._run_restore_transaction(signals, selected, package, reason), with_signals=True)
        worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress)
        worker.signals.finished.connect(lambda result: self._restore_done(result, reason)); worker.signals.error.connect(self._restore_failed); self.pool.start(worker)

    def _prepare_restore_migration_task(self, signals, selected, package):
        service = BackupPackageService(); repository = self._backup_repository(); root = Path(self.storage.root) / "migrations" / selected.id / "restore"; snapshot_root = root / "snapshot"
        import shutil
        if snapshot_root.exists(): shutil.rmtree(snapshot_root)
        snapshot_root.mkdir(parents=True)
        signals.progress.emit(TaskProgress(5, "恢复预检", "正在保存目标服务器当前世界", False))
        if selected.kind == "local":
            lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, signals.log.emit)
            signals.progress.emit(TaskProgress(8, "停止服务器", "正在停止本机服务器以取得一致性快照", True))
            saved = Path(selected.install_dir).expanduser().resolve() / "Pal" / "Saved"; lifecycle.stop()
            try:
                signals.progress.emit(TaskProgress(12, "创建恢复点", "正在打包恢复前完整世界", True))
                restore_point = service.create(selected, saved, repository.root, "restore-point", "恢复玩家迁移前自动恢复点")
                repository.set_metadata(restore_point, protected=True, verified_at=datetime.now().isoformat(timespec="seconds"))
                signals.progress.emit(TaskProgress(18, "复制当前世界", "正在复制服务器当前存档用于身份解析", True))
                shutil.copytree(saved, snapshot_root / "Saved")
            finally: lifecycle.start()
            signals.progress.emit(TaskProgress(24, "解析玩家身份", "正在解析备份玩家与服务器已有玩家", True))
            target = service.detect_server_world(saved / "SaveGames")
            session = service.prepare_restore_migration(package, selected, snapshot_root / "Saved", target.world_path, self.storage.root, "local", "windows")
            return session, str(restore_point)
        client = self._remote_client(); lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit); install_dir = str(selected.remote_profile.get("install_dir") or selected.install_dir); platform_name = str(selected.remote_profile.get("platform") or "linux").lower()
        signals.progress.emit(TaskProgress(8, "停止远程服务器", "正在停止服务器以创建一致性快照", True))
        lifecycle.stop()
        try:
            signals.progress.emit(TaskProgress(12, "下载恢复前快照", "正在远程打包并通过 SFTP 下载当前世界", True))
            raw = BackupService().create_remote(client, selected, snapshot_root, install_dir)
        finally: lifecycle.start()
        if raw is None: raise RuntimeError("无法创建远程恢复前快照")
        signals.progress.emit(TaskProgress(20, "校验远程快照", "快照下载完成，正在安全解包并校验", True))
        saved_snapshot = service.extract_saved_snapshot(raw, snapshot_root / "extracted")
        restore_point = repository.import_source(raw, selected); repository.set_metadata(restore_point, protected=True, verified_at=datetime.now().isoformat(timespec="seconds"), note="恢复玩家迁移前自动恢复点")
        signals.progress.emit(TaskProgress(26, "解析玩家身份", "正在解析备份玩家与远程服务器已有玩家", True))
        local_target = service.detect_server_world(saved_snapshot / "SaveGames")
        relative = Path(local_target.world_path).relative_to(saved_snapshot / "SaveGames").as_posix()
        if platform_name == "windows":
            from .services import WindowsRemotePath
            savegames = WindowsRemotePath.normalize(install_dir).rstrip("\\/") + "\\Pal\\Saved\\SaveGames"; remote_world = ntpath.join(savegames, *relative.split("/"))
        else:
            savegames = install_dir.rstrip("/") + "/Pal/Saved/SaveGames"; remote_world = savegames + "/" + relative
        session = service.prepare_restore_migration(package, selected, saved_snapshot, remote_world, self.storage.root, "remote", platform_name)
        return session, str(restore_point)

    def _restore_migration_prepared(self, payload, reason):
        session, restore_point = payload; service = BackupPackageService(); targets = list(session.placeholder_players)
        self._set_install_progress(TaskProgress(30, "等待玩家映射确认", "请在弹出的映射窗口中逐项确认玩家身份", False))
        confirmations = {}; used = set()
        for player in session.source_players:
            old_guid = service._player_guid(player); old_name = str(player.get("nickname") or "未命名")
            available = list(service.available_identity_targets(old_guid, targets, used))
            options = ["稍后迁移（玩家进入恢复后的服务器创建临时角色）"] + [f"{service._player_guid(item)} · {item.get('nickname') or '未命名'}" for item in available]
            suggested = 0
            matches = [index for index, target in enumerate(available, 1) if str(target.get("nickname") or "").casefold() == old_name.casefold()]
            if len(matches) == 1: suggested = matches[0]
            choice, ok = QInputDialog.getItem(self, "恢复玩家身份映射", f"备份角色：{old_name} ({old_guid})\n请选择该玩家在目标服务器的登录身份：", options, suggested, False)
            if not ok: self._finish_backup_task(False); return
            if choice.startswith("稍后迁移"): continue
            new_guid = choice.split(" · ", 1)[0]
            if new_guid in used: self._finish_backup_task(False); return QMessageBox.critical(self, "映射冲突", "同一个目标服务器身份不能分配给多个备份角色。")
            used.add(new_guid); confirmations[old_guid] = new_guid
        try: session = service.confirm_restore_mappings(session, confirmations, self.storage.root)
        except Exception as exc: self._finish_backup_task(False); return QMessageBox.critical(self, "玩家映射失败", str(exc))
        summary = f"将恢复世界并立即迁移 {len(session.mappings)} 个玩家；另有 {len(session.pending_player_guids)} 个玩家需要恢复后创建临时角色。"
        if QMessageBox.question(self, "确认恢复和玩家迁移", summary) != QMessageBox.Yes: self._finish_backup_task(False); return
        selected = self.selected; worker = Worker(lambda signals: self._deploy_restore_migration_task(signals, selected, session, restore_point), with_signals=True)
        worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(lambda result: self._restore_done(result, reason)); worker.signals.error.connect(self._restore_failed); self.pool.start(worker)

    def _deploy_restore_migration_task(self, signals, selected, session, restore_point):
        import shutil
        service = BackupPackageService(); root = Path(self.storage.root) / "migrations" / selected.id / "restore" / "deployment-snapshot"
        if root.exists(): shutil.rmtree(root)
        root.mkdir(parents=True)
        if selected.kind == "local":
            lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, signals.log.emit)
            signals.progress.emit(TaskProgress(30, "停止服务器", "正在停止服务器并锁定最新世界", True)); lifecycle.stop()
            try:
                target_world = Path(session.target_world_path).expanduser().resolve(); savegames = root / "SaveGames"; savegames.mkdir()
                shutil.copytree(target_world, savegames / target_world.name)
                session = service.refresh_restore_target_snapshot(session, root, self.storage.root)
                signals.progress.emit(TaskProgress(32, "构建迁移候选", "正在基于最新服务器世界迁移玩家身份并二次校验", True))
                session, report = service.build_restore_candidate(session, self.storage.root); signals.log.emit(f"候选世界验证通过：迁移 {report.get('migrated', 0)} 个玩家")
            except Exception:
                lifecycle.start(); raise
            signals.progress.emit(TaskProgress(35, "候选验证通过", f"已完成 {report.get('migrated', 0)} 个玩家身份迁移，准备部署", False))
            try:
                return service.deploy_restore_candidate_local(session, lambda: None, lifecycle.start, lambda: lifecycle.status() == "running", self.storage.root, restore_point, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
            except Exception:
                lifecycle.start(); raise
        client = self._remote_client(); lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit); install_dir = str(selected.remote_profile.get("install_dir") or selected.install_dir)
        signals.progress.emit(TaskProgress(30, "停止远程服务器", "正在停止服务器并锁定最新世界", True)); lifecycle.stop()
        try:
            archive = BackupService().create_remote(client, selected, root, install_dir)
            if archive is None: raise RuntimeError("无法取得部署前远程服务器快照")
            saved = service.extract_saved_snapshot(archive, root / "extracted")
            session = service.refresh_restore_target_snapshot(session, saved, self.storage.root)
            signals.progress.emit(TaskProgress(32, "构建迁移候选", "正在基于最新服务器世界迁移玩家身份并二次校验", True))
            session, report = service.build_restore_candidate(session, self.storage.root); signals.log.emit(f"候选世界验证通过：迁移 {report.get('migrated', 0)} 个玩家")
        except Exception:
            lifecycle.start(); raise
        signals.progress.emit(TaskProgress(35, "候选验证通过", f"已完成 {report.get('migrated', 0)} 个玩家身份迁移，准备部署", False))
        try:
            return service.deploy_restore_candidate_remote(session, client, lambda: None, lifecycle.start, lambda: self._remote_health_ok(selected), self.storage.root, restore_point, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
        except Exception:
            lifecycle.start(); raise

    def _resume_restore_deployment_task(self, signals, selected, session):
        signals.progress.emit(TaskProgress(28, "恢复迁移部署", "正在重新锁定服务器最新世界并重建候选", True))
        return self._deploy_restore_migration_task(signals, selected, session, session.backup_path)

    def _run_restore_transaction(self, signals, selected, package, reason=""):
        try:
            try: self._rest_client().save()
            except Exception as exc: signals.log.emit(f"REST 保存世界失败，将通过停服保证一致性：{exc}")
            transaction = RestoreTransaction()
            if selected.kind == "remote":
                client = self._remote_client(); lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit)
                platform_name = str(selected.remote_profile.get("platform") or "linux").lower()
                install_dir = str(selected.remote_profile.get("install_dir") or selected.install_dir)
                if platform_name == "windows":
                    from .services import WindowsRemotePath
                    savegames = WindowsRemotePath.normalize(install_dir).rstrip("\\/") + "\\Pal\\Saved\\SaveGames"
                else:
                    savegames = install_dir.rstrip("/") + "/Pal/Saved/SaveGames"
                return transaction.restore_savegames_remote(package, savegames, client, platform_name, lifecycle.stop, lifecycle.start, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
            lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, signals.log.emit)
            savegames = Path(selected.install_dir).expanduser().resolve() / "Pal" / "Saved" / "SaveGames"
            return transaction.restore_savegames(package, savegames, lifecycle.stop, lifecycle.start, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
        finally:
            self._close_rest_tunnel()

    def _restore_done(self, result, reason):
        if self.selected:
            self.selected.last_backup = result.package_path
            AuditService.record(self.selected, "恢复存档", result.package_path, result="成功", detail=f"{result.detail}；原因={reason}")
            self.storage.save_instances(self.instances); self._render_audit()
        self.append_log(f"恢复完成：服务器存档文件已替换。{result.detail}"); self._finish_backup_task(); self.refresh_backup_list(); self.refresh_status()
        self.load_ini(); self.load_save_snapshot()

    def _restore_failed(self, error):
        if self.selected:
            AuditService.record(self.selected, "恢复存档", str(self._selected_backup_path() or ""), result="失败", detail=error); self.storage.save_instances(self.instances); self._render_audit()
        message = self._friendly_backup_error(error)
        self.append_log(f"恢复失败：{message}"); self.install_stage.setText("恢复失败"); self.install_message.setText(message)
        self.backup_task_stage.setText("恢复失败"); self.backup_task_message_label.setText(message); self.backup_task_message = message
        self._finish_backup_task(False); self.refresh_backup_list(); QMessageBox.critical(self, "恢复失败", message)

    def import_backup_file(self):
        if not self.selected: return
        file, _ = QFileDialog.getOpenFileName(self, "导入存档或备份", "", "备份与存档 (*.pwcbackup *.zip *.tar.gz *.tgz *.sav);;所有文件 (*)")
        if file: self._import_backup_source(Path(file))

    def import_backup_directory(self):
        if not self.selected: return
        directory = QFileDialog.getExistingDirectory(self, "选择 Saved 或 SaveGames 目录")
        if directory: self._import_backup_source(Path(directory))

    def _import_backup_source(self, source):
        if self.install_task_active: return QMessageBox.information(self, "任务进行中", "当前服务器任务尚未结束。")
        source = Path(source).expanduser().resolve()
        if not source.exists(): return QMessageBox.warning(self, "导入前检查失败", f"待导入文件或目录不存在：\n{source}")
        selected = self.selected; repository = self._backup_repository(); self._begin_backup_task("导入备份")
        def import_task(signals):
            return repository.import_source(source, selected, on_progress=lambda percent, message: signals.progress.emit(TaskProgress(percent, "导入备份", message)))
        worker = Worker(import_task, with_signals=True); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._import_backup_done); worker.signals.error.connect(self._backup_task_failed); self.pool.start(worker)

    def _import_backup_done(self, path):
        self.append_log(f"备份已导入并校验：{path}"); self._finish_backup_task(); self.refresh_backup_list(Path(path))

    def _navigate_to_page(self, attribute: str):
        page = getattr(self, attribute, None)
        if page is None: return
        index = self.page_stack.indexOf(page)
        if index >= 0: self.navigation.setCurrentRow(index)

    def _current_save_tool_source(self) -> Path | None:
        value = self.save_tool_source.text().strip() if hasattr(self, "save_tool_source") else ""
        if not value:
            QMessageBox.information(self, "选择存档", "请先扫描或选择一个存档来源。")
            return None
        source = Path(value).expanduser().resolve()
        if not source.exists():
            QMessageBox.warning(self, "存档不存在", f"所选存档来源不存在：\n{source}")
            return None
        return source

    def refresh_save_tool_sources(self):
        try:
            sources = SaveToolsService(self.storage.root).detect_sources()
        except Exception as exc:
            return QMessageBox.critical(self, "本地存档检测失败", str(exc))
        self.save_tool_source_records = list(sources)
        self.save_tool_sources.setRowCount(len(sources))
        for row, source in enumerate(sources):
            values = (source.source_path, source.world_id, source.save_format, "是" if source.has_players else "否", source.file_count, source.modified_at)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, {"path": source.source_path, "kind": source.source_kind, "world_id": source.world_id}); self.save_tool_sources.setItem(row, column, item)
        if sources:
            self.save_tool_sources.setCurrentCell(0, 0); self.save_tool_source.setText(sources[0].source_path); self.save_tool_selected_world_id = sources[0].world_id; self.save_tool_selected_kind = sources[0].source_kind
            self.save_tool_stage.setText(f"检测到 {len(sources)} 个本地存档来源")
        else:
            self.save_tool_stage.setText("未在常见路径检测到存档")

    def _select_save_tool_source(self, row: int, _column: int = 0):
        item = self.save_tool_sources.item(row, 0)
        if item:
            data = item.data(Qt.UserRole)
            if isinstance(data, dict):
                self.save_tool_source.setText(str(data.get("path") or item.text())); self.save_tool_selected_world_id = str(data.get("world_id") or ""); self.save_tool_selected_kind = str(data.get("kind") or "")
            else: self.save_tool_source.setText(str(data or item.text()))

    def choose_save_tool_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Palworld 存档或转换包", "", "存档来源 (*.sav *.zip *.tar.gz *.tgz *.pwcbackup *.pwc-conversion);;所有文件 (*)")
        if path: self.save_tool_source.setText(path); self.save_tool_selected_world_id = ""; self.save_tool_selected_kind = ""; self._inspect_save_tool_source(Path(path))

    def choose_save_tool_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Palworld 世界、Saved 或 SaveGames 文件夹")
        if path: self.save_tool_source.setText(path); self.save_tool_selected_world_id = ""; self.save_tool_selected_kind = ""; self._inspect_save_tool_source(Path(path))

    def _inspect_save_tool_source(self, source: Path):
        if source.suffix.lower() == ".pwc-conversion":
            try: report = SaveToolsService(self.storage.root).verify_conversion_package(source)
            except Exception as exc: return QMessageBox.critical(self, "转换包校验失败", str(exc))
            self.save_tool_result.setPlainText(f"转换包有效\n世界：{report['world_id']}\n文件：{report['entries']}\nSHA-256：{report['sha256']}")
            return
        try: inspection = SaveToolsService(self.storage.root).inspect(source)
        except Exception as exc: return QMessageBox.critical(self, "存档预检失败", str(exc))
        warnings = "；".join(inspection.warnings) or "无"
        self.save_tool_result.setPlainText(f"世界：{inspection.world_id}\n格式：{inspection.save_format}\n文件：{inspection.file_count}\n玩家文件：{'有' if inspection.has_players else '无'}\n警告：{warnings}")

    def _set_save_tool_progress(self, progress: TaskProgress):
        self.save_tool_stage.setText(progress.stage); self.save_tool_percent.setText("处理中" if progress.indeterminate else f"{progress.percent}%")
        if progress.indeterminate: self.save_tool_progress.setRange(0, 0)
        else: self.save_tool_progress.setRange(0, 100); self.save_tool_progress.setValue(progress.percent)
        if progress.message: self.save_tool_result.setPlainText(progress.message)

    def export_save_conversion_package(self):
        source = self._current_save_tool_source()
        if source is None: return
        default = f"{source.stem or 'palworld-world'}.pwc-conversion"
        output, _ = QFileDialog.getSaveFileName(self, "导出存档转换包", default, "Palworld 转换包 (*.pwc-conversion)")
        if not output: return
        output_path = Path(output if output.lower().endswith(".pwc-conversion") else output + ".pwc-conversion")
        self._set_save_tool_progress(TaskProgress(0, "创建转换包", "正在规范化存档", True))
        world_id = getattr(self, "save_tool_selected_world_id", "")
        worker = Worker(lambda signals: SaveToolsService(self.storage.root).create_conversion_package(source, "convert", output_path, lambda p, m: signals.progress.emit(TaskProgress(p, "创建转换包", m)), world_id), with_signals=True)
        worker.signals.progress.connect(self._set_save_tool_progress); worker.signals.finished.connect(self._save_conversion_done); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def _save_conversion_done(self, package):
        self._set_save_tool_progress(TaskProgress(100, "转换包已创建", package.path))
        self.save_tool_result.setPlainText(f"转换包：{package.path}\n世界：{package.world_id}\n文件：{package.file_count}\n大小：{package.total_bytes} 字节\nSHA-256：{package.sha256}")
        self.append_log(f"存档转换包已创建：{package.path}")

    def _save_tool_failed(self, error: str):
        self.save_tool_progress.setRange(0, 100); self.save_tool_stage.setText("存档工具任务失败"); self.save_tool_percent.setText("失败"); self.save_tool_result.setPlainText(error); QMessageBox.critical(self, "存档工具任务失败", error)

    def verify_save_conversion_package(self):
        source = self._current_save_tool_source()
        if source is None: return
        try: report = SaveToolsService(self.storage.root).verify_conversion_package(source)
        except Exception as exc: return self._save_tool_failed(str(exc))
        self._set_save_tool_progress(TaskProgress(100, "转换包校验通过", f"已校验 {report['entries']} 个文件"))
        self.save_tool_result.setPlainText(f"世界：{report['world_id']}\n文件：{report['entries']}\nSHA-256：{report['sha256']}")

    def _convert_save_file_dialog(self, source_suffix: str, target_suffix: str):
        label = source_suffix[1:].upper()
        source, _ = QFileDialog.getOpenFileName(self, f"选择 {label} 文件", "", f"{label} 文件 (*{source_suffix})")
        if not source: return
        default = str(Path(source).with_suffix(target_suffix))
        output, _ = QFileDialog.getSaveFileName(self, f"保存 {target_suffix[1:].upper()} 文件", default, f"{target_suffix[1:].upper()} 文件 (*{target_suffix})")
        if not output: return
        if not output.lower().endswith(target_suffix): output += target_suffix
        self._set_save_tool_progress(TaskProgress(0, "转换存档格式", f"正在转换 {Path(source).name}", True))
        worker = Worker(lambda: SaveToolsService(self.storage.root).convert_save_file(Path(source), Path(output)))
        worker.signals.finished.connect(self._save_file_conversion_done); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def convert_sav_to_json(self): self._convert_save_file_dialog(".sav", ".json")

    def convert_json_to_sav(self): self._convert_save_file_dialog(".json", ".sav")

    def _save_file_conversion_done(self, report):
        self._set_save_tool_progress(TaskProgress(100, "格式转换完成", report["output"]))
        backup = f"\n原目标备份：{report['backup']}" if report.get("backup") else ""
        self.save_tool_result.setPlainText(f"输出：{report['output']}\nSHA-256：{report['sha256']}{backup}")
        self.append_log(f"存档格式转换完成：{report['output']}")

    def convert_steam_id(self):
        value, ok = QInputDialog.getText(self, "SteamID 转 Palworld UID", "SteamID64、steam_ 前缀或 Steam 个人资料链接：")
        if not ok: return
        try: result = SaveToolsService(self.storage.root).steam_id_to_uid(value)
        except Exception as exc: return self._save_tool_failed(str(exc))
        text = f"SteamID：{result['steam_id']}\nPalworld UID：{result['palworld_uid']}\nNoSteam UID：{result['nosteam_uid']}"
        self.save_tool_result.setPlainText(text); QApplication.clipboard().setText(result["palworld_uid"]); self._set_save_tool_progress(TaskProgress(100, "UID 转换完成", "Palworld UID 已复制到剪贴板"))

    def restore_save_map(self):
        source = self._current_save_tool_source()
        if source is None: return
        try:
            source = SaveToolsService(self.storage.root).materialize_source(source, getattr(self, "save_tool_selected_world_id", ""))
            candidates = [source] if source.is_file() and source.name.casefold() == "localdata.sav" else list(source.rglob("LocalData.sav"))
            if not candidates: raise FileNotFoundError("所选来源中未找到 LocalData.sav")
            local_data = max(candidates, key=lambda path: path.stat().st_mtime)
        except Exception as exc:
            return self._save_tool_failed(str(exc))
        if QMessageBox.question(self, "恢复地图迷雾", f"将备份并修改：\n{local_data}\n\n地图标记会保留，Game Pass WGS 源不会被直接修改。继续吗？") != QMessageBox.Yes: return
        self._set_save_tool_progress(TaskProgress(0, "恢复地图迷雾", "正在生成并验证 LocalData.sav 候选", True))
        worker = Worker(lambda: SaveToolsService(self.storage.root).restore_map_file(local_data))
        worker.signals.finished.connect(self._map_restore_done); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def _map_restore_done(self, report):
        self._set_save_tool_progress(TaskProgress(100, "地图迷雾已恢复", report["output"]))
        self.save_tool_result.setPlainText(f"LocalData：{report['output']}\n遮罩：{report.get('mask_textures', 0)}\n隐藏地点：{report.get('hidden_locations', 0)}\n保护备份：{report['backup']}\nSHA-256：{report['sha256']}")
        self.append_log(f"地图迷雾恢复完成：{report['output']}")

    def expand_save_palbox(self):
        source = self._current_save_tool_source()
        if source is None: return
        service = SaveToolsService(self.storage.root)
        try:
            source = service.materialize_source(source, getattr(self, "save_tool_selected_world_id", "")); _level, payload = service.load_world_snapshot(source)
        except Exception as exc: return self._save_tool_failed(str(exc))
        players = [player for player in payload.get("players", []) if service._player_guid(player)]
        if not players: return QMessageBox.information(self, "扩容 Palbox", "当前世界没有可识别玩家。")
        labels = [f"{player.get('nickname') or '未命名'} · {service._player_guid(player)}" for player in players]
        label, ok = QInputDialog.getItem(self, "选择玩家", "要扩容 Palbox 的玩家：", labels, 0, False)
        if not ok: return
        slots, ok = QInputDialog.getInt(self, "Palbox 槽位", "新的最大槽位数：", 960, 1, 99999, 1)
        if not ok: return
        parent = QFileDialog.getExistingDirectory(self, "选择扩容世界输出位置")
        if not parent: return
        player = players[labels.index(label)]; destination = Path(parent) / f"{Path(source).name}-palbox-{slots}"
        self._set_save_tool_progress(TaskProgress(0, "扩容 Palbox", "正在构建并验证离线候选世界", True))
        worker = Worker(lambda: service.expand_palbox_world(source, service._player_guid(player), slots, destination))
        worker.signals.finished.connect(self._palbox_expand_done); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def _palbox_expand_done(self, report):
        backup = f"\n旧输出备份：{report['backup']}" if report.get("backup") else ""
        self._set_save_tool_progress(TaskProgress(100, "Palbox 扩容完成", report["destination"]))
        self.save_tool_result.setPlainText(f"候选世界：{report['destination']}\n玩家：{report['player_guid']}\n槽位：{report['old_slots']} -> {report['new_slots']}\n已使用：{report['used_slots']}{backup}")
        self.append_log(f"Palbox 扩容候选已生成：{report['destination']}")

    def convert_gamepass_world(self):
        source = self._current_save_tool_source()
        if source is None: return
        service = SaveToolsService(self.storage.root)
        try:
            worlds = service.detect_gamepass_worlds(source)
        except Exception as exc:
            return self._save_tool_failed(str(exc))
        if not worlds:
            return QMessageBox.information(self, "选择 Game Pass 存档", "当前来源不是可读取的 Game Pass WGS 用户容器。请扫描本地存档，或选择包含 containers.index 的用户目录。")
        selected_id = getattr(self, "save_tool_selected_world_id", "")
        selected = next((world for world in worlds if world.save_id == selected_id), None)
        if selected is None and len(worlds) > 1:
            labels = [f"{world.save_id}（{world.player_count} 名玩家）" for world in worlds]
            label, ok = QInputDialog.getItem(self, "选择 Game Pass 世界", "要转换的世界：", labels, 0, False)
            if not ok: return
            selected = worlds[labels.index(label)]
        selected = selected or worlds[0]
        parent = QFileDialog.getExistingDirectory(self, "选择 Steam 世界输出位置")
        if not parent: return
        destination = Path(parent) / selected.save_id
        self._set_save_tool_progress(TaskProgress(0, "Game Pass 转 Steam", "正在只读解析 WGS 容器", True))
        worker = Worker(lambda signals: service.convert_gamepass_to_steam(source, selected.save_id, destination, lambda p, m: signals.progress.emit(TaskProgress(p, "Game Pass 转 Steam", m))), with_signals=True)
        worker.signals.progress.connect(self._set_save_tool_progress); worker.signals.finished.connect(self._gamepass_conversion_done); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def _gamepass_conversion_done(self, report):
        self.save_tool_source.setText(report["destination"]); self.save_tool_selected_world_id = report["save_id"]; self.save_tool_selected_kind = "folder"
        backup = f"\n旧目标备份：{report['backup']}" if report.get("backup") else ""
        self._set_save_tool_progress(TaskProgress(100, "Game Pass 转换完成", report["destination"]))
        self.save_tool_result.setPlainText(f"Steam 世界：{report['destination']}\n玩家文件：{report['player_count']}\n文件：{len(report['files'])}\nWGS 源保持只读{backup}")
        self.append_log(f"Game Pass 世界已只读转换：{report['save_id']} -> {report['destination']}")

    def rebind_save_identity(self):
        source = self._current_save_tool_source()
        if source is None: return
        service = SaveToolsService(self.storage.root)
        try:
            source = service.materialize_source(source, getattr(self, "save_tool_selected_world_id", ""))
            _level, payload = service.load_world_snapshot(source)
        except Exception as exc:
            return self._save_tool_failed(str(exc))
        players = [player for player in payload.get("players", []) if service._player_guid(player) and player.get("instance_id")]
        if len(players) < 2:
            return QMessageBox.information(self, "角色重绑定", "当前世界至少需要原角色和一个已登录创建的占位角色。")
        labels = [f"{player.get('nickname') or '未命名'} · {service._player_guid(player)}" for player in players]
        old_label, ok = QInputDialog.getItem(self, "选择原角色", "要保留数据的原角色：", labels, 0, False)
        if not ok: return
        remaining = [label for label in labels if label != old_label]
        new_label, ok = QInputDialog.getItem(self, "选择占位角色", "用于接收原角色数据的新身份：", remaining, 0, False)
        if not ok: return
        old = players[labels.index(old_label)]; new = players[labels.index(new_label)]
        parent = QFileDialog.getExistingDirectory(self, "选择重绑定世界输出位置")
        if not parent: return
        destination = Path(parent) / f"{Path(source).name}-identity-rebound"
        self._set_save_tool_progress(TaskProgress(0, "角色身份重绑定", "正在构建并验证离线候选世界", True))
        worker = Worker(lambda: service.rebind_world_identity(source, service._player_guid(old), service._player_guid(new), destination))
        worker.signals.finished.connect(self._identity_rebind_done); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def _identity_rebind_done(self, report):
        backup = f"\n旧输出备份：{report['backup']}" if report.get("backup") else ""
        self._set_save_tool_progress(TaskProgress(100, "角色重绑定完成", report["destination"]))
        self.save_tool_result.setPlainText(f"候选世界：{report['destination']}\n原 GUID：{report['old_guid']}\n新 GUID：{report['new_guid']}\n迁移玩家：{report.get('migrated', 0)}{backup}")
        self.append_log(f"角色身份重绑定候选已生成：{report['destination']}")

    def open_save_diagnostics(self):
        source = self._current_save_tool_source()
        if source is None: return
        self._set_save_tool_progress(TaskProgress(0, "解析存档", "正在构建地图与关系诊断", True))
        def task():
            service = SaveToolsService(self.storage.root); level, payload = service.load_world_snapshot(source); return service.diagnose(source), payload
        worker = Worker(task); worker.signals.finished.connect(self._save_diagnostics_ready); worker.signals.error.connect(self._save_tool_failed); self.pool.start(worker)

    def _save_diagnostics_ready(self, result):
        report, payload = result; self._set_save_tool_progress(TaskProgress(100, "存档诊断完成", f"发现 {len(report.findings)} 条风险"))
        self.save_tool_result.setPlainText(f"Level.sav：{report.level_path}\n玩家：{report.players} · 帕鲁：{report.pals} · 公会：{report.guilds} · 基地：{report.bases}\n风险：{len(report.findings)}")
        dialog = SaveDiagnosticsDialog(report, payload, self); dialog.exec()

    def import_selected_save_source(self):
        source = self._current_save_tool_source()
        if source is not None:
            try: source = SaveToolsService(self.storage.root).materialize_source(source, getattr(self, "save_tool_selected_world_id", ""))
            except Exception as exc: return self._save_tool_failed(str(exc))
            self._import_local_save_source(source)

    def start_selected_coop_migration(self):
        source = self._current_save_tool_source()
        if source is not None:
            try: source = SaveToolsService(self.storage.root).materialize_source(source, getattr(self, "save_tool_selected_world_id", ""))
            except Exception as exc: return self._save_tool_failed(str(exc))
            self._start_coop_migration_source(source)

    def detect_local_save_sources(self):
        try:
            sources = BackupPackageService().detect_local_save_sources()
        except Exception as exc:
            return QMessageBox.critical(self, "本地存档检测失败", str(exc))
        if not sources:
            return QMessageBox.information(self, "未检测到本地存档", "未在常见 Palworld 路径找到世界存档，请使用“选择存档文件”或“选择存档文件夹”。")
        lines = [f"{index}. {item.world_id} | {item.source_kind} | {item.file_count} 个文件 | {item.source_path}" for index, item in enumerate(sources, 1)]
        QMessageBox.information(self, "检测到本地存档", "\n".join(lines))

    def import_local_save_file(self):
        if not self.selected: return
        path, _ = QFileDialog.getOpenFileName(self, "选择本地 Palworld 存档", "", "存档来源 (*.zip *.tar.gz *.tgz *.pwcbackup *.sav);;所有文件 (*)")
        if path: self._import_local_save_source(Path(path))

    def import_local_save_directory(self):
        if not self.selected: return
        path = QFileDialog.getExistingDirectory(self, "选择本地 Palworld 存档文件夹")
        if path: self._import_local_save_source(Path(path))

    def _import_local_save_source(self, source: Path):
        if self.install_task_active: return QMessageBox.information(self, "任务进行中", "当前服务器任务尚未结束。")
        source = Path(source).expanduser().resolve()
        try:
            inspection = BackupPackageService().inspect_save_source(source)
        except Exception as exc:
            return QMessageBox.critical(self, "本地存档检测失败", str(exc))
        warnings = "\n".join(inspection.warnings) if inspection.warnings else "无"
        detail = f"来源：{inspection.source_path}\n世界：{inspection.world_id}\n文件：{inspection.file_count}\n格式：{inspection.save_format}\n警告：{warnings}\n\n将覆盖服务器当前世界的对应文件，保留服务器额外文件。继续吗？"
        if QMessageBox.question(self, "确认导入本地存档", detail) != QMessageBox.Yes: return
        selected = self.selected; self._begin_backup_task("导入本地存档")
        def task(signals):
            service = BackupPackageService()
            if selected.kind == "local":
                lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, signals.log.emit)
                target = Path(selected.install_dir).expanduser().resolve() / "Pal" / "Saved" / "SaveGames"
                return service.import_local_save_to_server(source, target, lifecycle.stop, lifecycle.start, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
            package = self._backup_repository().import_source(source, selected, on_progress=lambda p, m: signals.progress.emit(TaskProgress(min(30, p // 3), "准备本地存档", m)))
            client = self._remote_client(); lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit)
            platform_name = str(selected.remote_profile.get("platform") or "linux").lower(); install_dir = str(selected.remote_profile.get("install_dir") or selected.install_dir)
            if platform_name == "windows":
                from .services import WindowsRemotePath
                target = WindowsRemotePath.normalize(install_dir).rstrip("\\/") + "\\Pal\\Saved\\SaveGames"
            else:
                target = install_dir.rstrip("/") + "/Pal/Saved/SaveGames"
            return RestoreTransaction().restore_savegames_remote(package, target, client, platform_name, lifecycle.stop, lifecycle.start, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
        worker = Worker(task, with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._local_save_import_done); worker.signals.error.connect(self._backup_task_failed); self.pool.start(worker)

    def _local_save_import_done(self, result):
        self.append_log(result.detail); self._finish_backup_task(); self.refresh_status(); self.load_save_snapshot(); QMessageBox.information(self, "本地存档导入完成", result.detail)

    def start_coop_migration(self):
        if not self.selected: return
        source, _ = QFileDialog.getOpenFileName(self, "选择本地联机存档来源", "", "存档来源 (*.zip *.tar.gz *.tgz *.pwcbackup *.sav);;所有文件 (*)")
        if not source:
            source = QFileDialog.getExistingDirectory(self, "选择本地联机存档文件夹")
        if source: self._start_coop_migration_source(Path(source))

    def _start_coop_migration_source(self, source: Path):
        if not self.selected: return
        selected = self.selected; source = Path(source).expanduser().resolve()
        if self.install_task_active: return QMessageBox.information(self, "任务进行中", "当前服务器任务尚未结束。")
        self._begin_backup_task("准备本地联机存档恢复")
        def task(signals):
            package = self._backup_repository().import_source(source, selected, on_progress=lambda p, m: signals.progress.emit(TaskProgress(min(20, p // 5), "导入本地联机存档", m, False)))
            return self._prepare_restore_migration_task(signals, selected, package)
        worker = Worker(task, with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress)
        worker.signals.finished.connect(lambda payload: self._restore_migration_prepared(payload, "本地联机存档恢复")); worker.signals.error.connect(self._backup_task_failed); self.pool.start(worker)

    def _coop_deploy_done(self, result, session):
        self._finish_backup_task(); self.refresh_status(); self.append_log(f"本地联机世界已部署：{session.detail}"); QMessageBox.information(self, "等待玩家创建临时角色", "世界已部署到专用服务器。请所有需要迁移的玩家分别进入服务器创建一次临时角色，然后退出服务器。完成后使用“联机角色迁移 → 刷新临时角色并迁移”。")

    def finish_coop_migration(self):
        if not self.selected: return
        service = BackupPackageService(); session = service.load_migration_session(self.storage.root, self.selected.id)
        if not session: return QMessageBox.information(self, "角色迁移", "没有找到待处理的迁移会话，请先恢复包含玩家的存档或部署本地联机世界。")
        if session.package_path:
            if session.phase == "complete": return QMessageBox.information(self, "玩家迁移", "当前恢复会话中的玩家身份已经全部迁移完成。")
            original = Path(session.original_source_path or session.source_path)
            if session.phase == "source_missing" or not original.is_dir() or not (original / "Level.sav").is_file():
                return QMessageBox.warning(self, "需要重新关联原始存档", "不可变原始联机存档不存在或不可读取。请重新选择原始联机存档/转换包后再继续，当前专服世界不会被修改。")
            if session.phase == "deploying":
                candidate = Path(self.storage.root) / "migrations" / self.selected.id / "restore" / "candidate"
                if candidate.is_dir():
                    if QMessageBox.question(self, "继续部署玩家迁移", "检测到上次已生成但尚未部署的迁移候选。是否继续部署？") != QMessageBox.Yes:
                        return
                    selected = self.selected; self._begin_backup_task("继续部署玩家迁移")
                    worker = Worker(lambda signals: self._resume_restore_deployment_task(signals, selected, session), with_signals=True)
                    worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress)
                    worker.signals.finished.connect(lambda result: self._restore_done(result, "继续部署玩家迁移")); worker.signals.error.connect(self._restore_failed); self.pool.start(worker); return
                session = replace(session, phase="waiting_placeholders", detail="上次候选已不存在，请重新刷新临时角色")
                service.save_migration_session(self.storage.root, session)
            if session.phase not in {"waiting_placeholders", "mapping_ready"}: return QMessageBox.information(self, "玩家迁移", f"当前迁移阶段为：{session.phase}")
            self._begin_backup_task("继续玩家迁移")
            if session.phase == "mapping_ready" and session.placeholder_players:
                self._restore_continuation_prepared(session); return
            selected = self.selected
            worker = Worker(lambda signals: self._prepare_restore_continuation_task(signals, selected, session), with_signals=True)
            worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress)
            worker.signals.finished.connect(self._restore_continuation_prepared); worker.signals.error.connect(self._backup_task_failed); self.pool.start(worker); return
        if session.phase not in {"world_deployed", "waiting_placeholders", "mapping_ready"}: return QMessageBox.information(self, "角色迁移", f"当前迁移阶段为：{session.phase}")
        try: session = service.refresh_coop_placeholders(session, self.storage.root)
        except Exception as exc: return QMessageBox.critical(self, "刷新临时角色失败", str(exc))
        if not session.placeholder_players: return QMessageBox.information(self, "尚未发现临时角色", "请让玩家进入专用服务器创建角色并退出后再刷新。")
        options = [f"{str(item.get('player_guid') or item.get('player_uid') or '').replace('-', '').upper()} · {item.get('nickname') or '未命名'}" for item in session.placeholder_players]
        confirmations = {}
        try:
            for player in session.source_players:
                old = str(player.get("player_guid") or player.get("player_uid") or "").replace("-", "").upper(); old_name = player.get("nickname") or "未命名"
                choice, ok = QInputDialog.getItem(self, "确认玩家映射", f"本地角色：{old_name} ({old})\n请选择对应的专服临时角色：", options, 0, False)
                if not ok: return
                confirmations[old] = choice.split(" · ", 1)[0]
            session = service.build_identity_mappings(session, confirmations, self.storage.root)
        except Exception as exc: return QMessageBox.critical(self, "玩家映射失败", str(exc))
        if QMessageBox.question(self, "执行角色迁移", f"确认迁移 {len(session.mappings)} 个玩家角色？迁移会停止服务器并创建迁移前备份。") != QMessageBox.Yes: return
        selected = self.selected; self._begin_backup_task("迁移玩家角色")
        def task(signals):
            lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, signals.log.emit)
            return service.apply_coop_migration(session, lifecycle.stop, lifecycle.start, lambda: lifecycle.status() == "running", self.storage.root, lambda p, s, m: signals.progress.emit(TaskProgress(p, s, m, False)))
        worker = Worker(task, with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._coop_migration_done); worker.signals.error.connect(self._backup_task_failed); self.pool.start(worker)

    def _prepare_restore_continuation_task(self, signals, selected, session):
        import shutil
        service = BackupPackageService(); root = Path(self.storage.root) / "migrations" / selected.id / "restore" / "continuation-snapshot"
        if root.exists(): shutil.rmtree(root)
        root.mkdir(parents=True)
        signals.progress.emit(TaskProgress(5, "保存当前世界", "正在取得恢复后服务器世界快照", False))
        try:
            self._rest_client().save()
        except Exception as exc:
            signals.log.emit(f"REST 保存世界未完成，将在停服后读取磁盘存档：{exc}")
        if selected.kind == "local":
            lifecycle = self.lifecycle if isinstance(self.lifecycle, LocalServerLifecycle) else LocalServerLifecycle(selected, signals.log.emit)
            target_world = Path(session.target_world_path).expanduser().resolve(); lifecycle.stop()
            try:
                savegames = root / "SaveGames"; savegames.mkdir()
                shutil.copytree(target_world, savegames / target_world.name)
            finally:
                lifecycle.start()
            return service.refresh_restore_placeholders(session, root, self.storage.root)
        client = self._remote_client(); lifecycle = _remote_lifecycle_for(selected, client, signals.log.emit)
        install_dir = str(selected.remote_profile.get("install_dir") or selected.install_dir)
        lifecycle.stop()
        try:
            archive = BackupService().create_remote(client, selected, root, install_dir)
        finally:
            lifecycle.start()
        if archive is None: raise RuntimeError("无法下载恢复后的远程世界快照")
        saved = service.extract_saved_snapshot(archive, root / "extracted")
        return service.refresh_restore_placeholders(session, saved, self.storage.root)

    def _restore_continuation_prepared(self, session):
        service = BackupPackageService()
        if not session.placeholder_players:
            self._finish_backup_task(False)
            return QMessageBox.information(self, "尚未发现临时角色", "请让待迁移玩家进入恢复后的服务器创建临时角色并退出，然后再次继续迁移。")
        pending = set(session.pending_player_guids); targets = list(session.placeholder_players)
        migrated_count = sum(1 for item in session.mappings if item.status == "migrated")
        self.append_log(f"继续迁移第 {session.snapshot_generation + 1} 轮：已完成 {migrated_count} 个，待处理 {len(pending)} 个")
        confirmations = {}; used = {item.new_guid for item in session.mappings if item.status == "migrated"}
        for player in session.source_players:
            old_guid = service._player_guid(player)
            if old_guid not in pending: continue
            old_name = str(player.get("nickname") or "未命名")
            available = list(service.available_identity_targets(old_guid, targets, used))
            options = ["暂不迁移"] + [f"{service._player_guid(item)} · {item.get('nickname') or '未命名'}" for item in available]
            matches = [index for index, target in enumerate(available, 1) if str(target.get("nickname") or "").casefold() == old_name.casefold()]
            suggested = matches[0] if len(matches) == 1 else 0
            choice, ok = QInputDialog.getItem(self, "继续玩家身份迁移", f"备份角色：{old_name} ({old_guid})\n请选择刚在服务器创建的临时角色：", options, suggested, False)
            if not ok: self._finish_backup_task(False); return
            if choice == "暂不迁移": continue
            new_guid = choice.split(" · ", 1)[0]
            if new_guid in used:
                self._finish_backup_task(False); return QMessageBox.critical(self, "映射冲突", "同一个服务器身份不能分配给多个备份角色。")
            used.add(new_guid); confirmations[old_guid] = new_guid
        if not confirmations:
            self._finish_backup_task(False); return QMessageBox.information(self, "未选择玩家", "本次没有确认任何玩家映射，服务器存档未修改。")
        try: session = service.confirm_restore_mappings(session, confirmations, self.storage.root)
        except Exception as exc: self._finish_backup_task(False); return QMessageBox.critical(self, "玩家映射失败", str(exc))
        if QMessageBox.question(self, "执行玩家迁移", f"本次将迁移 {len(confirmations)} 个玩家；完成后仍有 {len(session.pending_player_guids)} 个玩家待处理。") != QMessageBox.Yes:
            self._finish_backup_task(False); return
        selected = self.selected
        worker = Worker(lambda signals: self._deploy_restore_migration_task(signals, selected, session, session.backup_path), with_signals=True)
        worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress)
        worker.signals.finished.connect(lambda result: self._restore_done(result, "继续玩家迁移")); worker.signals.error.connect(self._restore_failed); self.pool.start(worker)

    def _coop_migration_done(self, result):
        self._finish_backup_task(); self.refresh_status(); self.load_save_snapshot(); self.append_log(result.detail); QMessageBox.information(self, "角色迁移完成", result.detail)

    @staticmethod
    def _friendly_backup_error(error) -> str:
        text = str(error)
        if "[Errno 2]" in text or "No such file" in text:
            return f"恢复所需的文件或目录不存在。请重新导入备份并确认服务器已完成安装。\n原始信息：{text}"
        return text

    def export_selected_backup(self):
        package = self._selected_backup_path()
        if not package: return QMessageBox.information(self, "导出", "请先选择备份。")
        if package.suffix.lower() != ".pwcbackup": return QMessageBox.warning(self, "导出", "旧格式请先转换为 .pwcbackup。")
        manifest = BackupPackageService().read_manifest(package)
        target, _ = QFileDialog.getSaveFileName(self, "导出备份", str(Path.home() / package.name), "Palworld Console Backup (*.pwcbackup)")
        if not target: return
        target_path = Path(target)
        overwrite = target_path.exists()
        if overwrite and QMessageBox.question(self, "覆盖导出文件", f"目标文件已存在，确认原子覆盖？\n{target_path}") != QMessageBox.Yes: return
        world_only = manifest.backup_type != "world" and QMessageBox.question(self, "导出范围", "是否转换为仅含 SaveGames 的世界导出包？\n选择“否”将导出当前完整脱敏灾备包。") == QMessageBox.Yes
        self.run_async(lambda: BackupPackageService().export(package, target_path, world_only, overwrite), lambda path: self.append_log(f"备份已导出：{path}"))

    def export_selected_backup_report(self):
        package = self._selected_backup_path()
        if not package or package.suffix.lower() != ".pwcbackup": return QMessageBox.information(self, "校验报告", "请选择已转换并校验的 .pwcbackup 文件。")
        target, _ = QFileDialog.getSaveFileName(self, "导出清单与 SHA-256 报告", str(Path.home() / f"{package.stem}-checksums.txt"), "文本报告 (*.txt)")
        if not target: return
        target_path = Path(target); overwrite = target_path.exists()
        if overwrite and QMessageBox.question(self, "覆盖校验报告", f"目标文件已存在，确认覆盖？\n{target_path}") != QMessageBox.Yes: return
        self.run_async(lambda: BackupPackageService().export_report(package, target_path, overwrite), lambda path: self.append_log(f"校验报告已导出：{path}"))

    def verify_selected_backup(self):
        package = self._selected_backup_path()
        if not package or package.suffix.lower() != ".pwcbackup": return QMessageBox.information(self, "校验", "请选择 .pwcbackup 文件。")
        self.run_async(lambda: BackupPackageService().validate(package), lambda _manifest: (self._backup_repository().set_metadata(package, verified_at=datetime.now().isoformat(timespec="seconds")), self.refresh_backup_list(), QMessageBox.information(self, "校验完成", "CRC、manifest 和全部 SHA-256 均通过。")))

    def note_selected_backup(self):
        package = self._selected_backup_path()
        if not package: return
        note, ok = QInputDialog.getText(self, "备份备注", "备注：")
        if ok: self._backup_repository().set_metadata(package, note=note); self.refresh_backup_list()

    def toggle_selected_backup_protection(self):
        package = self._selected_backup_path()
        if not package: return
        record = next((item for item in getattr(self, "backup_records", []) if Path(item["path"]) == package), None)
        protected = bool(record and record.get("protected"))
        action = "解除保护" if protected else "保护"
        if QMessageBox.question(self, action, f"确认{action}此备份？\n{package.name}") != QMessageBox.Yes: return
        self._backup_repository().set_metadata(package, protected=not protected); self.refresh_backup_list()

    def delete_selected_backup(self):
        package = self._selected_backup_path()
        if not package: return
        if QMessageBox.warning(self, "删除备份", f"确认删除？\n{package}", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
        try:
            self._backup_repository().delete(package)
            if self.selected and self.selected.last_backup == str(package): self.selected.last_backup = ""; self.storage.save_instances(self.instances)
            self.refresh_backup_list()
        except Exception as exc: QMessageBox.critical(self, "删除失败", str(exc))
    def save_ini(self):
        if not self.selected or not self.selected.install_dir: return QMessageBox.warning(self, "提示", "请先填写本地安装目录")
        try:
            values = self._collect_config_values()
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
            worker = Worker(lambda signals: self._update_remote_config_checked(client, selected, values), with_signals=True)
            worker.signals.finished.connect(self._config_saved); worker.signals.error.connect(lambda e: QMessageBox.critical(self, "配置错误", e)); self.pool.start(worker)
        else:
            try: self._config_saved(self._update_local_config_checked(self.selected, values))
            except Exception as exc: QMessageBox.critical(self, "配置错误", str(exc))

    def _config_saved(self, result):
        self._apply_config_result(result)
        self.config_cache.clear_draft(self.selected.id)
        self.selected.config_source = "用户修改"
        self.selected.config_restart_required = True
        self.config_source_label.setText("配置状态：用户修改，需要重启")
        self.storage.save_instances(self.instances)
        self.append_log("配置已保存并创建备份，需要重启服务器后生效")

    def _collect_config_values(self) -> dict[str, object]:
        values = {}
        for key, edit in self.ini_fields.items():
            text = self._setting_text(edit)
            if text != "":
                values[key] = coerce_setting_value(key, text)
        return values

    def save_config_draft(self):
        if not self.selected:
            return
        try:
            values = self._collect_config_values()
        except Exception as exc:
            return QMessageBox.critical(self, "草稿错误", str(exc))
        server_password = str(values.get("ServerPassword") or "")
        if server_password:
            self.selected.server_password_secret_ref = self.selected.server_password_secret_ref or f"server-password-{self.selected.id}"
            self.storage.set_secret(self.selected.server_password_secret_ref, server_password)
        snapshot = self.config_cache.load_snapshot(self.selected.id)
        draft = self.config_cache.save_draft(self.selected.id, values, snapshot.content_hash if snapshot else "")
        self.selected.config_cache_state = {"draft_saved_at": draft.saved_at, "has_draft": True}
        self.storage.save_instances(self.instances)
        self.config_source_label.setText(f"配置状态：离线草稿，保存于 {draft.saved_at}，尚未推送")
        self.append_log("游戏配置已保存为本地草稿，不会自动写入服务器")

    def _update_local_config_checked(self, selected, values):
        snapshot = self.config_cache.load_snapshot(selected.id)
        if snapshot:
            current = ServerConfigBootstrap.read_local(selected)
            if self.config_cache.hash_values(current.values) != snapshot.content_hash:
                raise RuntimeError("服务器配置已被外部修改，请先重新读取并处理差异")
        return ServerConfigBootstrap.update_local(selected, values)

    def _update_remote_config_checked(self, client, selected, values):
        snapshot = self.config_cache.load_snapshot(selected.id)
        if snapshot:
            current = ServerConfigBootstrap.read_remote(client, selected)
            if self.config_cache.hash_values(current.values) != snapshot.content_hash:
                raise RuntimeError("远程配置已被外部修改，请先重新读取并处理差异")
        return ServerConfigBootstrap.update_remote(client, selected, values)

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

    def _load_cached_config(self):
        if not self.selected:
            return
        snapshot = self.config_cache.load_snapshot(self.selected.id)
        if not snapshot:
            self.config_original = {}
            return
        self._apply_config_result(ConfigSyncResult(snapshot.values, snapshot.config_path, "本地缓存", False, snapshot.synced_at), persist_cache=False)
        self.config_secret_presence = dict(snapshot.secret_presence)
        server_password = self.storage.get_secret(self.selected.server_password_secret_ref)
        if server_password and "ServerPassword" in self.ini_fields:
            self._set_setting_widget(self.ini_fields["ServerPassword"], server_password)
            self.config_original["ServerPassword"] = server_password
        draft = self.config_cache.load_draft(self.selected.id)
        if draft:
            for key, value in draft.values.items():
                if key in self.ini_fields:
                    self._set_setting_widget(self.ini_fields[key], value)
            self.config_source_label.setText(f"配置状态：离线草稿，保存于 {draft.saved_at}，尚未推送")
        else:
            self.config_source_label.setText(f"配置状态：本地缓存，服务器同步于 {snapshot.synced_at}")
        self._config_changed()

    def _apply_config_result(self, result: ConfigSyncResult, persist_cache: bool = True):
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
        server_password = str(result.values.get("ServerPassword") or "")
        if server_password:
            self.selected.server_password_secret_ref = self.selected.server_password_secret_ref or f"server-password-{self.selected.id}"
            self.storage.set_secret(self.selected.server_password_secret_ref, server_password)
            self.config_secret_presence["ServerPassword"] = True
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
        if persist_cache:
            record = self.config_cache.save_snapshot(self.selected.id, result)
            self.selected.config_cache_state = {"snapshot_hash": record.content_hash, "snapshot_synced_at": record.synced_at, "has_draft": False}
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

    def sync_player_center(self):
        if not self.selected or self.install_task_active: return
        selected = self.selected; self.player_center.begin_sync(selected.id); self.player_sync_button.setEnabled(False); self.player_sync_label.setText("正在同步玩家、帕鲁、背包、公会和基地…")
        def sync():
            players = PlayerAdminService(self._rest_client()).list_players()
            path = self._find_save_path(); local = Path(path)
            if selected.kind == "remote":
                local = self.storage.root / "cache" / selected.id / "world" / "Level.sav"; self._download_remote_save_bundle(self._remote_client(), path, local)
            document = SaveGameService().load(local)
            return players, document, path, local
        worker = Worker(sync); worker.signals.finished.connect(self._player_center_sync_done); worker.signals.error.connect(self._player_center_sync_failed); self.pool.start(worker)

    def _player_center_sync_done(self, payload):
        players, document, path, local = payload
        try:
            self.save_document, self.save_scalar_values = document, SaveGameService.flatten(document.properties)
            self.save_remote_path, self.save_working_path = path, local
            ready = isinstance(document, PluginParsedSave)
            if ready:
                run_id = self.player_repository.begin_sync(self.selected.id)
                try:
                    count = self.player_repository.upsert_save_snapshot(self.selected.id, document.properties)
                    self.player_repository.finish_sync(run_id, "success", count)
                except Exception as exc:
                    self.player_repository.finish_sync(run_id, "failed", detail=str(exc)); raise
            self._players_loaded(players)
            self.current_players = self.player_repository.list_players(self.selected.id)
            self.current_player_groups = self.player_repository.list_identity_groups(self.selected.id)
            self._render_players()
            self.save_path_label.setText(f"已载入：{path} · {len(self.save_scalar_values)} 个结构化字段")
            self.player_center.complete_sync(self.selected.id, list(document.properties.get("players", [])), [asdict(player) for player in players], path, ready)
            self._render_save_fields(); self.append_log("玩家、帕鲁、背包、公会、基地和在线状态已完成一次性同步")
            self.player_detail_tabs.setEnabled(True); self._set_player_editing_enabled(ready); self.player_sync_button.setEnabled(True); self.player_sync_label.setText("已同步，可选择玩家进入详情"); self.player_detail_sync_label.setText(f"同步于 {self.player_center.snapshot.synced_at}")
        except Exception as exc:
            self._player_center_sync_failed(str(exc))

    def _player_center_sync_failed(self, error):
        self.player_center.fail_sync(error); self.player_sync_button.setEnabled(True); self.player_sync_label.setText("同步失败，保留上次有效数据"); self.player_detail_sync_label.setText("数据已过期" if self.player_center.snapshot.synced else "同步失败"); self.player_detail_tabs.setEnabled(bool(self.player_center.snapshot.synced)); self._set_player_editing_enabled(bool(self.player_center.snapshot.plugin_ready)); QMessageBox.critical(self, "玩家中心同步失败", error)

    def _players_loaded(self, players):
        if not self.selected: return
        players = PlayerIdentityService.deduplicate_online(players)
        self.player_repository.overlay_online(self.selected.id, players)
        self.current_players = self.player_repository.list_players(self.selected.id)
        self.current_player_groups = self.player_repository.list_identity_groups(self.selected.id)
        self._render_players(); self._enforce_whitelist(players); self.append_log(f"玩家列表已刷新：{len(players)} 个唯一在线身份，玩家中心 {len(self.current_player_groups)} 人")

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
        selected_user = self._selected_player_id() if self.players_table.currentRow() >= 0 else ""
        selected_uid = self.active_player_uid or (self._selected_player_uid() if self.players_table.currentRow() >= 0 else "")
        query = self.player_search.text().strip().lower() if hasattr(self, "player_search") else ""; state = self.player_state_filter.currentData() if hasattr(self, "player_state_filter") else "all"
        repository_groups = self.player_repository.list_identity_groups(self.selected.id) if self.selected else []
        self.current_player_groups = repository_groups or PlayerIdentityService.group(self.current_players)
        rows = [group for group in self.current_player_groups if (not query or query in group.primary.name.lower() or query in group.primary.user_id.lower() or query in group.primary.account_name.lower() or any(query in uid.lower() for uid in group.aliases)) and (state == "all" or (state == "online" and group.primary.online) or (state == "offline" and not group.primary.online and group.primary.save_status != "missing") or (state == "missing" and group.primary.save_status == "missing"))]
        self.players_table.setSortingEnabled(False); self.players_table.blockSignals(True); self.players_table.setRowCount(len(rows))
        for row, group in enumerate(rows):
            player = group.primary; draft_count = sum(self.player_center.pending_count(self.selected.id, uid) for uid in group.role_uids) if self.selected else 0
            values = ("在线" if player.online else "离线", player.name or player.account_name or "未命名玩家", player.account_name or player.user_id, player.level, player.last_seen or "-", "缺失" if player.save_status == "missing" else "存在", f"{draft_count} 项" if draft_count else "-")
            identity = {"player_uid": player.player_uid, "user_id": player.user_id, "aliases": list(group.aliases), "role_uids": list(group.role_uids)}
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, identity); self.players_table.setItem(row, column, item)
            if (selected_user and selected_user in {player.user_id, player.player_uid}) or (selected_uid and selected_uid in group.aliases):
                self.players_table.setCurrentCell(row, 0)
        self.players_table.blockSignals(False); self.players_table.setSortingEnabled(True)
        self.player_list_hint.setText(f"共 {len(rows)} 名唯一玩家。选择玩家后进入其存档详情；返回列表不会丢失草稿。")

    def _selected_player_id(self) -> str:
        row = self.players_table.currentRow(); item = self.players_table.item(row, 0) if row >= 0 else None
        data = item.data(Qt.UserRole) if item else {}; return str((data or {}).get("user_id") or (data or {}).get("player_uid") or "")

    def _selected_player_uid(self) -> str:
        if self.active_player_uid: return self.active_player_uid
        row = self.players_table.currentRow(); item = self.players_table.item(row, 0) if row >= 0 else None
        data = item.data(Qt.UserRole) if item else {}; return str((data or {}).get("player_uid") or "")

    def _show_player_detail(self, row, _column=0, *_args):
        if not self.selected or row < 0: return
        if not self.player_center.snapshot.synced:
            return QMessageBox.information(self, "玩家中心", "请先同步玩家数据，再打开玩家存档详情。")
        uid_item = self.players_table.item(row, 0)
        if not uid_item: return
        identity = uid_item.data(Qt.UserRole) or {}; roles = list(identity.get("role_uids") or [])
        preferred = self.active_player_uid if self.active_player_uid in roles else str(roles[0] if roles else "")
        self.player_role_combo.blockSignals(True); self.player_role_combo.clear()
        for role_uid in roles:
            role = self.player_repository.player_detail(self.selected.id, role_uid).get("player", {})
            self.player_role_combo.addItem(f"{role.get('nickname') or '角色'} · {role_uid}", role_uid)
        index = self.player_role_combo.findData(preferred); self.player_role_combo.setCurrentIndex(max(0, index)); self.player_role_combo.blockSignals(False)
        if preferred:
            self._load_player_role(str(self.player_role_combo.currentData() or preferred), roles)
        else:
            self.active_player_uid = ""; self.player_detail_title.setText(identity.get("user_id") or "历史玩家"); self.player_detail_text.setPlainText("该玩家当前没有可编辑的存档角色，历史身份记录仍然保留。")
            self.player_detail_tabs.setEnabled(True); self._set_player_editing_enabled(False); self._set_player_tab_counts(0, 0, 0)
        self.player_view_stack.setCurrentWidget(self.player_detail_page)

    def _return_to_player_list(self):
        self._update_pending_save_label(); self._render_players(); self.player_view_stack.setCurrentWidget(self.player_list_page); self.players_table.setFocus()

    def _load_player_role(self, uid: str, aliases: list[str] | None = None):
        if not self.selected or not uid: return
        if not self.player_center.snapshot.synced: return
        self.active_player_uid = uid; self.player_center.select(uid); aliases = aliases or [uid]
        detail = self.player_repository.player_detail(self.selected.id, uid)
        player = detail.get("player", {}); pals = detail.get("pals", []); items = detail.get("items", []); guild = detail.get("guild", {}); bases = detail.get("bases", []); completeness = detail.get("completeness", {})
        self.player_detail_title.setText(player.get("nickname") or player.get("account_name") or uid)
        self.player_detail_sync_label.setText(("数据已过期 · " if self.player_center.snapshot.stale else "同步于 ") + (self.player_center.snapshot.synced_at or "未知时间"))
        self.player_note.setText(player.get("note") or "")
        masked_ips = ", ".join(__import__("json").loads(player.get("masked_ips") or "[]")) or "无"
        self.player_detail_text.setPlainText(
            f"关联角色 UID：{', '.join(aliases)}\n当前编辑角色：{uid}\n平台用户 ID：{player.get('user_id') or '-'}\n"
            f"状态：{'在线' if player.get('online') else '离线'} / 存档 {player.get('save_status')}\n"
            f"等级 / 经验：{player.get('level', 0)} / {player.get('experience', 0)}\n"
            f"首次 / 最后出现：{player.get('first_seen') or '-'} / {player.get('last_seen') or '-'}\n"
            f"历史 IP（脱敏）：{masked_ips}\n公会：{guild.get('name') or guild.get('guild_name') or '-'}\n"
            f"关联帕鲁：{len(pals)} 只（{self._data_status_text(completeness.get('pals'))}）\n"
            f"背包记录：{len(items)} 项（{self._data_status_text(completeness.get('inventory'))}）\n"
            f"关联基地：{len(bases)} 个（{self._data_status_text(completeness.get('bases'))}）"
        )
        self.player_pals_table.setRowCount(len(pals))
        for pal_row, pal in enumerate(pals):
            pal_id = str(pal.get("type") or "")
            pal_name = self.localization.display("pals", pal_id)
            gender_id = str(pal.get("gender") or "Unknown")
            stable = bool(pal.get("stable_id_valid", bool(pal.get("individual_id"))))
            state = "可编辑" if stable else pal.get("read_only_reason") or "关系不完整"
            values = (pal_name, pal.get("nickname") or "-", pal.get("level", 0), self.localization.display("gender", gender_id), "是" if pal.get("is_lucky") else "否", pal.get("rank", 0), f"生 {pal.get('melee', 0)} / 攻 {pal.get('ranged', 0)} / 防 {pal.get('defense', 0)}", state)
            metadata = {"individual_id": pal.get("individual_id") or "", "stable_id_valid": stable, "pal": pal}
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, metadata)
                if column == 0: item.setToolTip(f"帕鲁内部 ID：{pal_id}\n稳定 InstanceId：{pal.get('individual_id') or '-'}")
                elif column == 3: item.setToolTip(f"性别内部值：{gender_id}")
                self.player_pals_table.setItem(pal_row, column, item)
        container_counts = {entry.get("key"): int(entry.get("count") or 0) for entry in detail.get("inventory_containers") or []}
        container_summary = "，".join(f"{CONTAINER_LABELS.get(key, key)} {container_counts.get(key, 0)}" for key in CONTAINER_LABELS)
        inventory_reason = detail.get("inventory_read_only_reason") or self._data_status_text(completeness.get("inventory"))
        self.inventory_status_label.setText(f"{inventory_reason} · {container_summary}")
        self._render_inventory_for_active_player(items)
        self._render_player_relations(detail); self._set_player_tab_counts(len(pals), len(items), len(bases)); self._render_save_fields(); self._update_pending_save_label(); self.player_detail_tabs.setEnabled(True); self._set_player_editing_enabled(bool(self.player_center.snapshot.plugin_ready))

    @staticmethod
    def _data_status_text(status: str | None) -> str:
        return {"complete": "完整", "partial": "部分数据未解析", "empty": "无记录"}.get(str(status or ""), "状态未知")

    def _set_player_tab_counts(self, pals: int, items: int, bases: int):
        self.player_detail_tabs.setTabText(self.player_pals_tab_index, f"帕鲁 {pals}")
        self.player_detail_tabs.setTabText(self.player_inventory_tab_index, f"背包 {items}")
        self.player_detail_tabs.setTabText(self.player_relations_tab_index, f"公会与基地 {bases}")

    def _set_player_editing_enabled(self, enabled: bool):
        enabled = bool(enabled and not getattr(self, "player_save_busy", False))
        self.save_fields_table.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        self.stage_inventory_button.setEnabled(enabled)
        self.stage_pal_button.setEnabled(enabled)
        self.preview_save_button.setEnabled(enabled)
        self.apply_save_button.setEnabled(enabled)
        selected_pal = getattr(self, "selected_pal_edit", {})
        pal_editable = bool(enabled and selected_pal.get("individual_id") and selected_pal.get("pal_index") is not None)
        for editor in self.pal_editors.values(): editor.setEnabled(pal_editable)

    def _render_player_relations(self, detail: dict):
        guild = detail.get("guild") or {}; members = list(detail.get("guild_members") or []); bases = list(detail.get("bases") or []); completeness = detail.get("completeness") or {}
        guild_name = guild.get("name") or guild.get("guild_name") or "未加入公会"; guild_id = str(guild.get("guild_id") or ""); admin_uid = str(guild.get("admin_player_uid") or "")
        identity = "会长" if guild.get("is_admin") else "成员" if guild_id else "-"
        self.player_relations_summary.setText(f"公会：{guild_name}　公会 ID：{guild_id or '-'}　当前身份：{identity}　公会基地等级：{guild.get('base_camp_level', '-')}　关系状态：{self._data_status_text(completeness.get('guild'))}\n公会和基地关系当前仅供查看；关系不完整时不会进入存档写回补丁。")
        self.player_guild_members_table.setRowCount(len(members))
        for row, member in enumerate(members):
            member_uid = str(member.get("player_uid") or ""); values = (member.get("nickname") or "未命名成员", member_uid or "-", "会长" if member_uid and member_uid == admin_uid else "成员")
            for column, value in enumerate(values): self.player_guild_members_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.player_bases_table.setRowCount(len(bases))
        for row, base in enumerate(bases):
            position = base.get("position") or {}; coordinate = f"X {position.get('x', 0)} / Y {position.get('y', 0)} / Z {position.get('z', 0)}"
            worker_names = [self.localization.display("pals", worker.get("type")) if worker.get("type") else worker.get("individual_id", "未知帕鲁") for worker in base.get("worker_pals") or []]
            status = "完整" if base.get("data_status") == "complete" else base.get("read_only_reason") or "关系不完整"
            values = (base.get("name") or "未命名基地", base.get("base_id") or "-", coordinate, f"{len(base.get('worker_pal_ids') or [])} 只", base.get("worker_container_id") or "-", f"{len(base.get('container_ids') or [])} 个", status)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setToolTip("\n".join(([f"工作帕鲁：{', '.join(worker_names) or '无'}"] if column == 3 else []) + ([f"关联容器：{', '.join(base.get('container_ids') or []) or '无'}"] if column == 5 else []))); self.player_bases_table.setItem(row, column, cell)

    def _confirm_leave_active_role(self) -> bool:
        current = self._active_edit_session(create=False)
        if not current or not current.changes: return True
        answer = QMessageBox.question(self, "切换玩家", "当前角色有未保存修改。选择“是”保留草稿并切换，选择“否”撤销草稿，选择“取消”留在当前角色。", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if answer == QMessageBox.Cancel: return False
        if answer == QMessageBox.No: current.discard()
        return True

    def _restore_active_player_selection(self):
        for row in range(self.players_table.rowCount()):
            item = self.players_table.item(row, 0); aliases = (item.data(Qt.UserRole) or {}).get("aliases", []) if item else []
            if self.active_player_uid in aliases:
                self.players_table.blockSignals(True); self.players_table.setCurrentCell(row, 0); self.players_table.blockSignals(False); return

    def _role_uid_changed(self, _index=0):
        uid = str(self.player_role_combo.currentData() or "")
        if not uid or uid == self.active_player_uid: return
        aliases = [str(self.player_role_combo.itemData(index)) for index in range(self.player_role_combo.count())]
        self._load_player_role(uid, aliases)

    def _active_edit_session(self, create: bool = True) -> PlayerEditSession | None:
        if not self.selected or not self.active_player_uid: return None
        key = (self.selected.id, self.active_player_uid)
        if create and key not in self.player_edit_sessions: self.player_edit_sessions[key] = PlayerEditSession(*key)
        return self.player_edit_sessions.get(key)

    def _player_document_entry(self):
        if not isinstance(self.save_document, PluginParsedSave): return None, None
        for index, player in enumerate(self.save_document.properties.get("players", [])):
            if str(player.get("player_uid")) == self.active_player_uid: return index, player
        return None, None

    def _show_pal_editor(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        item = self.player_pals_table.item(row, 0) if row >= 0 else None; metadata = item.data(Qt.UserRole) if item else None
        pal = (metadata or {}).get("pal") or {}; stable = bool((metadata or {}).get("stable_id_valid", False)); individual_id = str((metadata or {}).get("individual_id") or "") if stable else ""
        _player_index, document_player = self._player_document_entry(); document_pal = None; pal_index = None
        if document_player and individual_id:
            for index, candidate in enumerate(document_player.get("pals", [])):
                if str(candidate.get("individual_id") or "") == individual_id: document_pal, pal_index = candidate, index; break
        self.selected_pal_edit = {"individual_id": individual_id, "pal_index": pal_index, "pal": document_pal or pal}
        session = self._active_edit_session(create=False)
        for key, editor in self.pal_editors.items():
            path = f"players[{_player_index}].pals[{pal_index}].{key}" if _player_index is not None and pal_index is not None else ""
            value = (document_pal or pal).get(key, ""); value = session.value_for(path, value) if session and path else value
            editor.setText(", ".join(str(item) for item in value) if isinstance(value, list) else ("是" if value is True else "否" if value is False else str(value))); editor.setEnabled(bool(individual_id and document_pal is not None and self.player_center.snapshot.plugin_ready))
        if not pal:
            self.pal_detail_text.setPlainText("选择帕鲁后查看完整存档字段。")
            return
        passive = [self.localization.display("passives", skill) for skill in pal.get("passive_skills") or pal.get("skills") or []]
        active = [self.localization.display("skills", skill) for skill in pal.get("active_skills") or []]
        learned = [self.localization.display("skills", skill) for skill in pal.get("learned_skills") or []]
        self.pal_detail_text.setPlainText(
            f"帕鲁：{self.localization.display('pals', pal.get('type'))}\n昵称：{pal.get('nickname') or '-'}\n"
            f"等级 / 经验：{pal.get('level', 0)} / {pal.get('exp', 0)}\n性别：{self.localization.display('gender', pal.get('gender') or 'Unknown')}\n"
            f"幸运状态：{'幸运帕鲁' if pal.get('is_lucky') else '普通'}\n生命 / 攻击 / 防御个体值：{pal.get('melee', 0)} / {pal.get('ranged', 0)} / {pal.get('defense', 0)}\n"
            f"工作速度：{pal.get('workspeed', 0)}\n星级：{pal.get('rank', 0)}\n攻击 / 防御 / 工作强化：{pal.get('rank_attack', 0)} / {pal.get('rank_defence', 0)} / {pal.get('rank_craftspeed', 0)}\n"
            f"被动技能：{'、'.join(passive) or '无'}\n装备主动技能：{'、'.join(active) or '未解析'}\n已掌握主动技能：{'、'.join(learned) or '未解析'}\n\n"
            f"稳定 InstanceId：{pal.get('individual_id') or '-'}\n数据状态：{'完整' if pal.get('data_status') == 'complete' else pal.get('read_only_reason') or '部分数据未解析'}"
        )

    def stage_selected_pal(self):
        player_index, player = self._player_document_entry(); selected = getattr(self, "selected_pal_edit", {})
        pal_index = selected.get("pal_index"); individual_id = str(selected.get("individual_id") or "")
        if player_index is None or pal_index is None or not individual_id:
            return QMessageBox.warning(self, "帕鲁不可编辑", "该帕鲁没有经过验证的稳定 IndividualId/GUID，已保持只读。")
        pal = player.get("pals", [])[pal_index]; session = self._active_edit_session()
        try:
            for key, editor in self.pal_editors.items():
                path = f"players[{player_index}].pals[{pal_index}].{key}"
                session.stage(path, pal.get(key), editor.text(), resolve_path(path).label, "pal", individual_id, resolve_path(path).risk)
        except Exception as exc: return QMessageBox.warning(self, "帕鲁修改无效", str(exc))
        self._update_pending_save_label(); self.append_log(f"已暂存帕鲁 {individual_id} 的修改")

    def stage_legal_pal_repairs(self):
        player_index, player = self._player_document_entry()
        if player_index is None or not player: return QMessageBox.information(self, "修复非法帕鲁", "请先同步并选择玩家角色。")
        session = self._active_edit_session(); repairs = 0
        limits = {"level": (1, 80), "melee": (0, 100), "ranged": (0, 100), "defense": (0, 100), "rank": (1, 5), "rank_attack": (0, 20), "rank_defence": (0, 20), "rank_craftspeed": (0, 20)}
        try:
            for pal_index, pal in enumerate(player.get("pals") or []):
                individual_id = str(pal.get("individual_id") or "")
                if not individual_id or not pal.get("stable_id_valid", True): continue
                for key, (minimum, maximum) in limits.items():
                    value = pal.get(key)
                    if not isinstance(value, (int, float)): continue
                    corrected = max(minimum, min(maximum, value))
                    if corrected == value: continue
                    path = f"players[{player_index}].pals[{pal_index}].{key}"; field = resolve_path(path)
                    session.stage(path, value, corrected, field.label, "pal", individual_id, field.risk); repairs += 1
        except Exception as exc: return QMessageBox.warning(self, "修复草稿生成失败", str(exc))
        self._update_pending_save_label(); self._show_pal_editor(self.player_pals_table.currentRow())
        QMessageBox.information(self, "修复草稿已生成", f"已生成 {repairs} 项合法值修复草稿。请使用“预览修改”检查后再保存。" if repairs else "当前玩家没有需要修复的已识别非法 Pal 数值。")

    def _render_inventory_for_active_player(self, items=None):
        if items is not None: self.active_inventory_items = list(items)
        records = list(getattr(self, "active_inventory_items", [])); selected_container = self.inventory_container_filter.currentData() if hasattr(self, "inventory_container_filter") else "all"
        records = [item for item in records if selected_container == "all" or str(item.get("container")) == selected_container]
        self.player_inventory_table.setRowCount(len(records))
        player_index, player = self._player_document_entry(); session = self._active_edit_session(create=False)
        for row, item in enumerate(records):
            container = str(item.get("container") or ""); quantity = item.get("StackCount", 0); document_items = (player.get("items") or {}).get(container, []) if player else []
            item_index = next((index for index, current in enumerate(document_items) if str(current.get("ContainerId") or "") == str(item.get("ContainerId") or "") and int(current.get("SlotIndex") or 0) == int(item.get("SlotIndex") or 0)), None)
            path = f"players[{player_index}].items.{container}[{item_index}].StackCount" if player_index is not None and item_index is not None else ""; quantity = session.value_for(path, quantity) if session and path else quantity
            item_id = str(item.get("ItemId") or "")
            container_name = CONTAINER_LABELS.get(container, self.localization.display("containers", container))
            localized_item = self.localization.display("items", item_id) if item_id else (item.get("ItemName") or "未知物品")
            values = (container_name, item.get("SlotIndex", 0), localized_item, item_id or "-", quantity)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setData(Qt.UserRole, item | {"edit_path": path})
                if column in {0, 2}: cell.setToolTip(f"游戏内部 ID：{container if column == 0 else item_id}")
                self.player_inventory_table.setItem(row, column, cell)

    def _show_inventory_editor(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        item = self.player_inventory_table.item(row, 0) if row >= 0 else None; raw = dict(item.data(Qt.UserRole) or {}) if item else {}
        self.selected_inventory_edit = raw
        container = str(raw.get("container") or ""); item_id = str(raw.get("ItemId") or "")
        self.inventory_selected_label.setText(f"{CONTAINER_LABELS.get(container, self.localization.display('containers', container))} · 槽位 {raw.get('SlotIndex', '-')} · {self.localization.display('items', item_id)}")
        session = self._active_edit_session(create=False); value = session.value_for(str(raw.get("edit_path") or ""), int(raw.get("StackCount") or 0)) if session else int(raw.get("StackCount") or 0); self.inventory_quantity.setValue(int(value))

    def stage_selected_inventory(self):
        raw = getattr(self, "selected_inventory_edit", {}); player_index, player = self._player_document_entry()
        container_name = str(raw.get("container") or ""); container_id = str(raw.get("ContainerId") or ""); slot = int(raw.get("SlotIndex") or 0)
        if player_index is None or not container_id or not container_name:
            return QMessageBox.warning(self, "背包槽位不可编辑", "该槽位缺少真实容器 ID，已保持只读。")
        document_items = (player.get("items") or {}).get(container_name, []); item_index = next((index for index, item in enumerate(document_items) if str(item.get("ContainerId") or "") == container_id and int(item.get("SlotIndex") or 0) == slot), None)
        if item_index is None: return QMessageBox.warning(self, "背包槽位不可编辑", "当前存档中找不到对应的容器和槽位。")
        path = f"players[{player_index}].items.{container_name}[{item_index}].StackCount"; original = document_items[item_index].get("StackCount", 0)
        try: self._active_edit_session().stage(path, original, self.inventory_quantity.value(), "物品数量", "inventory", f"{container_id}:{slot}", "高")
        except Exception as exc: return QMessageBox.warning(self, "背包修改无效", str(exc))
        self._update_pending_save_label(); self.append_log(f"已暂存背包槽位 {container_id}:{slot} 的数量修改")

    def save_player_note(self):
        if not self.selected or not self._selected_player_uid(): return
        self.player_repository.set_note(self.selected.id, self._selected_player_uid(), self.player_note.text().strip())
        self.current_players = self.player_repository.list_players(self.selected.id); self.current_player_groups = self.player_repository.list_identity_groups(self.selected.id); self._render_players(); self.append_log("玩家备注已保存")

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
            path_item = self.save_fields_table.item(row, 0); path = str(path_item.data(Qt.UserRole) or "") if path_item else ""
            if path in wanted:
                self.save_fields_table.item(row, 3).setText(wanted[path])
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
        online_players, self.current_guilds = payload
        if self.selected:
            self.player_repository.overlay_online(self.selected.id, online_players)
            self.current_players = self.player_repository.list_players(self.selected.id)
            self.current_player_groups = self.player_repository.list_identity_groups(self.selected.id)
            self._render_players()
        self._render_guilds(); self.append_log(f"公会快照已刷新：{len(self.current_guilds)} 个公会")

    def _render_guilds(self):
        if not hasattr(self, "guilds_table"): return
        self.guilds_table.setRowCount(len(self.current_guilds))
        for row, guild in enumerate(self.current_guilds):
            for column, value in enumerate((guild.name, guild.guild_id, guild.member_count, guild.online_count, guild.average_level, guild.base_count, guild.pal_count)): self.guilds_table.setItem(row, column, QTableWidgetItem(str(value)))
        self._show_guild_members(self.guilds_table.currentRow(), 0, -1, -1)

    def _render_world_editors(self):
        if not hasattr(self, "world_guild_combo"): return
        payload = self.save_document.properties if isinstance(self.save_document, PluginParsedSave) else {}
        guilds = list(payload.get("guilds") or []); bases = list(payload.get("bases") or [])
        current_guild = self.world_guild_combo.currentData(); current_base = self.world_base_combo.currentData()
        self.world_guild_combo.blockSignals(True); self.world_guild_combo.clear()
        for index, guild in enumerate(guilds): self.world_guild_combo.addItem(f"{guild.get('name') or '未命名公会'} · {guild.get('guild_id') or '-'}", index)
        self.world_guild_combo.blockSignals(False)
        self.world_base_combo.blockSignals(True); self.world_base_combo.clear()
        for index, base in enumerate(bases): self.world_base_combo.addItem(f"{base.get('name') or '未命名基地'} · {base.get('base_id') or '-'}", index)
        self.world_base_combo.blockSignals(False)
        if current_guild is not None and self.world_guild_combo.findData(current_guild) >= 0: self.world_guild_combo.setCurrentIndex(self.world_guild_combo.findData(current_guild))
        if current_base is not None and self.world_base_combo.findData(current_base) >= 0: self.world_base_combo.setCurrentIndex(self.world_base_combo.findData(current_base))
        self._show_world_guild_editor(); self._show_world_base_editor(); self._update_world_edit_status()

    def _show_world_guild_editor(self, *_args):
        if not isinstance(self.save_document, PluginParsedSave): return
        index = self.world_guild_combo.currentData(); guilds = self.save_document.properties.get("guilds") or []
        if index is None or not 0 <= int(index) < len(guilds): return
        guild = guilds[int(index)]; session = self.world_edit_session
        name_path = f"guilds[{index}].name"; level_path = f"guilds[{index}].base_camp_level"
        self.world_guild_name.setText(str(session.value_for(name_path, guild.get("name") or "") if session else guild.get("name") or ""))
        self.world_guild_level.setValue(int(session.value_for(level_path, guild.get("base_camp_level") or 1) if session else guild.get("base_camp_level") or 1))

    def _show_world_base_editor(self, *_args):
        if not isinstance(self.save_document, PluginParsedSave): return
        index = self.world_base_combo.currentData(); bases = self.save_document.properties.get("bases") or []
        if index is None or not 0 <= int(index) < len(bases): return
        base = bases[int(index)]; position = base.get("position") or {}; session = self.world_edit_session
        self.world_base_name.setText(str(session.value_for(f"bases[{index}].name", base.get("name") or "") if session else base.get("name") or ""))
        for axis, editor in (("x", self.world_base_x), ("y", self.world_base_y), ("z", self.world_base_z)):
            path = f"bases[{index}].position.{axis}"; value = session.value_for(path, position.get(axis, 0)) if session else position.get(axis, 0); editor.setText(str(value))

    def stage_world_guild(self):
        if not isinstance(self.save_document, PluginParsedSave) or not self.world_edit_session: return QMessageBox.information(self, "公会修改", "请先同步完整存档。")
        index = self.world_guild_combo.currentData(); guilds = self.save_document.properties.get("guilds") or []
        if index is None or not 0 <= int(index) < len(guilds): return
        guild = guilds[int(index)]; guild_id = str(guild.get("guild_id") or "")
        if not guild_id: return QMessageBox.warning(self, "公会不可编辑", "所选公会缺少稳定 ID。")
        try:
            for key, value in (("name", self.world_guild_name.text()), ("base_camp_level", self.world_guild_level.value())):
                path = f"guilds[{index}].{key}"; field = resolve_path(path); self.world_edit_session.stage(path, guild.get(key), value, field.label, "guild", guild_id, field.risk)
        except Exception as exc: return QMessageBox.warning(self, "公会修改无效", str(exc))
        self._update_world_edit_status()

    def stage_world_base(self):
        if not isinstance(self.save_document, PluginParsedSave) or not self.world_edit_session: return QMessageBox.information(self, "基地修改", "请先同步完整存档。")
        index = self.world_base_combo.currentData(); bases = self.save_document.properties.get("bases") or []
        if index is None or not 0 <= int(index) < len(bases): return
        base = bases[int(index)]; base_id = str(base.get("base_id") or ""); position = base.get("position") or {}
        if not base_id: return QMessageBox.warning(self, "基地不可编辑", "所选基地缺少稳定 ID。")
        try:
            values = (("name", base.get("name"), self.world_base_name.text()), ("position.x", position.get("x", 0), self.world_base_x.text()), ("position.y", position.get("y", 0), self.world_base_y.text()), ("position.z", position.get("z", 0), self.world_base_z.text()))
            for key, original, value in values:
                path = f"bases[{index}].{key}"; field = resolve_path(path); self.world_edit_session.stage(path, original, value, field.label, "base", base_id, field.risk)
        except Exception as exc: return QMessageBox.warning(self, "基地修改无效", str(exc))
        self._update_world_edit_status()

    def _update_world_edit_status(self):
        count = len(self.world_edit_session.changes) if self.world_edit_session else 0
        self.world_edit_status.setText(f"世界修改草稿 {count} 项" if isinstance(self.save_document, PluginParsedSave) else "尚未同步结构化存档")

    def preview_world_changes(self):
        changes = self.world_edit_session.preview() if self.world_edit_session else []
        QMessageBox.information(self, "世界修改预览", "\n".join(changes[:40]) if changes else "当前没有世界修改草稿。")

    def revert_world_changes(self):
        if self.world_edit_session: self.world_edit_session.discard()
        self._show_world_guild_editor(); self._show_world_base_editor(); self._update_world_edit_status()

    def apply_world_changes(self):
        session = self.world_edit_session
        if not self.selected or not isinstance(self.save_document, PluginParsedSave) or not session or not session.changes: return QMessageBox.information(self, "世界修改", "没有需要保存的世界修改草稿。")
        name, ok = QInputDialog.getText(self, "高风险世界操作", f"将修改 {len(session.changes)} 个公会或基地字段。请输入实例名称“{self.selected.name}”确认：")
        if not ok or name != self.selected.name: return
        reason, ok = QInputDialog.getText(self, "操作原因", "请输入本次世界修改原因：")
        if not ok or not reason.strip(): return
        selected = self.selected; service = SaveGameService(); self.player_save_busy = True; self.navigation.setEnabled(False)
        def mutate(document): session.apply(document)
        def run(_signals):
            from .management import SaveTransaction
            lifecycle = self._remote_lifecycle() if selected.kind == "remote" else (self.lifecycle or LocalServerLifecycle(selected, self.ui_signals.log.emit))
            if selected.kind == "remote":
                backup_root = self._backup_destination(selected); client = self._remote_client()
                return SaveTransaction(service).execute_remote(client, self.save_remote_path, backup_root, mutate, lifecycle.stop, lifecycle.start, lambda: self._remote_health_ok(selected), lambda: BackupService().create_remote(client, selected, backup_root, selected.install_dir))
            backup_root = self._backup_destination(selected)
            return SaveTransaction(service).execute_local(Path(self.save_remote_path), backup_root, mutate, [], lifecycle.stop, lifecycle.start, lambda: lifecycle.status() == "running", lambda: BackupService().create_local(selected, backup_root))
        worker = Worker(run, with_signals=True); worker.signals.finished.connect(lambda backup: self._world_save_done(backup, reason)); worker.signals.error.connect(self._save_apply_failed); self.pool.start(worker)

    def _world_save_done(self, backup, reason):
        self.world_edit_session = PlayerEditSession(self.selected.id, "__world__") if self.selected else None
        self.player_save_busy = False; self.navigation.setEnabled(True); AuditService.record(self.selected, "公会与基地存档修改", str(self.save_remote_path), detail=reason); self.storage.save_instances(self.instances); self._render_audit(); self.append_log(f"公会与基地修改完成，回滚备份：{backup}"); self.load_save_snapshot()

    def _show_guild_members(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        self.guild_members.setPlainText("\n".join(self.current_guilds[row].members) if 0 <= row < len(self.current_guilds) else "")

    def _mod_manager(self) -> ModManager:
        return ModManager(self.storage.root / "mod-cache")

    def _navigation_page_changed(self, row: int):
        if hasattr(self, "mods_page") and self.page_stack.widget(row) is self.mods_page and not hasattr(self, "workshop_catalog_page"):
            self.load_workshop_catalog()

    def load_workshop_catalog(self, force: bool = False):
        if not hasattr(self, "workshop_table"): return
        query = self.workshop_search.text().strip(); sort = self.workshop_sort.currentData() or "trend"; page = getattr(getattr(self, "workshop_catalog_page", None), "page", 1)
        self.workshop_cache_label.setText("正在加载 Steam Workshop…")
        service = WorkshopCatalogService(self.storage.root / "mod-cache" / "catalog")
        self.run_async(lambda: service.fetch(query, sort, page, force), self._workshop_catalog_loaded)

    def search_workshop_catalog(self):
        self.workshop_catalog_page = WorkshopCatalogPage((), 1, self.workshop_search.text().strip(), self.workshop_sort.currentData() or "trend")
        self.load_workshop_catalog(force=True)

    def change_workshop_page(self, delta: int):
        current = getattr(getattr(self, "workshop_catalog_page", None), "page", 1); target = max(1, current + delta)
        self.workshop_catalog_page = WorkshopCatalogPage((), target, self.workshop_search.text().strip(), self.workshop_sort.currentData() or "trend")
        self.load_workshop_catalog()

    def _workshop_catalog_loaded(self, page: WorkshopCatalogPage):
        self.workshop_catalog_page = page; installed = {mod.workshop_id for mod in self._stored_mods() if mod.workshop_id}
        self.workshop_table.setRowCount(len(page.items))
        for row, item in enumerate(page.items):
            values = (item.title, item.author or "-", item.workshop_id, "已安装" if item.workshop_id in installed else "未安装", "安装时校验 Info.json")
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setData(Qt.UserRole, item.workshop_id); self.workshop_table.setItem(row, column, cell)
        self.workshop_page_label.setText(f"第 {page.page} 页"); self.workshop_cache_label.setText(("缓存目录" if page.from_cache else "在线目录") + f" · {page.fetched_at or '刚刚'} · {len(page.items)} 项")
        if page.items: self.workshop_table.setCurrentCell(0, 0)

    def _selected_workshop_item(self):
        row = self.workshop_table.currentRow()
        if row < 0 or not hasattr(self, "workshop_catalog_page"): return None
        workshop_id = str(self.workshop_table.item(row, 0).data(Qt.UserRole) or "")
        return next((item for item in self.workshop_catalog_page.items if item.workshop_id == workshop_id), None)

    def _show_workshop_detail(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        item = self._selected_workshop_item()
        if not item: self.workshop_preview.setText("选择模组查看详情"); self.workshop_detail.clear(); return
        self.workshop_preview.setText("正在加载封面…" if item.preview_url else item.title)
        self.workshop_detail.setPlainText(f"{item.title}\n作者：{item.author or '-'}\nWorkshop ID：{item.workshop_id}\n来源：Steam Workshop 公共目录\n\n服务器兼容性、PackageName、依赖和冲突将在下载 Info.json 后校验。\n安装目标自动使用当前服务器实例。")
        service = WorkshopCatalogService(self.storage.root / "mod-cache" / "catalog"); self.run_async(lambda: service.fetch_detail(item), self._workshop_detail_loaded)
        if item.preview_url:
            def load_image():
                import urllib.request
                request = urllib.request.Request(item.preview_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=15) as response: return response.read()
            self.run_async(load_image, lambda data, expected=item.workshop_id: self._set_workshop_preview(expected, data))

    def _workshop_detail_loaded(self, item):
        current = self._selected_workshop_item()
        if not current or current.workshop_id != item.workshop_id: return
        self.workshop_detail.setPlainText(f"{item.title}\n作者：{item.author or '-'}\nWorkshop ID：{item.workshop_id}\n更新时间：{item.updated_at or '-'}\n\n{item.description or 'Steam 页面没有提供可读取的简介。'}\n\n服务器兼容性、PackageName、依赖和冲突将在下载 Info.json 后校验。\n安装目标自动使用当前服务器实例。")

    def _set_workshop_preview(self, workshop_id: str, data: bytes):
        current = self._selected_workshop_item()
        if not current or current.workshop_id != workshop_id: return
        pixmap = QPixmap();
        if pixmap.loadFromData(data): self.workshop_preview.setPixmap(pixmap.scaled(220, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else: self.workshop_preview.setText(current.title)

    def install_selected_workshop(self):
        item = self._selected_workshop_item()
        if not item: return QMessageBox.information(self, "创意工坊", "请先选择一个模组")
        self._download_workshop_mod(item.workshop_id)

    def detect_mod_environment(self):
        if not self.selected: return
        selected = self.selected
        if selected.kind == "local":
            environment = ModManager.detect_local(Path(selected.install_dir), platform.system())
            self._mod_environment_done(environment); return
        def detect():
            client = self._remote_client(); profile = dict(selected.remote_profile)
            service = str(profile.get("service_name") or "palworld")
            _code, wine, _error = client.run("command -v wine64 || command -v wine || true")
            _code, version, _error = client.run(f"{self._shell_quote(wine.strip())} --version 2>/dev/null || true") if wine.strip() else (0, "", "")
            _code, command, _error = client.run(f"systemctl show {self._shell_quote(service)} -p ExecStart --value 2>/dev/null || true")
            install = selected.install_dir or str(profile.get("install_dir") or "")
            _code, exe, _error = client.run(f"find {self._shell_quote(install)} -maxdepth 4 -type f -iname PalServer.exe -print -quit 2>/dev/null") if install else (0, "", "")
            exe = exe.strip(); root = exe.rsplit("/", 1)[0] if "/" in exe else install
            mods = f"{root}/Mods/Workshop" if root else ""; settings = f"{root}/Mods/PalModSettings.ini" if root else ""
            writable = False
            if root:
                _code, out, _error = client.run(f"test -w {self._shell_quote(root)} && echo yes || true"); writable = out.strip() == "yes"
            ue4ss_root = f"{root}/UE4SS" if root else ""
            paks = f"{root}/Pal/Content/Paks" if root else ""
            ue4ss_mods = f"{ue4ss_root}/Mods" if ue4ss_root else ""
            native_mods = f"{ue4ss_root}/NativeMods" if ue4ss_root else ""
            profile.update({"wine_path": wine.strip(), "wine_version": version.strip(), "service_exec": command.strip(), "palserver_exe": exe, "mods_dir": mods, "mod_settings_path": settings, "mods_writable": writable, "settings_writable": writable, "workshop_root": mods, "managed_mods_dir": f"{root}/Mods/ManagedMods" if root else "", "ue4ss_root": ue4ss_root, "ue4ss_mods_dir": ue4ss_mods, "native_mods_dir": native_mods, "paks_dir": paks, "ue4ss_config_path": f"{ue4ss_root}/UE4SS-settings.ini" if ue4ss_root else ""})
            return ModManager.detect_remote(profile), profile
        self.run_async(detect, lambda payload: self._mod_environment_done(payload[0], payload[1]))

    def start_wine_migration(self):
        if not self.selected or self.selected.kind != "remote":
            return QMessageBox.information(self, "Wine 迁移", "Wine 迁移仅用于远程原生 Linux 实例。")
        data = self.selected.mod_environment or {}
        if data.get("server_type") == "linux-wine":
            return QMessageBox.information(self, "Wine 迁移", "当前实例已经是 Linux Wine 模式。")
        target, ok = QInputDialog.getText(self, "隔离 Wine 安装目录", "远程绝对路径或 $HOME 相对路径：", text="$HOME/palworld-wine-server")
        if not ok or not target.strip():
            return
        selected = self.selected
        def inspect():
            service = WineMigrationService(self._remote_client(), selected, self.ui_signals.log.emit, self._set_install_progress)
            return service.inspect(target.strip())
        self.run_async(inspect, self._wine_preflight_done)

    def _wine_preflight_done(self, preflight: WineMigrationPreflight):
        if not self.selected:
            return
        self.selected.wine_migration = {"preflight": preflight.to_dict(), "status": "ready" if preflight.ready else "blocked"}
        self.storage.save_instances(self.instances)
        detail = (f"系统：{preflight.distribution} ({preflight.architecture})\n目标目录：{preflight.target_dir}\n"
                  f"Wine：{preflight.wine_path or '缺失'}\nSteamCMD：{preflight.steamcmd_path or '缺失'}\n"
                  f"可用空间：{preflight.free_kb // 1024} MB\n缺少：{'、'.join(preflight.missing) or '无'}")
        if not preflight.ready:
            return QMessageBox.warning(self, "Wine 迁移条件不完整", detail + "\n\n" + "\n".join(preflight.suggestions))
        if QMessageBox.question(self, "准备隔离 Wine 实例", detail + "\n\n下一步将创建完整备份、安装独立 Windows 服务端并在临时端口验证；不会停止原生服务。继续？") != QMessageBox.Yes:
            return
        selected = self.selected
        def prepare(signals):
            backup = BackupService().create_remote(self._remote_client(), selected, self._backup_destination(selected), selected.install_dir)
            signals.log.emit(f"Wine 迁移前备份：{backup}")
            return WineMigrationService(self._remote_client(), selected, signals.log.emit, signals.progress.emit).prepare(preflight), str(backup or "")
        worker = Worker(prepare, with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._wine_prepared); worker.signals.error.connect(lambda error: QMessageBox.critical(self, "Wine 实例准备失败", error)); self.pool.start(worker)

    def _wine_prepared(self, payload):
        if not self.selected:
            return
        migration, backup = payload; migration["backup_path"] = backup
        self.selected.wine_migration = migration; self.storage.save_instances(self.instances)
        typed, ok = QInputDialog.getText(self, "确认切换到 Wine", f"隔离 Wine 服务已在临时端口验证。\n切换会停止原生服务。请输入实例名称“{self.selected.name}”确认：")
        if not ok or typed.strip() != self.selected.name:
            self.append_log("Wine 实例已准备完成，未切换生产服务")
            return
        selected = self.selected
        def activate(signals):
            return WineMigrationService(self._remote_client(), selected, signals.log.emit, signals.progress.emit).activate(migration)
        worker = Worker(activate, with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._wine_activated); worker.signals.error.connect(lambda error: QMessageBox.critical(self, "Wine 切换失败", error)); self.pool.start(worker)

    def _wine_activated(self, migration: dict):
        if not self.selected:
            return
        self.selected.wine_migration = migration
        self.selected.install_dir = str(migration.get("target_dir") or self.selected.install_dir)
        self.selected.remote_profile.update({"install_dir": self.selected.install_dir, "service_name": migration.get("wine_service"), "palserver_exe": f"{self.selected.install_dir}/PalServer.exe"})
        self.storage.save_instances(self.instances); self.append_log("Wine 迁移完成，正在重新检测模组环境"); self.detect_mod_environment()

    def restore_native_server(self):
        if not self.selected or self.selected.kind != "remote" or not self.selected.wine_migration:
            return QMessageBox.information(self, "恢复原生服务", "当前实例没有可恢复的 Wine 迁移记录。")
        if QMessageBox.question(self, "恢复原生 Linux 服务", "将停止 Wine 服务并重新启动原生 Linux 服务。继续？") != QMessageBox.Yes:
            return
        selected = self.selected; migration = dict(selected.wine_migration)
        self.run_async(lambda: WineMigrationService(self._remote_client(), selected, self.ui_signals.log.emit).restore_native(migration), self._native_restored)

    def _native_restored(self, migration: dict):
        if not self.selected:
            return
        self.selected.wine_migration = migration
        preflight = dict(migration.get("preflight") or self.selected.wine_migration.get("preflight") or {})
        source_dir = str(migration.get("source_dir") or preflight.get("source_dir") or "")
        source_service = str(migration.get("source_service") or preflight.get("source_service") or "palworld")
        if source_dir:
            self.selected.install_dir = source_dir; self.selected.remote_profile["install_dir"] = source_dir
        self.selected.remote_profile["service_name"] = source_service
        self.storage.save_instances(self.instances); self.append_log("已恢复原生 Linux 服务"); self.detect_mod_environment()

    def _mod_environment_done(self, environment: ModEnvironment, profile: dict | None = None):
        if not self.selected: return
        self.selected.mod_environment = environment.to_dict(); self.selected.mod_last_sync = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        if profile: self.selected.remote_profile.update(profile)
        self.storage.save_instances(self.instances); self._show_mod_environment(); self._render_mods(); self.append_log("模组环境检测完成：" + (environment.reason or environment.server_type))

    def _show_mod_environment(self):
        if not hasattr(self, "mod_environment_status") or not self.selected: return
        data = self.selected.mod_environment or {}; environment = ModEnvironment(**{key: value for key, value in data.items() if key in ModEnvironment.__dataclass_fields__}) if data else None
        if not environment:
            self.mod_environment_status.setText("尚未检测，请先确认服务端类型和目录"); self.mod_paths_status.setText("Workshop / Mods / 配置路径：-"); return
        mode = {"windows": "Windows Dedicated Server", "linux-native": "原生 Linux", "linux-wine": "Linux Wine（实验）"}.get(environment.server_type, environment.server_type)
        state = "支持 UE4SS 部署" if environment.can_deploy else "只读/待修复"
        if environment.server_type == "linux-native":
            migration = self.selected.wine_migration or {}
            migration_state = {"prepared": "隔离 Wine 实例已准备，可确认切换", "active": "Wine 实例已启用", "blocked": "Wine 迁移条件不完整"}.get(str(migration.get("status") or ""), "可迁移到隔离的 Linux Wine 实例")
            self.mod_environment_status.setText(f"服务器可正常管理 · {mode} 不支持 UE4SS 服务端模组 · {migration_state}")
            self.install_catalog_button.setText("下载并校验模组包")
            for label in ("启用", "禁用", "更新/修复", "移除"):
                if label in self.mod_action_buttons: self.mod_action_buttons[label].setEnabled(False)
        else:
            self.mod_environment_status.setText(f"{mode} · {state} · {environment.reason or '环境检查通过'}" + (f" · Wine {environment.wine_version}" if environment.wine_version else ""))
            self.install_catalog_button.setText("安装到当前服务器")
            for label in ("启用", "禁用", "更新/修复", "移除"):
                if label in self.mod_action_buttons: self.mod_action_buttons[label].setEnabled(environment.can_deploy)
        self.mod_paths_status.setText(f"UE4SS：{environment.ue4ss_root or '-'}\nMods：{environment.ue4ss_mods_dir or '-'}\nNativeMods：{environment.native_mods_dir or '-'}\nUE4SS 配置：{environment.ue4ss_config_path or '-'}\n旧官方目录（仅诊断）：{environment.legacy_mods_dir or '-'}\n可写目录：{len(environment.writable_paths)} 个")

    def _stored_mods(self) -> list[ModManifest]:
        return [ModManifest.from_dict(item) for item in (self.selected.mods if self.selected else [])]

    def _store_mods(self, mods: list[ModManifest]) -> None:
        if not self.selected: return
        self.selected.mods = [mod.to_dict() for mod in mods]; self.storage.save_instances(self.instances); self._render_mods()

    def _render_mods(self):
        if not hasattr(self, "mods_table") or not self.selected: return
        mods = self._stored_mods()
        mod_filter = self.mod_type_filter.currentData() if hasattr(self, "mod_type_filter") else "all"
        if mod_filter != "all":
            mods = [mod for mod in mods if (mod.mod_type or "unknown") == mod_filter]
        self.mods_table.setRowCount(len(mods))
        for row, mod in enumerate(mods):
            state = "已启用" if mod.enabled else "已禁用"
            if mod.legacy_mode or mod.migration_status in {"legacy-readonly", "package-ready"}:
                state = "旧模式/待迁移"
            elif mod.validation_status in {"awaiting_confirmation", "unverified"} or not mod.metadata_complete:
                state = "只读/待确认"
            values = (state, mod.display_name or mod.package_name, mod.package_name, mod.version,
                      {"official": "官方 Mods", "ue4ss": "UE4SS", "native": "NativeMods", "pak": "PAK"}.get(mod.mod_type, "未知"),
                      mod.runtime or "-", "是" if mod.server_supported else "否", "、".join(mod.dependencies) or "-", "、".join(mod.conflicts) or "-",
                      mod.validation_status if mod.validation_status != "unverified" else ("SHA-256 已记录" if mod.sha256 else "元数据不完整"))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, mod.package_name); self.mods_table.setItem(row, column, item)
        enabled = [f"{mod.ue4ss_kind or mod.mod_type}: {mod.package_name}" for mod in mods if mod.enabled]; self.mod_config_preview.setPlainText("\n".join(enabled) or "当前没有已启用的 UE4SS 模组")

    def _selected_mod(self) -> ModManifest | None:
        row = self.mods_table.currentRow(); item = self.mods_table.item(row, 0) if row >= 0 else None; package = str(item.data(Qt.UserRole) or "") if item else ""
        return next((mod for mod in self._stored_mods() if mod.package_name == package), None)

    def _select_mod_package(self, package: str) -> None:
        for row in range(self.mods_table.rowCount()):
            item = self.mods_table.item(row, 0)
            if item and item.data(Qt.UserRole) == package:
                self.mods_table.setCurrentCell(row, 0); return

    def _show_mod_detail(self, row, _column=0, _previous_row=-1, _previous_column=-1):
        mod = self._selected_mod()
        if not mod: self.mod_detail_title.setText("选择模组查看详情"); self.mod_detail_text.clear(); return
        self.mod_detail_title.setText(mod.display_name or mod.package_name)
        plan_path = mod.install_path or "尚未部署"
        environment_data = self.selected.mod_environment if self.selected else {}
        if environment_data:
            try:
                env = ModEnvironment(**{key: value for key, value in environment_data.items() if key in ModEnvironment.__dataclass_fields__})
                plan = ModManager.build_install_plan(mod, env, allow_unverified=True)
                plan_path = plan.target or plan.reason or plan_path
            except Exception:
                pass
        self.mod_detail_text.setPlainText(f"PackageName：{mod.package_name}\n显示名称：{mod.display_name or '-'}\n模组类型：{'官方 Mods' if mod.mod_type == 'official' else mod.mod_type}\n运行环境：{mod.runtime or '-'}\n作者：{mod.author or '-'}\n版本：{mod.version}\n来源：{mod.source}\n来源地址：{mod.source_url or '-'}\nWorkshop ID：{mod.workshop_id or '-'}\n服务器兼容：{'是' if mod.server_supported else '未确认'}\n需要 UE4SS：{'是' if mod.requires_ue4ss else '否'}\n安装规则：{', '.join(mod.install_rules) or '-'}\n依赖：{', '.join(mod.dependencies) or '-'}\n冲突：{', '.join(mod.conflicts) or '-'}\nSHA-256：{mod.sha256 or '-'}\n验证状态：{mod.validation_status}\n目标路径：{plan_path}\n最近操作：{mod.last_operation or '-'} {mod.last_operation_at}\n风险等级：{mod.risk}\n\n说明：没有 Info.json 的 PAK 只能在人工确认后进入高级部署；原生 Linux Dedicated Server 不执行服务端模组安装。")

    def import_url_mod(self):
        value, ok = QInputDialog.getText(self, "导入 URL/GitHub 模组", "ZIP/TAR/PAK 下载地址：")
        if not ok or not value.strip():
            return
        service = ModPackageService(self.storage.root / "mod-cache" / "downloads")
        self.run_async(lambda: service.prepare_url(value.strip()), lambda manifest: self._url_mod_imported(manifest))

    def _url_mod_imported(self, manifest: ModManifest):
        self._add_imported_mod(manifest)
        QMessageBox.information(self, "已导入，等待确认", f"{manifest.display_name or manifest.package_name} 已完成下载与校验。\n来源：{manifest.source_url}\nSHA-256：{manifest.sha256}\n当前保持只读，需在详情中确认风险后启用。")
        self.mod_views.setCurrentIndex(1); self._select_mod_package(manifest.package_name)

    def import_directory_mod(self):
        path = QFileDialog.getExistingDirectory(self, "导入服务端模组目录")
        if not path:
            return
        try:
            manifest = ModPackageService(self.storage.root / "mod-cache" / "downloads").prepare_directory(path)
            self._add_imported_mod(manifest)
            if not manifest.metadata_complete:
                QMessageBox.warning(self, "目录已导入但保持只读", "目录中未找到 Info.json，无法自动确认服务器兼容性和安装目录。")
            self.mod_views.setCurrentIndex(1); self._select_mod_package(manifest.package_name)
        except Exception as exc:
            QMessageBox.critical(self, "目录导入失败", str(exc))

    def _add_imported_mod(self, manifest: ModManifest):
        mods = self._stored_mods(); existing = next((index for index, mod in enumerate(mods) if mod.package_name == manifest.package_name), None)
        if existing is None: mods.append(manifest)
        else: manifest.enabled = mods[existing].enabled; manifest.install_path = mods[existing].install_path; mods[existing] = manifest
        self._store_mods(mods); self.append_log(f"模组已导入清单：{manifest.display_name}（默认不启用）")

    def import_zip_mod(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入服务端模组 ZIP", "", "ZIP 模组包 (*.zip)")
        if not path: return
        try: self._add_imported_mod(LocalArchiveProvider().prepare(path, self.storage.root / "mod-cache" / "archives"))
        except Exception as exc: QMessageBox.critical(self, "模组导入失败", str(exc))

    def import_pak_mod(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 PAK", "", "PAK 文件 (*.pak)")
        if not path: return
        try:
            manifest = LocalPakProvider().prepare(path, self.storage.root / "mod-cache" / "archives"); self._add_imported_mod(manifest)
            QMessageBox.warning(self, "PAK 已导入但保持禁用", "该 PAK 没有 Info.json，元数据和服务器兼容性无法确认。程序不会自动启用。")
        except Exception as exc: QMessageBox.critical(self, "PAK 导入失败", str(exc))

    def import_workshop_mod(self):
        value, ok = QInputDialog.getText(self, "Workshop 模组", "Workshop ID 或链接：")
        if not ok or not value.strip(): return
        self._download_workshop_mod(value)

    def _download_workshop_mod(self, value: str):
        if not self.selected: return
        data = self.selected.mod_environment or {}
        if not data: return QMessageBox.warning(self, "需要检测", "请先检测当前服务器的模组环境")
        environment = ModEnvironment(**{key: item for key, item in data.items() if key in ModEnvironment.__dataclass_fields__})
        selected = self.selected
        def prepare(signals):
            if selected.kind == "remote":
                profile = selected.remote_profile or {}
                steamcmd = str(profile.get("steamcmd_path") or "")
                if not steamcmd:
                    raise RuntimeError("远程检测未找到 SteamCMD，请先重新检测远程主机")
                signals.progress.emit(TaskProgress(15, "下载 Workshop 模组", "正在远程主机使用 SteamCMD 下载", True))
                return WorkshopProvider(Path("steamcmd")).prepare_remote(self._remote_client(), value, self.storage.root / "mod-cache", steamcmd)
            helper_root = Path(selected.install_dir)
            state = LocalSteamCmdManager().prepare(helper_root, signals.log.emit, signals.progress.emit)
            return WorkshopProvider(Path(state.executable)).prepare(value, self.storage.root / "mod-cache")
        worker = Worker(prepare, with_signals=True); worker.signals.log.connect(self.append_log); worker.signals.progress.connect(self._set_install_progress); worker.signals.finished.connect(self._workshop_manifest_prepared); worker.signals.error.connect(lambda error: QMessageBox.critical(self, "Workshop 下载失败", error)); self.pool.start(worker)

    def _workshop_manifest_prepared(self, manifest: ModManifest):
        existing = next((mod for mod in self._stored_mods() if mod.package_name == manifest.package_name), None)
        if existing: manifest.enabled, manifest.install_path = existing.enabled, existing.install_path
        self._add_imported_mod(manifest); self.mod_views.setCurrentIndex(1); self._select_mod_package(manifest.package_name)
        data = self.selected.mod_environment if self.selected else {}
        if data.get("server_type") == "linux-native":
            QMessageBox.information(self, "模组已下载", "模组包已下载并完成清单校验。原生 Linux 不支持部署，可在完成 Wine 迁移后启用。")
            return
        self.enable_selected_mod()

    def enable_selected_mod(self):
        mod = self._selected_mod()
        if not mod or not self.selected: return QMessageBox.information(self, "模组", "请先选择模组")
        data = self.selected.mod_environment or {}
        if not data: return QMessageBox.warning(self, "需要检测", "请先检测模组环境")
        environment = ModEnvironment(**{key: value for key, value in data.items() if key in ModEnvironment.__dataclass_fields__})
        if environment.ue4ss_only and mod.mod_type not in {"ue4ss", "native"}:
            return QMessageBox.warning(self, "需要迁移", "当前环境只接受 UE4SS Mods 或 NativeMods。该旧模组已保留，请先生成 UE4SS 迁移包并重新确认。")
        allow_unverified = False
        if mod.validation_status == "awaiting_confirmation":
            confirmation = (f"来源：{mod.source_url or mod.source}\nSHA-256：{mod.sha256 or '未记录'}\n"
                            f"类型：{mod.mod_type}\n目标将由当前服务器环境决定。\n\n"
                            "该第三方包尚未经过发布者身份验证，程序只会复制文件，不会执行包内脚本或安装程序。确认继续？")
            if QMessageBox.warning(self, "确认第三方模组包", confirmation, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            mod.validation_status = "user_confirmed"
        if not mod.metadata_complete and mod.mod_type == "pak":
            if QMessageBox.warning(self, "高风险 PAK", "该 PAK 没有 Info.json，无法确认服务器兼容性。\n你必须明确确认目标目录后才能进入高级部署。继续？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            allow_unverified = True
        try: self._mod_manager().validate_enable(mod, self._stored_mods(), allow_unverified=allow_unverified)
        except Exception as exc: return QMessageBox.warning(self, "无法启用", str(exc))
        if QMessageBox.question(self, "启用模组", f"启用 {mod.display_name} 将停服、备份、部署并重启。继续？") != QMessageBox.Yes: return
        selected = self.selected
        def run():
            manager = self._mod_manager(); mods = self._stored_mods(); lifecycle = self._remote_lifecycle() if selected.kind == "remote" else self.lifecycle
            if not lifecycle: raise RuntimeError("服务器生命周期不可用")
            backup_root = self._backup_destination(selected)
            if selected.kind == "remote":
                BackupService().create_remote(self._remote_client(), selected, backup_root, selected.install_dir)
                return manager.install_remote(mod, environment, mods, self._remote_client(), lifecycle.stop, lifecycle.start, lambda: self._remote_health_ok(selected), allow_unverified=allow_unverified)
            BackupService().create_local(selected, backup_root)
            return manager.install_local(mod, environment, mods, lifecycle.stop, lifecycle.start, lambda: lifecycle.status() == "running", allow_unverified=allow_unverified)
        self.run_async(run, self._mod_enabled)

    def _mod_enabled(self, updated: ModManifest):
        updated.validation_status = "deployed"
        updated.last_operation = "启用/部署成功"
        updated.last_operation_at = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        mods = self._stored_mods(); index = next(i for i, mod in enumerate(mods) if mod.package_name == updated.package_name); mods[index] = updated; self._store_mods(mods); self.append_log(f"模组已启用：{updated.display_name}")

    def generate_ue4ss_migration_package(self):
        if not self.selected: return
        legacy = [mod for mod in self._stored_mods() if mod.mod_type == "official" or mod.legacy_mode or mod.migration_status == "legacy-readonly"]
        if not legacy: return QMessageBox.information(self, "旧模组迁移", "当前实例没有检测到旧官方模组清单。")
        target = self.storage.root / "mod-cache" / "migrations" / f"{self.selected.id}-{__import__('datetime').datetime.now():%Y%m%d-%H%M%S}.zip"; target.parent.mkdir(parents=True, exist_ok=True)
        import zipfile, json
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("migration.json", json.dumps({"format": "palworld-console-ue4ss-migration-v1", "instance_id": self.selected.id, "mods": [mod.to_dict() for mod in legacy]}, ensure_ascii=False, indent=2))
                for mod in legacy:
                    source = Path(mod.archive_path)
                    if source.is_file(): bundle.write(source, f"packages/{mod.package_name}/{source.name}")
            mods = self._stored_mods()
            for mod in mods:
                if mod in legacy: mod.migration_status = "package-ready"; mod.legacy_mode = True
            self._store_mods(mods)
            QMessageBox.information(self, "迁移包已生成", f"已生成 UE4SS 迁移包：\n{target}\n\n包内不自动启用模组，需在 UE4SS 环境中逐个确认。")
        except Exception as exc: QMessageBox.critical(self, "迁移包生成失败", str(exc))

    def disable_selected_mod(self): self._change_selected_mod(False)
    def remove_selected_mod(self): self._change_selected_mod(False, remove=True)

    def repair_selected_mod(self):
        mod = self._selected_mod()
        if not mod: return QMessageBox.information(self, "模组", "请先选择模组")
        if mod.source == "workshop" and mod.workshop_id:
            self._download_workshop_mod(mod.workshop_id); return
        if mod.source == "local-zip":
            path, _ = QFileDialog.getOpenFileName(self, "选择新版服务端模组 ZIP", "", "ZIP 模组包 (*.zip)")
            if not path: return
            try:
                updated = LocalArchiveProvider().prepare(path, self.storage.root / "mod-cache" / "archives")
                if updated.package_name != mod.package_name: return QMessageBox.warning(self, "PackageName 不匹配", "新版 ZIP 的 PackageName 与当前模组不同，已拒绝覆盖。")
                updated.enabled = mod.enabled; updated.install_path = mod.install_path; self._add_imported_mod(updated); self._select_mod_package(updated.package_name)
                if updated.enabled: self.enable_selected_mod()
            except Exception as exc: QMessageBox.critical(self, "模组更新失败", str(exc))
            return
        if not mod.metadata_complete:
            return QMessageBox.warning(self, "无法自动修复", "缺少 Info.json 的 PAK 无法自动确认安装规则，请重新导入并人工核对。")
        self.enable_selected_mod()

    def _change_selected_mod(self, enabled: bool, remove: bool = False):
        mod = self._selected_mod()
        if not mod or not self.selected: return
        action = "移除" if remove else "禁用"
        if QMessageBox.question(self, action + "模组", f"{action} {mod.display_name} 将停服、备份并重启。继续？") != QMessageBox.Yes: return
        data = self.selected.mod_environment or {}; environment = ModEnvironment(**{key: value for key, value in data.items() if key in ModEnvironment.__dataclass_fields__}); selected = self.selected
        def run():
            manager = self._mod_manager(); mods = self._stored_mods(); lifecycle = self._remote_lifecycle() if selected.kind == "remote" else self.lifecycle
            if not lifecycle: raise RuntimeError("服务器生命周期不可用")
            backup_root = self._backup_destination(selected)
            if selected.kind == "remote":
                BackupService().create_remote(self._remote_client(), selected, backup_root, selected.install_dir); manager.change_remote(mod, environment, mods, self._remote_client(), remove, lifecycle.stop, lifecycle.start, lambda: self._remote_health_ok(selected))
            else:
                BackupService().create_local(selected, backup_root); manager.change_local(mod, environment, mods, remove, lifecycle.stop, lifecycle.start, lambda: lifecycle.status() == "running")
            return mod.package_name
        self.run_async(run, lambda package: self._mod_changed(package, remove))

    def _mod_changed(self, package: str, remove: bool):
        mods = self._stored_mods()
        if remove: mods = [mod for mod in mods if mod.package_name != package]
        else:
            for mod in mods:
                if mod.package_name == package: mod.enabled = False
        self._store_mods(mods); self.append_log(("模组已移除：" if remove else "模组已禁用：") + package)

    def rollback_last_mod_change(self):
        if not self.selected: return
        if self.selected.kind == "remote":
            return QMessageBox.information(self, "远程模组回滚", "远程 Wine 模式请在“备份与恢复”页面恢复最近一次完整服务器备份；不会自动猜测远程 /tmp 回滚包。")
        data = self.selected.mod_environment or {}
        if not data: return QMessageBox.warning(self, "需要检测", "请先检测模组环境")
        environment = ModEnvironment(**{key: value for key, value in data.items() if key in ModEnvironment.__dataclass_fields__}); lifecycle = self.lifecycle
        if not lifecycle: return QMessageBox.warning(self, "无法回滚", "服务器生命周期不可用")
        if QMessageBox.question(self, "回滚模组变更", "将停服并恢复最近一次本机模组事务的文件与启用清单。继续？") != QMessageBox.Yes: return
        def done(states):
            mods = self._stored_mods()
            for mod in mods: mod.enabled = bool(states.get(mod.package_name, False))
            self._store_mods(mods); self.append_log("已回滚最近一次本机模组变更")
        self.run_async(lambda: self._mod_manager().rollback_latest_local(environment, lifecycle.stop, lifecycle.start, lambda: lifecycle.status() == "running"), done)

    def export_mod_manifest(self):
        if not self.selected: return
        target, _ = QFileDialog.getSaveFileName(self, "导出模组清单", f"{self.selected.name}-mods.json", "JSON (*.json)")
        if target: Path(target).write_text(__import__("json").dumps(self.selected.mods, ensure_ascii=False, indent=2), encoding="utf-8")

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
                self.current_players = self.player_repository.list_players(self.selected.id); self.current_player_groups = self.player_repository.list_identity_groups(self.selected.id); self._render_players()
            except Exception as exc:
                self.player_repository.finish_sync(run_id, "failed", detail=str(exc)); raise
        self.save_path_label.setText(f"已载入：{remote_path} · {len(self.save_scalar_values)} 个结构化字段")
        self.player_sync_label.setText("存档已同步")
        if self.selected:
            self.player_center.complete_sync(self.selected.id, list(self.save_document.properties.get("players", [])) if isinstance(self.save_document, PluginParsedSave) else [], [asdict(player) for player in self.current_players], remote_path, isinstance(self.save_document, PluginParsedSave))
            self.player_detail_tabs.setEnabled(True); self._set_player_editing_enabled(isinstance(self.save_document, PluginParsedSave))
            self.player_detail_sync_label.setText(f"同步于 {self.player_center.snapshot.synced_at}")
            if self.active_player_uid and self.player_view_stack.currentWidget() is self.player_detail_page:
                aliases = [str(self.player_role_combo.itemData(index)) for index in range(self.player_role_combo.count())]
                self._load_player_role(self.active_player_uid, aliases)
        self._render_save_fields(); self._render_world_editors(); self.append_log("已读取服务器当前存档用于玩家中心展示，未执行写回操作")

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
        session = self._active_edit_session(create=False); selected_uid = self.active_player_uid or self._selected_player_uid()
        selected_index = None
        if isinstance(self.save_document, PluginParsedSave) and selected_uid:
            selected_index = next((index for index, player in enumerate(self.save_document.properties.get("players", [])) if str(player.get("player_uid")) == selected_uid), None)
        prefix = f"players[{selected_index}]" if selected_index is not None else ""
        rows = []
        for path, value in self.save_scalar_values.items():
            if prefix and not (path == prefix or path.startswith(prefix + ".")):
                continue
            if selected_uid and not prefix:
                continue
            info = display_field(path, value)
            edit = str(session.value_for(path, value) if session else value)
            changed = edit != str(value)
            scoped = scope == "all" or info["object_type"] == scope
            searchable = f"{info['object']} {info['label']} {value}".lower()
            if scoped and (not query or query in searchable) and (not self.save_changed_only.isChecked() or changed): rows.append((path, value, edit, info))
        self.save_fields_table.blockSignals(True)
        self.save_fields_table.setRowCount(len(rows))
        for row, (path, value, edit, info) in enumerate(rows):
            values = (info["object"], info["label"], value, edit, info["source"], info["status"], info["risk"])
            for column, cell_value in enumerate(values):
                item = QTableWidgetItem(str(cell_value)); item.setToolTip(info["tooltip"]); item.setData(Qt.UserRole, path)
                if column != 3 or not info["definition"].writable: item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.save_fields_table.setItem(row, column, item)
        self.save_fields_table.blockSignals(False)
        self._save_edit_changed()

    def _save_edit_changed(self, *_args):
        self._update_pending_save_label()

    def _update_pending_save_label(self):
        if not hasattr(self, "save_fields_table"): return
        paths = set((self._active_edit_session(create=False).changes if self._active_edit_session(create=False) else {}).keys())
        for row in range(self.save_fields_table.rowCount()):
            identity, current, edited = self.save_fields_table.item(row, 0), self.save_fields_table.item(row, 2), self.save_fields_table.item(row, 3)
            if identity and current and edited and current.text() != edited.text(): paths.add(str(identity.data(Qt.UserRole) or ""))
        if hasattr(self, "pending_save_label"): self.pending_save_label.setText(f"未保存修改 {len(paths)} 项")

    def _collect_attribute_table_changes(self) -> PlayerEditSession | None:
        session = self._active_edit_session()
        if not session: return None
        for row in range(self.save_fields_table.rowCount()):
            identity, label, current, edited, risk = self.save_fields_table.item(row, 0), self.save_fields_table.item(row, 1), self.save_fields_table.item(row, 2), self.save_fields_table.item(row, 3), self.save_fields_table.item(row, 6)
            if not all((identity, label, current, edited)): continue
            path = str(identity.data(Qt.UserRole) or "")
            if current.text() == edited.text():
                if path in session.changes and session.changes[path].object_type == "player": session.discard(path)
                continue
            original = self.save_scalar_values.get(path)
            session.stage(path, original, self._coerce_save_value(edited.text(), original), label.text(), "player", self.active_player_uid, risk.text() if risk else "中")
        self._update_pending_save_label(); return session

    def preview_save_changes(self):
        try: session = self._collect_attribute_table_changes()
        except Exception as exc: return QMessageBox.warning(self, "修改值无效", str(exc))
        changes = session.preview() if session else []
        QMessageBox.information(self, "存档修改预览", "\n".join(changes[:30]) if changes else "当前没有未保存修改。")

    def revert_save_changes(self):
        session = self._active_edit_session(create=False)
        if session: session.discard()
        self._render_save_fields(); self._show_pal_editor(self.player_pals_table.currentRow()); self._show_inventory_editor(self.player_inventory_table.currentRow()); self._update_pending_save_label()

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
        if getattr(self, "player_save_busy", False): return
        try: session = self._collect_attribute_table_changes()
        except Exception as exc: return QMessageBox.warning(self, "字段值无效", str(exc))
        if not session or not session.changes: return QMessageBox.information(self, "存档", "没有需要写回的修改")
        name, ok = QInputDialog.getText(self, "高风险存档操作", f"将修改角色 {session.player_uid} 的 {len(session.changes)} 个字段。请输入实例名称“{self.selected.name}”确认：")
        if not ok or name != self.selected.name: return
        reason, ok = QInputDialog.getText(self, "操作原因", "请输入本次修改原因：")
        if not ok or not reason.strip(): return
        QMessageBox.information(self, "存档事务", "即将保存世界、停止服务、创建双重备份并验证写回。任务完成前请勿关闭程序。")
        selected = self.selected; service = SaveGameService(); session_key = (selected.id, session.player_uid)
        self.player_save_busy = True; self.player_detail_tabs.setEnabled(False); self.player_sync_button.setEnabled(False); self.navigation.setEnabled(False)
        def mutate(document): session.apply(document)
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
        worker = Worker(run, with_signals=True); worker.signals.finished.connect(lambda backup: self._save_apply_done(backup, reason, session_key)); worker.signals.error.connect(self._save_apply_failed); self.pool.start(worker)

    def _remote_health_ok(self, selected):
        snapshot = ServerDiagnostics.collect_remote(self._remote_client(), selected, None)
        return snapshot.service_state.lower() in {"active", "running"} and snapshot.pid > 0 and bool(snapshot.game_endpoint and snapshot.game_endpoint.listening)

    def _save_apply_done(self, backup, reason, session_key=None):
        if session_key:
            self.player_edit_sessions.pop(session_key, None); self.player_center.mark_saved(*session_key)
        if hasattr(self, "retry_save_button"): self.retry_save_button.setVisible(False)
        self.player_save_busy = False; self.navigation.setEnabled(True); self.player_sync_button.setEnabled(True)
        AuditService.record(self.selected, "高级存档修改", str(self.save_remote_path), detail=reason); self.storage.save_instances(self.instances); self._render_audit(); self.append_log(f"存档修改完成，回滚备份：{backup}"); self.load_save_snapshot()

    def _save_apply_failed(self, error):
        self.player_center.mark_save_failure(error)
        if hasattr(self, "retry_save_button"): self.retry_save_button.setVisible(True)
        self.player_save_busy = False; self.navigation.setEnabled(True); self.player_sync_button.setEnabled(True); self.player_detail_tabs.setEnabled(bool(self.player_center.snapshot.synced)); self._set_player_editing_enabled(bool(self.player_center.snapshot.plugin_ready))
        self.append_log(f"存档事务失败，草稿已保留，可重试：{error}")
        QMessageBox.critical(self, "存档事务失败", f"{error}\n\n当前修改草稿仍保留，可在重新同步确认基线后重试。")

    def _refresh_plm_plugin_status(self):
        if not hasattr(self, "plm_plugin_status"): return
        ready, detail = PlmCodecPlugin(self.storage.root).probe()
        self.plm_plugin_status.setText(("可用" if ready else "只读") + f" · 固定提交 {PALWORLD_SAVE_TOOLS_COMMIT[:12]} · {detail}")

    def _refresh_localization_status(self):
        if not hasattr(self, "localization_status"):
            return
        catalog = self.localization.catalog
        counts = sum(len(values) for values in catalog.entries.values())
        source = "内置与本地缓存" if catalog.build_id == "builtin" else f"游戏/导入资源 {catalog.build_id}"
        self.localization_status.setText(f"中文资源：{source} · {counts} 条映射")

    def detect_localization_source(self):
        path = self.localization.detect_palworld_client()
        if path:
            QMessageBox.information(self, "检测到 Palworld 客户端", f"客户端目录：{path}\n\n程序只读取本地化资源，不修改或上传游戏文件。若已有提取后的 JSON 目录，可点击“导入中文资源”。")
        else:
            QMessageBox.information(self, "未检测到客户端", "没有在本机 Steam 库中检测到 Palworld 客户端。当前继续使用内置及已缓存中文词典，也可手动导入提取后的中文资源目录。")

    def import_localization_source(self):
        path = QFileDialog.getExistingDirectory(self, "选择中文资源目录（pal.json / items.json 等）")
        if not path:
            return
        try:
            catalog = self.localization.import_asset_directory(Path(path))
        except Exception as exc:
            return QMessageBox.critical(self, "中文资源导入失败", str(exc))
        self._refresh_localization_status()
        if self.active_player_uid:
            self._load_player_role(self.active_player_uid, [str(self.player_role_combo.itemData(index)) for index in range(self.player_role_combo.count())])
        QMessageBox.information(self, "中文资源已更新", f"版本：{catalog.build_id}\n帕鲁：{len(catalog.entries.get('pals', {}))}\n物品：{len(catalog.entries.get('items', {}))}")

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
                    run_script = "" if getattr(sys, "frozen", False) else str(Path(__file__).resolve().parent.parent / "run.py")
                    args = HostTaskDeployer.windows_task_arguments(selected.id, task, sys.executable, run_script)
                    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
                    if result.returncode: raise RuntimeError(result.stderr or result.stdout or "创建 Windows 任务计划失败")
                return len(enabled)
            client = self._remote_client(); helper_tmp = f"/tmp/palworld-task-{selected.id}.py"; client.upload_text(helper_tmp, HostTaskDeployer.remote_helper())
            code, output, error = client.run(f"sudo -n install -d -m 700 /usr/local/lib/palworld-console /etc/palworld-console && sudo -n install -m 700 {self._shell_quote(helper_tmp)} /usr/local/lib/palworld-console/task.py && rm -f {self._shell_quote(helper_tmp)}")
            if code: raise RuntimeError(error.strip() or output.strip())
            for task in enabled:
                config = {"action": task.action, "install_dir": selected.install_dir, "service": selected.remote_profile.get("service_name") or "palworld", "config_path": selected.remote_profile.get("config_path") or "", "rest_port": selected.remote_profile.get("rest_port") or 8212, "steamcmd": selected.remote_profile.get("steamcmd_path") or "steamcmd", "backup_dir": f"{selected.install_dir}/_backups/palworld-console", "retention": task.retention, "message": task.payload.get("message", "服务器计划通知"), "allowed": [item.player_uid for item in WhitelistService.normalize(selected.whitelist) if item.enabled], "policy": selected.whitelist_policy}
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

    def refresh_backup_list(self, preferred_path: Path | None = None):
        if not hasattr(self, "backup_table") or not self.selected: return
        selected_path = preferred_path.resolve() if preferred_path else self._selected_backup_path()
        records = self._backup_repository().list(); self.backup_records = records
        self.backup_table.setSortingEnabled(False); self.backup_table.setRowCount(len(records))
        type_labels = {"world": "世界导出", "disaster": "完整灾备", "restore-point": "恢复点"}
        for row, record in enumerate(records):
            path, manifest = Path(record["path"]), record.get("manifest")
            state = "受保护" if record.get("protected") else ("可用" if record["status"] == "通过" else "异常")
            values = (
                state, type_labels.get(manifest.backup_type, manifest.backup_type) if manifest else "旧格式",
                manifest.source_instance_name if manifest else "未知", manifest.world_id or "-" if manifest else "-",
                manifest.game_version or "未知" if manifest else "未知", ", ".join(manifest.components) if manifest else "待转换",
                f"{record['size_bytes'] / 1024 / 1024:.1f} MB", manifest.created_at.replace("T", " ")[:19] if manifest else "未知",
                record["status"], record.get("note") or "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, str(path)); self.backup_table.setItem(row, column, item)
        self.backup_table.setSortingEnabled(True)
        if records:
            selected_row = next((row for row, record in enumerate(records) if selected_path and Path(record["path"]) == selected_path), 0)
            self.backup_table.selectRow(selected_row)
        else: self.backup_details.clear()

    def show_backup_details(self):
        path = self._selected_backup_path()
        if not path or not hasattr(self, "backup_details"): return
        record = next((item for item in getattr(self, "backup_records", []) if Path(item["path"]) == path), None)
        if not record: return
        manifest = record.get("manifest")
        if not manifest:
            self.backup_details.setPlainText(f"旧格式备份：{path.name}\n状态：{record['status']}\n\n恢复前必须转换为统一 .pwcbackup 并完成 CRC、路径与 SHA-256 校验。")
            return
        entries = "\n".join(f"- {entry.component}: {entry.path} ({entry.size_bytes} bytes)" for entry in manifest.entries[:40])
        if len(manifest.entries) > 40: entries += f"\n... 另有 {len(manifest.entries) - 40} 个文件"
        text = (
            f"文件：{path.name}\n包 ID：{manifest.package_id}\n来源平台：{manifest.source_platform}\n来源实例：{manifest.source_instance_name} ({manifest.source_instance_id})\n"
            f"世界 ID：{manifest.world_id or '未知'}\n游戏版本：{manifest.game_version or '未知'}\n存档格式：{manifest.save_format}\n玩家数量：{manifest.player_count}\n"
            f"可恢复组件：{', '.join(manifest.components)}\n配置脱敏：{', '.join(manifest.redacted_fields) or '不含配置'}\n完整性：{'信息不完整' if manifest.incomplete else '完整'}\n"
            f"校验：{record['status']}\n备注：{record.get('note') or '-'}\n\n文件清单：\n{entries}"
        )
        self.backup_details.setPlainText(text)

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
        self.path_box.setEnabled(not remote)
        self.user_edit.setEnabled(remote); self.ssh_port_spin.setEnabled(remote); self.auth_combo.setEnabled(remote); self.ssh_password_edit.setEnabled(remote and self.auth_combo.currentData() == "password"); self.key_path_edit.setEnabled(remote and self.auth_combo.currentData() == "key"); self.key_passphrase_edit.setEnabled(remote and self.auth_combo.currentData() == "key")
        detected = bool(self.selected and self.selected.discovery_status == "ready")
        for widget in (self.port_spin, self.rest_edit, self.rest_user_edit, self.admin_password_box, self.public_edit): widget.setVisible(not remote or detected)
        self.discover_btn.setVisible(remote)

    def _remote_client(self):
        password = self.storage.get_secret(self.selected.ssh_secret_ref) if self.selected.ssh_auth_type == "password" else ""
        phrase = self.storage.get_secret(self.selected.ssh_key_passphrase_ref)
        return RemoteHostClient(self.selected.host, self.selected.remote_username, password, self.selected.ssh_port, self.selected.ssh_key_path if self.selected.ssh_auth_type == "key" else "", phrase)

    def _remote_lifecycle(self): return _remote_lifecycle_for(self.selected, self._remote_client(), self.ui_signals.log.emit)

    def discover_remote(self):
        self.save_instance()
        if not self.selected or self.selected.kind != "remote": return
        client = self._remote_client()
        selected = self.selected
        worker = Worker(lambda signals: RemoteServerInspector(client, signals.log.emit, selected.install_dir, selected.id).discover(), with_signals=True)
        worker.signals.log.connect(self.append_log)
        worker.signals.finished.connect(self._discovery_done)
        worker.signals.error.connect(self._discovery_failed)
        self.pool.start(worker)

    def _discovery_done(self, profile):
        self._apply_discovery_profile(profile)
        self.append_log("SSH 连接和服务器检测完成")
        self.refresh_status()
        if profile.get("installed"):
            selected = self.selected; repository = self._backup_repository(selected); known = tuple(selected.remote_profile.get("scheduled_backups_imported") or ())
            worker = Worker(lambda: (selected.id, repository.import_remote_scheduled(self._remote_client(), selected, known)))
            worker.signals.finished.connect(self._scheduled_backups_synced)
            worker.signals.error.connect(lambda error: self.append_log(f"计划备份同步跳过：{error}"))
            self.pool.start(worker)
        if profile.get("config_path"):
            client = self._remote_client(); selected = self.selected
            worker = Worker(lambda signals: ServerConfigBootstrap.read_remote(client, selected), with_signals=True)
            worker.signals.finished.connect(lambda result: (self._apply_config_result(result), self.append_log("游戏配置已自动回填")))
            worker.signals.error.connect(lambda e: self.append_log(f"配置自动回填失败：{e}"))
            self.pool.start(worker)

    def _scheduled_backups_synced(self, payload):
        instance_id, names = payload
        instance = next((item for item in self.instances if item.id == instance_id), None)
        if not instance or not names: return
        existing = list(instance.remote_profile.get("scheduled_backups_imported") or ())
        instance.remote_profile["scheduled_backups_imported"] = (existing + list(names))[-200:]
        self.storage.save_instances(self.instances)
        self.append_log(f"已下载并校验 {len(names)} 个服务器计划备份")
        if self.selected and self.selected.id == instance_id: self.refresh_backup_list()

    def _apply_discovery_profile(self, profile):
        self.selected.remote_profile = profile; self.selected.discovery_status = "ready" if profile.get("platform") in {"linux", "windows"} else "unknown"
        from datetime import datetime
        self.selected.discovered_at = datetime.now().isoformat(timespec="seconds")
        self.selected.install_dir = str(profile.get("install_dir", self.selected.install_dir)); self.selected.game_port = int(profile.get("game_port", self.selected.game_port)); self.selected.rest_url = str(profile.get("rest_url", self.selected.rest_url)); self.lifecycle = self._remote_lifecycle() if self.selected.discovery_status == "ready" else None; self.storage.save_instances(self.instances); self.path_edit.setText(self.selected.install_dir); self.port_spin.setValue(self.selected.game_port); self.rest_edit.setText(self.selected.rest_url); self._show_discovery(); self._toggle_remote_fields()

    def _discovery_failed(self, error):
        self.selected.discovery_status = "failed"; self.storage.save_instances(self.instances); self._show_discovery(error); QMessageBox.critical(self, "SSH 检测失败", error)

    def _show_discovery(self, error=""):
        if not self.selected or self.selected.kind != "remote": self.discovery_result.setPlainText(""); return
        if error: self.discovery_result.setPlainText(f"检测失败\n{error}"); return
        profile = self.selected.remote_profile
        if not profile: self.discovery_result.setPlainText("尚未检测。请保存 SSH 信息后点击“连接并检测 SSH”。"); return
        platform_name = str(profile.get("platform") or "linux")
        if platform_name == "unknown":
            self.discovery_result.setPlainText(f"系统: 未知\n部署能力: 已禁用\n诊断: {profile.get('detection_error') or 'Windows 与 Linux 探针均未成功'}\n\n若目标是 Windows Server 且 SSH 尚未启用，请先通过 RDP 或云厂商控制台以管理员 PowerShell 安装并启动 OpenSSH Server。")
            return
        steamcmd = profile.get("steamcmd_path") or ("未安装，部署时将自动安装" if profile.get("steamcmd_installable") else "未安装，等待修复依赖")
        if platform_name == "windows":
            fields = (("平台", "Windows Server / OpenSSH"), ("系统", profile.get("os")), ("版本", profile.get("version")), ("架构", profile.get("architecture")), ("PowerShell", profile.get("powershell_version")), ("管理员权限", "可用" if profile.get("elevated") else "缺失，安装前需修复"), ("固定磁盘", profile.get("disk")), ("SteamCMD", steamcmd), ("WinSW", profile.get("winsw_path") or "未安装，部署时将自动安装并校验"), ("服务端", profile.get("install_dir") or "未部署"), ("Windows 服务", f"{profile.get('service_name') or '未找到'} / {profile.get('service_state')}"), ("配置", profile.get("config_path") or "未找到"), ("网络", f"游戏 UDP {profile.get('game_port', 8211)}；REST {profile.get('rest_port', 8212)} 仅 SSH 隧道"))
        else:
            fields = (("平台", "Linux / SSH"), ("系统", profile.get("os")), ("架构", profile.get("architecture")), ("磁盘", profile.get("disk")), ("sudo", "可用" if profile.get("sudo") else "不可免交互使用"), ("SteamCMD", steamcmd), ("SteamCMD 来源", profile.get("steamcmd_source") or "未知"), ("服务端", profile.get("install_dir") or "未部署"), ("systemd", f"{profile.get('service_name') or '未找到'} / {profile.get('service_state')}"), ("配置", profile.get("config_path") or "未找到"), ("REST", "仅通过 SSH 隧道访问" if profile.get("rest_enabled") else "未启用"))
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
