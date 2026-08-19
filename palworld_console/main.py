import sys
from PySide6.QtWidgets import QApplication
from . import __version__
from .ui import MainWindow


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] in {"--version", "-V"}:
        print(__version__)
        return
    if len(sys.argv) >= 6 and sys.argv[1] == "task-run" and sys.argv[2] == "--instance" and sys.argv[4] == "--task":
        from .task_runner import run_scheduled_task
        raise SystemExit(run_scheduled_task(sys.argv[3], sys.argv[5]))
    app = QApplication(sys.argv)
    app.setApplicationName("Palworld Console")
    window = MainWindow(); window.show()
    sys.exit(app.exec())
