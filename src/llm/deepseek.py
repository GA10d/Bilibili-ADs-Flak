"""DeepSeek LLM 客户端。

基于 OpenAI 兼容 API，提供：
- 普通对话（chat）
- JSON 结构化输出（chat_json）- DeepSeek 原生支持 response_format json_object
"""

import json
import asyncio
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, OpenAI
from loguru import logger


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ChatMessage:
    """单条对话消息。"""
    role: str          # "system" | "user" | "assistant"
    content: str


@dataclass
class TokenUsage:
    """Token 用量统计。"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatResponse:
    """LLM 响应。"""
    content: str                                          # 回复文本
    model: str                                            # 实际使用的模型
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = ""                               # "stop" | "length" | "content_filter"
    raw: dict | None = None                               # 完整的 API 响应


# ============================================================
# DeepSeek 客户端
# ============================================================

class DeepSeekClient:
    """DeepSeek API 客户端（OpenAI 兼容）。

    使用方式：
        client = DeepSeekClient(api_key="sk-...")
        resp = await client.chat([ChatMessage("user", "你好")])
        resp = await client.chat_json([...], json_schema="...")
    """

    # DeepSeek 支持的模型
    MODELS = {
        "chat": "deepseek-chat",           # 通用对话
        "reasoner": "deepseek-reasoner",   # 推理增强（R1）
    }

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries

        # 异步客户端
        self._async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        # 同步客户端
        self._sync_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    # ==================== 异步接口 ====================

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> ChatResponse:
        """普通对话。

        Args:
            messages: 对话历史
            model: 模型名（默认用实例的 model）
            temperature: 温度 0~2，越低越确定性
            max_tokens: 最大输出 token 数
            system: 系统提示（可选，追加到 messages 最前面）
        """
        return await self._request(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, system=system, json_mode=False,
        )

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        system: str | None = None,
        json_schema: str | None = None,
    ) -> ChatResponse:
        """JSON 结构化输出。

        DeepSeek 原生支持 response_format={"type": "json_object"}。
        注意：prompt 中必须包含 "json" 字样，否则可能被拒绝。

        Args:
            messages: 对话历史
            json_schema: 期望的 JSON 格式描述（注入到 system prompt）
            其他参数同 chat()
        """
        if json_schema:
            schema_hint = (
                f"输出以下格式的 JSON：\n{json_schema}\n"
                "请只返回 JSON，不要加任何解释或 markdown 标记。"
            )
            if system:
                system = f"{system}\n\n{schema_hint}"
            else:
                system = schema_hint

        return await self._request(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, system=system, json_mode=True,
        )

    # ==================== 同步接口（线程安全） ====================

    def chat_sync(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """同步普通对话（适用于非 async 场景）。"""
        return self._request_sync(messages=messages, json_mode=False, **kwargs)

    def chat_json_sync(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """同步 JSON 输出。"""
        return self._request_sync(messages=messages, json_mode=True, **kwargs)

    # ==================== 内部 ====================

    async def _request(
        self,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float,
        max_tokens: int,
        system: str | None,
        json_mode: bool,
    ) -> ChatResponse:
        api_messages = self._build_messages(messages, system)
        kwargs = {
            "model": model or self._model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        logger.debug(f"DeepSeek 请求: model={kwargs['model']}, msgs={len(api_messages)}, json={json_mode}")
        resp = await self._async_client.chat.completions.create(**kwargs)
        return self._parse_response(resp)

    def _request_sync(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        system: str | None = None,
        json_mode: bool = False,
        json_schema: str | None = None,
    ) -> ChatResponse:
        if json_mode and json_schema:
            schema_hint = (
                f"输出以下格式的 JSON：\n{json_schema}\n"
                "请只返回 JSON，不要加任何解释或 markdown 标记。"
            )
            if system:
                system = f"{system}\n\n{schema_hint}"
            else:
                system = schema_hint

        api_messages = self._build_messages(messages, system)
        kwargs = {
            "model": model or self._model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = self._sync_client.chat.completions.create(**kwargs)
        return self._parse_response(resp)

    def _build_messages(self, messages: list[ChatMessage], system: str | None) -> list[dict]:
        result: list[dict] = []
        if system:
            result.append({"role": "system", "content": system})
        for m in messages:
            result.append({"role": m.role, "content": m.content})
        return result

    def _parse_response(self, resp) -> ChatResponse:
        choice = resp.choices[0]
        usage = TokenUsage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0),
            completion_tokens=getattr(resp.usage, "completion_tokens", 0),
            total_tokens=getattr(resp.usage, "total_tokens", 0),
        )
        return ChatResponse(
            content=choice.message.content or "",
            model=resp.model,
            usage=usage,
            finish_reason=choice.finish_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    # ==================== 工具方法 ====================

    def parse_json_response(self, response: ChatResponse) -> dict | list:
        """从 JSON 模式的响应中解析出 dict 或 list。

        DeepSeek 的 json_object 模式保证返回合法 JSON，但有时会包在 ```json``` 里。
        """
        text = response.content.strip()
        # 去除可能的 markdown 包裹
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        return json.loads(text)
