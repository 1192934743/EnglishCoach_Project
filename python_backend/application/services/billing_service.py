"""
BillingService — 计费服务抽象接口（Phase 3 预留）

当前使用 NoBillingService（永远放行）。
替换实现时只需在 server.py / main.py 的依赖注入处换掉即可，WebSocket 核心循环不变。
"""
from abc import ABC, abstractmethod


class BillingService(ABC):
    @abstractmethod
    async def on_turn_start(self, user_id: str, session_id: str) -> bool:
        """返回 False 时本轮被拒绝（如余额不足），server 应向前端发 quota_exceeded 事件"""

    @abstractmethod
    async def on_turn_end(self, user_id: str, duration_ms: int, tokens_used: int) -> None:
        """扣除费用 / 记录用量"""


class NoBillingService(BillingService):
    """MVP 阶段空实现，永远放行，不记录任何用量"""
    async def on_turn_start(self, user_id: str, session_id: str) -> bool:
        return True

    async def on_turn_end(self, user_id: str, duration_ms: int, tokens_used: int) -> None:
        pass
