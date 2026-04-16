import json
import random
import logging
from sqlalchemy.orm import Session
from fastapi import WebSocket
import asyncio

from database import User, Topic, TargetNode, UserProgress

logger = logging.getLogger("EnglishCoach")

def load_active_scene(file_path="scenes.json"):
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

ACTIVE_SCENE = load_active_scene()

# 突发事件池 (在第三阶段随机抽取)
EVENT_POOL = [
    "A colleague just texted the user asking them to add something specific to the order. Ask the user what their colleague wants.",
    "A different staff member enthusiastically interrupts to introduce a new seasonal item. Briefly act as this new character.",
    "There is a sudden mix-up with another customer's items. The user needs to describe exactly what they originally ordered to help you fix it.",
    "A system glitch means the user has to switch their payment method or split the bill. Ask them how they want to handle it."
]

# 🌟 闲聊得分的心流曲线 (非线性：前后慢，中间快。总和正好 40 分)
CHAT_SCORE_CURVE = [2.0, 3.0, 5.0, 8.0, 10.0, 6.0, 3.0, 2.0, 1.0]

def build_dynamic_prompt(user: User, is_flipped: bool, session_ctx: dict):
    # 🌟 防过拟合：从配置文件动态读取变量，拒绝在 Prompt 中硬编码特定场景
    scene_name = ACTIVE_SCENE.get("scene", "Daily Conversation")
    role_name = ACTIVE_SCENE.get("role", "Assistant")
    user_level = ACTIVE_SCENE.get("level", "Intermediate")

    role_desc = f"You are acting as: {role_name} in a {scene_name} setting." if not is_flipped else f"You are the CUSTOMER/USER. The user is acting as the {role_name}."

    politeness = {
        0: "Your personality: IMPATIENT and RUDE.",
        1: "Your personality: PROFESSIONAL and POLITE.",
        2: "Your personality: EXTREMELY POLITE and TALKATIVE."
    }
    personality_desc = politeness.get(user.politeness_level, politeness[1]) if user else politeness[1]

    # 🌟 精简且强硬的通用规则 (Universal Rules)，不超过5条
    critical_rules = """
[UNIVERSAL COACHING RULES]
1. OPEN-ENDED: NEVER ask simple Yes/No questions. Always use What/How/Why/Which to force longer user responses.
2. AGGRESSIVE PIVOT: If the user drifts off-topic, give a 1-sentence friendly reply, then IMMEDIATELY return to the current phase's goal.
3. CONVERSATIONAL: Keep replies engaging but concise (15 to 30 words).
4. PLAIN TEXT: Reply in plain English text only. Spell out numbers/symbols.
5. NO SPOILERS: Never explicitly list the secret target words to the user.
"""

    phase = session_ctx.get("phase", "ICE_BREAKING")
    loop_count = session_ctx.get("loop_count", 1)

    llm_router_rules = f"""
[PHASE CONTROL]
You are the director. We are in Round {loop_count}.
Current Phase: {phase}
HOW TO ADVANCE: When the current phase's objective is naturally satisfied based on the guidelines below, you MUST append EXACTLY "[ADVANCE]" to the VERY END of your reply to trigger the next stage. 
CRITICAL: NEVER output just "[ADVANCE]" by itself! You MUST provide a natural, conversational reply first, and then put "[ADVANCE]" at the very end.
"""

    # 🌟 强悍的阶段防火墙与抽象化任务描述
    dynamic_instruction = ""
    if phase == "ICE_BREAKING":
        dynamic_instruction = f"""PHASE 1: ICE BREAKING (Small Talk)
Goal: Chat naturally to build rapport in this {scene_name} context.
STRICT FIREWALL: DO NOT process the main transaction or mission in this phase! DO NOT execute the core task yet!
ADVANCE RULE: The moment the user tries to initiate the core task or states their main purpose, acknowledge it briefly and IMMEDIATELY output [ADVANCE] at the end of your reply. Leave the actual task execution for Phase 2!"""
    
    elif phase == "CORE_TASK":
        new_targets = ", ".join([f"'{n.node_text}'" for n in session_ctx.get("new_targets", [])])
        history_targets = ", ".join([f"'{n.node_text}'" for n in session_ctx.get("history_targets", [])])
        history_instruction = f"If natural, casually review these past words too: {history_targets}." if history_targets else ""
        
        dynamic_instruction = f"""PHASE 2: CORE TASK (Language Practice)
Goal: Now officially execute the main transaction/mission of this {scene_name}.
SECRET MISSION: Naturally guide the user to say these NEW words: {new_targets}.
{history_instruction}
ADVANCE RULE: Output [ADVANCE] ONLY when the core task is fully complete (e.g., final results given, ready to conclude or pay)."""
    
    elif phase == "EVENT_EXTENSION":
        dynamic_instruction = f"""PHASE 3: EVENT EXTENSION (The Twist)
Goal: Introduce a sudden complication or unexpected twist.
SYSTEM OVERRIDE: Introduce this specific scenario: '{session_ctx.get('current_event', '')}'
ADVANCE RULE: Fully embrace this twist. Once the user successfully responds to and resolves this twist, output [ADVANCE]."""
    
    elif phase == "WRAP_UP":
        dynamic_instruction = """PHASE 4: WRAP UP (Conclusion)
Goal: End the current interaction naturally (e.g., finalize process, saying goodbye).
ADVANCE RULE: Conclude the conversation gracefully. Output [ADVANCE] in your final goodbye message to reset the simulation for the next round."""

    return f"Learner Level: {user_level}\n{role_desc}\n{personality_desc}\n\n{critical_rules}\n\n{llm_router_rules}\n\n[YOUR CURRENT DIRECTIVE]:\n{dynamic_instruction}"

