import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from palworld_console.models import ConfigSyncResult, PlayerRecord, ServerInstance, TaskProgress, UninstallResult
from palworld_console.backup_packages import BackupPackageService, RestoreTransaction
import palworld_console.ui as ui_module


class MemoryStorage:
    def __init__(self):
        self.instances = [ServerInstance()]
        self.secrets = {}
        self.root = Path.cwd() / ".test-storage"

    def load_instances(self):
        return self.instances

    def save_instances(self, instances):
        self.instances = instances

    def get_secret(self, _reference):
        return self.secrets.get(_reference, "")

    def set_secret(self, reference, value):
        self.secrets[reference] = value
        return reference

    def delete_secret(self, reference):
        self.secrets.pop(reference, None)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(ui_module, "AppStorage", MemoryStorage)
    main_window = ui_module.MainWindow()
    yield main_window
    main_window.close()


def test_progress_widget_supports_indeterminate_and_determinate_states(window):
    window._set_install_progress(TaskProgress(20, "下载服务端", "等待 SteamCMD", True))
    assert window.install_progress.minimum() == 0
    assert window.install_progress.maximum() == 0
    assert window.install_percent.text() == "处理中"

    window._set_install_progress(TaskProgress(64, "下载服务端", "已下载 64%"))
    assert window.install_progress.maximum() == 100
    assert window.install_progress.value() == 64
    assert window.install_percent.text() == "64%"


def test_install_task_locks_and_restores_instance_controls(window, monkeypatch):
    window.selected = None
    window._begin_install_task("正在安装…")
    assert window.install_task_active is True
    assert window.install_button.isEnabled() is False
    assert window.instance_list.isEnabled() is False
    assert window.delete_button.isEnabled() is False
    assert window.uninstall_button.isEnabled() is False

    window._install_succeeded("安装完成")
    assert window.install_task_active is False
    assert window.install_progress.value() == 100
    assert window.install_button.isEnabled() is True
    assert window.instance_list.isEnabled() is True
    assert window.uninstall_button.isEnabled() is True

    monkeypatch.setattr(QMessageBox, "critical", lambda *args: QMessageBox.Ok)
    window._begin_install_task("正在更新…")
    window._set_install_progress(TaskProgress(42, "下载服务端", "下载中"))
    window._install_failed("网络连接中断")
    assert window.install_task_active is False
    assert window.install_progress.value() == 42
    assert window.install_stage.text() == "安装失败"
    assert window.install_button.isEnabled() is True


def test_backup_page_uses_selected_verified_package_and_details(window, tmp_path):
    window.storage.root = tmp_path / "storage"
    install = tmp_path / "server"
    world = install / "Pal" / "Saved" / "SaveGames" / "0" / "WORLD-UI"
    players = world / "Players"; players.mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"save")
    (players / "uid.sav").write_bytes(b"player")
    window.selected.install_dir = str(install)
    package = BackupPackageService().create(window.selected, install / "Pal" / "Saved", window._backup_repository().root, "world")

    window.refresh_backup_list()

    assert window.backup_table.columnCount() == 10
    assert window.backup_table.rowCount() == 1
    assert window._selected_backup_path() == package
    assert "WORLD-UI" in window.backup_details.toPlainText()
    assert "可恢复组件：world" in window.backup_details.toPlainText()


def test_restore_dialog_requires_advanced_confirmation_for_incomplete_package(window, tmp_path, monkeypatch):
    level = tmp_path / "Level.sav"; level.write_bytes(b"not-a-supported-save")
    package = BackupPackageService().import_source(level, window.selected, tmp_path / "packages")
    plan = RestoreTransaction().plan(package, window.selected)
    manifest = BackupPackageService().validate(package)
    dialog = ui_module.RestoreOptionsDialog(plan, manifest, window)
    assert dialog.advanced.isVisible() is False or plan.requires_advanced_confirmation is True
    assert plan.blocked_reason
    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is False
    dialog.close()


def test_restore_dialog_single_player_is_mutually_exclusive_with_world(window, tmp_path):
    saved = tmp_path / "server" / "Pal" / "Saved"; world = saved / "SaveGames" / "0" / "WORLD"
    (world / "Players").mkdir(parents=True); (world / "Level.sav").write_bytes(b"save"); (world / "Players" / "42.sav").write_bytes(b"player")
    package = BackupPackageService().create(window.selected, saved, tmp_path / "packages")
    manifest = BackupPackageService().validate(package); plan = RestoreTransaction().plan(package, window.selected)
    dialog = ui_module.RestoreOptionsDialog(plan, manifest, window, ("42",), (True, "ready"))
    assert dialog.world.isChecked()
    dialog.player.setChecked(True)
    assert dialog.world.isChecked() is False
    assert dialog.selected_components() == ("player",)
    assert dialog.selected_player_uid() == "42"
    dialog.world.setChecked(True)
    assert dialog.player.isChecked() is False
    dialog.close()


def test_duplicate_install_is_rejected(window, monkeypatch):
    calls = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: calls.append(args) or QMessageBox.Ok)
    window.install_task_active = True
    window.update_server()
    assert len(calls) == 1


