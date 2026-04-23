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
    init_tts_pool,
)

from core.dialogue_engine import (
    build_prompts, build_evaluator_prompt, advance_state_machine, evaluate_and_check_progress
)
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
try:
    _log_path = os.path.join(_BACKEND_DIR, "server_output.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    if not any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(_log_path) for h in
            logger.handlers):
        logger.addHandler(_fh)
    logger.info("EnglishCoach file log: %s", os.path.abspath(_log_path))
except OSError as _e:
    logger.warning("Could not open server_output.log: %s", _e)

load_dotenv("config.env")

CONFIG = {
    "DEEPSEEK_KEY": os.getenv("DEEPSEEK_KEY"),
    "DEEPSEEK_BASE": os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"),
    "VOLC_API_KEY": os.getenv("VOLC_API_KEY"),
    "VOLC_RESOURCE_ID_ASR": os.getenv("VOLC_RESOURCE_ID"),
    "AZURE_SPEECH_KEY": os.getenv("AZURE_SPEECH_KEY"),
    "AZURE_SPEECH_REGION": os.getenv("AZURE_SPEECH_REGION", "eastasia"),
    "ASR_LANGUAGE": os.getenv("ASR_LANGUAGE", "en-US"),
}

if not CONFIG["DEEPSEEK_KEY"] or not CONFIG["VOLC_API_KEY"]:
    logger.error("🚨 致命错误: 缺少必要的环境变量 (DEEPSEEK_KEY 或 VOLC_API_KEY)。请检查 config.env 文件。")
    raise RuntimeError("Missing essential API keys in config.")

if not CONFIG.get("VOLC_RESOURCE_ID_ASR"):
    logger.warning("⚠️ 未设置 VOLC_RESOURCE_ID（ASR）：语音转写将在调用时失败，请检查 config.env。")
if not CONFIG.get("AZURE_SPEECH_KEY") or not CONFIG.get("AZURE_SPEECH_REGION"):
    logger.warning("⚠️ 未设置 AZURE_SPEECH_KEY/REGION：合成语音将在调用时失败，请检查 config.env。")

TEACHING_CONFIG = {"enable_correction": False, "enable_translation": True, "enable_hints": True}

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


app = FastAPI()


@app.on_event("startup")
async def on_startup():
    await run_in_threadpool(ensure_schema_upgrades)
    await run_in_threadpool(session_planner.warm_up)
    logger.info("🧠 SessionPlanner VectorStore 已就绪。")
    await warm_deepseek_connection("server_startup")
    logger.info("🔊 Azure Neural TTS 已就绪（无需预热）。")


@app.on_event("shutdown")
async def on_shutdown():
    try:
        await client.close()
    except Exception as e:
        logger.warning("[LLM] AsyncOpenAI close: %s", e)


# ================= 副 LLM 导演工具 Schema =================
# 主 LLM 将不再挂载任何 Tools，全力保障 TTFB。以下工具专供后台副 LLM 使用。
EVALUATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_analysis_and_feedback",
            "description": "Submit analysis of the user's latest speech and the AI coach's response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ai_translation_cn": {
                        "type": "string",
                        "description": "A natural Chinese translation of the AI Coach's English reply."
                    },
                    "suggested_hints_en": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 or 3 short English responses the User could say next."
                    },
                    "coach_correction_cn": {
                        "type": "string",
                        "description": "If the user made a grammar/vocabulary mistake, briefly correct it in Chinese. Otherwise leave empty."
                    },
                    "should_advance_phase": {
                        "type": "boolean",
                        "description": "True ONLY if the current phase goal is fulfilled and the coach is moving to the next phase."
                    }
                },
                "required": ["ai_translation_cn", "suggested_hints_en", "coach_correction_cn", "should_advance_phase"]
            }
        }
    }
]

# ================= 业务全局常量 =================

LLM_MAX_TOKENS = 500
DEFAULT_TOPIC_ID = 999
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_BUFFER_CHARS = 65
FIRST_TTS_EARLY_FLUSH_CHARS = 22
MAX_CONTEXT_TOKENS = 2000


