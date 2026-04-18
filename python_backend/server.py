import os
import json
import asyncio
import logging
import time
from typing import Optional
from contextlib import contextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from dotenv import load_dotenv
import httpx
import openai

from database import (
    SessionLocal,
    User,
    Topic,
    TargetNode,
    UserProgress,
    LearningSession,
    ensure_schema_upgrades,
    effective_topic_title_zh,
)
from core.audio_service import (
    MIN_AUDIO_BYTES,
    ASR_STREAM_START_BYTES,
    run_volcengine_wss_asr,
    run_volc_streaming_asr_worker,
    run_tts_to_ws,
    run_tts_turn_reused_from_queue,
)
from core.dialogue_engine import build_dynamic_prompt, advance_state_machine, evaluate_and_check_progress, async_fetch_and_send_teaching
from domain.entities.session_context import SessionContext
import uuid
import application.services.session_planner as session_planner
import application.services.assessment_engine as assessment_engine
from application.services.report_builder import build_preliminary_report
import infrastructure.topic_generator as topic_generator

# ================= 0. 初始化与配置 =================
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("EnglishCoach")
# 同时写入 python_backend/server_output.log，便于查找含 [Prompt] / [L1] 的调试日志
try:
    _log_path = os.path.join(_BACKEND_DIR, "server_output.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    _coach = logging.getLogger("EnglishCoach")
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(_log_path) for h in _coach.handlers):
        _coach.addHandler(_fh)
    logger.info("EnglishCoach file log: %s", os.path.abspath(_log_path))
except OSError as _e:
    logging.getLogger("EnglishCoach").warning("Could not open server_output.log: %s", _e)

load_dotenv("config.env")

CONFIG = {
    "DEEPSEEK_KEY": os.getenv("DEEPSEEK_KEY"),
    "DEEPSEEK_BASE": os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"),
    "VOLC_API_KEY": os.getenv("VOLC_API_KEY"),
    "VOLC_RESOURCE_ID_ASR": os.getenv("VOLC_RESOURCE_ID"),
    "VOLC_RESOURCE_ID_TTS": os.getenv("VOLC_RESOURCE_ID_TTS"),
    "VOICE": os.getenv("VOICE", "BV001_streaming"),
    # 与 core.audio_service.run_volcengine_wss_asr 约定一致；可在 config.env 覆盖
    "ASR_LANGUAGE": os.getenv("ASR_LANGUAGE", "en-US"),
}

# 启动时硬性校验核心配置
if not CONFIG["DEEPSEEK_KEY"] or not CONFIG["VOLC_API_KEY"]:
    logger.error("🚨 致命错误: 缺少必要的环境变量 (DEEPSEEK_KEY 或 VOLC_API_KEY)。请检查 config.env 文件。")
    raise RuntimeError("Missing essential API keys in config.")

if not CONFIG.get("VOLC_RESOURCE_ID_ASR"):
    logger.warning("⚠️ 未设置 VOLC_RESOURCE_ID（ASR）：语音转写将在调用时失败，请检查 config.env。")
if not CONFIG.get("VOLC_RESOURCE_ID_TTS"):
    logger.warning("⚠️ 未设置 VOLC_RESOURCE_ID_TTS：合成语音将在调用时失败，请检查 config.env。")

TEACHING_CONFIG = {"enable_correction": False, "enable_translation": True, "enable_hints": True}

# 共享 httpx 连接池：避免默认「懒连接」导致用户首轮 chat.completions 承担完整 TLS + 建连。
_deepseek_http = httpx.AsyncClient(
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    timeout=httpx.Timeout(connect=25.0, read=180.0, write=60.0, pool=10.0),
)
client = openai.AsyncOpenAI(
    api_key=CONFIG["DEEPSEEK_KEY"],
    base_url=CONFIG["DEEPSEEK_BASE"],
    http_client=_deepseek_http,
)

# DeepSeek 首轮偏慢：此前仅有 VectorStore warm_up，无 LLM 侧预热；首条 HTTPS 冷启动 ~0.5–1s+。
_llm_warm_lock = asyncio.Lock()
_last_llm_warm_ok_mono: float = -1e12
LLM_WARM_COOLDOWN_SEC = 45.0


async def warm_deepseek_connection(reason: str = "") -> None:
    """
    发一条极小的流式请求，建立 TLS + 连接池。带冷却与互斥，避免多 WS 同时重复预热。
    在 server_startup 与 websocket accept 各打一次（间隔够长时），减轻 [LATENCY] 04→04b 首轮尖刺。
    """
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


app = FastAPI()


@app.on_event("startup")
async def on_startup():
    """服务启动时：SQLite 结构升级 → VectorStore 预热 → DeepSeek HTTP 预热"""
    await run_in_threadpool(ensure_schema_upgrades)
    await run_in_threadpool(session_planner.warm_up)
    logger.info("🧠 SessionPlanner VectorStore 已就绪。")
    await warm_deepseek_connection("server_startup")


