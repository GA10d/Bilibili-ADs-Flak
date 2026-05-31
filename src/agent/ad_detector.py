"""广告检测 Agent。

职责：
1. 从 prompts/ 目录加载系统提示词（pt1 + CSV 样本 + pt2）
2. 调用 DeepSeek JSON 模式批量检测评论是否广告
3. 返回结构化的 BatchAdJudgment
"""

import csv
import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from loguru import logger

from src.llm.deepseek import DeepSeekClient, ChatMessage


# ============================================================
# 数据模型
# ============================================================

@dataclass
class CommentAdJudgment:
    """单条评论的广告判定结果。"""
    rpid: str
    is_ad: bool
    ad_type: Optional[str] = None   # 售卖/引流/诈骗/色情/刷粉刷量/其他广告，非广告时为 null
    reason: str = ""                # 判定理由简述


@dataclass
class BatchAdJudgment:
    """批量评论的判定结果。"""
    judgments: list[CommentAdJudgment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"judgments": [asdict(j) for j in self.judgments]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "BatchAdJudgment":
        """从 API 返回的 dict 构建。"""
        j_list = data.get("judgments", [])
        judgments = [
            CommentAdJudgment(
                rpid=j.get("rpid", ""),
                is_ad=j.get("is_ad", False),
                ad_type=j.get("ad_type"),
                reason=j.get("reason", ""),
            )
            for j in j_list
        ]
        return cls(judgments=judgments)


# ============================================================
# Prompt 构建
# ============================================================

def _extract_csv_samples(csv_path: Path) -> str:
    """从 CSV 中提取所有非空文本格子，换行拼接。

    自动尝试 utf-8-sig、gbk、gb2312 编码。
    """
    texts: list[str] = []
    seen: set[str] = set()

    # 尝试多种编码
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb2312", "gb18030"):
        try:
            with open(csv_path, encoding=encoding) as f:
                reader = csv.reader(f)
                for row in reader:
                    for cell in row:
                        cell = cell.strip()
                        if cell and cell not in seen:
                            texts.append(cell)
                            seen.add(cell)
            break  # 成功则跳出
        except (UnicodeDecodeError, UnicodeError):
            continue

    # 按序号格式化
    numbered = [f"{i}. {t}" for i, t in enumerate(texts, 1)]
    return "\n".join(numbered)


def build_system_prompt(prompts_dir: str | Path = "prompts") -> str:
    """组装系统提示词：pt1 + CSV 样本 + pt2。"""
    root = Path(prompts_dir)
    pt1_path = root / "ads_guard_pt1.txt"
    csv_path = root / "ads_samples.csv"
    pt2_path = root / "ads_guard_pt2.txt"

    if not pt1_path.exists():
        raise FileNotFoundError(f"Prompt 文件缺失: {pt1_path}")
    if not pt2_path.exists():
        raise FileNotFoundError(f"Prompt 文件缺失: {pt2_path}")

    pt1 = pt1_path.read_text(encoding="utf-8")
    pt2 = pt2_path.read_text(encoding="utf-8")

    if csv_path.exists():
        samples = _extract_csv_samples(csv_path)
    else:
        samples = "(无样本)"

    return f"{pt1}\n{samples}\n\n{pt2}"


def _build_user_message(comments: list[dict]) -> str:
    """将评论列表格式化为 user prompt。"""
    items = []
    for c in comments:
        parent = c.get("parent_id") or ""
        items.append({
            "rpid": str(c["rpid"]),
            "content": c.get("content", ""),
            "parent_id": str(parent),
        })
    return json.dumps(items, ensure_ascii=False, indent=2)


# ============================================================
# Agent
# ============================================================

class AdDetector:
    """广告检测 Agent。

    使用方式：
        detector = AdDetector(client, prompts_dir="prompts")
        result = await detector.detect([
            {"rpid": "123", "content": "加微信xxx", "parent_id": ""},
            {"rpid": "456", "content": "up讲得真好", "parent_id": ""},
        ])
        print(result.to_json())
    """

    # 每批检测的最大评论数（避免 token 超限）
    MAX_BATCH_SIZE = 50

    def __init__(
        self,
        client: DeepSeekClient,
        prompts_dir: str = "prompts",
    ) -> None:
        self._client = client
        self._system_prompt = build_system_prompt(prompts_dir)
        logger.info(f"AdDetector 初始化完成，system prompt 长度: {len(self._system_prompt)} 字符")

    @property
    def system_prompt(self) -> str:
        """获取当前生效的 system prompt（调试用）。"""
        return self._system_prompt

    def reload_prompts(self, prompts_dir: str = "prompts") -> None:
        """重新加载 prompt（修改文件后热更新）。"""
        self._system_prompt = build_system_prompt(prompts_dir)
        logger.info(f"Prompt 已重新加载，长度: {len(self._system_prompt)} 字符")

    # ==================== 检测接口 ====================

    async def detect(self, comments: list[dict], on_progress=None) -> BatchAdJudgment:
        """批量检测评论是否为广告。

        Args:
            comments: 评论列表
            on_progress: 可选回调 (current: int, total: int) → None

        Returns:
            BatchAdJudgment
        """
        if not comments:
            return BatchAdJudgment()

        total = len(comments)

        # 超量分批
        if total > self.MAX_BATCH_SIZE:
            batches = (total - 1) // self.MAX_BATCH_SIZE + 1
            logger.info(f"评论数 {total} 超过单批上限，分 {batches} 批处理")
            all_judgments: list[CommentAdJudgment] = []
            for idx, i in enumerate(range(0, total, self.MAX_BATCH_SIZE)):
                chunk = comments[i:i + self.MAX_BATCH_SIZE]
                result = await self._detect_batch(chunk)
                all_judgments.extend(result.judgments)
                if on_progress:
                    on_progress(min(i + self.MAX_BATCH_SIZE, total), total)
            return BatchAdJudgment(judgments=all_judgments)

        result = await self._detect_batch(comments)
        if on_progress:
            on_progress(total, total)
        return result

    async def detect_single(self, rpid: str, content: str, parent_id: str = "") -> CommentAdJudgment:
        """检测单条评论（轻量封装）。"""
        result = await self.detect([{"rpid": rpid, "content": content, "parent_id": parent_id}])
        return result.judgments[0] if result.judgments else CommentAdJudgment(
            rpid=rpid, is_ad=False, reason="无结果"
        )

    def detect_sync(self, comments: list[dict]) -> BatchAdJudgment:
        """同步检测（适用于非 async 场景）。"""
        if not comments:
            return BatchAdJudgment()
        return self._detect_batch_sync(comments)

    # ==================== 内部 ====================

    async def _detect_batch(self, comments: list[dict]) -> BatchAdJudgment:
        user_msg = _build_user_message(comments)
        logger.info(f"检测 {len(comments)} 条评论…")

        resp = await self._client.chat_json(
            [ChatMessage("user", user_msg)],
            system=self._system_prompt,
            temperature=0.1,
            max_tokens=4096,
        )
        return self._parse_response(resp, comments)

    def _detect_batch_sync(self, comments: list[dict]) -> BatchAdJudgment:
        user_msg = _build_user_message(comments)
        logger.info(f"检测 {len(comments)} 条评论（同步）…")

        resp = self._client.chat_json_sync(
            [ChatMessage("user", user_msg)],
            system=self._system_prompt,
            temperature=0.1,
            max_tokens=4096,
        )
        return self._parse_response(resp, comments)

    def _parse_response(self, resp, comments: list[dict]) -> BatchAdJudgment:
        """解析 LLM 返回的 JSON，校验 rpid 完整性。"""
        try:
            data = self._client.parse_json_response(resp)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}\n原始内容: {resp.content[:500]}")
            # 降级：全部标记为正常
            return BatchAdJudgment(judgments=[
                CommentAdJudgment(rpid=str(c["rpid"]), is_ad=False, reason="解析失败")
                for c in comments
            ])

        result = BatchAdJudgment.from_dict(data)
        logger.info(
            f"检测完成: {sum(1 for j in result.judgments if j.is_ad)}/{len(result.judgments)} 条广告, "
            f"tokens: {resp.usage.total_tokens}"
        )

        # 校验 rpid 是否与输入一致
        input_rpids = {str(c["rpid"]) for c in comments}
        output_rpids = {j.rpid for j in result.judgments}
        missing = input_rpids - output_rpids
        extra = output_rpids - input_rpids

        if missing:
            logger.warning(f"LLM 遗漏 {len(missing)} 条评论: {missing}")
            for rpid in missing:
                result.judgments.append(CommentAdJudgment(rpid=rpid, is_ad=False, reason="LLM遗漏"))
        if extra:
            logger.warning(f"LLM 多出 {len(extra)} 条评论: {extra}")

        return result
