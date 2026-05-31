# 评论爬取模块 — GUI 对接文档

> **面向读者**：GUI 开发者。本文档涵盖 GUI 与爬取模块交互的全部接口、数据类型、调用模式与示例代码，无需阅读爬虫源码。

---

## 一、设计原则

爬取模块被设计为 **库**而非独立应用。CLI（`main.py`）只是该库的一个薄封装。GUI 和 CLI 共享同一个 `CrawlerService` 入口，唯一区别在于 **进度反馈方式**：

| 层 | 进度呈现 |
|----|---------|
| CLI | `loguru` 控制台日志 |
| GUI | `ProgressEvent` 回调 → 更新进度条 / 状态文本 |

---

## 二、架构分层

```
┌──────────────────────────────────────────────┐
│  GUI 层 (PyQt / Tkinter / Web)               │
│  ├─ 设置面板  ──── update_config()           │
│  ├─ 预览面板  ──── get_video_info()          │
│  ├─ 主流程    ──── crawl() + on_progress     │
│  ├─ 取消按钮  ──── cancel()                  │
│  └─ 断点管理  ──── list/remove_checkpoints() │
├──────────────────────────────────────────────┤
│  CrawlerService       ← 统一对外门面         │
│  ├─ CommentCrawler    ← 核心爬取逻辑         │
│  ├─ CheckpointManager ← 断点管理             │
│  ├─ Config            ← 配置                 │
│  └─ Models            ← 数据模型             │
└──────────────────────────────────────────────┘
```

---

## 三、唯一入口：`CrawlerService`

```python
from src.service import CrawlerService
```

GUI 开发者**只需与这一个类交互**，无需引入 `crawler/` 内部模块。

### 3.0 实例化

```python
from src.config import Config

# 方式1：使用默认配置（匿名模式，1.5s 限速，2层深度）
service = CrawlerService()

# 方式2：自定义配置
config = Config(
    auth_mode="cookie",
    sessdata="你的SESSDATA",
    bili_jct="你的bili_jct",
    delay_base=2.0,
    max_reply_depth=2,
)
service = CrawlerService(config)
```

### 3.1 配置读写

```python
# 获取当前配置
config: Config = service.get_config()
print(config.delay_base)   # → 1.5

# 动态修改（运行时即时生效，下次 crawl 使用新配置）
service.update_config(delay_base=2.0, max_reply_depth=1)
```

**GUI 设置面板典型用法**：用户在设置面板修改限速/深度/认证 → 调用 `service.update_config(**form_data)` → 下次爬取生效。

### 3.2 视频信息预览

```python
video: VideoInfo = await service.get_video_info("BV1xx4y1z7EG")
# video.title          → "视频标题"
# video.total_comments → 12345  (API 返回的评论总数)
# video.cover_url      → "https://i0.hdslb.com/bfs/..."
```

**典型用法**：用户输入 BV 号后，点击"预览"按钮，显示视频标题和评论总数以确认。

### 3.3 爬取单个视频

```python
def on_progress(event: ProgressEvent) -> None:
    # 更新 GUI 进度条和状态文本
    progress_bar.setValue(event.total_crawled)
    status_label.setText(event.message)
    QApplication.processEvents()   # PyQt 需强制刷新

result: CrawlResult = await service.crawl(
    "BV1xx4y1z7EG",
    on_progress=on_progress,
)
```

**`on_progress` 回调签名**：`Callable[[ProgressEvent], None]`，可选（传 `None` 则不推送进度）。

### 3.4 批量爬取

```python
results: list[CrawlResult] = await service.crawl_batch(
    ["BV1aa", "BV2bb", "BV3cc"],
    on_video_progress=on_progress,
)
```

串行执行，每个视频完成后推送一次 `completed` 阶段事件。

### 3.5 取消爬取

```python
service.cancel()   # 同步方法，立即返回。当前页处理完后停止，断点自动保存。
```

**典型用法**：用户点击"取消"按钮 → `service.cancel()` → 进度回调收到最后的 `completed` 事件 → 断开 UI 连接。

### 3.6 断点管理