def test_remote_install_reports_recheck_failure(window, monkeypatch):
    class Signal:
        def __init__(self):
            self.values = []

        def emit(self, value):
            self.values.append(value)

    class Lifecycle:
        def __init__(self, *_args):
            pass

        def install(self):
            return None

        def configure_service(self):
            return None

        def start(self):
            return None

        def wait_for_game_listener(self):
            return None

    class Inspector:
        def __init__(self, *_args):
            pass

        def discover(self):
            raise RuntimeError("SSH 已断开")

    monkeypatch.setattr(ui_module, "RemoteServerLifecycle", Lifecycle)
    monkeypatch.setattr(ui_module, "RemoteServerInspector", Inspector)
    monkeypatch.setattr(ui_module.ServerConfigBootstrap, "ensure_remote", lambda *_args: object())
    signals = SimpleNamespace(log=Signal(), progress=Signal())

    with pytest.raises(RuntimeError, match="状态复检失败"):
        window._run_remote_install(signals, ServerInstance(kind="remote"), object(), "secret")
    assert signals.progress.values[-1].stage == "重新检测"


def test_remote_install_generates_config_before_service_start(window, monkeypatch):
    order = []

    class Signal:
        def emit(self, _value): pass

    class Lifecycle:
        def __init__(self, *_args): pass
        def install(self): order.append("install")
        def configure_service(self): order.append("service")
        def start(self): order.append("start")
        def wait_for_game_listener(self): order.append("verify")

    class Inspector:
        def __init__(self, *_args): pass
        def discover(self): order.append("discover"); return {"installed": True}

    def ensure_remote(*_args):
        order.append("config")
        return ConfigSyncResult({}, "/config", "自动生成", True, "now")

    monkeypatch.setattr(ui_module, "RemoteServerLifecycle", Lifecycle)
    monkeypatch.setattr(ui_module, "RemoteServerInspector", Inspector)
    monkeypatch.setattr(ui_module.ServerConfigBootstrap, "ensure_remote", ensure_remote)
    signals = SimpleNamespace(log=Signal(), progress=Signal())

    window._run_remote_install(signals, ServerInstance(kind="remote"), object(), "secret")
    assert order == ["install", "config", "service", "start", "verify", "discover"]


def test_config_result_fills_gui_and_stores_password_reference(window):
    result = ConfigSyncResult(
        {"ServerName": "Detected", "AdminPassword": "secret-value", "PublicPort": 9001, "RESTAPIEnabled": True, "RESTAPIPort": 9002},
        "/server/PalWorldSettings.ini",
        "服务器读取",
        False,
        "2026-08-17T21:30:00",
    )
    window._apply_config_result(result)

    assert window.ini_fields["ServerName"].text() == "Detected"
    assert window.port_spin.value() == 9001
    assert window.rest_edit.text().endswith(":9002")
    assert window.rest_password_edit.text() == "secret-value"
    assert window.selected.admin_secret_ref
    assert "secret-value" not in str(window.selected.to_dict())


def test_uninstall_success_keeps_instance_and_ssh_credentials(window):
    window.selected.kind = "remote"
    window.selected.install_dir = "/home/pal/palworld-server"
    window.selected.ssh_secret_ref = "ssh-ref"
    window.selected.admin_secret_ref = "admin-ref"
    window.storage.secrets.update({"ssh-ref": "ssh-secret", "admin-ref": "admin-secret"})
    window._begin_server_task("uninstall", "正在卸载…", "准备卸载", "检查中")

    result = UninstallResult("/home/pal/palworld-server", "C:/backup/server.tar.gz", True)
    profile = {"installed": False, "install_dir": "/home/pal/palworld-server", "game_port": 8211, "rest_url": ""}
    window._uninstall_succeeded((result, profile))

    assert window.selected in window.instances
    assert window.storage.secrets["ssh-ref"] == "ssh-secret"
    assert "admin-ref" not in window.storage.secrets
    assert window.selected.remote_profile["installed"] is False
    assert window.install_stage.text() == "卸载完成"


def test_main_window_exposes_ten_management_pages(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["仪表盘", "连接与部署", "游戏配置", "玩家中心", "公会与基地", "模组管理", "RCON 与自动化", "备份与恢复", "日志与审计", "关于我们"]
    assert len(window.ini_fields) >= 60
    assert not hasattr(window, "status_timer")


def test_player_center_renders_one_row_for_shared_platform_identity(window):
    window.current_players = [
        PlayerRecord(name="Alice", user_id="steam-1", player_uid="100", level=10),
        PlayerRecord(name="Alice", user_id="steam-1", player_uid="200", level=12),
    ]
    window._render_players()
    assert window.players_table.rowCount() == 1
    identity = window.players_table.item(0, 0).data(ui_module.Qt.UserRole)
    assert set(identity["aliases"]) == {"100", "200"}


def test_config_preset_updates_fields_without_saving(window):
    window.preset_combo.setCurrentText("高倍率")
    window.apply_config_preset()
    assert window._setting_text(window.ini_fields["ExpRate"]) == "5.0"
    assert "已修改" in window.config_diff_label.text()
