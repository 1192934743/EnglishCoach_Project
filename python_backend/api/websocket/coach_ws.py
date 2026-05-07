import json
import uuid
import time
import asyncio
import logging
import base64
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

# --- 引入我们前两阶段拆分好的底层设施 ---
from core.config import CONFIG, TEACHING_CONFIG, LLM_MAX_TOKENS, MAX_AUDIO_BYTES, DEFAULT_TOPIC_ID
from infrastructure.llm.client import warm_llm_connection, client as llm_client
from infrastructure.llm import llm_router
from core.telemetry import _latency_log, _coerce_turn_trace_id, _e2e_log_client_report
from api.dependencies import get_db
from application.services.user_service import (
    init_or_get_user, _fetch_user_settings_dict, _update_lms_settings, 
    update_user_politeness, _take_mastery_snapshot
)
from application.services.session_service import EVALUATOR_TOOLS, _renew_task_packet, trim_chat_history

# --- 引入项目原有的核心业务模块 ---
from database import Topic
from core.audio_service import (
    MIN_AUDIO_BYTES,
    ASR_STREAM_START_BYTES,
    run_volcengine_wss_asr,
    run_volc_streaming_asr_worker,
)
from infrastructure.tts import get_tts_factory
from core.dialogue_engine import build_prompts, build_evaluator_prompt, advance_state_machine, evaluate_and_check_progress
from domain.entities.session_context import SessionContext
import application.services.session_planner as session_planner
import application.services.assessment_engine as assessment_engine
from application.services.report_builder import build_preliminary_report
import infrastructure.topic_generator as topic_generator

logger = logging.getLogger("EnglishCoach")
router = APIRouter()

# session_transcript 最大长度，防止内存无限增长
MAX_TRANSCRIPT_LENGTH = 50

async def safe_send_ws(websocket: WebSocket, ws_lock: asyncio.Lock, payload: dict):
    """
    【请将 server.py 中的 safe_send_ws 完整函数体剪切到这里】
    """
    try:
        async with ws_lock:
            await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.error(f"Ws 发送失败: {e}")

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
        # 使用 stream=False，直接获取完整响应
        response = await llm_router.chat(
            messages=eval_messages,
            model=None,  # 副 LLM 始终走自动容灾，不受开发者模式影响
            tools=EVALUATOR_TOOLS,
            tool_choice={"type": "function", "function": {"name": "submit_analysis_and_feedback"}},
            max_tokens=400,
            stream=False,  # 副 LLM 不需要流式
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
            "should_advance_phase": False,
            # Phase 2 new fields (all false to avoid false positives)
            "intent_achieved": False,
            "constraints_hit": False,
            "constraints_hit_details": [],
            "scenario_completed": False,
        }

    # 兼容处理：若旧模型未返回新字段，补填默认值
    feedback_data.setdefault("intent_achieved", False)
    feedback_data.setdefault("constraints_hit", False)
    feedback_data.setdefault("constraints_hit_details", [])
    feedback_data.setdefault("scenario_completed", False)

    # 提取状态机信号，写入上下文，供用户下一轮发言(L1 判定)时触发流转
    should_adv = feedback_data.get("should_advance_phase", False)
    if should_adv:
        session_ctx["llm_wants_to_advance"] = True
        logger.info("[Director] 🎬 副模型导演批准：本阶段目标达成，准备进入下一阶段！")

    # Phase 2：双轨校验信号注入 session_ctx
    if feedback_data.get("scenario_completed"):
        session_ctx["director_scenario_completed"] = True
        session_ctx["director_constraints_hit"] = feedback_data.get("constraints_hit", False)
        session_ctx["director_intent_achieved"] = feedback_data.get("intent_achieved", False)
        logger.info("[Director] 🎯 双轨校验通过，scenario_completed=True，触发微场景通关信号")

    # 最终装盘推给前端 (替换掉骨架屏)
    feedback_data["user_text"] = user_text
    feedback_data["ai_text"] = ai_text
    _latency_log(lat, "12_evaluator_json_ready")
    await safe_send_ws(websocket, ws_lock, {"event": "teaching_data", "data": feedback_data})
    pass


