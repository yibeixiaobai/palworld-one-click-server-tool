import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QItemSelectionModel, QThread

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


def test_world_path_is_initialized_and_auto_detected_before_save_sync(window, monkeypatch, tmp_path):
    level = tmp_path / "Saved" / "SaveGames" / "imported-world" / "Level.sav"
    window.save_remote_path = ""
    window.save_working_path = None
    monkeypatch.setattr(window, "_find_save_path", lambda: str(level))

    window.selected.kind = "local"
    assert window._world_local_path() == level.parent.resolve()

    window.selected.kind = "remote"
    window.selected.remote_profile["platform"] = "linux"
    monkeypatch.setattr(window, "_find_save_path", lambda: "/srv/palworld/Pal/Saved/SaveGames/imported-world/Level.sav")
    assert window._world_remote_path() == "/srv/palworld/Pal/Saved/SaveGames/imported-world"


def test_switching_instance_clears_previous_save_paths(window):
    window.save_remote_path = "/old/SaveGames/WORLD/Level.sav"
    window.save_working_path = Path("old/Level.sav")

    window.select_instance(0)

    assert window.save_remote_path == ""
    assert window.save_working_path is None


def test_backup_page_mirrors_task_progress_and_heartbeat(window, monkeypatch):
    clock = iter([100.0, 100.0, 103.0, 112.0, 115.0])
    monkeypatch.setattr(ui_module.time, "monotonic", lambda: next(clock))

    window._begin_backup_task("恢复服务器存档")
    assert window.backup_task_stage.text() == "恢复服务器存档"
    assert window.backup_task_progress.maximum() == 0
    assert window.backup_task_percent.text() == "处理中"

    window._set_install_progress(TaskProgress(35, "构建迁移候选", "正在解析 Level.sav", False))
    assert window.backup_task_progress.maximum() == 100
    assert window.backup_task_progress.value() == 35
    assert window.backup_task_percent.text() == "35%"

    window._backup_task_heartbeat()
    assert "远程操作仍在执行" in window.backup_task_message_label.text()
    assert "最近进度更新" in window.backup_task_elapsed.text()

    window._finish_backup_task()
    assert window.backup_task_progress.value() == 100
    assert window.backup_task_stage.text() == "任务完成"
    assert window.navigation.isEnabled()


def test_save_tools_page_mirrors_migration_progress(window, monkeypatch):
    monkeypatch.setattr(ui_module.time, "monotonic", lambda: 100.0)
    window.navigation.setCurrentRow(window.page_stack.indexOf(window.save_tools_page))

    window._begin_backup_task("准备四人联机迁移")
    window._set_install_progress(TaskProgress(48, "构建迁移候选", "正在校验玩家身份", False))

    assert window.save_tool_progress.value() == 48
    assert window.save_tool_stage.text() == "构建迁移候选"
    assert window.save_tool_percent.text() == "48%"
    assert "玩家身份" in window.save_tool_result.toPlainText()
    window._finish_backup_task()
    assert window.save_tool_stage.text() == "任务完成"


def test_identity_mapping_dialog_keeps_identical_options_and_swaps_claimed_targets(window):
    dialog = ui_module.IdentityMappingDialog(
        BackupPackageService(),
        (
            {"player_guid": "A" * 32, "nickname": "Alice"},
            {"player_guid": "C" * 32, "nickname": "Carol"},
        ),
        (
            {"player_guid": "B" * 32, "nickname": "Bob"},
            {"player_guid": "D" * 32, "nickname": "Dave"},
            {"player_guid": "E" * 32, "nickname": "Eve"},
        ),
        window,
        require_all=True,
    )

    assert dialog.table.rowCount() == 2
    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is False
    first_options = [dialog.combos[0].itemData(i) for i in range(dialog.combos[0].count())]
    second_options = [dialog.combos[1].itemData(i) for i in range(dialog.combos[1].count())]
    assert first_options == second_options == ["", "B" * 32, "D" * 32, "E" * 32]
    dialog.combos[0].setCurrentIndex(dialog.combos[0].findData("B" * 32))
    claimed_index = dialog.combos[1].findData("B" * 32)
    assert claimed_index >= 0
    assert dialog.combos[1].model().item(claimed_index).isEnabled() is True
    assert [dialog.combos[1].itemData(i) for i in range(dialog.combos[1].count())] == first_options

    dialog.combos[1].setCurrentIndex(claimed_index)
    assert dialog.combos[0].currentData() == ""
    assert dialog.combos[1].currentData() == "B" * 32

    dialog.combos[0].setCurrentIndex(dialog.combos[0].findData("D" * 32))
    dialog.combos[1].setCurrentIndex(dialog.combos[1].findData("D" * 32))

    assert dialog.confirmations() == {"A" * 32: "B" * 32, "C" * 32: "D" * 32}
    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is True
    dialog.close()


