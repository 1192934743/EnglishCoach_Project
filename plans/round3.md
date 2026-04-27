阶段三实施方案：发牌器与调度重构 — 微场景流转闭环
一、现状诊断
1.1 已有工作（阶段一 + 阶段二）
组件	完成状态
MicroScenario / ScenarioConstraint / ScenarioTransition ORM 模型
✅ database.py
ScenarioConstraintItem / MicroScenarioInfo / TaskPacket.has_constraints()
✅ task_packet.py
build_scenario_graph.py 图谱构建
✅ scripts/
STATIC_SYSTEM_TEMPLATE_V2 / DYNAMIC_TURN_V2_TEMPLATE / EVALUATOR_V2_TEMPLATE
✅ dialogue_engine.py
advance_state_machine 微场景流转 + scenario_completed 信号
✅ dialogue_engine.py
coach_ws.py 接收 scenario_completed 并发 WebSocket 事件
✅ coach_ws.py
session_ctx["next_scenario"]
✅ advance_state_machine 已填充
1.2 关键缺口（阶段三需补全）
coach_ws.py 接收 scenario_completed
    ↓ (已有但未完成)
session_ctx["next_scenario"] 已填充
    ↓ (缺口1: 没有根据 next_scenario_id 加载下一微场景数据)
没有重建新版 TaskPacket（含 current_scenario + constraints）
    ↓ (缺口2: session_planner.py 没有 MicroScenario 查询函数)
二、session_planner.py 重构
2.1 核心设计：双模式自动路由
# session_planner.py
# 新增导入
from database import (
    MicroScenario,    # 阶段一新增
    ScenarioConstraint,  # 阶段一新增
    ScenarioTransition,  # 阶段一新增
)
# 新增常量：微场景模式判定
def _has_micro_scenarios(db: Session, topic_id: int) -> bool:
    """判断某 topic 下是否有微场景数据（用于双模式路由）"""
    return db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id
    ).first() is not None
路由决策表：

调用方	条件	走哪个路径
build_task_packet() (首次选题)
topic 下有 MicroScenario
新路径 (_build_micro_task_packet)
build_task_packet() (首次选题)
topic 下无 MicroScenario
旧路径 (_build_packet_for_topic)
build_task_packet_for_topic() (用户主动选)
条件同上
同上
build_task_packet_for_next_scenario() (微场景流转)
scenario_id 指定
强制新路径
2.2 新增公开接口
def build_task_packet_for_next_scenario(
    user_id: str,
    next_scenario_id: int,
    db: Optional[Session] = None,
) -> TaskPacket:
    """
    阶段三新增：微场景流转时，根据 next_scenario_id 构建新版 TaskPacket。
    职责：
    1. 根据 scenario_id 加载 MicroScenario
    2. 查询关联的 ScenarioConstraint，填充 constraints
    3. 查询 ScenarioTransition 出边，填充 available_transitions
    4. 计算 depth_tier、max_reply_sentences 等
    """
