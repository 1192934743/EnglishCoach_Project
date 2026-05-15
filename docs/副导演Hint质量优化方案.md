# 副导演 Hint 质量优化方案

> **结论：推荐固定 3 个 Hint**
> - 符合"三选一"心理模型，用户容易做决策
> - 可做功能分层：确认型 + 回答问题型 + 目标词引导型
> - 即使一个不合适，用户还有 2 个备选

---

## 变更记录

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-05-15 | v1.0 | 初始版本 |
| 2026-05-15 | v1.1 | 吸收评审意见：修正7个问题，添加3个补充建议 |

---

## 一、问题诊断

### 1.1 当前 Hint 生成架构

```
coach_ws.py (line 81-169)
    │
    ├── _run_background_evaluator()
    │       │
    │       ├── build_evaluator_prompt()  → dialogue_engine.py
    │       │           │
    │       │           └── EVALUATOR_V2_TEMPLATE (line 361-449)
    │       │                   │
    │       │                   └── 生成 suggested_hints_en (2-3个)
    │       │
    │       └── llm_router.chat() → 返回 feedback_data
    │               │
    │               └── 推送 WebSocket: {"event": "teaching_data", "data": feedback_data}
```

### 1.2 发现的 10 个核心问题

| # | 问题 | 位置 | 严重度 |
|---|------|------|--------|
| 1 | **Hint数量定义混乱** - 模板说2个，schema说2-3个，回退策略用3个 | `dialogue_engine.py:369`, `session_service.py:26`, `coach_ws.py:120` | 高 |
| 2 | **缺少场景描述(scene_desc)** - Evaluator看不到用户正在做什么情境 | `dialogue_engine.py:761` | 高 |
| 3 | **缺少对话轮次信息** - 无法区分"刚开局"和"快超时"应给出不同提示 | `dialogue_engine.py:761` | 中 |
| 4 | **缺少历史Hint记录** - 可能重复推荐用户已看过的相同Hint | 架构缺失 | 中 |
| 5 | **Level指导过于模糊** - "2-4 word"vs"4-7 word"vs"6-10 word"粒度太粗 | `dialogue_engine.py:370-372` | 中 |
| 6 | **未指定Target Word优先级** - 多个constraint时未说明应优先hint哪个 | `dialogue_engine.py:373` | 中 |
| 7 | **缺少对话上下文** - 只给最后一句对话，无法理解对话流向 | `coach_ws.py:93` | 高 |
| 8 | **回退策略太通用** - 回退Hint与当前场景完全不相关 | `coach_ws.py:120` | 高 |
| 9 | **未区分Hint类型** - 没区分"安全回退型"和"教学引导型" | 架构缺失 | 中 |
| 10 | **缺少"继续当前话题"的Hint** - 当用户需要更多信息时无合适Hint | `dialogue_engine.py:369` | 中 |

---

## 二、代码具体问题位置

### 问题1：Hint数量不一致

```python
# session_service.py:26 - schema说2-3个
"suggested_hints_en": {
    "type": "array",
    "items": {"type": "string"},
    "description": "2 or 3 short English responses the User could say next."
}

# coach_ws.py:120-124 - 回退策略用3个
"suggested_hints_en": ["Could you repeat that?", "I see.", "Okay, thanks."],

# dialogue_engine.py:369-373 - 模板说2个
2. `suggested_hints_en`: 2 short replies the User could say next.
   - Beginner: 2-4 word phrases only
   - Intermediate: 4-7 word responses
   - Advanced: 6-10 word idiomatic responses
```

### 问题2：缺少scene_desc上下文

```python
# dialogue_engine.py:761-767
return EVALUATOR_V2_TEMPLATE.render(
    learner_level=learner_level,
    scenario_name=scenario_name_str,
    current_intent=current_intent_str,
    constraint_texts=constraint_texts,
    hit_constraints=hit_constraints_str,
    # ❌ 缺少 scene_desc、turn_count、recent_hints 等关键信息
)
```

---

## 三、优化方案

### 3.1 修改 EVALUATOR_V2_TEMPLATE (dialogue_engine.py)

**核心改进：统一为 3 个 Hint，分三种类型**

