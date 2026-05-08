"""
Dialogue Engine - English Coach
核心状态机、计分与对话引擎模块。

[阶段二演进 — 双轨校验微场景图谱架构]:
- LLM 1 (Actor): 纯粹的对话者，剥离所有 Tools，实现 TTFB < 1秒 的极速语音秒回。
- LLM 2 (Director/Evaluator): 后台旁路运行，调用强制 Tool，生成翻译/提示，
  并进行双轨校验 (Intent + Constraints) 决定是否推进微场景流转。

双模式共存：
- micro_mode=True: 基于 turn_count_in_scenario 的渐进诱导 + 双轨校验
- micro_mode=False: 维持原四阶段线性状态机（向后兼容）
"""

import os
import json
import random
import logging
import re
import asyncio
import datetime
from typing import Optional, Tuple, Any
from sqlalchemy.orm import Session
from fastapi import WebSocket
import jinja2

from database import User, Topic, TargetNode, UserProgress
from domain.entities.task_packet import (
    TaskPacket,
    compute_max_reply_sentences,
    effective_learner_label,
    ScenarioConstraintItem,
)
from application.services.mastery_scorer import (
    update_mastery, L1_EXACT_QUALITY, L1_STEM_QUALITY,
    normalize_text, simple_stem,
)

logger = logging.getLogger("EnglishCoach")

# ================= 业务全局常量 =================

ROUNDS_PER_LEVEL = 3
MAX_TURNS_PER_PHASE = 25

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
GLOBAL_RULES_FILE_PATH = os.path.join(_BACKEND_DIR, "prompts", "global_rules.json")

EVENT_POOL = [
    "A colleague just texted the user asking them to add something specific to the order. Ask the user what their colleague wants.",
    "A different staff member enthusiastically interrupts to introduce a new seasonal item. Briefly act as this new character.",
    "There is a sudden mix-up with another customer's items. The user needs to describe exactly what they originally ordered to help you fix it.",
    "A system glitch means the user has to switch their payment method or split the bill. Ask them how they want to handle it."
]

CHAT_SCORE_CURVE = [2.0, 3.0, 5.0, 8.0, 10.0, 6.0, 3.0, 2.0, 1.0]

# Depth Level → CEFR 难度压制级别映射
DEPTH_TO_CEFR = {
    1: "Beginner",
    2: "Elementary",
    3: "Advanced",
}

# Depth Level → 极简词汇强制压制指令（追加到 cognitive_load_levels 之后）
DEPTH_FORCE_RULES = {
    1: (
        "MANDATORY RESTRICTIONS for Depth Level 1 (Beginner/A1):\n"
        "- Use ONLY single-word or very short two-word phrases.\n"
        "- Avoid ALL compound sentences, subclauses, and complex structures.\n"
        "- Never use words longer than 6 letters unless the scene demands it.\n"
        "- Keep each sentence to under 10 words total."
    ),
    2: (
        "MANDATORY RESTRICTIONS for Depth Level 2 (Elementary/A2):\n"
        "- Prefer short simple sentences with one main clause.\n"
        "- Use common daily vocabulary only.\n"
        "- Avoid idioms, slang, or advanced academic expressions."
    ),
    3: (
        "MANDATORY RESTRICTIONS for Depth Level 3 (Advanced/B2-C1+):\n"
        "- Encourage idiomatic expressions and nuanced vocabulary.\n"
        "- Allow complex sentence structures when naturally appropriate.\n"
        "- Support advanced topic-specific terminology."
    ),
}

# ================= 统一入口辅助函数 =================

def is_micro_mode(task_packet: Optional[TaskPacket]) -> bool:
    """
    【修复】统一判断是否启用微场景模式。

    避免在多处重复写 has_constraints() 判断，确保 micro_mode 判断一致。
    这样 Director 信号存活链路中不会因为 micro_mode 判断不一致而跳过信号分支。
    """
    if task_packet is None:
        return False
    return task_packet.has_constraints()


# ================= Jinja2 模板引擎配置 =================
JINJA_ENV = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)

# 【主 LLM 演员静态模板 V1】：旧模式（向后兼容）
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

# 【主 LLM 演员动态模板 V1】：旧模式（向后兼容）
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