```python
# 列出所有已保存的断点
checkpoints: list[CheckpointInfo] = service.list_checkpoints()
for cp in checkpoints:
    print(f"{cp.bv_id}: cursor={cp.cursor}, completed={cp.completed}, saved={cp.saved_at}")

# 删除单个断点（下次从头爬）
service.remove_checkpoint("BV1xx4y1z7EG")

# 清空全部断点
service.clear_all_checkpoints()
```

**典型用法**：GUI 提供"断点管理"页面，列出断点列表，支持删除/清空。

---

## 四、数据类型速查

> 所有类型定义在 `src/crawler/models.py`，可直接导入：
> ```python
> from src.crawler.models import Comment, ProgressEvent, CrawlResult, VideoInfo, CheckpointInfo
> ```

### 4.1 `Comment`（Pydantic 模型）

```python
class Comment(BaseModel):
    rpid: int                          # 评论唯一ID
    parent_id: int | None = None       # None = 一级评论；否则为父评论 rpid
    uid: int                           # 评论者 UID
    username: str                      # 评论者昵称
    content: str                       # 评论文本
    publish_time: datetime             # 发布时间
    likes: int = 0                     # 点赞数
    is_deleted: bool = False           # 是否已被删除
    replies: list["Comment"] = []      # 楼中楼（树形嵌套，自引用）
```

**常用方法**：

| 方法 | 说明 |
|------|------|
| `comment.model_dump()` | 转为字典 |
| `comment.model_dump_json(indent=2)` | 转为 JSON 字符串 |
| `Comment.model_validate_json(json_str)` | 从 JSON 反序列化 |

**展平树形结构**（用于表格展示）：

```python
def flatten(comment: Comment) -> list[dict]:
    """将树形 Comment 展平为二维表格行。"""
    rows = []
    def _walk(c: Comment, depth: int = 0):
        rows.append({"depth": depth, "rpid": c.rpid, "parent_id": c.parent_id,
                     "username": c.username, "content": c.content, "likes": c.likes})
        for reply in c.replies:
            _walk(reply, depth + 1)
    _walk(comment)
    return rows
```

### 4.2 `ProgressEvent`

```python
@dataclass
class ProgressEvent:
    bv_id: str                    # 当前视频 BV 号
    phase: str                    # "fetching_top" | "fetching_replies" | "completed"
    current_page: int             # 当前页号
    page_size: int                # 每页条数
    total_crawled: int            # 已爬取评论数（含楼中楼）
    estimated_total: int | None   # API 返回的评论总数（可能为 None）
    message: str                  # 人类可读描述，如 "正在拉取一级评论 第3页…"
```

**`phase` 取值含义**：

| phase | 含义 |
|-------|------|
| `"fetching_top"` | 正在拉取一级评论 |
| `"fetching_replies"` | 正在拉取某条评论的楼中楼 |
| `"completed"` | 爬取完成（无论成功/取消） |

### 4.3 `CrawlResult`

```python
@dataclass
class CrawlResult:
    bv_id: str                            # 视频 BV 号
    video_title: str                      # 视频标题
    comments: list[Comment]               # 评论树形列表
    total_count: int                      # 实际爬到的一级评论数
    crawl_time: float                     # 耗时（秒）
    errors: list[str]                     # 非致命错误信息（空列表 = 无错误）
```

### 4.4 `VideoInfo`

```python
@dataclass
class VideoInfo:
    bv_id: str                            # 视频 BV 号
    title: str                            # 视频标题
    total_comments: int                   # API 返回的评论总数
    cover_url: str                        # 封面图 URL
```

### 4.5 `CheckpointInfo`

```python
@dataclass
class CheckpointInfo:
    bv_id: str                            # 视频 BV 号
    cursor: int                           # 当前分页游标
    completed: bool                       # True = 已完成爬取
    saved_at: datetime                    # 最后保存时间
```

### 4.6 `Config`