```python
EVALUATOR_V2_TEMPLATE = JINJA_ENV.from_string("""
You are the backend AI Director for an English coaching application.
You will be provided with the conversation history and the current exchange between the User and the AI Coach.

Your job is to use the `submit_analysis_and_feedback` tool to output a JSON object containing:

1. `ai_translation_cn`: Natural Chinese translation of the AI Coach's reply (not literal).

2. `suggested_hints_en`: CRITICAL — You MUST provide EXACTLY 3 short replies.
   - Format: string array with exactly 3 elements
   - CRITICAL word count (per hint):
     * Beginner: 2-4 words maximum
     * Intermediate: 4-6 words maximum
     * Advanced: 6-8 words maximum
   - Each hint must be a COMPLETE phrase (not just a word)
   - Do not use contractions in Beginner level (use "do not" not "don't")
   - Avoid polite fillers like "if you don't mind" or "if that's okay"

   COACHING INTENT: {{ current_intent }}
   This turn's teaching goal is to help the user: {{ current_intent }}

   HINT TYPE 1 — Acknowledge/Confirm:
   Acknowledge or confirm what the coach said. Keep it simple and universal.
   Examples: "I see.", "Okay.", "Got it.", "Fair enough."
   DO NOT use: "Yes, please." (context-dependent), "I'll take that" (not universal)

   HINT TYPE 2 — Answer the Coach's Question:
   Directly respond to the coach's last question or statement.
   If coach asked about preferences or made a statement, respond naturally.
   Examples:
   - If asked "What size?" → "Large, please." or "Medium please."
   - If asked "For here or to go?" → "For here, thanks."
   - If asked "Would you like anything else?" → "That's all, thanks."
   - If coach gave info with no question → "Thanks." or "Got it."

   HINT TYPE 3 — Use Target Expression:
   Naturally incorporate ONE or more target expressions from the list below.
   Target expressions: {{ constraint_texts | join(', ') }}
   Examples: "Can I get a receipt?", "I'd like to order", "Large, for here"

   Variety Rule: Each hint must serve a DIFFERENT purpose.
   Bad example: ["Yes please", "Sure", "Okay"] (all same meaning)
   Good example: ["Got it.", "Large please", "Can I pay by card?"]

   NEGATIVE EXAMPLES (do not use - too generic, teaches nothing):
   - "I see."
   - "Okay thanks."
   - "Could you repeat that?"
   - "That's all." (when not answering a question)
   - "I understand."

   POSITIVE EXAMPLES (teach something or move conversation forward):
   - "Large, please." (teaches size vocabulary)
   - "For here, thanks." (teaches preposition)
   - "Can I pay by card?" (teaches payment expression)
   - "That's all, thanks." (answers the upsell question)

3. `coach_correction_cn`: If the user made a grammar/vocabulary error, correct it in Chinese.
   Otherwise leave empty.

=== TRIPLE-TRACK VALIDATION ===

4. `intent_achieved`: BOOLEAN
   Set to TRUE if the user has clearly expressed their core request or main goal for this scenario.
   This flag is used to unlock the "Detail Probing" mode — it should be easy to trigger.

5. `constraints_hit`: BOOLEAN
   IMPORTANT — Prevailing Rule: Check BOTH this turn AND the conversation history.
   Set to TRUE if the user has EVER naturally used AT LEAST ONE of these target expressions:
   {% for c in constraint_texts %}
   - "{{ c }}"{% endfor %}

6. `coach_ready_to_transition`: BOOLEAN
   This is the REAL gatekeeper for scenario completion. Be STRICT here.
   Set to TRUE ONLY IF the AI Coach's latest reply includes a smooth closing.

7. `scenario_completed`: BOOLEAN
   Set to TRUE ONLY when ALL THREE are TRUE:
     intent_achieved == TRUE AND constraints_hit == TRUE AND coach_ready_to_transition == TRUE.

=== CONTEXT FOR BETTER HINTS ===
Learner Level: {{ learner_level }}
Current Scenario: {{ scenario_name }}
{% if scene_desc %}
Scene Description: {{ scene_desc }}
{% endif %}
{% if turn_count %}
Turn: {{ turn_count }} / {{ max_turns }}
{% endif %}
{% if last_coach_question %}
Coach's Last Question/Statement: {{ last_coach_question }}
{% endif %}
{% if recent_hints %}
DO NOT repeat these recent hints: {{ recent_hints | join(', ') }}
{% endif %}
""")
```

### 3.2 增强 build_evaluator_prompt() 函数