2.3 内部函数：build_micro_task_packet
def _build_micro_task_packet(
    user_id: str,
    topic: Topic,
    micro_scenario: MicroScenario,
    db: Session,
) -> TaskPacket:
    """
    为单个微场景构建完整的新版 TaskPacket。
    """
    depth_tier = micro_scenario.depth_level
    # 1. 加载关联的 ScenarioConstraint
    constraints = db.query(ScenarioConstraint).filter(
        ScenarioConstraint.micro_scenario_id == micro_scenario.id
    ).all()
    # 2. 构建 ScenarioConstraintItem 列表
    from domain.entities.task_packet import ScenarioConstraintItem
    constraint_items = [
        ScenarioConstraintItem(
            constraint_id=c.id,
            constraint_text=c.constraint_text,
            constraint_type=c.constraint_type,
            depth_level=c.depth_level,
            weight=c.weight,
            hint_cn=c.hint_cn,
        )
        for c in constraints
    ]
    # 3. 加载该场景的出边（available_transitions）
    transitions = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == micro_scenario.id,
        ScenarioTransition.trigger_type.in_(["auto", "intent_driven"]),
    ).all()
    # 加载 to_scenario 的基本信息
    to_ids = [t.to_scenario_id for t in transitions]
    to_scenarios = {
        s.id: s for s in db.query(MicroScenario).filter(
            MicroScenario.id.in_(to_ids)
        ).all()
    }
    available_transitions = [
        {
            "scenario_id": t.to_scenario_id,
            "scenario_code": to_scenarios.get(t.to_scenario_id, None) and to_scenarios[t.to_scenario_id].scenario_code or "",
            "scenario_name": to_scenarios.get(t.to_scenario_id, None) and to_scenarios[t.to_scenario_id].scenario_name or "",
            "overlap_ratio": t.overlap_ratio,
            "required_hit_rate": t.required_hit_rate,
        }
        for t in transitions
    ]
    # 4. 构建 MicroScenarioInfo
    from domain.entities.task_packet import MicroScenarioInfo
    scenario_info = MicroScenarioInfo(
        scenario_id=micro_scenario.id,
        scenario_code=micro_scenario.scenario_code,
        scenario_name=micro_scenario.scenario_name,
        intent_desc=micro_scenario.intent_desc,
        scene_desc=micro_scenario.scene_desc,
        depth_level=micro_scenario.depth_level,
        step_order=micro_scenario.step_order,
        max_turns=micro_scenario.max_turns,
        available_transitions=available_transitions,
    )
    # 5. 加载用户复习约束（旧 depth_level 的未掌握节点）
    from domain.entities.task_packet import ScenarioConstraintItem as SCI
    review_items = _load_review_constraints(user_id, topic.id, depth_tier, db)
    # 6. DifficultyConfig
    diff_config = DifficultyConfig(
        first_turn_depth=depth_tier,
        bonus_node_unlock_after=3,
        correction_frequency=min(0.5, 0.1 + depth_tier * 0.1),
    )
    # 7. learner_level + max_reply_sentences
    user = db.query(User).filter(User.id == user_id).first()
    settings = dict(user.settings or {}) if user else None
    topic_lv = topic.learner_level or "Intermediate"
    eff_label = effective_learner_label(settings, topic_lv, topic_lv)
    max_reply = compute_max_reply_sentences(eff_label, depth_tier)
    # 8. scene_specific_rules（从 Topic 继承）
    difficulty_tiers_cfg: dict = topic.difficulty_tiers or {}
    tier_rules: list = difficulty_tiers_cfg.get(str(depth_tier), {}).get("rules", [])
    scene_rules: list = (topic.scene_specific_rules or []) + tier_rules
    tzh = effective_topic_title_zh(topic)
    return TaskPacket(
        topic_id=topic.id,
        topic_title=topic.title,
        topic_title_zh=tzh,
        scene_prompt=topic.system_prompt or topic.title,
        role_name=topic.role_name or topic.category or "English Coach",
        learner_level=eff_label,
        voice=topic.voice or "Stanley",
        depth_tier=depth_tier,
        max_reply_sentences=max_reply,
        current_scenario=scenario_info,
        constraints=constraint_items,
        bonus_constraints=[],   # 阶段三：bonus_constraints 逻辑待定
        review_constraints=review_items,
        current_intent=micro_scenario.intent_desc,  # 关键：intent_desc → current_intent
        difficulty_config=diff_config,
        scene_specific_rules=scene_rules,
        vocab_tags=topic.vocab_tags or [],
        sentence_patterns=topic.sentence_patterns or [],
        session_goal=_build_micro_session_goal(micro_scenario, constraint_items),
    )
def _load_review_constraints(
    user_id: str,
    topic_id: int,
    current_depth: int,
    db: Session,
) -> list:
    """加载低于 current_depth 的未掌握约束（用于复习）"""
    from domain.entities.task_packet import ScenarioConstraintItem as SCI
    # 找出 lower depth constraints 未掌握的部分
    lower_constraints = db.query(ScenarioConstraint).join(
        MicroScenario, ScenarioConstraint.micro_scenario_id == MicroScenario.id
    ).filter(
        MicroScenario.topic_id == topic_id,
        ScenarioConstraint.depth_level < current_depth,
    ).all()
    if not lower_constraints:
        return []
    # 查 UserProgress，找未掌握的（mastery < 80）
    progress_map = {
        p.node_id: p.mastery_score
        for p in db.query(UserProgress).filter(
            UserProgress.user_id == user_id,
            UserProgress.node_id.in_([c.legacy_node_id for c in lower_constraints if c.legacy_node_id])
        ).all()
    }
    review = []
    for c in lower_constraints:
        legacy_id = c.legacy_node_id
        if legacy_id and progress_map.get(legacy_id, 0) < 80.0:
            review.append(SCI(
                constraint_id=c.id,
                constraint_text=c.constraint_text,
                constraint_type=c.constraint_type,
                depth_level=c.depth_level,
                weight=c.weight,
                hint_cn=c.hint_cn,
            ))
    return review[:3]  # 最多 3 个复习约束