# 【主 LLM 演员静态模板 V2】：微场景模式（阶段二）
STATIC_SYSTEM_TEMPLATE_V2 = JINJA_ENV.from_string("""
You are an expert English Coach.
Learner Level: {{ canonical_level }} | Practice content tier: {{ depth_tier_val }}
Coach turn length cap: {{ max_reply_sentences }} short in-character sentences per reply.

[ROLE AND PERSONALITY]
{{ role_desc }}
{{ personality_desc }}

{% if cog_line %}
[COGNITIVE LOAD GUIDELINE]
{{ cog_line }}
{% endif %}

{% if cefr_force_block %}
[CEFR DIFFICULTY ENFORCEMENT — {{ cefr_level }}]
{{ cefr_force_block }}
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

{% if micro_scene_block %}
[MICRO-SCENE CONTEXT]
Scenario: {{ scenario_name }}
Teaching Intent: {{ current_intent }}
{% if unhit_constraints %}
TARGET EXPRESSIONS (learner should naturally use at least one): {{ unhit_constraints }}
{% endif %}
{% if hit_constraints %}
ALREADY PRACTICED (these were used successfully before — do not force repetition): {{ hit_constraints }}
{% endif %}
{% endif %}

[OUTPUT BUDGET & QUESTION POLICY]
- Within your {{ max_reply_sentences }} sentence allowance: ask at most ONE question that expects an answer from the learner this turn.
- Do NOT put two answerable questions in the same sentence.
- No bullet lists, no lecture-style multi-paragraph answers. Use plain conversational text only.
""")

# 【主 LLM 演员动态模板 V2】：微场景模式渐进诱导
DYNAMIC_TURN_V2_TEMPLATE = JINJA_ENV.from_string("""
{% if turn_count == 1 %}
[SITUATION — TURN {{ turn_count }} / {{ max_turns }}]
{{ scene_desc }}

Action: Start the conversation naturally and set the scene for the learner.
Give them space to respond. Do NOT push practice vocabulary yet.
{% elif turn_count == 2 %}
[SITUATION — TURN {{ turn_count }} / {{ max_turns }}]
{{ scene_desc }}

{% if current_constraint %}
Action: If the user has NOT yet naturally used the target expression '{{ current_constraint }}',
use a gentle prompt that makes saying it feel like the most natural choice.
For example, offer a simple yes/no or choice question that leads toward the target word.
DO NOT directly mention or spell out the target expression.
{% else %}
Action: The user seems engaged. Continue naturally guiding the conversation.
{% endif %}
{% else %}
[SITUATION — TURN {{ turn_count }} / {{ max_turns }} — RESCUE MODE]
{{ scene_desc }}

{% if current_constraint %}
Action: The learner has not yet naturally used the target expression.
You MUST guide them to it more directly. Choose ONE of the following:
  1. Complete their sentence and ask them to repeat: "You can say: '{{ current_constraint }}'"
  2. Offer a forced-choice: "Do you mean small, medium, or large?"
  3. Model the phrase clearly and invite repetition: "Could you try saying: '{{ current_constraint }}'?"

Do NOT give up. Keep encouraging them until they succeed.
{% else %}
Action: The learner seems stuck. Gently help them move forward with a simple rephrasing.
{% endif %}
{% endif %}

[CRITICAL INSTRUCTION]
Reply with ONLY your in-character English dialogue. No tags, no JSON, no tool calls.
""")

# 【副 LLM 导演评估模板 V1】：旧模式（向后兼容）
EVALUATOR_SYSTEM_TEMPLATE = JINJA_ENV.from_string("""
You are the backend AI Director for an English coaching application.
You will be provided with the last exchange between the User and the AI Coach.

Your job is to strictly use the `submit_analysis_and_feedback` tool to output a JSON object containing:
1. `ai_translation_cn`: A natural (not literal) Chinese translation of the AI Coach's English reply.
2. `suggested_hints_en`: 2 short English replies the User could say next.
   - Each hint must be 3-8 words. No full sentences over 8 words.
   - Match the learner's level ({{ learner_level }}):
     * Beginner: simple 2-4 word phrases
     * Intermediate: natural 4-7 word responses
     * Advanced: idiomatic or complex 6-10 word replies
   {% if vocab_tags %}
   - Relevance rule: at least 1 of 3 hints must naturally use a word from the scene vocabulary ({{ vocab_tags | join(', ') }}) if provided.
   {% endif %}
   {% if sentence_patterns %}
   - Relevance rule: at least 1 of 3 hints should follow a sentence pattern like ({{ sentence_patterns | join(' | ') }}) if provided.
   {% endif %}
   - Variety rule: do not give 3 hints that all start the same way (e.g. "I would like...", "I want...", "Can I have...").
   - Format: string array, e.g. ["Sure.", "That sounds good.", "Let me think."]
3. `coach_correction_cn`: If the User made a severe grammar/vocabulary mistake in their text, correct it in Chinese. Otherwise, leave empty.
4. `should_advance_phase`: A boolean. Set to TRUE ONLY IF the AI Coach's reply strongly indicates that the current phase goal is fulfilled and the conversation is naturally transitioning.

Current Phase Rules for Evaluation:
- If current phase is ICE_BREAKING: Advance to CORE_TASK if the small talk is over and they are ready to start the main scenario.
- If current phase is CORE_TASK: Advance to EVENT_EXTENSION if the user has successfully completed the main task objective.
- If current phase is EVENT_EXTENSION: Advance to WRAP_UP if the complication/twist has been resolved.
- If current phase is WRAP_UP: Advance to ICE_BREAKING if the coach has said their final goodbye.

Current Phase: {{ phase }}
Scene: {{ scene_name }}
Learner Level: {{ learner_level }}
{% if sentence_patterns %}
[HELPFUL SENTENCE STARTERS — pick from these patterns when generating hints]
{% for pattern in sentence_patterns %}
- {{ pattern }}
{% endfor %}
{% endif %}
""")

