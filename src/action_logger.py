"""操作日志模块 —— 记录用户操作、系统响应、结果到 logs/actions.log。

每条日志格式：
    [2026-05-31 14:30:00.123] [操作类型] 描述 → 结果
    [2026-05-31 14:30:00.456] [操作类型]   详细信息（多行缩进）

使用方式：
    from src.action_logger import ActionLogger
    logger = ActionLogger()
    logger.log("GUI启动", "检查 .env 配置", "auth_mode=cookie, SESSDATA 已设置")
    logger.log("Cookie导入", "自动从 Chrome 读取", "成功", details="SESSDATA长度=128, bili_jct长度=32")
    logger.log("验证凭证", "调用 B站 user.get_self_info", "失败", error="-400: 请求错误")
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


# ---- 时间戳格式 ----

def _local_now() -> str:
    """返回本地时间字符串，精确到毫秒。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"


# ---- 单例 ActionLogger ----

class ActionLogger:
    """线程安全的操作日志记录器，写入 logs/actions.log。

    每次调用 log() 立即 flush，确保崩溃时不丢数据。
    """

    _instance: "ActionLogger | None" = None
    _lock = threading.Lock()

    def __init__(self, log_path: Path | None = None):
        if log_path is None:
            log_path = Path(__file__).parent.parent / "logs" / "actions.log"
        self._path = log_path
        self._file: TextIO | None = None
        self._open()

    def _open(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")

    @classmethod
    def get(cls, log_path: Path | None = None) -> "ActionLogger":
        """获取全局单例。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(log_path)
        return cls._instance

    def log(
        self,
        action: str,
        description: str,
        result: str = "",
        *,
        error: str | None = None,
        details: str | None = None,
    ):
        """记录一条操作日志。

        Args:
            action:      操作类型，如 "GUI启动"、"Cookie导入"、"验证凭证"、"爬取评论"
            description: 操作描述
            result:      操作结果（成功/失败/状态信息）
            error:       错误信息（失败时）
            details:     补充详细信息
        """
        ts = _local_now()
        lines = [f"[{ts}] [{action}] {description}"]

        if result:
            suffix = f" → {result}"
            if error:
                suffix += f" | 错误: {error}"
            lines[0] += suffix

        if details:
            lines.append(f"{' ' * (len(ts) + 3)}  {details}")

        with self._lock:
            if self._file is None:
                self._open()
            for line in lines:
                self._file.write(line + "\n")
            self._file.flush()

    def separator(self, char: str = "─", width: int = 60):
        """写一条分隔线。"""
        with self._lock:
            if self._file is None:
                self._open()
            self._file.write(char * width + "\n")
            self._file.flush()


# ---- 便捷函数 ----

_LOG: ActionLogger | None = None


def _get() -> ActionLogger:
    global _LOG
    if _LOG is None:
        _LOG = ActionLogger()
    return _LOG


def log_action(action: str, description: str, result: str = "", *, error: str | None = None, details: str | None = None):
    """模块级快捷函数，无需手动创建 ActionLogger 实例。"""
    _get().log(action, description, result, error=error, details=details)


def log_separator(char: str = "─"):
    _get().separator(char)