def test_identity_mapping_dialog_skip_does_not_consume_targets(window):
    dialog = ui_module.IdentityMappingDialog(
        BackupPackageService(),
        (
            {"player_guid": "A" * 32, "nickname": "Alice"},
            {"player_guid": "C" * 32, "nickname": "Carol"},
        ),
        (
            {"player_guid": "B" * 32, "nickname": "Bob"},
            {"player_guid": "D" * 32, "nickname": "Dave"},
        ),
        window,
        skip_option="稍后迁移",
    )

    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is True
    assert dialog.confirmations() == {}
    assert dialog.combos[0].currentText() == "稍后迁移"
    assert dialog.combos[1].findData("B" * 32) >= 0
    assert dialog.combos[1].findData("D" * 32) >= 0
    dialog.close()


def test_identity_mapping_dialog_shows_every_temporary_identity_in_every_row(window):
    dialog = ui_module.IdentityMappingDialog(
        BackupPackageService(),
        (
            {"player_guid": "A" * 32, "nickname": "Alice"},
            {"player_guid": "C" * 32, "nickname": "Carol"},
        ),
        (
            {"player_guid": "A" * 32, "nickname": "Temporary A"},
            {"player_guid": "C" * 32, "nickname": "Temporary C"},
            {"player_guid": "D" * 32, "nickname": "Temporary D"},
        ),
        window,
        require_all=True,
    )

    expected = ["", "A" * 32, "C" * 32, "D" * 32]
    assert [dialog.combos[0].itemData(i) for i in range(dialog.combos[0].count())] == expected
    assert [dialog.combos[1].itemData(i) for i in range(dialog.combos[1].count())] == expected
    assert dialog.combos[0].model().item(dialog.combos[0].findData("A" * 32)).isEnabled() is True
    assert dialog.combos[0].model().item(dialog.combos[0].findData("C" * 32)).isEnabled() is True
    assert dialog.combos[1].model().item(dialog.combos[1].findData("A" * 32)).isEnabled() is True
    assert dialog.combos[1].model().item(dialog.combos[1].findData("C" * 32)).isEnabled() is True
    dialog.combos[0].setCurrentIndex(dialog.combos[0].findData("A" * 32))
    assert dialog.combos[0].currentData() == "A" * 32
    dialog.close()


def test_identity_mapping_dialog_filters_history_pending_invalid_and_self_targets(window):
    dialog = ui_module.IdentityMappingDialog(
        BackupPackageService(),
        (
            {"player_guid": "A" * 32, "nickname": "Pending"},
            {"player_guid": "F" * 32, "nickname": "Finished"},
        ),
        (
            {"player_guid": "B" * 32, "nickname": "Completed"},
            {"player_guid": "A" * 32, "nickname": "Same"},
            {"player_guid": "D" * 32, "nickname": "Available"},
            {"player_guid": "d" * 32, "nickname": "Duplicate"},
            {"player_guid": "", "nickname": "Invalid"},
        ),
        window,
        skip_option="暂不迁移",
        used_guids=("B" * 32,),
        pending_guids=("A" * 32,),
        require_selection=True,
    )

    assert dialog.table.rowCount() == 1
    assert dialog.combos[0].findData("B" * 32) == -1
    own_index = dialog.combos[0].findData("A" * 32)
    assert own_index >= 0
    assert dialog.combos[0].model().item(own_index).isEnabled() is True
    assert [dialog.combos[0].itemData(i) for i in range(dialog.combos[0].count())] == ["", "A" * 32, "D" * 32]
    dialog.combos[0].setCurrentIndex(own_index)
    assert dialog.combos[0].currentData() == "A" * 32
    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is True
    dialog.combos[0].setCurrentIndex(dialog.combos[0].findData("D" * 32))
    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is True
    dialog.close()