# 【副 LLM 导演评估模板 V2】：微场景双轨校验模式
# 隐患1修复：在模板中注入 hit_constraints，防止"视野失忆"
EVALUATOR_V2_TEMPLATE = JINJA_ENV.from_string("""
You are the backend AI Director for an English coaching application.
You will be provided with the last exchange between the User and the AI Coach.

Your job is to use the `submit_analysis_and_feedback` tool to output a JSON object containing:

1. `ai_translation_cn`: Natural Chinese translation of the AI Coach's reply (not literal).

2. `suggested_hints_en`: 2 short replies the User could say next.
   - Beginner: 2-4 word phrases only
   - Intermediate: 4-7 word responses
   - Advanced: 6-10 word idiomatic responses
   - At least 1 hint should naturally use a word from the target list.
   - Format: string array, e.g. ["Sure.", "That sounds good."]

3. `coach_correction_cn`: If the user made a grammar/vocabulary error, correct it in Chinese.
   Otherwise leave empty.

=== DUAL-TRACK VALIDATION ===

4. `intent_achieved`: BOOLEAN
   Set to TRUE if the user's reply demonstrates that they have fulfilled the teaching intent:
   "{{ current_intent }}"
   Consider: Did they meaningfully engage with the scenario goal? Was the conversation productive?

5. `constraints_hit`: BOOLEAN
   IMPORTANT — Prevailing Rule: Check BOTH this turn AND the conversation history.
   Set to TRUE if the user has EVER naturally used AT LEAST ONE of these target expressions:
   {% for c in constraint_texts %}
   - "{{ c }}"{% endfor %}
   {% if hit_constraints %}
   NOTE — Already Practiced (do NOT penalize if absent this turn):
   {{ hit_constraints }}
   If ANY of the above were used in PREVIOUS turns, this should be TRUE.
   {% endif %}
   "Naturally" means they used the expression in context, not just in a forced repetition.
   "This turn OR history" — as long as the constraint was used at some point, return TRUE.

6. `constraints_hit_details`: ARRAY of objects
   For each constraint that was hit (this turn OR history), report:
   [
     {
       "constraint_text": "...",
       "quality": 0.0-1.0,
       "note": "..."
     }
   ]
   - quality 1.0 = exact use in perfect context
   - quality 0.8 = stem/close variant in good context
   - quality 0.0 = not hit yet

7. `scenario_completed`: BOOLEAN
   Set to TRUE ONLY when BOTH:
     intent_achieved == TRUE AND constraints_hit == TRUE.
   This signals the engine to trigger micro-scenario transition.

Learner Level: {{ learner_level }}
Current Scenario: {{ scenario_name }}
""")

# ================= 辅助工具 =================

def load_global_rules(file_path: str = GLOBAL_RULES_FILE_PATH) -> dict:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Could not load global_rules.json ({e}). Prompt will be degraded.")
        return {}


def _cognitive_load_line(rules: dict, canonical: str) -> Optional[str]:
    cog = rules.get("cognitive_load_levels") or {}
    line = cog.get(canonical)
    if line is not None:
        return str(line)
    return None


def _build_cefr_force_block(rules: dict, depth_level: int) -> str:
    """根据 depth_level 追加极简词汇强制压制指令"""
    cefr_level = DEPTH_TO_CEFR.get(depth_level, "Elementary")
    base_cog = _cognitive_load_line(rules, cefr_level) or ""
    force_rules = DEPTH_FORCE_RULES.get(depth_level, "")
    if force_rules:
        return f"{base_cog}\n\n{force_rules}"
    return base_cog


