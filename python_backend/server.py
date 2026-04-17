import os
import json
import asyncio
import logging
from typing import Optional
from contextlib import contextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from dotenv import load_dotenv
import openai

from database import SessionLocal, User, Topic
from core.audio_service import run_volcengine_wss_asr, run_tts_to_ws
from core.dialogue_engine import build_dynamic_prompt, advance_state_machine, evaluate_and_check_progress, async_fetch_and_send_teaching
from domain.entities.session_context import SessionContext

# ================= 0. 初始化与配置 =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("EnglishCoach")

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
client = openai.AsyncOpenAI(api_key=CONFIG["DEEPSEEK_KEY"], base_url=CONFIG["DEEPSEEK_BASE"])

app = FastAPI()

# ================= 业务全局常量 =================
LLM_MAX_TOKENS = 80                # 限制每次模型输出的长度，保证响应速度
DEFAULT_TOPIC_ID = 999             # 兜底的话题ID
MAX_AUDIO_BYTES = 5 * 1024 * 1024  # 音频防爆限制：5MB
MAX_BUFFER_CHARS = 50              # 流式处理中，无标点字符超过此长度强制截断发送 TTS
# chat_history token 预算：4 chars ≈ 1 token（粗估），给模型上下文留足余量
MAX_CONTEXT_TOKENS = 2000          # 进入 LLM 前历史消息的 token 上限（含 system prompt）

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