def test_identity_mapping_dialog_only_preselects_globally_unique_names(window):
    dialog = ui_module.IdentityMappingDialog(
        BackupPackageService(),
        (
            {"player_guid": "A" * 32, "nickname": "Alice"},
            {"player_guid": "C" * 32, "nickname": "Twin"},
            {"player_guid": "F" * 32, "nickname": "Twin"},
        ),
        (
            {"player_guid": "B" * 32, "nickname": "Alice"},
            {"player_guid": "D" * 32, "nickname": "Twin"},
            {"player_guid": "E" * 32, "nickname": "Twin"},
        ),
        window,
        require_all=True,
    )

    assert dialog.combos[0].currentData() == "B" * 32
    assert dialog.combos[1].currentData() == ""
    assert dialog.combos[2].currentData() == ""
    assert dialog.buttons.button(ui_module.QDialogButtonBox.Ok).isEnabled() is False
    dialog.close()


def test_cancelled_identity_mapping_dialog_does_not_confirm_restore(window, monkeypatch):
    calls = []

    class CancelledDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return ui_module.QDialog.Rejected

        def confirmations(self):
            calls.append("confirmations")
            return {}

    monkeypatch.setattr(ui_module, "IdentityMappingDialog", CancelledDialog)
    monkeypatch.setattr(BackupPackageService, "confirm_restore_mappings", lambda *_args: calls.append("confirm"))
    monkeypatch.setattr(BackupPackageService, "temporary_identity_targets_from_player_center", classmethod(lambda cls, _instance, targets, _root: tuple(targets)))
    session = SimpleNamespace(instance_id="server", source_players=(), placeholder_players=())

    window._restore_migration_prepared((session, "restore-point"), "test")

    assert calls == []


def test_restore_migration_worker_returns_to_main_thread_and_is_released(window, app, monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        window,
        "_restore_migration_prepared",
        lambda prepared, reason: callbacks.append((prepared, reason, QThread.currentThread())),
    )
    worker = ui_module.Worker(lambda _signals: (("session", "restore-point"), "coop"), with_signals=True)
    worker.signals.finished.connect(window._restore_migration_task_done)

    window._start_worker(worker)
    deadline = ui_module.time.monotonic() + 5
    while (not callbacks or window._active_workers) and ui_module.time.monotonic() < deadline:
        app.processEvents()
        QThread.msleep(10)

    assert callbacks == [(('session', 'restore-point'), 'coop', app.thread())]
    assert worker not in window._active_workers


def test_update_check_states_and_automatic_failure(window, monkeypatch):
    messages = []
    logs = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args) or QMessageBox.Ok)
    monkeypatch.setattr(window, "append_log", logs.append)

    window.update_check_active = True
    window.update_button.setEnabled(False)
    window._update_check_done(None, automatic=False)
    assert window.update_check_active is False
    assert window.update_button.isEnabled()
    assert "最新" in window.update_status.text()
    assert messages

    message_count = len(messages)
    window._update_check_failed("offline", automatic=True)
    assert len(messages) == message_count
    assert logs == ["自动更新检查失败：offline"]


def test_installer_start_failure_does_not_quit(window, tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"; installer.write_bytes(b"setup")
    warnings = []
    quit_calls = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args) or QMessageBox.Ok)
    monkeypatch.setattr(ui_module.QProcess, "startDetached", lambda *_args: (False, -1))
    monkeypatch.setattr(ui_module.QApplication, "quit", lambda: quit_calls.append(True))

    window._update_download_done(installer)

    assert warnings
    assert quit_calls == []


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