def _get_constraint_id(constraint: Any) -> Any:
    """提取约束的 ID，支持新旧两种格式"""
    if isinstance(constraint, ScenarioConstraintItem):
        return constraint.constraint_id
    if isinstance(constraint, dict):
        return constraint.get("constraint_id", constraint.get("id"))
    return None


def _get_constraint_text(constraint: Any) -> str:
    """提取约束的文本，支持新旧两种格式"""
    if isinstance(constraint, ScenarioConstraintItem):
        return constraint.constraint_text
    if isinstance(constraint, dict):
        return constraint.get("constraint_text", constraint.get("node_text", ""))
    return str(constraint) if constraint else ""


def _preload_new_targets_if_empty(
        db: Session,
        session_ctx: dict,
        task_packet: Optional[TaskPacket],
        topic_id: int,
) -> None:
    """本局 new_targets 为空时，从 TaskPacket 或 DB 预取最多 3 个节点（排除已在 history 中的 id）"""
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
                f"{[n.get('node_text') or n.get('constraint_text', '') for n in session_ctx['new_targets']]}"
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
) -> Tuple[str, str]:
    """
    构建发给主 LLM（演员）的 Prompt。

    双模式自动选择：
    - micro_mode=True: 微场景模式，使用 V2 模板
    - micro_mode=False: 旧四阶段模式，使用 V1 模板
    """
    if task_packet is None:
        logger.warning(
            "[Prompt] task_packet is None — returning fallback prompt. "
            "Callers should ensure task_packet is set before build_prompts."
        )
        return (
            "You are an English conversation partner. Please have a friendly chat in English.",
            "Hello! How are you today? What would you like to practice?"
        )

    rules = load_global_rules()

    # 判断运行模式
    micro_mode = is_micro_mode(task_packet)

    # 基础变量
    scene_name = task_packet.scene_prompt
    role_name = task_packet.role_name
    user_level = task_packet.learner_level
    scene_specific_rules = task_packet.scene_specific_rules
    depth_level = int(task_packet.depth_tier or 1)

    settings_dict = (user.settings or {}) if user and getattr(user, "settings", None) else None
    canonical_level = effective_learner_label(
        settings_dict,
        task_packet.learner_level,
        user_level,
    )
    max_reply_sentences = compute_max_reply_sentences(canonical_level, depth_level)

    logger.info(
        f"[Prompt] mode={'micro' if micro_mode else 'legacy'} "
        f"canonical_level={canonical_level} depth={depth_level} "
        f"max_reply={max_reply_sentences} "
        f"turn={session_ctx.get('turn_count_in_scenario', 1)}"
    )

    # 角色与性格
    role_desc = (
        f"You are acting as: {role_name} in a {scene_name} setting."
        if not is_flipped
        else f"You are the CUSTOMER/USER. The user is acting as the {role_name}."
    )
    personality_levels = rules.get("personality_levels", {})
    politeness_key = str(user.politeness_level) if user else "1"
    personality_desc = personality_levels.get(politeness_key, personality_levels.get("1", ""))

    # 认知负荷
    cog_line = _cognitive_load_line(rules, canonical_level)
    if not cog_line:
        cog_line = _cognitive_load_line(rules, "Intermediate")

    # 全局规则
    by_level = rules.get("universal_rules_by_level") or {}
    universal_rules = by_level.get(canonical_level) or rules.get("universal_rules", [])

    # 会话目标
    session_goal_line = (
        f"\n[SESSION GOAL] {task_packet.session_goal}"
        if task_packet.session_goal
        else ""
    )

    # 微场景变量
    cefr_level = DEPTH_TO_CEFR.get(depth_level, "Elementary")
    cefr_force_block = _build_cefr_force_block(rules, depth_level) if micro_mode else ""

    scenario_name_str = (
        task_packet.current_scenario.scenario_name
        if task_packet.current_scenario and task_packet.current_scenario.scenario_name
        else scene_name
    )
    current_intent_str = (
        task_packet.current_intent or task_packet.session_goal
    )
    scene_desc_str = (
        task_packet.current_scenario.scene_desc
        if task_packet.current_scenario and task_packet.current_scenario.scene_desc
        else scene_name
    )
    max_turns = (
        task_packet.current_scenario.max_turns
        if task_packet.current_scenario
        else 8
    )

    # 渲染静态模板
    # 统一使用 session_ctx["_constraint_hits"] 作为唯一数据源
    constraint_hits = session_ctx.get("_constraint_hits", set())
    # 【修复】防止 from_dict 反序列化后 _constraint_hits 变成 MISSING_TYPE
    if not isinstance(constraint_hits, set):
        constraint_hits = set()
        session_ctx["_constraint_hits"] = constraint_hits

    # 默认值，micro_mode 时会被覆盖
    current_constraint_str = ""

    if micro_mode:
        # 约束命中状态
        all_constraints = task_packet.constraints + task_packet.review_constraints
        unhit = [
            c for c in all_constraints
            if _get_constraint_id(c) not in constraint_hits
        ]
        hit = [
            c for c in all_constraints
            if _get_constraint_id(c) in constraint_hits
        ]
        unhit_str = ", ".join(
            f"'{_get_constraint_text(c)}'" for c in unhit
        ) or "(none — great job)"
        hit_str = ", ".join(
            f"'{_get_constraint_text(c)}'" for c in hit
        ) or "(none yet)"
        current_constraint_str = _get_constraint_text(unhit[0]) if unhit else ""

        static_prompt = STATIC_SYSTEM_TEMPLATE_V2.render(
            canonical_level=canonical_level,
            depth_tier_val=depth_level,
            max_reply_sentences=max_reply_sentences,
            role_desc=role_desc,
            personality_desc=personality_desc,
            cog_line=cog_line,
            cefr_force_block=cefr_force_block,
            cefr_level=cefr_level,
            universal_rules=universal_rules,
            scene_specific_rules=scene_specific_rules,
            session_goal_line=session_goal_line,
            micro_scene_block=bool(task_packet.current_scenario),
            scenario_name=scenario_name_str,
            current_intent=current_intent_str,
            unhit_constraints=unhit_str,
            hit_constraints=hit_str,
        )

    else:
        # Legacy mode: use original template without constraint tracking
        static_prompt = STATIC_SYSTEM_TEMPLATE.render(
            canonical_level=canonical_level,
            depth_tier_val=depth_level,
            max_reply_sentences=max_reply_sentences,
            role_desc=role_desc,
            personality_desc=personality_desc,
            cog_line=cog_line,
            cefr_force_block=cefr_force_block,
            cefr_level=cefr_level,
            universal_rules=universal_rules,
            scene_specific_rules=scene_specific_rules,
            session_goal_line=session_goal_line,
        )

    # 渲染动态模板
    turn_count = session_ctx.get("turn_count_in_scenario", 1)
    dynamic_prompt = DYNAMIC_TURN_V2_TEMPLATE.render(
        turn_count=turn_count,
        max_turns=max_turns,
        scene_desc=scene_desc_str,
        current_constraint=current_constraint_str,
    )

    return static_prompt, dynamic_prompt


