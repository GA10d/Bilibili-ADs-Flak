"""自动删除模块。

严格的权限校验：删除前必须验证当前登录用户是否为视频 UP 主本人。
"""

import asyncio
import random
import time
from dataclasses import dataclass, field

from bilibili_api import comment as bili_comment, video as bv_video, Credential
from loguru import logger

from src.config import Config
from src.crawler.models import Comment
from src.agent.ad_detector import BatchAdJudgment


# ============================================================
# 数据模型
# ============================================================

@dataclass
class DeleteResult:
    """单条评论的删除结果。"""
    rpid: str
    success: bool
    message: str = ""              # 成功时为 "ok"，失败时为错误信息


@dataclass
class BatchDeleteResult:
    """批量删除结果。"""
    bv_id: str
    video_title: str
    total_to_delete: int           # 计划删除数
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0         # 跳过的（非本人视频等情况）
    results: list[DeleteResult] = field(default_factory=list)
    dry_run: bool = False
    duration: float = 0.0          # 耗时（秒）

    @property
    def all_success(self) -> bool:
        return self.failed_count == 0 and self.success_count > 0


# ============================================================
# 权限校验
# ============================================================

class PermissionError(Exception):
    """权限不足异常。"""


async def verify_video_ownership(bv_id: str, credential: Credential) -> tuple[int, str]:
    """验证当前登录用户是否为视频 UP 主。

    Returns:
        (owner_uid, video_title): 视频拥有者 UID + 视频标题

    Raises:
        PermissionError: 当前用户不是 UP 主，或未登录
    """
    from bilibili_api import user as bv_user

    # 获取视频 UP 主 UID
    v = bv_video.Video(bvid=bv_id, credential=credential)
    info = await v.get_info()
    owner_uid = info.get("owner", {}).get("mid", 0)
    video_title = info.get("title", bv_id)

    if not owner_uid:
        raise PermissionError(f"无法获取视频 {bv_id} 的 UP 主信息")

    # 获取当前登录用户 UID
    self_info = await bv_user.get_self_info(credential=credential)
    my_uid = self_info.get("mid", 0)

    if not my_uid:
        raise PermissionError("无法获取当前用户信息，请确认 Cookie 有效")

    if my_uid != owner_uid:
        raise PermissionError(
            f"视频 {bv_id}（《{video_title}》）不属于当前登录用户。"
            f"\n  视频 UP 主 UID: {owner_uid}"
            f"\n  当前用户 UID: {my_uid}"
            f"\n  安全规则：只能删除自己视频下的评论。"
        )

    logger.info(f"权限验证通过: 视频《{video_title}》属于当前用户 (UID={my_uid})")
    return owner_uid, video_title


# ============================================================
# 删除器
# ============================================================

