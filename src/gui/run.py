"""GUI 启动入口。

用法：
    python -m src.gui.run
    或
    cd 项目根目录 && python src/gui/run.py
"""

import sys
import time
import asyncio
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QThread, QTimer, QUrl, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PyQt6.QtWidgets import (
    QApplication, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QInputDialog, QVBoxLayout, QWidget,
)
from src.config import Config, ENV_FILE, ensure_env_file, resource_path
from src.gui.main_window import MainWindow

LOGO_IDLE_SIZE = 190
LOGO_OUTRO_SIZE = 260


class CookieCheckWorker(QThread):
    checked = pyqtSignal(bool, str, int)

    def run(self):
        config = Config.from_env()
        if (
            config.auth_mode != "cookie"
            or not config.sessdata
            or not config.bili_jct
            or config.sessdata.startswith(("test_", "在此"))
        ):
            self.checked.emit(False, "", 0)
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
                self.checked.emit(True, data.get("uname") or "用户", int(data.get("mid") or 0))
            else:
                self.checked.emit(False, "", 0)
        except Exception:
            self.checked.emit(False, "", 0)


class SubmissionPreloadWorker(QThread):
    preloaded = pyqtSignal(object)

    def __init__(self, mid: int, parent=None):
        super().__init__(parent)
        self._mid = mid

    def run(self):
        try:
            result = asyncio.run(self._fetch_first_pages())
            self.preloaded.emit(result)
        except Exception as exc:
            self.preloaded.emit({"mid": self._mid, "error": str(exc), "pages": {}, "total_count": 0})

    async def _fetch_first_pages(self) -> dict:
        from bilibili_api import Credential, user
        from src.submission_snapshot import clear_in_progress, load_in_progress, save_in_progress, save_snapshot

        config = Config.from_env()
        credential = None
        if config.sessdata and config.bili_jct:
            credential = Credential(sessdata=config.sessdata, bili_jct=config.bili_jct)

        u = user.User(self._mid, credential=credential)
        page_size = 10
        pages: dict[int, list[dict]] = {}
        all_videos: list[dict] = []
        total_count = 0
        total_known = False

        partial = load_in_progress(self._mid)
        start_page = 1
        if partial.get("videos") and int(partial.get("page_size") or page_size) == page_size:
            all_videos = list(partial.get("videos") or [])
            total_count = int(partial.get("total_count") or 0)
            start_page = max(1, int(partial.get("next_page") or 1))

        async def fetch_with_retry(page: int):
            last_exc = None
            for attempt in range(max(1, config.max_retries) + 1):
                try:
                    return await u.get_videos(pn=page, ps=page_size)
                except Exception as exc:
                    last_exc = exc
                    if attempt >= max(1, config.max_retries):
                        break
                    await asyncio.sleep(0.6 * (attempt + 1))
            raise last_exc

        page = start_page
        total_pages = 5
        if total_count:
            total_pages = max(1, (total_count + page_size - 1) // page_size)
        while page <= total_pages:
            if self.isInterruptionRequested():
                return {
                    "mid": self._mid,
                    "pages": {},
                    "total_count": total_count,
                    "total_known": total_known,
                    "requested_pages": [],
                    "page_size": page_size,
                    "cancelled": True,
                }
            resp = await fetch_with_retry(page)
            page_info = resp.get("page") or {}
            if page_info.get("count") is not None:
                total_count = int(page_info.get("count") or 0)
                total_known = True
                total_pages = max(1, (total_count + page_size - 1) // page_size)
            videos = ((resp.get("list") or {}).get("vlist") or [])
            pages[page] = videos
            all_videos.extend(videos)
            save_in_progress(self._mid, all_videos, page + 1, page_size, total_count)
            await asyncio.sleep(0.25)
            page += 1

        snapshot = save_snapshot(self._mid, all_videos)
        clear_in_progress(self._mid)
        annotated_map = {video["bvid"]: video for video in snapshot["videos"]}
        for page, videos in pages.items():
            pages[page] = [annotated_map.get(video.get("bvid"), video) for video in videos]
        if start_page > 1:
            pages = {}
            for idx, offset in enumerate(range(0, min(len(snapshot["videos"]), 50), page_size), start=1):
                pages[idx] = snapshot["videos"][offset:offset + page_size]

        return {
            "mid": self._mid,
            "pages": pages,
            "total_count": total_count,
            "total_known": total_known,
            "requested_pages": list(pages.keys()),
            "page_size": page_size,
            "snapshot_at": snapshot["snapshot_at"],
            "previous_snapshot_at": snapshot["previous_snapshot_at"],
        }


class CookieImportWorker(QThread):
    imported = pyqtSignal(bool, str)

    def run(self):
        try:
            from src.cookie_importer import import_cookies, save_to_env
            cookies = import_cookies()
            save_to_env(cookies)
            self.imported.emit(True, "")
        except Exception as exc:
            self.imported.emit(False, str(exc))


class DeepSeekApiKeyCheckWorker(QThread):
    checked = pyqtSignal(bool, str)

    def run(self):
        config = Config.from_env()
        key = (config.deepseek_api_key or "").strip()
        if not key or key.startswith(("sk-xxx", "在此")):
            self.checked.emit(False, "未填写 DeepSeek API Key")
            return

        try:
            import requests
            resp = requests.get(
                f"{config.deepseek_base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                self.checked.emit(True, "")
            else:
                self.checked.emit(False, f"API Key 不可用（HTTP {resp.status_code}）")
        except Exception as exc:
            self.checked.emit(False, str(exc))


class LogoSpinner(QWidget):
    """Logo with a simple loading ring around it."""

    def __init__(self, logo_path: Path, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._span = 95.0
        self._flash_hidden = False
        self._ring_visible = True
        self._logo_size_value = LOGO_IDLE_SIZE
        self._logo = QPixmap(str(logo_path)) if logo_path.exists() else QPixmap()
        self._logo_image: QImage | None = None
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

    def set_logo_size(self, size: int):
        self._logo_size_value = size
        self.update()

    def _get_logo_size_value(self):
        return self._logo_size_value

    def _set_logo_size_value(self, value):
        self._logo_size_value = value
        self.update()

    logo_size_value = pyqtProperty(float, _get_logo_size_value, _set_logo_size_value)

    def set_logo_image(self, image: QImage):
        self._logo_image = image
        self.update()

    def clear_static_logo(self):
        self._logo = QPixmap()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._logo_image is not None and not self._logo_image.isNull():
            pixmap = QPixmap.fromImage(self._logo_image)
        elif not self._logo.isNull():
            pixmap = self._logo
        else:
            pixmap = None

        if pixmap is not None:
            scaled = pixmap.scaled(
                QSize(int(self._logo_size_value), int(self._logo_size_value)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)

        ring_rect = QRectF(20, 20, 220, 220)
        if self._ring_visible:
            painter.setPen(QPen(QColor("#EEF2F7"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring_rect, 0, 360 * 16)

        if self._ring_visible and not self._flash_hidden:
            painter.setPen(QPen(QColor("#FF6B98"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring_rect, self._angle * 16, int(self._span * 16))


class VideoLogoWidget(QWidget):
    """Paints MP4 frames as a normal QWidget, avoiding native video-window drift."""

    first_frame_ready = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(260, 260)
        self._image = None
        self._has_emitted_first_frame = False
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)

    def reset(self):
        self._image = None
        self._has_emitted_first_frame = False
        self.update()

    def _on_frame(self, frame):
        image = frame.toImage()
        if image.isNull() or self._is_black_frame(image):
            return
        self._image = image.copy()
        if not self._has_emitted_first_frame:
            self._has_emitted_first_frame = True
            self.first_frame_ready.emit()
        self.update()

    def _is_black_frame(self, image) -> bool:
        sample = image.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        dark = 0
        total = sample.width() * sample.height()
        for y in range(sample.height()):
            for x in range(sample.width()):
                color = sample.pixelColor(x, y)
                if color.red() + color.green() + color.blue() < 36:
                    dark += 1
        return total > 0 and dark / total > 0.92

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._image is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pixmap = QPixmap.fromImage(self._image)
        scaled = pixmap.scaled(
            self.size(),
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
        self.label.setWordWrap(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.icon)
        layout.addWidget(self.label, 1)

    def set_loading(self, text: str):
        self.icon.set_loading()
        self.label.setText(text)

    def set_complete(self, text: str):
        self.icon.set_complete()
        self.label.setText(text)

    def fit_to_width(self, max_width: int):
        max_width = max(180, max_width)
        self.setMaximumWidth(max_width)
        self.label.setMaximumWidth(max_width - self.icon.width() - 12)
        hint = self.sizeHint()
        self.resize(min(max_width, hint.width()), hint.height())


class CookieGuide(QWidget):
    auto_import_requested = pyqtSignal()
    manual_import_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(390)
        self.setStyleSheet("""
            QLabel#guideTitle {
                color: #111827;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#guideText {
                color: #4B5563;
                font-size: 13px;
                line-height: 1.45;
            }
            QLabel#guideStatus {
                color: #EF4444;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton {
                min-height: 36px;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                padding: 0 16px;
                background: white;
                color: #111827;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #F9FAFB;
                border-color: #FF8BAD;
            }
            QPushButton#primary {
                background: #FF6B98;
                border-color: #FF6B98;
                color: white;
            }
            QPushButton#primary:hover {
                background: #F85688;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("需要先配置 bilibili Cookie")
        title.setObjectName("guideTitle")
        layout.addWidget(title)

        text = QLabel(
            "如果你的电脑里，要查的账号已经在 Chrome、Edge、Firefox "
            "这三个浏览器之一登录了 bilibili，就选择自动导入。<br>"
            "否则选择手动导入，按教程复制 SESSDATA 和 bili_jct：<br>"
            '<a href="https://www.bilibili.com/opus/824969342470848537">'
            "https://www.bilibili.com/opus/824969342470848537</a>"
        )
        text.setObjectName("guideText")
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        layout.addWidget(text)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 2, 0, 0)
        btn_row.setSpacing(10)
        self.auto_btn = QPushButton("自动导入")
        self.auto_btn.setObjectName("primary")
        self.manual_btn = QPushButton("手动导入")
        btn_row.addWidget(self.auto_btn)
        btn_row.addWidget(self.manual_btn)
        layout.addLayout(btn_row)

        self.status = QLabel("")
        self.status.setObjectName("guideStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.auto_btn.clicked.connect(self.auto_import_requested.emit)
        self.manual_btn.clicked.connect(self.manual_import_requested.emit)

    def set_status(self, text: str):
        self.status.setText(text)

    def set_busy(self, busy: bool):
        self.auto_btn.setEnabled(not busy)
        self.manual_btn.setEnabled(not busy)


class ApiKeyGuide(QWidget):
    save_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(390)
        self.setStyleSheet("""
            QLabel#guideTitle {
                color: #111827;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#guideText {
                color: #4B5563;
                font-size: 13px;
                line-height: 1.45;
            }
            QLabel#guideStatus {
                color: #EF4444;
                font-size: 12px;
                font-weight: 600;
            }
            QLineEdit {
                min-height: 34px;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                padding: 0 10px;
                background: white;
                color: #111827;
            }
            QLineEdit:focus {
                border-color: #FF6B98;
            }
            QPushButton {
                min-height: 36px;
                border: 1px solid #FF6B98;
                border-radius: 8px;
                padding: 0 16px;
                background: #FF6B98;
                color: white;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #F85688;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("需要配置 DeepSeek API Key")
        title.setObjectName("guideTitle")
        layout.addWidget(title)

        text = QLabel(
            "广告识别需要调用 DeepSeek。可以在 DeepSeek 开放平台创建：<br>"
            '<a href="https://platform.deepseek.com/api_keys">'
            "https://platform.deepseek.com/api_keys</a>"
        )
        text.setObjectName("guideText")
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        layout.addWidget(text)

        self.input = QLineEdit()
        self.input.setPlaceholderText("输入 sk-...")
        self.input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.input)

        self.save_btn = QPushButton("保存并验证")
        layout.addWidget(self.save_btn)

        self.status = QLabel("")
        self.status.setObjectName("guideStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.save_btn.clicked.connect(lambda: self.save_requested.emit(self.input.text().strip()))

    def set_status(self, text: str):
        self.status.setText(text)

    def set_busy(self, busy: bool):
        self.input.setEnabled(not busy)
        self.save_btn.setEnabled(not busy)


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
        if resource_path("logo.mp4").exists():
            self.spinner.clear_static_logo()
        self.spinner_effect = QGraphicsOpacityEffect(self.spinner)
        self.spinner_effect.setOpacity(1.0)
        self.spinner.setGraphicsEffect(self.spinner_effect)

        self.video_widget = VideoLogoWidget(self.stage)
        self.video_widget.setStyleSheet("background: transparent;")
        self.video_effect = QGraphicsOpacityEffect(self.video_widget)
        self.video_effect.setOpacity(0.0)
        self.video_widget.setGraphicsEffect(self.video_effect)
        self.video_widget.hide()
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.0)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoSink(self.video_widget.sink)

        self.status_line = StatusLine(self.stage)
        self.status_effect = QGraphicsOpacityEffect(self.status_line)
        self.status_effect.setOpacity(0.0)
        self.status_line.setGraphicsEffect(self.status_effect)
        self.status_line.setVisible(False)

        self.cookie_guide = CookieGuide(self.stage)
        self.cookie_guide_effect = QGraphicsOpacityEffect(self.cookie_guide)
        self.cookie_guide_effect.setOpacity(0.0)
        self.cookie_guide.setGraphicsEffect(self.cookie_guide_effect)
        self.cookie_guide.setVisible(False)

        self.api_key_guide = ApiKeyGuide(self.stage)
        self.api_key_guide_effect = QGraphicsOpacityEffect(self.api_key_guide)
        self.api_key_guide_effect.setOpacity(0.0)
        self.api_key_guide.setGraphicsEffect(self.api_key_guide_effect)
        self.api_key_guide.setVisible(False)
        layout.addWidget(self.stage)

        layout.addStretch(1)
        credit = QLabel("@Developed by Zhewen Guo from Columbia University")
        credit.setObjectName("credit")
        credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(credit)

        self._animations = []
        self._logo_has_moved = False
        self._cookie_guide_active = False
        self._api_key_guide_active = False
        self._outro_active = False
        self._outro_finished = False
        QTimer.singleShot(0, self._position_logo_center)
        QTimer.singleShot(0, self._prime_logo_first_frame)

    def reveal_status(self, text: str):
        self.spinner.hide_ring()
        self.status_line.set_loading(text)
        self.status_line.setVisible(True)
        self.status_effect.setOpacity(0.0)

        logo_start = self.spinner.pos()
        logo_end = self._logo_left_pos()
        self._fit_status_for_logo(logo_end)
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
        self._fit_status_for_logo()
        self.status_line.move(self._status_pos(self.spinner.pos()))

    def set_status_complete(self, text: str):
        self.status_line.set_complete(text)
        self._fit_status_for_logo()
        self.status_line.move(self._status_pos(self.spinner.pos()))

    def show_success_from_guide(self, text: str):
        active_guide = None
        if self.cookie_guide.isVisible():
            active_guide = self.cookie_guide
        elif self.api_key_guide.isVisible():
            active_guide = self.api_key_guide

        logo_pos = self._logo_left_pos()
        self.spinner.move(logo_pos)
        self._logo_has_moved = True
        self._cookie_guide_active = False
        self._api_key_guide_active = False
        self.spinner_effect.setOpacity(0.0)

        self.status_line.set_complete(text)
        self._fit_status_for_logo(logo_pos)
        status_start = self._status_right_pos(active_guide)
        status_end = self._status_pos(logo_pos)
        self.status_line.move(status_start)
        self.status_effect.setOpacity(1.0)
        self.status_line.setVisible(True)

        status_move = QPropertyAnimation(self.status_line, b"pos")
        status_move.setDuration(520)
        status_move.setStartValue(status_start)
        status_move.setEndValue(status_end)
        status_move.setEasingCurve(QEasingCurve.Type.InOutCubic)
        status_move.finished.connect(lambda: (
            self._fit_status_for_logo(logo_pos),
            self.status_line.move(self._status_pos(logo_pos)),
        ))

        logo_fade = QPropertyAnimation(self.spinner_effect, b"opacity")
        logo_fade.setDuration(420)
        logo_fade.setStartValue(0.0)
        logo_fade.setEndValue(1.0)
        logo_fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        animations = self._fade_visible_guides()
        animations.extend([logo_fade, status_move])
        for animation in animations:
            self._keep_animation(animation)
            animation.start()

    def show_cookie_guide(self):
        self._cookie_guide_active = True
        self._api_key_guide_active = False
        self.cookie_guide.set_status("")
        self.cookie_guide.set_busy(False)
        self.cookie_guide.resize(self.cookie_guide.sizeHint())
        self.cookie_guide.move(self._guide_logo_pos(self.cookie_guide))
        self.cookie_guide.setVisible(True)

        self.status_line.setVisible(True)
        status_start = self.status_line.pos()
        status_end = self._status_right_pos(self.cookie_guide)

        status_move = QPropertyAnimation(self.status_line, b"pos")
        status_move.setDuration(520)
        status_move.setStartValue(status_start)
        status_move.setEndValue(status_end)
        status_move.setEasingCurve(QEasingCurve.Type.InOutCubic)

        logo_fade = QPropertyAnimation(self.spinner_effect, b"opacity")
        logo_fade.setDuration(420)
        logo_fade.setStartValue(self.spinner_effect.opacity())
        logo_fade.setEndValue(0.0)
        logo_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)

        guide_fade = QPropertyAnimation(self.cookie_guide_effect, b"opacity")
        guide_fade.setDuration(420)
        guide_fade.setStartValue(0.0)
        guide_fade.setEndValue(1.0)
        guide_fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        for animation in (status_move, logo_fade, guide_fade):
            self._keep_animation(animation)
            animation.start()

    def set_cookie_guide_status(self, text: str, busy: bool = False):
        self.cookie_guide.set_status(text)
        self.cookie_guide.set_busy(busy)

    def show_api_key_guide(self):
        self._cookie_guide_active = False
        self._api_key_guide_active = True
        self.api_key_guide.set_status("")
        self.api_key_guide.set_busy(False)
        self.api_key_guide.resize(self.api_key_guide.sizeHint())
        self.api_key_guide.move(self._guide_logo_pos(self.api_key_guide))
        self.api_key_guide.setVisible(True)

        self.status_line.setVisible(True)
        status_start = self.status_line.pos()
        status_end = self._status_right_pos(self.api_key_guide)

        status_move = QPropertyAnimation(self.status_line, b"pos")
        status_move.setDuration(520)
        status_move.setStartValue(status_start)
        status_move.setEndValue(status_end)
        status_move.setEasingCurve(QEasingCurve.Type.InOutCubic)

        logo_fade = QPropertyAnimation(self.spinner_effect, b"opacity")
        logo_fade.setDuration(420)
        logo_fade.setStartValue(self.spinner_effect.opacity())
        logo_fade.setEndValue(0.0)
        logo_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)

        guide_fade = QPropertyAnimation(self.api_key_guide_effect, b"opacity")
        guide_fade.setDuration(420)
        guide_fade.setStartValue(0.0)
        guide_fade.setEndValue(1.0)
        guide_fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        for animation in (status_move, logo_fade, guide_fade):
            self._keep_animation(animation)
            animation.start()

    def set_api_key_guide_status(self, text: str, busy: bool = False):
        self.api_key_guide.set_status(text)
        self.api_key_guide.set_busy(busy)

    def _fade_visible_guides(self) -> list[QPropertyAnimation]:
        guides = (
            (self.cookie_guide, self.cookie_guide_effect),
            (self.api_key_guide, self.api_key_guide_effect),
        )
        animations = []
        for guide, effect in guides:
            if not guide.isVisible():
                continue
            fade = QPropertyAnimation(effect, b"opacity")
            fade.setDuration(260)
            fade.setStartValue(effect.opacity())
            fade.setEndValue(0.0)
            fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
            fade.finished.connect(guide.hide)
            animations.append(fade)
        return animations

    def transition_status(self, text: str, after_fade_in=None):
        fade_out = QPropertyAnimation(self.status_effect, b"opacity")
        fade_out.setDuration(220)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def fade_in_next():
            self.status_line.set_loading(text)
            self._fit_status_for_logo()
            self.status_line.move(self._status_pos(self.spinner.pos()))
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

    def play_logo_outro(self, video_path: Path, on_finished):
        self._outro_active = True
        self._outro_finished = False
        self._cookie_guide_active = False
        self._api_key_guide_active = False

        center_pos = self._logo_center_pos()
        self.video_widget.move(center_pos)
        self.video_widget.reset()
        self.video_widget.show()
        self.video_widget.raise_()
        self.video_effect.setOpacity(0.0)

        status_fade = QPropertyAnimation(self.status_effect, b"opacity")
        status_fade.setDuration(320)
        status_fade.setStartValue(self.status_effect.opacity())
        status_fade.setEndValue(0.0)
        status_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        status_fade.finished.connect(self.status_line.hide)

        logo_move = QPropertyAnimation(self.spinner, b"pos")
        logo_move.setDuration(620)
        logo_move.setStartValue(self.spinner.pos())
        logo_move.setEndValue(center_pos)
        logo_move.setEasingCurve(QEasingCurve.Type.InOutCubic)

        logo_scale = QPropertyAnimation(self.spinner, b"logo_size_value")
        logo_scale.setDuration(620)
        logo_scale.setStartValue(self.spinner.logo_size_value)
        logo_scale.setEndValue(float(LOGO_OUTRO_SIZE))
        logo_scale.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def start_video():
            self._logo_has_moved = False
            self.video_widget.move(self._logo_center_pos())
            if not video_path.exists():
                finish_once()
                return

            self.media_player.setSource(QUrl.fromLocalFile(str(video_path)))
            self.media_player.play()
            QTimer.singleShot(1200, reveal_video)
            QTimer.singleShot(6000, finish_once)

        def reveal_video():
            if self._outro_finished or self.video_effect.opacity() >= 1.0:
                return
            self.video_effect.setOpacity(1.0)
            self.spinner.hide()

        def on_media_status(status):
            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                finish_once()

        def on_media_error(error, error_text=""):
            if error != QMediaPlayer.Error.NoError:
                finish_once()

        def finish_once():
            if self._outro_finished:
                return
            self._outro_finished = True
            self.media_player.stop()

            fade_video = QPropertyAnimation(self.video_effect, b"opacity")
            fade_video.setDuration(420)
            fade_video.setStartValue(self.video_effect.opacity())
            fade_video.setEndValue(0.0)
            fade_video.setEasingCurve(QEasingCurve.Type.InOutCubic)

            def done():
                self.video_widget.hide()
                self._outro_active = False
                self.spinner.set_logo_size(LOGO_IDLE_SIZE)
                try:
                    self.media_player.mediaStatusChanged.disconnect(on_media_status)
                except Exception:
                    pass
                try:
                    self.media_player.errorOccurred.disconnect(on_media_error)
                except Exception:
                    pass
                try:
                    self.video_widget.first_frame_ready.disconnect(reveal_video)
                except Exception:
                    pass
                on_finished()

            fade_video.finished.connect(done)
            self._keep_animation(fade_video)
            fade_video.start()

        self.media_player.mediaStatusChanged.connect(on_media_status)
        self.media_player.errorOccurred.connect(on_media_error)
        self.video_widget.first_frame_ready.connect(reveal_video)
        logo_move.finished.connect(start_video)

        for animation in (status_fade, logo_move, logo_scale):
            self._keep_animation(animation)
            animation.start()

    def _prime_logo_first_frame(self):
        video_path = resource_path("logo.mp4")
        if not video_path.exists():
            return

        def apply_first_frame():
            if self.video_widget._image is not None:
                self.spinner.set_logo_image(self.video_widget._image)
            try:
                self.video_widget.first_frame_ready.disconnect(apply_first_frame)
            except Exception:
                pass
            if not self._outro_active:
                self.media_player.pause()
                self.media_player.setPosition(0)

        self.video_widget.reset()
        self.video_widget.first_frame_ready.connect(apply_first_frame)
        self.media_player.setSource(QUrl.fromLocalFile(str(video_path)))
        self.media_player.play()

    def _keep_animation(self, animation):
        self._animations.append(animation)
        animation.finished.connect(lambda: self._animations.remove(animation) if animation in self._animations else None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "stage"):
            return
        if self._outro_active:
            center_pos = self._logo_center_pos()
            self.spinner.move(center_pos)
            self.video_widget.move(center_pos)
            return
        if self._logo_has_moved:
            logo_pos = self._logo_left_pos()
            self.spinner.move(logo_pos)
            self._fit_status_for_logo(logo_pos)
            if self._cookie_guide_active:
                self.cookie_guide.move(self._guide_logo_pos(self.cookie_guide))
                self.status_line.move(self._status_right_pos(self.cookie_guide))
            elif self._api_key_guide_active:
                self.api_key_guide.move(self._guide_logo_pos(self.api_key_guide))
                self.status_line.move(self._status_right_pos(self.api_key_guide))
            else:
                self.status_line.move(self._status_pos(logo_pos))
                self.cookie_guide.move(self._guide_pos())
                self.api_key_guide.move(self._api_key_guide_pos())
        else:
            self._position_logo_center()

    def _position_logo_center(self):
        if not hasattr(self, "stage"):
            return
        self.spinner.move(self._logo_center_pos())

    def _logo_center_pos(self) -> QPoint:
        x = (self.stage.width() - self.spinner.width()) // 2
        y = (self.stage.height() - self.spinner.height()) // 2
        return QPoint(max(0, x), max(0, y))

    def _logo_left_pos(self) -> QPoint:
        x = max(18, (self.stage.width() - self.spinner.width()) // 2 - 230)
        y = (self.stage.height() - self.spinner.height()) // 2
        return QPoint(x, max(0, y))

    def _status_pos(self, logo_pos: QPoint) -> QPoint:
        x = self._status_x_for_logo(logo_pos)
        y = logo_pos.y() + (self.spinner.height() - self.status_line.height()) // 2
        return QPoint(x, max(0, y))

    def _fit_status_for_logo(self, logo_pos: QPoint | None = None):
        if logo_pos is None:
            logo_pos = self.spinner.pos()
        x = self._status_x_for_logo(logo_pos)
        available = self.stage.width() - x - 24
        self.status_line.fit_to_width(available)

    def _status_x_for_logo(self, logo_pos: QPoint) -> int:
        visible_logo_right = logo_pos.x() + (self.spinner.width() + 150) // 2
        return visible_logo_right + 70

    def _fit_status_centered(self):
        self.status_line.fit_to_width(self.stage.width() - 48)

    def _status_right_pos(self, guide: QWidget | None = None) -> QPoint:
        min_x = 24
        if guide is not None:
            guide_pos = self._guide_logo_pos(guide)
            min_x = guide_pos.x() + guide.width() + 38

        available = max(180, self.stage.width() - min_x - 24)
        self.status_line.fit_to_width(available)
        x = max(min_x, self.stage.width() - self.status_line.width() - 24)
        y = (self.stage.height() - self.status_line.height()) // 2
        return QPoint(max(24, x), max(0, y))

    def _guide_logo_pos(self, guide: QWidget) -> QPoint:
        logo_pos = self._logo_left_pos()
        x = logo_pos.x() + (self.spinner.width() - guide.width()) // 2
        y = logo_pos.y() + (self.spinner.height() - guide.height()) // 2
        return QPoint(max(24, x), max(0, y))

    def _guide_pos(self) -> QPoint:
        x = max(24, (self.stage.width() - self.cookie_guide.width()) // 2)
        y = max(0, (self.stage.height() - self.cookie_guide.height()) // 2)
        return QPoint(x, y)

    def _api_key_guide_pos(self) -> QPoint:
        x = max(24, (self.stage.width() - self.api_key_guide.width()) // 2)
        y = max(0, (self.stage.height() - self.api_key_guide.height()) // 2)
        return QPoint(x, y)

    def _guide_status_pos(self) -> QPoint:
        x = max(24, (self.stage.width() - self.status_line.width()) // 2)
        y = max(0, (self.stage.height() - self.status_line.height()) // 2)
        return QPoint(x, y)


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

    state = {
        "window": None,
        "splash": splash,
        "animations": [],
        "cookie_worker": None,
        "import_worker": None,
        "api_key_worker": None,
        "submission_preload_worker": None,
        "submission_preload": None,
    }

    def stop_running_workers():
        for key in ("cookie_worker", "import_worker", "api_key_worker", "submission_preload_worker"):
            worker = state.get(key)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(3000):
                    worker.terminate()
                    worker.wait(1000)

    app.aboutToQuit.connect(stop_running_workers)

    def show_main_window():
        window = MainWindow(preloaded_submissions=state.get("submission_preload"))
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
        if splash.cookie_guide.isVisible() or splash.api_key_guide.isVisible():
            splash.show_success_from_guide(text)
        else:
            splash.set_status_complete(text)
        QTimer.singleShot(1000, next_step)

    def check_cookie():
        splash.transition_status(
            "正在检查bilibili cookie",
            lambda: start_cookie_worker(
                lambda: complete_and_continue(f"{state['username']}，cookie 已就绪", check_api_key),
                show_cookie_guide_after_missing,
            ),
        )

    def show_cookie_guide_after_missing():
        splash.set_status_loading("未找到有效 cookie")
        QTimer.singleShot(1000, splash.show_cookie_guide)

    def start_cookie_worker(on_valid, on_invalid):
        state["cookie_loading_since"] = time.time()
        worker = CookieCheckWorker()
        state["cookie_worker"] = worker

        def cleanup_cookie_worker():
            if state.get("cookie_worker") is worker:
                state["cookie_worker"] = None
            worker.deleteLater()

        def on_cookie_checked(valid: bool, username: str, mid: int):
            def apply():
                if valid:
                    state["username"] = username
                    state["mid"] = mid
                    start_submission_preload(mid)
                    on_valid()
                else:
                    on_invalid()

            elapsed = time.time() - state.get("cookie_loading_since", 0)
            if elapsed < 1.0:
                QTimer.singleShot(int((1.0 - elapsed) * 1000), apply)
            else:
                apply()

        worker.checked.connect(on_cookie_checked)
        worker.finished.connect(cleanup_cookie_worker)
        worker.start()

    def start_submission_preload(mid: int):
        if not mid:
            return
        existing = state.get("submission_preload_worker")
        if existing is not None and existing.isRunning():
            return
        worker = SubmissionPreloadWorker(mid)
        state["submission_preload_worker"] = worker

        def on_preloaded(result: dict):
            if result.get("cancelled"):
                return
            state["submission_preload"] = result
            window = state.get("window")
            if window is not None:
                window.apply_preloaded_submissions(result)

        def cleanup_submission_worker():
            if state.get("submission_preload_worker") is worker:
                state["submission_preload_worker"] = None
            worker.deleteLater()

        worker.preloaded.connect(on_preloaded)
        worker.finished.connect(cleanup_submission_worker)
        worker.start()

    def validate_after_import():
        splash.set_cookie_guide_status("正在验证 Cookie…", busy=True)
        start_cookie_worker(
            lambda: complete_and_continue(f"{state['username']}，cookie 已就绪", check_api_key),
            lambda: splash.set_cookie_guide_status("验证不通过请重试", busy=False),
        )

    def import_cookie_auto():
        splash.set_cookie_guide_status("正在从 Chrome / Edge / Firefox 自动导入…", busy=True)
        worker = CookieImportWorker()
        state["import_worker"] = worker

        def cleanup_import_worker():
            if state.get("import_worker") is worker:
                state["import_worker"] = None
            worker.deleteLater()

        def on_imported(ok: bool, err: str):
            if ok:
                validate_after_import()
            else:
                splash.set_cookie_guide_status(f"验证不通过请重试：{err}", busy=False)

        worker.imported.connect(on_imported)
        worker.finished.connect(cleanup_import_worker)
        worker.start()

    def import_cookie_manual():
        sessdata, ok1 = QInputDialog.getText(
            splash,
            "手动导入 Cookie",
            "请输入 SESSDATA:",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok1 or not sessdata.strip():
            return

        bili_jct, ok2 = QInputDialog.getText(
            splash,
            "手动导入 Cookie",
            "请输入 bili_jct:",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok2 or not bili_jct.strip():
            return

        try:
            from src.cookie_importer import CookiePair, save_to_env
            save_to_env(CookiePair(sessdata=sessdata.strip(), bili_jct=bili_jct.strip()))
            validate_after_import()
        except Exception as exc:
            splash.set_cookie_guide_status(f"验证不通过请重试：{exc}", busy=False)

    splash.cookie_guide.auto_import_requested.connect(import_cookie_auto)
    splash.cookie_guide.manual_import_requested.connect(import_cookie_manual)

    def check_api_key():
        splash.transition_status(
            "正在检查DeepSeek API Key",
            lambda: start_api_key_worker(
                lambda: complete_and_continue("DeepSeek API Key 可用", show_welcome),
                show_api_key_guide_after_missing,
            ),
        )

    def show_welcome():
        complete_and_continue(
            f"{state['username']}，欢迎使用ADs Flank",
            lambda: splash.play_logo_outro(resource_path("logo.mp4"), show_main_window),
        )

    def show_api_key_guide_after_missing():
        splash.set_status_loading("未找到可用 API Key")
        QTimer.singleShot(1000, splash.show_api_key_guide)

    def start_api_key_worker(on_valid, on_invalid):
        state["api_key_loading_since"] = time.time()
        worker = DeepSeekApiKeyCheckWorker()
        state["api_key_worker"] = worker

        def cleanup_api_key_worker():
            if state.get("api_key_worker") is worker:
                state["api_key_worker"] = None
            worker.deleteLater()

        def on_api_key_checked(valid: bool, err: str):
            def apply():
                if valid:
                    on_valid()
                else:
                    state["api_key_error"] = err
                    on_invalid()

            elapsed = time.time() - state.get("api_key_loading_since", 0)
            if elapsed < 1.0:
                QTimer.singleShot(int((1.0 - elapsed) * 1000), apply)
            else:
                apply()

        worker.checked.connect(on_api_key_checked)
        worker.finished.connect(cleanup_api_key_worker)
        worker.start()

    def save_api_key_to_env(key: str):
        ensure_env_file()
        existing: dict[str, str] = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    existing[k.strip()] = v.strip()
        existing["DEEPSEEK_API_KEY"] = key
        lines = [f"{k}={v}" for k, v in existing.items()]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def validate_after_api_key_save():
        splash.set_api_key_guide_status("正在验证 DeepSeek API Key…", busy=True)
        start_api_key_worker(
            lambda: complete_and_continue("DeepSeek API Key 可用", show_welcome),
            lambda: splash.set_api_key_guide_status("验证不通过请重试", busy=False),
        )

    def save_api_key_from_guide(key: str):
        if not key:
            splash.set_api_key_guide_status("请先填写 DeepSeek API Key", busy=False)
            return
        try:
            save_api_key_to_env(key)
            validate_after_api_key_save()
        except Exception as exc:
            splash.set_api_key_guide_status(f"验证不通过请重试：{exc}", busy=False)

    splash.api_key_guide.save_requested.connect(save_api_key_from_guide)

    def check_env():
        splash.reveal_status("正在检测env配置文件")
        QTimer.singleShot(1000, lambda: (
            ensure_env_file(),
            complete_and_continue("找到env文件", check_cookie) if ENV_FILE.exists() else None
        ))

    QTimer.singleShot(2000, lambda: splash.spinner.close_and_flash(check_env))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