def build_evaluator_prompt(
    session_ctx: dict,
    task_packet: Optional[TaskPacket] = None,
) -> str:
    """
    构建发给旁路副 LLM（导演）的系统 Prompt。
    使用微场景双轨校验模板 EVALUATOR_V2_TEMPLATE。

    【修复 Prompt 污染】：排除本轮刚命中的约束，防止 Director 误判。
    """
    learner_level = task_packet.learner_level if task_packet else "Intermediate"
    all_constraints = (task_packet.constraints + task_packet.review_constraints) if task_packet else []
    constraint_texts = [_get_constraint_text(c) for c in all_constraints]

    # 获取已命中约束文本（用于注入 Evaluator，防止视野失忆）
    constraint_hits_set = session_ctx.get("_constraint_hits", set())
    # 【修复】防止 from_dict 反序列化后 _constraint_hits 变成 MISSING_TYPE
    if not isinstance(constraint_hits_set, set):
        constraint_hits_set = set()

    # 【修复 Prompt 污染】：排除本轮刚命中的约束
    # 本轮刚命中的 ID 不能告诉 Director，否则 Director 会认为"用户本轮没说也没关系"
    current_turn_hits = session_ctx.get("_current_turn_hits", set())
    if not isinstance(current_turn_hits, set):
        current_turn_hits = set()

    # 历史命中 = 总命中 - 本轮命中
    history_hit_ids = constraint_hits_set - current_turn_hits

    hit_constraints_list = [
        _get_constraint_text(c)
        for c in all_constraints
        if _get_constraint_id(c) in history_hit_ids
    ]
    hit_constraints_str = ", ".join(f"'{t}'" for t in hit_constraints_list) if hit_constraints_list else ""

    scenario_name_str = (
        task_packet.current_scenario.scenario_name
        if task_packet.current_scenario and task_packet.current_scenario.scenario_name
        else (task_packet.scene_prompt if task_packet else "General Conversation")
    )
    current_intent_str = (
        task_packet.current_intent or task_packet.session_goal or "Complete the conversation naturally."
    )

    return EVALUATOR_V2_TEMPLATE.render(
        learner_level=learner_level,
        scenario_name=scenario_name_str,
        current_intent=current_intent_str,
        constraint_texts=constraint_texts,
        hit_constraints=hit_constraints_str,
    )


