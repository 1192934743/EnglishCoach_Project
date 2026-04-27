阶段二实施方案：对话引擎重构 — 双轨校验微场景流转
一、总体重构策略
1.1 双模式共存（向后兼容）
阶段二采用双模式共存策略，避免破坏现有调用链：

模式	触发条件	流转逻辑
新模式 (micro_mode=True)
TaskPacket.has_constraints() == True
基于 turn_count_in_scenario + 双轨校验
旧模式 (micro_mode=False)
TaskPacket.has_constraints() == False
维持原四阶段线性状态机
注：session_planner.py 在阶段二同步升级后，所有新生 TaskPacket 均包含 constraints，旧模式将自动退出。这是渐进式迁移，不是破坏性重构。

1.2 核心变量对照
旧变量	新变量	说明
phase
scenario_mode + turn_count_in_scenario
废弃线性相位，改为场景轮数
should_advance_phase
intent_achieved + constraints_hit 双轨信号
废弃单一信号
new_targets / history_targets
constraints / review_constraints
升级为结构化约束列表
phase_turns
turn_count_in_scenario
改名更语义化
无
scenario_completed
新增：微场景通关标志
二、模板重构设计（Jinja2）
2.1 静态模板更新（STATIC_SYSTEM_TEMPLATE）
变更要点：在现有模板基础上，新增 depth_level → CEFR 映射注入块，根据微场景的 depth_level 强制下发难度压制指令。

# 新增常量：Depth Level → CEFR 压制级别映射
DEPTH_TO_CEFR = {
    1: "Beginner",   # depth_level 1 → A1/A2 极简词汇
    2: "Elementary", # depth_level 2 → A2/B1 简单日常
    3: "Advanced",   # depth_level 3 → B2/C1+ 复杂表达
}
# 静态模板改动：在 [SESSION GOAL] 之后追加 CEFR 压制块
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
TARGET EXPRESSIONS (learner should use these): {{ unhit_constraints }}
{% endif %}
{% if hit_constraints %}
PRACTICED (already used): {{ hit_constraints }}
{% endif %}
{% endif %}
[OUTPUT BUDGET & QUESTION POLICY]
- Within your {{ max_reply_sentences }} sentence allowance: ask at most ONE question that expects an answer from the learner this turn.
- Do NOT put two answerable questions in the same sentence.
- No bullet lists, no lecture-style multi-paragraph answers. Use plain conversational text only.
""")
关键渲染变量说明：

cefr_level：来自 DEPTH_TO_CEFR[depth_level]，作为 cognitive_load_levels 的 key
cefr_force_block：根据 depth_level 从 global_rules.json 的 cognitive_load_levels 中提取，并额外追加 A1/A2 词汇限制指令（如 Depth 1 强制 "Use ONLY single-word or very short phrases. Avoid all complex sentences."）
2.2 动态模板（DYNAMIC_TURN_V2_TEMPLATE）
核心设计：基于 turn_count_in_scenario 的渐进式诱导三档。

DYNAMIC_TURN_V2_TEMPLATE = JINJA_ENV.from_string("""
{% if turn_count == 1 %}
[SITUATION — TURN {{ turn_count }} / {{ max_turns }}]
{{ scene_desc }}
Action: Start the conversation naturally and set the scene.
Give the learner space to respond. Do NOT push practice vocabulary yet.
{% elif turn_count == 2 %}
[SITUATION — TURN {{ turn_count }} / {{ max_turns }}]
{{ scene_desc }}
Action: If the user has NOT yet used the target expression '{{ current_constraint }}',
use a natural prompt to gently guide them toward it.
For example, you can ask a yes/no question that makes saying the target expression feel like the most natural choice.
DO NOT directly mention the target expression.
{% elif turn_count >= 3 %}
[SITUATION — TURN {{ turn_count }} / {{ max_turns }} — RESCUE MODE]
{{ scene_desc }}
Action: The learner has not yet naturally used the target expression.
You MUST guide them to it more directly.
OPTIONS (pick ONE that feels most natural in context):
  1. Complete their sentence for them and ask them to repeat: "You can say: '{{ current_constraint }}'"
  2. Offer a forced-choice: "Do you mean small, medium, or large?"
  3. Model the phrase clearly and ask: "Could you repeat after me: '{{ current_constraint }}'?"
