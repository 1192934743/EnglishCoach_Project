import time
import asyncio
import logging
import httpx
import openai
from core.config import CONFIG

logger = logging.getLogger("EnglishCoach")

# DeepSeek client (保留备用)
_deepseek_http = httpx.AsyncClient(
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    timeout=httpx.Timeout(connect=25.0, read=180.0, write=60.0, pool=10.0),
)

deepseek_client = openai.AsyncOpenAI(
    api_key=CONFIG["DEEPSEEK_KEY"],
    base_url=CONFIG["DEEPSEEK_BASE"],
    http_client=_deepseek_http,
)

# Doubao (火山方舟) client
_doubao_http = httpx.AsyncClient(
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    timeout=httpx.Timeout(connect=25.0, read=180.0, write=60.0, pool=10.0),
)

doubao_client = openai.AsyncOpenAI(
    api_key=CONFIG["DOUBAO_ARK_KEY"],
    base_url=CONFIG["DOUBAO_BASE"],
    http_client=_doubao_http,
)

# 默认使用豆包
client = doubao_client
DEFAULT_LLM_MODEL = CONFIG["DOUBAO_MODEL"]

_llm_warm_lock = asyncio.Lock()
_last_llm_warm_ok_mono: float = -1e12
LLM_WARM_COOLDOWN_SEC = 45.0

async def warm_llm_connection(reason: str = "") -> None:
    """预热 LLM 连接（豆包）"""
    global _last_llm_warm_ok_mono
    async with _llm_warm_lock:
        now = time.monotonic()
        if now - _last_llm_warm_ok_mono < LLM_WARM_COOLDOWN_SEC:
            logger.debug("[LLM] skip warm (cooldown) reason=%s", reason or "?")
            return
        t0 = time.perf_counter()
        try:
            stream = await client.chat.completions.create(
                model=DEFAULT_LLM_MODEL,
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
            logger.warning("[LLM] 预热失败 reason=%s: %s", reason or "?", e)
            return
        _last_llm_warm_ok_mono = time.monotonic()
        ms = (time.perf_counter() - t0) * 1000.0
        logger.info("[LLM] 连接预热完成 reason=%s wall=%.0fms", reason or "?", ms)

# 保持向后兼容的别名
warm_deepseek_connection = warm_llm_connection