def test_imported_backup_is_selected_after_refresh(window, tmp_path):
    install = tmp_path / "server"
    saved = install / "Pal" / "Saved"
    world = saved / "SaveGames" / "0" / "WORLD-IMPORT"
    (world / "Players").mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"save")
    source = tmp_path / "upload.zip"
    import zipfile
    with zipfile.ZipFile(source, "w") as archive:
        for path in saved.rglob("*"):
            if path.is_file():
                archive.write(path, Path("Saved") / path.relative_to(saved))
    window.selected.install_dir = str(install)
    imported = window._backup_repository().import_source(source, window.selected)
    window.refresh_backup_list(imported)
    assert window._selected_backup_path() == imported


def test_backup_page_supports_extended_selection_and_preserves_multiple_rows(window, tmp_path):
    window.storage.root = tmp_path / "storage"
    install = tmp_path / "server"
    world = install / "Pal" / "Saved" / "SaveGames" / "0" / "WORLD-BATCH"
    (world / "Players").mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"save")
    first = BackupPackageService().create(window.selected, install / "Pal" / "Saved", window._backup_repository().root, "world")
    second = BackupPackageService().create(window.selected, install / "Pal" / "Saved", window._backup_repository().root, "world")

    window.refresh_backup_list()
    assert window.backup_table.selectionMode() == ui_module.QAbstractItemView.ExtendedSelection
    window.backup_table.clearSelection()
    selection = window.backup_table.selectionModel()
    selection.select(window.backup_table.model().index(0, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    selection.select(window.backup_table.model().index(1, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)

    assert set(window._selected_backup_paths()) == {first.resolve(), second.resolve()}
    assert "已选择 2 个备份" in window.backup_selection_label.text()
    assert "恢复操作必须保持单选" in window.backup_details.toPlainText()


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


def test_main_window_exposes_save_tools_as_an_independent_management_page(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["仪表盘", "连接与部署", "游戏配置", "玩家中心", "公会与基地", "模组管理", "RCON 与自动化", "存档工具", "备份与恢复", "日志与审计", "关于我们"]
    assert window.page_stack.indexOf(window.save_tools_page) != window.page_stack.indexOf(window.backups_page)
    assert hasattr(window, "save_tool_sources")
    assert hasattr(window, "save_tool_engine_status")
    assert not any(button.text() == "本地存档转服务器" for button in window.backup_action_buttons)
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


def test_player_center_uses_list_then_detail_pages_and_preserves_draft(window):
    instance_id = "ui-player-detail"
    window.selected.id = instance_id
    payload = {
        "players": [{
            "player_uid": "200", "nickname": "Alice", "level": 20, "exp": 3000,
            "inventory_status": "complete",
            "inventory_containers": [{"key": key, "count": 1 if key == "CommonContainerId" else 0, "data_status": "complete"} for key in ui_module.CONTAINER_LABELS],
            "pals": [{"individual_id": "pal-1", "type": "SheepBall", "nickname": "棉花", "level": 10, "exp": 500, "gender": "Female", "is_lucky": True, "rank": 2, "melee": 30, "ranged": 40, "defense": 50, "workspeed": 80, "rank_attack": 1, "rank_defence": 2, "rank_craftspeed": 3, "active_skills": ["FireBall"], "passive_skills": ["Lucky"], "data_status": "complete", "stable_id_valid": True}],
            "items": {"CommonContainerId": [{"ContainerId": "bag-1", "SlotIndex": 0, "ItemId": "wood", "StackCount": 12, "data_status": "complete"}]},
        }],
        "guilds": [{"guild_id": "guild-1", "name": "Builders", "admin_player_uid": "200", "base_camp_level": 8, "players": [{"player_uid": "200", "nickname": "Alice"}, {"player_uid": "201", "nickname": "Bob"}], "data_status": "complete"}],
        "bases": [{"base_id": "base-1", "name": "主基地", "guild_id": "guild-1", "position": {"x": 1, "y": 2, "z": 3}, "worker_container_id": "workers-1", "worker_pal_ids": ["pal-1"], "worker_pals": [{"individual_id": "pal-1", "type": "SheepBall"}], "container_ids": ["items-1"], "data_status": "complete"}],
    }
    window.player_repository.upsert_save_snapshot(instance_id, payload)
    window.current_players = window.player_repository.list_players(instance_id)
    window.current_player_groups = window.player_repository.list_identity_groups(instance_id)
    window.player_center.complete_sync(instance_id, payload["players"], [], "Level.sav", True)
    window._render_players()
    assert window.player_view_stack.currentWidget() is window.player_list_page
    assert window.players_table.rowCount() == 1

    window._show_player_detail(0, 0)
    assert window.player_view_stack.currentWidget() is window.player_detail_page
    assert window.active_player_uid == "200"
    assert window.player_pals_table.rowCount() == 1
    assert window.player_inventory_table.rowCount() == 1
    assert window.player_guild_members_table.rowCount() == 2
    assert window.player_bases_table.rowCount() == 1
    assert window.player_detail_tabs.tabText(window.player_pals_tab_index) == "帕鲁 1"
    assert "普通背包 1" in window.inventory_status_label.text()

    session = window._active_edit_session()
    session.stage("players[0].level", 20, 21, "玩家等级", "player", "200", "中")
    window._return_to_player_list()
    assert window.player_view_stack.currentWidget() is window.player_list_page
    assert window.player_center.pending_count(instance_id, "200") == 1
    assert window.players_table.item(0, 6).text() == "1 项"


def test_config_preset_updates_fields_without_saving(window):
    window.preset_combo.setCurrentText("高倍率")
    window.apply_config_preset()
    assert window._setting_text(window.ini_fields["ExpRate"]) == "5.0"
    assert "已修改" in window.config_diff_label.text()


def test_illegal_pal_repair_creates_reviewable_draft(window, tmp_path, monkeypatch):
    payload = {"players": [{"player_uid": "42", "pals": [{"individual_id": "pal-1", "stable_id_valid": True, "level": 99, "melee": 140, "ranged": 50, "defense": -5, "rank": 8, "rank_attack": 30, "rank_defence": 2, "rank_craftspeed": 3}]}]}
    window.selected.id = "repair-instance"; window.active_player_uid = "42"
    window.save_document = ui_module.PluginParsedSave.create(payload, tmp_path / "Level.sav", object())
    messages = []; monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args) or QMessageBox.Ok)

    window.stage_legal_pal_repairs()

    session = window._active_edit_session()
    assert session.value_for("players[0].pals[0].level", 99) == 80
    assert session.value_for("players[0].pals[0].melee", 140) == 100
    assert session.value_for("players[0].pals[0].defense", -5) == 0
    assert session.value_for("players[0].pals[0].rank", 8) == 5
    assert len(session.changes) == 5
    assert messages


def test_world_editors_stage_guild_and_base_draft(window, tmp_path):
    payload = {
        "players": [],
        "guilds": [{"guild_id": "guild-1", "name": "Old Guild", "base_camp_level": 2}],
        "bases": [{"base_id": "base-1", "name": "Old Base", "position": {"x": 1, "y": 2, "z": 3}}],
    }
    window.selected.id = "world-edit-instance"
    window.save_document = ui_module.PluginParsedSave.create(payload, tmp_path / "Level.sav", object())
    window.world_edit_session = ui_module.PlayerEditSession(window.selected.id, "__world__")
    window._render_world_editors()
    window.world_guild_name.setText("New Guild"); window.world_guild_level.setValue(7)
    window.world_base_name.setText("New Base"); window.world_base_x.setText("12.5")

    window.stage_world_guild(); window.stage_world_base()

    assert window.world_edit_session.value_for("guilds[0].name", "") == "New Guild"
    assert window.world_edit_session.value_for("guilds[0].base_camp_level", 0) == 7
    assert window.world_edit_session.value_for("bases[0].name", "") == "New Base"
    assert window.world_edit_session.value_for("bases[0].position.x", 0) == 12.5
    assert "4 项" in window.world_edit_status.text()


def test_save_failure_keeps_world_draft_and_reenables_ui(window, monkeypatch):
    window.world_edit_session = ui_module.PlayerEditSession(window.selected.id, "__world__")
    window.world_edit_session.stage("guilds[0].name", "Before", "After", "公会名称", "guild", "guild-1", "中")
    window.player_save_busy = True; window.navigation.setEnabled(False)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: QMessageBox.Ok)

    window._save_apply_failed("round-trip failed")

    assert len(window.world_edit_session.changes) == 1
    assert window.navigation.isEnabled()
    assert window.player_save_busy is False