async def evaluate_and_check_progress(db: Session, user_id: str, topic_id: int, user_text: str, session_hits: set,
                                      session_ctx: dict, websocket: WebSocket, ws_lock: asyncio.Lock):
    try:
        phase = session_ctx.get("phase", "ICE_BREAKING")
        user_text_lower = user_text.lower()
        
        # ==========================================
        # 计分模块 1： 闲聊分 (40%) - 心流曲线算法
        # ==========================================
        if phase in ["ICE_BREAKING", "EVENT_EXTENSION", "WRAP_UP"]:
            if user_text.strip():
                chat_idx = session_ctx.get("chat_interaction_count", 0)
                if chat_idx < len(CHAT_SCORE_CURVE):
                    added_score = CHAT_SCORE_CURVE[chat_idx]
                    session_ctx["chat_score"] = min(40.0, session_ctx.get("chat_score", 0.0) + added_score)
                    session_ctx["chat_interaction_count"] = chat_idx + 1
                    logger.info(f"💬 [闲聊加分] 曲线阶段 {chat_idx+1} (+{added_score}分) | 当前轮闲聊分: {session_ctx['chat_score']:.1f}/40")

        # ==========================================
        # 计分模块 2： 核心任务分 (60%) - 滚雪球记忆匹配
        # ==========================================
        is_core_task = (phase == "CORE_TASK")
        new_targets = session_ctx.get("new_targets", [])
        history_targets = session_ctx.get("history_targets", [])
        all_active_targets = new_targets + history_targets

        if is_core_task and all_active_targets:
            points_per_new_word = 60.0 / len(new_targets) if new_targets else 10.0
            hit_occurred = False

            for node in all_active_targets:
                if node.node_text.lower() in user_text_lower and node.id not in session_hits:
                    session_hits.add(node.id)
                    # 击中新词拿满分，击中复习旧词给一半奖励分
                    added_task = points_per_new_word if node in new_targets else (points_per_new_word * 0.5)
                    session_ctx["task_score"] = min(60.0, session_ctx.get("task_score", 0.0) + added_task)
                    hit_occurred = True
                    logger.info(f"🎯 [任务加分] 击中核心词 '{node.node_text}' (+{added_task:.1f}分) | 当前轮任务分: {session_ctx['task_score']:.1f}/60")

                    # 持久化到数据库
                    progress = db.query(UserProgress).filter(UserProgress.user_id == user_id, UserProgress.node_id == node.id).first()
                    if not progress:
                        progress = UserProgress(user_id=user_id, node_id=node.id, mastery_score=0, practice_count=0)
                        db.add(progress)
                    progress.practice_count += 1
                    progress.mastery_score = min(100.0, progress.mastery_score + 15.0)

            if hit_occurred:
                db.commit()

        # ==========================================
        # 计分模块 3： 聚合当前层级 (Level) 总进度
        # ==========================================
        completed_rounds = session_ctx.get("completed_rounds_in_level", 0)
        current_round_score = session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0)
        
        total_level_score = (completed_rounds * 100.0) + current_round_score
        overall_progress = min(100.0, total_level_score / 3.0) 
        
        async with ws_lock:
            await websocket.send_text(json.dumps({
                "event": "topic_mastery_reached", 
                "progress": overall_progress,
                "level": session_ctx.get("current_level", 1),
                "next_topic_suggestion": "" 
            }))
            
    except Exception as e:
        logger.error(f"打分系统异常: {e}", exc_info=True)