```python
def build_evaluator_prompt(
    session_ctx: dict,
    task_packet: Optional[TaskPacket] = None,
) -> str:
    learner_level = task_packet.learner_level if task_packet else "Intermediate"
    all_constraints = (task_packet.constraints + task_packet.review_constraints) if task_packet else []
    constraint_texts = [_get_constraint_text(c) for c in all_constraints]

    # 获取已命中约束
    constraint_hits_set = session_ctx.get("_constraint_hits", set())
    if not isinstance(constraint_hits_set, set):
        constraint_hits_set = set()

    current_turn_hits = session_ctx.get("_current_turn_hits", set())
    if not isinstance(current_turn_hits, set):
        current_turn_hits = set()

    history_hit_ids = constraint_hits_set - current_turn_hits
    hit_constraints_list = [
        _get_constraint_text(c)
        for c in all_constraints
        if _get_constraint_id(c) in history_hit_ids
    ]
    hit_constraints_str = ", ".join(f"'{t}'" for t in hit_constraints_list) if hit_constraints_list else ""

    scenario_name_str = (
        task_packet.current_scenario.scenario_name
        if task_packet and task_packet.current_scenario and task_packet.current_scenario.scenario_name
        else (task_packet.scene_prompt if task_packet else "General Conversation")
    )

    # 【新增】场景描述
    scene_desc_str = (
        task_packet.current_scenario.scene_desc
        if task_packet and task_packet.current_scenario and task_packet.current_scenario.scene_desc
        else ""
    )

    # 【新增】当前轮次
    turn_count = session_ctx.get("turn_count_in_scenario", 1)
    max_turns = (
        task_packet.current_scenario.max_turns
        if task_packet and task_packet.current_scenario
        else 8
    )

    # 【新增】上一轮教练问题（从上一轮AI回复中提取）
    last_coach_question = session_ctx.get("_last_coach_question", "")

    # 【新增】最近Hint历史（去重用）
    recent_hints = session_ctx.get("_recent_hint_history", [])

    # 【新增】当前教学意图
    current_intent = (
        task_packet.current_intent or task_packet.session_goal or "Complete the conversation naturally."
    )

    return EVALUATOR_V2_TEMPLATE.render(
        learner_level=learner_level,
        scenario_name=scenario_name_str,
        scene_desc=scene_desc_str,
        current_intent=current_intent,
        constraint_texts=constraint_texts,
        hit_constraints=hit_constraints_str,
        turn_count=turn_count,
        max_turns=max_turns,
        last_coach_question=last_coach_question,
        recent_hints=recent_hints,
    )
```

### 3.3 修改 _run_background_evaluator() 添加历史记录

**重要：顺序必须正确**

```python
async def _run_background_evaluator(
    user_text: str, ai_text: str, session_ctx: dict, task_packet,
    websocket: WebSocket, ws_lock: asyncio.Lock, lat: dict
):
    _latency_log(lat, "11_evaluator_llm_start")

    # 【修正】Step 1: 先提取教练最后的问题/陈述（必须在构建Prompt之前）
    import re
    # 尝试提取问句
    questions = re.findall(r'[^.!?]*\?[^.!?]*[.!?]?', ai_text)
    if questions:
        session_ctx["_last_coach_question"] = questions[-1].strip()
    else:
        # Fallback: 提取最后一句完整陈述作为上下文
        sentences = re.findall(r'[^.!?]+[.!?]', ai_text)
        if sentences:
            session_ctx["_last_coach_question"] = sentences[-1].strip()
        else:
            # 最后的fallback：使用AI回复的前50个字符
            session_ctx["_last_coach_question"] = ai_text[:100].strip() if ai_text else ""

    # Step 2: 构建 Prompt（此时 _last_coach_question 已准备好）
    sys_prompt = build_evaluator_prompt(session_ctx, task_packet)

    # 【新增】构建带历史上下文的用户消息
    history_lines = []
    session_transcript = session_ctx.get("session_transcript", [])
    # 最近3轮对话（6条消息：user + assistant 交替）
    for msg in session_transcript[-6:]:
        role = "User" if msg.get("role") == "user" else "Coach"
        text = msg.get('text', '') or msg.get('content', '')
        if text:
            history_lines.append(f"{role}: {text}")
    
    if history_lines:
        user_message = f"""Recent conversation:
{chr(10).join(history_lines)}

Current turn:
User said: {user_text}
Coach replied: {ai_text}"""
    else:
        user_message = f"""User said: {user_text}
Coach replied: {ai_text}"""

    eval_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_message}
    ]

    # Step 3: 调用 LLM
    feedback_data = None
    try:
        response = await llm_router.chat(
            messages=eval_messages,
            model=None,
            tools=EVALUATOR_TOOLS,
            tool_choice={"type": "function", "function": {"name": "submit_analysis_and_feedback"}},
            max_tokens=600,  # 【修正】从400提高到600，新模板内容更多
            stream=False,
        )
        msg = response.choices[0].message
        if msg.tool_calls:
            args = msg.tool_calls[0].function.arguments
            feedback_data = json.loads(args)
    except Exception as e:
        logger.error(f"副 LLM (Evaluator) 调用失败或解析异常: {e}")

    # 【修正】Step 4: 回退策略（移到LLM调用之后）
    if not feedback_data:
        logger.warning("⚠️ 副 LLM 提取 JSON 失败，触发优雅兜底策略。")
        scenario_name = (
            task_packet.current_scenario.scenario_name
            if task_packet and task_packet.current_scenario
            else ""
        )
        scenario_type = _detect_scenario_type(scenario_name)
        fallback_hints = SCENARIO_FALLBACK_HINTS.get(
            scenario_type,
            SCENARIO_FALLBACK_HINTS["default"]
        )
        feedback_data = {
            "ai_translation_cn": "（AI教练这段话太投入，小助教没来得及翻译~）",
            "suggested_hints_en": fallback_hints,
            "coach_correction_cn": "",
            "should_advance_phase": False,
            "intent_achieved": False,
            "constraints_hit": False,
            "constraints_hit_details": [],
            "scenario_completed": False,
            "coach_ready_to_transition": False,
        }

    # Step 5: 兼容处理...
    feedback_data.setdefault("intent_achieved", False)
    feedback_data.setdefault("constraints_hit", False)
    feedback_data.setdefault("constraints_hit_details", [])
    feedback_data.setdefault("scenario_completed", False)
    feedback_data.setdefault("coach_ready_to_transition", False)

    # Step 6: 【修正】在所有处理完成后，才记录Hints到历史（用于下一轮去重）
    if feedback_data and feedback_data.get("suggested_hints_en"):
        current_hints = feedback_data["suggested_hints_en"]
        recent_history = session_ctx.get("_recent_hint_history", [])
        recent_history.extend(current_hints)
        # 保留最近15个Hint（约5轮对话）
        session_ctx["_recent_hint_history"] = recent_history[-15:]

    # ... 后续代码不变 ...
```