```python
@dataclass
class Config:
    # 认证
    auth_mode: str = "anonymous"           # "anonymous" | "cookie"
    sessdata: str | None = None
    bili_jct: str | None = None

    # 爬取
    max_reply_depth: int = 2               # 递归深度（1=仅一级评论）
    delay_base: float = 1.5                # 基础延时（秒）
    delay_jitter: float = 0.5              # 随机抖动（秒）
    request_timeout: int = 15              # 单次请求超时（秒）
    max_retries: int = 3                   # 最大重试次数
    page_size: int = 20                    # 每页评论数

    # 输出
    output_dir: str = "data"
    output_format: str = "json"            # "json" | "csv"

    # 检查点
    checkpoint_dir: str = "data/checkpoints"

    # 日志
    log_dir: str = "logs"
    log_level: str = "INFO"
```

---

## 五、GUI 框架集成模式

### 5.1 PyQt / PySide 集成

```python
import asyncio
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QProgressBar, QLabel
from PyQt6.QtCore import Qt
from src.service import CrawlerService
from src.crawler.models import ProgressEvent, CrawlResult

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.service = CrawlerService()
        self._task = None          # 持有 asyncio Task 引用

        # ... UI 控件初始化 ...

    def on_start_clicked(self):
        """用户点击"开始爬取"按钮。"""
        bv_id = self.bv_input.text().strip()

        # 在后台事件循环中启动异步任务
        self._task = asyncio.ensure_future(
            self.service.crawl(bv_id, on_progress=self.on_progress)
        )
        self._task.add_done_callback(self.on_crawl_finished)

    def on_progress(self, event: ProgressEvent) -> None:
        """进度回调（在 asyncio 事件循环线程中执行）。"""
        # 关键：通过信号/slot 或 invokeMethod 切回主线程更新 UI
        self.progress_bar.setMaximum(event.estimated_total or 0)
        self.progress_bar.setValue(event.total_crawled)
        self.status_label.setText(event.message)

    def on_cancel_clicked(self):
        """用户点击"取消"按钮。"""
        self.service.cancel()

    def on_crawl_finished(self, task):
        """爬取完成。"""
        result: CrawlResult = task.result()
        if result.errors:
            self.status_label.setText(f"出错: {result.errors}")
        else:
            self.status_label.setText(f"完成: {result.total_count} 条, 耗时 {result.crawl_time:.1f}s")
            self.display_comments(result.comments)
```

**PyQt 注意要点**：

- `on_progress` 回调在 **asyncio 事件循环线程**中执行，非主线程。需要用 `QMetaObject.invokeMethod` 或信号/slot 更新 UI。
- 推荐使用 `qasync` 库（`pip install qasync`）来统一 Qt 和 asyncio 事件循环，避免线程问题。

**使用 `qasync` 的简化方案**：

```python
import qasync

class MainWindow(QMainWindow):
    async def on_start_clicked(self):
        bv_id = self.bv_input.text().strip()
        result = await self.service.crawl(bv_id, on_progress=self.on_progress)
        # 此处已在主线程，可直接更新 UI
        self.status_label.setText(f"完成: {result.total_count} 条")

# 启动
app = QApplication(sys.argv)
loop = qasync.QEventLoop(app)
asyncio.set_event_loop(loop)
# ...
```

### 5.2 Tkinter 集成

```python
import asyncio
import tkinter as tk
from tkinter import ttk

class TkApp:
    def __init__(self):
        self.root = tk.Tk()
        self.service = CrawlerService()
        self._loop = asyncio.new_event_loop()
        # ... UI 控件 ...

    def on_start(self):
        bv_id = self.bv_entry.get().strip()
        asyncio.run_coroutine_threadsafe(
            self._run_crawl(bv_id), self._loop
        )

    async def _run_crawl(self, bv_id: str):
        result = await self.service.crawl(bv_id, on_progress=self._on_progress)
        # 使用 after() 切回主线程
        self.root.after(0, self._display_result, result)

    def _on_progress(self, event):
        self.root.after(0, self._update_progress, event)

    def _update_progress(self, event):
        self.progress_bar["value"] = event.total_crawled
        self.status_label["text"] = event.message

    def run(self):
        # 在后台线程运行 asyncio 事件循环
        import threading
        t = threading.Thread(target=self._loop.run_forever, daemon=True)
        t.start()
        self.root.mainloop()
```

### 5.3 Web 前端（Flask/FastAPI + SSE）

