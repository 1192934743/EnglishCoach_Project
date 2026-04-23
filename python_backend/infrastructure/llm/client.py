import time
import asyncio
import logging
import httpx
import openai
from core.config import CONFIG

logger = logging.getLogger("EnglishCoach")

_deepseek_http = httpx.AsyncClient(
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    timeout=httpx.Timeout(connect=25.0, read=180.0, write=60.0, pool=10.0),
)

client = openai.AsyncOpenAI(
    api_key=CONFIG["DEEPSEEK_KEY"],
    base_url=CONFIG["DEEPSEEK_BASE"],
    http_client=_deepseek_http,
)

_llm_warm_lock = asyncio.Lock()
_last_llm_warm_ok_mono: float = -1e12
LLM_WARM_COOLDOWN_SEC = 45.0

async def warm_deepseek_connection(reason: str = "") -> None:
    global _last_llm_warm_ok_mono
    async with _llm_warm_lock:
        now = time.monotonic()
        if now - _last_llm_warm_ok_mono < LLM_WARM_COOLDOWN_SEC:
            logger.debug("[LLM] skip DeepSeek warm (cooldown) reason=%s", reason or "?")
            return
        t0 = time.perf_counter()
        try:
            stream = await client.chat.completions.create(
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
        _last_llm_warm_ok_mono = time.monotonic()
        ms = (time.perf_counter() - t0) * 1000.0
        logger.info("[LLM] DeepSeek 连接预热完成 reason=%s wall=%.0fms", reason or "?", ms)