@app.on_event("shutdown")
async def on_shutdown():
    try:
        await client.close()
    except Exception as e:
        logger.warning("[LLM] AsyncOpenAI close: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# REST API — 话题浏览器 + 学习统计
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/topics")
async def get_topics(user_id: Optional[str] = Query(default=None)):
    """
    返回所有话题列表，附带用户对该话题的掌握度概况。
    前端话题浏览器使用。
    """
    def _query():
        with get_db() as db:
            topics = db.query(Topic).all()
            result = []
            for t in topics:
                nodes = db.query(TargetNode).filter(TargetNode.topic_id == t.id).all()
                total_nodes = len(nodes)
                avg_mastery = 0.0
                last_practiced = None
                if user_id and nodes:
                    node_ids = [n.id for n in nodes]
                    progresses = db.query(UserProgress).filter(
                        UserProgress.user_id == user_id,
                        UserProgress.node_id.in_(node_ids),
                    ).all()
                    if progresses:
                        avg_mastery = sum(p.mastery_score for p in progresses) / len(nodes)
                        dates = [p.last_practiced_at for p in progresses if p.last_practiced_at]
                        last_practiced = max(dates).isoformat() if dates else None
                # Count distinct depth levels
                depth_levels = sorted(set(n.depth_level for n in nodes))
                result.append({
                    "id": t.id,
                    "title": t.title,
                    "title_zh": effective_topic_title_zh(t),
                    "category": t.category or "General",
                    "learner_level": t.learner_level or "Intermediate",
                    "role_name": t.role_name or "Coach",
                    "total_nodes": total_nodes,
                    "depth_levels": depth_levels,
                    "avg_mastery": round(avg_mastery, 1),
                    "last_practiced": last_practiced,
                })
            return result

    data = await run_in_threadpool(_query)
    return JSONResponse(content={"topics": data})


@app.get("/api/stats")
async def get_stats(user_id: str = Query(...)):
    """
    返回用户学习统计数据。
    学习统计页使用。
    """
    def _query():
        import datetime
        with get_db() as db:
            # Total sessions
            sessions = db.query(LearningSession).filter(
                LearningSession.user_id == user_id
            ).order_by(LearningSession.start_time.desc()).all()

            # Total nodes practiced (unique)
            all_progress = db.query(UserProgress).filter(
                UserProgress.user_id == user_id
            ).all()

            mastered_nodes = [p for p in all_progress if p.mastery_score >= 60.0]
            total_practiced = len(all_progress)

            # Streak calculation (consecutive days with at least 1 session)
            streak = 0
            if sessions:
                today = datetime.datetime.utcnow().date()
                day = today
                session_dates = set(s.start_time.date() for s in sessions if s.start_time)
                while day in session_dates:
                    streak += 1
                    day -= datetime.timedelta(days=1)

            # Topics practiced
            topic_ids_practiced = list(set(s.topic_id for s in sessions if s.topic_id))

            # Recent sessions (last 7)
            recent = []
            for s in sessions[:7]:
                topic = db.query(Topic).filter(Topic.id == s.topic_id).first()
                recent.append({
                    "topic_title": topic.title if topic else "Unknown",
                    "topic_title_zh": (effective_topic_title_zh(topic) if topic else None),
                    "depth_tier": s.depth_tier_used or 1,
                    "nodes_mastered": len(s.nodes_mastered or []),
                    "date": s.start_time.isoformat() if s.start_time else None,
                    "quality": s.session_summary.get("avg_quality") if s.session_summary else None,
                })

            # Per-topic mastery summary
            topics_summary = []
            all_topics = db.query(Topic).all()
            for t in all_topics:
                nodes = db.query(TargetNode).filter(TargetNode.topic_id == t.id).all()
                if not nodes:
                    continue
                node_ids = [n.id for n in nodes]
                progs = db.query(UserProgress).filter(
                    UserProgress.user_id == user_id,
                    UserProgress.node_id.in_(node_ids),
                ).all()
                if not progs:
                    continue
                avg = sum(p.mastery_score for p in progs) / len(nodes)
                topics_summary.append({
                    "topic_title": t.title,
                    "topic_title_zh": effective_topic_title_zh(t),
                    "category": t.category or "General",
                    "avg_mastery": round(avg, 1),
                    "nodes_practiced": len(progs),
                    "total_nodes": len(nodes),
                })
            topics_summary.sort(key=lambda x: x["avg_mastery"], reverse=True)

            return {
                "total_sessions": len(sessions),
                "total_expressions_practiced": total_practiced,
                "total_expressions_mastered": len(mastered_nodes),
                "topics_touched": len(topic_ids_practiced),
                "current_streak_days": streak,
                "recent_sessions": recent,
                "topics_summary": topics_summary[:10],  # top 10
            }

    data = await run_in_threadpool(_query)
    return JSONResponse(content=data)

# ================= 业务全局常量 =================
LLM_MAX_TOKENS = 80                # 限制每次模型输出的长度，保证响应速度
DEFAULT_TOPIC_ID = 999             # 兜底的话题ID
MAX_AUDIO_BYTES = 5 * 1024 * 1024  # 音频防爆限制：5MB
MAX_BUFFER_CHARS = 38              # 无标点时略缩短，更快送入 TTS
# 首段语音：不等长句标点也可提前合成（避免首句卡在句号前）
FIRST_TTS_EARLY_FLUSH_CHARS = 22
# chat_history token 预算：4 chars ≈ 1 token（粗估），给模型上下文留足余量
MAX_CONTEXT_TOKENS = 2000          # 进入 LLM 前历史消息的 token 上限（含 system prompt）


def _latency_log(lat: dict, stage: str, **kwargs) -> None:
    """同一轮 user_finish_speaking / test_text_input 内各阶段相对 t0 的累计毫秒（服务端）。"""
    t0 = lat.get("t0")
    if not isinstance(t0, (int, float)):
        return
    tid = lat.get("turn_id", "?")
    ms = (time.perf_counter() - float(t0)) * 1000.0
    extra = (" " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())) if kwargs else ""
    logger.info("[LATENCY] turn=%s stage=%-28s cum=%8.1fms%s", tid, stage, ms, extra)


