"""
WsSessionHandler — WebSocket 会话处理器。

将 server.py 中 ~600 行的 WebSocket 主函数拆分为独立类，
每个实例封装一个连接的所有状态，彻底消除闭包捕获的隐式状态。
"""
from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from database import (
    User,
    Topic,
    UserProgress,
)
from core.audio.asr import run_volcengine_wss_asr, run_volc_streaming_asr_worker
from core.audio.tts import run_tts_to_ws, run_tts_turn_reused_from_queue
from core.audio.protocol import ASR_STREAM_START_BYTES
from core.dialogue_engine import (
    build_dynamic_prompt,
    advance_state_machine,
    evaluate_and_check_progress,
    async_fetch_and_send_teaching,
)
from domain.entities.session_context import SessionContext
from application.services.report_builder import build_preliminary_report
from application.services.session_planner import (
    build_task_packet,
    build_task_packet_for_topic,
    add_topic_to_store,
    save_learning_session,
)
from application.services import assessment_engine
import infrastructure.topic_generator as topic_generator

logger = logging.getLogger("EnglishCoach")

# ================= 业务常量 =================

LLM_MAX_TOKENS = 80
DEFAULT_TOPIC_ID = 999
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_BUFFER_CHARS = 38
FIRST_TTS_EARLY_FLUSH_CHARS = 22
MAX_CONTEXT_TOKENS = 2000


# ================= Latency 辅助 =================

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


# ================= 辅助函数（server.py 中的工具函数复制，handler 内使用） =================

def _get_mastery_snapshot(user_id: str, task_packet) -> dict:
    """快照所有目标节点的掌握度"""
    if not task_packet or not user_id:
        return {}
    all_nodes = task_packet.target_nodes + task_packet.review_nodes
    node_ids = [n["id"] for n in all_nodes if n.get("id")]
    if not node_ids:
        return {}
    from server import get_db
    with get_db() as db:
        progresses = db.query(UserProgress).filter(
            UserProgress.user_id == user_id,
            UserProgress.node_id.in_(node_ids),
        ).all()
        return {p.node_id: p.mastery_score for p in progresses}


def _renew_task_packet_async(
    user_id: str,
    old_packet,
    nodes_hit: set,
    transcript: list,
    openai_client,
    session_id: str,
    websocket,
    ws_lock,
):
    """WRAP_UP 完成后异步执行的后处理任务（fire-and-forget）"""
    import application.services.session_planner as sp
    if not old_packet or not user_id:
        return
    try:
        sp.save_learning_session(user_id, old_packet, nodes_hit)
    except Exception as e:
        logger.error(f"[WRAP_UP] save_learning_session error: {e}")
    try:
        assessment_engine.run_l2_assessment(
            transcript, old_packet, user_id, session_id,
            openai_client, websocket, ws_lock,
        )
    except Exception as e:
        logger.error(f"[WRAP_UP] L2 assessment error (non-blocking): {e}")


# ================= WebSocket 会话处理器 =================

