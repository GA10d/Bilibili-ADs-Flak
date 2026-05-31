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

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QThread, QTimer, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from src.config import Config, ENV_FILE, ensure_env_file, resource_path
from src.gui.main_window import MainWindow


class CookieCheckWorker(QThread):
    checked = pyqtSignal(bool, str)

    def run(self):
        config = Config.from_env()
        if (
            config.auth_mode != "cookie"
            or not config.sessdata
            or not config.bili_jct
            or config.sessdata.startswith(("test_", "在此"))
        ):
            self.checked.emit(False, "")
            return

        try:
            import requests
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                cookies={
                    "SESSDATA": config.sessdata,
                    "bili_jct": config.bili_jct,
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            payload = resp.json()
            data = payload.get("data") or {}
            if payload.get("code") == 0 and data.get("isLogin"):
                self.checked.emit(True, data.get("uname") or "用户")
            else:
                self.checked.emit(False, "")
        except Exception:
            self.checked.emit(False, "")


class LogoSpinner(QWidget):
    """Logo with a simple loading ring around it."""

    def __init__(self, logo_path: Path, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._span = 95.0
        self._flash_hidden = False
        self._ring_visible = True
        self._logo = QPixmap(str(logo_path)) if logo_path.exists() else QPixmap()
        self.setFixedSize(260, 260)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def _tick(self):
        self._angle = (self._angle + 6) % 360
        self.update()

    def _get_span(self):
        return self._span

    def _set_span(self, value):
        self._span = value
        self.update()

    span = pyqtProperty(float, _get_span, _set_span)

    def close_and_flash(self, on_finished):
        self._timer.stop()
        self._close_animation = QPropertyAnimation(self, b"span", self)
        self._close_animation.setDuration(360)
        self._close_animation.setStartValue(self._span)
        self._close_animation.setEndValue(360.0)
        self._close_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def flash():
            self._flash_hidden = True
            self.update()
            QTimer.singleShot(90, self._show_flash)
            QTimer.singleShot(220, on_finished)

        self._close_animation.finished.connect(flash)
        self._close_animation.start()

    def _show_flash(self):
        self._flash_hidden = False
        self.update()

    def hide_ring(self):
        self._ring_visible = False
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        ring_rect = QRectF(20, 20, 220, 220)
        if self._ring_visible:
            painter.setPen(QPen(QColor("#EEF2F7"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring_rect, 0, 360 * 16)

        if self._ring_visible and not self._flash_hidden:
            painter.setPen(QPen(QColor("#FF6B98"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring_rect, self._angle * 16, int(self._span * 16))

        if self._logo.isNull():
            return
        logo_size = QSize(150, 150)
        scaled = self._logo.scaled(
            logo_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)


class StatusIcon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._complete = False
        self.setFixedSize(24, 24)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def _tick(self):
        if not self._complete:
            self._angle = (self._angle + 8) % 360
            self.update()

    def set_loading(self):
        self._complete = False
        self._timer.start(16)
        self.update()

    def set_complete(self):
        self._complete = True
        self._timer.stop()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._complete:
            painter.setBrush(QColor("#22C55E"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(0, 0, 24, 24)
            painter.setPen(QPen(QColor("white"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(7, 12, 11, 16)
            painter.drawLine(11, 16, 18, 8)
            return

        rect = QRectF(3, 3, 18, 18)
        painter.setPen(QPen(QColor("#E5E7EB"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 0, 360 * 16)
        painter.setPen(QPen(QColor("#FF6B98"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, self._angle * 16, 105 * 16)


class StatusLine(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon = StatusIcon()
        self.label = QLabel("")
        self.label.setObjectName("statusText")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.icon)
        layout.addWidget(self.label)

    def set_loading(self, text: str):
        self.icon.set_loading()
        self.label.setText(text)

    def set_complete(self, text: str):
        self.icon.set_complete()
        self.label.setText(text)


class SplashWindow(QWidget):
    def __init__(self, logo_path: Path):
        super().__init__()
        self.setWindowTitle("Bilibili ADs Flak")
        self.resize(760, 520)
        self.setMinimumSize(720, 420)
        self.setStyleSheet("""
            QWidget {
                background: white;
                color: #111827;
                font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
            }
            QLabel#credit {
                color: #6B7280;
                font-size: 13px;
            }
            QLabel#statusText {
                color: #111827;
                font-size: 17px;
                font-weight: 600;
            }
        """)
        if logo_path.exists():
            self.setWindowIcon(QIcon(str(logo_path)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 28)
        layout.addStretch(1)

        self.stage = QWidget()
        self.stage.setMinimumHeight(270)
        self.stage.setStyleSheet("background: transparent;")

        self.spinner = LogoSpinner(logo_path, self.stage)

        self.status_line = StatusLine(self.stage)
        self.status_effect = QGraphicsOpacityEffect(self.status_line)
        self.status_effect.setOpacity(0.0)
        self.status_line.setGraphicsEffect(self.status_effect)
        self.status_line.setVisible(False)
        layout.addWidget(self.stage)

        layout.addStretch(1)
        credit = QLabel("@Developed by Zhewen Guo from Columbia University")
        credit.setObjectName("credit")
        credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(credit)

        self._animations = []
        self._logo_has_moved = False
        QTimer.singleShot(0, self._position_logo_center)

    def reveal_status(self, text: str):
        self.spinner.hide_ring()
        self.status_line.set_loading(text)
        self.status_line.resize(self.status_line.sizeHint())
        self.status_line.setVisible(True)
        self.status_effect.setOpacity(0.0)

        logo_start = self.spinner.pos()
        logo_end = self._logo_left_pos()
        status_end = self._status_pos(logo_end)
        status_start = QPoint(status_end.x() + 42, status_end.y())
        self.status_line.move(status_start)

        logo_move = QPropertyAnimation(self.spinner, b"pos")
        logo_move.setDuration(520)
        logo_move.setStartValue(logo_start)
        logo_move.setEndValue(logo_end)
        logo_move.setEasingCurve(QEasingCurve.Type.InOutCubic)

        status_move = QPropertyAnimation(self.status_line, b"pos")
        status_move.setDuration(520)
        status_move.setStartValue(status_start)
        status_move.setEndValue(status_end)
        status_move.setEasingCurve(QEasingCurve.Type.OutCubic)

        status_fade = QPropertyAnimation(self.status_effect, b"opacity")
        status_fade.setDuration(420)
        status_fade.setStartValue(0.0)
        status_fade.setEndValue(1.0)
        status_fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        logo_move.finished.connect(lambda: setattr(self, "_logo_has_moved", True))
        for animation in (logo_move, status_move, status_fade):
            self._keep_animation(animation)
            animation.start()

    def set_status_loading(self, text: str):
        self.status_line.set_loading(text)

    def set_status_complete(self, text: str):
        self.status_line.set_complete(text)

    def transition_status(self, text: str, after_fade_in=None):
        fade_out = QPropertyAnimation(self.status_effect, b"opacity")
        fade_out.setDuration(220)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def fade_in_next():
            self.status_line.set_loading(text)
            fade_in = QPropertyAnimation(self.status_effect, b"opacity")
            fade_in.setDuration(260)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.Type.InOutCubic)
            if after_fade_in:
                fade_in.finished.connect(after_fade_in)
            self._keep_animation(fade_in)
            fade_in.start()

        fade_out.finished.connect(fade_in_next)
        self._keep_animation(fade_out)
        fade_out.start()

    def _keep_animation(self, animation):
        self._animations.append(animation)
        animation.finished.connect(lambda: self._animations.remove(animation) if animation in self._animations else None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "stage"):
            return
        if self._logo_has_moved:
            logo_pos = self._logo_left_pos()
            self.spinner.move(logo_pos)
            self.status_line.move(self._status_pos(logo_pos))
        else:
            self._position_logo_center()

    def _position_logo_center(self):
        if not hasattr(self, "stage"):
            return
        x = (self.stage.width() - self.spinner.width()) // 2
        y = (self.stage.height() - self.spinner.height()) // 2
        self.spinner.move(max(0, x), max(0, y))

    def _logo_left_pos(self) -> QPoint:
        x = max(18, (self.stage.width() - self.spinner.width()) // 2 - 150)
        y = (self.stage.height() - self.spinner.height()) // 2
        return QPoint(x, max(0, y))

    def _status_pos(self, logo_pos: QPoint) -> QPoint:
        x = logo_pos.x() + self.spinner.width() + 34
        y = logo_pos.y() + (self.spinner.height() - self.status_line.height()) // 2
        return QPoint(x, max(0, y))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Bilibili ADs Flak")
    app.setStyle("Fusion")   # 跨平台一致的外观

    # 应用图标（任务栏）
    icon_path = resource_path("icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Windows 任务栏分组 ID
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BilibiliADsFlak")
    except Exception:
        pass

    splash = SplashWindow(icon_path)
    splash.show()

    state = {"window": None, "splash": splash, "animations": [], "cookie_worker": None}

    def show_main_window():
        window = MainWindow()
        state["window"] = window
        window.setWindowOpacity(0.0)
        window.show()

        splash_fade = QPropertyAnimation(splash, b"windowOpacity")
        splash_fade.setDuration(520)
        splash_fade.setStartValue(1.0)
        splash_fade.setEndValue(0.0)
        splash_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        splash_fade.finished.connect(splash.close)

        window_fade = QPropertyAnimation(window, b"windowOpacity")
        window_fade.setDuration(640)
        window_fade.setStartValue(0.0)
        window_fade.setEndValue(1.0)
        window_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)

        state["animations"] = [splash_fade, window_fade]
        splash_fade.start()
        window_fade.start()

    def complete_and_continue(text: str, next_step):
        splash.set_status_complete(text)
        QTimer.singleShot(1000, next_step)

    def check_cookie():
        splash.transition_status("正在检查bilibili cookie", start_cookie_worker)

    def start_cookie_worker():
        worker = CookieCheckWorker()
        state["cookie_worker"] = worker

        def on_cookie_checked(valid: bool, username: str):
            if valid:
                complete_and_continue(f"{username}，欢迎使用ADs Flank", show_main_window)
            else:
                splash.set_status_loading("未找到有效 cookie")
                QTimer.singleShot(1000, show_main_window)
            worker.deleteLater()
            state["cookie_worker"] = None

        worker.checked.connect(on_cookie_checked)
        worker.start()

    def check_env():
        splash.reveal_status("正在检测env配置文件")
        ensure_env_file()
        if ENV_FILE.exists():
            complete_and_continue("找到env文件", check_cookie)

    QTimer.singleShot(3000, lambda: splash.spinner.close_and_flash(check_env))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