Do NOT give up. Keep the learner moving forward until they succeed.
{% endif %}
[CRITICAL INSTRUCTION]
Reply with ONLY your in-character English dialogue. No tags, no JSON, no tool calls.
""")
三档诱导策略详解：

档位	触发条件	教练策略	目的
Turn 1 放养
turn_count == 1
自然开场，不提任何词汇
减少认知负荷，建立自然感
Turn 2 诱导
turn_count == 2 且未命中
二选一提问 + 自然引导
让用户"自己说出"目标词汇
Turn 3+ 救援
turn_count >= 3 且未命中
半拉子示范 + 重复请求
强制保底，防止卡死
2.3 导演评估模板（EVALUATOR_V2_TEMPLATE）
核心变更：废弃 should_advance_phase，新增双轨校验输出字段。

EVALUATOR_V2_TEMPLATE = JINJA_ENV.from_string("""
You are the backend AI Director for an English coaching application.
You will be provided with the last exchange between the User and the AI Coach.
Your job is to use the `submit_analysis_and_feedback` tool to output a JSON object:
1. `ai_translation_cn`: Natural Chinese translation of the AI Coach's reply (not literal).
2. `suggested_hints_en`: 2 short replies the User could say next.
   - Beginner: 2-4 word phrases only
   - Intermediate: 4-7 word responses
   - Advanced: 6-10 word idiomatic responses
   - At least 1 hint should naturally use a word from the target list.
   - Format: string array, e.g. ["Sure.", "That sounds good."]
3. `coach_correction_cn`: If the user made a grammar/vocabulary error, correct it in Chinese.
   Otherwise leave empty.
=== DUAL-TRACK VALIDATION (Your most important output) ===
4. `intent_achieved`: BOOLEAN
   Set to TRUE if the user's reply demonstrates that they have fulfilled the teaching intent:
   "{{ current_intent }}"
   Consider: Did they meaningfully engage with the scenario goal?
5. `constraints_hit`: BOOLEAN
   Set to TRUE if the user naturally used AT LEAST ONE of these target expressions:
   {% for c in constraint_texts %}
   - "{{ c }}"{% endfor %}
   Note: "Naturally" means they used the expression in context, not just in a forced repetition.
6. `constraints_hit_details`: ARRAY of objects
   For each constraint that was hit, report:
   [{"constraint_text": "...", "quality": 0.0-1.0, "note": "..."}]
   - quality 1.0 = exact use in perfect context
   - quality 0.8 = stem/close variant in good context
   - quality 0.0 = not hit
