"""
LLMClient — 大模型接口抽象（Phase 1 预留）

当前实现由 server.py 直接使用 openai.AsyncOpenAI。
迁移时创建 DeepSeekClient(LLMClient) 实现此接口，server 层依赖注入。
"""
from abc import ABC, abstractmethod
from typing import AsyncIterator


class LLMClient(ABC):
    @abstractmethod
    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int = 80,
    ) -> AsyncIterator[str]:
        """流式生成，逐 delta 字符串 yield"""
