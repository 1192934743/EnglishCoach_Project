"""
LLM Client 实现 —— DeepSeek。

实现了 infrastructure/llm/base.py 中定义的 LLMClient 抽象接口。
"""
from __future__ import annotations

import time
import logging
from typing import AsyncIterator

import httpx
import openai

logger = logging.getLogger("EnglishCoach")


class DeepSeekLLMClient:
    """
    DeepSeek 大模型客户端封装。

    实现了流式对话接口，内部复用 httpx 连接池以减少 TLS 建连开销。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        http_client: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._client = http_client or openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self._warm_lock = __import__("asyncio").Lock()
        self._last_warm_ok: float = -1e12
        self._warm_cooldown: float = 45.0

    @property
    def raw_client(self) -> openai.AsyncOpenAI:
        """暴露原始 openai 客户端，供 server.py 兼容（teaching 等场景直接调用）"""
        return self._client

    async def warm_up(self, reason: str = "") -> None:
        """
        发一条极小的流式请求，建立 TLS + 连接池。
        带冷却与互斥，避免多 WS 同时重复预热。
        """
        async with self._warm_lock:
            now = time.monotonic()
            if now - self._last_warm_ok < self._warm_cooldown:
                logger.debug("[LLM] skip warm (cooldown) reason=%s", reason or "?")
                return
            t0 = time.perf_counter()
            try:
                stream = await self._client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": "."}],
                    max_tokens=1,
                    stream=True,
                )
                n = 0
                async for chunk in stream:
                    n += 1
                    if chunk.choices:
                        d = chunk.choices[0].delta
                        if d and getattr(d, "content", None):
                            break
                    if n > 48:
                        break
            except Exception as e:
                logger.warning("[LLM] DeepSeek 预热失败 reason=%s: %s", reason or "?", e)
                return
            self._last_warm_ok = time.monotonic()
            ms = (time.perf_counter() - t0) * 1000.0
            logger.info("[LLM] DeepSeek 连接预热完成 reason=%s wall=%.0fms", reason or "?", ms)

    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int = 80,
    ) -> AsyncIterator[str]:
        """
        流式生成对话，逐 delta 字符串 yield。
        """
        response = await self._client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 80,
    ) -> str:
        """
        非流式对话，返回完整文本。
        """
        response = await self._client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            max_tokens=max_tokens,
            stream=False,
        )
        return response.choices[0].message.content or ""

    async def close(self) -> None:
        await self._client.close()