[WHEN TO TRIGGER SCENARIO COMPLETION]
If BOTH intent_achieved==TRUE AND constraints_hit==TRUE, set:
7. `scenario_completed`: TRUE
This signals the engine to trigger micro-scenario transition.
Learner Level: {{ learner_level }}
Current Scenario: {{ scenario_name }}
""")
三、核心函数伪代码
3.1 build_prompts() 重构
def build_prompts(
    user: Optional[User],
    is_flipped: bool,
    session_ctx: dict,
    task_packet: Optional[TaskPacket] = None,
    session_hits: Optional[set] = None,
) -> Tuple[str, str]:
    """构建主 LLM (Actor) 的 Prompt。自动选择新旧模式。"""
    if task_packet is None:
        raise ValueError("...")
    rules = load_global_rules()
    if session_hits is None:
        session_hits = set()
    # ── 判断运行模式 ──────────────────────────────────────────────────────
    micro_mode = task_packet.has_constraints()
    # ── 基础变量提取 ──────────────────────────────────────────────────────
    scene_name = task_packet.scene_prompt
    role_name = task_packet.role_name
    depth_level = int(task_packet.depth_tier or 1)
    canonical_level = effective_learner_label(
        user.settings or {} if user else None,
        task_packet.learner_level,
        task_packet.learner_level,
    )
    max_reply = compute_max_reply_sentences(canonical_level, depth_level)
    # ── 角色与全局规则（所有模式共享）─────────────────────────────────────
    role_desc = (f"You are acting as: {role_name} in a {scene_name} setting."
                 if not is_flipped
                 else f"You are the CUSTOMER/USER. The user is acting as the {role_name}.")
    personality_desc = rules.get("personality_levels", {}).get(
        str(user.politeness_level) if user else "1", "")
    cog_line = _cognitive_load_line(rules, canonical_level)
    universal_rules = (rules.get("universal_rules_by_level") or {}).get(
        canonical_level, rules.get("universal_rules", []))
    # ── 【新模式】CEFR 压制 + 微场景变量 ─────────────────────────────────
    cefr_level = DEPTH_TO_CEFR.get(depth_level, "Elementary")
    cefr_force_block = _build_cefr_force_block(rules, depth_level)
    # 约束命中状态
    all_constraints = task_packet.constraints + task_packet.review_constraints
    unhit = [c for c in all_constraints if _get_constraint_id(c) not in session_hits]
    hit = [c for c in all_constraints if _get_constraint_id(c) in session_hits]
    unhit_str = ", ".join([f"'{_get_text(c)}'" for c in unhit]) or "(none)"
    hit_str = ", ".join([f"'{_get_text(c)}'" for c in hit]) or "(none)"
    current_constraint = _get_text(unhit[0]) if unhit else ""
    # ── 渲染静态模板 ──────────────────────────────────────────────────────
    if micro_mode:
        static_prompt = STATIC_SYSTEM_TEMPLATE_V2.render(
            canonical_level=canonical_level,
            depth_tier_val=depth_level,
            max_reply_sentences=max_reply,
            role_desc=role_desc,
            personality_desc=personality_desc,
            cog_line=cog_line,
            cefr_force_block=cefr_force_block,
            cefr_level=cefr_level,
            universal_rules=universal_rules,
            scene_specific_rules=task_packet.scene_specific_rules,
            session_goal_line=task_packet.session_goal,
            micro_scene_block=bool(task_packet.current_scenario),
            scenario_name=task_packet.current_scenario.scenario_name if task_packet.current_scenario else "",
            current_intent=task_packet.current_intent or task_packet.session_goal,
            unhit_constraints=unhit_str,
            hit_constraints=hit_str,
        )
    else:
        # 旧模式 fallback
        static_prompt = STATIC_SYSTEM_TEMPLATE.render(...)
    # ── 渲染动态模板 ──────────────────────────────────────────────────────
    if micro_mode:
        turn_count = session_ctx.get("turn_count_in_scenario", 1)
        max_turns = (task_packet.current_scenario.max_turns
                     if task_packet.current_scenario else 8)
        dynamic_prompt = DYNAMIC_TURN_V2_TEMPLATE.render(
            turn_count=turn_count,
            max_turns=max_turns,
            scene_desc=(task_packet.current_scenario.scene_desc
                        if task_packet.current_scenario else scene_name),
            current_constraint=current_constraint,
        )
    else:
        phase = session_ctx.get("phase", "ICE_BREAKING")
        dynamic_prompt = DYNAMIC_TURN_TEMPLATE.render(
            phase=phase,
            scene_name=scene_name,
            ...  # 旧模式变量
        )
    return static_prompt, dynamic_prompt
3.2 build_evaluator_prompt() 重构
def build_evaluator_prompt(
    session_ctx: dict,
    task_packet: Optional[TaskPacket] = None,
) -> str:
    """构建副 LLM (Director) 的评估 Prompt。"""
    learner_level = task_packet.learner_level if task_packet else "Intermediate"
    micro_mode = task_packet.has_constraints() if task_packet else False
    if micro_mode:
        constraint_texts = [
            _get_text(c) for c in (
                task_packet.constraints + task_packet.review_constraints
            )
        ]
        return EVALUATOR_V2_TEMPLATE.render(
            learner_level=learner_level,
            scenario_name=(task_packet.current_scenario.scenario_name
                           if task_packet.current_scenario else task_packet.scene_prompt),
            current_intent=task_packet.current_intent or task_packet.session_goal,
            constraint_texts=constraint_texts,
        )
    else:
        return EVALUATOR_SYSTEM_TEMPLATE.render(
            phase=session_ctx.get("phase", "ICE_BREAKING"),
            scene_name=task_packet.scene_prompt if task_packet else "General Conversation",
            learner_level=learner_level,
            vocab_tags=task_packet.vocab_tags if task_packet else [],
            sentence_patterns=task_packet.sentence_patterns if task_packet else [],
        )
3.3 evaluate_and_check_progress() 重构
async def evaluate_and_check_progress(
    db: Session,
    user_id: str,
    topic_id: int,
    user_text: str,
    session_hits: set,
    session_ctx: dict,
    websocket: WebSocket,
    ws_lock: asyncio.Lock,
    task_packet: Optional[TaskPacket] = None,
):
    """
    评估层：每轮对话触发。
    新模式（L1 检测）：
    - 遍历 constraints（含 review_constraints）
    - 使用 normalize_text + simple_stem 匹配
    - 更新 session_hits 和 session_ctx["task_score"]
    - 推送 WebSocket 进度
    Director 信号处理（在异步调用链中）：
    - 提取 intent_achieved + constraints_hit + scenario_completed
    - 若 scenario_completed == True，触发微场景通关逻辑
    """
    try:
        if not task_packet:
            return
        micro_mode = task_packet.has_constraints()
        turn_count = session_ctx.get("turn_count_in_scenario", 0) + 1
        session_ctx["turn_count_in_scenario"] = turn_count
        # ── 【新模式】约束命中检测 ──────────────────────────────────────
        if micro_mode and user_text.strip():
            all_constraints = task_packet.constraints + task_packet.review_constraints
            _check_constraint_hits(
                db, user_id, user_text, all_constraints,
                session_hits, session_ctx,
            )
        # ── 闲聊分（所有模式共享）───────────────────────────────────────
        if user_text.strip():
            chat_idx = session_ctx.get("chat_interaction_count", 0)
            if chat_idx < len(CHAT_SCORE_CURVE):
                added = CHAT_SCORE_CURVE[chat_idx]
                session_ctx["chat_score"] = min(
                    40.0, session_ctx.get("chat_score", 0.0) + added)
                session_ctx["chat_interaction_count"] = chat_idx + 1
        # ── WebSocket 进度推送 ───────────────────────────────────────────
        completed_rounds = session_ctx.get("completed_rounds_in_level", 0)
        current_score = session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0)
        overall = min(100.0, ((completed_rounds * 100.0) + current_score) / float(ROUNDS_PER_LEVEL))
        async with ws_lock:
            await websocket.send_text(json.dumps({
                "event": "topic_mastery_reached",
                "progress": overall,
                "level": session_ctx.get("current_level", 1),
                "turn_in_scenario": turn_count,
                "next_topic_suggestion": "",
            }))
    except Exception as e:
        logger.error(f"evaluate_and_check_progress error: {e}", exc_info=True)
3.4 导演评估信号处理
Director LLM 的 JSON 响应中新增两个字段：intent_achieved 和 constraints_hit。引擎侧新增解析函数：

def parse_director_signal(director_json: dict) -> dict:
    """
    解析 Director LLM 的 JSON 输出，返回标准化的信号字典。
    Returns:
        {
            "intent_achieved": bool,
            "constraints_hit": bool,
            "scenario_completed": bool,  # 双轨均通过时为 True
            "hit_details": list[dict],
        }
    """
    intent_achieved = bool(director_json.get("intent_achieved", False))
    constraints_hit = bool(director_json.get("constraints_hit", False))
    scenario_completed = intent_achieved and constraints_hit
    return {
        "intent_achieved": intent_achieved,
        "constraints_hit": constraints_hit,
        "scenario_completed": scenario_completed,
        "hit_details": director_json.get("constraints_hit_details", []),
    }
def check_scenario_completion(
    session_ctx: dict,
    task_packet: Optional[TaskPacket],
    director_signal: dict,
) -> bool:
    """
    判断是否触发微场景通关。
    通关条件（三选一）：
    1. Director 双轨信号均为 True（主路径）
    2. 轮数超限（max_turns exceeded，强制通关保底）
    3. 所有 constraints 均已命中（提前通关）
    Returns:
        True if scenario should complete and trigger transition
    """
    micro_mode = task_packet.has_constraints() if task_packet else False
    if not micro_mode:
        return False  # 旧模式不参与微场景流转
    # 条件1：双轨信号
    if director_signal.get("scenario_completed"):
        logger.info("[SCENARIO] 双轨校验通过，触发微场景通关")
        return True
    # 条件2：轮数超限
    turn_count = session_ctx.get("turn_count_in_scenario", 0)
    max_turns = (task_packet.current_scenario.max_turns
                 if task_packet and task_packet.current_scenario else 8)
    if turn_count >= max_turns:
        logger.info(f"[SCENARIO] 轮数超限（{turn_count}>={max_turns}），强制通关")
        return True
    # 条件3：所有约束已命中
    all_constraints = (task_packet.constraints + task_packet.review_constraints
                      if task_packet else [])
    if all_constraints:
        session_hits = session_ctx.get("_constraint_hits", set())
        all_hit = all(
            _get_constraint_id(c) in session_hits
            for c in all_constraints
        )
        if all_hit:
            logger.info("[SCENARIO] 所有约束已命中，提前通关")
            return True
    return False
3.5 advance_state_machine() 重构
def advance_state_machine(
    session_ctx: dict,
    db: Session,
    current_topic: Topic,
    session_hits: set,
    task_packet: Optional[TaskPacket] = None,
    director_signal: Optional[dict] = None,
) -> dict:
    """
    推进状态机。
    新模式：
    - 检查 scenario_completed 信号
    - 若触发：输出 {"action": "scenario_complete", "next_scenario_id": int}
    - 触发后重置 turn_count_in_scenario = 0
    旧模式：
    - 维持原四阶段流转逻辑不变
    - 返回 {"action": "phase_advance", "new_phase": str}
    Returns:
        控制指令字典，供调用方（coach_ws）执行具体切换
    """
    micro_mode = task_packet.has_constraints() if task_packet else False
    if micro_mode:
        # ── 【新模式】微场景流转 ────────────────────────────────────────
        completed = check_scenario_completion(session_ctx, task_packet, director_signal or {})
        if completed:
            session_ctx["turn_count_in_scenario"] = 0
            session_ctx["scenario_completed"] = True
            # 从 available_transitions 中选下一场景
            next_scenario = _select_next_scenario(
                session_ctx, task_packet.current_scenario
            )
            logger.info(
                f"[SCENARIO] 微场景通关！准备流转至: "
                f"{next_scenario.get('scenario_name', 'N/A') if next_scenario else 'END'}"
            )
            return {
                "action": "scenario_complete",
                "next_scenario": next_scenario,
                "turn_count": session_ctx.get("turn_count_in_scenario", 0),
            }
        else:
            return {"action": "continue"}
    else:
        # ── 【旧模式】四阶段流转 ────────────────────────────────────────
        phase = session_ctx.get("phase", "ICE_BREAKING")
        llm_signal = session_ctx.get("llm_wants_to_advance", False)
        turns = session_ctx.get("phase_turns", 0)
        force_advance = (turns >= MAX_TURNS_PER_PHASE)
        # ... 原有的四阶段流转逻辑（完全保留）...
        return {"action": "phase_advance", "new_phase": new_phase}
def _select_next_scenario(
    session_ctx: dict,
    current_scenario_info,
) -> Optional[dict]:
    """
    从当前微场景的 available_transitions 中选择下一场景。
    选择策略：
    1. 优先选 step_order 更大的场景（正向推进）
    2. 同 step_order 内，选 overlap_ratio 最高的
    3. 若 available_transitions 为空，返回 None（教研干预触发）
    """
    if not current_scenario_info or not current_scenario_info.available_transitions:
        return None
    transitions = current_scenario_info.available_transitions
    # 按 overlap_ratio 降序排序
    transitions.sort(key=lambda t: t.get("overlap_ratio", 0), reverse=True)
    return transitions[0] if transitions else None
四、SessionContext 字段变更
新增字段（追加到现有 dataclass）：

# ── 【新增】微场景流转 ────────────────────────────────────────────────────
turn_count_in_scenario: int = 0      # 当前微场景已进行轮数（每轮 +1，通关重置）
scenario_completed: bool = False      # 本轮是否已通关（触发流转后设为 True）
_constraint_hits: set = field(default_factory=set)  # 内部：约束命中 ID 集合
# 旧字段保留（向后兼容）：
# phase / phase_turns / llm_wants_to_advance  → 旧模式使用
# new_targets / history_targets               → 旧模式使用
五、Director LLM JSON 响应解析升级
submit_analysis_and_feedback tool 的返回 schema 需要更新。引擎侧需要同时兼容新旧两种 JSON 格式：

def parse_director_response(raw_json: dict) -> dict:
    """
    兼容新旧两种 Director JSON 格式。
    新格式（含双轨信号）：
    {
        "ai_translation_cn": "...",
        "suggested_hints_en": ["...", "..."],
        "coach_correction_cn": "...",
        "intent_achieved": true,
        "constraints_hit": true,
        "scenario_completed": true,
        "constraints_hit_details": [{"constraint_text": "...", "quality": 0.8}]
    }
    旧格式（无双轨信号）：
    {
        "ai_translation_cn": "...",
        "suggested_hints_en": ["...", "..."],
        "coach_correction_cn": "...",
        "should_advance_phase": true/false
    }
    """
    result = {
        "ai_translation_cn": raw_json.get("ai_translation_cn", ""),
        "suggested_hints_en": raw_json.get("suggested_hints_en", []),
        "coach_correction_cn": raw_json.get("coach_correction_cn", ""),
    }
    # 检测是否为新格式
    if "intent_achieved" in raw_json:
        signal = parse_director_signal(raw_json)
        result.update(signal)
    else:
        # 旧格式兼容：should_advance_phase → 触发旧状态机
        result["should_advance_phase"] = raw_json.get("should_advance_phase", False)
    return result
六、文件变更清单
文件	操作	变更说明
core/dialogue_engine.py
修改
新增 V2 模板、build_prompts_v2()、parse_director_signal()、check_scenario_completion()；重构 build_prompts()、build_evaluator_prompt()、evaluate_and_check_progress()、advance_state_machine()
domain/entities/session_context.py
修改
新增 turn_count_in_scenario、scenario_completed、_constraint_hits 字段
prompts/global_rules.json
修改
可选：新增 cefr_force_rules 字段，专用于 Depth 1/2/3 的极简词汇压制规则
七、风险评估与缓解
风险	缓解
新旧模式并行导致行为不一致
双轨信号和 phase 独立运行，共用 chat_score/task_score/current_level 计分体系
Director LLM 输出格式不稳定
引擎侧同时兼容新旧 JSON，缺失字段使用默认值
微场景流转导致话题跨度大
70/30 原则（IoU 0.6~0.8）已在上游 build_scenario_graph.py 保证
阶段三（session_planner 升级）未完成时新模式无法触发
micro_mode = task_packet.has_constraints() 为 False 时自动降级旧模式
是否同意该方案并开始阶段二的编码？