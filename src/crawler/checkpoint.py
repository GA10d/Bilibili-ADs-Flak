"""断点续爬管理。每个视频的状态存为独立 JSON 文件。"""

import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.crawler.models import CheckpointInfo


class CheckpointManager:
    """管理所有视频的爬取进度。

    文件布局：
        {checkpoint_dir}/
            BV1xx4y1z7EG.json
            BV1abc123def4.json
            ...

    单文件结构：
        {
            "bv_id": "BV1xx4y1z7EG",
            "cursor": 456,
            "completed": false,
            "saved_at": "2026-05-31T12:00:00"
        }
    """

    def __init__(self, checkpoint_dir: str | Path) -> None:
        self._dir = Path(checkpoint_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ---- 读 ----

    def load(self, bv_id: str) -> CheckpointInfo | None:
        """加载指定视频的断点。若无则返回 None。"""
        file = self._file_for(bv_id)
        if not file.exists():
            return None
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            return CheckpointInfo(
                bv_id=data["bv_id"],
                cursor=data["cursor"],
                completed=data["completed"],
                saved_at=datetime.fromisoformat(data["saved_at"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning(f"断点文件损坏，已忽略: {file}")
            file.unlink(missing_ok=True)
            return None

    def is_completed(self, bv_id: str) -> bool:
        cp = self.load(bv_id)
        return cp is not None and cp.completed

    def get_cursor(self, bv_id: str) -> int:
        cp = self.load(bv_id)
        return cp.cursor if cp else 0

    # ---- 写 ----

    def save(self, bv_id: str, cursor: int, completed: bool) -> None:
        """保存断点（每次翻页后调用）。"""
        payload = {
            "bv_id": bv_id,
            "cursor": cursor,
            "completed": completed,
            "saved_at": datetime.now().isoformat(),
        }
        self._file_for(bv_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---- 列表 / 删除 ----

    def list_all(self) -> list[CheckpointInfo]:
        """列出所有断点文件。"""
        result: list[CheckpointInfo] = []
        for file in sorted(self._dir.glob("*.json")):
            bv_id = file.stem
            cp = self.load(bv_id)
            if cp:
                result.append(cp)
        return result

    def remove(self, bv_id: str) -> None:
        """删除指定视频的断点。"""
        file = self._file_for(bv_id)
        if file.exists():
            file.unlink()
            logger.info(f"已删除断点: {bv_id}")

    def clear_all(self) -> None:
        """清空所有断点。"""
        for file in self._dir.glob("*.json"):
            file.unlink()
        logger.info("已清空全部断点")

    # ---- 内部 ----

    def _file_for(self, bv_id: str) -> Path:
        return self._dir / f"{bv_id}.json"