# ================= Director 信号解析（阶段二新增）====================

def parse_director_signal(director_json: dict) -> dict:
    """
    解析 Director LLM 的 JSON 输出，返回标准化信号字典。

    格式：
    {
        "ai_translation_cn": "...",
        "suggested_hints_en": ["...", "..."],
        "coach_correction_cn": "...",
        "intent_achieved": bool,
        "constraints_hit": bool,
        "scenario_completed": bool,
        "constraints_hit_details": [...],
        ...
    }
    """
    result = {
        "ai_translation_cn": director_json.get("ai_translation_cn", ""),
        "suggested_hints_en": director_json.get("suggested_hints_en", []),
        "coach_correction_cn": director_json.get("coach_correction_cn", ""),
    }

    # 检测双轨信号
    intent_achieved = bool(director_json.get("intent_achieved", False))
    constraints_hit = bool(director_json.get("constraints_hit", False))
    scenario_completed = intent_achieved and constraints_hit
    result.update({
        "intent_achieved": intent_achieved,
        "constraints_hit": constraints_hit,
        "scenario_completed": scenario_completed,
        "constraints_hit_details": director_json.get("constraints_hit_details", []),
    })

    return result


def check_scenario_completion(
    session_ctx: dict,
    task_packet: Optional[TaskPacket],
    director_signal: Optional[dict] = None,
) -> bool:
    """
    判断是否触发微场景通关。

    通关条件（二选一）：
    1. Director 双轨信号均为 True（需要 intent AND constraints 都满足）
    2. 轮数超限（max_turns exceeded，强制通关保底）

    注意：约束命中只用于教学反馈，不直接触发通关。

    Returns:
        True if scenario should complete and trigger transition.
    """
    micro_mode = is_micro_mode(task_packet) if task_packet else False
    if not micro_mode:
        return False

    # 条件1：Director 双轨信号
    if director_signal and director_signal.get("scenario_completed"):
        logger.info("[SCENARIO] 双轨校验通过，触发微场景通关")
        return True

    # 条件2：轮数超限
    turn_count = session_ctx.get("turn_count_in_scenario", 0)
    max_turns = (
        task_packet.current_scenario.max_turns
        if task_packet and task_packet.current_scenario
        else 8
    )
    if turn_count >= max_turns:
        logger.info(f"[SCENARIO] 轮数超限（{turn_count}>={max_turns}），强制通关")
        return True

    return False


# ================= 核心计分与状态机 =================

async def evaluate_and_check_progress(
    db: Session,
    user_id: str,
    topic_id: int,
    user_text: str,
    session_ctx: dict,
    websocket: WebSocket,
    ws_lock: asyncio.Lock,
    task_packet: Optional[TaskPacket] = None,
):
    """
    L1 评估层：每轮对话触发。

    微场景模式（L1 约束命中检测）：
    - 遍历 constraints（含 review_constraints）
    - 使用 normalize_text + simple_stem 匹配
    - 更新 _constraint_hits 和 task_score
    - 推送 WebSocket 进度
    """
    try:
        _preload_new_targets_if_empty(db, session_ctx, task_packet, topic_id)

        micro_mode = is_micro_mode(task_packet) if task_packet else False
        phase = session_ctx.get("phase", "ICE_BREAKING")

        # 闲聊分（所有模式共享）
        if user_text.strip():
            chat_idx = session_ctx.get("chat_interaction_count", 0)
            if chat_idx < len(CHAT_SCORE_CURVE):
                added_score = CHAT_SCORE_CURVE[chat_idx]
                session_ctx["chat_score"] = min(
                    40.0, session_ctx.get("chat_score", 0.0) + added_score
                )
                session_ctx["chat_interaction_count"] = chat_idx + 1
                logger.info(
                    f"[L1] Chat curve step {chat_idx + 1} (+{added_score:.1f}) "
                    f"| total={session_ctx['chat_score']:.1f}/40"
                )

        # 微场景模式：L1 约束命中检测
        if micro_mode and user_text.strip():
            all_constraints = task_packet.constraints + task_packet.review_constraints
            _check_constraint_hits(
                db, user_id, user_text, all_constraints,
                session_ctx,
            )

        # WebSocket 进度推送
        completed_rounds = session_ctx.get("completed_rounds_in_level", 0)
        current_round_score = session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0)
        overall_progress = min(
            100.0,
            ((completed_rounds * 100.0) + current_round_score) / float(ROUNDS_PER_LEVEL)
        )

        last_sent = session_ctx.get("_ws_last_progress")
        skip_ws = (
            phase == "ICE_BREAKING"
            and last_sent is not None
            and abs(overall_progress - float(last_sent)) < 2.5
        )

        if not skip_ws:
            try:
                async with ws_lock:
                    await websocket.send_text(json.dumps({
                        "event": "topic_mastery_reached",
                        "progress": overall_progress,
                        "level": session_ctx.get("current_level", 1),
                        "turn_in_scenario": session_ctx.get("turn_count_in_scenario", 0),
                        "next_topic_suggestion": "",
                    }))
                session_ctx["_ws_last_progress"] = overall_progress
            except Exception as e:
                logger.error(f"Ws send progress error: {e}")

    except Exception as e:
        logger.error(f"evaluate_and_check_progress error: {e}", exc_info=True)


