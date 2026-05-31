"""GUI 启动入口。

用法：
    python -m src.gui.run
    或
    cd 项目根目录 && python src/gui/run.py
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PyQt6.QtWidgets import QApplication
from src.config import ensure_env_file
from src.gui.main_window import MainWindow


def main():
    ensure_env_file()

    app = QApplication(sys.argv)
    app.setApplicationName("Bilibili ADs Flak")
    app.setStyle("Fusion")   # 跨平台一致的外观

    # 应用图标（任务栏）
    icon_path = _PROJECT_ROOT / "icon.png"
    if icon_path.exists():
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    # Windows 任务栏分组 ID
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BilibiliADsFlak")
    except Exception:
        pass

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
