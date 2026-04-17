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

[Prompt 物理隔离架构]:
- 所有英文文案（通用规则、阶段指令、性格描述）存储于 prompts/global_rules.json。
- 场景三元组与场景专属护栏：优先从 TaskPacket 读取；TaskPacket 为 None 时降级到 scenes.json。
- 本文件只负责结构组装与动态变量注入，不硬编码任何一句英文提示词。

[TaskPacket 集成]:
- build_dynamic_prompt 新增 task_packet 可选参数
- 传入 TaskPacket 时：scene/role/level/rules 均从 TaskPacket 读取（LMS 主导）
- task_packet=None 时：降级到 scenes.json（向后兼容，便于测试和迁移期使用）
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
from domain.entities.task_packet import TaskPacket, compute_max_reply_sentences
from application.services.mastery_scorer import (
    update_mastery, L1_EXACT_QUALITY, L1_STEM_QUALITY,
    normalize_text, simple_stem,
)

logger = logging.getLogger("EnglishCoach")

# ================= 业务全局常量 =================

ROUNDS_PER_LEVEL = 3           # 每个段位需要完成的局数
MAX_TURNS_PER_PHASE = 25       # 兜底机制：每个阶段最大互动轮数，超时强制推进

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))  # python_backend/
# 绝对路径，确保从任意目录启动 uvicorn 均能正常加载配置
SCENES_FILE_PATH = os.path.join(_BACKEND_DIR, "scenes.json")
GLOBAL_RULES_FILE_PATH = os.path.join(_BACKEND_DIR, "prompts", "global_rules.json")

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

