import os
import json
import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
import openai

from database import SessionLocal, User, Topic
from core.audio_service import run_volcengine_wss_asr, run_tts_to_ws
from core.dialogue_engine import build_dynamic_prompt, advance_state_machine, evaluate_and_check_progress, \
    async_fetch_and_send_teaching

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
    "VOICE": os.getenv("VOICE", "BV001_streaming")
}

TEACHING_CONFIG = {"enable_correction": False, "enable_translation": True, "enable_hints": True}
client = openai.AsyncOpenAI(api_key=CONFIG["DEEPSEEK_KEY"], base_url=CONFIG["DEEPSEEK_BASE"])

app = FastAPI()


# ================= 1. WebSocket 主入口 =================
@app.websocket("/ws/coach")
async def websocket_endpoint(websocket: WebSocket, user_id: str = None):
    await websocket.accept()
    logger.info(f"📱 客户端已连接: {websocket.client.host}")

    ws_lock = asyncio.Lock()
    audio_buffer = bytearray()

    is_flipped = False
    session_hits = set()
    chat_history = []

    # 🌟 初始化完整的段位与计分状态机
    session_ctx = {
        "loop_count": 1,
        "phase": "ICE_BREAKING",
        "phase_turns": 0,
        "active_targets": [],
        "current_event": "",
        "llm_wants_to_advance": False,
        "current_level": 1,                # 当前所处段位 (Lv)
        "chat_score": 0.0,                 # 本轮闲聊分 (上限40)
        "task_score": 0.0,                 # 本轮任务分 (上限60)
        "completed_rounds_in_level": 0     # 本段位已完成轮数 (满3轮升段)
    }

    db = SessionLocal()
    current_user = None
    current_topic = db.query(Topic).first()  

    try:
        topic_id_for_progress = current_topic.id if current_topic else 999
        
        if user_id:
            current_user = db.query(User).filter(User.id == user_id).first()
            if not current_user:
                current_user = User(id=user_id)
                db.add(current_user)
                db.commit()

        initial_prompt = build_dynamic_prompt(current_user, is_flipped, session_ctx)
        chat_history = [{"role": "system", "content": initial_prompt}]

        if current_user:
            await evaluate_and_check_progress(db, current_user.id, topic_id_for_progress, "", 
                                              session_hits, session_ctx, websocket, ws_lock)

        while True:
            message = await websocket.receive()
            
            # 🌟 关键修复点：如果底层告知连接已断开，必须直接跳出循环，防止后续提取 "text" 报错！
            if message.get("type") == "websocket.disconnect":
                logger.info(f"👋 用户 {user_id or '未知'} 正常断开了 WebSocket 连接。")
                break
                
            if "bytes" in message:
                audio_buffer.extend(message["bytes"])
            elif "text" in message:
                data = json.loads(message["text"])
                action = data.get("action")

                payload_user_id = data.get("user_id")
                if payload_user_id and not current_user:
                    current_user = db.query(User).filter(User.id == payload_user_id).first()
                    if not current_user:
                        current_user = User(id=payload_user_id)
                        db.add(current_user)
                        db.commit()

                if action == "swap_role":
                    is_flipped = not is_flipped
                    session_ctx["phase"], session_ctx["phase_turns"] = "ICE_BREAKING", 0
                    prompt = build_dynamic_prompt(current_user, is_flipped, session_ctx)
                    chat_history = [{"role": "system", "content": prompt}]
                    async with ws_lock: 
                        await websocket.send_text(json.dumps({"event": "role_swapped", "is_flipped": is_flipped}))
                    continue

                if action == "update_politeness":
                    if current_user:
                        current_user.politeness_level = data.get("level", 1)
                        db.commit()
                        chat_history[0]["content"] = build_dynamic_prompt(current_user, is_flipped, session_ctx)
                    continue

                if action == "ping":
                    async with ws_lock:
                        await websocket.send_text(json.dumps({"event": "warmup_success"}))

                elif action in ["user_finish_speaking", "test_text_input"]:
                    is_test_mode = (action == "test_text_input")

                    if is_test_mode:
                        user_text, user_emotion = data.get("text", ""), "neutral"
                    else:
                        user_text, user_emotion = await run_volcengine_wss_asr(audio_buffer, CONFIG)
                        audio_buffer.clear()

                    if not user_text:
                        if not is_test_mode:
                            async with ws_lock: 
                                await websocket.send_text(json.dumps({"event": "tts_finished"}))
                        continue

                    session_ctx["phase_turns"] += 1
                    logger.info(f"✅ 回合 {session_ctx['phase_turns']} ({session_ctx['phase']}) | 你说: {user_text}")

                    if current_user:
                        await evaluate_and_check_progress(db, current_user.id, topic_id_for_progress, user_text,
                                                          session_hits, session_ctx, websocket, ws_lock)

                        if current_topic and advance_state_machine(session_ctx, db, current_topic, session_hits):
                            chat_history[0]["content"] = build_dynamic_prompt(current_user, is_flipped, session_ctx)

                    chat_history.append({"role": "user", "content": f"[{user_emotion} tone] {user_text}"})

                    if not is_test_mode:
                        tts_queue = asyncio.Queue()

                        async def tts_consumer():
                            while True:
                                text_to_speak = await tts_queue.get()
                                if text_to_speak is None:
                                    tts_queue.task_done()
                                    break
                                try:
                                    await run_tts_to_ws(text_to_speak, websocket, ws_lock, CONFIG)
                                except:
                                    pass
                                finally:
                                    tts_queue.task_done()
                            async with ws_lock:
                                await websocket.send_text(json.dumps({"event": "tts_finished"}))

                        consumer_task = asyncio.create_task(tts_consumer())

                    response = await client.chat.completions.create(model="deepseek-chat", messages=chat_history,
                                                                    max_tokens=80, stream=True)

                    raw_full_reply = ""
                    sentence_buffer = ""
                    punctuation_marks = ['.', '!', '?', ',', '。', '！', '？', '，', '\n']

                    # 🌟 核心修复：添加 try-except 保护罩防止网络断流导致崩溃
                    try:
                        async for chunk in response:
                            if chunk.choices and chunk.choices[0].delta.content:
                                delta = chunk.choices[0].delta.content
                                raw_full_reply += delta

                                if not is_test_mode:
                                    sentence_buffer += delta
                                    if any(p in delta for p in punctuation_marks):
                                        chunk_text = sentence_buffer.replace("[ADVANCE]", "").strip()
                                        if chunk_text: await tts_queue.put(chunk_text)
                                        sentence_buffer = ""
                    except Exception as e:
                        logger.error(f"⚠️ 生成过程中遭遇网络断流: {e}")
                        error_msg = " [网络波动，信号中断...]"
                        raw_full_reply += error_msg
                        if not is_test_mode:
                            await tts_queue.put("Network interrupted.")

                    if not is_test_mode:
                        final_chunk = sentence_buffer.replace("[ADVANCE]", "").strip()
                        if final_chunk: await tts_queue.put(final_chunk)
                        await tts_queue.put(None)

                    if "[ADVANCE]" in raw_full_reply:
                        session_ctx["llm_wants_to_advance"] = True
                        logger.info("🧠 AI 发出了切阶段信号 [[ADVANCE]]！将在下一回合生效。")
                    else:
                        session_ctx["llm_wants_to_advance"] = False

                    clean_full_reply = raw_full_reply.replace("[ADVANCE]", "").strip()
                    chat_history.append({"role": "assistant", "content": clean_full_reply})
                    if len(chat_history) > 11: 
                        chat_history = [chat_history[0]] + chat_history[-10:]

                    logger.info(f"🤖 AI: {clean_full_reply}")

                    if is_test_mode:
                        async with ws_lock:
                            await websocket.send_text(json.dumps(
                                {"event": "test_ai_reply", "text": clean_full_reply, "phase": session_ctx["phase"]}))
                    else:
                        asyncio.create_task(
                            async_fetch_and_send_teaching(user_text, clean_full_reply, TEACHING_CONFIG, client,
                                                          websocket, ws_lock))

                elif action == "request_tts":
                    text_to_speak = data.get("text", "")
                    if text_to_speak:
                        await run_tts_to_ws(text_to_speak, websocket, ws_lock, CONFIG)
                        async with ws_lock: 
                            await websocket.send_text(json.dumps({"event": "tts_finished"}))

    except WebSocketDisconnect:
        logger.info(f"👋 用户正常断开了 WebSocket 连接 (WebSocketDisconnect 被捕获)。")
    except Exception as e:
        logger.error(f"💥 异常: {e}", exc_info=True)
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)