"""
Dialogue Engine - English Coach
核心状态机、计分与对话引擎模块。

[终极演进 - 主从分离架构 (Dual-LLM Actor-Director Model)]:
- LLM 1 (Actor): 纯粹的对话者，剥离所有 Tools，实现 TTFB < 1秒 的极速语音秒回。
- LLM 2 (Director/Evaluator): 后台旁路运行，调用强制 Tool，生成翻译/提示，并裁定是否推进状态机。
"""

import os
import json
import random
import logging
import re
import asyncio
import datetime
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from fastapi import WebSocket
import jinja2

from database import User, Topic, TargetNode, UserProgress
from domain.entities.task_packet import (
    TaskPacket,
    compute_max_reply_sentences,
    effective_learner_label,
)
from application.services.mastery_scorer import (
    update_mastery, L1_EXACT_QUALITY, L1_STEM_QUALITY,
    normalize_text, simple_stem,
)

logger = logging.getLogger("EnglishCoach")

# ================= 业务全局常量 =================

ROUNDS_PER_LEVEL = 3  # 每个段位需要完成的局数
MAX_TURNS_PER_PHASE = 25  # 兜底机制：每个阶段最大互动轮数，超时强制推进

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))  # python_backend/
GLOBAL_RULES_FILE_PATH = os.path.join(_BACKEND_DIR, "prompts", "global_rules.json")

EVENT_POOL = [
    "A colleague just texted the user asking them to add something specific to the order. Ask the user what their colleague wants.",
    "A different staff member enthusiastically interrupts to introduce a new seasonal item. Briefly act as this new character.",
    "There is a sudden mix-up with another customer's items. The user needs to describe exactly what they originally ordered to help you fix it.",
    "A system glitch means the user has to switch their payment method or split the bill. Ask them how they want to handle it."
]

# 闲聊得分的心流曲线 (非线性：前后慢，中间快。总和正好 40 分)
CHAT_SCORE_CURVE = [2.0, 3.0, 5.0, 8.0, 10.0, 6.0, 3.0, 2.0, 1.0]

# ================= Jinja2 模板引擎配置 =================
JINJA_ENV = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)

# 【主 LLM 演员静态模板】：只关注沉浸式角色扮演，彻底无格式负担
STATIC_SYSTEM_TEMPLATE = JINJA_ENV.from_string("""
You are an expert English Coach. 
Learner Level: {{ canonical_level }} | Practice content tier (target nodes): {{ depth_tier_val }}
Coach turn length cap: {{ max_reply_sentences }} short in-character sentences per reply.

[ROLE AND PERSONALITY]
{{ role_desc }}
{{ personality_desc }}

{% if cog_line %}
[COGNITIVE LOAD GUIDELINE]
{{ cog_line }}
{% endif %}

{% if universal_rules %}
[UNIVERSAL COACHING RULES]
{% for rule in universal_rules %}
{{ loop.index }}. {{ rule }}
{% endfor %}
{% endif %}

{% if scene_specific_rules %}
[SCENE-SPECIFIC RULES]
{% for rule in scene_specific_rules %}
- {{ rule }}
{% endfor %}
{% endif %}

{% if session_goal_line %}
[SESSION GOAL] 
{{ session_goal_line }}
{% endif %}

[OUTPUT BUDGET & QUESTION POLICY]
- Within your {{ max_reply_sentences }} sentence allowance: ask at most ONE question that expects an answer from the learner this turn.
- Do NOT put two answerable questions in the same sentence. 
- No bullet lists, no lecture-style multi-paragraph answers. Use plain conversational text only.
""")

# 【主 LLM 演员动态模板】：每轮的任务引导，剥离了所有 Tool Calling 命令
DYNAMIC_TURN_TEMPLATE = JINJA_ENV.from_string("""
[SYSTEM DIRECTIVE FOR CURRENT TURN]
Current Phase: {{ phase }}

{% if phase == 'ICE_BREAKING' %}
PHASE 1: ICE BREAKING (Small Talk)
Goal: Build rapport. Briefly greet the user and set the scene for {{ scene_name }}. Wrap up the greeting naturally when the user is ready.
{% elif phase == 'CORE_TASK' %}
PHASE 2: CORE TASK (Language Practice)
Goal: Guide the user through the main task of the {{ scene_name }} while focusing on practice.
Mandatory Practice: You MUST naturally guide the user to say these REMAINING target words: {{ unhit_targets }}.
Bonus Review: The user already used these words: {{ hit_targets }}. 
Action: Keep the conversation flowing naturally toward completing the task.
{% elif phase == 'EVENT_EXTENSION' %}
PHASE 3: EVENT EXTENSION (The Twist)
Goal: Introduce this complication: '{{ current_event }}'. Test user's problem-solving skills.
{% elif phase == 'WRAP_UP' %}
PHASE 4: WRAP UP (Conclusion)
Goal: Conclude naturally. Give one sentence of positive feedback and say a final goodbye.
{% endif %}

[CRITICAL INSTRUCTION]
Just speak your English reply. DO NOT output any tags, JSON, or tool calls. Reply as fast and naturally as possible.
""")

