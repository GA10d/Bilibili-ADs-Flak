"""CrawlerService — 爬取模块对上层（CLI / GUI）的统一入口。"""

import asyncio
import time

from loguru import logger

from src.config import Config
from src.crawler.checkpoint import CheckpointManager
from src.crawler.crawler import CommentCrawler
from src.crawler.models import (
    CheckpointInfo,
    CrawlResult,
    ProgressEvent,
    VideoInfo,
)


class CrawlerService:
    """对外统一门面。CLI 和 GUI 共用同一实例。"""

    def __init__(self, config: Config | None = None) -> None:
        self._config = config or Config()
        self._checkpoint = CheckpointManager(self._config.checkpoint_path)
        self._crawler: CommentCrawler | None = None

    # ==================== 配置 ====================

    def update_config(self, **kwargs) -> None:
        """动态更新配置项，运行时即时生效（需要重新创建 crawler）。"""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
            else:
                logger.warning(f"未知配置项: {key}")
        self._crawler = None   # 下次调用 crawl 时用新配置重建

    def get_config(self) -> Config:
        return self._config

    # ==================== 视频信息 ====================

    async def get_video_info(self, bv_id: str) -> VideoInfo:
        """查询视频基本信息（不爬取评论）。"""
        from bilibili_api import video as bv_video, Credential

        cred = self._make_credential()
        v = bv_video.Video(bvid=bv_id, credential=cred)
        info = await v.get_info()
        return VideoInfo(
            bv_id=bv_id,
            title=info.get("title", bv_id),
            total_comments=info.get("stat", {}).get("reply", 0),
            cover_url=info.get("pic", ""),
        )

    # ==================== 爬取 ====================

    async def crawl(
        self,
        bv_id: str,
        on_progress: "callable[[ProgressEvent], None] | None" = None,
    ) -> CrawlResult:
        """爬取单个视频的全部评论。"""
        start = time.perf_counter()
        errors: list[str] = []

        try:
            crawler = self._get_crawler()
            comments, title = await crawler.crawl(bv_id, on_progress=on_progress)
        except Exception as e:
            logger.exception(f"爬取失败: {bv_id}")
            errors.append(str(e))
            comments, title = [], bv_id

        return CrawlResult(
            bv_id=bv_id,
            video_title=title,
            comments=comments,
            total_count=len(comments),
            crawl_time=time.perf_counter() - start,
            errors=errors,
        )

    async def crawl_batch(
        self,
        bv_ids: list[str],
        on_video_progress: "callable[[ProgressEvent], None] | None" = None,
    ) -> list[CrawlResult]:
        """批量爬取多个视频（串行）。"""
        results: list[CrawlResult] = []
        for bv_id in bv_ids:
            result = await self.crawl(bv_id, on_progress=on_video_progress)
            results.append(result)
        return results

    # ==================== 控制 ====================

    def cancel(self) -> None:
        """发送取消信号。"""
        if self._crawler:
            self._crawler.cancel()

    # ==================== 断点 ====================

    def list_checkpoints(self) -> list[CheckpointInfo]:
        return self._checkpoint.list_all()

    def remove_checkpoint(self, bv_id: str) -> None:
        self._checkpoint.remove(bv_id)

    def clear_all_checkpoints(self) -> None:
        self._checkpoint.clear_all()

    # ==================== 内部 ====================

    def _get_crawler(self) -> CommentCrawler:
        """惰性创建爬虫实例（配置变更后重建）。"""
        if self._crawler is None:
            self._crawler = CommentCrawler(self._config, self._checkpoint)
        return self._crawler

    def _make_credential(self):
        """按 auth_mode 构造 Credential 或返回 None。"""
        if self._config.auth_mode == "cookie" and self._config.sessdata and self._config.bili_jct:
            from bilibili_api import Credential
            return Credential(sessdata=self._config.sessdata, bili_jct=self._config.bili_jct)
        return None