# ================= 数据库上下文 =================
@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ================= 辅助/处理器函数 =================
def init_or_get_user(user_id: str):
    """线程安全的 DB 操作，获取或创建用户并将其解绑（Detached）以供全局使用"""
    if not user_id: 
        return None
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            db.add(user)
            db.commit()
            db.refresh(user)
        db.expunge(user)  # 解绑：防止后续在 async 主程中使用其属性时触发 detached 异常
        return user


def _fetch_user_settings_dict(user_id: str) -> dict:
    """供 WS 握手返回：当前用户在 DB 中的 settings JSON（LMS / learner_level 等）。"""
    if not user_id:
        return {}
    with get_db() as db:
        from database import User as UserModel
        u = db.query(UserModel).filter(UserModel.id == user_id).first()
        return dict(u.settings or {}) if u else {}


def _update_lms_settings(user_id: str, depth_preference=None, new_topic_appetite=None, learner_level=None):
    """Update LMS parameters in User.settings (thread-safe sync)."""
    if not user_id:
        return
    with get_db() as db:
        from database import User as UserModel
        user = db.query(UserModel).filter(UserModel.id == user_id).first()
        if user:
            settings = dict(user.settings or {})
            if depth_preference is not None:
                settings["depth_preference"] = float(depth_preference)
            if new_topic_appetite is not None:
                settings["new_topic_appetite"] = float(new_topic_appetite)
            if learner_level is not None:
                settings["learner_level"] = str(learner_level).strip()
            user.settings = settings
            db.commit()
            logger.info(
                f"[LMS] Settings updated: depth={settings.get('depth_preference')}, "
                f"appetite={settings.get('new_topic_appetite')}, "
                f"learner_level={settings.get('learner_level')}"
            )


def update_user_politeness(user_id: str, level: int):
    """线程安全的同步更新用户礼貌度"""
    if not user_id: 
        return
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.politeness_level = level
            db.commit()

def _take_mastery_snapshot(user_id: str, task_packet) -> dict:
    """
    会话开始时快照所有目标节点的掌握度，用于 WRAP_UP 时计算增量。
    同步函数，通过 run_in_threadpool 调用。
    """
    if not task_packet or not user_id:
        return {}
    all_nodes = task_packet.target_nodes + task_packet.review_nodes
    node_ids = [n["id"] for n in all_nodes if n.get("id")]
    if not node_ids:
        return {}
    with get_db() as db:
        progresses = db.query(UserProgress).filter(
            UserProgress.user_id == user_id,
            UserProgress.node_id.in_(node_ids),
        ).all()
        return {p.node_id: p.mastery_score for p in progresses}


async def _renew_task_packet(
    user_id: str,
    old_packet,
    nodes_hit: set,
    transcript: list,
    openai_client,
    session_id: str = "",
    websocket=None,
    ws_lock=None,
):
    """
    WRAP_UP 完成后异步执行的后处理任务（不阻塞新一轮对话）：
    1. 保存 LearningSession 记录到 DB
    2. 触发 L2 评估：校正 L1 掌握度 + 推送 final 报告到前端
    """
    if not old_packet or not user_id:
        return
    try:
        await run_in_threadpool(
            session_planner.save_learning_session, user_id, old_packet, nodes_hit
        )
    except Exception as e:
        logger.error(f"[WRAP_UP] save_learning_session error: {e}")

    try:
        await assessment_engine.run_l2_assessment(
            transcript, old_packet, user_id, session_id,
            openai_client, websocket, ws_lock,
        )
    except Exception as e:
        logger.error(f"[WRAP_UP] L2 assessment error (non-blocking): {e}")