# 【副 LLM 导演评估模板】：专职负责翻译、提示与状态推进，高内聚高稳定
EVALUATOR_SYSTEM_TEMPLATE = JINJA_ENV.from_string("""
You are the backend AI Director for an English coaching application. 
You will be provided with the last exchange between the User and the AI Coach.

Your job is to strictly use the `submit_analysis_and_feedback` tool to output a JSON object containing:
1. `ai_translation_cn`: A natural Chinese translation of the AI Coach's English reply.
2. `suggested_hints_en`: 2 or 3 short English responses the User could say next.
3. `coach_correction_cn`: If the User made a severe grammar/vocabulary mistake in their text, correct it in Chinese. Otherwise, leave empty.
4. `should_advance_phase`: A boolean. Set to TRUE ONLY IF the AI Coach's reply strongly indicates that the current phase goal is fulfilled and the conversation is naturally transitioning.

Current Phase Rules for Evaluation:
- If current phase is ICE_BREAKING: Advance to CORE_TASK if the small talk is over and they are ready to start the main scenario.
- If current phase is CORE_TASK: Advance to EVENT_EXTENSION if the user has successfully completed the main task objective.
- If current phase is EVENT_EXTENSION: Advance to WRAP_UP if the complication/twist has been resolved.
- If current phase is WRAP_UP: Advance to ICE_BREAKING if the coach has said their final goodbye.

Current Phase: {{ phase }}
Scene: {{ scene_name }}
""")

# ================= 辅助工具 =================

def load_global_rules(file_path=GLOBAL_RULES_FILE_PATH):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"🚨 Could not load global_rules.json ({e}). Prompt will be degraded.")
        return {}


def _cognitive_load_line(rules: dict, canonical: str) -> Optional[str]:
    cog = rules.get("cognitive_load_levels") or {}
    line = cog.get(canonical)
    if line is not None:
        return str(line)
    return None


def _preload_new_targets_if_empty(
        db: Session,
        session_ctx: dict,
        task_packet: Optional[TaskPacket],
        topic_id: int,
) -> None:
    """本局 new_targets 为空时，从 TaskPacket 或 DB 预取最多 3 个节点（排除已在 history 中的 id）。"""
    if session_ctx.get("new_targets"):
        return
    used_ids = {n.get("id") for n in session_ctx.get("history_targets", []) if n.get("id") is not None}
    if task_packet is not None:
        available = [
            n for n in task_packet.all_practice_nodes
            if n.get("id") is not None and n.get("id") not in used_ids
        ]
        available.sort(key=lambda n: (int(n.get("depth_level") or 1), int(n.get("id") or 0)))
        session_ctx["new_targets"] = available[:3]
        if session_ctx["new_targets"]:
            logger.info(
                f"[L1] Preloaded new_targets from TaskPacket: "
                f"{[n.get('node_text') for n in session_ctx['new_targets']]}"
            )
        return
    if not topic_id:
        session_ctx["new_targets"] = []
        return
    all_nodes = db.query(TargetNode).filter(TargetNode.topic_id == topic_id).all()
    candidates = [n for n in all_nodes if n.id not in used_ids]
    candidates.sort(key=lambda n: (n.depth_level or 1, n.id))
    selected = candidates[:3]
    session_ctx["new_targets"] = [
        {
            "id": n.id,
            "node_text": n.node_text,
            "node_type": n.node_type,
            "depth_level": n.depth_level,
        }
        for n in selected
    ]
    if session_ctx["new_targets"]:
        logger.info(
            f"[L1] Preloaded new_targets from DB: "
            f"{[n.get('node_text') for n in session_ctx['new_targets']]}"
        )


# ================= 核心提示词构建 (动静分离) =================