class WsSessionHandler:
    """
    封装单个 WebSocket 连接的所有状态和逻辑。

    用法::

        @app.websocket("/ws/coach")
        async def websocket_endpoint(websocket: WebSocket, user_id: Optional[str] = None):
            await websocket.accept()
            await WsSessionHandler(websocket, user_id, config).run()
    """

    def __init__(self, websocket: WebSocket, user_id: Optional[str], config: dict):
        from server import get_db, init_or_get_user

        self._ws = websocket
        self._user_id = user_id
        self._config = config
        self._ws_lock = asyncio.Lock()

        # 音频状态
        self._audio_buffer = bytearray()
        self._asr_stream_queue: Optional[asyncio.Queue] = None
        self._asr_stream_task: Optional[asyncio.Task] = None
        self._asr_partial_last_mono = 0.0
        self._consumer_task: Optional[asyncio.Task] = None
        self._tts_queue: Optional[asyncio.Queue] = None

        # 会话状态
        self._is_flipped = False
        self._session_hits: set = set()
        self._session_ctx = SessionContext()
        self._current_task_packet = None
        self._session_transcript: list[dict] = []
        self._session_id = str(uuid.uuid4())
        self._mastery_snapshot: dict = {}
        self._chat_history: list[dict] = []

        # 用户对象
        self._current_user = None
        self._db = get_db

    # ── 公开入口 ─────────────────────────────────────────────────────────

    async def run(self):
        try:
            await self._init_session()
            await self._run_message_loop()
        except WebSocketDisconnect:
            logger.info("WS 连接已断开 (WebSocketDisconnect)。")
        except Exception as e:
            logger.error(f"WsSessionHandler 意外异常: {e}", exc_info=True)
        finally:
            await self._cleanup()

    # ── 初始化 ───────────────────────────────────────────────────────────

    async def _init_session(self):
        from server import get_db, init_or_get_user

        self._current_user = await run_in_threadpool(init_or_get_user, self._user_id)

        if self._current_user:
            self._current_task_packet = await run_in_threadpool(
                build_task_packet, self._current_user.id
            )
            self._mastery_snapshot = _get_mastery_snapshot(
                self._current_user.id, self._current_task_packet
            )

        topic_id = (
            self._current_task_packet.topic_id
            if self._current_task_packet and self._current_task_packet.topic_id
            else DEFAULT_TOPIC_ID
        )

        initial_prompt = build_dynamic_prompt(
            self._current_user, self._is_flipped,
            self._session_ctx, self._current_task_packet, self._session_hits
        )
        self._chat_history = [{"role": "system", "content": initial_prompt}]

        if self._current_user:
            with get_db() as db:
                await evaluate_and_check_progress(
                    db, self._current_user.id, topic_id, "",
                    self._session_hits, self._session_ctx,
                    self._ws, self._ws_lock,
                    task_packet=self._current_task_packet,
                )
                self._chat_history[0]["content"] = build_dynamic_prompt(
                    self._current_user, self._is_flipped,
                    self._session_ctx, self._current_task_packet, self._session_hits
                )

    async def _run_message_loop(self):
        while True:
            message = await self._ws.receive()

            if message.get("type") == "websocket.disconnect":
                logger.info(f"用户 {self._user_id or '未知'} 正常断开。")
                break

            if "bytes" in message:
                await self._handle_audio_frame(message["bytes"])
                continue

            if "text" in message:
                await self._handle_text_command(message["text"])

    # ── 音频处理 ─────────────────────────────────────────────────────────

    async def _handle_audio_frame(self, audio_bytes: bytes):
        self._audio_buffer.extend(audio_bytes)

        if len(self._audio_buffer) > MAX_AUDIO_BYTES:
            logger.warning("音频缓冲区超限，强制清空。")
            await self._abort_streaming_asr()
            self._audio_buffer.clear()
            await self._send_ws({"event": "error", "message": "Audio buffer overflow"})
            return

        if self._asr_stream_task is not None and self._asr_stream_task.done():
            try:
                exc = self._asr_stream_task.exception()
                if exc:
                    logger.warning("流式 ASR 任务异常结束: %s", exc)
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass
            self._asr_stream_task = None
            self._asr_stream_queue = None

        if self._asr_stream_task is None and len(self._audio_buffer) >= ASR_STREAM_START_BYTES:
            self._asr_stream_queue = asyncio.Queue(maxsize=0)
            self._asr_stream_task = asyncio.create_task(
                run_volc_streaming_asr_worker(
                    self._asr_stream_queue, self._config,
                    on_partial=self._emit_asr_partial
                )
            )
            await self._asr_stream_queue.put(bytes(self._audio_buffer))
        elif (self._asr_stream_queue is not None
              and self._asr_stream_task is not None
              and not self._asr_stream_task.done()):
            await self._asr_stream_queue.put(audio_bytes)

    async def _emit_asr_partial(self, txt: str):
        if not txt:
            return
        now = time.monotonic()
        if now - self._asr_partial_last_mono < 0.22:
            return
        self._asr_partial_last_mono = now
        await self._send_ws({"event": "asr_partial", "text": txt})

    async def _abort_streaming_asr(self):
        if self._asr_stream_task and not self._asr_stream_task.done():
            self._asr_stream_task.cancel()
            try:
                await self._asr_stream_task
            except (asyncio.CancelledError, Exception):
                pass
        self._asr_stream_task = None
        self._asr_stream_queue = None

    # ── 文本指令处理 ───────────────────────────────────────────────────────

    async def _handle_text_command(self, raw_text: str):
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("收到非法的 JSON 数据，已忽略。")
            await self._send_ws({"event": "error", "message": "Invalid JSON format"})
            return

        action = data.get("action")
        payload_user_id = data.get("user_id")

        if payload_user_id and not self._current_user:
            from server import init_or_get_user
            self._current_user = await run_in_threadpool(init_or_get_user, payload_user_id)

        if action == "client_latency_report":
            _e2e_log_client_report(data)
            return

        if action == "ping":
            await self._handle_ping(payload_user_id)
            return

        if action == "swap_role":
            await self._handle_swap_role()
            return

        if action == "update_lms_settings":
            await self._handle_update_lms_settings(data)
            return

        if action == "update_politeness":
            await self._handle_update_politeness(data)
            return

        if action == "request_topic":
            await self._handle_request_topic(data)
            return

        if action == "request_tts":
            await self._handle_request_tts(data)
            return

        if action == "cancel_tts":
            await self._handle_cancel_tts()
            return

        if action in ("user_finish_speaking", "test_text_input"):
            await self._handle_user_input(action, data)

    # ── Action Handlers ────────────────────────────────────────────────────

    async def _handle_ping(self, payload_user_id: str):
        from server import _fetch_user_settings_dict, init_or_get_user

        ws_payload = {"event": "warmup_success"}
        uid = payload_user_id or (self._current_user.id if self._current_user else None)
        if uid:
            if not self._current_user:
                self._current_user = await run_in_threadpool(init_or_get_user, uid)
            st = await run_in_threadpool(_fetch_user_settings_dict, uid)
            if st:
                ws_payload["user_settings"] = st
                logger.info("[WS] warmup_success + user_settings for %s", uid[:8])
        await self._send_ws(ws_payload)

    async def _handle_swap_role(self):
        self._is_flipped = not self._is_flipped
        self._session_ctx["phase"], self._session_ctx["phase_turns"] = "ICE_BREAKING", 0
        self._rebuild_prompt()
        await self._send_ws({"event": "role_swapped", "is_flipped": self._is_flipped})

    async def _handle_update_lms_settings(self, data: dict):
        from server import _update_lms_settings, init_or_get_user

        if not self._current_user:
            return
        depth = data.get("depth_preference")
        appetite = data.get("new_topic_appetite")
        learner_level = data.get("learner_level")
        await run_in_threadpool(
            _update_lms_settings, self._current_user.id, depth, appetite, learner_level
        )
        self._current_user = await run_in_threadpool(init_or_get_user, self._current_user.id)
        self._rebuild_prompt()

    async def _handle_update_politeness(self, data: dict):
        from server import update_user_politeness, init_or_get_user

        if not self._current_user:
            return
        level = data.get("level", 1)
        await run_in_threadpool(update_user_politeness, self._current_user.id, level)
        self._current_user = await run_in_threadpool(init_or_get_user, self._current_user.id)
        self._rebuild_prompt()

    async def _handle_request_topic(self, data: dict):
        description = data.get("description", "").strip()
        if not description or not self._current_user:
            return

        await self._send_ws({
            "event": "topic_generating",
            "message": f"Finding the best match for: {description}",
            "message_zh": f"正在为你匹配练习场景：{description}",
        })

        try:
            from server import client as openai_client
            new_topic = await topic_generator.get_or_generate_topic(description, openai_client)
            await run_in_threadpool(add_topic_to_store, new_topic)
            self._current_task_packet = await run_in_threadpool(
                build_task_packet_for_topic, self._current_user.id, new_topic.id
            )
            topic_id = self._current_task_packet.topic_id
            self._session_id = str(uuid.uuid4())
            self._session_hits = set()
            self._session_transcript = []
            self._session_ctx = SessionContext()
            self._mastery_snapshot = _get_mastery_snapshot(
                self._current_user.id, self._current_task_packet
            )
            self._chat_history = [{
                "role": "system",
                "content": build_dynamic_prompt(
                    self._current_user, self._is_flipped,
                    self._session_ctx, self._current_task_packet, self._session_hits
                ),
            }]
            await self._send_ws({
                "event": "topic_changed",
                "topic_id": topic_id,
                "topic_title": self._current_task_packet.topic_title,
                "topic_title_zh": getattr(self._current_task_packet, "topic_title_zh", None),
                "role_name": self._current_task_packet.role_name,
                "depth_tier": self._current_task_packet.depth_tier,
                "session_id": self._session_id,
            })
            logger.info(f"[TopicRequest] Switched to '{self._current_task_packet.topic_title}'")

        except Exception as e:
            logger.error(f"[TopicRequest] Failed: {e}", exc_info=True)
            await self._send_ws({
                "event": "error",
                "code": "TOPIC_GENERATION_FAILED",
                "message": "Could not generate topic. Please try again.",
                "message_zh": "暂时无法生成话题，请稍后再试。",
            })

    async def _handle_request_tts(self, data: dict):
        text_to_speak = data.get("text", "")
        if not text_to_speak:
            return
        try:
            await run_tts_to_ws(text_to_speak, self._ws, self._ws_lock, self._config)
        except Exception as e:
            logger.error(f"主动点读 TTS 异常: {e}")
        finally:
            await self._send_ws({"event": "tts_finished"})

    async def _handle_cancel_tts(self):
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await asyncio.wait_for(self._consumer_task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._consumer_task = None

        if self._tts_queue:
            while not self._tts_queue.empty():
                try:
                    self._tts_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._tts_queue = None

        await self._abort_streaming_asr()
        self._audio_buffer.clear()
        logger.info("用户打断 TTS，已取消合成任务并清空缓冲区。")

    # ── 核心对话处理 ─────────────────────────────────────────────────────

    async def _handle_user_input(self, action: str, data: dict):
        from server import get_db, client as openai_client, TEACHING_CONFIG

        is_test_mode = (action == "test_text_input")
        self._asr_partial_last_mono = 0.0
        turn_id = _coerce_turn_trace_id(data.get("trace_id"))
        wall_recv_ms = int(time.time() * 1000)
        csw = data.get("client_submit_wall_ms")
        client_wall_ok = isinstance(csw, int) and csw > 0

        lat: dict = {"turn_id": turn_id, "t0": time.perf_counter()}
        if client_wall_ok:
            lat["client_submit_wall_ms"] = int(csw)

        _latency_log(lat, "01_turn_accepted", action=action)

        # ── 1. 语音转文本 ─────────────────────────────────────────────
        if is_test_mode:
            user_text, user_emotion = data.get("text", ""), "neutral"
            _latency_log(lat, "02_skip_asr_test_text")
        else:
            used_streaming_asr = False
            if (self._asr_stream_task is not None
                    and not self._asr_stream_task.done()
                    and self._asr_stream_queue is not None):
                used_streaming_asr = True
                _latency_log(lat, "02_asr_stream_finish")
                await self._asr_stream_queue.put(None)
                try:
                    user_text, user_emotion = await self._asr_stream_task
                except asyncio.CancelledError:
                    user_text, user_emotion = "", "neutral"
                except Exception as e:
                    logger.warning("流式 ASR 收束失败，回退整包 ASR: %s", e)
                    self._asr_stream_task = None
                    self._asr_stream_queue = None
                    user_text, user_emotion = await run_volcengine_wss_asr(
                        self._audio_buffer, self._config
                    )
                self._asr_stream_task = None
                self._asr_stream_queue = None
                self._audio_buffer.clear()
            else:
                user_text, user_emotion = await run_volcengine_wss_asr(
                    self._audio_buffer, self._config
                )
                self._audio_buffer.clear()
                if self._asr_stream_task is not None or self._asr_stream_queue is not None:
                    await self._abort_streaming_asr()

            _latency_log(lat, "02_asr_done", user_chars=len(user_text or ""), streaming_asr=used_streaming_asr)

        if not user_text:
            _latency_log(lat, "02b_empty_after_asr_skip_rest")
            if not is_test_mode:
                await self._send_ws({"event": "tts_finished"})
            return

        self._session_ctx["phase_turns"] += 1
        self._session_transcript.append({"role": "user", "text": user_text})
        logger.info(f"[Turn {self._session_ctx['phase_turns']}] ({self._session_ctx['phase']}) User: {user_text}")

        # ── 2. 状态机评估 ─────────────────────────────────────────────
        topic_id = (
            self._current_task_packet.topic_id
            if self._current_task_packet and self._current_task_packet.topic_id
            else DEFAULT_TOPIC_ID
        )

        if self._current_user:
            with get_db() as db:
                await evaluate_and_check_progress(
                    db, self._current_user.id, topic_id, user_text,
                    self._session_hits, self._session_ctx,
                    self._ws, self._ws_lock,
                    task_packet=self._current_task_packet,
                )

                fresh_topic = db.query(Topic).filter(Topic.id == topic_id).first()
                did_transition = advance_state_machine(
                    self._session_ctx, db, fresh_topic,
                    self._session_hits, self._current_task_packet
                )

                if did_transition:
                    await self._handle_phase_transition(db)

        _latency_log(lat, "03_l1_and_prompt_ready", logged_in=bool(self._current_user))

        self._chat_history.append({"role": "user", "content": f"[{user_emotion} tone] {user_text}"})

        # ── 3. 准备并发 TTS ───────────────────────────────────────────
        if not is_test_mode:
            self._tts_queue = asyncio.Queue()

            async def tts_consumer(queue: asyncio.Queue):
                try:
                    await run_tts_turn_reused_from_queue(
                        self._ws, self._ws_lock, self._config, queue,
                        latency_hooks=lat,
                    )
                except Exception:
                    logger.exception("TTS 流式播放期间发生异常")
                await self._send_ws({"event": "tts_finished"})
                _latency_log(lat, "10_ws_tts_finished_event_sent")

            if self._consumer_task and not self._consumer_task.done():
                self._consumer_task.cancel()
            self._consumer_task = asyncio.create_task(tts_consumer(self._tts_queue))

        # ── 4. LLM 流式对话 ─────────────────────────────────────────
        raw_full_reply = ""
        sentence_buffer = ""
        punctuation_marks = ['.', '!', '?', ',', '。', '！', '？', '，', '\n']
        early_head_flush_used = False
        _lat_flags = {"llm_first": False, "tts_enqueue": False}

        async def _stream_llm_to_queue():
            nonlocal raw_full_reply, sentence_buffer, early_head_flush_used
            _latency_log(lat, "04_llm_api_request_start", chat_messages=len(self._chat_history))

            response = await openai_client.chat.completions.create(
                model="deepseek-chat",
                messages=self._chat_history,
                max_tokens=LLM_MAX_TOKENS,
                stream=True,
            )
            _latency_log(lat, "04b_llm_stream_iterable_ready")

            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    if not _lat_flags["llm_first"]:
                        _lat_flags["llm_first"] = True
                        _latency_log(lat, "05_llm_first_content_delta", preview=delta[:72])
                    raw_full_reply += delta

                    if not is_test_mode and self._tts_queue:
                        sentence_buffer += delta
                        want_flush = (
                            any(p in delta for p in punctuation_marks)
                            or len(sentence_buffer) > MAX_BUFFER_CHARS
                        )
                        if (not want_flush and not early_head_flush_used
                                and len(sentence_buffer) >= FIRST_TTS_EARLY_FLUSH_CHARS):
                            want_flush = True

                        if want_flush:
                            chunk_text = sentence_buffer.replace("[ADVANCE]", "").strip()
                            if chunk_text:
                                if not _lat_flags["tts_enqueue"]:
                                    _lat_flags["tts_enqueue"] = True
                                    _latency_log(lat, "06_tts_first_text_enqueued", preview=chunk_text[:72])
                                await self._tts_queue.put(chunk_text)
                                early_head_flush_used = True
                            sentence_buffer = ""

        async def _abort_tts():
            if self._consumer_task and not self._consumer_task.done():
                self._consumer_task.cancel()
                try:
                    await asyncio.wait_for(self._consumer_task, timeout=0.5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            self._consumer_task = None
            self._tts_queue = None

        try:
            await asyncio.wait_for(_stream_llm_to_queue(), timeout=20.0)
        except asyncio.TimeoutError:
            _latency_log(lat, "ERR_llm_wait_timeout")
            logger.error("LLM 响应超时（>20s），本轮中止。")
            await _abort_tts()
            await self._send_ws({
                "event": "error",
                "code": "LLM_TIMEOUT",
                "message": "The AI coach is taking a bit too long to think. Could you please try saying that again?",
            })
            return
        except Exception as e:
            _latency_log(lat, "ERR_llm_stream_exception", err_type=type(e).__name__)
            logger.error(f"LLM 调用异常: {e}", exc_info=True)
            await _abort_tts()
            await self._send_ws({
                "event": "error",
                "code": "LLM_ERROR",
                "message": "Something went wrong with the AI. Please try again in a moment.",
            })
            return

        # ── 5. 尾盘入列 ───────────────────────────────────────────────
        if not is_test_mode and self._tts_queue:
            final_chunk = sentence_buffer.replace("[ADVANCE]", "").strip()
            if final_chunk:
                if not _lat_flags["tts_enqueue"]:
                    _lat_flags["tts_enqueue"] = True
                    _latency_log(lat, "06_tts_first_text_enqueued", preview=final_chunk[:72], tail_flush=True)
                await self._tts_queue.put(final_chunk)
            await self._tts_queue.put(None)

        # ── 6. 指令清洗与记录 ────────────────────────────────────────
        self._session_ctx["llm_wants_to_advance"] = "[ADVANCE]" in raw_full_reply
        clean_full_reply = raw_full_reply.replace("[ADVANCE]", "").strip()
        self._chat_history.append({"role": "assistant", "content": clean_full_reply})
        self._chat_history = self._trim_chat_history(self._chat_history)
        self._session_transcript.append({"role": "assistant", "text": clean_full_reply})

        logger.info(f"AI: {clean_full_reply}")
        _latency_log(lat, "98_assistant_reply_ready", ai_chars=len(clean_full_reply))

        # ── 7. 后置辅导 ───────────────────────────────────────────────
        if is_test_mode:
            await self._send_ws({"event": "test_ai_reply", "text": clean_full_reply, "phase": self._session_ctx["phase"]})
        else:
            asyncio.create_task(async_fetch_and_send_teaching(
                user_text, clean_full_reply, TEACHING_CONFIG,
                openai_client, self._ws, self._ws_lock
            ))

    # ── 阶段转换处理 ─────────────────────────────────────────────────────

    async def _handle_phase_transition(self, db):
        from server import client as openai_client

        if self._session_ctx["phase"] == "ICE_BREAKING":
            # ── 1. 立刻推送初步报告 ───────────────────────────────
            preliminary = build_preliminary_report(
                task_packet=self._current_task_packet,
                session_id=self._session_id,
                session_hits=self._session_hits,
                session_ctx=self._session_ctx,
                mastery_snapshot=self._mastery_snapshot,
                db=db,
                user_id=self._current_user.id,
            )
            await self._send_ws(preliminary)

            # ── 2. 后台：保存记录 + 触发 L2 ──────────────────────
            asyncio.create_task(_renew_task_packet_async(
                self._current_user.id,
                self._current_task_packet,
                self._session_hits.copy(),
                self._session_transcript.copy(),
                openai_client,
                self._session_id,
                self._ws,
                self._ws_lock,
            ))

            # ── 3. 生成新 TaskPacket，重置会话状态 ───────────────
            self._current_task_packet = await run_in_threadpool(
                build_task_packet, self._current_user.id
            )
            self._session_id = str(uuid.uuid4())
            self._session_hits = set()
            self._session_transcript = []
            self._mastery_snapshot = _get_mastery_snapshot(
                self._current_user.id, self._current_task_packet
            )
            logger.info(f"[LMS] New session '{self._current_task_packet.topic_title}'")

        self._rebuild_prompt()

    # ── 辅助方法 ─────────────────────────────────────────────────────────

    def _rebuild_prompt(self):
        self._chat_history[0]["content"] = build_dynamic_prompt(
            self._current_user, self._is_flipped,
            self._session_ctx, self._current_task_packet, self._session_hits
        )

    def _trim_chat_history(self, history: list[dict]) -> list[dict]:
        if len(history) <= 1:
            return history
        system = history[0]
        turns = history[1:]
        while len(turns) > 2:
            total_chars = sum(len(m.get("content", "")) for m in [system] + turns)
            if total_chars // 4 <= MAX_CONTEXT_TOKENS:
                break
            turns = turns[2:]
        return [system] + turns

    async def _send_ws(self, payload: dict):
        await self._ws.send_text(json.dumps(payload))

    async def _cleanup(self):
        await self._abort_streaming_asr()
        self._audio_buffer.clear()
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await asyncio.wait_for(self._consumer_task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
