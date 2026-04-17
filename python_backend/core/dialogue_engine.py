"""
Dialogue Engine - English Coach
核心状态机、计分与对话引擎模块。

[Session Ctx 状态字典约定]:
- phase: 当前对话阶段 (ICE_BREAKING, CORE_TASK, EVENT_EXTENSION, WRAP_UP)
- loop_count: 完成「整局模拟」的次数（WRAP_UP 结算并回到破冰时 +1，非每轮用户发言）
- current_level: 用户当前所处的段位等级
- completed_rounds_in_level: 在当前段位下已完成的局数 (满 ROUNDS_PER_LEVEL 局晋级)
- new_targets / history_targets: 纯数据字典列表 [{"id": int, "node_text": str}], 彻底避免 ORM 跨会话 Detached 报错
- chat_score / task_score: 维系当前局的分数统计

[关于 ADVANCE 切阶段指令]:
- 所有的切阶段行为，均由系统 Prompt 约束 LLM 在回复末尾输出 "[ADVANCE]" 触发。
- 注意分工: 由 server.py 负责从大模型文本中提取该标记，并写入 session_ctx["llm_wants_to_advance"]。
- 本模块 (dialogue_engine) 负责在下一轮用户交互时，读取该标志位并推进状态机。
"""

import os
import json
import random
import logging
import re
import asyncio
from typing import Optional
from sqlalchemy.orm import Session
from fastapi import WebSocket

from database import User, Topic, TargetNode, UserProgress

logger = logging.getLogger("EnglishCoach")

# ================= 业务全局常量 =================

ROUNDS_PER_LEVEL = 3           # 每个段位需要完成的局数
MAX_TURNS_PER_PHASE = 25       # 兜底机制：每个阶段最大互动轮数，超时强制推进
# 绝对路径，确保从任意目录启动 uvicorn 均能正常加载场景配置
SCENES_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scenes.json")

EVENT_POOL = [
    "A colleague just texted the user asking them to add something specific to the order. Ask the user what their colleague wants.",
    "A different staff member enthusiastically interrupts to introduce a new seasonal item. Briefly act as this new character.",
    "There is a sudden mix-up with another customer's items. The user needs to describe exactly what they originally ordered to help you fix it.",
    "A system glitch means the user has to switch their payment method or split the bill. Ask them how they want to handle it."
]

# 闲聊得分的心流曲线 (非线性：前后慢，中间快。总和正好 40 分)
CHAT_SCORE_CURVE = [2.0, 3.0, 5.0, 8.0, 10.0, 6.0, 3.0, 2.0, 1.0]

# ================= 辅助工具 =================