def build_prompts(
        user: Optional[User],
        is_flipped: bool,
        session_ctx: dict,
        task_packet: Optional[TaskPacket] = None,
        session_hits: Optional[set] = None,
) -> Tuple[str, str]:
    """构建发给主 LLM（演员）的 Prompt"""
    if task_packet is None:
        raise ValueError("build_prompts requires a non-None TaskPacket. "
                         "Callers must ensure task_packet is set before invoking this function.")

    rules = load_global_rules()
    if session_hits is None:
        session_hits = set()

    # 1. 提取基础变量
    scene_name = task_packet.scene_prompt
    role_name = task_packet.role_name
    user_level = task_packet.learner_level
    scene_specific_rules = task_packet.scene_specific_rules
    depth_tier_val = int(task_packet.depth_tier or 1)
    session_goal_line = (f"\n[SESSION GOAL] {task_packet.session_goal}" if task_packet.session_goal else "")

    settings_dict = (user.settings or {}) if user and getattr(user, "settings", None) else None
    canonical_level = effective_learner_label(settings_dict, task_packet.learner_level if task_packet else None,
                                              user_level)
    max_reply_sentences = compute_max_reply_sentences(canonical_level, depth_tier_val)

    logger.info(
        f"[Prompt] canonical_level={canonical_level} tier={depth_tier_val} "
        f"max_reply={max_reply_sentences} phase={session_ctx.get('phase', 'ICE_BREAKING')}"
    )

    phase = session_ctx.get("phase", "ICE_BREAKING")

    # 角色与性格
    role_desc = (
        f"You are acting as: {role_name} in a {scene_name} setting."
        if not is_flipped
        else f"You are the CUSTOMER/USER. The user is acting as the {role_name}."
    )
    personality_levels = rules.get("personality_levels", {})
    politeness_key = str(user.politeness_level) if user else "1"
    personality_desc = personality_levels.get(politeness_key, personality_levels.get("1", ""))

    # 认知负荷与全局规则
    cog_line = _cognitive_load_line(rules, canonical_level)
    if not cog_line:
        cog_line = _cognitive_load_line(rules, "Intermediate")

    by_level = rules.get("universal_rules_by_level") or {}
    universal_rules = by_level.get(canonical_level) or rules.get("universal_rules", [])

    # 2. 准备动态变量 (打靶词汇与事件)
    new_targets_list = session_ctx.get("new_targets", [])
    history_targets_list = session_ctx.get("history_targets", [])
    all_active = new_targets_list + history_targets_list

    unhit_nodes = [n for n in all_active if n.get("id") is not None and n["id"] not in session_hits]
    hit_nodes = [n for n in all_active if n.get("id") is not None and n["id"] in session_hits]

    unhit_targets = ", ".join(
        [f"'{t}'" for t in (n.get("node_text") for n in unhit_nodes) if t]) or "(none — great job)"
    hit_targets = ", ".join([f"'{t}'" for t in (n.get("node_text") for n in hit_nodes) if t]) or "(none yet)"
    current_event = session_ctx.get("current_event", "There is a small problem with your request.")

    # 3. 渲染静态模板
    static_system_prompt = STATIC_SYSTEM_TEMPLATE.render(
        canonical_level=canonical_level,
        depth_tier_val=depth_tier_val,
        max_reply_sentences=max_reply_sentences,
        role_desc=role_desc,
        personality_desc=personality_desc,
        cog_line=cog_line,
        universal_rules=universal_rules,
        scene_specific_rules=scene_specific_rules,
        session_goal_line=session_goal_line,
    )

    # 4. 渲染动态模板
    dynamic_turn_prompt = DYNAMIC_TURN_TEMPLATE.render(
        phase=phase,
        scene_name=scene_name,
        unhit_targets=unhit_targets,
        hit_targets=hit_targets,
        current_event=current_event,
    )

    return static_system_prompt, dynamic_turn_prompt


def build_evaluator_prompt(session_ctx: dict, task_packet: Optional[TaskPacket] = None) -> str:
    """构建发给旁路副 LLM（导演）的系统 Prompt"""
    phase = session_ctx.get("phase", "ICE_BREAKING")
    scene_name = task_packet.scene_prompt if task_packet else "Conversation"
    return EVALUATOR_SYSTEM_TEMPLATE.render(phase=phase, scene_name=scene_name)


# ================= 核心计分与状态机 =================

