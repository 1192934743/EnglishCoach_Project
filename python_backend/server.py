import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

# --- 引入配置与预热 ---
from core.config import logger
from infrastructure.llm.client import client, warm_deepseek_connection

# --- 引入拆分好的路由 ---
from api.http.routes import router as http_router
from api.websocket.coach_ws import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    from database import ensure_schema_upgrades
    import application.services.session_planner as session_planner

    await run_in_threadpool(ensure_schema_upgrades)
    await run_in_threadpool(session_planner.warm_up)
    logger.info("🧠 SessionPlanner VectorStore 已就绪。")

    await warm_deepseek_connection("server_startup")
    logger.info("🔊 Azure Neural TTS 已就绪（无需预热）。")

    yield  # --- 这里是分隔线，shutdown 代码在 yield 之后 ---

    # --- Shutdown ---
    try:
        await client.close()
    except Exception as e:
        logger.warning("[LLM] AsyncOpenAI close: %s", e)


# 实例化 FastAPI (唯一的实例化地点)
app = FastAPI(title="EnglishCoach API", lifespan=lifespan)

# 挂载路由
app.include_router(http_router)
app.include_router(ws_router)

if __name__ == "__main__":
    import uvicorn
    # 本地开发启动入口
    uvicorn.run(app, host="0.0.0.0", port=8000)