### 3.4 回退策略优化

```python
# 在 coach_ws.py 文件顶部定义

SCENARIO_FALLBACK_HINTS = {
    # 点餐场景
    "ordering": ["Large, please.", "For here, thanks.", "That's all, thanks."],
    # 付款场景
    "payment": ["Credit card, please.", "That's all.", "Can I get the receipt?"],
    # 投诉/道歉场景
    "complaint": ["I understand.", "That's fine.", "No problem."],
    # 问候场景
    "greeting": ["Nice to meet you.", "I'm doing well.", "How about you?"],
    # 通用场景
    "default": ["Sure.", "That works.", "Got it."],
}

def _detect_scenario_type(scenario_name: str) -> str:
    """根据场景名称检测场景类型"""
    if not scenario_name:
        return "default"
    name_lower = scenario_name.lower()
    if any(k in name_lower for k in ["order", "menu", "food", "coffee", "burger", "meal"]):
        return "ordering"
    if any(k in name_lower for k in ["pay", "bill", "card", "cash", "check"]):
        return "payment"
    if any(k in name_lower for k in ["sorry", "apolog", "complaint", "problem", "issue"]):
        return "complaint"
    if any(k in name_lower for k in ["greet", "hello", "meet", "nice to"]):
        return "greeting"
    return "default"
```

---

## 四、文件修改清单

| 文件 | 修改内容 |
|------|----------|
| `python_backend/core/dialogue_engine.py` | 1. 重写 EVALUATOR_V2_TEMPLATE（固定3个Hint，三种类型）<br>2. 增强 build_evaluator_prompt() 函数 |
| `python_backend/api/websocket/coach_ws.py` | 1. 添加场景回退Hint字典<br>2. 优化回退策略<br>3. 添加Hint历史记录<br>4. 添加对话历史上下文 |
| `python_backend/application/services/session_service.py` | 更新 EVALUATOR_TOOLS schema：Hint数量改为固定3个，max_tokens改为600 |

---

## 五、预期效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| Hint相关性 | 随机/通用 | 场景相关 + 回答教练问题 |
| Hint重复率 | 高（可能连续重复） | 低（15轮内不重复） |
| 回退Hint质量 | 通用无关 | 场景匹配（5种场景类型） |
| 用户可选择数 | 2-3个不稳定 | 固定3个稳定 |
| 教学针对性 | 弱 | 强（区分Acknowledge/Answer/UseTarget三种类型） |