async def evaluate_and_check_progress(db: Session, user_id: str, _topic_id: int, user_text: str, session_hits: set,
                                      session_ctx: dict, websocket: WebSocket, ws_lock: asyncio.Lock,
                                      task_packet: Optional[TaskPacket] = None):
    """
    L1 评估层：轻量同步，每轮对话触发。
    保留原版词干提取、计分逻辑和 WS 进度推送。
    """
    try:
        _preload_new_targets_if_empty(db, session_ctx, task_packet, _topic_id)

        phase = session_ctx.get("phase", "ICE_BREAKING")

        # --- 计分模块 1：闲聊分 (40%) ---
        if phase in ["ICE_BREAKING", "EVENT_EXTENSION", "WRAP_UP"]:
            if user_text.strip():
                chat_idx = session_ctx.get("chat_interaction_count", 0)
                if chat_idx < len(CHAT_SCORE_CURVE):
                    added_score = CHAT_SCORE_CURVE[chat_idx]
                    session_ctx["chat_score"] = min(40.0, session_ctx.get("chat_score", 0.0) + added_score)
                    session_ctx["chat_interaction_count"] = chat_idx + 1
                    logger.info(
                        f"[L1] Chat curve step {chat_idx + 1} (+{added_score:.1f}) | total={session_ctx['chat_score']:.1f}/40")

        # --- 计分模块 2：核心任务 L1 命中检测 (60%) ---
        new_targets = session_ctx.get("new_targets", [])
        history_targets = session_ctx.get("history_targets", [])
        all_active_targets = new_targets + history_targets

        if user_text.strip() and all_active_targets:
            denom = max(1, len(new_targets) if new_targets else len(all_active_targets))
            points_per_new_word = 60.0 / denom

            user_normalized = normalize_text(user_text)
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
                    added_task *= quality
                    session_ctx["task_score"] = min(60.0, session_ctx.get("task_score", 0.0) + added_task)
                    logger.info(f"[L1] Hit '{node_text}' quality={quality:.2f} +{added_task:.1f}pts")

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
                    progress.last_practiced_at = datetime.datetime.utcnow()

                db.commit()

        # --- 计分模块 3：计算并下发总进度 ---
        completed_rounds = session_ctx.get("completed_rounds_in_level", 0)
        current_round_score = session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0)
        overall_progress = min(100.0, ((completed_rounds * 100.0) + current_round_score) / float(ROUNDS_PER_LEVEL))

        last_sent = session_ctx.get("_ws_last_progress")
        skip_ws = (
                phase == "ICE_BREAKING"
                and last_sent is not None
                and abs(overall_progress - float(last_sent)) < 2.5
        )

        try:
            if not skip_ws:
                async with ws_lock:
                    await websocket.send_text(json.dumps({
                        "event": "topic_mastery_reached",
                        "progress": overall_progress,
                        "level": session_ctx.get("current_level", 1),
                        "next_topic_suggestion": ""
                    }))
                session_ctx["_ws_last_progress"] = overall_progress
        except Exception as e:
            logger.error(f"Ws send progress error: {e}")

    except Exception as e:
        logger.error(f"evaluate_and_check_progress error: {e}", exc_info=True)


def _l1_match_quality(node_normalized: str, user_normalized: str) -> float:
    """
    L1 节点命中检测，返回质量分 0~1（0 = 未命中）。
    """
    pattern_exact = rf"(?<!\w){re.escape(node_normalized)}(?!\w)"
    if re.search(pattern_exact, user_normalized):
        return L1_EXACT_QUALITY

    node_words = node_normalized.split()
    if len(node_words) <= 3:
        user_word_set = {w for w in user_normalized.split() if len(w) >= 3}
        user_stem_set = {simple_stem(w) for w in user_word_set}
        all_user_forms = user_word_set | user_stem_set

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
    [演进]：根据副 LLM 异步传来的 llm_wants_to_advance 标志位推进状态机。
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
        logger.info(
            "🔄 [推进] ICE_BREAKING -> CORE_TASK（沿用预加载词表）: "
            f"{[n.get('node_text') for n in session_ctx.get('new_targets', [])]}"
        )
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
            old_new = session_ctx.get("new_targets", [])
            session_ctx["history_targets"] = session_ctx.get("history_targets", []) + old_new
            session_ctx["new_targets"] = []
            logger.info(
                f"🔄 [状态机结算] 进入本段位第 {session_ctx['completed_rounds_in_level'] + 1} 轮；"
                f"已合并 {len(old_new)} 个节点到 history，等待预加载新词。"
            )

        transitioned = True

    return transitioned