"""主窗口 — 评论爬取 → AI 检测 → 删除，一站式操作。"""

import asyncio
import json
from pathlib import Path

import requests
from loguru import logger
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QUrl
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QSplitter, QStatusBar, QMessageBox,
    QGroupBox, QCheckBox, QSpinBox, QTabWidget,
    QTextEdit, QApplication, QInputDialog,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from src.config import Config
from src.crawler.models import Comment, ProgressEvent, CrawlResult
from src.agent.ad_detector import BatchAdJudgment, CommentAdJudgment
from src.gui.theme import Light, Dark, build_stylesheet, SPACING, FONT_SIZES, FONT_WEIGHTS, FONT_FAMILY
from src.action_logger import ActionLogger


# ============================================================
# 异步桥接（让 async 任务在后台线程安全执行）
# ============================================================

class AsyncRunner(QThread):
    """QThread 子类，在独立线程中运行 async 函数，完成后发射信号回主线程。"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, coro_factory, timeout: float | None = None, parent=None):
        """coro_factory: 返回协程的可调用对象。
           timeout: 超时秒数，None 表示不限制（用于长时间爬取任务）。
        """
        super().__init__(parent)
        self._coro_factory = coro_factory
        self._timeout = timeout

    def run(self):
        from src.action_logger import ActionLogger
        alog = ActionLogger.get()
        alog.log("异步任务", "AsyncRunner.run() 开始",
            details=f"timeout={self._timeout}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            coro = self._coro_factory()
            alog.log("异步任务", f"协程已创建: {type(coro).__name__}", "执行中…")
            if self._timeout is not None:
                result = loop.run_until_complete(asyncio.wait_for(coro, timeout=self._timeout))
            else:
                result = loop.run_until_complete(coro)
            alog.log("异步任务", "协程执行完成", "成功")
            self.finished.emit(result)
        except asyncio.TimeoutError:
            alog.log("异步任务", f"协程执行超时 (>{self._timeout}s)", "超时",
                error="asyncio.TimeoutError")
            self.error.emit(f"请求超时（{self._timeout:.0f}秒），请检查网络连接")
        except Exception as e:
            alog.log("异步任务", f"协程执行异常: {type(e).__name__}", "失败", error=str(e))
            self.error.emit(str(e))
        finally:
            alog.log("异步任务", "事件循环关闭")
            loop.close()


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):

    # 跨线程进度信号（worker 线程 → 主线程 UI 更新）
    _progress_signal = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bilibili ADs Flak")
        self.resize(1100, 780)
        self.setMinimumSize(900, 600)

        # 窗口图标
        icon_path = Path(__file__).parent.parent.parent / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # 状态
        self._config = Config.from_env()
        self._dark_mode = False
        self._current_theme = Light()
        self._comments: list[Comment] = []
        self._oid: int | None = None
        self._video_title: str = ""
        self._judgments: BatchAdJudgment | None = None
        self._network_mgr = QNetworkAccessManager(self)
        self._bg_threads: list[QThread] = []  # 持有引用防止 GC 回收
        self._alog = ActionLogger.get()  # 操作日志

        self._init_ui()
        self._apply_theme()

        # 跨线程进度信号 → 安全更新 UI
        self._progress_signal.connect(self._on_progress_update)

        # 启动日志
        self._alog.log("GUI启动", "应用初始化",
            f"auth_mode={self._config.auth_mode}, "
            f"sessdata={'已设置' if self._config.sessdata else '无'}, "
            f"deepseek={'已设置' if self._config.deepseek_api_key else '无'}")

        # 启动时尝试加载用户信息
        if self._config.auth_mode == "cookie" and self._config.sessdata:
            self._load_user_info()
        else:
            self._on_user_info_failed("无 Cookie")

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
        """品牌标题栏 + 用户信息。"""
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

        # 用户头像 + ID
        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(28, 28)
        self._avatar_label.setStyleSheet(
            "border-radius: 14px; background: #E2E4E8;"
        )
        self._avatar_label.setVisible(False)
        bar.addWidget(self._avatar_label)

        self._user_label = QLabel("未登录")
        self._user_label.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.TEXT_TERTIARY};"
        )
        bar.addWidget(self._user_label)
        bar.addSpacing(SPACING["md"])

        # Cookie 按钮
        self._btn_auto_cookie = QPushButton("自动导入 Cookie")
        self._btn_auto_cookie.clicked.connect(self._import_cookies_auto)
        bar.addWidget(self._btn_auto_cookie)

        self._btn_manual_cookie = QPushButton("手动导入")
        self._btn_manual_cookie.clicked.connect(self._import_cookies_manual)
        bar.addWidget(self._btn_manual_cookie)

        self._dark_btn = QPushButton("🌙 暗色模式")
        self._dark_btn.setObjectName("dark_toggle")
        self._dark_btn.clicked.connect(self._toggle_dark_mode)
        bar.addWidget(self._dark_btn)

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
        self._table.setColumnWidth(0, 55)
        self._table.setColumnWidth(1, 120)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 80)
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
        self._run_async_with_result(
            lambda: self._do_preview(bv),
            self._on_preview_done,
            self._on_preview_error,
            timeout=10,
        )

    async def _do_preview(self, bv_id: str):
        from src.service import CrawlerService
        svc = CrawlerService(self._config)
        info = await svc.get_video_info(bv_id)
        return info  # 交给 _on_preview_done 在主线程处理

    def _on_preview_done(self, info):
        self._video_title = info.title
        self._status_bar.showMessage(f"预览: {info.title}  |  评论数: {info.total_comments}")
        QMessageBox.information(self, "视频预览",
            f"标题: {info.title}\n评论总数: {info.total_comments}")

    def _on_preview_error(self, err: str):
        self._status_bar.showMessage(f"预览失败: {err}")
        QMessageBox.warning(self, "预览失败", err)

    def _on_crawl(self):
        bv = self._bv_input.text().strip()
        if not bv:
            return
        self._alog.log("爬取评论", f"用户点击「开始爬取」, BV={bv}")
        self._btn_crawl.setEnabled(False)
        self._btn_preview.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._btn_detect.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._table.setRowCount(0)
        self._judgments = None

        # 爬取无超时限制（可能很长），完成后在主线程处理结果
        self._run_async_with_result(
            lambda: self._do_crawl(bv),
            self._on_crawl_done,
            self._on_crawl_error,
            timeout=None,
        )

    def _on_progress_update(self, ev: ProgressEvent):
        """主线程中安全更新进度 UI。"""
        self._progress.setMaximum(ev.estimated_total or 0)
        self._progress.setValue(ev.total_crawled)
        self._status_bar.showMessage(ev.message)

    async def _do_crawl(self, bv_id: str):
        """在后台线程中运行的爬取协程。不直接操作 UI，通过信号发送进度。"""
        from src.service import CrawlerService
        svc = CrawlerService(self._config)

        def on_progress(ev: ProgressEvent):
            # 通过信号安全发送到主线程
            self._progress_signal.emit(ev)

        result: CrawlResult = await svc.crawl(bv_id, on_progress=on_progress)
        return result  # 结果交给 _on_crawl_done 在主线程处理

    def _on_crawl_done(self, result: CrawlResult):
        """爬取完成回调（主线程）。"""
        self._comments = result.comments
        self._video_title = result.video_title
        self._refresh_table()
        self._status_bar.showMessage(
            f"爬取完成: {result.video_title}  |  {result.total_count} 条  |  {result.crawl_time:.1f}s"
        )
        self._btn_crawl.setEnabled(True)
        self._btn_preview.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._btn_detect.setEnabled(len(self._comments) > 0)
        self._progress.setVisible(False)
        self._alog.log("爬取评论", f"爬取完成: {result.video_title}",
            f"{result.total_count}条, {result.crawl_time:.1f}s",
            error="; ".join(result.errors) if result.errors else None)

    def _on_crawl_error(self, err: str):
        """爬取出错回调（主线程）。"""
        self._status_bar.showMessage(f"爬取出错: {err}")
        self._btn_crawl.setEnabled(True)
        self._btn_preview.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._progress.setVisible(False)
        self._alog.log("爬取评论", "爬取出错", "失败", error=err)
        QMessageBox.critical(self, "爬取出错", err)

    def _on_cancel(self):
        from src.service import CrawlerService
        self._alog.log("爬取评论", "用户点击「取消」")
        self._status_bar.showMessage("已请求取消…")

    def _on_detect(self):
        if not self._comments:
            return
        self._alog.log("AI检测", f"用户点击「检测广告评论」, {len(self._comments)}条评论")
        self._btn_detect.setEnabled(False)
        self._detect_status.setText("检测中…")
        self._detect_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_BLUE};")
        self._run_async_with_result(
            lambda: self._do_detect(),
            self._on_detect_done,
            self._on_detect_error,
            timeout=60,
        )

    async def _do_detect(self):
        """在后台线程中运行的 AI 检测协程。"""
        from src.llm.deepseek import DeepSeekClient
        from src.agent.ad_detector import AdDetector

        client = DeepSeekClient(api_key=self._config.deepseek_api_key)
        detector = AdDetector(client)

        # 展平树形结构，二级评论（楼中楼）也纳入检测
        all_comments: list[dict] = []
        def walk(comments):
            for c in comments:
                all_comments.append({
                    "rpid": str(c.rpid),
                    "content": c.content,
                    "parent_id": str(c.parent_id or ""),
                })
                if c.replies:
                    walk(c.replies)
        walk(self._comments)

        judgments = await detector.detect(all_comments)
        return judgments  # 交给 _on_detect_done 在主线程处理

    def _on_detect_done(self, judgments: BatchAdJudgment):
        """AI 检测完成回调（主线程）。"""
        self._judgments = judgments
        ad_count = sum(1 for j in judgments.judgments if j.is_ad)
        self._detect_status.setText(f"检测完成: {ad_count}/{len(judgments.judgments)} 条广告")
        self._detect_status.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED if ad_count > 0 else self._current_theme.BRAND_GREEN};"
        )
        self._refresh_table(show_judgments=True)
        self._btn_detect.setEnabled(True)
        self._btn_delete.setEnabled(ad_count > 0)
        self._alog.log("AI检测", f"检测完成: {self._video_title}",
            f"{ad_count}/{len(judgments.judgments)}条广告")

    def _on_detect_error(self, err: str):
        """AI 检测出错回调（主线程）。"""
        self._detect_status.setText(f"检测失败: {err}")
        self._detect_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED};")
        self._btn_detect.setEnabled(True)
        self._alog.log("AI检测", "检测出错", "失败", error=err)
        QMessageBox.critical(self, "AI 检测失败", err)

    def _on_delete(self):
        if not self._judgments:
            return
        dry_run = self._dry_run_check.isChecked()
        ad_count = sum(1 for j in self._judgments.judgments if j.is_ad)
        self._alog.log("删除操作", f"用户点击「删除广告评论」, dry_run={dry_run}, 共{ad_count}条")
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
        self._run_async_with_result(
            lambda: self._do_delete(dry_run),
            lambda r: self._on_delete_done(r, dry_run),
            self._on_delete_error,
            timeout=120,
        )

    async def _do_delete(self, dry_run: bool):
        """在后台线程中运行的删除协程。"""
        from src.deleter.deleter import AdDeleter

        bv = self._bv_input.text().strip()
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
        return result  # 交给 _on_delete_done 在主线程处理

    def _on_delete_done(self, result, dry_run: bool):
        """删除完成回调（主线程）。"""
        bv = self._bv_input.text().strip()

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

        self._alog.log("删除操作", f"删除完成: {bv}",
            f"成功{result.success_count}/失败{result.failed_count}/跳过{result.skipped_count}, dry_run={dry_run}")

        self._btn_delete.setEnabled(True)

    def _on_delete_error(self, err: str):
        """删除出错回调（主线程）。"""
        self._delete_status.setText(f"删除失败: {err}")
        self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED};")
        self._btn_delete.setEnabled(True)
        self._alog.log("删除操作", "删除出错", "失败", error=err)
        QMessageBox.critical(self, "删除失败", err)

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

    # ==================== Cookie 导入 + 用户信息 ====================

    def _import_cookies_auto(self):
        """从浏览器自动导入 Cookie。"""
        self._alog.log("Cookie导入", "用户点击「自动导入 Cookie」")
        try:
            from src.cookie_importer import import_cookies, save_to_env
            cookies = import_cookies()
            save_to_env(cookies)
            self._config = Config.from_env()
            self._status_bar.showMessage("Cookie 已保存，正在验证…")
            self._alog.log("Cookie导入", "从浏览器成功读取并写入 .env",
                details=f"SESSDATA长度={len(cookies.sessdata)}, bili_jct长度={len(cookies.bili_jct)}")

            # 异步验证（避免嵌套 event loop 导致闪退）
            self._verify_and_login()
        except Exception as e:
            self._alog.log("Cookie导入", "自动导入失败", "失败", error=str(e))
            QMessageBox.warning(self, "导入失败", str(e))

    def _import_cookies_manual(self):
        """手动输入 SESSDATA 和 bili_jct。"""
        self._alog.log("Cookie导入", "用户点击「手动导入」")
        sessdata, ok1 = QInputDialog.getText(
            self, "手动导入 Cookie", "请输入 SESSDATA:",
            QLineEdit.EchoMode.Normal, ""
        )
        if not ok1 or not sessdata.strip():
            return

        bili_jct, ok2 = QInputDialog.getText(
            self, "手动导入 Cookie", "请输入 bili_jct:",
            QLineEdit.EchoMode.Normal, ""
        )
        if not ok2 or not bili_jct.strip():
            return

        from src.cookie_importer import CookiePair, save_to_env
        save_to_env(CookiePair(sessdata=sessdata.strip(), bili_jct=bili_jct.strip()))
        self._config = Config.from_env()
        self._status_bar.showMessage("Cookie 已保存，正在验证…")
        self._verify_and_login()

    def _verify_and_login(self):
        """在后台线程验证凭证并更新 UI（导入 Cookie 后调用，弹窗告知结果）。"""
        from bilibili_api import user as bv_user, Credential
        cred = Credential(sessdata=self._config.sessdata, bili_jct=self._config.bili_jct)
        self._alog.log("验证凭证", "_verify_and_login 发起异步验证",
            details=f"SESSDATA={self._config.sessdata[:10]}…")

        self._run_async_with_result(
            lambda: bv_user.get_self_info(credential=cred),
            self._on_verify_ok,
            self._on_verify_fail,
            timeout=10,
        )

    def _on_verify_ok(self, info: dict):
        name = info.get("name", "")
        face = info.get("face", "")
        self._update_user_display({"name": name, "face": face})
        self._status_bar.showMessage(f"已登录: {name}")
        self._alog.log("验证凭证", "user.get_self_info 调用成功",
            f"已登录: {name}",
            details=f"name={name}, mid={info.get('mid')}, level={info.get('level')}")
        QMessageBox.information(self, "导入成功",
            f"Cookie 已写入 .env\n当前用户: {name}")

    def _on_verify_fail(self, err: str):
        self._user_label.setText("凭证无效")
        self._user_label.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED};"
        )
        self._avatar_label.setVisible(False)
        self._highlight_cookie_buttons()
        self._status_bar.showMessage("Cookie 验证失败，请重试")
        self._alog.log("验证凭证", "调用 B站 user.get_self_info", "失败", error=err)
        QMessageBox.warning(self, "凭证无效",
            f"Cookie 已写入 .env，但验证失败:\n{err}\n\n"
            "请确认浏览器已登录 bilibili.com，然后重试。")

    def _load_user_info(self):
        """启动时静默加载 B站 用户头像和昵称。失败则显示"未登录"。"""
        if not self._config.sessdata or not self._config.bili_jct:
            self._alog.log("加载用户", ".env 中无 SESSDATA/bili_jct", "跳过")
            self._on_user_info_failed("无 SESSDATA/bili_jct")
            return
        if self._config.sessdata.startswith("test_") or self._config.sessdata.startswith("在此"):
            self._alog.log("加载用户", "SESSDATA 为占位符", "跳过")
            self._on_user_info_failed("SESSDATA 为占位符")
            return

        from bilibili_api import user, Credential
        cred = Credential(sessdata=self._config.sessdata, bili_jct=self._config.bili_jct)
        self._alog.log("加载用户", "启动时静默调用 user.get_self_info")
        logger.info("正在加载用户信息…")

        self._run_async_with_result(
            lambda: user.get_self_info(credential=cred),
            self._update_user_display,
            lambda e: self._on_user_info_failed(e),
            timeout=10,
        )

    def _on_user_info_failed(self, err: str):
        logger.error(f"获取用户信息失败: {err}")
        self._alog.log("加载用户", "user.get_self_info 失败", "显示未登录", error=err)
        self._user_label.setText("未登录")
        self._user_label.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.TEXT_TERTIARY};"
        )
        self._avatar_label.setVisible(False)
        self._highlight_cookie_buttons()

    def _update_user_display(self, info: dict):
        """更新顶栏用户头像和昵称。"""
        name = info.get("name", "")
        face_url = info.get("face", "")

        if name:
            self._user_label.setText(name)
            self._user_label.setStyleSheet(
                f"font-size:{FONT_SIZES['small']}; font-weight:{FONT_WEIGHTS['medium']}; "
                f"color:{self._current_theme.TEXT_PRIMARY};"
            )
            self._reset_cookie_button_style()
            logger.info(f"用户信息已加载: {name}")
        else:
            logger.warning("用户信息无 name 字段")

        if face_url:
            self._avatar_label.setVisible(True)
            self._fetch_avatar(face_url)

    # ==================== Cookie 按钮高亮 ====================

    def _highlight_cookie_buttons(self):
        """未登录时高亮 Cookie 导入按钮，引导用户操作。"""
        radius = "6px" if self._dark_mode else "8px"
        style = (
            f"font-weight:{FONT_WEIGHTS['bold']}; "
            f"border: 2px solid {self._current_theme.BRAND_PINK}; "
            f"color: {self._current_theme.BRAND_PINK}; "
            f"padding: 4px 12px; border-radius: {radius};"
        )
        self._btn_auto_cookie.setStyleSheet(style)
        self._btn_manual_cookie.setStyleSheet(style)

    def _reset_cookie_button_style(self):
        """登录成功后恢复按钮默认样式。"""
        self._btn_auto_cookie.setStyleSheet("")
        self._btn_manual_cookie.setStyleSheet("")

    def _fetch_avatar(self, url: str):
        """通过网络请求加载头像。"""
        req = QNetworkRequest(QUrl(url))
        reply = self._network_mgr.get(req)

        def on_finished():
            data = reply.readAll()
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            if not pixmap.isNull():
                scaled = pixmap.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                self._avatar_label.setPixmap(scaled)

        reply.finished.connect(on_finished)

    def _run_async(self, coro_factory, timeout: float | None = None):
        """在后台线程中运行异步任务，完成后在主线程回调。"""
        self._alog.log("异步任务", "_run_async 创建线程")
        self._runner = AsyncRunner(coro_factory, timeout=timeout)
        self._bg_threads.append(self._runner)  # 持有引用防止 GC 回收

        def on_finished(result):
            if self._runner in self._bg_threads:
                self._bg_threads.remove(self._runner)
        def on_error(err):
            self._status_bar.showMessage(f"错误: {err}")
            QMessageBox.critical(self, "错误", err)
            if self._runner in self._bg_threads:
                self._bg_threads.remove(self._runner)

        self._runner.finished.connect(on_finished)
        self._runner.error.connect(on_error)
        self._runner.start()
        self._alog.log("异步任务", "QThread 已启动")

    def _run_async_with_result(self, coro_factory, on_done, on_error, timeout: float | None = None):
        """运行异步任务，完成后用自定义回调处理结果。"""
        self._alog.log("异步任务", "_run_async_with_result 创建线程")
        runner = AsyncRunner(coro_factory, timeout=timeout)
        self._bg_threads.append(runner)        # 持有引用

        def cleanup():
            if runner in self._bg_threads:
                self._bg_threads.remove(runner)

        runner.finished.connect(on_done)
        runner.error.connect(on_error)
        runner.finished.connect(lambda _: cleanup())
        runner.error.connect(lambda _: cleanup())
        runner.start()
        self._alog.log("异步任务", "QThread 已启动")
