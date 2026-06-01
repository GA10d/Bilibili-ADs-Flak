"""主窗口 — 评论爬取 → AI 检测 → 删除，一站式操作。"""

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path

import requests
from loguru import logger
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QUrl, QSignalBlocker
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QSplitter, QStatusBar, QMessageBox,
    QGroupBox, QCheckBox, QSpinBox, QTabWidget, QComboBox, QDoubleSpinBox, QStackedWidget,
    QTextEdit, QApplication, QInputDialog, QDialog, QAbstractSpinBox,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from src.config import Config, ensure_env_file, resource_path
from src.crawler.models import Comment, ProgressEvent, CrawlResult
from src.agent.ad_detector import BatchAdJudgment, CommentAdJudgment
from src.gui.theme import Light, Dark, build_stylesheet, SPACING, FONT_SIZES, FONT_WEIGHTS, FONT_FAMILY
from src.action_logger import ActionLogger
from src.whitelist import WhitelistManager


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
    _detect_batch_signal = pyqtSignal(object)  # 每批检测结果
    _comments_batch_signal = pyqtSignal(object)  # 每页爬到的评论

    def __init__(self, preloaded_submissions: dict | None = None):
        super().__init__()
        self.setWindowTitle("Bilibili ADs Flak")
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)

        # 窗口图标
        icon_path = resource_path("icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # 状态
        ensure_env_file()
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
        self._whitelist = WhitelistManager()  # 白名单
        self._manual_toggle = False  # 手动修改模式
        self._show_ads_only = False  # 只看广告
        self._crawl_delay_seconds = 1.0
        self._ai_concurrency = 100
        self._delete_rate_per_minute = 10
        self._current_user_mid: int | None = None
        self._submission_page_size = 10
        self._submission_current_page = 1
        self._submission_total_count = 0
        self._submission_total_pages = 0
        self._submission_pages: dict[int, list[dict]] = {}
        self._submission_loading_pages: set[int] = set()
        self._submission_owner_mid: int | None = None
        self._preloaded_submissions = preloaded_submissions

        self._init_ui()
        self._apply_theme()

        # 跨线程进度信号 → 安全更新 UI
        self._progress_signal.connect(self._on_progress_update)
        self._detect_batch_signal.connect(self._on_detect_batch)
        self._comments_batch_signal.connect(self._on_comments_batch)

        # 启动日志
        self._alog.log("GUI启动", "应用初始化",
            f"auth_mode={self._config.auth_mode}, "
            f"sessdata={'已设置' if self._config.sessdata else '无'}, "
            f"deepseek={'已设置' if self._config.deepseek_api_key else '无'}, "
            f"model={self._config.deepseek_model}")

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
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_top_bar())

        # ---- 主工作区 ----
        workspace = QFrame()
        workspace.setObjectName("workspace")
        content = QVBoxLayout(workspace)
        content.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], SPACING["md"])
        content.setSpacing(SPACING["md"])

        self._workflow_stack = QStackedWidget()
        self._workflow_stack.addWidget(self._build_submission_flow())
        self._workflow_stack.addWidget(self._build_comment_flow())
        content.addWidget(self._workflow_stack, 1)
        root.addWidget(workspace, 1)

        # ---- 状态栏 ----
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("就绪  |  双击 .bat 可导入 Cookie")

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACING["lg"], SPACING["sm"], SPACING["lg"], SPACING["sm"])
        layout.setSpacing(SPACING["md"])

        logo = QLabel()
        icon_path = resource_path("icon.png")
        if icon_path.exists():
            pixmap = QPixmap(str(icon_path)).scaled(
                44, 44,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo.setPixmap(pixmap)
        logo.setFixedSize(48, 48)
        layout.addWidget(logo)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title = QLabel("Bilibili ADs Flak")
        title.setObjectName("appTitle")
        subtitle = QLabel("投稿快照、评论清理与广告复核")
        subtitle.setObjectName("appSubtitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        layout.addLayout(title_col)

        layout.addStretch()
        self._top_account = QFrame()
        self._top_account.setObjectName("metricCard")
        account_layout = QHBoxLayout(self._top_account)
        account_layout.setContentsMargins(SPACING["sm"], SPACING["xs"], SPACING["md"], SPACING["xs"])
        account_layout.setSpacing(SPACING["sm"])
        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(32, 32)
        self._avatar_label.setVisible(False)
        account_layout.addWidget(self._avatar_label)
        self._user_label = QLabel("未登录")
        account_layout.addWidget(self._user_label)
        self._top_account.setMinimumWidth(220)
        layout.addWidget(self._top_account)

        self._dark_btn = QPushButton("🌙 暗色模式")
        self._dark_btn.setObjectName("dark_toggle")
        self._dark_btn.clicked.connect(self._toggle_dark_mode)
        layout.addWidget(self._dark_btn)
        return bar

    def _build_submission_flow(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING["md"])
        layout.addWidget(self._build_submission_panel(), 1)
        return page

    def _build_comment_flow(self) -> QWidget:
        page = QWidget()
        content = QVBoxLayout(page)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(SPACING["md"])

        top = QHBoxLayout()
        top.setSpacing(SPACING["md"])
        top.addWidget(self._build_control_panel(), 2)
        top.addWidget(self._build_summary_panel(), 1)
        content.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_table_panel())
        splitter.addWidget(self._build_action_panel())
        splitter.setSizes([520, 190])
        splitter.setChildrenCollapsible(False)
        content.addWidget(splitter, 1)
        return page

    def _build_header(self) -> QWidget:
        """左侧品牌、账号和全局设置。"""
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(260)

        bar = QVBoxLayout(side)
        bar.setContentsMargins(SPACING["md"], SPACING["lg"], SPACING["md"], SPACING["md"])
        bar.setSpacing(SPACING["sm"])

        title = QLabel("Bilibili ADs Flak")
        title.setObjectName("appTitle")
        bar.addWidget(title)
        subtitle = QLabel("评论爬取、广告识别与清理工作台")
        subtitle.setObjectName("appSubtitle")
        subtitle.setWordWrap(True)
        bar.addWidget(subtitle)
        bar.addSpacing(SPACING["lg"])

        # 用户头像 + ID
        account_title = QLabel("账号")
        account_title.setObjectName("sectionCaption")
        bar.addWidget(account_title)

        account = QFrame()
        account.setObjectName("metricCard")
        account_layout = QHBoxLayout(account)
        account_layout.setContentsMargins(SPACING["sm"], SPACING["sm"], SPACING["sm"], SPACING["sm"])
        account_layout.setSpacing(SPACING["sm"])
        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(36, 36)
        self._avatar_label.setStyleSheet(
            "border-radius: 18px; background: #E2E4E8;"
        )
        self._avatar_label.setVisible(False)
        account_layout.addWidget(self._avatar_label)

        self._user_label = QLabel("未登录")
        self._user_label.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.TEXT_TERTIARY};"
        )
        account_layout.addWidget(self._user_label, 1)
        bar.addWidget(account)

        # Cookie 按钮
        self._btn_auto_cookie = QPushButton("自动导入 Cookie")
        self._btn_auto_cookie.clicked.connect(self._import_cookies_auto)
        bar.addWidget(self._btn_auto_cookie)

        self._btn_manual_cookie = QPushButton("手动导入")
        self._btn_manual_cookie.clicked.connect(self._import_cookies_manual)
        bar.addWidget(self._btn_manual_cookie)

        bar.addSpacing(SPACING["lg"])
        flow_title = QLabel("流程")
        flow_title.setObjectName("sectionCaption")
        bar.addWidget(flow_title)

        self._btn_video_flow = QPushButton("投稿快照")
        self._btn_video_flow.setCheckable(True)
        self._btn_video_flow.setChecked(True)
        self._btn_video_flow.clicked.connect(lambda: self._switch_flow(0))
        bar.addWidget(self._btn_video_flow)

        self._btn_comment_flow = QPushButton("评论清理")
        self._btn_comment_flow.setCheckable(True)
        self._btn_comment_flow.clicked.connect(lambda: self._switch_flow(1))
        bar.addWidget(self._btn_comment_flow)

        bar.addSpacing(SPACING["lg"])
        ai_title = QLabel("AI 配置")
        ai_title.setObjectName("sectionCaption")
        bar.addWidget(ai_title)

        # 模型选择
        self._model_combo = QComboBox()
        self._model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])
        idx = self._model_combo.findText(self._config.deepseek_model)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        bar.addWidget(self._model_combo)

        # API Key 按钮
        self._btn_api_key = QPushButton("API Key")
        self._btn_api_key.clicked.connect(self._on_api_key)
        bar.addWidget(self._btn_api_key)

        self._btn_settings = QPushButton("设置")
        self._btn_settings.clicked.connect(self._on_settings)
        bar.addWidget(self._btn_settings)

        bar.addStretch()

        self._dark_btn = QPushButton("🌙 暗色模式")
        self._dark_btn.setObjectName("dark_toggle")
        self._dark_btn.clicked.connect(self._toggle_dark_mode)
        bar.addWidget(self._dark_btn)

        return side

    def _switch_flow(self, index: int):
        if not hasattr(self, "_workflow_stack"):
            return
        self._workflow_stack.setCurrentIndex(index)
        if hasattr(self, "_btn_video_flow"):
            self._btn_video_flow.setChecked(index == 0)
        if hasattr(self, "_btn_comment_flow"):
            self._btn_comment_flow.setChecked(index == 1)
        self._refresh_theme_bound_styles()

    def _build_compact_settings_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("metricCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        account_title = QLabel("账号")
        account_title.setObjectName("sectionCaption")
        layout.addWidget(account_title)

        account = QFrame()
        account.setObjectName("metricCard")
        account_layout = QHBoxLayout(account)
        account_layout.setContentsMargins(SPACING["sm"], SPACING["sm"], SPACING["sm"], SPACING["sm"])
        account_layout.setSpacing(SPACING["sm"])
        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(36, 36)
        self._avatar_label.setVisible(False)
        account_layout.addWidget(self._avatar_label)
        self._user_label = QLabel("未登录")
        account_layout.addWidget(self._user_label, 1)
        layout.addWidget(account)

        self._btn_auto_cookie = QPushButton("自动导入 Cookie")
        self._btn_auto_cookie.clicked.connect(self._import_cookies_auto)
        layout.addWidget(self._btn_auto_cookie)

        self._btn_manual_cookie = QPushButton("手动导入")
        self._btn_manual_cookie.clicked.connect(self._import_cookies_manual)
        layout.addWidget(self._btn_manual_cookie)

        flow_title = QLabel("流程")
        flow_title.setObjectName("sectionCaption")
        layout.addWidget(flow_title)

        self._btn_video_flow = QPushButton("投稿快照")
        self._btn_video_flow.setCheckable(True)
        self._btn_video_flow.setChecked(True)
        self._btn_video_flow.clicked.connect(lambda: self._switch_flow(0))
        layout.addWidget(self._btn_video_flow)

        self._btn_comment_flow = QPushButton("评论清理")
        self._btn_comment_flow.setCheckable(True)
        self._btn_comment_flow.clicked.connect(lambda: self._switch_flow(1))
        layout.addWidget(self._btn_comment_flow)

        ai_title = QLabel("AI 配置")
        ai_title.setObjectName("sectionCaption")
        layout.addWidget(ai_title)

        self._model_combo = QComboBox()
        self._model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])
        idx = self._model_combo.findText(self._config.deepseek_model)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addWidget(self._model_combo)

        self._btn_api_key = QPushButton("API Key")
        self._btn_api_key.clicked.connect(self._on_api_key)
        layout.addWidget(self._btn_api_key)

        self._btn_settings = QPushButton("设置")
        self._btn_settings.clicked.connect(self._on_settings)
        layout.addWidget(self._btn_settings)

        return panel

    def _build_control_panel(self) -> QFrame:
        """爬取控制面板：BV 输入 + 进度。"""
        card = QFrame()
        card.setObjectName("heroCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        # 标题行
        header = QHBoxLayout()
        h = QLabel("评论爬取")
        h.setObjectName("sectionTitle")
        header.addWidget(h)
        header.addStretch()
        layout.addLayout(header)

        caption = QLabel("输入 BV 号后先预览确认，再开始爬取；爬取结果会实时进入下方表格。")
        caption.setObjectName("sectionCaption")
        caption.setWordWrap(True)
        layout.addWidget(caption)

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

    def _build_submission_panel(self) -> QFrame:
        """当前登录用户的投稿视频分页表格。"""
        page = QFrame()
        page.setObjectName("workspace")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING["md"])

        stats_card = QFrame()
        stats_card.setObjectName("card")
        stats_layout = QVBoxLayout(stats_card)
        stats_layout.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], SPACING["md"])
        stats_layout.setSpacing(SPACING["md"])

        stats_title = QLabel("数据统计")
        stats_title.setObjectName("sectionTitle")
        stats_layout.addWidget(stats_title)

        stats = QHBoxLayout()
        stats.setSpacing(SPACING["sm"])
        self._submission_total_metric = self._build_metric("0", "投稿数")
        self._submission_play_delta_metric = self._build_metric("0", "播放新增")
        self._submission_comment_delta_metric = self._build_metric("0", "评论新增")
        for metric in (
            self._submission_total_metric,
            self._submission_play_delta_metric,
            self._submission_comment_delta_metric,
        ):
            stats.addWidget(metric)
        stats_layout.addLayout(stats)
        layout.addWidget(stats_card)

        list_card = QFrame()
        list_card.setObjectName("card")
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(SPACING["lg"], SPACING["lg"], SPACING["lg"], SPACING["md"])
        list_layout.setSpacing(SPACING["md"])

        title = QLabel("投稿视频")
        title.setObjectName("sectionTitle")
        list_layout.addWidget(title)

        hidden_controls = QWidget()
        hidden_layout = QVBoxLayout(hidden_controls)
        self._submission_status = QLabel("登录后自动生成本次完整快照")
        self._btn_submission_refresh = QPushButton("刷新")
        self._btn_submission_refresh.clicked.connect(self._refresh_submissions)
        self._btn_submission_refresh.setEnabled(False)
        self._btn_submission_prev = QPushButton("上一页")
        self._btn_submission_prev.clicked.connect(lambda: self._show_submission_page(self._submission_current_page - 1))
        self._btn_submission_prev.setEnabled(False)
        self._submission_page_label = QLabel("第 0/0 页")
        self._btn_submission_next = QPushButton("下一页")
        self._btn_submission_next.clicked.connect(lambda: self._show_submission_page(self._submission_current_page + 1))
        self._btn_submission_next.setEnabled(False)
        for widget in (
            self._submission_status,
            self._btn_submission_refresh,
            self._submission_page_label,
        ):
            hidden_layout.addWidget(widget)
        hidden_controls.hide()
        list_layout.addWidget(hidden_controls)

        self._submission_table = QTableWidget(0, 8)
        self._submission_table.setHorizontalHeaderLabels(["BV", "标题", "播放", "Δ播放", "评论", "Δ评论", "时长", "发布时间"])
        self._submission_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._submission_table.setColumnWidth(0, 130)
        self._submission_table.setColumnWidth(2, 80)
        self._submission_table.setColumnWidth(3, 75)
        self._submission_table.setColumnWidth(4, 70)
        self._submission_table.setColumnWidth(5, 70)
        self._submission_table.setColumnWidth(6, 70)
        self._submission_table.setColumnWidth(7, 110)
        self._submission_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._submission_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._submission_table.verticalHeader().setVisible(False)
        self._submission_table.verticalHeader().setDefaultSectionSize(34)
        self._submission_table.setAlternatingRowColors(True)
        self._submission_table.setWordWrap(False)
        self._submission_table.cellDoubleClicked.connect(self._on_submission_double_clicked)
        self._fit_submission_table_height()
        list_layout.addWidget(self._submission_table)

        pager = QHBoxLayout()
        pager.setSpacing(SPACING["md"])
        pager.addStretch()
        self._btn_submission_prev.setMinimumWidth(96)
        self._btn_submission_next.setMinimumWidth(96)
        pager.addWidget(self._btn_submission_prev)
        pager.addWidget(self._btn_submission_next)
        pager.addStretch()
        list_layout.addLayout(pager)
        layout.addWidget(list_card)

        return page

    def _build_submission_controls_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("metricCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        title = QLabel("投稿快照")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self._submission_status = QLabel("登录后自动生成本次完整快照")
        self._submission_status.setObjectName("sectionCaption")
        self._submission_status.setWordWrap(True)
        layout.addWidget(self._submission_status)

        pager = QHBoxLayout()
        self._btn_submission_refresh = QPushButton("刷新")
        self._btn_submission_refresh.clicked.connect(self._refresh_submissions)
        self._btn_submission_refresh.setEnabled(False)
        pager.addWidget(self._btn_submission_refresh)

        self._btn_submission_prev = QPushButton("上一页")
        self._btn_submission_prev.clicked.connect(lambda: self._show_submission_page(self._submission_current_page - 1))
        self._btn_submission_prev.setEnabled(False)
        pager.addWidget(self._btn_submission_prev)
        layout.addLayout(pager)

        pager2 = QHBoxLayout()
        self._submission_page_label = QLabel("第 0/0 页")
        self._submission_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pager2.addWidget(self._submission_page_label, 1)

        self._btn_submission_next = QPushButton("下一页")
        self._btn_submission_next.clicked.connect(lambda: self._show_submission_page(self._submission_current_page + 1))
        self._btn_submission_next.setEnabled(False)
        pager2.addWidget(self._btn_submission_next)
        layout.addLayout(pager2)

        metrics = QVBoxLayout()
        metrics.setSpacing(SPACING["sm"])
        self._submission_total_metric = self._build_metric("0", "投稿数")
        self._submission_play_delta_metric = self._build_metric("0", "播放新增")
        self._submission_comment_delta_metric = self._build_metric("0", "评论新增")
        metrics.addWidget(self._submission_total_metric)
        metrics.addWidget(self._submission_play_delta_metric)
        metrics.addWidget(self._submission_comment_delta_metric)
        layout.addLayout(metrics)
        return panel

    def _build_summary_panel(self) -> QFrame:
        """当前任务摘要。"""
        card = QFrame()
        card.setObjectName("heroCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        title = QLabel("任务摘要")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        grid = QHBoxLayout()
        grid.setSpacing(SPACING["sm"])
        self._summary_comments = self._build_metric("0", "评论")
        self._summary_ads = self._build_metric("0", "广告")
        self._summary_deletable = self._build_metric("0", "可删除")
        grid.addWidget(self._summary_comments)
        grid.addWidget(self._summary_ads)
        grid.addWidget(self._summary_deletable)
        layout.addLayout(grid)

        self._summary_status = QLabel("等待爬取")
        self._summary_status.setObjectName("sectionCaption")
        self._summary_status.setWordWrap(True)
        layout.addWidget(self._summary_status)
        return card

    def _build_metric(self, value: str, label: str) -> QFrame:
        metric = QFrame()
        metric.setObjectName("metricCard")
        metric.setMinimumWidth(76)
        layout = QVBoxLayout(metric)
        layout.setContentsMargins(SPACING["sm"], SPACING["sm"], SPACING["sm"], SPACING["sm"])
        layout.setSpacing(0)
        value_label = QLabel(value)
        value_label.setObjectName("metricValue")
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label_widget = QLabel(label)
        label_widget.setObjectName("metricLabel")
        label_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(value_label)
        layout.addWidget(label_widget)
        metric._value_label = value_label
        return metric

    def _build_table_panel(self) -> QFrame:
        """评论表格。"""
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["sm"])
        layout.setSpacing(SPACING["sm"])

        title = QLabel("评论列表 (0 条)")
        title.setObjectName("table_title")
        title.setStyleSheet(f"font-size:{FONT_SIZES['h3']}; font-weight:{FONT_WEIGHTS['semibold']};")
        self._table_title = title
        layout.addWidget(title)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(["序号", "用户名", "内容", "点赞", "广告", "判定理由", "评论白名单"])
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 55)
        self._table.setColumnWidth(1, 120)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 95)
        self._table.setColumnWidth(6, 95)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.cellClicked.connect(self._on_table_cell_clicked)
        self._table.itemChanged.connect(self._on_table_item_changed)
        layout.addWidget(self._table, 1)

        return card

    def _build_action_panel(self) -> QFrame:
        """操作面板：AI 检测 + 白名单 + 手动修改 + 删除。"""
        card = QFrame()
        card.setObjectName("card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(SPACING["md"], SPACING["sm"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["md"])

        # 左侧：检测
        detect_group = QGroupBox("AI 广告检测")
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

        # 中间：白名单 + 手动修改
        tools_group = QGroupBox("复核工具")
        tl = QVBoxLayout(tools_group)
        self._btn_whitelist = QPushButton("白名单")
        self._btn_whitelist.clicked.connect(self._on_whitelist)
        tl.addWidget(self._btn_whitelist)
        self._btn_manual = QPushButton("手动修改: 关")
        self._btn_manual.setCheckable(True)
        self._btn_manual.toggled.connect(self._on_manual_toggle)
        tl.addWidget(self._btn_manual)
        self._btn_filter = QPushButton("只看广告: 关")
        self._btn_filter.setCheckable(True)
        self._btn_filter.toggled.connect(self._on_filter_toggle)
        tl.addWidget(self._btn_filter)
        layout.addWidget(tools_group)

        # 右侧：删除
        del_group = QGroupBox("删除操作")
        dl2 = QVBoxLayout(del_group)

        self._btn_delete = QPushButton("删除广告评论")
        self._btn_delete.setObjectName("danger")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        dl2.addWidget(self._btn_delete)

        self._delete_status = QLabel("")
        self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']};")
        dl2.addWidget(self._delete_status)
        layout.addWidget(del_group, 1)

        return card

    # ==================== 主题 ====================

    def _apply_theme(self):
        self.setStyleSheet(build_stylesheet(self._current_theme))
        self._dark_btn.setText("☀️ 亮色模式" if self._dark_mode else "🌙 暗色模式")
        self._sync_summary_colors()
        self._refresh_theme_bound_styles()

    def _toggle_dark_mode(self):
        self._dark_mode = not self._dark_mode
        self._current_theme = Dark() if self._dark_mode else Light()
        self._apply_theme()

    def _refresh_theme_bound_styles(self):
        """Refresh inline styles that global QSS cannot override."""
        if not hasattr(self, "_user_label"):
            return

        self._avatar_label.setStyleSheet(
            f"border-radius: 18px; background: {self._current_theme.BORDER};"
        )

        user_text = self._user_label.text()
        if user_text == "凭证无效":
            user_color = self._current_theme.BRAND_RED
            user_weight = FONT_WEIGHTS["medium"]
        elif user_text == "未登录":
            user_color = self._current_theme.TEXT_TERTIARY
            user_weight = FONT_WEIGHTS["regular"]
        else:
            user_color = self._current_theme.TEXT_PRIMARY
            user_weight = FONT_WEIGHTS["medium"]
        self._user_label.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; font-weight:{user_weight}; color:{user_color};"
        )

        if self._manual_toggle:
            self._btn_manual.setStyleSheet(
                f"font-weight:{FONT_WEIGHTS['bold']}; "
                f"border: 2px solid {self._current_theme.BRAND_ORANGE}; "
                f"color: {self._current_theme.BRAND_ORANGE};"
            )
        else:
            self._btn_manual.setStyleSheet("")

        if self._show_ads_only:
            self._btn_filter.setStyleSheet(
                f"font-weight:{FONT_WEIGHTS['bold']}; "
                f"border: 2px solid {self._current_theme.BRAND_RED}; "
                f"color: {self._current_theme.BRAND_RED};"
            )
        else:
            self._btn_filter.setStyleSheet("")

        if user_text in ("未登录", "凭证无效"):
            self._highlight_cookie_buttons()
        else:
            self._reset_cookie_button_style()

        if self._delete_status.text():
            self._delete_status.setStyleSheet(
                f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.TEXT_SECONDARY};"
            )

        if hasattr(self, "_btn_video_flow"):
            for btn, checked in (
                (self._btn_video_flow, self._btn_video_flow.isChecked()),
                (self._btn_comment_flow, self._btn_comment_flow.isChecked()),
            ):
                if checked:
                    btn.setStyleSheet(
                        f"font-weight:{FONT_WEIGHTS['bold']}; "
                        f"border: 2px solid {self._current_theme.BRAND_PINK}; "
                        f"color: {self._current_theme.BRAND_PINK};"
                    )
                else:
                    btn.setStyleSheet("")

    def _on_model_changed(self, model: str):
        """用户切换模型。"""
        self._config.deepseek_model = model
        self._alog.log("配置", f"切换模型: {model}")

    def _on_api_key(self):
        """配置 DeepSeek API Key。"""
        current = self._config.deepseek_api_key or ""
        masked = current[:8] + "…" + current[-4:] if len(current) > 12 else (current or "")

        dialog = QDialog(self)
        dialog.setWindowTitle("DeepSeek API Key 配置")
        dialog.resize(420, 180)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel(f"当前 Key: {masked if current else '（未设置）'}"))

        input_key = QLineEdit()
        input_key.setPlaceholderText("输入 sk-xxx…")
        input_key.setEchoMode(QLineEdit.EchoMode.Password)
        if current:
            input_key.setText(current)
        layout.addWidget(input_key)

        hint = QLabel("获取 Key: platform.deepseek.com → API Keys")
        hint.setStyleSheet(f"font-size:{FONT_SIZES['caption']}; color:{self._current_theme.TEXT_TERTIARY};")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary")

        def do_save():
            key = input_key.text().strip()
            if not key:
                QMessageBox.warning(dialog, "提示", "API Key 不能为空")
                return
            self._config.deepseek_api_key = key
            # 写入 .env
            from src.config import ENV_FILE
            env_path = ENV_FILE
            existing: dict[str, str] = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        existing[k.strip()] = v.strip()
            existing["DEEPSEEK_API_KEY"] = key
            lines = [f"{k}={v}" for k, v in existing.items()]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._alog.log("配置", "DeepSeek API Key 已更新")
            QMessageBox.information(dialog, "已保存", "API Key 已写入 .env")
            dialog.accept()

        btn_save.clicked.connect(do_save)
        btn_row.addWidget(btn_save)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(dialog.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        dialog.exec()

    def _on_settings(self):
        """配置主界面隐藏的运行参数。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("运行设置")
        dialog.resize(360, 260)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(SPACING["md"])

        crawl_row = QHBoxLayout()
        crawl_row.addWidget(QLabel("爬取间隔"))
        crawl_delay = QDoubleSpinBox()
        crawl_delay.setRange(0.1, 10.0)
        crawl_delay.setSingleStep(0.1)
        crawl_delay.setDecimals(1)
        crawl_delay.setValue(self._crawl_delay_seconds)
        crawl_delay.setSuffix(" s")
        crawl_delay.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        crawl_row.addWidget(crawl_delay)
        layout.addLayout(crawl_row)

        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(QLabel("AI 最大并发"))
        concurrency = QSpinBox()
        concurrency.setRange(1, 500)
        concurrency.setValue(self._ai_concurrency)
        concurrency.setKeyboardTracking(False)
        concurrency.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        concurrency_row.addWidget(concurrency)
        layout.addLayout(concurrency_row)

        delete_row = QHBoxLayout()
        delete_row.addWidget(QLabel("删除限速"))
        delete_rate = QSpinBox()
        delete_rate.setRange(1, 60)
        delete_rate.setValue(self._delete_rate_per_minute)
        delete_rate.setSuffix(" 条/分")
        delete_rate.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        delete_row.addWidget(delete_rate)
        layout.addLayout(delete_row)

        hint = QLabel("AI 检测会按实际批次数自动降低并发；其他参数保持原默认值。")
        hint.setObjectName("sectionCaption")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary")
        btn_cancel = QPushButton("取消")
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        def save_settings():
            self._crawl_delay_seconds = crawl_delay.value()
            self._ai_concurrency = concurrency.value()
            self._delete_rate_per_minute = delete_rate.value()
            self._alog.log(
                "配置",
                "运行设置已更新",
                f"delay={self._crawl_delay_seconds:.1f}s, "
                f"concurrency={self._ai_concurrency}, "
                f"delete_rate={self._delete_rate_per_minute}/min",
            )
            dialog.accept()

        btn_save.clicked.connect(save_settings)
        btn_cancel.clicked.connect(dialog.reject)

        dialog.exec()

    # ==================== 投稿视频分页 ====================

    def _reset_submissions(self, status: str = ""):
        self._submission_current_page = 1
        self._submission_total_count = 0
        self._submission_total_pages = 0
        self._submission_pages.clear()
        self._submission_loading_pages.clear()
        self._submission_owner_mid = self._current_user_mid
        self._submission_table.clearSpans()
        self._submission_table.setRowCount(0)
        self._submission_page_label.setText("第 0/0 页")
        self._submission_status.setText(status)
        self._update_submission_metrics([])
        self._btn_submission_refresh.setEnabled(bool(self._current_user_mid))
        self._btn_submission_prev.setEnabled(False)
        self._btn_submission_next.setEnabled(False)

    def _refresh_submissions(self):
        if not self._current_user_mid:
            self._reset_submissions("登录后自动加载")
            return
        self._reset_submissions("正在加载第 1 页…")
        self._show_submission_page(1)

    def _show_submission_page(self, page: int):
        if not self._current_user_mid:
            return
        page = max(1, page)
        if self._submission_total_pages:
            page = min(page, self._submission_total_pages)
        self._submission_current_page = page
        self._render_submission_page()
        self._prefetch_submission_pages(page)

    def _prefetch_submission_pages(self, start_page: int):
        pages = list(range(start_page, start_page + 5))
        if self._submission_total_pages:
            pages = [p for p in pages if p <= self._submission_total_pages]
        missing = [
            p for p in pages
            if p not in self._submission_pages and p not in self._submission_loading_pages
        ]
        if not missing:
            return

        self._submission_loading_pages.update(missing)
        first, last = missing[0], missing[-1]
        self._submission_status.setText(f"后台加载第 {first}-{last} 页…")
        for page in missing:
            self._run_async_with_result(
                lambda page=page: self._fetch_submission_pages([page]),
                self._on_submission_pages_loaded,
                self._on_submission_pages_error,
                timeout=30,
            )

    async def _fetch_submission_pages(self, pages: list[int]) -> dict:
        from bilibili_api import user, Credential

        cred = None
        if self._config.sessdata and self._config.bili_jct:
            cred = Credential(sessdata=self._config.sessdata, bili_jct=self._config.bili_jct)

        u = user.User(self._current_user_mid, credential=cred)
        loaded: dict[int, list[dict]] = {}
        total_count = self._submission_total_count
        total_known = False

        for page in pages[:5]:
            resp = await self._retry_submission_request(
                lambda page=page: u.get_videos(pn=page, ps=self._submission_page_size)
            )
            page_info = resp.get("page") or {}
            if page_info.get("count") is not None:
                total_count = int(page_info.get("count") or 0)
                total_known = True
            video_list = ((resp.get("list") or {}).get("vlist") or [])
            loaded[page] = video_list
            await asyncio.sleep(0.25)

        return {
            "pages": loaded,
            "total_count": total_count,
            "total_known": total_known,
            "requested_pages": pages,
        }

    async def _retry_submission_request(self, request_factory):
        last_exc: Exception | None = None
        retries = max(1, self._config.max_retries)
        for attempt in range(retries + 1):
            try:
                return await request_factory()
            except Exception as exc:
                last_exc = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(0.6 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def _on_submission_pages_loaded(self, result: dict):
        requested_pages = result.get("requested_pages") or []
        self._submission_loading_pages.difference_update(requested_pages)

        for page, videos in (result.get("pages") or {}).items():
            self._submission_pages[int(page)] = list(videos)

        total_count = int(result.get("total_count") or 0)
        if result.get("total_known"):
            self._submission_total_count = total_count
            self._submission_total_pages = max(1, math.ceil(total_count / self._submission_page_size)) if total_count else 1

        self._render_submission_page()
        loaded_count = sum(len(videos) for videos in self._submission_pages.values())
        snapshot_at = result.get("snapshot_at")
        if self._submission_total_count:
            if snapshot_at:
                self._submission_status.setText(f"快照已保存 {snapshot_at}，共 {loaded_count} 个投稿")
            elif self._submission_loading_pages:
                self._submission_status.setText(
                    f"已显示 {loaded_count} 个投稿，后台仍在加载 {len(self._submission_loading_pages)} 页"
                )
            else:
                self._submission_status.setText(f"已缓存 {len(self._submission_pages)} 页 / {loaded_count} 个投稿")
        else:
            self._submission_status.setText("暂无投稿视频")
        self._update_submission_metrics(self._all_cached_submission_videos())

    def apply_preloaded_submissions(self, result: dict):
        if not result:
            return
        mid = result.get("mid")
        if mid and self._current_user_mid and int(mid) != self._current_user_mid:
            return
        if result.get("error"):
            if not self._submission_pages:
                self._submission_status.setText(f"投稿预加载失败: {result['error']}")
            return
        if mid:
            self._submission_owner_mid = int(mid)
        self._submission_page_size = int(result.get("page_size") or self._submission_page_size)
        self._submission_current_page = 1
        self._submission_loading_pages.clear()
        self._on_submission_pages_loaded(result)
        self._preloaded_submissions = None

    def _on_submission_pages_error(self, err: str):
        self._submission_loading_pages.clear()
        self._submission_status.setText(f"投稿加载失败: {err}")
        self._alog.log("投稿视频", "加载投稿失败", "失败", error=err)

    def _render_submission_page(self):
        page = self._submission_current_page
        total_pages = self._submission_total_pages
        videos = self._submission_pages.get(page)

        if videos is None:
            self._submission_table.clearSpans()
            self._submission_table.setRowCount(1)
            item = QTableWidgetItem("正在加载…")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._submission_table.setSpan(0, 0, 1, 8)
            self._submission_table.setItem(0, 0, item)
        else:
            self._submission_table.clearSpans()
            self._submission_table.setRowCount(len(videos))
            for row, video in enumerate(videos):
                created = video.get("created") or 0
                created_text = ""
                if created:
                    created_text = datetime.fromtimestamp(int(created)).strftime("%Y-%m-%d")

                values = [
                    str(video.get("bvid") or ""),
                    str(video.get("title") or ""),
                    self._format_count(video.get("play")),
                    self._format_delta(video.get("play_delta"), video.get("is_new")),
                    self._format_count(video.get("comment")),
                    self._format_delta(video.get("comment_delta"), video.get("is_new")),
                    str(video.get("length") or ""),
                    created_text,
                ]
                for col, value in enumerate(values):
                    table_item = QTableWidgetItem(value)
                    if col in (2, 3, 4, 5, 6, 7):
                        table_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if col in (3, 5) and value.startswith("+"):
                        table_item.setForeground(QColor(self._current_theme.BRAND_GREEN))
                    elif col in (3, 5) and value == "NEW":
                        table_item.setForeground(QColor(self._current_theme.BRAND_BLUE))
                    table_item.setToolTip(value)
                    table_item.setData(Qt.ItemDataRole.UserRole, video)
                    self._submission_table.setItem(row, col, table_item)

        shown_total = total_pages or max(page, 1 if self._submission_pages or self._submission_loading_pages else 0)
        self._submission_page_label.setText(f"第 {page}/{shown_total} 页" if shown_total else "第 0/0 页")
        self._btn_submission_refresh.setEnabled(bool(self._current_user_mid))
        self._btn_submission_prev.setEnabled(page > 1)
        self._btn_submission_next.setEnabled(
            bool(self._current_user_mid)
            and (not total_pages or page < total_pages)
        )
        self._fit_submission_table_height()

    def _fit_submission_table_height(self):
        """Keep the submission table just tall enough for one page."""
        if not hasattr(self, "_submission_table"):
            return
        row_height = self._submission_table.verticalHeader().defaultSectionSize()
        header_height = self._submission_table.horizontalHeader().height() or 40
        frame = self._submission_table.frameWidth() * 2
        visible_rows = max(1, self._submission_page_size)
        self._submission_table.setFixedHeight(header_height + row_height * visible_rows + frame + 4)

    def _all_cached_submission_videos(self) -> list[dict]:
        seen = set()
        videos = []
        for page in sorted(self._submission_pages):
            for video in self._submission_pages[page]:
                bvid = video.get("bvid")
                if bvid in seen:
                    continue
                seen.add(bvid)
                videos.append(video)
        return videos

    def _update_submission_metrics(self, videos: list[dict]):
        if not hasattr(self, "_submission_total_metric"):
            return
        total = self._submission_total_count or len(videos)
        play_delta = sum(max(0, int(video.get("play_delta") or 0)) for video in videos)
        comment_delta = sum(max(0, int(video.get("comment_delta") or 0)) for video in videos)
        self._submission_total_metric._value_label.setText(str(total))
        self._submission_play_delta_metric._value_label.setText(self._format_count(play_delta))
        self._submission_comment_delta_metric._value_label.setText(self._format_count(comment_delta))

    def _on_submission_double_clicked(self, row: int, column: int):
        item = self._submission_table.item(row, column)
        if not item:
            return
        video = item.data(Qt.ItemDataRole.UserRole) or {}
        bvid = video.get("bvid")
        if not bvid:
            return
        self._bv_input.setText(str(bvid))
        title = video.get("title") or bvid
        self._status_bar.showMessage(f"已选中投稿: {title}")

    @staticmethod
    def _format_count(value) -> str:
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            return "0"
        if count >= 10000:
            return f"{count / 10000:.1f}万"
        return str(count)

    @staticmethod
    def _format_delta(value, is_new: bool = False) -> str:
        if is_new:
            return "NEW"
        try:
            delta = int(value or 0)
        except (TypeError, ValueError):
            delta = 0
        if delta > 0:
            return f"+{delta}"
        if delta < 0:
            return str(delta)
        return "-"

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
        self._comments = []
        self._judgments = None
        self._update_summary(status="正在爬取评论…")

        # 爬取无超时限制（可能很长），完成后在主线程处理结果
        self._run_async_with_result(
            lambda: self._do_crawl(bv),
            self._on_crawl_done,
            self._on_crawl_error,
            timeout=None,
        )

    def _on_progress_update(self, ev: ProgressEvent):
        """主线程中安全更新进度 UI。"""
        maximum = ev.estimated_total or ev.total_crawled or 1
        self._progress.setMaximum(max(maximum, ev.total_crawled))
        self._progress.setValue(ev.total_crawled)
        self._status_bar.showMessage(ev.message)
        self._update_summary(comment_count=ev.total_crawled, status=ev.message)

    def _on_comments_batch(self, comments: list[Comment]):
        """实时追加显示本页已爬到的评论。"""
        self._comments.extend(comments)
        self._refresh_table()

    async def _do_crawl(self, bv_id: str):
        """在后台线程中运行的爬取协程。不直接操作 UI，通过信号发送进度。"""
        from src.service import CrawlerService
        self._config.delay_base = self._crawl_delay_seconds
        self._config.delay_jitter = self._crawl_delay_seconds * 0.5
        self._crawl_service = CrawlerService(self._config)

        def on_progress(ev: ProgressEvent):
            # 通过信号安全发送到主线程
            self._progress_signal.emit(ev)

        def on_comments(comments: list[Comment]):
            self._comments_batch_signal.emit(comments)

        result: CrawlResult = await self._crawl_service.crawl(
            bv_id,
            on_progress=on_progress,
            on_comments=on_comments,
        )
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
        self._update_summary(
            comment_count=len(self._flatten_comments()),
            ad_count=0,
            deletable=0,
            status=f"已完成爬取: {result.video_title}",
        )
        self._alog.log("爬取评论", f"爬取完成: {result.video_title}",
            f"{result.total_count}条, {result.crawl_time:.1f}s",
            error="; ".join(result.errors) if result.errors else None)
        self._crawl_service = None

    def _on_crawl_error(self, err: str):
        """爬取出错回调（主线程）。"""
        self._status_bar.showMessage(f"爬取出错: {err}")
        self._btn_crawl.setEnabled(True)
        self._btn_preview.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._progress.setVisible(False)
        self._alog.log("爬取评论", "爬取出错", "失败", error=err)
        self._crawl_service = None
        QMessageBox.critical(self, "爬取出错", err)

    def _on_cancel(self):
        self._alog.log("爬取评论", "用户点击「取消」")
        if hasattr(self, '_crawl_service') and self._crawl_service:
            self._crawl_service.cancel()
        self._status_bar.showMessage("已请求取消…")

    def _on_detect(self):
        if not self._comments:
            return
        self._alog.log("AI检测", f"用户点击「检测广告评论」, {len(self._comments)}条评论")
        self._btn_detect.setEnabled(False)
        self._detect_status.setText("检测中…")
        self._detect_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_BLUE};")
        self._update_summary(status="AI 正在检测广告评论…")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._run_async_with_result(
            lambda: self._do_detect(),
            self._on_detect_done,
            self._on_detect_error,
            timeout=None,  # 每批 DeepSeek 自带 60s 超时，外层不限制
        )

    async def _do_detect(self):
        """在后台线程中运行的 AI 检测协程，5路并发，每批完成后实时刷新。"""
        import asyncio
        from src.llm.deepseek import DeepSeekClient
        from src.agent.ad_detector import AdDetector

        client = DeepSeekClient(
            api_key=self._config.deepseek_api_key,
            model=self._config.deepseek_model,
        )
        detector = AdDetector(client)

        # 展平树形结构
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

        total = len(all_comments)
        if total == 0:
            return BatchAdJudgment()

        batch_size = 50
        chunks = [all_comments[i:i + batch_size] for i in range(0, total, batch_size)]
        max_concurrent = min(self._ai_concurrency, len(chunks))
        sem = asyncio.Semaphore(max_concurrent)

        # batch_index → judgments（用于合并）
        done_map: dict[int, list] = {}
        completed_count = 0

        async def process_batch(idx: int, chunk: list[dict]):
            nonlocal completed_count
            async with sem:
                result = await detector.detect(chunk)
            # 存入结果
            done_map[idx] = result.judgments
            completed_count += 1

            # 合并已完成批次的判定（按顺序）
            merged: list = []
            for i in range(len(chunks)):
                if i in done_map:
                    merged.extend(done_map[i])
                else:
                    break  # 遇到缺口就停

            current = min(completed_count * batch_size, total)
            self._progress_signal.emit(ProgressEvent(
                bv_id="", phase="detecting",
                current_page=0, page_size=0,
                total_crawled=current, estimated_total=total,
                message=f"AI 检测中: {current}/{total} ({max_concurrent}路并发)",
            ))
            self._detect_batch_signal.emit(BatchAdJudgment(judgments=list(merged)))

        # 启动所有并发任务
        tasks = [process_batch(i, chunk) for i, chunk in enumerate(chunks)]
        await asyncio.gather(*tasks)

        # 最终合并全部
        all_judgments: list = []
        for i in range(len(chunks)):
            all_judgments.extend(done_map[i])
        return BatchAdJudgment(judgments=all_judgments)

    def _on_detect_batch(self, partial: BatchAdJudgment):
        """收到一批检测结果，实时更新表格（主线程）。"""
        self._judgments = partial
        self._refresh_table(show_judgments=True)

    def _on_detect_done(self, judgments: BatchAdJudgment):
        """AI 检测完成回调（主线程）。"""
        self._judgments = judgments
        self._refresh_table(show_judgments=True)
        self._btn_detect.setEnabled(True)
        ad_count, deletable = self._refresh_detection_summary()
        self._progress.setVisible(False)
        self._update_summary(
            comment_count=len(self._flatten_comments()),
            ad_count=ad_count,
            deletable=deletable,
            status="检测完成，可继续复核或删除",
        )
        self._alog.log("AI检测", f"检测完成: {self._video_title}",
            f"{ad_count}/{len(judgments.judgments)}条广告, {deletable}条可删")

    def _on_detect_error(self, err: str):
        """AI 检测出错回调（主线程）。"""
        self._detect_status.setText(f"检测失败: {err}")
        self._detect_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED};")
        self._btn_detect.setEnabled(True)
        self._progress.setVisible(False)
        self._alog.log("AI检测", "检测出错", "失败", error=err)
        QMessageBox.critical(self, "AI 检测失败", err)

    def _on_delete(self):
        if not self._judgments:
            return

        # 排除白名单
        ad_judgments = [j for j in self._judgments.judgments
                        if j.is_ad and not self._is_judgment_whitelisted(j)]
        ad_count = len(ad_judgments)

        self._alog.log("删除操作", f"用户点击「删除广告评论」, 共{ad_count}条(已排除白名单)")

        if ad_count == 0:
            QMessageBox.information(self, "无需删除", "没有检测到广告评论。")
            return

        reply = QMessageBox.warning(
            self, "确认删除",
            f"将删除 {ad_count} 条广告评论。\n此操作不可逆，确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._btn_delete.setEnabled(False)
        self._delete_status.setText("删除中…")

        from src.agent.ad_detector import BatchAdJudgment as BAJ
        filtered = BAJ(judgments=ad_judgments)

        self._run_async_with_result(
            lambda: self._do_delete(filtered),
            self._on_delete_done,
            self._on_delete_error,
            timeout=120,
        )

    async def _do_delete(self, judgments):
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
            bv_id=bv, oid=oid, judgments=judgments,
            dry_run=False,
            delete_rate_per_minute=self._delete_rate_per_minute,
        )
        return result  # 交给 _on_delete_done 在主线程处理

    def _on_delete_done(self, result):
        """删除完成回调（主线程）。"""
        bv = self._bv_input.text().strip()

        if result.skipped_count > 0:
            self._delete_status.setText(f"已跳过 {result.skipped_count} 条（非本人视频）")
            self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_ORANGE};")
        else:
            self._delete_status.setText(f"删除完成: {result.success_count} 成功, {result.failed_count} 失败")
            color = self._current_theme.BRAND_GREEN if result.all_success else self._current_theme.BRAND_ORANGE
            self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{color};")

        self._alog.log("删除操作", f"删除完成: {bv}",
            f"成功{result.success_count}/失败{result.failed_count}/跳过{result.skipped_count}")

        self._btn_delete.setEnabled(True)
        self._update_summary(status=f"删除完成: {result.success_count} 成功, {result.failed_count} 失败")

    def _on_delete_error(self, err: str):
        """删除出错回调（主线程）。"""
        self._delete_status.setText(f"删除失败: {err}")
        self._delete_status.setStyleSheet(f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED};")
        self._btn_delete.setEnabled(True)
        self._alog.log("删除操作", "删除出错", "失败", error=err)
        QMessageBox.critical(self, "删除失败", err)

    # ==================== 白名单 ====================

    def _get_uid_by_rpid(self, rpid: str) -> str:
        """根据 rpid 查找对应评论的 uid。"""
        def search(comments):
            for c in comments:
                if str(c.rpid) == rpid:
                    return str(c.uid)
                if c.replies:
                    result = search(c.replies)
                    if result:
                        return result
            return ""
        return search(self._comments)

    def _is_judgment_whitelisted(self, judgment: CommentAdJudgment) -> bool:
        """检查判定项是否命中用户白名单或评论白名单。"""
        bv = self._bv_input.text().strip()
        return (
            self._whitelist.contains(self._get_uid_by_rpid(judgment.rpid))
            or self._whitelist.contains_comment(bv, judgment.rpid)
        )

    def _on_whitelist(self):
        """打开白名单管理对话框。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("白名单管理")
        dialog.resize(620, 460)
        dialog.setMinimumSize(520, 360)

        layout = QVBoxLayout(dialog)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # ==================== 用户白名单 ====================
        user_tab = QWidget()
        user_layout = QVBoxLayout(user_tab)

        user_title = QLabel("白名单用户（免删）")
        user_title.setStyleSheet(f"font-size:{FONT_SIZES['h3']}; font-weight:{FONT_WEIGHTS['semibold']};")
        user_layout.addWidget(user_title)

        user_table = QTableWidget(0, 2)
        user_table.setHorizontalHeaderLabels(["UID", "备注名"])
        user_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        user_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        user_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        user_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        user_table.verticalHeader().setVisible(False)
        user_layout.addWidget(user_table, 1)

        def refresh_user_table():
            user_table.setRowCount(0)
            for info_item in self._whitelist.get_info():
                row = user_table.rowCount()
                user_table.insertRow(row)
                user_table.setItem(row, 0, QTableWidgetItem(info_item["uid"]))
                user_table.setItem(row, 1, QTableWidgetItem(info_item["name"] or "—"))

        refresh_user_table()

        user_op_layout = QHBoxLayout()

        uid_input = QLineEdit()
        uid_input.setPlaceholderText("输入用户 UID")
        user_op_layout.addWidget(uid_input)

        name_input = QLineEdit()
        name_input.setPlaceholderText("备注名（可选）")
        user_op_layout.addWidget(name_input)

        btn_add = QPushButton("添加")
        btn_add.clicked.connect(lambda: (
            self._whitelist.add(uid_input.text().strip(), name_input.text().strip()),
            uid_input.clear(),
            name_input.clear(),
            refresh_user_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
            self._refresh_detection_summary(),
        ))
        user_op_layout.addWidget(btn_add)

        btn_remove = QPushButton("删除选中")
        btn_remove.clicked.connect(lambda: (
            [self._whitelist.remove(user_table.item(user_table.currentRow(), 0).text())
             for _ in [0] if user_table.currentRow() >= 0],
            refresh_user_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
            self._refresh_detection_summary(),
        ))
        user_op_layout.addWidget(btn_remove)
        user_layout.addLayout(user_op_layout)

        btn_clear = QPushButton("清空全部")
        btn_clear.setObjectName("danger")
        btn_clear.clicked.connect(lambda: (
            self._whitelist.clear(),
            refresh_user_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
            self._refresh_detection_summary(),
        ))
        user_layout.addWidget(btn_clear)
        tabs.addTab(user_tab, "用户白名单")

        # ==================== 评论白名单 ====================
        comment_tab = QWidget()
        comment_layout = QVBoxLayout(comment_tab)

        comment_title = QLabel("评论白名单（按 BV + 评论 ID 免删）")
        comment_title.setStyleSheet(f"font-size:{FONT_SIZES['h3']}; font-weight:{FONT_WEIGHTS['semibold']};")
        comment_layout.addWidget(comment_title)

        comment_table = QTableWidget(0, 4)
        comment_table.setHorizontalHeaderLabels(["BV号", "评论ID", "用户", "内容"])
        comment_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        comment_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        comment_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        comment_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        comment_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        comment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        comment_table.verticalHeader().setVisible(False)
        comment_layout.addWidget(comment_table, 1)

        def refresh_comment_table():
            comment_table.setRowCount(0)
            for info_item in self._whitelist.get_comment_info():
                row = comment_table.rowCount()
                comment_table.insertRow(row)
                values = [
                    info_item["bv_id"],
                    info_item["rpid"],
                    info_item["username"] or "—",
                    info_item["content"] or "—",
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    comment_table.setItem(row, col, item)

        refresh_comment_table()

        comment_op_layout = QHBoxLayout()

        btn_remove_comment = QPushButton("删除选中")
        btn_remove_comment.clicked.connect(lambda: (
            [self._whitelist.remove_comment(
                comment_table.item(comment_table.currentRow(), 0).text(),
                comment_table.item(comment_table.currentRow(), 1).text(),
            ) for _ in [0] if comment_table.currentRow() >= 0],
            refresh_comment_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
            self._refresh_detection_summary(),
        ))
        comment_op_layout.addWidget(btn_remove_comment)

        btn_clear_current_bv = QPushButton("清空当前 BV")
        btn_clear_current_bv.clicked.connect(lambda: (
            self._whitelist.clear_comments(self._bv_input.text().strip()),
            refresh_comment_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
            self._refresh_detection_summary(),
        ))
        comment_op_layout.addWidget(btn_clear_current_bv)

        btn_clear_comments = QPushButton("清空全部评论白名单")
        btn_clear_comments.setObjectName("danger")
        btn_clear_comments.clicked.connect(lambda: (
            self._whitelist.clear_comments(),
            refresh_comment_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
            self._refresh_detection_summary(),
        ))
        comment_op_layout.addWidget(btn_clear_comments)
        comment_layout.addLayout(comment_op_layout)
        tabs.addTab(comment_tab, "评论白名单")

        dialog.exec()

    # ==================== 手动修改模式 ====================

    def _on_manual_toggle(self, checked: bool):
        """切换手动修改模式。"""
        self._manual_toggle = checked
        self._btn_manual.setText("手动修改: 开" if checked else "手动修改: 关")
        if checked:
            self._btn_manual.setStyleSheet(
                f"font-weight:{FONT_WEIGHTS['bold']}; "
                f"border: 2px solid {self._current_theme.BRAND_ORANGE}; "
                f"color: {self._current_theme.BRAND_ORANGE};"
            )
        else:
            self._btn_manual.setStyleSheet("")
        # 刷新表格以更新悬浮效果
        self._refresh_table(show_judgments=self._judgments is not None)

    def _on_filter_toggle(self, checked: bool):
        """切换只看广告模式。"""
        self._show_ads_only = checked
        self._btn_filter.setText("只看广告: 开" if checked else "只看广告: 关")
        if checked:
            self._btn_filter.setStyleSheet(
                f"font-weight:{FONT_WEIGHTS['bold']}; "
                f"border: 2px solid {self._current_theme.BRAND_RED}; "
                f"color: {self._current_theme.BRAND_RED};"
            )
        else:
            self._btn_filter.setStyleSheet("")
        self._refresh_table(show_judgments=self._judgments is not None)

    def _on_table_cell_clicked(self, row: int, col: int):
        """手动修改模式下点击广告列切换判定。"""
        if not self._manual_toggle or col != 4:
            return
        if self._judgments is None:
            return

        item = self._table.item(row, col)
        if item is None:
            return
        role = item.data(Qt.ItemDataRole.UserRole)
        if role in ("whitelist", ""):
            return  # 白名单或无判定不响应

        rpid_str = item.data(Qt.ItemDataRole.UserRole + 2)
        if not rpid_str:
            return

        # 翻转判定
        for j in self._judgments.judgments:
            if j.rpid == rpid_str:
                j.is_ad = not j.is_ad
                if j.is_ad:
                    j.ad_type = "手动标记"
                    j.reason = "用户手动标记为广告"
                else:
                    j.ad_type = ""
                    j.reason = "用户手动取消广告标记"
                break

        self._refresh_table(show_judgments=True)
        self._refresh_detection_summary()

    def _on_table_item_changed(self, item: QTableWidgetItem):
        """评论白名单复选框变化时持久化。"""
        if item.column() != 6:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        bv, rpid, content, username = data
        if item.checkState() == Qt.CheckState.Checked:
            self._whitelist.add_comment(bv, rpid, content=content, username=username)
        else:
            self._whitelist.remove_comment(bv, rpid)
        self._refresh_table(show_judgments=self._judgments is not None)
        self._refresh_detection_summary()

    def _refresh_detection_summary(self) -> tuple[int, int]:
        """根据当前判定和白名单状态刷新检测摘要与删除按钮。"""
        if not self._judgments:
            self._btn_delete.setEnabled(False)
            return 0, 0
        ad_count = sum(1 for j in self._judgments.judgments if j.is_ad)
        deletable = sum(1 for j in self._judgments.judgments if j.is_ad and not self._is_judgment_whitelisted(j))
        exempt = ad_count - deletable
        self._detect_status.setText(
            f"检测完成: {ad_count}/{len(self._judgments.judgments)} 条广告"
            f"{' (白名单豁免 ' + str(exempt) + ' 条)' if exempt > 0 else ''}"
        )
        self._detect_status.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED if ad_count > 0 else self._current_theme.BRAND_GREEN};"
        )
        self._btn_delete.setEnabled(deletable > 0)
        self._update_summary(
            comment_count=len(self._flatten_comments()),
            ad_count=ad_count,
            deletable=deletable,
        )
        return ad_count, deletable

    def _flatten_comments(self) -> list[tuple[Comment, int]]:
        """将树形评论展平成带层级的列表。"""
        flat: list[tuple[Comment, int]] = []

        def walk(comments, depth=0):
            for c in comments:
                flat.append((c, depth))
                if c.replies:
                    walk(c.replies, depth + 1)

        walk(self._comments)
        return flat

    def _update_summary(
        self,
        comment_count: int | None = None,
        ad_count: int | None = None,
        deletable: int | None = None,
        status: str | None = None,
    ):
        """刷新右上角任务摘要。"""
        if comment_count is not None:
            self._summary_comments._value_label.setText(str(comment_count))
        if ad_count is not None:
            self._summary_ads._value_label.setText(str(ad_count))
        if deletable is not None:
            self._summary_deletable._value_label.setText(str(deletable))
        if status is not None:
            self._summary_status.setText(status)
        self._sync_summary_colors()

    def _sync_summary_colors(self):
        """主题切换后恢复摘要数字的语义色。"""
        if not hasattr(self, "_summary_ads"):
            return
        style = f"font-size:{FONT_SIZES['h2']}; font-weight:{FONT_WEIGHTS['bold']};"
        self._summary_comments._value_label.setStyleSheet(f"{style} color: {self._current_theme.BRAND_BLUE};")
        self._summary_ads._value_label.setStyleSheet(f"{style} color: {self._current_theme.BRAND_RED};")
        self._summary_deletable._value_label.setStyleSheet(f"{style} color: {self._current_theme.BRAND_ORANGE};")

    # ==================== 表格刷新 ====================

    def _refresh_table(self, show_judgments: bool = False):
        """用 Comment 列表填充表格。"""
        blocker = QSignalBlocker(self._table)
        self._table.setRowCount(0)
        bv = self._bv_input.text().strip()

        flat = self._flatten_comments()

        # 只看广告模式：过滤非广告行
        if self._show_ads_only and self._judgments:
            ad_rpids = {j.rpid for j in self._judgments.judgments if j.is_ad}
            flat = [(c, d) for c, d in flat if str(c.rpid) in ad_rpids]

        self._table.setRowCount(len(flat))
        self._table_title.setText(f"评论列表 ({len(flat)} 条)")
        if not self._judgments:
            self._update_summary(comment_count=len(flat))

        # 选中行用红色系高亮（通过 QPalette，避免 QSS 覆盖 setBackground）
        if show_judgments:
            sel_color = QColor("#FF9090" if not self._dark_mode else "#5A1A1A")
            palette = self._table.palette()
            palette.setColor(palette.ColorRole.Highlight, sel_color)
            palette.setColor(palette.ColorRole.HighlightedText, QColor(self._current_theme.TEXT_PRIMARY))
            self._table.setPalette(palette)
        else:
            self._table.setPalette(self.style().standardPalette())

        # 判定映射：rpid → CommentAdJudgment
        j_map: dict[str, CommentAdJudgment] = {}
        if show_judgments and self._judgments:
            j_map = {j.rpid: j for j in self._judgments.judgments}

        for i, (c, depth) in enumerate(flat):
            prefix = "  " * depth + ("└ " if depth > 0 else "")
            rpid_str = str(c.rpid)
            uid_str = str(c.uid)
            is_comment_whitelisted = self._whitelist.contains_comment(bv, rpid_str)

            items = [
                QTableWidgetItem(str(i + 1)),
                QTableWidgetItem(prefix + c.username),
                QTableWidgetItem(c.content),
                QTableWidgetItem(str(c.likes)),
                QTableWidgetItem(""),
                QTableWidgetItem(""),
                QTableWidgetItem(""),
            ]

            # 序号、点赞、广告列、评论白名单列居中
            for col in (0, 3, 4, 6):
                items[col].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            items[2].setToolTip(c.content)
            items[4].setData(Qt.ItemDataRole.UserRole + 2, rpid_str)
            items[6].setFlags(
                items[6].flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            items[6].setCheckState(Qt.CheckState.Checked if is_comment_whitelisted else Qt.CheckState.Unchecked)
            items[6].setData(Qt.ItemDataRole.UserRole, (bv, rpid_str, c.content, c.username))
            items[6].setToolTip("勾选后此评论在当前 BV 下免删")

            is_user_whitelisted = self._whitelist.contains(c.uid)
            is_whitelisted = is_user_whitelisted or is_comment_whitelisted
            j = j_map.get(rpid_str)

            # ---- 广告判定着色 ----
            if is_whitelisted:
                # 白名单用户：白色背景，不可被删除
                items[4].setText("📋 白名单")
                items[4].setForeground(QColor(self._current_theme.BRAND_BLUE))
                items[5].setText("用户白名单免删" if is_user_whitelisted else "评论白名单免删")
                for item in items:
                    item.setBackground(QColor("#FFFFFF" if not self._dark_mode else "#2A2A2A"))
                # 存储元数据供手动修改判断
                items[4].setData(Qt.ItemDataRole.UserRole, "whitelist")

            elif j and j.is_ad:
                items[4].setText("🚫 广告")
                items[4].setForeground(QColor(self._current_theme.BRAND_RED))
                items[5].setText(f"{j.ad_type or ''}: {j.reason}")
                for item in items:
                    item.setBackground(QColor("#FFD8D8" if not self._dark_mode else "#4A1818"))
                items[4].setData(Qt.ItemDataRole.UserRole, "ad")

            elif j:
                items[4].setText("✓ 正常")
                items[5].setText(j.reason)
                for item in items:
                    item.setBackground(QColor("#E8F8E8" if not self._dark_mode else "#1A3A1A"))
                items[4].setData(Qt.ItemDataRole.UserRole, "normal")

            else:
                # 无判定、非白名单
                items[4].setData(Qt.ItemDataRole.UserRole, "")

            # 手动修改模式下悬浮提示
            if self._manual_toggle and show_judgments and not is_whitelisted:
                items[4].setToolTip("点击切换 广告 ↔ 正常")
                # 给广告列加光标样式（通过存储标记）
                items[4].setData(Qt.ItemDataRole.UserRole + 1, "togglable")
            if items[5].text():
                items[5].setToolTip(items[5].text())

            for col, item in enumerate(items):
                self._table.setItem(i, col, item)
        del blocker

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
        self._current_user_mid = None
        self._user_label.setText("未登录")
        self._user_label.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.TEXT_TERTIARY};"
        )
        self._avatar_label.setVisible(False)
        self._highlight_cookie_buttons()
        self._reset_submissions("登录后自动加载")

    def _update_user_display(self, info: dict):
        """更新顶栏用户头像和昵称。"""
        name = info.get("name", "")
        face_url = info.get("face", "")
        mid = info.get("mid")
        if mid is not None:
            try:
                self._current_user_mid = int(mid)
            except (TypeError, ValueError):
                self._current_user_mid = None

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

        if self._current_user_mid:
            if self._submission_pages and self._submission_owner_mid == self._current_user_mid:
                return
            preload_mid = None
            if self._preloaded_submissions:
                preload_mid = self._preloaded_submissions.get("mid")
            if self._preloaded_submissions and (not preload_mid or int(preload_mid) == self._current_user_mid):
                self.apply_preloaded_submissions(self._preloaded_submissions)
            else:
                self._refresh_submissions()

    # ==================== Cookie 按钮高亮 ====================

    def _highlight_cookie_buttons(self):
        """未登录时高亮 Cookie 导入按钮，引导用户操作。"""
        if not hasattr(self, "_btn_auto_cookie") or not hasattr(self, "_btn_manual_cookie"):
            return
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
        if not hasattr(self, "_btn_auto_cookie") or not hasattr(self, "_btn_manual_cookie"):
            return
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
                scaled = pixmap.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio,
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
