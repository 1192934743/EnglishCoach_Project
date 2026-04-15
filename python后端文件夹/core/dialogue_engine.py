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

EVENT_POOL = [
    "A colleague just texted the user asking them to add something specific to the order. Ask the user what their colleague wants.",
    "A different staff member (like an ice cream vendor or promo mascot) enthusiastically interrupts to introduce a new seasonal item. Briefly act as this new character.",
    "There is a sudden mix-up with another customer's items. The user needs to describe exactly what they originally ordered to help you fix it.",
    "A system glitch means the user has to switch their payment method or split the bill. Ask them how they want to handle it."
]


def build_dynamic_prompt(user: User, is_flipped: bool, session_ctx: dict):
    scene_context = f"Current Simulation: {ACTIVE_SCENE['scene']}.\nLearner Level: {ACTIVE_SCENE['level']}."
    role_desc = f"You are the {ACTIVE_SCENE['role']}." if not is_flipped else f"You are the CUSTOMER. The user is the {ACTIVE_SCENE['role']}."

    politeness = {
        0: "Your personality: IMPATIENT and RUDE.",
        1: "Your personality: PROFESSIONAL and POLITE.",
        2: "Your personality: EXTREMELY POLITE and TALKATIVE."
    }
    personality_desc = politeness.get(user.politeness_level, politeness[1]) if user else politeness[1]

    critical_rules = """
CRITICAL RULES FOR LANGUAGE COACHING:
1. OPEN-ENDED QUESTIONS: NEVER ask simple Yes/No questions. Always ask open-ended questions (using What, How, Why, Which) to force the user to speak full sentences.
2. DIALOGUE LENGTH: Keep your replies engaging and conversational. Aim for 15 to 30 words. Do not give extremely short answers.
3. STRICT CONTEXT & PRONOUNS: Be immersive. If the user is ordering for a friend, maintain your role but correctly refer to the friend in the third person (e.g., "What does he/she want?").
4. FORMAT: Reply in PLAIN ENGLISH TEXT ONLY. Spell out all numbers and symbols.
5. ANTI-END DIRECTIVE: NEVER say "Goodbye" or end the conversation. Use the [ADVANCE] tag to shift phases instead.
"""

    phase = session_ctx["phase"]
    loop_count = session_ctx["loop_count"]

    llm_router_rules = f"""
[PHASE TRANSITION POWER]
Current Phase: {phase}
Current Loop: Round {loop_count}

RULES FOR ADVANCING:
- If ICE_BREAKING: Advance ONLY when the user is clearly trying to conclude the interaction or pay. Do not advance if they just say hello.
- If CORE_TASK: Advance ONLY when the user has successfully used the secret words, OR if they are completely stuck.
- If EVENT_EXTENSION: You MUST advance immediately AFTER the user responds to your plot twist.

HOW TO ADVANCE: Append EXACTLY "[ADVANCE]" to the VERY END of your reply.
"""

    dynamic_instruction = ""
    if phase == "ICE_BREAKING":
        dynamic_instruction = """PHASE: ICE BREAKING (Main Immersion)
Interact with the user naturally based on the scene. Let the conversation flow."""
    elif phase == "CORE_TASK":
        target_str = ", ".join([f"'{n.node_text}'" for n in session_ctx["active_targets"]])
        dynamic_instruction = f"""PHASE: CORE TASK (Language Practice)
SECRET MISSION BATCH: You must naturally guide the user to say these words: {target_str}.
STRATEGY: Weave them into the conversation logically. Ask OPEN-ENDED questions."""
    elif phase == "EVENT_EXTENSION":
        dynamic_instruction = f"""PHASE: EVENT EXTENSION (The Story Bridge)
SYSTEM OVERRIDE: Introduce this specific scenario to start a new round of conversation:
'{session_ctx['current_event']}'
ACTION: Fully embrace this twist. Ask an open-ended question.
CRITICAL: Once the user replies to this twist, output [ADVANCE] in your next response!"""

    return f"{scene_context}\n\n{role_desc}\n{personality_desc}\n{critical_rules}\n\n{llm_router_rules}\n\n[YOUR CURRENT DIRECTIVE]:\n{dynamic_instruction}"


