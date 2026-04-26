import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

# --- 引入配置与预热 ---
from core.config import logger, CONFIG
from infrastructure.llm.client import warm_llm_connection
from infrastructure.tts import init_tts_factory, get_tts_factory

# --- 引入拆分好的路由 ---
from api.http.routes import router as http_router
from api.websocket.coach_ws import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    from database import ensure_schema_upgrades, Base, engine
    import application.services.session_planner as session_planner

    # 首次启动时确保所有表存在（不会删旧数据）
    Base.metadata.create_all(bind=engine)
    await run_in_threadpool(ensure_schema_upgrades)
    await run_in_threadpool(session_planner.warm_up)
    logger.info("🧠 SessionPlanner VectorStore 已就绪。")

    await warm_llm_connection("server_startup")
    logger.info("🔊 LLM connection warmed up.")

    # Initialize TTS factory and pool
    init_tts_factory(CONFIG)
    # Initialize the TTS pool from audio_service.py (for volcengine)
    from core.audio_service import init_tts_pool, warm_tts_pool, _TTS_POOL_SIZE as _TP_SIZE
    pool = init_tts_pool(CONFIG)
    # Warm up TTS pool in background (non-blocking)
    asyncio.create_task(warm_tts_pool())
    logger.info(f"🔥 TTS 连接池已初始化（大小={_TP_SIZE}），正在后台预热...")

    # Initialize ASR pool (弹匣模式)
    from core.audio_service import init_asr_pool, warm_asr_pool, _ASR_POOL_SIZE as _ASR_SIZE
    asr_pool = init_asr_pool(CONFIG)
    asyncio.create_task(warm_asr_pool())
    logger.info(f"🎙️ ASR 连接池已初始化（大小={_ASR_SIZE}），正在后台预热...")

    # Warm up all TTS providers (for azure)
    await get_tts_factory().warm_up_all()
    logger.info("🔊 TTS Factory initialized and all providers warmed up.")

    yield  # --- 这里是分隔线，shutdown 代码在 yield 之后 ---

    # --- Shutdown ---
    try:
        await client.close()
    except Exception as e:
        logger.warning("[LLM] AsyncOpenAI close: %s", e)

    # Close ASR pool
    try:
        from core.audio_service import get_asr_pool
        asr_pool = get_asr_pool()
        if asr_pool:
            await asr_pool.close()
    except Exception as e:
        logger.warning("[ASR] Pool close error: %s", e)

    # Close all TTS providers
    try:
        from core.audio_service import get_tts_pool
        pool = get_tts_pool()
        if pool:
            await pool.close()
    except Exception as e:
        logger.warning("[TTS] Pool close error: %s", e)

    try:
        factory = get_tts_factory()
        if factory:
            await factory.close_all()
    except Exception as e:
        logger.warning("[TTS] Factory close error: %s", e)


# 实例化 FastAPI (唯一的实例化地点)
app = FastAPI(title="EnglishCoach API", lifespan=lifespan)

# 挂载路由
app.include_router(http_router)
app.include_router(ws_router)
@app.get("/health")
async def health_check():
    # 后期你可以在这里加入数据库连接检查
    return {"status": "healthy", "service": "EnglishCoach Backend"}


if __name__ == "__main__":
    import uvicorn
    # 本地开发启动入口
    uvicorn.run(app, host="0.0.0.0", port=8000)