def load_active_scene(file_path=SCENES_FILE_PATH):
    """动态读取场景，支持绝对路径与热更新，修改配置无需重启服务"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        active_id = data.get("current_active_id")
        scene_info = data["templates"].get(active_id)
        if not scene_info:
            raise ValueError(f"Scene ID '{active_id}' not found.")
        return scene_info
    except Exception as e:
        logger.warning(f"⚠️ Could not load scenes.json ({e}). Using default.")
        return {"scene": "McDonald's Ordering", "level": "Intermediate", "role": "McDonald's Cashier"}

def clean_llm_json(raw_text: str) -> dict:
    """工业级 LLM JSON 清洗工具，逆向解析防长篇废话与多代码块干扰"""
    raw_text = raw_text.strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # 使用 `{3}` 替代反引号防 Markdown 截断；获取代码块全量内容
    matches = re.findall(r'`{3}(?:json)?\s*([\s\S]*?)\s*`{3}', raw_text)
    if matches:
        # 逆向遍历：如果模型输出了多个代码块，真正的 JSON 通常在最后一个
        for match in reversed(matches):
            try:
                return json.loads(match.strip())
            except Exception:
                continue

    raise ValueError("LLM returned malformed JSON that could not be parsed.")

# ================= 核心提示词构建 =================

def build_dynamic_prompt(user: Optional[User], is_flipped: bool, session_ctx: dict):
    #毎回リアルタイムで最新のシーンを取得（ホットリロード対応）
    active_scene = load_active_scene()

    scene_name = active_scene.get("scene", "Daily Conversation")
    role_name = active_scene.get("role", "Assistant")
    user_level = active_scene.get("level", "Intermediate")

    role_desc = f"You are acting as: {role_name} in a {scene_name} setting." if not is_flipped else f"You are the CUSTOMER/USER. The user is acting as the {role_name}."

    politeness = {
        0: "Your personality: IMPATIENT and RUDE.",
        1: "Your personality: PROFESSIONAL and POLITE.",
        2: "Your personality: EXTREMELY POLITE and TALKATIVE."
    }
    personality_desc = politeness.get(user.politeness_level, politeness[1]) if user else politeness[1]

    phase = session_ctx.get("phase", "ICE_BREAKING")
    loop_count = session_ctx.get("loop_count", 1)

    prompt_blocks = []
    prompt_blocks.append(f"Learner Level: {user_level}")
    prompt_blocks.append(role_desc)
    prompt_blocks.append(personality_desc)
    prompt_blocks.append("")

    prompt_blocks.append("[UNIVERSAL COACHING RULES]")
    prompt_blocks.append("1. PROBE DEEPER: Never ask simple Yes/No questions. Use open-ended questions (What, How, Why) and ask for reasons or details to encourage longer, more complex user responses.")
    prompt_blocks.append("2. PULL-BACK MANDATE: If the user tries to end the simulation prematurely or goes completely off-topic (e.g., 'I'm leaving now'), gently re-engage them (e.g., 'Before we finish, let's quickly sort this out.') and immediately steer back to the current phase's goal.")
    prompt_blocks.append("3. CONCISE COACHING: Keep your replies conversational and encouraging. Aim for 2-3 sentences.")
    prompt_blocks.append("4. PLAIN TEXT: Reply in plain English text only. Spell out numbers/symbols.")
    prompt_blocks.append("5. NO SPOILERS: Never reveal the target words/phrases directly to the user.")
    prompt_blocks.append("")

    prompt_blocks.append("[PHASE CONTROL]")
    prompt_blocks.append(f"You are the director. We are in Round {loop_count}.")
    prompt_blocks.append(f"Current Phase: {phase}")
    prompt_blocks.append("HOW TO ADVANCE: When the current phase's objective is naturally satisfied based on the guidelines below, you MUST append EXACTLY \"[ADVANCE]\" to the VERY END of your reply to trigger the next stage.")
    prompt_blocks.append("CRITICAL: NEVER output just \"[ADVANCE]\" by itself! You MUST provide a natural, conversational reply first, and then put \"[ADVANCE]\" at the very end.")
    prompt_blocks.append("")

    prompt_blocks.append("[YOUR CURRENT DIRECTIVE]:")

    if phase == "ICE_BREAKING":
        prompt_blocks.append("PHASE 1: ICE BREAKING (Small Talk)")
        prompt_blocks.append(f"Goal: Build rapport. Ask 1-2 open-ended questions related to the {scene_name} to start a natural conversation. (e.g., 'What brings you here today?', 'Is this your first time trying this?').")
        prompt_blocks.append("STRICT FIREWALL: DO NOT process the main transaction or mission in this phase! Focus only on small talk.")
        prompt_blocks.append("ADVANCE RULE: The moment the user tries to initiate the core task or states their main purpose, acknowledge it briefly and IMMEDIATELY output [ADVANCE] at the end of your reply. Leave the actual task execution for Phase 2!")

    elif phase == "CORE_TASK":
        new_targets_list = [n.get('node_text') for n in session_ctx.get("new_targets", []) if n.get('node_text')]
        history_targets_list = [n.get('node_text') for n in session_ctx.get("history_targets", []) if n.get('node_text')]
        new_targets = ", ".join([f"'{t}'" for t in new_targets_list])
        history_targets = ", ".join([f"'{t}'" for t in history_targets_list])
        history_instruction = f"If natural, casually review these past words too: {history_targets}." if history_targets else ""

        prompt_blocks.append("PHASE 2: CORE TASK (Language Practice)")
        prompt_blocks.append(f"Goal: Guide the user through the main task of the {scene_name} while focusing on language practice.")
        prompt_blocks.append(f"DIRECTIVE: Naturally guide the user to say these NEW words: {new_targets}.")
        if history_instruction:
            prompt_blocks.append(history_instruction)
        prompt_blocks.append(f"COACHING MANDATE: Your primary role is a coach, not just a {role_name}. If the user uses a very simple phrase, gently model a more natural alternative in your response. (e.g., If user says 'I want burger,' you reply 'Excellent, one burger coming up. And would you like any toppings on that?').")
        prompt_blocks.append("ADVANCE RULE: Output [ADVANCE] ONLY when the core task is fully complete (e.g., order is confirmed, appointment is set, problem is initially diagnosed).")

    elif phase == "EVENT_EXTENSION":
        prompt_blocks.append("PHASE 3: EVENT EXTENSION (The Twist)")
        prompt_blocks.append("Goal: Introduce a LOGICAL and REASONABLE complication related to the Core Task. Your aim is to test the user's problem-solving and negotiation language skills.")
        prompt_blocks.append(f"SYSTEM OVERRIDE: Introduce this specific scenario: '{session_ctx.get('current_event', 'There is a small problem with your request.')}'")
        prompt_blocks.append("MAINTAIN COACH ROLE: Do not just act out the event. You are a coach observing a test. Guide the user by asking questions like 'Oh, that's unexpected. What do you think we should do?' or 'How would you explain the situation?'. Help them solve the problem.")
        prompt_blocks.append("ADVANCE RULE: Once the user has successfully navigated the complication using their English skills, provide a brief concluding statement for the event (e.g., 'Great, looks like we've sorted that out.') and then output [ADVANCE].")

    elif phase == "WRAP_UP":
        prompt_blocks.append("PHASE 4: WRAP UP (Conclusion)")
        prompt_blocks.append("Goal: Conclude the conversation naturally. Provide a single, brief sentence of positive feedback on how the user performed during the session (especially during the twist).")
        prompt_blocks.append("ADVANCE RULE: After giving feedback and saying your final goodbye, output [ADVANCE] to end the simulation. (e.g., 'You handled that unexpected problem really well. Have a great day! [ADVANCE]').")

    return "\n".join(prompt_blocks)

# ================= 核心计分与状态机 =================

async def evaluate_and_check_progress(db: Session, user_id: str, _topic_id: int, user_text: str, session_hits: set,
                                      session_ctx: dict, websocket: WebSocket, ws_lock: asyncio.Lock):
    """
    打分与进度评估模块。
    @param _topic_id: 当前保留作备用参数，供后续话题维度细粒度统计。
    """
    try:
        phase = session_ctx.get("phase", "ICE_BREAKING")
        user_text_lower = user_text.lower()

        # --- 计分模块 1：闲聊分 (40%) ---
        if phase in ["ICE_BREAKING", "EVENT_EXTENSION", "WRAP_UP"]:
            if user_text.strip():
                chat_idx = session_ctx.get("chat_interaction_count", 0)
                if chat_idx < len(CHAT_SCORE_CURVE):
                    added_score = CHAT_SCORE_CURVE[chat_idx]
                    session_ctx["chat_score"] = min(40.0, session_ctx.get("chat_score", 0.0) + added_score)
                    session_ctx["chat_interaction_count"] = chat_idx + 1
                    logger.info(f"💬 [闲聊加分] 曲线阶段 {chat_idx+1} (+{added_score}分) | 当前轮闲聊分: {session_ctx['chat_score']:.1f}/40")

        # --- 计分模块 2：核心任务分 (60%) ---
        is_core_task = (phase == "CORE_TASK")
        new_targets = session_ctx.get("new_targets", [])
        history_targets = session_ctx.get("history_targets", [])
        all_active_targets = new_targets + history_targets

        if is_core_task and all_active_targets:
            points_per_new_word = 60.0 / len(new_targets) if new_targets else 10.0
            hit_occurred = False
            current_hits = []

            for node in all_active_targets:
                node_id = node.get("id")
                if node_id is None:
                    continue

                node_text = node.get("node_text", "")
                if not node_text:
                    continue

                # NLP 边界升级：使用 \w 防止撇号 (如 "don't") 或带有非英文字符的词语被错误截断
                pattern = rf"(?<!\w){re.escape(node_text.lower())}(?!\w)"
                if re.search(pattern, user_text_lower) and node_id not in session_hits:
                    session_hits.add(node_id)
                    current_hits.append(node_id)

                    is_new_word = any(n.get("id") == node_id for n in new_targets)
                    added_task = points_per_new_word if is_new_word else (points_per_new_word * 0.5)
                    session_ctx["task_score"] = min(60.0, session_ctx.get("task_score", 0.0) + added_task)
                    hit_occurred = True
                    logger.info(f"🎯 [任务加分] 精准击中核心词 '{node_text}' (+{added_task:.1f}分)")

            # 消除 N+1 查询，批量拉取和更新进度
            if hit_occurred and current_hits:
                existing_progress = db.query(UserProgress).filter(
                    UserProgress.user_id == user_id,
                    UserProgress.node_id.in_(current_hits)
                ).all()
                progress_map = {p.node_id: p for p in existing_progress}

                for hit_id in current_hits:
                    progress = progress_map.get(hit_id)
                    if not progress:
                        progress = UserProgress(user_id=user_id, node_id=hit_id, mastery_score=0, practice_count=0)
                        db.add(progress)
                    progress.practice_count += 1
                    progress.mastery_score = min(100.0, progress.mastery_score + 15.0)

                db.commit()

        # --- 计分模块 3：计算并下发总进度 ---
        completed_rounds = session_ctx.get("completed_rounds_in_level", 0)
        current_round_score = session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0)

        total_level_score = (completed_rounds * 100.0) + current_round_score
        overall_progress = min(100.0, total_level_score / float(ROUNDS_PER_LEVEL))

        try:
            async with ws_lock:
                await websocket.send_text(json.dumps({
                    "event": "topic_mastery_reached",
                    "progress": overall_progress,
                    "level": session_ctx.get("current_level", 1),
                    "next_topic_suggestion": ""
                }))
        except Exception as e:
            logger.error(f"Ws 发送进度异常: {e}")

    except Exception as e:
        logger.error(f"打分系统异常: {e}", exc_info=True)


def advance_state_machine(session_ctx: dict, db: Session, current_topic: Topic, session_hits: set):
    phase = session_ctx.get("phase", "ICE_BREAKING")
    llm_signal = session_ctx.get("llm_wants_to_advance", False)
    transitioned = False

    turns = session_ctx.get("phase_turns", 0)
    force_advance = (turns >= MAX_TURNS_PER_PHASE)

    if phase == "ICE_BREAKING" and (llm_signal or force_advance):
        session_ctx["phase"] = "CORE_TASK"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False

        old_new = session_ctx.get("new_targets", [])
        session_ctx["history_targets"] = session_ctx.get("history_targets", []) + old_new

        all_nodes = db.query(TargetNode).filter(TargetNode.topic_id == current_topic.id).all() if current_topic else []
        # 铁壁防御：过滤 None
        used_ids = [n.get("id") for n in session_ctx["history_targets"] if n.get("id") is not None]
        available = [n for n in all_nodes if n.id not in used_ids]

        selected_nodes = random.sample(available, min(2, len(available))) if available else []
        session_ctx["new_targets"] = [{"id": n.id, "node_text": n.node_text} for n in selected_nodes]

        transitioned = True
        if not all_nodes:
            logger.warning("⚠️ Topic 无目标词，CORE_TASK 将作为普通聊天进行。")
        else:
            logger.info(f"🔄 [推进] 进入核心考核！本轮新词: {[n.get('node_text') for n in session_ctx['new_targets']]}")

    elif phase == "CORE_TASK" and (llm_signal or force_advance):
        session_ctx["phase"] = "EVENT_EXTENSION"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False
        session_ctx["current_event"] = random.choice(EVENT_POOL)
        transitioned = True
        logger.info(f"🔄 [推进] 考核结束，触发随机剧情: {session_ctx['current_event']}")

    elif phase == "EVENT_EXTENSION" and (llm_signal or force_advance):
        session_ctx["phase"] = "WRAP_UP"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False

        session_ctx["task_score"] = 60.0
        logger.info("🎁 [奖励发放] 成功推进至收尾阶段，系统发放本轮保底满分 (任务分 60/60)！")
        transitioned = True

    elif phase == "WRAP_UP" and (llm_signal or force_advance):
        session_ctx["phase"] = "ICE_BREAKING"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False

        session_ctx["loop_count"] = session_ctx.get("loop_count", 1) + 1
        session_ctx["completed_rounds_in_level"] = session_ctx.get("completed_rounds_in_level", 0) + 1

        session_ctx["chat_score"] = 0.0
        session_ctx["task_score"] = 0.0
        session_ctx["chat_interaction_count"] = 0

        if session_ctx["completed_rounds_in_level"] >= ROUNDS_PER_LEVEL:
            session_ctx["current_level"] = session_ctx.get("current_level", 1) + 1
            session_ctx["completed_rounds_in_level"] = 0

            session_ctx["history_targets"] = []
            session_ctx["new_targets"] = []
            session_hits.clear()

            logger.info(f"🎉🎉🎉 [状态机结算] 恭喜突破！成功晋级至 Lv.{session_ctx['current_level']}，词表重置 🎉🎉🎉")
        else:
            logger.info(f"🔄 [状态机结算] 进入本段位第 {session_ctx['completed_rounds_in_level'] + 1} 轮！词汇继续滚雪球。")

        transitioned = True

    return transitioned


# ================= 异步后置任务 =================

async def async_fetch_and_send_teaching(user_msg, ai_msg, teaching_config, client, client_ws: WebSocket, ws_lock: asyncio.Lock):
    """后台调用 LLM 获取翻译和回复建议，具备超时防阻塞和 JSON 清洗"""
    if not any(teaching_config.values()): return

    # 防御：对话超长时截断防爆 (限制最后 1000 字符)
    safe_user_msg = user_msg[-1000:] if len(user_msg) > 1000 else user_msg
    safe_ai_msg = ai_msg[-1000:] if len(ai_msg) > 1000 else ai_msg

    tasks_str = "1. A natural Chinese translation of the AI's reply.\n2. 1-2 suggested ways for the user to respond next (PLAIN ENGLISH ONLY).\n"
    json_format_str = '{\n  "ai_translation_cn": "...",\n  "suggested_hints_en": ["Hint 1", "Hint 2"]\n}'

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a professional English coach."},
                    {"role": "user",
                     "content": f"User: {safe_user_msg}\nAI: {safe_ai_msg}\n\nTasks:\n{tasks_str}\nOutput JSON:\n{json_format_str}"}
                ],
                response_format={"type": "json_object"}
            ),
            timeout=15.0
        )

        raw_content = resp.choices[0].message.content
        data = clean_llm_json(raw_content)

        data["user_text"] = user_msg
        data["ai_text"] = ai_msg

        try:
            async with ws_lock:
                await client_ws.send_text(json.dumps({"event": "teaching_data", "data": data}))
        except Exception as ws_err:
            logger.error(f"Ws 发送教学数据失败: {ws_err}")

    except asyncio.TimeoutError:
        logger.warning("⚠️ 教学面板获取超时，本次主动放弃。")
    except ValueError as ve:
        logger.warning(f"⚠️ 教学面板 JSON 格式解析失败: {ve}")
    except Exception as e:
        logger.error(f"⚠️ 教学面板生成发生系统异常: {e}")