async def evaluate_and_check_progress(db: Session, user_id: str, topic_id: int, user_text: str, session_hits: set,
                                      session_ctx: dict, websocket: WebSocket, ws_lock: asyncio.Lock):
    try:
        user_text_lower = user_text.lower()
        nodes = db.query(TargetNode).filter(TargetNode.topic_id == topic_id).all()
        total_weight, current_weighted_score = 0.0, 0.0

        for node in nodes:
            total_weight += node.weight
            progress = db.query(UserProgress).filter(UserProgress.user_id == user_id,
                                                     UserProgress.node_id == node.id).first()
            if not progress:
                progress = UserProgress(user_id=user_id, node_id=node.id, mastery_score=0, practice_count=0)
                db.add(progress)

            if node.node_text.lower() in user_text_lower and node.id not in session_hits:
                if progress.mastery_score < 100:
                    progress.practice_count += 1
                    progress.mastery_score = min(100.0, progress.mastery_score + 15.0)
                    session_hits.add(node.id)
                    logger.info(f"📈 [打分] 击中 '{node.node_text}' (+15分)")

            current_weighted_score += (progress.mastery_score * node.weight)

        db.commit()

        overall_progress = (current_weighted_score / (total_weight * 100.0)) * 100.0 if total_weight > 0 else 0.0
        if overall_progress >= 70.0 and "notified_topic_mastery" not in session_hits:
            session_hits.add("notified_topic_mastery")
            async with ws_lock:
                await websocket.send_text(json.dumps({"event": "topic_mastery_reached", "progress": overall_progress,
                                                      "next_topic_suggestion": "Starbucks Coffee"}))
    except Exception as e:
        logger.error(f"打分系统异常: {e}")


def advance_state_machine(session_ctx: dict, db: Session, current_topic: Topic, session_hits: set):
    phase = session_ctx["phase"]
    turns = session_ctx["phase_turns"]
    llm_signal = session_ctx.get("llm_wants_to_advance", False)
    transitioned = False

    if phase == "ICE_BREAKING" and (llm_signal or turns >= 15):
        if session_ctx["loop_count"] == 1:
            session_ctx["phase"] = "EVENT_EXTENSION"
            session_ctx["current_event"] = random.choice(EVENT_POOL)
            logger.info(f"🔄 [状态机 - 第1轮] 破冰结束，切入剧情桥梁: {session_ctx['current_event']}")
        else:
            session_ctx["phase"] = "CORE_TASK"
            all_nodes = db.query(TargetNode).filter(
                TargetNode.topic_id == current_topic.id).all() if current_topic else []
            available = [n for n in all_nodes if n.id not in session_hits]
            session_ctx["active_targets"] = random.sample(available, min(3, len(available))) if available else []
            logger.info(
                f"🔄 [状态机 - 第{session_ctx['loop_count']}轮] 切入主线考核 -> 目标: {[n.node_text for n in session_ctx['active_targets']]}")

        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False
        transitioned = True

    elif phase == "CORE_TASK" and (llm_signal or turns >= 15):
        session_ctx["phase"] = "EVENT_EXTENSION"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False
        session_ctx["current_event"] = random.choice(EVENT_POOL)
        logger.info(
            f"🔄 [状态机 - 第{session_ctx['loop_count']}轮] 考核结束，切入剧情桥梁: {session_ctx['current_event']}")
        transitioned = True

    elif phase == "EVENT_EXTENSION" and (llm_signal or turns >= 15):
        session_ctx["loop_count"] += 1
        session_ctx["phase"] = "ICE_BREAKING"
        session_ctx["phase_turns"] = 0
        session_ctx["llm_wants_to_advance"] = False
        logger.info(f"🔄 [状态机] 桥梁跨越成功 -> 开启第 {session_ctx['loop_count']} 轮无限流！")
        transitioned = True

    return transitioned


async def async_fetch_and_send_teaching(user_msg, ai_msg, teaching_config, client, client_ws: WebSocket,
                                        ws_lock: asyncio.Lock):
    """🌟 完整找回的教学辅助模块"""
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