def _check_constraint_hits(
    db: Session,
    user_id: str,
    user_text: str,
    all_constraints: list,
    session_ctx: dict,
) -> set:
    """
    L1 约束命中检测（微场景模式专用）。

    遍历 ScenarioConstraintItem 列表，使用 normalize_text + simple_stem 匹配。
    命中的 constraint_id 写入 session_ctx["_constraint_hits"]。

    Returns:
        新命中的 constraint_id 集合（用于 Prompt 污染修复）
    """
    user_normalized = normalize_text(user_text)
    constraint_hits_set = session_ctx.get("_constraint_hits", set())
    # 【修复】防止 from_dict 反序列化后 _constraint_hits 变成 MISSING_TYPE
    if not isinstance(constraint_hits_set, set):
        constraint_hits_set = set()
        session_ctx["_constraint_hits"] = constraint_hits_set

    # 记录本轮新命中的 ID，用于排除 Evaluator Prompt 中的"视野失忆"问题
    newly_hit_ids: set = set()

    for constraint in all_constraints:
        cid = _get_constraint_id(constraint)
        if cid is None:
            continue
        if cid in constraint_hits_set:
            continue

        text = _get_constraint_text(constraint)
        if not text:
            continue

        text_normalized = normalize_text(text)
        quality = _l1_match_quality(text_normalized, user_normalized)
        if quality > 0:
            constraint_hits_set.add(cid)
            newly_hit_ids.add(cid)
            session_ctx["_constraint_hits"] = constraint_hits_set

            # 计分
            points_per = 60.0 / max(1, len(all_constraints))
            session_ctx["task_score"] = min(
                60.0, session_ctx.get("task_score", 0.0) + (points_per * quality)
            )

            # 写回 UserProgress（通过 legacy_node_id 关联）
            _update_constraint_mastery(db, user_id, constraint, quality)
            logger.info(
                f"[L1] Constraint hit: '{text}' quality={quality:.2f} "
                f"(cid={cid})"
            )

    # 存储本轮新命中，供 Evaluator Prompt 排除使用
    if newly_hit_ids:
        session_ctx["_current_turn_hits"] = newly_hit_ids

    return newly_hit_ids


