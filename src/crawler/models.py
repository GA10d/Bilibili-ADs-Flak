"""数据模型定义。供爬虫、service、CLI、GUI 各层共享。"""

from dataclasses import dataclass, field
from datetime import datetime
from pydantic import BaseModel


# ============================================================
# 评论数据模型
# ============================================================

class Comment(BaseModel):
    """单条评论（含嵌套楼中楼）。"""
    rpid: int
    parent_id: int | None = None     # None = 一级评论
    uid: int
    username: str
    content: str
    publish_time: datetime
    likes: int = 0
    is_deleted: bool = False
    replies: list["Comment"] = []    # 楼中楼子评论，自引用


# ============================================================
# 对外接口的数据类型（dataclass，方便 GUI/CLI 消费）
# ============================================================

@dataclass
class ProgressEvent:
    """爬取进度事件，通过回调推送给 GUI/CLI。"""
    bv_id: str
    phase: str                      # "fetching_top" | "fetching_replies" | "saving" | "completed"
    current_page: int
    page_size: int
    total_crawled: int
    estimated_total: int | None     # API 返回的评论总数，可能为 None
    message: str                    # 人类可读描述


@dataclass
class CrawlResult:
    """单个视频的完整爬取结果。"""
    bv_id: str
    video_title: str
    comments: list[Comment]
    total_count: int                # 实际爬到的评论总数
    crawl_time: float               # 耗时（秒）
    errors: list[str] = field(default_factory=list)


@dataclass
class VideoInfo:
    """视频基本信息（爬取前的预览）。"""
    bv_id: str
    title: str
    total_comments: int             # API 返回的评论总数
    cover_url: str


@dataclass
class CheckpointInfo:
    """断点信息（供 GUI 断点管理页展示）。"""
    bv_id: str
    cursor: int
    completed: bool
    saved_at: datetime