# ═══════════════════════════════════════════════════════════════════════════
# REST API 
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/topics")
async def get_topics(user_id: Optional[str] = Query(default=None)):
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
    def _query():
        import datetime
        with get_db() as db:
            sessions = db.query(LearningSession).filter(
                LearningSession.user_id == user_id
            ).order_by(LearningSession.start_time.desc()).all()

            all_progress = db.query(UserProgress).filter(
                UserProgress.user_id == user_id
            ).all()

            mastered_nodes = [p for p in all_progress if p.mastery_score >= 60.0]
            total_practiced = len(all_progress)

            streak = 0
            if sessions:
                today = datetime.datetime.utcnow().date()
                day = today
                session_dates = set(s.start_time.date() for s in sessions if s.start_time)
                while day in session_dates:
                    streak += 1
                    day -= datetime.timedelta(days=1)

            topic_ids_practiced = list(set(s.topic_id for s in sessions if s.topic_id))

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
                "topics_summary": topics_summary[:10],
            }

    data = await run_in_threadpool(_query)
    return JSONResponse(content=data)


# ================= 辅助函数 =================

def _latency_log(lat: dict, stage: str, **kwargs) -> None:
    t0 = lat.get("t0")
    if not isinstance(t0, (int, float)):
        return
    tid = lat.get("turn_id", "?")
    ms = (time.perf_counter() - float(t0)) * 1000.0
    extra = (" " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())) if kwargs else ""
    logger.info("[LATENCY] turn=%s stage=%-28s cum=%8.1fms%s", tid, stage, ms, extra)


def _coerce_turn_trace_id(raw) -> str:
    if not isinstance(raw, str):
        return uuid.uuid4().hex[:8]
    s = raw.strip().replace("-", "")
    if 4 <= len(s) <= 32 and s.isalnum():
        return s.lower()
    return uuid.uuid4().hex[:8]


def _e2e_log_client_report(data: dict) -> None:
    tid_raw = data.get("trace_id")
    tid = tid_raw.strip()[:32].lower() if isinstance(tid_raw, str) and tid_raw.strip() else "?"
    logger.info(
        "[E2E] turn=%s client_submit_to_first_pcm_ms=%r client_submit_to_tts_finished_ms=%r "
        "had_first_pcm=%r client_report_wall_ms=%r",
        tid,
        data.get("submit_to_first_pcm_ms"),
        data.get("submit_to_tts_finished_ms"),
        data.get("had_first_pcm"),
        data.get("client_report_wall_ms"),
    )


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_or_get_user(user_id: str):
    if not user_id:
        return None
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            db.add(user)
            db.commit()
            db.refresh(user)
        db.expunge(user)
        return user


def _fetch_user_settings_dict(user_id: str) -> dict:
    if not user_id:
        return {}
    with get_db() as db:
        from database import User as UserModel
        u = db.query(UserModel).filter(UserModel.id == user_id).first()
        return dict(u.settings or {}) if u else {}


def _update_lms_settings(user_id: str, depth_preference=None, new_topic_appetite=None, learner_level=None):
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


def update_user_politeness(user_id: str, level: int):
    if not user_id:
        return
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.politeness_level = level
            db.commit()


def _take_mastery_snapshot(user_id: str, task_packet) -> dict:
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
    if len(history) <= 1:
        return history
    system = history[0]
    turns = history[1:]
    while len(turns) > 2:
        total_chars = sum(len(m.get("content", "")) for m in [system] + turns)
        if total_chars // 4 <= max_tokens:
            break
        turns = turns[2:]
    return [system] + turns


async def safe_send_ws(websocket: WebSocket, ws_lock: asyncio.Lock, payload: dict):
    try:
        async with ws_lock:
            await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.error(f"Ws 发送失败: {e}")