```python
# 后端 (FastAPI)
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from src.service import CrawlerService
import json

app = FastAPI()
service = CrawlerService()

async def event_stream(bv_id: str):
    async def on_progress(event):
        yield f"data: {json.dumps(event.__dict__, default=str)}\n\n"

    result = await service.crawl(bv_id, on_progress=on_progress)
    yield f"data: {json.dumps({'type': 'done', 'result': ...})}\n\n"

@app.get("/api/crawl/{bv_id}")
async def crawl_stream(bv_id: str):
    return StreamingResponse(event_stream(bv_id), media_type="text/event-stream")
```

```javascript
// 前端 (JavaScript)
const source = new EventSource(`/api/crawl/${bvId}`);
source.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.phase) {
        progressBar.value = data.total_crawled;
        statusText.textContent = data.message;
    }
};
```

---

## 六、完整交互流程示例

### 6.1 单视频爬取（标准流程）

```
1. 用户输入 BV 号，点击"预览"
2. GUI 调用 video = await service.get_video_info(bv_id)
3. 显示: 视频标题、封面、评论总数
4. 用户点击"开始爬取"
5. GUI 调用 result = await service.crawl(bv_id, on_progress=callback)
6. 回调实时更新进度条和状态文本
7. 完成后展示 CrawlResult
   - 如果 result.errors 非空 → 显示错误对话框
   - 否则 → 在树形表格中展示评论
8. 用户点击"保存" → GUI 调用 result.comments 序列化为 JSON/CSV
```

### 6.2 批量爬取

```
1. 用户导入 BV 列表文件
2. GUI 调用 results = await service.crawl_batch(bv_list, on_progress=callback)
3. 每个视频完成后回调一次 (phase="completed")
4. 全部完成后展示统计: 总视频数、成功/失败数
```

### 6.3 取消

```
1. 爬取进行中，用户点击"取消"
2. GUI 调用 service.cancel()
3. 爬虫在当前页处理完后停止，保存断点
4. 进度回调收到 phase="completed" 事件
5. GUI 显示"已取消"，断开回调连接
```

### 6.4 断点管理

```
1. 用户进入"断点管理"页
2. GUI 调用 checkpoints = service.list_checkpoints()
3. 以表格展示: BV号、游标位置、是否完成、保存时间
4. 用户选中某条 → 点击"删除"
5. GUI 调用 service.remove_checkpoint(bv_id)
6. GUI 调用 service.clear_all_checkpoints() → 全部清空
```

---

## 七、线程安全说明

| 方法 | 同步/异步 | 线程约束 |
|------|----------|---------|
| `update_config()` | 同步 | 任意线程安全 |
| `get_config()` | 同步 | 任意线程安全 |
| `cancel()` | 同步 | 任意线程安全 |
| `list_checkpoints()` | 同步 | 任意线程安全 |
| `remove_checkpoint()` | 同步 | 任意线程安全 |
| `clear_all_checkpoints()` | 同步 | 任意线程安全 |
| `get_video_info()` | 异步 (`async`) | 必须在 asyncio 事件循环中调用 |
| `crawl()` | 异步 (`async`) | 必须在 asyncio 事件循环中调用 |
| `crawl_batch()` | 异步 (`async`) | 必须在 asyncio 事件循环中调用 |
| `on_progress` 回调 | 同步 | 在 asyncio 事件循环线程中执行 |

---

## 八、错误处理指南

```python
result: CrawlResult = await service.crawl(bv_id, on_progress=callback)

if result.errors:
    # result.errors 是 list[str]，包含非致命错误信息
    # 例如: ["网络超时"], ["BV号无效: BVxxx"]
    for err in result.errors:
        show_error_dialog(err)
else:
    # result.comments 一定有数据（可能为空列表）
    display(result.comments)
```

**常见错误类型**：

| 错误 | 表现 | 处理建议 |
|------|------|---------|
| 无效 BV 号 | `errors=["BV号无效: ..."]` | 提示用户检查 BV 号 |
| 网络超时 | 自动重试 3 次。若全部失败则 `errors` 包含超时信息 | 提示用户检查网络 |
| API 限流 | 自动重试。若持续失败则报错 | 建议增大 `delay_base` |
| 爬取被取消 | `result.comments` 为已爬到的部分，无 error | 正常处理，断点已保存 |

