"""
LLM Router - 多模型容灾路由层

支持两种模式：
1. 开发者调试模式：直接调用指定模型（不走容灾）
2. 生产模式：自动容灾切换 + 熔断保护

熔断机制 (Circuit Breaker):
- 连续失败 >= 2 次，触发熔断，冷却 60 秒
- 冷却期内跳过该模型
- 冷却结束后自动尝试恢复，成功则清零失败计数
"""

import logging
import asyncio
import time
from typing import Optional, Any

from openai import APIError, RateLimitError, Timeout as OpenAITimeout

logger = logging.getLogger("EnglishCoach")

# 模型名映射：前端显示名 → 实际模型名
MODEL_ALIASES = {
    "doubao-pro": "doubao-seed-2-0-pro-260215",
}

# 容灾顺序配置（DeepSeek 优先，豆包备灾）
FAILOVER_ORDER = [
    "deepseek-chat",
    "doubao-seed-2-0-pro-260215",
]

TIMEOUT_SECONDS = 30.0
MAX_RETRIES_PER_PROVIDER = 2
CIRCUIT_BREAKER_THRESHOLD = 2
CIRCUIT_BREAKER_COOLDOWN = 60.0


class AllProvidersFailedError(Exception):
    """所有 provider 都失败"""
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"All providers failed: {errors}")


class LLMRouter:
    """
    LLM 路由层，支持两种模式：
    1. 开发者调试模式：直接调用指定模型（不走容灾）
    2. 生产模式：自动容灾切换 + 熔断保护
    """
    
    def __init__(self, http_client=None):
        self._http_client = http_client
        self._consecutive_failures: dict[str, int] = {m: 0 for m in FAILOVER_ORDER}
        self._cooldown_until: dict[str, float] = {m: 0.0 for m in FAILOVER_ORDER}
    
    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        stream: bool = True,
        **kwargs
    ) -> Any:
        """
        统一入口
        
        Args:
            model: 如果传入，则走开发者模式；否则走生产容灾模式
        """
        if model:
            logger.info(f"[LLMRouter] 开发者模式，直接调用: {model}")
            return await self._call_model(model, messages, stream, **kwargs)
        
        return await self._call_with_failover(messages, stream, **kwargs)
    
    def _is_in_circuit_break(self, model: str) -> bool:
        """检查模型是否处于熔断冷却期"""
        if time.time() < self._cooldown_until.get(model, 0):
            return True
        return False
    
    def _trip_circuit_breaker(self, model: str) -> None:
        """触发熔断"""
        self._cooldown_until[model] = time.time() + CIRCUIT_BREAKER_COOLDOWN
        logger.warning(f"[LLMRouter] 模型 [{model}] 触发熔断，冷却 {CIRCUIT_BREAKER_COOLDOWN} 秒")
    
    def _record_failure(self, model: str) -> None:
        """记录失败，增加连续失败计数"""
        self._consecutive_failures[model] = self._consecutive_failures.get(model, 0) + 1
        if self._consecutive_failures[model] >= CIRCUIT_BREAKER_THRESHOLD:
            self._trip_circuit_breaker(model)
    
    def _record_success(self, model: str) -> None:
        """记录成功，清零失败计数"""
        self._consecutive_failures[model] = 0
        logger.info(f"[LLMRouter] 模型 [{model}] 恢复成功")
    
    async def _call_with_failover(
        self,
        messages: list[dict],
        stream: bool = True,
        **kwargs
    ) -> Any:
        """按顺序尝试 providers，失败时自动切换"""
        last_error = None
        
        for model in FAILOVER_ORDER:
            if self._is_in_circuit_break(model):
                logger.debug(f"[LLMRouter] 模型 [{model}] 处于熔断冷却期，跳过")
                continue
            
            for attempt in range(MAX_RETRIES_PER_PROVIDER):
                try:
                    logger.info(f"[LLMRouter] 尝试 provider: {model} (attempt {attempt + 1})")
                    result = await self._call_model(model, messages, stream, **kwargs)
                    
                    self._record_success(model)
                    logger.info(f"[LLMRouter] 成功: {model}")
                    return result
                    
                except (OpenAITimeout, RateLimitError) as e:
                    last_error = f"{model}: {type(e).__name__}: {e}"
                    logger.warning(f"[LLMRouter] {model} 超时/限流，attempt {attempt + 1}，等待重试...")
                    self._record_failure(model)
                    if attempt < MAX_RETRIES_PER_PROVIDER - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue
                    
                except APIError as e:
                    if e.status_code and 500 <= e.status_code < 600:
                        last_error = f"{model}: {e}"
                        logger.warning(f"[LLMRouter] {model} 返回 {e.status_code}，切换下一个...")
                        self._record_failure(model)
                        break
                    raise
                    
                except Exception as e:
                    last_error = f"{model}: {e}"
                    logger.error(f"[LLMRouter] {model} 未知错误: {e}")
                    self._record_failure(model)
                    break
        
        raise AllProvidersFailedError([last_error] if last_error else [])
    
    async def _call_model(
        self,
        model: str,
        messages: list[dict],
        stream: bool = True,
        **kwargs
    ) -> Any:
        """调用单个模型，自动选择对应客户端"""
        from infrastructure.llm.client import doubao_client, deepseek_client
        
        # 解析实际模型名（处理别名）
        actual_model = MODEL_ALIASES.get(model, model)
        if actual_model != model:
            logger.info(f"[LLMRouter] 模型别名转换: {model} → {actual_model}")
        
        # 根据模型名选择客户端
        if "doubao" in actual_model.lower():
            client = doubao_client
        else:
            client = deepseek_client
        
        return await client.chat.completions.create(
            model=actual_model,
            messages=messages,
            stream=stream,
            **kwargs
        )


# 全局单例
llm_router = LLMRouter()
