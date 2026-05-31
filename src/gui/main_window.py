"""主窗口 — 评论爬取 → AI 检测 → 删除，一站式操作。"""

import asyncio
import json
from pathlib import Path

import requests
from loguru import logger
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QUrl
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QSplitter, QStatusBar, QMessageBox,
    QGroupBox, QCheckBox, QSpinBox, QTabWidget, QComboBox, QDoubleSpinBox,
    QTextEdit, QApplication, QInputDialog, QDialog,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from src.config import Config
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
        self._whitelist = WhitelistManager()  # 白名单
        self._manual_toggle = False  # 手动修改模式
        self._show_ads_only = False  # 只看广告

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

        # 模型选择
        bar.addSpacing(SPACING["sm"])
        self._model_combo = QComboBox()
        self._model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])
        idx = self._model_combo.findText(self._config.deepseek_model)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        self._model_combo.setMaximumWidth(150)
        bar.addWidget(self._model_combo)

        # API Key 按钮
        self._btn_api_key = QPushButton("🔑 API Key")
        self._btn_api_key.clicked.connect(self._on_api_key)
        bar.addWidget(self._btn_api_key)

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

        self._crawl_delay_spin = QDoubleSpinBox()
        self._crawl_delay_spin.setRange(0.1, 10.0)
        self._crawl_delay_spin.setValue(1.0)
        self._crawl_delay_spin.setSingleStep(0.1)
        self._crawl_delay_spin.setDecimals(1)
        self._crawl_delay_spin.setPrefix("间隔 ")
        self._crawl_delay_spin.setSuffix("s")
        self._crawl_delay_spin.setMaximumWidth(120)
        header.addWidget(self._crawl_delay_spin)
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
        self._table.setColumnWidth(4, 95)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.cellClicked.connect(self._on_table_cell_clicked)
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

        # 并发数
        conc_row = QHBoxLayout()
        conc_row.addWidget(QLabel("并发:"))
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 500)
        self._concurrency_spin.setValue(20)
        self._concurrency_spin.setMaximumWidth(70)
        self._concurrency_spin.setKeyboardTracking(False)
        conc_row.addWidget(self._concurrency_spin)
        conc_row.addStretch()
        dl.addLayout(conc_row)

        layout.addWidget(detect_group, 1)

        # 中间：白名单 + 手动修改
        tools_group = QGroupBox("🔧 工具")
        tl = QVBoxLayout(tools_group)
        self._btn_whitelist = QPushButton("📋 白名单")
        self._btn_whitelist.clicked.connect(self._on_whitelist)
        tl.addWidget(self._btn_whitelist)
        self._btn_manual = QPushButton("✏️ 手动修改: 关")
        self._btn_manual.setCheckable(True)
        self._btn_manual.toggled.connect(self._on_manual_toggle)
        tl.addWidget(self._btn_manual)
        self._btn_filter = QPushButton("🔍 只看广告: 关")
        self._btn_filter.setCheckable(True)
        self._btn_filter.toggled.connect(self._on_filter_toggle)
        tl.addWidget(self._btn_filter)
        layout.addWidget(tools_group)

        # 右侧：删除
        del_group = QGroupBox("🗑️ 删除操作")
        dl2 = QVBoxLayout(del_group)

        # 删除限速
        del_row = QHBoxLayout()
        del_row.addWidget(QLabel("限速:"))
        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(1, 60)
        self._delay_spin.setValue(10)
        self._delay_spin.setSuffix(" 条/分")
        self._delay_spin.setMaximumWidth(100)
        del_row.addWidget(self._delay_spin)
        del_row.addStretch()
        dl2.addLayout(del_row)
        hint = QLabel("每分钟最多删除数，防止触发风控")
        hint.setStyleSheet(f"font-size:{FONT_SIZES['caption']}; color:{self._current_theme.TEXT_TERTIARY};")
        dl2.addWidget(hint)

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

    def _toggle_dark_mode(self):
        self._dark_mode = not self._dark_mode
        self._current_theme = Dark() if self._dark_mode else Light()
        self._apply_theme()

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
            env_path = Path(__file__).parent.parent.parent / ".env"
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

    def _on_comments_batch(self, comments: list[Comment]):
        """实时追加显示本页已爬到的评论。"""
        self._comments.extend(comments)
        self._refresh_table()

    async def _do_crawl(self, bv_id: str):
        """在后台线程中运行的爬取协程。不直接操作 UI，通过信号发送进度。"""
        from src.service import CrawlerService
        self._config.delay_base = self._crawl_delay_spin.value()
        self._config.delay_jitter = self._crawl_delay_spin.value() * 0.5
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
        max_concurrent = min(self._concurrency_spin.value(), len(chunks))
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
        ad_count = sum(1 for j in judgments.judgments if j.is_ad)
        # 排除白名单后的可删除广告数
        deletable = sum(1 for j in judgments.judgments
                       if j.is_ad and not self._whitelist.contains(self._get_uid_by_rpid(j.rpid)))
        self._detect_status.setText(f"检测完成: {ad_count}/{len(judgments.judgments)} 条广告"
                                    f"{' (白名单豁免 ' + str(ad_count - deletable) + ' 条)' if ad_count > deletable else ''}")
        self._detect_status.setStyleSheet(
            f"font-size:{FONT_SIZES['small']}; color:{self._current_theme.BRAND_RED if ad_count > 0 else self._current_theme.BRAND_GREEN};"
        )
        self._refresh_table(show_judgments=True)
        self._btn_detect.setEnabled(True)
        self._btn_delete.setEnabled(deletable > 0)
        self._progress.setVisible(False)
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
                        if j.is_ad and not self._whitelist.contains(self._get_uid_by_rpid(j.rpid))]
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
            delete_rate_per_minute=self._delay_spin.value(),
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

    def _on_whitelist(self):
        """打开白名单管理对话框。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("白名单管理")
        dialog.resize(450, 400)
        dialog.setMinimumSize(380, 300)

        layout = QVBoxLayout(dialog)

        # 标题
        title = QLabel("白名单用户（免删）")
        title.setStyleSheet(f"font-size:{FONT_SIZES['h3']}; font-weight:{FONT_WEIGHTS['semibold']};")
        layout.addWidget(title)

        # 列表
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["UID", "备注名"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        layout.addWidget(table, 1)

        def refresh_table():
            table.setRowCount(0)
            for info_item in self._whitelist.get_info():
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(info_item["uid"]))
                table.setItem(row, 1, QTableWidgetItem(info_item["name"] or "—"))

        refresh_table()

        # 操作行
        op_layout = QHBoxLayout()

        uid_input = QLineEdit()
        uid_input.setPlaceholderText("输入用户 UID")
        op_layout.addWidget(uid_input)

        name_input = QLineEdit()
        name_input.setPlaceholderText("备注名（可选）")
        op_layout.addWidget(name_input)

        btn_add = QPushButton("添加")
        btn_add.clicked.connect(lambda: (
            self._whitelist.add(uid_input.text().strip(), name_input.text().strip()),
            uid_input.clear(),
            name_input.clear(),
            refresh_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
        ))
        op_layout.addWidget(btn_add)

        btn_remove = QPushButton("删除选中")
        btn_remove.clicked.connect(lambda: (
            [self._whitelist.remove(table.item(table.currentRow(), 0).text())
             for _ in [0] if table.currentRow() >= 0],
            refresh_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
        ))
        op_layout.addWidget(btn_remove)
        layout.addLayout(op_layout)

        btn_clear = QPushButton("清空全部")
        btn_clear.setObjectName("danger")
        btn_clear.clicked.connect(lambda: (
            self._whitelist.clear(),
            refresh_table(),
            self._refresh_table(show_judgments=self._judgments is not None),
        ))
        layout.addWidget(btn_clear)

        dialog.exec()

    # ==================== 手动修改模式 ====================

    def _on_manual_toggle(self, checked: bool):
        """切换手动修改模式。"""
        self._manual_toggle = checked
        self._btn_manual.setText("✏️ 手动修改: 开" if checked else "✏️ 手动修改: 关")
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
        self._btn_filter.setText("🔍 只看广告: 开" if checked else "🔍 只看广告: 关")
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

        rpid_str = str(self._table.item(row, 0).text())  # 从序号列反查比较困难

        # 通过展平列表反查 rpid
        flat = []
        def walk(comments):
            for c in comments:
                flat.append(c)
                if c.replies:
                    walk(c.replies)
        walk(self._comments)
        if row >= len(flat):
            return
        c = flat[row]
        rpid_str = str(c.rpid)

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

        # 只看广告模式：过滤非广告行
        if self._show_ads_only and self._judgments:
            ad_rpids = {j.rpid for j in self._judgments.judgments if j.is_ad}
            flat = [(c, d) for c, d in flat if str(c.rpid) in ad_rpids]

        self._table.setRowCount(len(flat))
        self._table_title.setText(f"💬 评论列表 ({len(flat)} 条)")

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

            items = [
                QTableWidgetItem(str(i + 1)),
                QTableWidgetItem(prefix + c.username),
                QTableWidgetItem(c.content),
                QTableWidgetItem(str(c.likes)),
                QTableWidgetItem(""),
                QTableWidgetItem(""),
            ]

            # 序号、点赞、广告列居中
            for col in (0, 3, 4):
                items[col].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            items[2].setToolTip(c.content)

            is_whitelisted = self._whitelist.contains(c.uid)
            j = j_map.get(rpid_str)

            # ---- 广告判定着色 ----
            if is_whitelisted:
                # 白名单用户：白色背景，不可被删除
                items[4].setText("📋 白名单")
                items[4].setForeground(QColor(self._current_theme.BRAND_BLUE))
                items[5].setText("白名单免删")
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