# ================= 后台并路副 LLM 逻辑 =================
async def _run_background_evaluator(
    user_text: str, ai_text: str, session_ctx: dict, task_packet,
    websocket: WebSocket, ws_lock: asyncio.Lock, lat: dict
):
    """
    旁路导演运行：利用副 LLM 分析对话，生成 JSON 辅导数据，并裁定状态机推进。
    这将在主语音流发送时并发执行，完全不阻塞主音频下发。
    """
    _latency_log(lat, "11_evaluator_llm_start")
    sys_prompt = build_evaluator_prompt(session_ctx, task_packet)
    eval_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"User said: {user_text}\\nCoach replied: {ai_text}"}
    ]

    feedback_data = None
    try:
        # 强制要求副 LLM 输出工具 JSON，保证 99.9% 稳定性
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=eval_messages,
            tools=EVALUATOR_TOOLS,
            tool_choice={"type": "function", "function": {"name": "submit_analysis_and_feedback"}},
            max_tokens=400,
        )
        msg = response.choices[0].message
        if msg.tool_calls:
            args = msg.tool_calls[0].function.arguments
            feedback_data = json.loads(args)
    except Exception as e:
        logger.error(f"副 LLM (Evaluator) 调用失败或解析异常: {e}")

    # 兜底降级处理 (Fallback)
    if not feedback_data:
        logger.warning("⚠️ 副 LLM 提取 JSON 失败，触发优雅兜底策略。")
        feedback_data = {
            "ai_translation_cn": "（AI教练这段话太投入，小助教没来得及翻译~）",
            "suggested_hints_en": ["Could you repeat that?", "I see.", "Okay, thanks."],
            "coach_correction_cn": "",
            "should_advance_phase": False
        }

    # 提取状态机信号，写入上下文，供用户下一轮发言(L1 判定)时触发流转
    should_adv = feedback_data.get("should_advance_phase", False)
    if should_adv:
        session_ctx["llm_wants_to_advance"] = True
        logger.info("[Director] 🎬 副模型导演批准：本阶段目标达成，准备进入下一阶段！")

    # 最终装盘推给前端 (替换掉骨架屏)
    feedback_data["user_text"] = user_text
    feedback_data["ai_text"] = ai_text
    _latency_log(lat, "12_evaluator_json_ready")
    await safe_send_ws(websocket, ws_lock, {"event": "teaching_data", "data": feedback_data})