def update_user_politeness(user_id: str, level: int):
    """线程安全的同步更新用户礼貌度"""
    if not user_id: 
        return
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.politeness_level = level
            db.commit()

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

    ws_lock = asyncio.Lock()
    audio_buffer = bytearray()
    consumer_task = None
    tts_queue = None

    is_flipped = False
    session_hits = set()
    session_ctx = SessionContext()  # 可序列化的会话状态，替代原裸 dict

    # 获取初始数据 (严格分离 Session)
    with get_db() as db:
        initial_topic = db.query(Topic).first()
        topic_id_for_progress = initial_topic.id if initial_topic else DEFAULT_TOPIC_ID
        
    current_user = await run_in_threadpool(init_or_get_user, user_id)
    initial_prompt = build_dynamic_prompt(current_user, is_flipped, session_ctx)
    chat_history = [{"role": "system", "content": initial_prompt}]

    if current_user:
        with get_db() as db:
            await evaluate_and_check_progress(db, current_user.id, topic_id_for_progress, "", 
                                              session_hits, session_ctx, websocket, ws_lock)

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
                    audio_buffer.clear()
                    await safe_send_ws(websocket, ws_lock, {"event": "error", "message": "Audio buffer overflow"})
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
                    await safe_send_ws(websocket, ws_lock, {"event": "warmup_success"})
                    continue

                if action == "swap_role":
                    is_flipped = not is_flipped
                    session_ctx["phase"], session_ctx["phase_turns"] = "ICE_BREAKING", 0
                    chat_history[0]["content"] = build_dynamic_prompt(current_user, is_flipped, session_ctx)
                    await safe_send_ws(websocket, ws_lock, {"event": "role_swapped", "is_flipped": is_flipped})
                    continue

                if action == "update_politeness":
                    if current_user:
                        level = data.get("level", 1)
                        await run_in_threadpool(update_user_politeness, current_user.id, level)
                        current_user.politeness_level = level  # 同步更新内存中的脱机对象属性
                        chat_history[0]["content"] = build_dynamic_prompt(current_user, is_flipped, session_ctx)
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
                    audio_buffer.clear()
                    logger.info("🛑 [打断] 用户打断 TTS，已取消合成任务并清空缓冲区。")
                    continue

                if action in ["user_finish_speaking", "test_text_input"]:
                    is_test_mode = (action == "test_text_input")

                    # 1. 语音转文本
                    if is_test_mode:
                        user_text, user_emotion = data.get("text", ""), "neutral"
                    else:
                        user_text, user_emotion = await run_volcengine_wss_asr(audio_buffer, CONFIG)
                        audio_buffer.clear()

                    if not user_text:
                        if not is_test_mode: 
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})
                        continue

                    session_ctx["phase_turns"] += 1
                    logger.info(f"✅ 回合 {session_ctx['phase_turns']} ({session_ctx['phase']}) | 你说: {user_text}")

                    # 2. 状态机评估 (开启新 DB 会话，避免跨会话 Detached 错误)
                    if current_user:
                        with get_db() as db:
                            await evaluate_and_check_progress(db, current_user.id, topic_id_for_progress, user_text,
                                                              session_hits, session_ctx, websocket, ws_lock)
                            
                            # 实时查询出 fresh_topic 供引擎推进状态，使用完即随 with 块回收
                            fresh_topic = db.query(Topic).filter(Topic.id == topic_id_for_progress).first()
                            if fresh_topic and advance_state_machine(session_ctx, db, fresh_topic, session_hits):
                                chat_history[0]["content"] = build_dynamic_prompt(current_user, is_flipped, session_ctx)

                    chat_history.append({"role": "user", "content": f"[{user_emotion} tone] {user_text}"})

                    # 3. 准备并发 TTS 任务
                    if not is_test_mode:
                        tts_queue = asyncio.Queue()
                        
                        async def tts_consumer(queue: asyncio.Queue):
                            while True:
                                text_to_speak = await queue.get()
                                if text_to_speak is None:
                                    queue.task_done()
                                    break
                                try:
                                    await run_tts_to_ws(text_to_speak, websocket, ws_lock, CONFIG)
                                except Exception:
                                    logger.exception("TTS 流式播放期间发生异常")
                                finally:
                                    queue.task_done()
                            await safe_send_ws(websocket, ws_lock, {"event": "tts_finished"})

                        # 终止上一轮仍未处理完的 TTS
                        if consumer_task and not consumer_task.done():
                            consumer_task.cancel()
                        consumer_task = asyncio.create_task(tts_consumer(tts_queue))

                    # 4. LLM 流式对话（包含首包连接 + 全量 chunk 消费，统一超时保护）
                    raw_full_reply = ""
                    sentence_buffer = ""
                    punctuation_marks = ['.', '!', '?', ',', '。', '！', '？', '，', '\n']

                    async def _stream_llm_to_queue():
                        """将 create + async for 封装为单协程，便于 wait_for 统一超时"""
                        nonlocal raw_full_reply, sentence_buffer
                        response = await client.chat.completions.create(
                            model="deepseek-chat", messages=chat_history,
                            max_tokens=LLM_MAX_TOKENS, stream=True
                        )
                        async for chunk in response:
                            if chunk.choices and chunk.choices[0].delta.content:
                                delta = chunk.choices[0].delta.content
                                raw_full_reply += delta
                                if not is_test_mode and tts_queue:
                                    sentence_buffer += delta
                                    if any(p in delta for p in punctuation_marks) or len(sentence_buffer) > MAX_BUFFER_CHARS:
                                        chunk_text = sentence_buffer.replace("[ADVANCE]", "").strip()
                                        if chunk_text:
                                            await tts_queue.put(chunk_text)
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
                        logger.error("⏱️ LLM 响应超时（>20s），本轮中止。")
                        await _abort_tts()
                        await safe_send_ws(websocket, ws_lock, {
                            "event": "error",
                            "code": "LLM_TIMEOUT",
                            "message": "The AI coach is taking a bit too long to think. Could you please try saying that again?"
                        })
                        continue  # 回到 while 循环等待下一帧，连接不断
                    except Exception as e:
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
                            await tts_queue.put(final_chunk)
                        await tts_queue.put(None)

                    # 5. 指令清洗与记录
                    session_ctx["llm_wants_to_advance"] = "[ADVANCE]" in raw_full_reply
                    if session_ctx["llm_wants_to_advance"]:
                        logger.info("🧠 AI 发出了切阶段信号！将在下一回合生效。")

                    clean_full_reply = raw_full_reply.replace("[ADVANCE]", "").strip()
                    chat_history.append({"role": "assistant", "content": clean_full_reply})
                    chat_history = trim_chat_history(chat_history)

                    logger.info(f"🤖 AI: {clean_full_reply}")

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