class AdDeleter:
    """广告评论删除器。

    使用方式：
        deleter = AdDeleter(config)
        result = await deleter.delete(
            bv_id="BVxxx", oid=123, judgments=batch_judgment,
            dry_run=False,
        )
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._credential: Credential | None = None
        self._cancelled = False

        if config.sessdata and config.bili_jct:
            self._credential = Credential(sessdata=config.sessdata, bili_jct=config.bili_jct)
        else:
            raise PermissionError("删除需要登录。请提供 SESSDATA + bili_jct")

    # ---- 配置阈值 ----
    DELETE_RATE_PER_MINUTE = 10       # 默认每分钟最多删除数
    DELETE_DELAY_JITTER = 0.3         # 间隔随机抖动

    async def delete(
        self,
        bv_id: str,
        oid: int,
        judgments: BatchAdJudgment,
        *,
        dry_run: bool = False,
        min_confidence: float | None = None,
        delete_rate_per_minute: int | None = None,
    ) -> BatchDeleteResult:
        """删除广告评论。

        删除前自动校验视频归属。

        Args:
            bv_id: 视频 BV 号
            oid: 评论区 oid
            judgments: 广告判定结果（只删除 is_ad=True 的）
            dry_run: True=模拟删除不实际执行
            min_confidence: 最低置信度（暂未使用，AdDetector 暂不输出置信度）
            delete_rate_per_minute: 每分钟最大删除数，默认 10

        Returns:
            BatchDeleteResult
        """
        start = time.perf_counter()
        self._cancelled = False
        rate = delete_rate_per_minute or self.DELETE_RATE_PER_MINUTE

        # ====== 1. 权限校验 ======
        try:
            owner_uid, video_title = await verify_video_ownership(bv_id, self._credential)
        except PermissionError as e:
            logger.error(str(e))
            return BatchDeleteResult(
                bv_id=bv_id, video_title=bv_id, total_to_delete=0,
                skipped_count=sum(1 for j in judgments.judgments if j.is_ad),
                dry_run=dry_run, duration=time.perf_counter() - start,
                results=[DeleteResult(rpid=j.rpid, success=False, message=str(e))
                         for j in judgments.judgments if j.is_ad],
            )

        # ====== 2. 筛选待删除评论 ======
        ad_judgments = [j for j in judgments.judgments if j.is_ad]
        if not ad_judgments:
            logger.info("无广告评论，跳过删除")
            return BatchDeleteResult(
                bv_id=bv_id, video_title=video_title,
                total_to_delete=0, dry_run=dry_run,
                duration=time.perf_counter() - start,
            )

        logger.info(
            f"待删除: {len(ad_judgments)} 条广告评论  |  "
            f"限速: {rate} 条/分钟  |  "
            f"模式: {'模拟(dry-run)' if dry_run else '实际删除'}"
        )

        # ====== 3. 逐条删除 ======
        results: list[DeleteResult] = []
        interval = 60.0 / rate       # 每条之间的间隔（秒）

        for i, j in enumerate(ad_judgments, 1):
            if self._cancelled:
                logger.info("收到取消信号，停止删除")
                break

            if dry_run:
                results.append(DeleteResult(rpid=j.rpid, success=True, message="dry-run"))
                logger.info(f"  [dry-run] [{i}/{len(ad_judgments)}] rpid={j.rpid} | {j.reason} ({j.ad_type})")
            else:
                result = await self._delete_one(oid=oid, rpid=j.rpid, judgment=j)
                results.append(result)
                status = "ok" if result.success else f"FAIL: {result.message}"
                logger.info(f"  [{i}/{len(ad_judgments)}] rpid={j.rpid} | {status}")

            # 限速（最后一条不需要等）
            if i < len(ad_judgments):
                jitter = random.uniform(-self.DELETE_DELAY_JITTER, self.DELETE_DELAY_JITTER)
                await asyncio.sleep(max(0, interval + jitter))

        # ====== 4. 汇总 ======
        elapsed = time.perf_counter() - start
        success = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success)

        logger.info(
            f"删除完成: 成功 {success}, 失败 {failed}, 总计 {len(results)}  |  "
            f"耗时 {elapsed:.1f}s"
        )

        return BatchDeleteResult(
            bv_id=bv_id, video_title=video_title,
            total_to_delete=len(ad_judgments),
            success_count=success, failed_count=failed,
            results=results, dry_run=dry_run, duration=elapsed,
        )

    def cancel(self) -> None:
        """取消删除操作。"""
        self._cancelled = True

    # ==================== 内部 ====================

    async def _delete_one(
        self, oid: int, rpid: int, judgment,
        max_retries: int = 3,
    ) -> DeleteResult:
        """删除单条评论，带重试。"""
        last_error = ""
        for attempt in range(max_retries + 1):
            try:
                c = bili_comment.Comment(
                    oid=oid,
                    type_=bili_comment.CommentResourceType.VIDEO,
                    rpid=rpid,
                    credential=self._credential,
                )
                await c.delete()
                return DeleteResult(rpid=str(rpid), success=True, message="ok")
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(f"删除失败 rpid={rpid} (尝试 {attempt+1}/{max_retries+1}): {e}，{wait}s 后重试")
                    await asyncio.sleep(wait)

        return DeleteResult(rpid=str(rpid), success=False, message=last_error)