def trim_chat_history(history: list[dict], max_tokens: int = MAX_CONTEXT_TOKENS) -> list[dict]:
    """
    按估算 token 数裁剪对话历史，永远保留 history[0]（system prompt）。
    策略：从最旧的 Q/A pair（[1][2]）开始丢弃，直到剩余 token 数在预算内。
    4 chars ≈ 1 token 是业界通行的粗估，对英文文本误差在 10% 内。
    """
    if len(history) <= 1:
        return history
    system = history[0]
    turns = history[1:]
    # 丢弃时以完整的 Q/A pair（2条）为最小单位，避免上下文不对称
    while len(turns) > 2:
        total_chars = sum(len(m.get("content", "")) for m in [system] + turns)
        if total_chars // 4 <= max_tokens:
            break
        turns = turns[2:]  # 丢弃最早一对 user/assistant
    return [system] + turns

async def safe_send_ws(websocket: WebSocket, ws_lock: asyncio.Lock, payload: dict):
    """统一的线程安全 WebSocket 发送工具"""
    try:
        async with ws_lock:
            await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.error(f"Ws 发送失败: {e}")

# ================= 1. WebSocket 主入口 =================
@app.websocket("/ws/coach")
async def websocket_endpoint(websocket: WebSocket, user_id: Optional[str] = None):
    await websocket.accept()
    logger.info(f"📱 客户端已连接: {websocket.client.host}")
    # 长间隔后重连时连接池可能已过期；不阻塞握手，后台预热下一轮对话的首包 LLM。
    asyncio.create_task(warm_deepseek_connection(f"ws_accept:{websocket.client.host}"))

    ws_lock = asyncio.Lock()
    audio_buffer = bytearray()
    asr_stream_queue: Optional[asyncio.Queue] = None
    asr_stream_task: Optional[asyncio.Task] = None
    asr_partial_last_mono = 0.0

    async def _emit_asr_partial(txt: str):
        """流式 ASR 中间结果：仅展示用，节流避免刷屏。"""
        nonlocal asr_partial_last_mono
        if not txt:
            return
        now = time.monotonic()
        if now - asr_partial_last_mono < 0.22:
            return
        asr_partial_last_mono = now
        await safe_send_ws(websocket, ws_lock, {"event": "asr_partial", "text": txt})

    async def _abort_streaming_asr():
        nonlocal asr_stream_queue, asr_stream_task
        if asr_stream_task and not asr_stream_task.done():
            asr_stream_task.cancel()
            try:
                await asr_stream_task
            except (asyncio.CancelledError, Exception):
                pass
        asr_stream_task = None
        asr_stream_queue = None

    consumer_task = None
    tts_queue = None

    is_flipped = False
    session_hits = set()
    session_ctx = SessionContext()       # 可序列化的会话状态
    current_task_packet = None           # 当前 session 的 TaskPacket
    session_transcript: list[dict] = [] # L2 评估用对话记录
    session_id: str = str(uuid.uuid4()) # 每局唯一 ID，用于前端匹配 preliminary/final 报告
    mastery_snapshot: dict = {}          # 本局开始时的节点掌握度快照

    # 获取或创建用户
    current_user = await run_in_threadpool(init_or_get_user, user_id)

    # 生成首个 TaskPacket（有用户 ID 时走 LMS，匿名时降级到兜底）
    if current_user:
        current_task_packet = await run_in_threadpool(
            session_planner.build_task_packet, current_user.id
        )
        mastery_snapshot = await run_in_threadpool(
            _take_mastery_snapshot, current_user.id, current_task_packet
        )

    # 确定话题 ID（用于进度追踪）
    topic_id_for_progress = (
        current_task_packet.topic_id
        if current_task_packet and current_task_packet.topic_id
        else DEFAULT_TOPIC_ID
    )

    initial_prompt = build_dynamic_prompt(
        current_user, is_flipped, session_ctx, current_task_packet, session_hits
    )
    chat_history = [{"role": "system", "content": initial_prompt}]

    if current_user:
        with get_db() as db:
            await evaluate_and_check_progress(
                db, current_user.id, topic_id_for_progress, "",
                session_hits, session_ctx, websocket, ws_lock,
                task_packet=current_task_packet,
            )
            chat_history[0]["content"] = build_dynamic_prompt(
                current_user, is_flipped, session_ctx, current_task_packet, session_hits
            )

    try:
        while True:
            message = await websocket.receive()
            
            if message.get("type") == "websocket.disconnect":
                logger.info(f"👋 用户 {user_id or '未知'} 正常断开。")
                break
                
            # --- 音频流处理 ---
            if "bytes" in message:
                audio_buffer.extend(message["bytes"])
                if len(audio_buffer) > MAX_AUDIO_BYTES:
                    logger.warning("⚠️ 警告：音频缓冲区超限，强制清空以防止内存溢出。")
                    await _abort_streaming_asr()
                    audio_buffer.clear()
                    await safe_send_ws(websocket, ws_lock, {"event": "error", "message": "Audio buffer overflow"})
                    continue
                # 流式 ASR：缓冲达到阈值后建连并开始上传（与整段模式共用 MIN_AUDIO_BYTES）
                if asr_stream_task is not None and asr_stream_task.done():
                    try:
                        exc = asr_stream_task.exception()
                        if exc:
                            logger.warning("流式 ASR 任务异常结束: %s", exc)
                    except asyncio.CancelledError:
                        pass
                    except asyncio.InvalidStateError:
                        pass
                    asr_stream_task = None
                    asr_stream_queue = None
                if asr_stream_task is None and len(audio_buffer) >= ASR_STREAM_START_BYTES:
                    asr_stream_queue = asyncio.Queue(maxsize=0)
                    asr_stream_task = asyncio.create_task(
                        run_volc_streaming_asr_worker(
                            asr_stream_queue, CONFIG, on_partial=_emit_asr_partial
                        )
                    )
                    await asr_stream_queue.put(bytes(audio_buffer))
                elif asr_stream_queue is not None and asr_stream_task is not None and not asr_stream_task.done():
                    await asr_stream_queue.put(message["bytes"])
                continue
                
            # --- 文本指令处理 ---
            if "text" in message:
                # 健壮性：防范畸形 JSON 包导致长连接断开
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    logger.warning("⚠️ 收到非法的 JSON 数据，已忽略。")
                    await safe_send_ws(websocket, ws_lock, {"event": "error", "message": "Invalid JSON format"})
                    continue

                action = data.get("action")
                payload_user_id = data.get("user_id")

                if payload_user_id and not current_user:
                    current_user = await run_in_threadpool(init_or_get_user, payload_user_id)

                if action == "ping":
                    ws_payload: dict = {"event": "warmup_success"}
                    uid = payload_user_id or (current_user.id if current_user else None)
                    if uid:
                        if not current_user:
                            current_user = await run_in_threadpool(init_or_get_user, uid)
                        st = await run_in_threadpool(_fetch_user_settings_dict, uid)
                        if st:
                            ws_payload["user_settings"] = st
                            logger.info(
                                "[WS] warmup_success + user_settings for %s keys=%s",
                                uid[:8] if len(uid) > 8 else uid,
                                list(st.keys()),
                            )
                    await safe_send_ws(websocket, ws_lock, ws_payload)
                    continue

                if action == "swap_role":
                    is_flipped = not is_flipped
                    session_ctx["phase"], session_ctx["phase_turns"] = "ICE_BREAKING", 0
                    chat_history[0]["content"] = build_dynamic_prompt(
                        current_user, is_flipped, session_ctx, current_task_packet, session_hits
                    )
                    await safe_send_ws(websocket, ws_lock, {"event": "role_swapped", "is_flipped": is_flipped})
                    continue

                if action == "update_lms_settings":
                    if current_user:
                        depth = data.get("depth_preference")
                        appetite = data.get("new_topic_appetite")
                        learner_level = data.get("learner_level")
                        await run_in_threadpool(
                            _update_lms_settings, current_user.id, depth, appetite, learner_level
                        )
                        current_user = await run_in_threadpool(init_or_get_user, current_user.id)
                        chat_history[0]["content"] = build_dynamic_prompt(
                            current_user, is_flipped, session_ctx, current_task_packet, session_hits
                        )
                    continue

                if action == "update_politeness":
                    if current_user:
                        level = data.get("level", 1)
                        await run_in_threadpool(update_user_politeness, current_user.id, level)
                        current_user.politeness_level = level
                        chat_history[0]["content"] = build_dynamic_prompt(
                            current_user, is_flipped, session_ctx, current_task_packet, session_hits
                        )
                    continue

                if action == "request_topic":
                    # 用户主动请求话题（任意自然语言描述）
                    # 三层策略：复用 → 参考引导生成 → 纯净生成
                    description = data.get("description", "").strip()
                    if description and current_user:
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "topic_generating",
                            "message": f"Finding the best match for: {description}",
                            "message_zh": f"正在为你匹配练习场景：{description}",
                        })
                        try:
                            new_topic = await topic_generator.get_or_generate_topic(
                                description, client
                            )
                            # 追加到 VectorStore（若是新生成的话题）
                            await run_in_threadpool(session_planner.add_topic_to_store, new_topic)
                            # 为该话题生成 TaskPacket
                            current_task_packet = await run_in_threadpool(
                                session_planner.build_task_packet_for_topic,
                                current_user.id, new_topic.id
                            )
                            topic_id_for_progress = current_task_packet.topic_id
                            # 重置当前局状态
                            session_id = str(uuid.uuid4())
                            session_hits.clear()
                            session_transcript.clear()
                            mastery_snapshot = await run_in_threadpool(
                                _take_mastery_snapshot, current_user.id, current_task_packet
                            )
                            # 重建 system prompt 并通知前端
                            session_ctx = SessionContext()
                            chat_history = [{
                                "role": "system",
                                "content": build_dynamic_prompt(
                                    current_user, is_flipped, session_ctx, current_task_packet, session_hits
                                ),
                            }]
                            await safe_send_ws(websocket, ws_lock, {
                                "event": "topic_changed",
                                "topic_id": current_task_packet.topic_id,
                                "topic_title": current_task_packet.topic_title,
                                "topic_title_zh": getattr(
                                    current_task_packet, "topic_title_zh", None
                                ),
                                "role_name": current_task_packet.role_name,
                                "depth_tier": current_task_packet.depth_tier,
                                "session_id": session_id,
                            })
                            logger.info(f"[TopicRequest] Switched to '{current_task_packet.topic_title}' tier={current_task_packet.depth_tier}")
                        except Exception as e:
                            logger.error(f"[TopicRequest] Failed: {e}", exc_info=True)
                            await safe_send_ws(websocket, ws_lock, {
                                "event": "error",
                                "code": "TOPIC_GENERATION_FAILED",
                                "message": "Could not generate topic. Please try again.",
                                "message_zh": "暂时无法生成话题，请稍后再试。",
                            })
                    continue
                
                # 🚀 恢复：前端点读单句的请求支持
                if action == "request_tts":
                    text_to_speak = data.get("text", "")
                    if text_to_speak:
                        try:
                            await run_tts_to_ws(text_to_speak, websocket, ws_lock, CONFIG)
                        except Exception as e:
                            logger.error(f"主动点读 TTS 异常: {e}")
                        finally:
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                    continue

                # 🛑 打断机制：用户开口时取消正在进行的 TTS 合成与播放
                if action == "cancel_tts":
                    # 1. 取消 TTS 消费任务（当前正在向火山引擎请求合成的协程）
                    if consumer_task and not consumer_task.done():
                        consumer_task.cancel()
                        try:
                            await asyncio.wait_for(consumer_task, timeout=0.5)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            pass
                    consumer_task = None
                    # 2. 清空 TTS 队列，防止已入队但未合成的句子继续消费
                    if tts_queue:
                        while not tts_queue.empty():
                            try:
                                tts_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                    tts_queue = None
                    # 3. 清空音频接收缓冲区，防止旧 PCM 混入下一轮 ASR
                    await _abort_streaming_asr()
                    audio_buffer.clear()
                    logger.info("🛑 [打断] 用户打断 TTS，已取消合成任务并清空缓冲区。")
                    continue

                if action in ["user_finish_speaking", "test_text_input"]:
                    is_test_mode = (action == "test_text_input")
                    asr_partial_last_mono = 0.0
                    lat: dict = {
                        "turn_id": uuid.uuid4().hex[:8],
                        "t0": time.perf_counter(),
                    }
                    _latency_log(
                        lat,
                        "01_turn_accepted",
                        action=action,
                        test_mode=is_test_mode,
                    )

                    # 1. 语音转文本
                    if is_test_mode:
                        user_text, user_emotion = data.get("text", ""), "neutral"
                        _latency_log(lat, "02_skip_asr_test_text")
                    else:
                        used_streaming_asr = False
                        if (
                            asr_stream_task is not None
                            and not asr_stream_task.done()
                            and asr_stream_queue is not None
                        ):
                            used_streaming_asr = True
                            _latency_log(lat, "02_asr_stream_finish")
                            await asr_stream_queue.put(None)
                            try:
                                user_text, user_emotion = await asr_stream_task
                            except asyncio.CancelledError:
                                user_text, user_emotion = "", "neutral"
                            except Exception as e:
                                logger.warning("流式 ASR 收束失败，回退整包 ASR: %s", e)
                                asr_stream_task = None
                                asr_stream_queue = None
                                user_text, user_emotion = await run_volcengine_wss_asr(
                                    audio_buffer, CONFIG
                                )
                            asr_stream_task = None
                            asr_stream_queue = None
                            audio_buffer.clear()
                        else:
                            user_text, user_emotion = await run_volcengine_wss_asr(
                                audio_buffer, CONFIG
                            )
                            audio_buffer.clear()
                            if asr_stream_task is not None or asr_stream_queue is not None:
                                await _abort_streaming_asr()
                        _latency_log(
                            lat,
                            "02_asr_done",
                            user_chars=len(user_text or ""),
                            streaming_asr=used_streaming_asr,
                        )

                    if not user_text:
                        _latency_log(lat, "02b_empty_after_asr_skip_rest")
                        if not is_test_mode:
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                        continue

                    session_ctx["phase_turns"] += 1
                    session_transcript.append({"role": "user", "text": user_text})
                    logger.info(f"[Turn {session_ctx['phase_turns']}] ({session_ctx['phase']}) User: {user_text}")

                    # 2. 状态机评估 (开启新 DB 会话，避免跨会话 Detached 错误)
                    if current_user:
                        with get_db() as db:
                            await evaluate_and_check_progress(
                                db, current_user.id, topic_id_for_progress, user_text,
                                session_hits, session_ctx, websocket, ws_lock,
                                task_packet=current_task_packet,
                            )

                            fresh_topic = db.query(Topic).filter(Topic.id == topic_id_for_progress).first()
                            did_transition = advance_state_machine(
                                session_ctx, db, fresh_topic, session_hits, current_task_packet
                            )
                            if did_transition:
                                if session_ctx["phase"] == "ICE_BREAKING":
                                    # ── 1. 立刻推送初步报告（L1 数据，< 50ms）──────────
                                    preliminary = build_preliminary_report(
                                        task_packet=current_task_packet,
                                        session_id=session_id,
                                        session_hits=session_hits,
                                        session_ctx=session_ctx,
                                        mastery_snapshot=mastery_snapshot,
                                        db=db,
                                        user_id=current_user.id,
                                    )
                                    await safe_send_ws(websocket, ws_lock, preliminary)

                                    # ── 2. 后台：保存记录 + 触发 L2（推送 final 报告）──
                                    asyncio.create_task(_renew_task_packet(
                                        current_user.id,
                                        current_task_packet,
                                        session_hits.copy(),
                                        session_transcript.copy(),
                                        client,
                                        session_id,
                                        websocket,
                                        ws_lock,
                                    ))

                                    # ── 3. 生成新 TaskPacket，重置会话状态 ───────────
                                    current_task_packet = await run_in_threadpool(
                                        session_planner.build_task_packet, current_user.id
                                    )
                                    topic_id_for_progress = (
                                        current_task_packet.topic_id
                                        if current_task_packet.topic_id else DEFAULT_TOPIC_ID
                                    )
                                    session_id = str(uuid.uuid4())
                                    session_hits.clear()
                                    session_transcript.clear()
                                    mastery_snapshot = await run_in_threadpool(
                                        _take_mastery_snapshot, current_user.id, current_task_packet
                                    )
                                    logger.info(f"[LMS] New session '{current_task_packet.topic_title}' tier={current_task_packet.depth_tier} id={session_id[:8]}")

                                chat_history[0]["content"] = build_dynamic_prompt(
                                    current_user, is_flipped, session_ctx, current_task_packet, session_hits
                                )

                    _latency_log(lat, "03_l1_and_prompt_ready", logged_in=bool(current_user))

                    chat_history.append({"role": "user", "content": f"[{user_emotion} tone] {user_text}"})

                    # 3. 准备并发 TTS 任务
                    if not is_test_mode:
                        tts_queue = asyncio.Queue()
                        
                        # 单 WebSocket 顺序多 session（官方链接复用）；句间在 152 后不发 event 2，
                        # 仅队列结束后 finish connection。点读仍用 run_tts_to_ws。
                        async def tts_consumer(queue: asyncio.Queue):
                            try:
                                await run_tts_turn_reused_from_queue(
                                    websocket,
                                    ws_lock,
                                    CONFIG,
                                    queue,
                                    latency_hooks=lat,
                                )
                            except Exception:
                                logger.exception("TTS 流式播放期间发生异常")
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                            _latency_log(lat, "10_ws_tts_finished_event_sent")

                        # 终止上一轮仍未处理完的 TTS
                        if consumer_task and not consumer_task.done():
                            consumer_task.cancel()
                        consumer_task = asyncio.create_task(tts_consumer(tts_queue))

                    # 4. LLM 流式对话（包含首包连接 + 全量 chunk 消费，统一超时保护）
                    raw_full_reply = ""
                    sentence_buffer = ""
                    punctuation_marks = ['.', '!', '?', ',', '。', '！', '？', '，', '\n']
                    early_head_flush_used = False
                    _lat_flags = {"llm_first": False, "tts_enqueue": False}

                    async def _stream_llm_to_queue():
                        """将 create + async for 封装为单协程，便于 wait_for 统一超时"""
                        nonlocal raw_full_reply, sentence_buffer, early_head_flush_used
                        _latency_log(
                            lat,
                            "04_llm_api_request_start",
                            chat_messages=len(chat_history),
                        )
                        response = await client.chat.completions.create(
                            model="deepseek-chat", messages=chat_history,
                            max_tokens=LLM_MAX_TOKENS, stream=True
                        )
                        _latency_log(lat, "04b_llm_stream_iterable_ready")
                        async for chunk in response:
                            if chunk.choices and chunk.choices[0].delta.content:
                                delta = chunk.choices[0].delta.content
                                if not _lat_flags["llm_first"]:
                                    _lat_flags["llm_first"] = True
                                    pv = delta[:72] + ("…" if len(delta) > 72 else "")
                                    _latency_log(lat, "05_llm_first_content_delta", preview=pv)
                                raw_full_reply += delta
                                if not is_test_mode and tts_queue:
                                    sentence_buffer += delta
                                    want_flush = (
                                        any(p in delta for p in punctuation_marks)
                                        or len(sentence_buffer) > MAX_BUFFER_CHARS
                                    )
                                    if (
                                        not want_flush
                                        and not early_head_flush_used
                                        and len(sentence_buffer) >= FIRST_TTS_EARLY_FLUSH_CHARS
                                    ):
                                        want_flush = True
                                    if want_flush:
                                        chunk_text = sentence_buffer.replace("[ADVANCE]", "").strip()
                                        if chunk_text:
                                            if not _lat_flags["tts_enqueue"]:
                                                _lat_flags["tts_enqueue"] = True
                                                cq = chunk_text[:72] + ("…" if len(chunk_text) > 72 else "")
                                                _latency_log(
                                                    lat,
                                                    "06_tts_first_text_enqueued",
                                                    preview=cq,
                                                )
                                            await tts_queue.put(chunk_text)
                                            early_head_flush_used = True
                                        sentence_buffer = ""

                    async def _abort_tts():
                        """超时 / 报错时统一清理 TTS 资源，防止消费任务永久挂起"""
                        nonlocal consumer_task, tts_queue
                        if consumer_task and not consumer_task.done():
                            consumer_task.cancel()
                            try:
                                await asyncio.wait_for(consumer_task, timeout=0.5)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass
                        consumer_task = None
                        tts_queue = None

                    try:
                        await asyncio.wait_for(_stream_llm_to_queue(), timeout=20.0)
                    except asyncio.TimeoutError:
                        _latency_log(lat, "ERR_llm_wait_timeout")
                        logger.error("⏱️ LLM 响应超时（>20s），本轮中止。")
                        await _abort_tts()
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "error",
                            "code": "LLM_TIMEOUT",
                            "message": "The AI coach is taking a bit too long to think. Could you please try saying that again?"
                        })
                        continue  # 回到 while 循环等待下一帧，连接不断
                    except Exception as e:
                        _latency_log(lat, "ERR_llm_stream_exception", err_type=type(e).__name__)
                        logger.error(f"💥 LLM 调用异常: {e}", exc_info=True)
                        await _abort_tts()
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "error",
                            "code": "LLM_ERROR",
                            "message": "Something went wrong with the AI. Please try again in a moment."
                        })
                        continue

                    # 尾盘入列（仅在正常完成时执行）
                    if not is_test_mode and tts_queue:
                        final_chunk = sentence_buffer.replace("[ADVANCE]", "").strip()
                        if final_chunk:
                            if not _lat_flags["tts_enqueue"]:
                                _lat_flags["tts_enqueue"] = True
                                fq = final_chunk[:72] + ("…" if len(final_chunk) > 72 else "")
                                _latency_log(
                                    lat,
                                    "06_tts_first_text_enqueued",
                                    preview=fq,
                                    tail_flush=True,
                                )
                            await tts_queue.put(final_chunk)
                        await tts_queue.put(None)

                    # 5. 指令清洗与记录
                    session_ctx["llm_wants_to_advance"] = "[ADVANCE]" in raw_full_reply
                    if session_ctx["llm_wants_to_advance"]:
                        logger.info("[StateMachine] AI signaled ADVANCE, will transition next turn.")

                    clean_full_reply = raw_full_reply.replace("[ADVANCE]", "").strip()
                    chat_history.append({"role": "assistant", "content": clean_full_reply})
                    chat_history = trim_chat_history(chat_history)

                    # 追加 AI 回复到 transcript（L2 评估使用）
                    session_transcript.append({"role": "assistant", "text": clean_full_reply})

                    logger.info(f"🤖 AI: {clean_full_reply}")
                    _latency_log(
                        lat,
                        "98_assistant_reply_ready",
                        ai_chars=len(clean_full_reply),
                    )

                    # 6. 后置辅导服务
                    if is_test_mode:
                        await safe_send_ws(websocket, ws_lock, {"event": "test_ai_reply", "text": clean_full_reply, "phase": session_ctx["phase"]})
                    else:
                        asyncio.create_task(async_fetch_and_send_teaching(user_text, clean_full_reply, TEACHING_CONFIG, client, websocket, ws_lock))

    except WebSocketDisconnect:
        logger.info("👋 连接已断开 (WebSocketDisconnect)。")
    except Exception as e:
        logger.error(f"💥 意外异常: {e}", exc_info=True)
    finally:
        await _abort_streaming_asr()
        audio_buffer.clear()
        # 确保彻底结束正在运行的 TTS 消费任务
        if consumer_task and not consumer_task.done():
            consumer_task.cancel()
            try:
                await asyncio.wait_for(consumer_task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)