def _build_micro_session_goal(
    scenario: MicroScenario,
    constraints: list,
) -> str:
    """为微场景生成 session_goal（给 Actor LLM 的人类可读目标）"""
    names = [f"'{c.constraint_text}'" for c in constraints[:3]]
    if names:
        return f"Practice: {scenario.scenario_name}. Try to use: {', '.join(names)}."
    return f"Practice: {scenario.scenario_name}."
2.4 修正 _build_packet_for_topic 的路由逻辑
def _build_packet_for_topic(
    user_id: str,
    topic: Topic,
    depth_preference: float,
    db: Session,
) -> TaskPacket:
    """
    双模式路由：
    - 若 topic 下有微场景数据，走新版微场景逻辑
    - 否则走旧版 TargetNode 逻辑（完全保留，向后兼容）
    """
    if _has_micro_scenarios(db, topic.id):
        # === 新路径：选入口微场景 ===
        earned_tier = _compute_depth_tier(user_id, topic.id, db)
        max_allowed = max(1, min(MAX_DEPTH_TIER, int(depth_preference)))
        depth_tier = min(earned_tier, max_allowed)
        # 优先选 is_entry_point=True 的场景
        entry = db.query(MicroScenario).filter(
            MicroScenario.topic_id == topic.id,
            MicroScenario.depth_level == depth_tier,
            MicroScenario.is_entry_point == True,
        ).first()
        if entry is None:
            # 降级：选该 depth 下 step_order 最小的
            entry = db.query(MicroScenario).filter(
                MicroScenario.topic_id == topic.id,
                MicroScenario.depth_level == depth_tier,
            ).order_by(MicroScenario.step_order).first()
        if entry is None:
            # 彻底没有微场景，降级到旧逻辑
            logger.warning(
                f"[SessionPlanner] No MicroScenario found for topic={topic.id}, "
                f"depth={depth_tier}. Falling back to legacy mode."
            )
            return _build_packet_for_topic_legacy(user_id, topic, depth_preference, db)
        logger.info(
            f"[SessionPlanner] Micro mode: topic='{topic.title}' "
            f"scenario='{entry.scenario_code}' depth={depth_tier}"
        )
        return _build_micro_task_packet(user_id, topic, entry, db)
    else:
        # === 旧路径：TargetNode 逻辑 ===
        return _build_packet_for_topic_legacy(user_id, topic, depth_preference, db)
注：将原有的 _build_packet_for_topic 内部逻辑重命名为 _build_packet_for_topic_legacy，保持代码整洁。