def advance_state_machine(session_ctx: dict, db: Session, current_topic: Topic, session_hits: set):
    phase = session_ctx.get("phase", "ICE_BREAKING")
    llm_signal = session_ctx.get("llm_wants_to_advance", False)
    transitioned = False

    turns = session_ctx.get("phase_turns", 0)
    force_advance = (turns >= 25) # 兜底防卡死机制

    if phase == "ICE_BREAKING" and (llm_signal or force_advance):
        session_ctx["phase"] = "CORE_TASK"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False
        
        # 滚雪球逻辑
        old_new = session_ctx.get("new_targets", [])
        session_ctx["history_targets"] = session_ctx.get("history_targets", []) + old_new
        
        all_nodes = db.query(TargetNode).filter(TargetNode.topic_id == current_topic.id).all() if current_topic else []
        used_ids = [n.id for n in session_ctx["history_targets"]]
        available = [n for n in all_nodes if n.id not in used_ids]
        session_ctx["new_targets"] = random.sample(available, min(2, len(available))) if available else []
        
        transitioned = True
        logger.info(f"🔄 [推进] 进入核心考核！本轮新词: {[n.node_text for n in session_ctx['new_targets']]} | 复习旧词: {[n.node_text for n in session_ctx['history_targets']]}")

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
        
        if session_ctx["completed_rounds_in_level"] >= 3:
            session_ctx["current_level"] = session_ctx.get("current_level", 1) + 1
            session_ctx["completed_rounds_in_level"] = 0
            logger.info(f"🎉🎉🎉 [状态机结算] 恭喜突破！成功晋级至 Lv.{session_ctx['current_level']} 🎉🎉🎉")
        else:
            logger.info(f"🔄 [状态机结算] 进入本段位第 {session_ctx['completed_rounds_in_level'] + 1} 轮！重新开始破冰。")

        transitioned = True

    return transitioned

async def async_fetch_and_send_teaching(user_msg, ai_msg, teaching_config, client, client_ws: WebSocket, ws_lock: asyncio.Lock):
    if not any(teaching_config.values()): return

    tasks_str = "1. A natural Chinese translation of the AI's reply.\n2. 1-2 suggested ways for the user to respond next (PLAIN ENGLISH ONLY).\n"
    json_format_str = '{\n  "ai_translation_cn": "...",\n  "suggested_hints_en": ["Hint 1", "Hint 2"]\n}'

    try:
        resp = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a professional English coach."},
                {"role": "user",
                 "content": f"User: {user_msg}\nAI: {ai_msg}\n\nTasks:\n{tasks_str}\nOutput JSON:\n{json_format_str}"}
            ],
            response_format={"type": "json_object"}
        )
        data = json.loads(resp.choices[0].message.content)
        data["user_text"] = user_msg
        data["ai_text"] = ai_msg

        async with ws_lock:
            await client_ws.send_text(json.dumps({"event": "teaching_data", "data": data}))
    except Exception as e:
        logger.error(f"⚠️ 教学面板生成失败: {e}")