"""核心爬虫：单视频评论的完整爬取（分页 + 楼中楼递归 + 限速 + 断点续爬）。

使用 B站旧版评论 API（x/v2/reply/main），通过 `next` 游标实现翻页。
"""

import asyncio
import random
import time
from datetime import datetime

import requests
from bilibili_api import video as bv_video, Credential
from loguru import logger

from src.config import Config
from src.crawler.models import Comment, ProgressEvent
from src.crawler.checkpoint import CheckpointManager

# B站评论 API 基础 URL
_COMMENT_API = "https://api.bilibili.com/x/v2/reply/main"


class CommentCrawler:
    """单个视频的评论爬虫。

    使用方式：
        crawler = CommentCrawler(config, checkpoint_manager)
        result = await crawler.crawl("BV1xx4y1z7EG", on_progress=my_callback)
    """

    def __init__(self, config: Config, checkpoint_mgr: CheckpointManager) -> None:
        self._config = config
        self._checkpoint = checkpoint_mgr
        self._credential: Credential | None = None
        self._cancelled = False

        if config.auth_mode == "cookie" and config.sessdata and config.bili_jct:
            self._credential = Credential(sessdata=config.sessdata, bili_jct=config.bili_jct)
            logger.info("已启用 Cookie 认证模式")
        else:
            logger.info("使用匿名模式（无需登录）")

    # ==================== 公开接口 ====================

    async def crawl(
        self,
        bv_id: str,
        on_progress: "callable | None" = None,
        on_comments: "callable | None" = None,
    ) -> tuple[list[Comment], str]:
        """爬取单个视频的全部评论。

        Returns:
            (comments, video_title): 评论树形列表 + 视频标题
        """
        start = time.perf_counter()
        self._cancelled = False

        # 1. 获取视频信息（标题 + oid）
        v = bv_video.Video(bvid=bv_id, credential=self._credential)
        info = await self._retry(lambda: v.get_info())
        video_title = info.get("title", bv_id)
        oid = info.get("aid")       # aid 即评论区 oid
        video_reply_total = info.get("stat", {}).get("reply", 0)
        logger.info(f"视频: {video_title}  |  oid={oid}")

        # 2. 构建请求头
        headers = self._build_headers(bv_id)

        # 3. 加载断点
        next_cursor = 0
        cp = self._checkpoint.load(bv_id)
        if cp and cp.cursor:
            next_cursor = cp.cursor
            logger.info(f"从断点续爬: next={next_cursor}")

        # 4. 分页拉取一级评论（next 游标翻页）
        all_comments: list[Comment] = []
        page = 0
        total_api = video_reply_total
        prev_cursor = 0
        expected_reply_count = 0
        fetched_reply_count = 0
        seen_root_rpids: set[int] = set()

        while not self._cancelled:
            crawled_count = len(all_comments) + fetched_reply_count
            page += 1
            self._emit_progress(on_progress, ProgressEvent(
                bv_id=bv_id, phase="fetching_top",
                current_page=page, page_size=self._config.page_size,
                total_crawled=crawled_count, estimated_total=total_api or None,
                message=f"正在拉取一级评论 第{page}页…",
            ))

            resp = await self._retry(
                lambda: asyncio.to_thread(self._fetch_page, oid, next_cursor, headers)
            )

            cursor = resp.get("cursor") or {}
            total_api = cursor.get("all_count", total_api)

            raw_replies = []
            if page == 1:
                raw_replies.extend(resp.get("top_replies") or [])
            raw_replies.extend(resp.get("replies") or [])
            page_comments: list[Comment] = []
            for raw in raw_replies:
                if self._cancelled:
                    break
                rpid = raw.get("rpid", 0)
                if rpid in seen_root_rpids:
                    continue
                seen_root_rpids.add(rpid)
                com = self._parse_comment(raw)
                all_comments.append(com)
                page_comments.append(com)

                # 楼中楼递归
                reply_count = raw.get("rcount", 0)
                if reply_count > 0:
                    expected_reply_count += reply_count
                    self._emit_progress(on_progress, ProgressEvent(
                        bv_id=bv_id, phase="fetching_replies",
                        current_page=page, page_size=self._config.page_size,
                        total_crawled=len(all_comments) + fetched_reply_count,
                        estimated_total=total_api or None,
                        message=f"正在拉取楼中楼 rpid={com.rpid}（{reply_count}条）…",
                    ))
                    try:
                        com.replies = await self._fetch_replies(oid, com.rpid)
                        fetched_reply_count += len(com.replies)
                        self._emit_progress(on_progress, ProgressEvent(
                            bv_id=bv_id, phase="fetching_replies",
                            current_page=page, page_size=self._config.page_size,
                            total_crawled=len(all_comments) + fetched_reply_count,
                            estimated_total=total_api or None,
                            message=f"已拉取楼中楼 rpid={com.rpid}（{len(com.replies)}/{reply_count}条）",
                        ))
                        if len(com.replies) < reply_count:
                            logger.warning(
                                f"楼中楼数量少于预期 rpid={com.rpid}: "
                                f"预期 {reply_count}, 实际 {len(com.replies)}"
                            )
                    except Exception as e:
                        logger.warning(f"楼中楼拉取失败 rpid={com.rpid}: {e}")

            if page_comments:
                self._emit_comments(on_comments, page_comments)

            # 游标推进
            next_cursor = cursor.get("next", 0)

            # next=0 时无条件重试一次（B站 API 偶发提前终止）
            if next_cursor == 0 and prev_cursor > 0:
                logger.info(f"next=0，用 prev={prev_cursor} 重试一次…")
                await asyncio.sleep(0.5)
                retry_resp = await self._retry(
                    lambda: asyncio.to_thread(self._fetch_page, oid, prev_cursor, headers)
                )
                retry_cursor = (retry_resp.get("cursor") or {}).get("next", 0)
                if retry_cursor != 0:
                    next_cursor = retry_cursor
                    logger.info(f"重试成功，新游标: {next_cursor}")

            if next_cursor == 0:
                break

            prev_cursor = next_cursor
            # 保存断点
            self._checkpoint.save(bv_id, cursor=next_cursor, completed=False)
            await self._rate_limit()

        # 5. 完成
        elapsed = time.perf_counter() - start
        if self._cancelled:
            logger.info(f"爬取已取消: {bv_id}, 已获取 {len(all_comments)} 条")
        else:
            self._checkpoint.save(bv_id, cursor=0, completed=True)

        total_crawled = self._count_comments(all_comments)
        logger.info(
            f"评论汇总: 一级={len(all_comments)}, 楼中楼={fetched_reply_count}/{expected_reply_count}, "
            f"总计={total_crawled}, API估计={total_api or '未知'}"
        )

        self._emit_progress(on_progress, ProgressEvent(
            bv_id=bv_id, phase="completed",
            current_page=page, page_size=self._config.page_size,
            total_crawled=total_crawled, estimated_total=total_api or None,
            message=f"爬取完成: {total_crawled} 条评论, 耗时 {elapsed:.1f}s",
        ))

        return all_comments, video_title

    def cancel(self) -> None:
        """发送取消信号。当前页处理完后停止。"""
        self._cancelled = True
        logger.info("收到取消信号，将在当前页完成后停止…")

    # ==================== 内部 ====================

    def _fetch_page(self, oid: int, next_cursor: int, headers: dict) -> dict:
        """同步请求一页一级评论（通过 asyncio.to_thread 调用）。"""
        params = {
            "oid": oid,
            "type": 1,
            "mode": 2,              # 2=按时间排序
            "ps": self._config.page_size,
        }
        if next_cursor > 0:
            params["next"] = next_cursor

        r = requests.get(_COMMENT_API, params=params, headers=headers, timeout=self._config.request_timeout)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"API 错误: code={data.get('code')}, message={data.get('message')}")
        return data.get("data") or {}

    async def _fetch_replies(self, oid: int, root_rpid: int) -> list[Comment]:
        """拉取某条一级评论的所有楼中楼回复（使用 bilibili_api 封装）。"""
        from bilibili_api import comment as bili_comment

        replies: list[Comment] = []
        page_index = 1

        while not self._cancelled:
            c = bili_comment.Comment(
                oid=oid,
                type_=bili_comment.CommentResourceType.VIDEO,
                rpid=root_rpid,
                credential=self._credential,
            )
            resp = await self._retry(
                lambda: c.get_sub_comments(page_index=page_index, page_size=self._config.page_size)
            )

            raw_replies = resp.get("replies") or []
            for raw in raw_replies:
                repl = self._parse_comment(raw, parent_id=root_rpid)
                replies.append(repl)

            # 分页信息
            page_info = resp.get("page") or {}
            total_count = page_info.get("count", 0)
            if total_count == 0:
                break
            total_pages = (total_count + self._config.page_size - 1) // self._config.page_size
            if page_index >= total_pages:
                break
            page_index += 1
            await self._rate_limit()

        return replies

    def _parse_comment(self, raw: dict, parent_id: int | None = None) -> Comment:
        """将 API 原始 dict 转为 Comment 模型。"""
        member = raw.get("member") or {}
        content = raw.get("content") or {}
        return Comment(
            rpid=raw.get("rpid", 0),
            parent_id=parent_id or raw.get("parent") or None,
            uid=raw.get("mid", 0),
            username=member.get("uname", ""),
            content=content.get("message", ""),
            publish_time=datetime.fromtimestamp(raw.get("ctime", 0)),
            likes=raw.get("like", 0),
            is_deleted=bool(raw.get("state", 0)),
        )

    def _build_headers(self, bv_id: str) -> dict:
        """构建 HTTP 请求头（含 Cookie）。"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://www.bilibili.com/video/{bv_id}",
        }
        if self._credential:
            cookie_str = f"SESSDATA={self._credential.sessdata}; bili_jct={self._credential.bili_jct}"
            headers["Cookie"] = cookie_str
        return headers

    async def _rate_limit(self) -> None:
        """延时 base + random(0, jitter) 秒。"""
        delay = self._config.delay_base + random.uniform(0, self._config.delay_jitter)
        await asyncio.sleep(delay)

    async def _retry(self, fn, max_retries: int | None = None):
        """带重试的请求包装。自动适配同步/异步函数。"""
        retries = max_retries if max_retries is not None else self._config.max_retries
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    return await result
                # 同步函数直接返回结果（已在调用前通过 to_thread 包装）
                return result
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"请求失败 (尝试 {attempt+1}/{retries+1}): {e}，{wait}s 后重试…")
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _emit_progress(on_progress, event: ProgressEvent) -> None:
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass   # 不因回调异常中断爬取

    @staticmethod
    def _emit_comments(on_comments, comments: list[Comment]) -> None:
        if on_comments:
            try:
                on_comments(comments)
            except Exception:
                pass   # 不因 UI 回调异常中断爬取

    @staticmethod
    def _count_comments(comments: list[Comment]) -> int:
        total = 0
        for comment in comments:
            total += 1 + CommentCrawler._count_comments(comment.replies)
        return total