@router.websocket("/ws/coach")
async def websocket_endpoint(websocket: WebSocket, user_id: Optional[str] = None):
    await websocket.accept()
    logger.info(f"📱 客户端已连接: {websocket.client.host}")
    asyncio.create_task(warm_llm_connection(f"ws_accept:{websocket.client.host}"))

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

    async def _send_recovery_tts(websocket, ws_lock, user_settings=None):
        """发送 ASR 恢复提示语，保底机制"""
        recovery_text = "Sorry, I didn't catch that. Could you please repeat that?"
        tts_engine_name = "azure"
        tts_voice = "en-US-GuyNeural"
        if user_settings:
            tts_engine_name = user_settings.get("tts_engine", "azure")
            tts_voice = user_settings.get("tts_voice", "en-US-GuyNeural")
        try:
            provider = get_tts_factory().get_provider(tts_engine_name)
            if provider:
                await provider.synthesize_single(
                    recovery_text, websocket, ws_lock, CONFIG, voice_id=tts_voice
                )
        except Exception as e:
            logger.warning(f"恢复 TTS 失败: {e}")

    consumer_task = None
    tts_queue = None

    is_flipped = False
    session_ctx = SessionContext()
    session_ctx["visited_scenarios"] = set()  # 用于循环检测
    current_task_packet = None
    session_transcript: list[dict] = []
    session_id: str = str(uuid.uuid4())
    mastery_snapshot: dict = {}

    current_user = await run_in_threadpool(init_or_get_user, user_id)

    if current_user:
        current_task_packet = await run_in_threadpool(
            session_planner.build_task_packet_for_topic, current_user.id, DEFAULT_TOPIC_ID
        )
        logger.info(
            f"[Init] topic_id={current_task_packet.topic_id} "
            f"title={current_task_packet.topic_title} "
            f"has_constraints={current_task_packet.has_constraints()}"
        )
        mastery_snapshot = await run_in_threadpool(
            _take_mastery_snapshot, current_user.id, current_task_packet
        )
    else:
        from database import SessionLocal as _DB
        _db = _DB()
        try:
            current_task_packet = session_planner._fallback_task_packet(_db)
        finally:
            _db.close()
        mastery_snapshot = {}

    topic_id_for_progress = (
        current_task_packet.topic_id
        if current_task_packet and current_task_packet.topic_id
        else DEFAULT_TOPIC_ID
    )

    # 验证 TaskPacket 数据
    if current_task_packet:
        logger.info(
            f"[Prompt] Using topic_id={current_task_packet.topic_id} "
            f"title={current_task_packet.topic_title} "
            f"role={current_task_packet.role_name} "
            f"has_constraints={current_task_packet.has_constraints()}"
        )

    static_sys, dynamic_turn = build_prompts(
        current_user, is_flipped, session_ctx, current_task_packet
    )
    chat_history = [{"role": "system", "content": static_sys}]

    if current_user:
        with get_db() as db:
            await evaluate_and_check_progress(
                db, current_user.id, topic_id_for_progress, "",
                session_ctx, websocket, ws_lock,
                task_packet=current_task_packet,
            )
            static_sys, dynamic_turn = build_prompts(
                current_user, is_flipped, session_ctx, current_task_packet
            )
            chat_history[0]["content"] = static_sys

    # 首次连接时主动推送话题信息
    if current_task_packet and current_task_packet.topic_id:
        await safe_send_ws(websocket, ws_lock, {
            "event": "topic_changed",
            "topic_id": current_task_packet.topic_id,
            "topic_title": current_task_packet.topic_title,
            "topic_title_zh": getattr(current_task_packet, "topic_title_zh", None) or "",
            "role_name": current_task_packet.role_name,
            "depth_tier": getattr(current_task_packet, "depth_tier", None),
            "session_id": session_id,
        })

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
                        logger.info(f"[Ping] Sending warmup_success to uid={uid}, user_settings={st}")
                    await safe_send_ws(websocket, ws_lock, ws_payload)
                    continue

                if action == "swap_role":
                    is_flipped = not is_flipped
                    session_ctx["phase"], session_ctx["phase_turns"] = "ICE_BREAKING", 0
                    # 阶段二：重置微场景轮数
                    session_ctx["turn_count_in_scenario"] = 0
                    session_ctx.pop("_constraint_hits", None)
                    static_sys, dynamic_turn = build_prompts(
                        current_user, is_flipped, session_ctx, current_task_packet
                    )
                    chat_history[0]["content"] = static_sys
                    await safe_send_ws(websocket, ws_lock, {"event": "role_swapped", "is_flipped": is_flipped})
                    continue

                if action == "update_lms_settings":
                    if current_user:
                        depth = data.get("depth_preference")
                        appetite = data.get("new_topic_appetite")
                        learner_level = data.get("learner_level")
                        tts_engine = data.get("tts_engine")
                        tts_voice = data.get("tts_voice")
                        # 更新数据库
                        await run_in_threadpool(_update_lms_settings, current_user.id, depth, appetite, learner_level, tts_engine, tts_voice)
                        # 更新内存中的 current_user.settings（无需重新查询数据库）
                        if tts_engine is not None:
                            current_user.settings["tts_engine"] = str(tts_engine).strip()
                        if tts_voice is not None:
                            current_user.settings["tts_voice"] = str(tts_voice).strip()
                        static_sys, dynamic_turn = build_prompts(current_user, is_flipped, session_ctx, current_task_packet)
                        chat_history[0]["content"] = static_sys
                    continue

                if action == "update_politeness":
                    if current_user:
                        level = data.get("level", 1)
                        await run_in_threadpool(update_user_politeness, current_user.id, level)
                        current_user = await run_in_threadpool(init_or_get_user, current_user.id)
                        static_sys, dynamic_turn = build_prompts(current_user, is_flipped, session_ctx, current_task_packet)
                        chat_history[0]["content"] = static_sys
                    continue

                if action == "switch_topic":
                    topic_id = data.get("topic_id")
                    if topic_id and current_user:
                        try:
                            current_task_packet = await run_in_threadpool(
                                session_planner.build_task_packet_for_topic,
                                current_user.id,
                                topic_id
                            )
                            if current_task_packet:
                                topic_id_for_progress = current_task_packet.topic_id
                                session_id = str(uuid.uuid4())
                                session_transcript.clear()
                                session_ctx = SessionContext()
                                mastery_snapshot = await run_in_threadpool(
                                    _take_mastery_snapshot, current_user.id, current_task_packet
                                )
                                static_sys, dynamic_turn = build_prompts(
                                    current_user, is_flipped, session_ctx, current_task_packet
                                )
                                chat_history = [{"role": "system", "content": static_sys}]
                                await safe_send_ws(websocket, ws_lock, {
                                    "event": "topic_changed",
                                    "topic_id": current_task_packet.topic_id,
                                    "topic_title": current_task_packet.topic_title,
                                    "topic_title_zh": getattr(current_task_packet, "topic_title_zh", None) or "",
                                    "role_name": current_task_packet.role_name,
                                    "depth_tier": current_task_packet.depth_tier,
                                    "session_id": session_id,
                                })
                        except Exception as e:
                            logger.error(f"[SwitchTopic] Failed: {e}", exc_info=True)
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
                            new_topic = await topic_generator.get_or_generate_topic(description, llm_client)
                            await run_in_threadpool(session_planner.add_topic_to_store, new_topic)
                            current_task_packet = await run_in_threadpool(
                                session_planner.build_task_packet_for_topic, current_user.id, new_topic.id
                            )
                            topic_id_for_progress = current_task_packet.topic_id
                            session_id = str(uuid.uuid4())
                            session_transcript.clear()
                            mastery_snapshot = await run_in_threadpool(_take_mastery_snapshot, current_user.id, current_task_packet)
                            session_ctx = SessionContext()

                            static_sys, dynamic_turn = build_prompts(current_user, is_flipped, session_ctx, current_task_packet)
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
                            tts_engine_name = (
                                current_user.settings.get("tts_engine")
                                if current_user and current_user.settings
                                else None
                            )
                            tts_voice = (
                                current_user.settings.get("tts_voice")
                                if current_user and current_user.settings
                                else None
                            )
                            provider = get_tts_factory().get_provider(tts_engine_name)
                            if provider:
                                await provider.synthesize_single(
                                    text_to_speak, websocket, ws_lock, CONFIG, voice_id=tts_voice
                                )
                            else:
                                logger.error(f"[TTS] Provider not found for engine: {tts_engine_name}")
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
                    is_ai_first_strike = data.get("is_ai_first_strike", False)  # ← 【阶段四新增】AI 先手标记
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
                        # 【阶段四新增】AI 先手模式：注入占位符触发 LLM 主动开场
                        if is_ai_first_strike and user_text == "":
                            user_text = "[AI_FIRST_STRIKE]"
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
                                logger.warning(f"ASR(stream) 异常（静默处理，用户可能没说话）: {e}")
                                user_text, user_emotion = "", "neutral"
                                asr_stream_task = asr_stream_queue = None
                                audio_buffer.clear()
                                # 静默处理，不发送 asr_error 事件，让后续流程正常处理空文本
                            asr_stream_task = asr_stream_queue = None
                            audio_buffer.clear()
                        else:
                            try:
                                user_text, user_emotion = await run_volcengine_wss_asr(audio_buffer, CONFIG)
                            except Exception as e:
                                logger.error(f"ASR(wss) 服务端错误: {e}", exc_info=True)
                                await safe_send_ws(websocket, ws_lock, {
                                    "event": "asr_error",
                                    "code": "WSS_FAILED",
                                    "message": str(e)
                                })
                                await _send_recovery_tts(websocket, ws_lock, current_user.settings if current_user else None)
                                audio_buffer.clear()
                                _latency_log(lat, "02_asr_done", user_chars=0, streaming_asr=False)
                                continue
                            audio_buffer.clear()
                            if asr_stream_task is not None or asr_stream_queue is not None:
                                await _abort_streaming_asr()
                        _latency_log(lat, "02_asr_done", user_chars=len(user_text or ""), streaming_asr=used_streaming_asr)

                    if not user_text:
                        _latency_log(lat, "02b_empty_after_asr_skip_rest")
                        if not is_test_mode:
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                        continue

                    # 【阶段四新增】AI 先手：跳过 Director 评估，直接让 LLM 开口
                    if is_ai_first_strike:
                        _latency_log(lat, "02c_ai_first_strike_skip_director")
                        # 不添加到 session_transcript，避免污染对话历史
                    else:
                        session_transcript.append({"role": "user", "text": user_text})

                        # 阶段二：微场景轮数递增
                        micro_mode = current_task_packet.has_constraints() if current_task_packet else False
                        if micro_mode:
                            session_ctx["turn_count_in_scenario"] = session_ctx.get("turn_count_in_scenario", 0) + 1
                            logger.info(
                                f"[Turn {session_ctx['turn_count_in_scenario']}] "
                                f"(scenario: {current_task_packet.current_scenario.scenario_code if current_task_packet and current_task_packet.current_scenario else 'N/A'}) "
                                f"User: {user_text}"
                            )
                        else:
                            session_ctx["phase_turns"] += 1
                            logger.info(f"[Turn {session_ctx['phase_turns']}] ({session_ctx['phase']}) User: {user_text}")

                        # 控制 session_transcript 长度
                        if len(session_transcript) > MAX_TRANSCRIPT_LENGTH:
                            session_transcript[:] = session_transcript[-MAX_TRANSCRIPT_LENGTH:]
                            logger.info(f"[TRANSCRIPT] Trimmed to last {MAX_TRANSCRIPT_LENGTH} entries")
                    # 【阶段四新增】AI 先手跳过评估，micro_mode 变量在下面仍然可用

                    # 2. 状态机评估 (利用上一轮副LLM写下的 llm_wants_to_advance / director 信号)
                    # 【阶段四修正】AI 先手时跳过 Director 评估
                    if current_user and not is_ai_first_strike:
                        with get_db() as db:
                            await evaluate_and_check_progress(
                                db, current_user.id, topic_id_for_progress, user_text,
                                session_ctx, websocket, ws_lock,
                                task_packet=current_task_packet,
                            )
                            fresh_topic = db.query(Topic).filter(Topic.id == topic_id_for_progress).first()

                            # 构建 Director 双轨信号（阶段二）
                            director_signal = None
                            if micro_mode and session_ctx.get("director_scenario_completed"):
                                director_signal = {
                                    "scenario_completed": True,
                                    "constraints_hit": session_ctx.get("director_constraints_hit", False),
                                    "intent_achieved": session_ctx.get("director_intent_achieved", False),
                                }

                            # 状态流转执行
                            did_transition = advance_state_machine(
                                session_ctx, db, fresh_topic,
                                current_task_packet, director_signal=director_signal,
                            )

                            if did_transition:
                                # ================================================================
                                # 微场景通关处理（阶段三）：前端同步 + AI 先手 + 无缝流转
                                # ================================================================
                                if micro_mode and session_ctx.get("scenario_completed"):
                                    next_sc = session_ctx.get("next_scenario")

                                    # ── 修复1：前端状态同步 ──────────────────────────────────
                                    # 发送完整的流转元数据，让前端 UI 立即切换显示
                                    if next_sc and next_sc.get("scenario_id"):
                                        next_scenario_id = next_sc["scenario_id"]

                                        # 循环检测：防止进入已访问过的场景
                                        visited = session_ctx.get("visited_scenarios")
                                        if isinstance(visited, set) and next_scenario_id in visited:
                                            logger.warning(
                                                f"[SCENARIO] 检测到循环访问: scenario_id={next_scenario_id} "
                                                f"已在本话题中访问过，阻止循环流转。"
                                            )
                                            session_ctx["next_scenario"] = None
                                            session_ctx["_cycle_detected_alert"] = True
                                            # 不执行流转，进入 dead-end 处理
                                        else:
                                            # 正常流转
                                            new_task_packet = await run_in_threadpool(
                                                session_planner.build_task_packet_for_next_scenario,
                                                current_user.id,
                                                next_scenario_id,
                                            )
                                        topic_id_for_progress = new_task_packet.topic_id

                                        # 计算过关奖励分
                                        reward_delta = session_ctx.get("task_score", 0.0)

                                        # 发送 scenario_transition 事件（含新场景元数据）
                                        await safe_send_ws(websocket, ws_lock, {
                                            "event": "scenario_transition",
                                            "previous_scenario_code": (
                                                current_task_packet.current_scenario.scenario_code
                                                if current_task_packet and current_task_packet.current_scenario
                                                else ""
                                            ),
                                            "new_scenario_code": new_task_packet.current_scenario.scenario_code if new_task_packet.current_scenario else "",
                                            "new_scenario_name": new_task_packet.current_scenario.scenario_name if new_task_packet.current_scenario else "",
                                            "new_intent": new_task_packet.current_intent or "",
                                            "progress_delta": reward_delta,
                                            "next_topic_suggestion": "",
                                        })

                                        # 重置状态，准备新场景
                                        current_task_packet = new_task_packet
                                        session_id = str(uuid.uuid4())
                                        session_ctx.pop("_constraint_hits", None)
                                        # 【修复】不要清空 session_transcript！AI 需要记忆之前的对话历史

                                        # 循环检测：记录已访问场景
                                        visited = session_ctx.get("visited_scenarios")
                                        if isinstance(visited, set):
                                            visited.add(next_scenario_id)

                                        mastery_snapshot = await run_in_threadpool(
                                            _take_mastery_snapshot, current_user.id, current_task_packet
                                        )

                                        logger.info(
                                            f"[SCENARIO] Transitioned to: "
                                            f"'{current_task_packet.current_scenario.scenario_code}' "
                                            f"(constraints={len(current_task_packet.constraints)})"
                                        )

                                        # ── 修复2：AI 无缝先手开场 ─────────────────────────
                                        # 注入控制 token：空 user_text + 标记位
                                        # 主 LLM 将直接根据 Turn 1 Prompt 开始新场景的自然表演
                                        session_ctx["_scenario_transition_pending"] = True

                                    else:
                                        # ── 修复3：图谱尽头，优雅退出 ────────────────────
                                        # 没有后续场景：强行推入 WRAP_UP，让 AI 自然道别
                                        logger.info(
                                            "[SCENARIO] Dead end: no next_scenario. "
                                            "Pushing to WRAP_UP for graceful exit."
                                        )
                                        session_ctx["phase"] = "WRAP_UP"
                                        session_ctx["phase_turns"] = 0
                                        session_ctx["_dead_end_exit"] = True

                                        # 给任务分保底奖励（用户已通关，应该满分）
                                        session_ctx["task_score"] = 60.0
                                        reward_delta = session_ctx.get("task_score", 0.0)

                                        await safe_send_ws(websocket, ws_lock, {
                                            "event": "scenario_transition",
                                            "previous_scenario_code": (
                                                current_task_packet.current_scenario.scenario_code
                                                if current_task_packet and current_task_packet.current_scenario
                                                else ""
                                            ),
                                            "new_scenario_code": "__WRAP_UP__",
                                            "new_scenario_name": "Session Complete",
                                            "new_intent": "Wrap up and say goodbye naturally.",
                                            "progress_delta": reward_delta,
                                            "next_topic_suggestion": "",
                                        })

                                        # 保存本次话题学习记录
                                        asyncio.create_task(_renew_task_packet(
                                            current_user.id, current_task_packet,
                                            session_ctx.get("_constraint_hits", set()).copy(),
                                            session_transcript.copy(), llm_client, session_id, websocket, ws_lock,
                                        ))

                                        # 重置，为下一局做准备
                                        current_task_packet = await run_in_threadpool(
                                            session_planner.build_task_packet, current_user.id
                                        )
                                        topic_id_for_progress = (
                                            current_task_packet.topic_id
                                            if current_task_packet.topic_id
                                            else DEFAULT_TOPIC_ID
                                        )
                                        session_id = str(uuid.uuid4())
                                        session_ctx.pop("_constraint_hits", None)
                                        session_transcript.clear()
                                        mastery_snapshot = await run_in_threadpool(
                                            _take_mastery_snapshot, current_user.id, current_task_packet
                                        )
                                        # dead-end 场景不触发 AI 先手，等用户下次主动开口
                                        session_ctx.pop("_scenario_transition_pending", None)

                                    # 清理信号
                                    session_ctx["scenario_completed"] = False
                                    session_ctx.pop("director_scenario_completed", None)
                                    session_ctx.pop("director_constraints_hit", None)
                                    session_ctx.pop("director_intent_achieved", None)
                                    session_ctx.pop("_constraint_hits", None)
                                    session_ctx.pop("next_scenario", None)
                                    session_ctx.pop("_dead_end_exit", None)

                            static_sys, dynamic_turn = build_prompts(
                                current_user, is_flipped, session_ctx, current_task_packet
                            )
                            chat_history[0]["content"] = static_sys

                    _latency_log(lat, "03_l1_and_prompt_ready", logged_in=bool(current_user))

                    # ── 修复2：AI 无缝先手开场 ────────────────────────────────────────────
                    # 当微场景流转完成且有下一场景时，注入控制 token 触发 AI 主动开场
                    # AI 会根据 Turn 1 Prompt 直接开始新情境的表演，而非等待用户开口
                    if session_ctx.pop("_scenario_transition_pending", False):
                        control_token = (
                            "[SCENARIO TRANSITION] "
                            "The previous micro-scenario has been completed. "
                            "Please start the new scenario naturally and set the scene."
                        )
                        chat_history.append({"role": "user", "content": control_token})
                        logger.info(
                            f"[SCENARIO] AI first-strike triggered: "
                            f"'{current_task_packet.current_scenario.scenario_code}' begins now"
                        )
                    else:
                        # 正常流程：追加用户消息
                        chat_history.append({"role": "user", "content": f"[{user_emotion} tone] {user_text}"})

                    messages_to_send = chat_history.copy()
                    messages_to_send.append({"role": "system", "content": dynamic_turn})

                    # 3. 准备并发 TTS 任务
                    # 【阶段四修正】AI 先手时也需要 TTS：做 TTS 的条件 = 非测试模式 或 AI先手
                    if not is_test_mode or is_ai_first_strike:
                        tts_queue = asyncio.Queue()
                        async def tts_consumer(queue: asyncio.Queue):
                            try:
                                tts_engine_name = (
                                    current_user.settings.get("tts_engine")
                                    if current_user and current_user.settings
                                    else None
                                )
                                tts_voice = (
                                    current_user.settings.get("tts_voice")
                                    if current_user and current_user.settings
                                    else None
                                )
                                provider = get_tts_factory().get_provider(tts_engine_name)
                                if provider:
                                    logger.info(f"[TTS] Using engine={tts_engine_name}, voice={tts_voice}")
                                    await provider.synthesize_queue(
                                        websocket, ws_lock, CONFIG, queue, latency_hooks=lat, voice_id=tts_voice
                                    )
                                else:
                                    logger.error(f"[TTS] Provider not found for engine: {tts_engine_name}")
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

                        # 从 debug 参数获取模型（开发者模式），否则走自动容灾
                        debug_info = data.get("debug", {}) if isinstance(data, dict) else {}
                        debug_model = debug_info.get("model")

                        # 🚨 此处删除了 Tools 参数，完全让其自由对话
                        # 使用 llm_router 支持开发者模式和自动容灾
                        response = await llm_router.chat(
                            model=debug_model,
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
                                        # 【阶段四修正】AI 先手时也需要 TTS
                                        if (not is_test_mode or is_ai_first_strike) and tts_queue:
                                            logger.info(f"[TRACK_LLM] 截断送入TTS队列 len={len(chunk_text)} text={chunk_text[:40]}")
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
                    # 【阶段四修正】AI 先手时也需要 TTS：做 TTS 的条件 = 非测试模式 或 AI先手
                    if (not is_test_mode or is_ai_first_strike) and tts_queue:
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

                    # 控制 session_transcript 长度
                    if len(session_transcript) > MAX_TRANSCRIPT_LENGTH:
                        session_transcript[:] = session_transcript[-MAX_TRANSCRIPT_LENGTH:]
                        logger.info(f"[TRANSCRIPT] Trimmed to last {MAX_TRANSCRIPT_LENGTH} entries")

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
    pass