2.5 build_task_packet_for_next_scenario 完整实现
def build_task_packet_for_next_scenario(
    user_id: str,
    next_scenario_id: int,
    db: Optional[Session] = None,
) -> TaskPacket:
    """
    微场景流转时：根据 next_scenario_id 构建新版 TaskPacket。
    关键设计：此函数不查 Topic 评分，直接加载指定 scenario，
    因此不需要用户评分和话题选择逻辑。
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        # 1. 加载微场景
        scenario = db.query(MicroScenario).filter(
            MicroScenario.id == next_scenario_id
        ).first()
        if scenario is None:
            logger.error(f"[SessionPlanner] MicroScenario id={next_scenario_id} not found")
            return _fallback_task_packet(db)
        # 2. 加载 Topic
        topic = db.query(Topic).filter(Topic.id == scenario.topic_id).first()
        if topic is None:
            logger.error(f"[SessionPlanner] Topic id={scenario.topic_id} not found")
            return _fallback_task_packet(db)
        logger.info(
            f"[SessionPlanner] Transition to scenario: "
            f"'{scenario.scenario_code}' ({scenario.scenario_name}) "
            f"topic='{topic.title}'"
        )
        return _build_micro_task_packet(user_id, topic, scenario, db)
    except Exception as e:
        logger.error(
            f"[SessionPlanner] build_task_packet_for_next_scenario error: {e}",
            exc_info=True
        )
        return _fallback_task_packet(db)
    finally:
        if should_close:
            db.close()
三、coach_ws.py 流转闭环补全
3.1 现状分析
当前 coach_ws.py 中的 scenario_completed 处理块只做了两件事：

发送 WebSocket scenario_completed 事件给前端
清理 session_ctx 信号
缺失：没有根据 next_scenario_id 调用 session_planner.build_task_packet_for_next_scenario 重建 TaskPacket。

3.2 修改点
在 if did_transition: 块中，微场景通关分支中追加 TaskPacket 重建逻辑：

# coach_ws.py — if did_transition: 块中，微场景通关分支
if micro_mode and session_ctx.get("scenario_completed"):
    next_sc = session_ctx.get("next_scenario")
    await safe_send_ws(websocket, ws_lock, {
        "event": "scenario_completed",
        "scenario_code": (
            current_task_packet.current_scenario.scenario_code
            if current_task_packet and current_task_packet.current_scenario
            else ""
        ),
        "next_scenario": next_sc,
    })
    # ── 阶段三核心：重建下一微场景的 TaskPacket ──────────────────────────
    if next_sc and next_sc.get("scenario_id"):
        next_scenario_id = next_sc["scenario_id"]
        current_task_packet = await run_in_threadpool(
            session_planner.build_task_packet_for_next_scenario,
            current_user.id,
            next_scenario_id,
        )
        topic_id_for_progress = current_task_packet.topic_id
        session_id = str(uuid.uuid4())
        session_hits.clear()
        session_transcript.clear()
        mastery_snapshot = await run_in_threadpool(
            _take_mastery_snapshot, current_user.id, current_task_packet
        )
        logger.info(
            f"[SCENARIO] Transitioned to: "
            f"'{current_task_packet.current_scenario.scenario_code}' "
            f"(constraints={len(current_task_packet.constraints)})"
        )
    else:
        # 没有后续场景：教研人员需要配置，话题结束
        logger.warning(
            "[SCENARIO] No next_scenario available. "
            "Topic exhausted — recommend adding more micro-scenarios."
        )
    # ── 清理信号（与阶段二相同）────────────────────────────────────────
    session_ctx["scenario_completed"] = False
    session_ctx.pop("director_scenario_completed", None)
    session_ctx.pop("director_constraints_hit", None)
    session_ctx.pop("director_intent_achieved", None)
    session_ctx.pop("_constraint_hits", None)
    logger.info(f"[SCENARIO] scenario_completed event sent")
四、向导导入补全
session_planner.py 需要新增 MicroScenario、ScenarioConstraint、ScenarioTransition 导入：

# session_planner.py 头部 import 段
from database import (
    SessionLocal,
    User,
    Topic,
    TargetNode,
    UserProgress,
    LearningSession,
    effective_topic_title_zh,
    # 阶段三新增
    MicroScenario,
    ScenarioConstraint,
    ScenarioTransition,
)
五、向后兼容策略总结
场景	行为
Topic 下无 MicroScenario 数据
降级到旧 _build_packet_for_topic_legacy，所有阶段二/阶段三逻辑不触发
ScenarioTransition 出边为空
session_ctx["next_scenario"] = None，发送 scenario_completed 但不重建 TaskPacket，提示教研人员
教研人员配置了单向边
只允许单向流转，避免循环
next_scenario_id 对应场景不存在
捕获异常，降级到 _fallback_task_packet
旧 session_ctx（无 turn_count_in_scenario）
SessionContext dataclass 有默认值 0，兼容
六、文件变更清单
文件	操作	变更说明
application/services/session_planner.py
修改
新增 MicroScenario/ScenarioConstraint/ScenarioTransition 导入；新增 build_task_packet_for_next_scenario()、_build_micro_task_packet()、_has_micro_scenarios()、_load_review_constraints()、_build_micro_session_goal()；重构 _build_packet_for_topic() 双模式路由
api/websocket/coach_ws.py
修改
scenario_completed 分支追加 build_task_packet_for_next_scenario() 调用，重建 TaskPacket
是否同意该方案并开始阶段三的编码？