def load_global_rules(file_path=GLOBAL_RULES_FILE_PATH):
    """加载全局 Prompt 配置，支持热更新；加载失败时返回空字典，prompt 降级但不崩溃"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"🚨 Could not load global_rules.json ({e}). Prompt will be degraded.")
        return {}

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

def build_dynamic_prompt(
    user: Optional[User],
    is_flipped: bool,
    session_ctx: dict,
    task_packet: Optional[TaskPacket] = None,
):
    """
    Prompt 组装入口。

    优先级：
    1. task_packet 不为 None → 从 TaskPacket 读取场景信息（LMS 主导模式）
    2. task_packet 为 None  → 降级到 scenes.json（兼容模式，用于测试/迁移期）

    所有英文文案模板仍从 global_rules.json 加载，本函数只负责结构拼接与变量注入。
    """
    rules = load_global_rules()

    if task_packet is not None:
        # ── LMS 主导模式：从 TaskPacket 读取场景三元组 ──────────────────────
        scene_name = task_packet.scene_prompt
        role_name = task_packet.role_name
        user_level = task_packet.learner_level
        scene_specific_rules = task_packet.scene_specific_rules
        depth_tier_val = int(task_packet.depth_tier or 1)
        max_reply_sentences = int(task_packet.max_reply_sentences or 0) or compute_max_reply_sentences(
            user_level, depth_tier_val
        )

        # 将 session_goal 注入 prompt（帮助 AI 理解本次练习意图）
        session_goal_line = (
            f"\n[SESSION GOAL] {task_packet.session_goal}" if task_packet.session_goal else ""
        )
    else:
        # ── 兼容模式：从 scenes.json 读取 ───────────────────────────────────
        active_scene = load_active_scene()
        scene_name = active_scene.get("scene", "Daily Conversation")
        role_name = active_scene.get("role", "Assistant")
        user_level = active_scene.get("level", "Intermediate")
        scene_specific_rules = active_scene.get("scene_specific_rules", [])
        session_goal_line = ""
        depth_tier_val = 1
        max_reply_sentences = compute_max_reply_sentences(user_level, depth_tier_val)

    phase = session_ctx.get("phase", "ICE_BREAKING")
    loop_count = session_ctx.get("loop_count", 1)

    # ── 角色与性格 ──────────────────────────────────────────────────────────
    role_desc = (
        f"You are acting as: {role_name} in a {scene_name} setting."
        if not is_flipped
        else f"You are the CUSTOMER/USER. The user is acting as the {role_name}."
    )
    personality_levels = rules.get("personality_levels", {
        "0": "Your personality: IMPATIENT and RUDE.",
        "1": "Your personality: PROFESSIONAL and POLITE.",
        "2": "Your personality: EXTREMELY POLITE and TALKATIVE."
    })
    politeness_key = str(user.politeness_level) if user else "1"
    personality_desc = personality_levels.get(politeness_key, personality_levels.get("1", ""))

    prompt_blocks = [
        (
            f"Learner Level: {user_level} | Practice content tier (target nodes): {depth_tier_val} "
            f"| Coach turn length cap: {max_reply_sentences} short in-character sentences per reply "
            f"(excluding a trailing [ADVANCE] token if required)"
        ),
        role_desc,
        personality_desc,
        "",
    ]

    # ── 通用规则 ────────────────────────────────────────────────────────────
    universal_rules = rules.get("universal_rules", [])
    if universal_rules:
        prompt_blocks.append("[UNIVERSAL COACHING RULES]")
        for i, rule in enumerate(universal_rules, 1):
            prompt_blocks.append(f"{i}. {rule}")
        prompt_blocks.append("")

    # ── 场景专属护栏（优先来自 TaskPacket，兼容模式来自 scenes.json）───────────
    if scene_specific_rules:
        prompt_blocks.append("[SCENE-SPECIFIC RULES]")
        for rule in scene_specific_rules:
            prompt_blocks.append(f"- {rule}")
        prompt_blocks.append("")

    # ── 本次练习目标（仅 TaskPacket 模式下注入）──────────────────────────────
    if session_goal_line:
        prompt_blocks.append(session_goal_line)
        prompt_blocks.append("")

    # ── 阶段控制框架 ────────────────────────────────────────────────────────
    phase_control = rules.get("phase_control", {})
    if phase_control:
        prompt_blocks.append("[PHASE CONTROL]")
        prompt_blocks.append(
            phase_control.get("header", "").format(loop_count=loop_count, phase=phase)
        )
        prompt_blocks.append(phase_control.get("advance_rule", ""))
        prompt_blocks.append(phase_control.get("advance_critical", ""))
        prompt_blocks.append("")

    # ── 当前阶段指令（从 JSON 读取，用 .format() 注入动态变量）─────────────
    prompt_blocks.append("[YOUR CURRENT DIRECTIVE]:")
    directive = rules.get("phase_directives", {}).get(phase, {})

    if phase == "ICE_BREAKING":
        prompt_blocks.append(directive.get("header", "PHASE 1: ICE BREAKING (Small Talk)"))
        prompt_blocks.append(directive.get("goal", "").format(scene_name=scene_name))
        prompt_blocks.append(directive.get("firewall", ""))
        prompt_blocks.append(directive.get("advance", ""))

    elif phase == "CORE_TASK":
        new_targets_list = [n.get("node_text") for n in session_ctx.get("new_targets", []) if n.get("node_text")]
        history_targets_list = [n.get("node_text") for n in session_ctx.get("history_targets", []) if n.get("node_text")]
        new_targets = ", ".join([f"'{t}'" for t in new_targets_list])
        history_targets = ", ".join([f"'{t}'" for t in history_targets_list])

        prompt_blocks.append(directive.get("header", "PHASE 2: CORE TASK (Language Practice)"))
        prompt_blocks.append(directive.get("goal", "").format(scene_name=scene_name))
        prompt_blocks.append(directive.get("directive", "").format(new_targets=new_targets))
        if history_targets:
            prompt_blocks.append(directive.get("history", "").format(history_targets=history_targets))
        prompt_blocks.append(directive.get("coaching", "").format(role_name=role_name))
        prompt_blocks.append(directive.get("advance", ""))

    elif phase == "EVENT_EXTENSION":
        current_event = session_ctx.get("current_event", "There is a small problem with your request.")
        prompt_blocks.append(directive.get("header", "PHASE 3: EVENT EXTENSION (The Twist)"))
        prompt_blocks.append(directive.get("goal", ""))
        prompt_blocks.append(directive.get("override", "").format(current_event=current_event))
        prompt_blocks.append(directive.get("coach_role", ""))
        prompt_blocks.append(directive.get("advance", ""))

    elif phase == "WRAP_UP":
        prompt_blocks.append(directive.get("header", "PHASE 4: WRAP UP (Conclusion)"))
        prompt_blocks.append(directive.get("goal", ""))
        prompt_blocks.append(directive.get("advance", ""))

    if phase == "ICE_BREAKING" and max_reply_sentences <= 2:
        prompt_blocks.append(
            "SESSION TIGHT BUDGET: In ICE BREAKING, use at most ONE open-ended question in this turn "
            "(a greeting plus one question still counts as ≤2 sentences)."
        )

    # Recency: models often overweight later instructions; repeat length cap after phase text.
    prompt_blocks.append("")
    prompt_blocks.append(
        f"[OUTPUT BUDGET] Your next reply: at most {max_reply_sentences} short in-character sentences "
        f"before any trailing [ADVANCE] token. No bullet lists, no lecture-style multi-paragraph answers."
    )

    return "\n".join(prompt_blocks)

# ================= 核心计分与状态机 =================

async def evaluate_and_check_progress(db: Session, user_id: str, _topic_id: int, user_text: str, session_hits: set,
                                      session_ctx: dict, websocket: WebSocket, ws_lock: asyncio.Lock):
    """
    L1 评估层：轻量同步，每轮对话触发。

    升级点（Phase 2）：
    1. 文本规范化：缩写展开 + 词干匹配（normalize_text / simple_stem）
    2. 命中质量分级：精准匹配=1.0，词干匹配=0.8
    3. 掌握度更新：SM-2 公式（update_mastery）替换原来的 flat +15
    4. 更新 last_practiced_at（之前从未更新）

    @param _topic_id: 保留作备用，供后续话题维度细粒度统计。
    """
    try:
        phase = session_ctx.get("phase", "ICE_BREAKING")

        # --- 计分模块 1：闲聊分 (40%) ---
        if phase in ["ICE_BREAKING", "EVENT_EXTENSION", "WRAP_UP"]:
            if user_text.strip():
                chat_idx = session_ctx.get("chat_interaction_count", 0)
                if chat_idx < len(CHAT_SCORE_CURVE):
                    added_score = CHAT_SCORE_CURVE[chat_idx]
                    session_ctx["chat_score"] = min(40.0, session_ctx.get("chat_score", 0.0) + added_score)
                    session_ctx["chat_interaction_count"] = chat_idx + 1
                    logger.info(f"[L1] Chat curve step {chat_idx+1} (+{added_score:.1f}) | total={session_ctx['chat_score']:.1f}/40")

        # --- 计分模块 2：核心任务 L1 命中检测 (60%) ---
        is_core_task = (phase == "CORE_TASK")
        new_targets = session_ctx.get("new_targets", [])
        history_targets = session_ctx.get("history_targets", [])
        all_active_targets = new_targets + history_targets

        if is_core_task and all_active_targets:
            points_per_new_word = 60.0 / len(new_targets) if new_targets else 10.0

            # 规范化用户输入（缩写展开，仅做一次）
            user_normalized = normalize_text(user_text)

            # hit_info: [(node_id, quality_score)]
            hit_info: list[tuple[int, float]] = []

            for node in all_active_targets:
                node_id = node.get("id")
                node_text = node.get("node_text", "")
                if node_id is None or not node_text or node_id in session_hits:
                    continue

                node_normalized = normalize_text(node_text)
                quality = _l1_match_quality(node_normalized, user_normalized)
                if quality > 0:
                    session_hits.add(node_id)
                    hit_info.append((node_id, quality))

                    is_new = any(n.get("id") == node_id for n in new_targets)
                    added_task = points_per_new_word if is_new else (points_per_new_word * 0.5)
                    added_task *= quality  # 词干匹配只得 80% 分
                    session_ctx["task_score"] = min(60.0, session_ctx.get("task_score", 0.0) + added_task)
                    logger.info(f"[L1] Hit '{node_text}' quality={quality:.2f} +{added_task:.1f}pts")

            # 批量更新 UserProgress（SM-2 公式）
            if hit_info:
                hit_ids = [h[0] for h in hit_info]
                existing = db.query(UserProgress).filter(
                    UserProgress.user_id == user_id,
                    UserProgress.node_id.in_(hit_ids),
                ).all()
                progress_map = {p.node_id: p for p in existing}

                for node_id, quality in hit_info:
                    progress = progress_map.get(node_id)
                    if not progress:
                        progress = UserProgress(
                            user_id=user_id, node_id=node_id,
                            mastery_score=0.0, practice_count=0,
                        )
                        db.add(progress)
                    progress.practice_count += 1
                    progress.mastery_score = update_mastery(
                        progress.mastery_score, was_correct=True, quality=quality
                    )
                    progress.last_practiced_at = __import__("datetime").datetime.utcnow()

                db.commit()

        # --- 计分模块 3：计算并下发总进度 ---
        completed_rounds = session_ctx.get("completed_rounds_in_level", 0)
        current_round_score = session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0)
        overall_progress = min(100.0, ((completed_rounds * 100.0) + current_round_score) / float(ROUNDS_PER_LEVEL))

        try:
            async with ws_lock:
                await websocket.send_text(json.dumps({
                    "event": "topic_mastery_reached",
                    "progress": overall_progress,
                    "level": session_ctx.get("current_level", 1),
                    "next_topic_suggestion": ""
                }))
        except Exception as e:
            logger.error(f"Ws send progress error: {e}")

    except Exception as e:
        logger.error(f"evaluate_and_check_progress error: {e}", exc_info=True)


def _l1_match_quality(node_normalized: str, user_normalized: str) -> float:
    """
    L1 节点命中检测，返回质量分 0~1（0 = 未命中）。

    检测顺序（优先精准）：
    1. 精准匹配：node 完整出现在 user_text 中（词边界匹配）→ 1.0
    2. 词干匹配：node 中每个词的词干都出现在 user 词干集合中 → 0.8
    """
    # 精准匹配（带词边界，防止 "order" 误匹配 "disorder"）
    pattern_exact = rf"(?<!\w){re.escape(node_normalized)}(?!\w)"
    if re.search(pattern_exact, user_normalized):
        return L1_EXACT_QUALITY

    # 词干匹配：只有用户输入做 stem，节点词本身已是原型，不 stem（否则 "burger"→"burg" 导致误判）
    node_words = node_normalized.split()
    if len(node_words) <= 3:  # 超过 3 词的短语不做词干匹配，避免误报
        # 用户词的 stem 集合 + 原始词集合（双保险）
        user_word_set = {w for w in user_normalized.split() if len(w) >= 3}
        user_stem_set = {simple_stem(w) for w in user_word_set}
        all_user_forms = user_word_set | user_stem_set

        # 节点中长度 >= 3 的词直接与用户词形集合匹配
        node_key_words = [w for w in node_words if len(w) >= 3]
        if node_key_words and all(nw in all_user_forms for nw in node_key_words):
            return L1_STEM_QUALITY

    return 0.0


def advance_state_machine(
    session_ctx: dict,
    db: Session,
    current_topic: Topic,
    session_hits: set,
    task_packet: Optional[TaskPacket] = None,
):
    """
    推进对话阶段状态机。

    task_packet 参数（可选）：
    - 若提供，进入 CORE_TASK 时优先使用 TaskPacket 中的 target_nodes（LMS 决策）
    - 若未提供，降级为从 DB 随机采样（兼容旧流程）
    """
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
        used_ids = {n.get("id") for n in session_ctx["history_targets"] if n.get("id") is not None}

        if task_packet is not None:
            # ── LMS 主导：使用 TaskPacket 中的节点 ─────────────────────────
            available = [n for n in task_packet.all_practice_nodes if n.get("id") not in used_ids]
            session_ctx["new_targets"] = available[:3]  # 每轮最多 3 个新节点
            if not available:
                logger.warning("⚠️ TaskPacket 无可用目标节点，CORE_TASK 将作为普通对话进行。")
            else:
                logger.info(f"🔄 [推进] 进入核心考核（TaskPacket 模式）！本轮词: {[n.get('node_text') for n in session_ctx['new_targets']]}")
        else:
            # ── 兼容模式：从 DB 随机采样 ────────────────────────────────────
            all_nodes = db.query(TargetNode).filter(TargetNode.topic_id == current_topic.id).all() if current_topic else []
            available_db = [n for n in all_nodes if n.id not in used_ids]
            selected_nodes = random.sample(available_db, min(2, len(available_db))) if available_db else []
            session_ctx["new_targets"] = [{"id": n.id, "node_text": n.node_text} for n in selected_nodes]
            if not all_nodes:
                logger.warning("⚠️ Topic 无目标词，CORE_TASK 将作为普通聊天进行。")
            else:
                logger.info(f"🔄 [推进] 进入核心考核（兼容模式）！本轮新词: {[n.get('node_text') for n in session_ctx['new_targets']]}")

        transitioned = True

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