---

## 九、数据持久化

GUI 负责最终的数据保存，爬取模块只负责返回内存中的结构化数据。

### 保存为 JSON

```python
import json
from pathlib import Path

def save_as_json(result: CrawlResult, output_dir: str) -> None:
    path = Path(output_dir) / f"{result.bv_id}.json"
    data = [c.model_dump(mode="json") for c in result.comments]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
```

### 保存为 CSV

```python
import csv

def save_as_csv(result: CrawlResult, output_dir: str) -> None:
    path = Path(output_dir) / f"{result.bv_id}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["rpid", "parent_id", "uid", "username", "content", "publish_time", "likes", "is_deleted"])
        for c in result.comments:
            _write_row(w, c)

def _write_row(w, c, parent_id=None):
    w.writerow([c.rpid, parent_id, c.uid, c.username, c.content,
                c.publish_time.isoformat(), c.likes, c.is_deleted])
    for reply in c.replies:
        _write_row(w, reply, parent_id=c.rpid)
```

---

## 十、完整可运行示例（最小 GUI）

```python
"""最小 PyQt GUI 示例：输入 BV 号 → 爬取 → 显示评论数。"""
import sys
import asyncio
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QLineEdit, QPushButton, QProgressBar, QLabel, QTextEdit,
)
from PyQt6.QtCore import pyqtSignal, QObject
from src.service import CrawlerService
from src.crawler.models import ProgressEvent, CrawlResult


class ProgressBridge(QObject):
    """将 asyncio 回调桥接到 Qt 主线程。"""
    updated = pyqtSignal(object)

    def on_progress(self, event: ProgressEvent):
        self.updated.emit(event)


class MinimalGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("B站评论爬取 - 最小示例")
        self.service = CrawlerService()
        self.bridge = ProgressBridge()
        self.bridge.updated.connect(self._update_ui)

        # UI 布局
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.bv_input = QLineEdit(placeholderText="输入 BV 号…")
        layout.addWidget(self.bv_input)

        self.btn_start = QPushButton("开始爬取")
        self.btn_start.clicked.connect(self._on_start)
        layout.addWidget(self.btn_start)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(lambda: self.service.cancel())
        layout.addWidget(self.btn_cancel)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        self.status = QLabel("就绪")
        layout.addWidget(self.status)

        self.output = QTextEdit(readOnly=True)
        layout.addWidget(self.output)

    def _on_start(self):
        bv_id = self.bv_input.text().strip()
        if not bv_id:
            return
        self.btn_start.setEnabled(False)
        asyncio.ensure_future(self._run_crawl(bv_id))

    async def _run_crawl(self, bv_id: str):
        result = await self.service.crawl(bv_id, on_progress=self.bridge.on_progress)
        self.btn_start.setEnabled(True)
        self.output.setText(
            f"视频: {result.video_title}\n"
            f"一级评论: {result.total_count} 条\n"
            f"耗时: {result.crawl_time:.1f} 秒\n"
            f"错误: {result.errors or '无'}"
        )

    def _update_ui(self, event: ProgressEvent):
        self.progress.setMaximum(event.estimated_total or 0)
        self.progress.setValue(event.total_crawled)
        self.status.setText(event.message)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MinimalGUI()
    window.show()
    sys.exit(app.exec())
```

---

## 十一、快速参考卡片

```
┌─────────────────────────────────────────────────────────┐
│  GUI 只需记住三个导入                                     │
│                                                         │
│  from src.service import CrawlerService                 │
│  from src.config import Config                          │
│  from src.crawler.models import (                       │
│      ProgressEvent, CrawlResult, VideoInfo,             │
│      CheckpointInfo, Comment,                           │
│  )                                                      │
│                                                         │
│  核心调用:                                               │
│  service = CrawlerService()                             │
│  result = await service.crawl(bv_id, on_progress=cb)    │
│  service.cancel()                                       │
└─────────────────────────────────────────────────────────┘
```
