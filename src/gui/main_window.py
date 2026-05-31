"""主窗口 — 评论爬取 → AI 检测 → 删除，一站式操作。"""

import asyncio
import json
from pathlib import Path

from loguru import logger
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QSplitter, QStatusBar, QMessageBox,
    QGroupBox, QCheckBox, QSpinBox, QTabWidget,
    QTextEdit, QApplication,
)

from src.config import Config
from src.crawler.models import Comment, ProgressEvent, CrawlResult
from src.agent.ad_detector import BatchAdJudgment, CommentAdJudgment
from src.gui.theme import Light, Dark, build_stylesheet, SPACING, FONT_SIZES, FONT_WEIGHTS, FONT_FAMILY


# ============================================================
# 异步桥接（让 async 任务在 Qt 主线程安全执行）
# ============================================================

class AsyncRunner(QObject):
    """在独立 QThread 中运行 async 函数，完成后发射信号回主线程。"""
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, coro, parent=None):
        super().__init__(parent)
        self._coro = coro

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._coro)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bilibili ADs Flak")
        self.resize(1100, 780)
        self.setMinimumSize(900, 600)

        # 状态
        self._config = Config.from_env()
        self._dark_mode = False
        self._current_theme = Light()
        self._comments: list[Comment] = []
        self._oid: int | None = None
        self._video_title: str = ""
        self._judgments: BatchAdJudgment | None = None

        self._init_ui()
        self._apply_theme()

    # ==================== UI 构建 ====================

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], 0)
        root.setSpacing(SPACING["md"])

        # ---- 顶栏 ----
        root.addWidget(self._build_header())

        # ---- 主内容区 ----
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_control_panel())
        splitter.addWidget(self._build_table_panel())
        splitter.addWidget(self._build_action_panel())
        splitter.setSizes([100, 400, 150])
        root.addWidget(splitter, 1)

        # ---- 状态栏 ----
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("就绪  |  双击 .bat 可导入 Cookie")

    def _build_header(self) -> QWidget:
        """品牌标题栏。"""
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Bilibili ADs Flak")
        title.setObjectName("title")
        title.setStyleSheet(
            f"font-size:{FONT_SIZES['h1']}; font-weight:{FONT_WEIGHTS['bold']}; "
            f"color:{self._current_theme.BRAND_PINK};"
        )
        bar.addWidget(title)
        bar.addStretch()

        self._dark_btn = QPushButton("🌙 暗色模式")
        self._dark_btn.setObjectName("dark_toggle")
        self._dark_btn.clicked.connect(self._toggle_dark_mode)
        bar.addWidget(self._dark_btn)

        btn_cookie = QPushButton("导入 Cookie")
        btn_cookie.clicked.connect(self._import_cookies)
        bar.addWidget(btn_cookie)

        container = QWidget()
        container.setLayout(bar)
        return container

    def _build_control_panel(self) -> QFrame:
        """爬取控制面板：BV 输入 + 进度。"""
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["sm"])
        layout.setSpacing(SPACING["sm"])

        # 标题行
        header = QHBoxLayout()
        h = QLabel("📥 评论爬取")
        h.setStyleSheet(f"font-size:{FONT_SIZES['h3']}; font-weight:{FONT_WEIGHTS['semibold']};")
        header.addWidget(h)
        header.addStretch()

        self._depth_spin = QSpinBox()
        self._depth_spin.setRange(1, 5)
        self._depth_spin.setValue(2)
        self._depth_spin.setPrefix("深度: ")
        self._depth_spin.setMaximumWidth(100)
        header.addWidget(self._depth_spin)

        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(1, 60)
        self._delay_spin.setValue(10)
        self._delay_spin.setSuffix(" 条/分")
        self._delay_spin.setMaximumWidth(100)
        header.addWidget(self._delay_spin)
        layout.addLayout(header)

        # 输入行
        row = QHBoxLayout()
        self._bv_input = QLineEdit()
        self._bv_input.setPlaceholderText("输入 BV 号，如 BV1unGi6UEwW…")
        self._bv_input.returnPressed.connect(self._on_crawl)
        row.addWidget(self._bv_input, 1)

        self._btn_preview = QPushButton("预览")
        self._btn_preview.clicked.connect(self._on_preview)
        row.addWidget(self._btn_preview)

        self._btn_crawl = QPushButton("开始爬取")
        self._btn_crawl.setObjectName("primary")
        self._btn_crawl.clicked.connect(self._on_crawl)
        row.addWidget(self._btn_crawl)

        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_cancel.setEnabled(False)
        row.addWidget(self._btn_cancel)
        layout.addLayout(row)

        # 进度条
        self._progress = QProgressBar()
        self._progress.setValue(0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        return card

    def _build_table_panel(self) -> QFrame:
        """评论表格。"""
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["sm"])
        layout.setSpacing(SPACING["sm"])

        title = QLabel(f"💬 评论列表 (0 条)")
        title.setObjectName("table_title")
        title.setStyleSheet(f"font-size:{FONT_SIZES['h3']}; font-weight:{FONT_WEIGHTS['semibold']};")
        self._table_title = title
        layout.addWidget(title)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["序号", "用户名", "内容", "点赞", "广告", "判定理由"])
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 50)
        self._table.setColumnWidth(1, 120)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 60)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        layout.addWidget(self._table, 1)

        return card

    def _build_action_panel(self) -> QFrame:
        """操作面板：AI 检测 + 删除。"""
        card = QFrame()
        card.setObjectName("card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(SPACING["md"], SPACING["sm"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["md"])

        # 左侧：检测
        detect_group = QGroupBox("🤖 AI 广告检测")
        dl = QVBoxLayout(detect_group)
        self._btn_detect = QPushButton("检测广告评论")
        self._btn_detect.setObjectName("primary")
        self._btn_detect.clicked.connect(self._on_detect)
        self._btn_detect.setEnabled(False)
        dl.addWidget(self._btn_detect)
        self._detect_status = QLabel("")
        self._detect_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.TEXT_SECONDARY};")
        dl.addWidget(self._detect_status)
        layout.addWidget(detect_group, 1)

        # 右侧：删除
        del_group = QGroupBox("🗑️ 删除操作")
        dl2 = QVBoxLayout(del_group)
        self._dry_run_check = QCheckBox("模拟删除 (dry-run)")
        self._dry_run_check.setChecked(True)
        dl2.addWidget(self._dry_run_check)

        btn_row = QHBoxLayout()
        self._btn_delete = QPushButton("删除广告评论")
        self._btn_delete.setObjectName("danger")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btn_row.addWidget(self._btn_delete)
        dl2.addLayout(btn_row)

        self._delete_status = QLabel("")
        self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']};")
        dl2.addWidget(self._delete_status)
        layout.addWidget(del_group, 1)

        return card

    # ==================== 主题 ====================

    def _apply_theme(self):
        self.setStyleSheet(build_stylesheet(self._current_theme))
        self._dark_btn.setText("☀️ 亮色模式" if self._dark_mode else "🌙 暗色模式")

    def _toggle_dark_mode(self):
        self._dark_mode = not self._dark_mode
        self._current_theme = Dark() if self._dark_mode else Light()
        self._apply_theme()

    # ==================== 事件处理 ====================

    def _on_preview(self):
        bv = self._bv_input.text().strip()
        if not bv:
            return
        self._run_async(self._do_preview(bv))

    async def _do_preview(self, bv_id: str):
        from src.service import CrawlerService
        svc = CrawlerService(self._config)
        info = await svc.get_video_info(bv_id)
        self._oid = int(bv_id)  # placeholder, oid fetched in crawl
        self._video_title = info.title
        self._status_bar.showMessage(f"预览: {info.title}  |  评论数: {info.total_comments}")
        QMessageBox.information(self, "视频预览",
            f"标题: {info.title}\n评论总数: {info.total_comments}")

    def _on_crawl(self):
        bv = self._bv_input.text().strip()
        if not bv:
            return
        self._btn_crawl.setEnabled(False)
        self._btn_preview.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._btn_detect.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._table.setRowCount(0)
        self._judgments = None
        self._run_async(self._do_crawl(bv))

    async def _do_crawl(self, bv_id: str):
        from src.service import CrawlerService
        self._config.max_reply_depth = self._depth_spin.value()
        svc = CrawlerService(self._config)

        def on_progress(ev: ProgressEvent):
            self._progress.setMaximum(ev.estimated_total or 0)
            self._progress.setValue(ev.total_crawled)
            self._status_bar.showMessage(ev.message)

        result: CrawlResult = await svc.crawl(bv_id, on_progress=on_progress)
        self._comments = result.comments
        self._video_title = result.video_title
        # oid 从 crawler 内部获取，这里用 config
        self._refresh_table()
        self._status_bar.showMessage(
            f"爬取完成: {result.video_title}  |  {result.total_count} 条  |  {result.crawl_time:.1f}s"
        )
        self._btn_crawl.setEnabled(True)
        self._btn_preview.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._btn_detect.setEnabled(len(self._comments) > 0)
        self._progress.setVisible(False)

    def _on_cancel(self):
        from src.service import CrawlerService
        # 简单取消（实际需持有 service 引用，此处用临时方案）
        self._status_bar.showMessage("已请求取消…")

    def _on_detect(self):
        if not self._comments:
            return
        self._btn_detect.setEnabled(False)
        self._detect_status.setText("检测中…")
        self._detect_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_BLUE};")
        self._run_async(self._do_detect())

    async def _do_detect(self):
        from src.llm.deepseek import DeepSeekClient
        from src.agent.ad_detector import AdDetector

        client = DeepSeekClient(api_key=self._config.deepseek_api_key)
        detector = AdDetector(client)
        comments_data = [
            {"rpid": str(c.rpid), "content": c.content, "parent_id": str(c.parent_id or "")}
            for c in self._comments
        ]
        self._judgments = await detector.detect(comments_data)

        ad_count = sum(1 for j in self._judgments.judgments if j.is_ad)
        self._detect_status.setText(f"检测完成: {ad_count}/{len(self._judgments.judgments)} 条广告")
        self._detect_status.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED if ad_count > 0 else self._current_theme.BRAND_GREEN};"
        )
        self._refresh_table(show_judgments=True)
        self._btn_detect.setEnabled(True)
        self._btn_delete.setEnabled(ad_count > 0)

    def _on_delete(self):
        if not self._judgments:
            return
        dry_run = self._dry_run_check.isChecked()
        if not dry_run:
            reply = QMessageBox.warning(
                self, "确认删除",
                f"将实际删除 {sum(1 for j in self._judgments.judgments if j.is_ad)} 条广告评论。\n此操作不可逆，确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._btn_delete.setEnabled(False)
        self._delete_status.setText("删除中…")
        self._run_async(self._do_delete(dry_run))

    async def _do_delete(self, dry_run: bool):
        from src.deleter.deleter import AdDeleter

        bv = self._bv_input.text().strip()
        # 获取 oid 需要再调一次 video info
        from src.service import CrawlerService
        svc = CrawlerService(self._config)
        info = await svc.get_video_info(bv)
        from bilibili_api import video as bv_video, Credential
        cred = None
        if self._config.sessdata and self._config.bili_jct:
            cred = Credential(sessdata=self._config.sessdata, bili_jct=self._config.bili_jct)
        v = bv_video.Video(bvid=bv, credential=cred)
        full_info = await v.get_info()
        oid = full_info["aid"]

        deleter = AdDeleter(self._config)
        result = await deleter.delete(
            bv_id=bv, oid=oid, judgments=self._judgments,
            dry_run=dry_run,
            delete_rate_per_minute=self._delay_spin.value(),
        )

        if result.skipped_count > 0:
            self._delete_status.setText(f"已跳过 {result.skipped_count} 条（非本人视频）")
            self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_ORANGE};")
        elif dry_run:
            self._delete_status.setText(f"模拟完成: {result.total_to_delete} 条待删除")
            self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_GREEN};")
        else:
            self._delete_status.setText(f"删除完成: {result.success_count} 成功, {result.failed_count} 失败")
            color = self._current_theme.BRAND_GREEN if result.all_success else self._current_theme.BRAND_ORANGE
            self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{color};")

        self._btn_delete.setEnabled(True)

    # ==================== 表格刷新 ====================

    def _refresh_table(self, show_judgments: bool = False):
        """用 Comment 列表填充表格。"""
        self._table.setRowCount(0)

        # 展平树形结构
        flat = []
        def walk(comments, depth=0):
            for c in comments:
                flat.append((c, depth))
                if c.replies:
                    walk(c.replies, depth + 1)
        walk(self._comments)

        self._table.setRowCount(len(flat))
        self._table_title.setText(f"💬 评论列表 ({len(flat)} 条)")

        j_map: dict[str, CommentAdJudgment] = {}
        if show_judgments and self._judgments:
            j_map = {j.rpid: j for j in self._judgments.judgments}

        for i, (c, depth) in enumerate(flat):
            prefix = "  " * depth + ("└ " if depth > 0 else "")
            rpid_str = str(c.rpid)

            items = [
                QTableWidgetItem(str(i + 1)),
                QTableWidgetItem(prefix + c.username),
                QTableWidgetItem(c.content),
                QTableWidgetItem(str(c.likes)),
                QTableWidgetItem(""),
                QTableWidgetItem(""),
            ]

            # 广告判定着色
            j = j_map.get(rpid_str)
            if j and j.is_ad:
                items[4].setText("🚫 广告")
                items[4].setForeground(QColor(self._current_theme.BRAND_RED))
                items[5].setText(f"{j.ad_type or ''}: {j.reason}")
                for item in items:
                    item.setBackground(QColor("#FFF0F0" if not self._dark_mode else "#3A1A1A"))
            elif j:
                items[4].setText("✓ 正常")
                items[5].setText(j.reason)

            for col, item in enumerate(items):
                self._table.setItem(i, col, item)

    # ==================== 工具方法 ====================

    def _import_cookies(self):
        try:
            from src.cookie_importer import import_cookies, save_to_env
            cookies = import_cookies()
            save_to_env(cookies)
            self._config = Config.from_env()
            QMessageBox.information(self, "导入成功", "Cookie 已写入 .env，下次启动生效。")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))

    def _run_async(self, coro):
        """在后台线程中运行异步任务，完成后在主线程回调。"""
        self._runner = AsyncRunner(coro)
        self._thread = QThread()
        self._runner.moveToThread(self._thread)
        self._thread.started.connect(self._runner.run)

        def on_finished(result):
            self._thread.quit()
            self._thread.wait()
        def on_error(err):
            self._status_bar.showMessage(f"错误: {err}")
            QMessageBox.critical(self, "错误", err)
            self._thread.quit()
            self._thread.wait()

        self._runner.finished.connect(on_finished)
        self._runner.error.connect(on_error)
        self._thread.start()