# ================= 1. WebSocket 主入口 =================
@app.websocket("/ws/coach")
async def websocket_endpoint(websocket: WebSocket, user_id: Optional[str] = None):
    await websocket.accept()
    logger.info(f"📱 客户端已连接: {websocket.client.host}")
    asyncio.create_task(warm_deepseek_connection(f"ws_accept:{websocket.client.host}"))

    ws_lock = asyncio.Lock()
    audio_buffer = bytearray()
    asr_stream_queue: Optional[asyncio.Queue] = None
    asr_stream_task: Optional[asyncio.Task] = None
    asr_partial_last_mono = 0.0

    async def _emit_asr_partial(txt: str):
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
    session_ctx = SessionContext()
    current_task_packet = None
    session_transcript: list[dict] = []
    session_id: str = str(uuid.uuid4())
    mastery_snapshot: dict = {}

    current_user = await run_in_threadpool(init_or_get_user, user_id)

    if current_user:
        current_task_packet = await run_in_threadpool(
            session_planner.build_task_packet, current_user.id
        )
        mastery_snapshot = await run_in_threadpool(
            _take_mastery_snapshot, current_user.id, current_task_packet
        )

    topic_id_for_progress = (
        current_task_packet.topic_id
        if current_task_packet and current_task_packet.topic_id
        else DEFAULT_TOPIC_ID
    )

    static_sys, dynamic_turn = build_prompts(
        current_user, is_flipped, session_ctx, current_task_packet, session_hits
    )
    chat_history = [{"role": "system", "content": static_sys}]

    if current_user:
        with get_db() as db:
            await evaluate_and_check_progress(
                db, current_user.id, topic_id_for_progress, "",
                session_hits, session_ctx, websocket, ws_lock,
                task_packet=current_task_packet,
            )
            static_sys, dynamic_turn = build_prompts(
                current_user, is_flipped, session_ctx, current_task_packet, session_hits
            )
            chat_history[0]["content"] = static_sys

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
                    logger.warning("⚠️ 警告：音频缓冲区超限，强制清空。")
                    await _abort_streaming_asr()
                    audio_buffer.clear()
                    await safe_send_ws(websocket, ws_lock, {"event": "error", "message": "Audio buffer overflow"})
                    continue
                if asr_stream_task is not None and asr_stream_task.done():
                    try:
                        exc = asr_stream_task.exception()
                        if exc: logger.warning("流式 ASR 任务异常: %s", exc)
                    except:
                        pass
                    asr_stream_task = asr_stream_queue = None
                if asr_stream_task is None and len(audio_buffer) >= ASR_STREAM_START_BYTES:
                    asr_stream_queue = asyncio.Queue(maxsize=0)
                    asr_stream_task = asyncio.create_task(
                        run_volc_streaming_asr_worker(asr_stream_queue, CONFIG, on_partial=_emit_asr_partial)
                    )
                    await asr_stream_queue.put(bytes(audio_buffer))
                elif asr_stream_queue is not None and asr_stream_task is not None and not asr_stream_task.done():
                    await asr_stream_queue.put(message["bytes"])
                continue

            # --- 文本指令处理 ---
            if "text" in message:
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue

                action = data.get("action")
                payload_user_id = data.get("user_id")

                if payload_user_id and not current_user:
                    current_user = await run_in_threadpool(init_or_get_user, payload_user_id)

                if action == "client_latency_report":
                    _e2e_log_client_report(data)
                    continue

                if action == "ping":
                    ws_payload: dict = {"event": "warmup_success"}
                    uid = payload_user_id or (current_user.id if current_user else None)
                    if uid:
                        if not current_user:
                            current_user = await run_in_threadpool(init_or_get_user, uid)
                        st = await run_in_threadpool(_fetch_user_settings_dict, uid)
                        if st:
                            ws_payload["user_settings"] = st
                    await safe_send_ws(websocket, ws_lock, ws_payload)
                    continue

                if action == "swap_role":
                    is_flipped = not is_flipped
                    session_ctx["phase"], session_ctx["phase_turns"] = "ICE_BREAKING", 0
                    static_sys, dynamic_turn = build_prompts(
                        current_user, is_flipped, session_ctx, current_task_packet, session_hits
                    )
                    chat_history[0]["content"] = static_sys
                    await safe_send_ws(websocket, ws_lock, {"event": "role_swapped", "is_flipped": is_flipped})
                    continue

                if action == "update_lms_settings":
                    if current_user:
                        depth = data.get("depth_preference")
                        appetite = data.get("new_topic_appetite")
                        learner_level = data.get("learner_level")
                        await run_in_threadpool(_update_lms_settings, current_user.id, depth, appetite, learner_level)
                        current_user = await run_in_threadpool(init_or_get_user, current_user.id)
                        static_sys, dynamic_turn = build_prompts(current_user, is_flipped, session_ctx, current_task_packet, session_hits)
                        chat_history[0]["content"] = static_sys
                    continue

                if action == "update_politeness":
                    if current_user:
                        level = data.get("level", 1)
                        await run_in_threadpool(update_user_politeness, current_user.id, level)
                        current_user = await run_in_threadpool(init_or_get_user, current_user.id)
                        static_sys, dynamic_turn = build_prompts(current_user, is_flipped, session_ctx, current_task_packet, session_hits)
                        chat_history[0]["content"] = static_sys
                    continue

                if action == "request_topic":
                    description = data.get("description", "").strip()
                    if description and current_user:
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "topic_generating",
                            "message": f"Finding the best match for: {description}",
                            "message_zh": f"正在为你匹配练习场景：{description}",
                        })
                        try:
                            new_topic = await topic_generator.get_or_generate_topic(description, client)
                            await run_in_threadpool(session_planner.add_topic_to_store, new_topic)
                            current_task_packet = await run_in_threadpool(
                                session_planner.build_task_packet_for_topic, current_user.id, new_topic.id
                            )
                            topic_id_for_progress = current_task_packet.topic_id
                            session_id = str(uuid.uuid4())
                            session_hits.clear()
                            session_transcript.clear()
                            mastery_snapshot = await run_in_threadpool(_take_mastery_snapshot, current_user.id, current_task_packet)
                            session_ctx = SessionContext()

                            static_sys, dynamic_turn = build_prompts(current_user, is_flipped, session_ctx, current_task_packet, session_hits)
                            chat_history = [{"role": "system", "content": static_sys}]

                            await safe_send_ws(websocket, ws_lock, {
                                "event": "topic_changed",
                                "topic_id": current_task_packet.topic_id,
                                "topic_title": current_task_packet.topic_title,
                                "topic_title_zh": getattr(current_task_packet, "topic_title_zh", None),
                                "role_name": current_task_packet.role_name,
                                "depth_tier": current_task_packet.depth_tier,
                                "session_id": session_id,
                            })
                        except Exception as e:
                            logger.error(f"[TopicRequest] Failed: {e}", exc_info=True)
                            await safe_send_ws(websocket, ws_lock, {
                                "event": "error", "code": "TOPIC_GENERATION_FAILED",
                                "message": "Could not generate topic. Please try again.",
                                "message_zh": "暂时无法生成话题，请稍后再试。",
                            })
                    continue

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

                if action == "cancel_tts":
                    if consumer_task and not consumer_task.done():
                        consumer_task.cancel()
                        try: await asyncio.wait_for(consumer_task, timeout=0.5)
                        except: pass
                    consumer_task = tts_queue = None
                    await _abort_streaming_asr()
                    audio_buffer.clear()
                    logger.info("🛑 [打断] 用户打断 TTS。")
                    continue

                if action in ["user_finish_speaking", "test_text_input"]:
                    is_test_mode = (action == "test_text_input")
                    asr_partial_last_mono = 0.0
                    turn_id = _coerce_turn_trace_id(data.get("trace_id"))
                    wall_recv_ms = int(time.time() * 1000)
                    csw = data.get("client_submit_wall_ms")
                    client_wall_ok = isinstance(csw, int) and csw > 0
                    lat: dict = {"turn_id": turn_id, "t0": time.perf_counter()}
                    if client_wall_ok: lat["client_submit_wall_ms"] = int(csw)
                    _kwargs_01 = {"action": action, "test_mode": is_test_mode, "wall_recv_ms": wall_recv_ms}
                    if client_wall_ok:
                        _kwargs_01["client_submit_wall_ms"] = int(csw)
                        _kwargs_01["wall_skew_hint_ms"] = wall_recv_ms - int(csw)
                    _latency_log(lat, "01_turn_accepted", **_kwargs_01)

                    # 1. 语音转文本
                    if is_test_mode:
                        user_text, user_emotion = data.get("text", ""), "neutral"
                        _latency_log(lat, "02_skip_asr_test_text")
                    else:
                        used_streaming_asr = False
                        if asr_stream_task is not None and not asr_stream_task.done() and asr_stream_queue is not None:
                            used_streaming_asr = True
                            _latency_log(lat, "02_asr_stream_finish")
                            await asr_stream_queue.put(None)
                            try: user_text, user_emotion = await asr_stream_task
                            except asyncio.CancelledError: user_text, user_emotion = "", "neutral"
                            except Exception as e:
                                logger.warning("流式 ASR 收束失败，回退整包: %s", e)
                                asr_stream_task = asr_stream_queue = None
                                user_text, user_emotion = await run_volcengine_wss_asr(audio_buffer, CONFIG)
                            asr_stream_task = asr_stream_queue = None
                            audio_buffer.clear()
                        else:
                            user_text, user_emotion = await run_volcengine_wss_asr(audio_buffer, CONFIG)
                            audio_buffer.clear()
                            if asr_stream_task is not None or asr_stream_queue is not None:
                                await _abort_streaming_asr()
                        _latency_log(lat, "02_asr_done", user_chars=len(user_text or ""), streaming_asr=used_streaming_asr)

                    if not user_text:
                        _latency_log(lat, "02b_empty_after_asr_skip_rest")
                        if not is_test_mode:
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                        continue

                    session_ctx["phase_turns"] += 1
                    session_transcript.append({"role": "user", "text": user_text})
                    logger.info(f"[Turn {session_ctx['phase_turns']}] ({session_ctx['phase']}) User: {user_text}")

                    # 2. 状态机评估 (利用上一轮副LLM写下的 llm_wants_to_advance)
                    if current_user:
                        with get_db() as db:
                            await evaluate_and_check_progress(
                                db, current_user.id, topic_id_for_progress, user_text,
                                session_hits, session_ctx, websocket, ws_lock,
                                task_packet=current_task_packet,
                            )
                            fresh_topic = db.query(Topic).filter(Topic.id == topic_id_for_progress).first()

                            # 状态流转执行
                            did_transition = advance_state_machine(
                                session_ctx, db, fresh_topic, session_hits, current_task_packet
                            )
                            if did_transition:
                                if session_ctx["phase"] == "ICE_BREAKING":
                                    preliminary = build_preliminary_report(
                                        task_packet=current_task_packet, session_id=session_id,
                                        session_hits=session_hits, session_ctx=session_ctx,
                                        mastery_snapshot=mastery_snapshot, db=db, user_id=current_user.id,
                                    )
                                    await safe_send_ws(websocket, ws_lock, preliminary)
                                    asyncio.create_task(_renew_task_packet(
                                        current_user.id, current_task_packet, session_hits.copy(),
                                        session_transcript.copy(), client, session_id, websocket, ws_lock,
                                    ))
                                    current_task_packet = await run_in_threadpool(
                                        session_planner.build_task_packet, current_user.id
                                    )
                                    topic_id_for_progress = current_task_packet.topic_id if current_task_packet.topic_id else DEFAULT_TOPIC_ID
                                    session_id = str(uuid.uuid4())
                                    session_hits.clear()
                                    session_transcript.clear()
                                    mastery_snapshot = await run_in_threadpool(
                                        _take_mastery_snapshot, current_user.id, current_task_packet
                                    )

                            static_sys, dynamic_turn = build_prompts(
                                current_user, is_flipped, session_ctx, current_task_packet, session_hits
                            )
                            chat_history[0]["content"] = static_sys

                    _latency_log(lat, "03_l1_and_prompt_ready", logged_in=bool(current_user))

                    # 追加用户消息到主历史记录
                    chat_history.append({"role": "user", "content": f"[{user_emotion} tone] {user_text}"})

                    messages_to_send = chat_history.copy()
                    messages_to_send.append({"role": "system", "content": dynamic_turn})

                    # 3. 准备并发 TTS 任务
                    if not is_test_mode:
                        tts_queue = asyncio.Queue()
                        async def tts_consumer(queue: asyncio.Queue):
                            try:
                                await run_tts_turn_reused_from_queue(websocket, ws_lock, CONFIG, queue, latency_hooks=lat)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logger.exception("TTS 流式播放期间发生异常")
                            finally:
                                await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                                _latency_log(lat, "10_ws_tts_finished_event_sent")

                        if consumer_task and not consumer_task.done(): consumer_task.cancel()
                        consumer_task = asyncio.create_task(tts_consumer(tts_queue))

                    # 4. 主 LLM 极速流式对话（卸下一切 Tools，实现完全秒回）
                    raw_full_reply = ""
                    sentence_buffer = ""
                    punctuation_marks = ['.', '!', '?', '。', '！', '？', '\n']
                    _lat_flags = {"llm_first": False, "tts_enqueue": False}

                    async def _stream_llm_to_queue():
                        nonlocal raw_full_reply, sentence_buffer
                        _latency_log(lat, "04_llm_api_request_start", chat_messages=len(messages_to_send))

                        # 🚨 此处删除了 Tools 参数，完全让其自由对话
                        response = await client.chat.completions.create(
                            model="deepseek-chat",
                            messages=messages_to_send,
                            max_tokens=LLM_MAX_TOKENS,
                            stream=True
                        )
                        _latency_log(lat, "04b_llm_stream_iterable_ready")

                        async for chunk in response:
                            if not chunk.choices: continue
                            delta = chunk.choices[0].delta

                            if delta.content:
                                content = delta.content
                                if not _lat_flags["llm_first"]:
                                    _lat_flags["llm_first"] = True
                                    _latency_log(lat, "05_llm_first_content_delta", preview=content[:72])

                                # 👉 核心前端掩护：将英文打字机字符推给前端
                                asyncio.create_task(safe_send_ws(websocket, ws_lock, {
                                    "event": "ai_text_stream", 
                                    "text": content
                                }))

                                raw_full_reply += content
                                sentence_buffer += content

                                has_punct = any(p in content for p in punctuation_marks)
                                safety_cutoff = len(sentence_buffer) > 120 and (' ' in content or '\n' in content)

                                if has_punct or safety_cutoff:
                                    chunk_text = sentence_buffer.strip()
                                    if chunk_text:
                                        if not _lat_flags["tts_enqueue"]:
                                            _lat_flags["tts_enqueue"] = True
                                            _latency_log(lat, "06_tts_first_text_enqueued", preview=chunk_text[:72])
                                        if not is_test_mode and tts_queue:
                                            await tts_queue.put(chunk_text)
                                    sentence_buffer = ""

                    async def _abort_tts():
                        nonlocal consumer_task, tts_queue
                        if consumer_task and not consumer_task.done():
                            consumer_task.cancel()
                            try: await asyncio.wait_for(consumer_task, timeout=0.5)
                            except: pass
                        consumer_task = tts_queue = None

                    try:
                        await asyncio.wait_for(_stream_llm_to_queue(), timeout=20.0)
                    except asyncio.TimeoutError:
                        _latency_log(lat, "ERR_llm_wait_timeout")
                        logger.error("⏱️ LLM 响应超时（>20s），本轮中止。")
                        await _abort_tts()
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "error", "code": "LLM_TIMEOUT",
                            "message": "The AI coach is taking a bit too long to think. Could you please try saying that again?"
                        })
                        continue
                    except Exception as e:
                        _latency_log(lat, "ERR_llm_stream_exception", err_type=type(e).__name__)
                        logger.error(f"💥 LLM 调用异常: {e}", exc_info=True)
                        await _abort_tts()
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "error", "code": "LLM_ERROR",
                            "message": "Something went wrong with the AI. Please try again in a moment."
                        })
                        continue

                    # 尾盘清理：刷入最后的文本并发送结束信号给 TTS 队列
                    if not is_test_mode and tts_queue:
                        final_chunk = sentence_buffer.strip()
                        if final_chunk:
                            if not _lat_flags["tts_enqueue"]:
                                _lat_flags["tts_enqueue"] = True
                            await tts_queue.put(final_chunk)
                        await tts_queue.put(None)

                    # 大模型对话上屏及历史留存
                    clean_full_reply = raw_full_reply.strip()
                    chat_history.append({"role": "assistant", "content": clean_full_reply})
                    chat_history = trim_chat_history(chat_history)
                    session_transcript.append({"role": "assistant", "text": clean_full_reply})

                    logger.info(f"🤖 演员 AI: {clean_full_reply}")
                    _latency_log(lat, "98_assistant_reply_ready", ai_chars=len(clean_full_reply))

                    if is_test_mode:
                        await safe_send_ws(websocket, ws_lock, {"event": "test_ai_reply", "text": clean_full_reply, "phase": session_ctx["phase"]})

                    # 5. 👉 触发副 LLM（导演/教辅）后台任务
                    if TEACHING_CONFIG.get("enable_translation", True):
                        asyncio.create_task(
                            _run_background_evaluator(
                                user_text, clean_full_reply, session_ctx, 
                                current_task_packet, websocket, ws_lock, lat
                            )
                        )

    except WebSocketDisconnect:
        logger.info("👋 连接已断开 (WebSocketDisconnect)。")
    except Exception as e:
        logger.error(f"💥 意外异常: {e}", exc_info=True)
    finally:
        await _abort_streaming_asr()
        audio_buffer.clear()
        if consumer_task and not consumer_task.done():
            consumer_task.cancel()
            try:
                await asyncio.wait_for(consumer_task, timeout=0.5)
            except:
                pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)