---

## 六、风险评估与缓解

| 风险 | 评估 | 缓解措施 |
|------|------|----------|
| max_tokens 不足导致截断 | 中 | 已提高至600 tokens（原400） |
| Hint类型2提取失败 | 中 | 多层fallback：问句 → 陈述 → AI回复片段 |
| 对话历史过长超出token | 低 | 只取最近6条消息（约3轮） |
| 场景匹配失败 | 低 | 5种场景类型 + default兜底 |
| Hint类型1/3重叠 | 低 | 已在模板中明确区分（ACK vs Target） |

---

## 七、实施顺序（分阶段）

### Phase 1 - 低风险，高价值 ✅

| 优先级 | 修改项 | 工作量 |
|--------|--------|--------|
| P0 | 统一Hint数量为3个 | 低 |
| P0 | 添加scene_desc到Prompt | 低 |
| P0 | 提高max_tokens到600 | 极低 |
| P2 | 优化回退策略（5种场景类型） | 低 |

### Phase 2 - 中风险，高价值 🔧

| 优先级 | 修改项 | 工作量 |
|--------|--------|--------|
| P1 | 添加教练问题提取（多级fallback） | 中 |
| P1 | 改进Hint类型定义（ACK/ANSWER/TARGET） | 中 |
| P1 | 添加current_intent到Prompt | 低 |

### Phase 3 - 高风险，锦上添花 🎯

| 优先级 | 修改项 | 工作量 |
|--------|--------|--------|
| P2 | 添加对话历史上下文（最近3轮） | 中 |
| P2 | 添加Hint去重机制 | 中 |

---

## 八、评审反馈摘要

本方案 v1.1 根据评审意见进行了以下修正：

1. **max_tokens**: 400 → 600（原风险评估遗漏此问题）
2. **Hint类型2**: 添加多级fallback机制，确保可靠性
3. **对话上下文**: 添加最近3轮对话历史（6条消息）
4. **类型区分**: 明确 Type 1 为 Acknowledge，Type 3 为 Use Target
5. **回退策略**: 从1种扩展到5种场景类型
6. **历史记录时机**: 修正为在LLM调用后记录
7. **Type 1示例**: 改为更通用的 "I see.", "Okay.", "Got it."
8. **current_intent**: 添加到模板中指导Hint生成
9. **约束细化**: 添加完整性、大小写、避免填充词等约束
10. **负面示例**: 明确正面vs负面示例

---

## 九、实际实施记录

### 9.1 修改的文件

| 文件 | 修改内容 |
|------|----------|
| `python_backend/core/dialogue_engine.py` | 1. 重写 EVALUATOR_V2_TEMPLATE<br>2. 增强 build_evaluator_prompt() 函数 |
| `python_backend/api/websocket/coach_ws.py` | 1. 添加场景回退Hint字典<br>2. 优化回退策略<br>3. 添加Hint历史记录<br>4. 添加对话历史上下文<br>5. 修改函数签名添加session_transcript参数 |
| `python_backend/application/services/session_service.py` | 更新 EVALUATOR_TOOLS schema：Hint数量改为固定3个 |

### 9.2 关键代码变更

**1. EVALUATOR_V2_TEMPLATE (dialogue_engine.py:355-450)**
- 固定3个Hint
- 三种类型：Acknowledge/Confirm、Respond to Question、Use Target Expression
- 添加场景描述、轮次、教练问题等上下文
- 添加历史Hint去重指令

**2. build_evaluator_prompt() (dialogue_engine.py:720-800)**
- 添加 scene_desc、turn_count、max_turns
- 添加 last_coach_question、recent_hints
- 添加 current_intent

**3. _run_background_evaluator() (coach_ws.py:94-200)**
- 新增 session_transcript 参数
- 添加教练问题提取逻辑
- 添加对话历史上下文构建
- 优化回退策略（场景相关Hint）
- 提高max_tokens到600

**4. SCENARIO_FALLBACK_HINTS (coach_ws.py:51-67)**
- 5种场景类型：ordering、payment、complaint、greeting、default

**5. EVALUATOR_TOOLS schema (session_service.py:23-29)**
- minItems: 3, maxItems: 3

### 9.3 评审细节问题修正

| 评审问题 | 实际处理 |
|----------|----------|
| session_transcript引用方式 | 作为参数传入_run_background_evaluator() |
| Type 2/Type 1边界 | Type 2明确为"回答问题"，Type 1为"确认" |
| Type 1的DO NOT列表 | 改为更宽松的"ordering-specific"限制 |