def _update_constraint_mastery(
    db: Session,
    user_id: str,
    constraint,
    quality: float,
) -> None:
    """
    将约束命中写回 UserProgress 表。

    【修复】支持动态创建虚拟 TargetNode：
    - 如果 legacy_node_id 存在，直接使用
    - 如果 legacy_node_id 为 None 但 constraint_id 存在，
      动态创建虚拟 TargetNode 并关联，确保学习记录能落库
    """
    try:
        legacy_id = None
        constraint_id = None
        constraint_text = ""
        constraint_type = "word"
        constraint_depth = 1

        if isinstance(constraint, ScenarioConstraintItem):
            legacy_id = getattr(constraint, "legacy_node_id", None)
            constraint_id = getattr(constraint, "constraint_id", None)
            constraint_text = getattr(constraint, "constraint_text", "")
            constraint_type = getattr(constraint, "constraint_type", "word")
            constraint_depth = getattr(constraint, "depth_level", 1)
        elif isinstance(constraint, dict):
            legacy_id = constraint.get("legacy_node_id")
            constraint_id = constraint.get("constraint_id")
            constraint_text = constraint.get("constraint_text", "")
            constraint_type = constraint.get("constraint_type", "word")
            constraint_depth = constraint.get("depth_level", 1)

        # 如果没有 legacy_id 但有 constraint_text，动态创建虚拟 TargetNode
        if legacy_id is None and constraint_text:
            # 先查找是否已有同名虚拟节点
            node_record = db.query(TargetNode).filter(
                TargetNode.node_text == constraint_text,
                TargetNode.topic_id == 0,  # 虚拟节点 topic_id=0
            ).first()

            if not node_record:
                # 创建虚拟节点
                node_record = TargetNode(
                    topic_id=0,  # 0 表示虚拟节点
                    node_text=constraint_text,
                    node_type=constraint_type,
                    depth_level=constraint_depth,
                    weight=1.0,
                )
                db.add(node_record)
                db.commit()
                db.refresh(node_record)
                logger.info(
                    f"[L1] Created virtual TargetNode for constraint: "
                    f"id={node_record.id}, text='{constraint_text}'"
                )

            legacy_id = node_record.id

        if legacy_id is None:
            logger.debug(f"[_update_constraint_mastery] No legacy_id for constraint: {constraint_text}")
            return

        progress = db.query(UserProgress).filter(
            UserProgress.user_id == user_id,
            UserProgress.node_id == legacy_id,
        ).first()

        if not progress:
            progress = UserProgress(
                user_id=user_id,
                node_id=legacy_id,
                mastery_score=0.0,
                practice_count=0,
            )
            db.add(progress)

        progress.practice_count += 1
        progress.mastery_score = update_mastery(
            progress.mastery_score, was_correct=True, quality=quality
        )
        progress.last_practiced_at = datetime.datetime.utcnow()
        db.commit()
    except Exception as e:
        logger.warning(f"[_update_constraint_mastery] Failed to update progress: {e}")
        db.rollback()


def _l1_match_quality(node_normalized: str, user_normalized: str) -> float:
    """
    L1 节点命中检测，返回质量分 0~1（0 = 未命中）。
    支持精确匹配和词干匹配。
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


def _select_next_scenario(
    session_ctx: dict,
    current_scenario_info,
) -> Optional[dict]:
    """
    从当前微场景的 available_transitions 中选择下一场景。

    选择策略：
    1. 优先选 overlap_ratio 最高的
    2. 若 available_transitions 为空，返回 None（教研干预触发）
    """
    if not current_scenario_info or not current_scenario_info.available_transitions:
        return None

    transitions = list(current_scenario_info.available_transitions)
    transitions.sort(key=lambda t: t.get("overlap_ratio", 0), reverse=True)
    return transitions[0] if transitions else None


def advance_state_machine(
        session_ctx: dict,
        db: Session,
        current_topic: Topic,
        task_packet: Optional[TaskPacket] = None,
        director_signal: Optional[dict] = None,
) -> bool:
    """
    推进微场景状态机。

    - 检查 scenario_completed 信号（双轨 / 超限 / 全部命中）
    - 若触发：重置 turn_count_in_scenario = 0，设置 scenario_completed = True
    - 返回 True 表示发生了状态转换

    Returns:
        True if a state transition occurred.
    """
    micro_mode = is_micro_mode(task_packet) if task_packet else False

    if micro_mode:
        # === 微场景模式 ===
        completed = check_scenario_completion(session_ctx, task_packet, director_signal)

        if completed:
            session_ctx["turn_count_in_scenario"] = 0
            session_ctx["scenario_completed"] = True

            # 【注意】Director 信号清理统一在 coach_ws.py 处理，此处不再清理

            next_scenario = _select_next_scenario(session_ctx, task_packet.current_scenario)
            if next_scenario:
                session_ctx["next_scenario"] = next_scenario
                logger.info(
                    f"[SCENARIO] 微场景通关！准备流转至: "
                    f"{next_scenario.get('scenario_name', 'N/A')}"
                )
            else:
                # 无后续场景：检查是否标记为正常出口
                current_scenario = task_packet.current_scenario
                if current_scenario and getattr(current_scenario, 'is_exit_point', False):
                    session_ctx["next_scenario"] = None  # 正常结束
                    logger.info("[SCENARIO] 微场景通关！正常出口场景结束")
                else:
                    # 孤立节点：触发教研告警
                    logger.warning(
                        "[SCENARIO] 孤立节点：无后续场景且未标记为出口。"
                        "建议教研人员在图谱中为该场景配置 available_transitions 或标记 is_exit_point=True。"
                    )
                    session_ctx["next_scenario"] = None
                    session_ctx["_isolated_node_alert"] = True

            return True
        else:
            return False