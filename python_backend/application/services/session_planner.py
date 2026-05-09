"""
SessionPlanner — LMS 的纯算法决策大脑

核心原则（来自架构设计）：
- 严禁任何 LLM 调用：输出的 TaskPacket 必须 100% 稳定、可预测，不存在幻觉风险
- 所有决策基于数学公式和数据库查询

话题评分公式：
    score = similarity_score * 40
          + review_urgency   * 40
          + novelty_score    * 20

深度晋级规则：
    某话题 depth_tier=N 的所有节点平均掌握度 > 75% → 晋级到 depth_tier=N+1

VectorStore 生命周期：
    _store 为模块级单例，服务启动时调用 warm_up() 初始化，之后每次 build_task_packet 直接使用。
    新话题生成后调用 add_topic_to_store(topic) 追加（无需全量重建）。
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Optional

from sqlalchemy.orm import Session

from database import (
    SessionLocal,
    User,
    Topic,
    LearningSession,
    effective_topic_title_zh,
    MicroScenario,
    ScenarioConstraint,
    ScenarioTransition,
)

from domain.entities.task_packet import (
    TaskPacket,
    DifficultyConfig,
    ScenarioConstraintItem,
    MicroScenarioInfo,
    compute_max_reply_sentences,
    effective_learner_label,
)
from infrastructure.vector_store.numpy_store import NumpyVectorStore, embed_topics_bow

logger = logging.getLogger("EnglishCoach")

# ── 模块级 VectorStore 单例（服务启动时由 warm_up() 初始化）─────────────────
_store: NumpyVectorStore = NumpyVectorStore()
_store_ready: bool = False

# ── 深度晋级阈值 ──────────────────────────────────────────────────────────
MASTERY_THRESHOLD_FOR_TIER_UP = 75.0   # 当前 tier 所有节点平均掌握度超过此值则晋级
MAX_DEPTH_TIER = 3                      # 话题最高深度层级（与 difficulty_tiers 的 key 对应）
# 与 /api/topics 的 avg_mastery 一致：平均掌握度超过此值后，自动选题明显降权（仍可手动点练）
MASTERY_AUTO_PICK_SOFT_CAP = 88.0

# ── 遗忘曲线参数（简化 SM-2）──────────────────────────────────────────────
REVIEW_URGENCY_HALF_LIFE_DAYS = 3.0    # 半衰期（天）：练习 N 天后复习紧迫度达 50%

# ── 虫洞防沉迷阈值 ─────────────────────────────────────────────────────
MAX_WORMHOLES_PER_SESSION = 1          # 每个会话最多触发 1 次虫洞（防止横向沉迷）


# ═══════════════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════════════

def warm_up(db: Optional[Session] = None) -> None:
    """
    服务启动时调用，加载所有话题向量到内存 VectorStore。

    优先使用 Topic.embedding（真实 embedding）；
    若为空则降级到 BOW 向量（基于 vocab_tags + sentence_patterns）。
    """
    global _store, _store_ready
    _store.clear()

    should_close = db is None
    db = db or SessionLocal()
    try:
        topics = db.query(Topic).all()
        if not topics:
            logger.warning("⚠️ SessionPlanner.warm_up: 数据库中没有话题，VectorStore 为空。")
            return

        topics_data = [
            {
                "id": t.id,
                "vocab_tags": t.vocab_tags or [],
                "sentence_patterns": t.sentence_patterns or [],
                "embedding": t.embedding,
            }
            for t in topics
        ]

        # 优先用真实 embedding，没有则 BOW 降级
        use_bow_ids = [t["id"] for t in topics_data if not t["embedding"]]
        if use_bow_ids:
            bow_vectors = embed_topics_bow([t for t in topics_data if t["id"] in use_bow_ids])
        else:
            bow_vectors = {}

        for t in topics_data:
            vec = t["embedding"] if t["embedding"] else bow_vectors.get(t["id"])
            if vec:
                _store.add(t["id"], vec)

        _store_ready = True
        logger.info(f"✅ SessionPlanner: VectorStore 已就绪，已加载 {len(topics)} 个话题向量。")
    finally:
        if should_close:
            db.close()


def build_task_packet(user_id: str, db: Optional[Session] = None) -> TaskPacket:
    """
    为指定用户生成本次练习的 TaskPacket。

    全程纯算法，无 LLM 调用，输出保证合法。
    失败时降级返回默认 TaskPacket（绝不抛出异常给调用方）。
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        return _build(user_id, db)
    except Exception as e:
        logger.error(f"💥 SessionPlanner.build_task_packet 异常: {e}", exc_info=True)
        return _fallback_task_packet(db)
    finally:
        if should_close:
            db.close()


def build_task_packet_for_topic(
    user_id: str,
    topic_id: int,
    db: Optional[Session] = None,
) -> TaskPacket:
    """
    为指定话题生成 TaskPacket，绕过推荐评分（用户主动选择话题时调用）。
    深度晋级逻辑与 build_task_packet 完全一致。
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        topic = db.query(Topic).filter(Topic.id == topic_id).first()
        if topic is None:
            return _fallback_task_packet(db)

        user = db.query(User).filter(User.id == user_id).first()
        settings = user.settings if user else {}
        depth_preference = float(settings.get("depth_preference", 1.0) if settings else 1.0)

        return _build_packet_for_topic(user_id, topic, depth_preference, db)
    except Exception as e:
        logger.error(f"build_task_packet_for_topic error: {e}", exc_info=True)
        return _fallback_task_packet(db)
    finally:
        if should_close:
            db.close()


def add_topic_to_store(topic) -> None:
    """
    新话题生成后立刻追加到 VectorStore（无需全量 warm_up）。
    topic 可以是 ORM Topic 对象或含 id/vocab_tags/sentence_patterns 的 dict。
    """
    global _store_ready
    try:
        if hasattr(topic, "id"):
            tid = topic.id
            vocab = topic.vocab_tags or []
            patterns = topic.sentence_patterns or []
            embedding = getattr(topic, "embedding", None)
        else:
            tid = topic["id"]
            vocab = topic.get("vocab_tags", [])
            patterns = topic.get("sentence_patterns", [])
            embedding = topic.get("embedding")

        if embedding:
            _store.add(tid, embedding)
        else:
            # BOW 降级
            from infrastructure.vector_store.numpy_store import embed_topics_bow
            bows = embed_topics_bow([{"id": tid, "vocab_tags": vocab, "sentence_patterns": patterns}])
            if tid in bows:
                _store.add(tid, bows[tid])

        _store_ready = True
        logger.info(f"[SessionPlanner] Topic id={tid} added to VectorStore.")
    except Exception as e:
        logger.warning(f"[SessionPlanner] add_topic_to_store failed (non-critical): {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 阶段三新增：微场景图谱发牌接口
# ═══════════════════════════════════════════════════════════════════════════


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
    5. 加载复习约束（低于 current_depth 的未掌握节点）

    若 scenario_id 对应场景不存在，捕获异常降级到 _fallback_task_packet。
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        scenario = db.query(MicroScenario).filter(
            MicroScenario.id == next_scenario_id
        ).first()
        if scenario is None:
            logger.error(
                f"[SessionPlanner] MicroScenario id={next_scenario_id} not found"
            )
            return _fallback_task_packet(db)

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
            exc_info=True,
        )
        return _fallback_task_packet(db)
    finally:
        if should_close:
            db.close()


# ═══════════════════════════════════════════════════════════════════════════
# 阶段三新增：内部函数
# ═══════════════════════════════════════════════════════════════════════════


def _has_micro_scenarios(db: Session, topic_id: int) -> bool:
    """
    判断某 topic 下是否有微场景数据（用于双模式路由）。
    若有，返回 True，优先使用微场景图谱模式。
    """
    return db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id
    ).first() is not None


def _build_micro_task_packet(
    user_id: str,
    topic: Topic,
    micro_scenario: MicroScenario,
    db: Session,
) -> TaskPacket:
    """
    为单个微场景构建完整的新版 TaskPacket。

    填充字段：
    - current_scenario: MicroScenarioInfo（含 available_transitions）
    - constraints: 该场景绑定的所有 ScenarioConstraint
    - review_constraints: 低于 current_depth 的未掌握约束（最多 3 个）
    - current_intent: 微场景的 intent_desc
    """
    depth_tier = micro_scenario.depth_level

    constraints_orm = db.query(ScenarioConstraint).filter(
        ScenarioConstraint.micro_scenario_id == micro_scenario.id
    ).all()

    constraint_items = [
        ScenarioConstraintItem(
            constraint_id=c.id,
            constraint_text=c.constraint_text,
            constraint_type=c.constraint_type,
            depth_level=c.depth_level,
            weight=c.weight,
            hint_cn=c.hint_cn,
        )
        for c in constraints_orm
    ]

    transitions = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == micro_scenario.id,
        ScenarioTransition.trigger_type.in_(["auto", "intent_driven"]),
    ).all()

    to_ids = [t.to_scenario_id for t in transitions]
    to_scenarios_map = (
        {s.id: s for s in db.query(MicroScenario).filter(MicroScenario.id.in_(to_ids)).all()}
        if to_ids else {}
    )

    available_transitions = [
        {
            "scenario_id": t.to_scenario_id,
            "scenario_code": (
                to_scenarios_map[t.to_scenario_id].scenario_code
                if t.to_scenario_id in to_scenarios_map else ""
            ),
            "scenario_name": (
                to_scenarios_map[t.to_scenario_id].scenario_name
                if t.to_scenario_id in to_scenarios_map else ""
            ),
            "overlap_ratio": t.overlap_ratio,
            "required_hit_rate": t.required_hit_rate,
        }
        for t in transitions
    ]

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

    review_items = _load_review_constraints(user_id, topic.id, depth_tier, db)

    diff_config = DifficultyConfig(
        first_turn_depth=depth_tier,
        bonus_node_unlock_after=3,
        correction_frequency=min(0.5, 0.1 + depth_tier * 0.1),
    )

    user = db.query(User).filter(User.id == user_id).first()
    settings = dict(user.settings or {}) if user else None
    topic_lv = topic.learner_level or "Intermediate"
    eff_label = effective_learner_label(settings, topic_lv, topic_lv)
    max_reply = compute_max_reply_sentences(eff_label, depth_tier)

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
        bonus_constraints=[],
        review_constraints=review_items,
        current_intent=micro_scenario.intent_desc,
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
) -> list[ScenarioConstraintItem]:
    """
    加载低于 current_depth 的未掌握约束（最多 3 个）。

    基于 LearningSession.nodes_mastered 判断是否已掌握。
    """
    lower_constraints = db.query(ScenarioConstraint).join(
        MicroScenario,
        ScenarioConstraint.micro_scenario_id == MicroScenario.id,
    ).filter(
        MicroScenario.topic_id == topic_id,
        ScenarioConstraint.depth_level < current_depth,
    ).all()

    if not lower_constraints:
        return []

    mastered_ids = set()
    sessions = db.query(LearningSession).filter(
        LearningSession.user_id == user_id,
        LearningSession.topic_id == topic_id,
    ).all()
    for s in sessions:
        if s.nodes_mastered:
            mastered_ids.update(s.nodes_mastered)

    review = []
    for c in lower_constraints:
        if c.id not in mastered_ids:
            review.append(ScenarioConstraintItem(
                constraint_id=c.id,
                constraint_text=c.constraint_text,
                constraint_type=c.constraint_type,
                depth_level=c.depth_level,
                weight=c.weight,
                hint_cn=c.hint_cn,
            ))

    return review[:3]


def _build_micro_session_goal(
    scenario: MicroScenario,
    constraints: list[ScenarioConstraintItem],
) -> str:
    """
    为微场景生成 session_goal（人类可读的练习目标）。
    """
    names = [f"'{c.constraint_text}'" for c in constraints[:3]]
    if names:
        return f"Practice: {scenario.scenario_name}. Try to use: {', '.join(names)}."
    return f"Practice: {scenario.scenario_name}."


def save_learning_session(
    user_id: str,
    task_packet: TaskPacket,
    nodes_hit: set[int],
    db: Optional[Session] = None,
) -> None:
    """
    在 WRAP_UP 阶段结束时调用，将本次练习记录写入 LearningSession 表。
    nodes_hit: server.py 维护的 session_hits（已击中的节点 ID 集合）
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        mastered_ids = [
            nid for nid in nodes_hit
            if _get_constraint_mastery(user_id, nid, db) >= MASTERY_THRESHOLD_FOR_TIER_UP
        ]

        session = LearningSession(
            user_id=user_id,
            topic_id=task_packet.topic_id,
            end_time=datetime.datetime.utcnow(),
            depth_tier_used=task_packet.depth_tier,
            nodes_attempted=list(nodes_hit),
            nodes_mastered=mastered_ids,
            task_packet_snapshot=task_packet.to_dict(),
        )
        db.add(session)
        db.commit()
        logger.info(
            f"📝 LearningSession 已保存: topic={task_packet.topic_title}, "
            f"depth={task_packet.depth_tier}, hit={len(nodes_hit)}, mastered={len(mastered_ids)}"
        )
    except Exception as e:
        logger.error(f"💥 save_learning_session 异常: {e}", exc_info=True)
        if not should_close:
            db.rollback()
    finally:
        if should_close:
            db.close()


# ═══════════════════════════════════════════════════════════════════════════
# 内部实现
# ═══════════════════════════════════════════════════════════════════════════

def _build_packet_for_topic(
    user_id: str,
    topic: Topic,
    depth_preference: float,
    db: Session,
) -> TaskPacket:
    """
    构建指定话题的 TaskPacket。
    优先使用微场景模式。
    """
    if _has_micro_scenarios(db, topic.id):
        return _build_packet_for_topic_micro(user_id, topic, depth_preference, db)
    else:
        return _fallback_task_packet(db)


def _build_packet_for_topic_micro(
    user_id: str,
    topic: Topic,
    depth_preference: float,
    db: Session,
) -> TaskPacket:
    """
    微场景模式：选择入口微场景并构建 TaskPacket。

    选择策略：
    1. 根据用户在该话题的有效掌握度，计算 earned_tier
    2. 在该 tier 下，优先选 is_entry_point=True 的场景
    3. 若没有 entry point，选 step_order 最小的场景
    4. 若彻底没有微场景，降级到旧逻辑
    """
    earned_tier = _compute_depth_tier(user_id, topic.id, db)
    max_allowed = max(1, min(MAX_DEPTH_TIER, int(depth_preference)))
    depth_tier = min(earned_tier, max_allowed)

    entry = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic.id,
        MicroScenario.depth_level == depth_tier,
        MicroScenario.is_entry_point == True,
    ).first()

    if entry is None:
        entry = db.query(MicroScenario).filter(
            MicroScenario.topic_id == topic.id,
            MicroScenario.depth_level == depth_tier,
        ).order_by(MicroScenario.step_order).first()

    if entry is None:
        logger.warning(
            f"[SessionPlanner] No MicroScenario for topic={topic.id}, depth={depth_tier}. "
            f"Falling back to default packet."
        )
        return _fallback_task_packet(db)

    logger.info(
        f"[SessionPlanner] Micro mode: topic='{topic.title}' "
        f"scenario='{entry.scenario_code}' depth={depth_tier}"
    )
    return _build_micro_task_packet(user_id, topic, entry, db)


def _build(user_id: str, db: Session) -> TaskPacket:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return _fallback_task_packet(db)

    settings = user.settings or {}
    depth_preference = float(settings.get("depth_preference", 1.0))  # 用户偏好的深度上限
    new_topic_appetite = float(settings.get("new_topic_appetite", 0.2))

    # ── 获取历史 session（用于相似度 + 新鲜度计算）────────────────────────
    recent_sessions = (
        db.query(LearningSession)
        .filter(LearningSession.user_id == user_id)
        .order_by(LearningSession.start_time.desc())
        .limit(10)
        .all()
    )
    recent_topic_ids: list[int] = [s.topic_id for s in recent_sessions]
    total_sessions = len(recent_sessions)

    # ── 对所有话题打分 ────────────────────────────────────────────────────
    all_topics = db.query(Topic).all()
    if not all_topics:
        return _fallback_task_packet(db)

    scored: list[tuple[float, Topic]] = []
    for topic in all_topics:
        score = _score_topic(
            topic, user_id, recent_topic_ids, total_sessions, new_topic_appetite, db
        )
        scored.append((score, topic))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_topic = scored[0][0], scored[0][1]
    avg_pick = _topic_avg_mastery_display(best_topic.id, user_id, db)

    packet = _build_packet_for_topic(user_id, best_topic, depth_preference, db)
    logger.info(
        f"[SessionPlanner] TaskPacket: topic='{best_topic.title}' score={best_score:.2f} "
        f"avg_mastery={avg_pick:.1f}% tier={packet.depth_tier}"
    )
    return packet



# ── 话题评分 ──────────────────────────────────────────────────────────────

def _topic_avg_mastery_display(topic_id: int, user_id: str, db: Session) -> float:
    """
    计算话题平均掌握度：基于 LearningSession.nodes_mastered。
    已掌握的 constraint 数 / 该话题总 constraint 数。
    """
    constraints = db.query(ScenarioConstraint).join(
        MicroScenario,
        ScenarioConstraint.micro_scenario_id == MicroScenario.id,
    ).filter(
        MicroScenario.topic_id == topic_id
    ).all()

    if not constraints:
        return 0.0

    total_constraints = len(constraints)
    constraint_ids = {c.id for c in constraints}

    mastered_ids = set()
    sessions = db.query(LearningSession).filter(
        LearningSession.user_id == user_id,
        LearningSession.topic_id == topic_id,
    ).all()
    for s in sessions:
        if s.nodes_mastered:
            mastered_ids.update(s.nodes_mastered)

    mastered_count = len(constraint_ids & mastered_ids)
    return (mastered_count / total_constraints) * 100.0


def _mastery_gap_priority(avg_mastery: float) -> float:
    """
    0~1：平均掌握度越低越接近 1，便于优先安排「未满」的话题；
    已达 MASTERY_AUTO_PICK_SOFT_CAP 的话题接近谷底，避免「都快练完了还总被自动选中」。
    """
    if avg_mastery >= MASTERY_AUTO_PICK_SOFT_CAP:
        return 0.08
    return max(0.08, (MASTERY_AUTO_PICK_SOFT_CAP - avg_mastery) / MASTERY_AUTO_PICK_SOFT_CAP)


def _score_topic(
    topic: Topic,
    user_id: str,
    recent_topic_ids: list[int],
    total_sessions: int,
    new_topic_appetite: float,
    db: Session,
) -> float:
    """
    话题综合评分（0~100）：
      相似度 32% + 复习紧迫度 32% + 探索新鲜度 16% + 「未满掌握度」缺口 20%

    缺口项与 /api/topics 展示的平均掌握度对齐，使自动开练更倾向平均仍低于 ~88% 的话题。
    """
    # ── 1. 相似度得分（与最近 3 次话题的平均余弦相似度）─────────────────────
    similarity_score = 0.0
    if _store_ready and recent_topic_ids:
        topic_vec = _get_topic_vector(topic.id)
        if topic_vec is not None:
            recent_vecs = [_get_topic_vector(tid) for tid in recent_topic_ids[:3]]
            sims = []
            for rv in recent_vecs:
                if rv is not None:
                    results = _store.get_similar(rv, top_k=len(_store._ids))
                    sim_map = dict(results)
                    sims.append(sim_map.get(topic.id, 0.0))
            similarity_score = sum(sims) / len(sims) if sims else 0.0

    # ── 2. 遗忘复习紧迫度（简化 SM-2 指数衰减）───────────────────────────
    review_urgency = _compute_review_urgency(topic.id, user_id, db)

    # ── 3. 新鲜度（话题被练习得越少，新鲜度越高）────────────────────────────
    practice_count = sum(1 for tid in recent_topic_ids if tid == topic.id)
    if total_sessions > 0:
        novelty = 1.0 - (practice_count / total_sessions)
    else:
        novelty = 1.0
    # 用户 new_topic_appetite 越低，新鲜度权重越低（偏好复习已学话题）
    novelty_score = novelty * new_topic_appetite

    avg_disp = _topic_avg_mastery_display(topic.id, user_id, db)
    gap_priority = _mastery_gap_priority(avg_disp)

    total = (
        similarity_score * 32.0
        + review_urgency * 32.0
        + novelty_score * 16.0
        + gap_priority * 20.0
    )
    return total


def _get_topic_vector(topic_id: int) -> list[float] | None:
    """从 VectorStore 取话题向量（仅用于相似度计算，不暴露给外部）"""
    return _store.get_vector(topic_id)


def _compute_review_urgency(topic_id: int, user_id: str, db: Session) -> float:
    """
    基于最近一次练习时间计算复习紧迫度（0~1）。
    使用指数衰减：urgency = 1 - exp(-elapsed_days / half_life)
    首次练习（无历史）：urgency = 0（还没练过，不用复习）
    """
    last_session = (
        db.query(LearningSession)
        .filter(LearningSession.user_id == user_id, LearningSession.topic_id == topic_id)
        .order_by(LearningSession.start_time.desc())
        .first()
    )
    if last_session is None:
        return 0.0

    elapsed = datetime.datetime.utcnow() - last_session.start_time
    elapsed_days = elapsed.total_seconds() / 86400.0
    urgency = 1.0 - math.exp(-elapsed_days / REVIEW_URGENCY_HALF_LIFE_DAYS)
    return min(1.0, urgency)


# ── 深度层级 ──────────────────────────────────────────────────────────────

def _compute_depth_tier(user_id: str, topic_id: int, db: Session) -> int:
    """
    根据用户对该话题的「有效掌握度」计算应解锁的深度层级。

    基于 LearningSession.nodes_mastered 判断是否已掌握某 constraint。
    逻辑：
    - 从 tier=1 开始，计算该 tier 所有 constraints 中已掌握的比例
    - 若比例 >= MASTERY_THRESHOLD_FOR_TIER_UP / 100，晋级到 tier+1
    - 直到达到 MAX_DEPTH_TIER 或未达到晋级阈值
    """
    for tier in range(1, MAX_DEPTH_TIER + 1):
        constraints = db.query(ScenarioConstraint).join(
            MicroScenario,
            ScenarioConstraint.micro_scenario_id == MicroScenario.id,
        ).filter(
            MicroScenario.topic_id == topic_id,
            MicroScenario.depth_level == tier,
        ).all()

        if not constraints:
            return tier

        constraint_ids = {c.id for c in constraints}
        mastered_ids = set()
        sessions = db.query(LearningSession).filter(
            LearningSession.user_id == user_id,
            LearningSession.topic_id == topic_id,
        ).all()
        for s in sessions:
            if s.nodes_mastered:
                mastered_ids.update(s.nodes_mastered)

        mastered_count = len(constraint_ids & mastered_ids)
        mastery_ratio = mastered_count / len(constraints)

        logger.debug(f"[DepthTier] topic={topic_id} tier={tier} mastered={mastered_count}/{len(constraints)} ratio={mastery_ratio:.2f}")

        if mastery_ratio < MASTERY_THRESHOLD_FOR_TIER_UP / 100.0:
            return tier

    return MAX_DEPTH_TIER


def _get_constraint_mastery(user_id: str, constraint_id: int, db: Session) -> float:
    """
    判断某 constraint 是否已被用户掌握。
    基于 LearningSession.nodes_mastered。
    """
    sessions = db.query(LearningSession).filter(
        LearningSession.user_id == user_id,
    ).all()
    for s in sessions:
        if s.nodes_mastered and constraint_id in s.nodes_mastered:
            return 100.0
    return 0.0


# ── 会话目标描述已由 _build_micro_session_goal 替代 ─────────────────────────

# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 新增：Mastery Score 驱动流转 + 虫洞即时奖励
# ═══════════════════════════════════════════════════════════════════════════

# 虫洞触发阈值
WORMHOLE_MASTERY_THRESHOLD = 85.0   # 掌握度达到此值才允许虫洞跳转
WORMHOLE_STEP_COMPLETE_MIN_SCENARIOS = 3  # Step 完成至少需要练习的场景数

# 动态 Twist 配置
TWIST_PROBABILITY = 0.15   # 每个场景触发 Twist 的概率


def compute_scenario_mastery(
    user_id: str,
    scenario_id: int,
    db: Session,
) -> float:
    """
    计算用户在某个微场景的掌握度。

    基于 LearningSession.nodes_mastered：场景关联的 constraints 中已掌握的比例。
    """
    constraints = db.query(ScenarioConstraint).filter(
        ScenarioConstraint.micro_scenario_id == scenario_id
    ).all()

    if not constraints:
        return 50.0  # 无约束，默认中等掌握度

    constraint_ids = {c.id for c in constraints}

    mastered_ids = set()
    sessions = db.query(LearningSession).filter(
        LearningSession.user_id == user_id,
    ).all()
    for s in sessions:
        if s.nodes_mastered:
            mastered_ids.update(s.nodes_mastered)

    mastered_in_scenario = len(constraint_ids & mastered_ids)
    return (mastered_in_scenario / len(constraints)) * 100.0


def should_wormhole(
    user_id: str,
    current_scenario_id: int,
    current_topic_id: int,
    session_ctx: dict,
    db: Session,
) -> dict | None:
    """
    判断是否应该触发虫洞即时奖励。

    触发条件：
    1. 当前场景掌握度 >= WORMHOLE_MASTERY_THRESHOLD
    2. 当前话题该 Step 已练习 >= WORMHOLE_STEP_COMPLETE_MIN_SCENARIOS 个场景
    3. 本会话已触发虫洞次数 < MAX_WORMHOLES_PER_SESSION（防横向沉迷）

    Returns:
        WormholePacket dict: {
            'target_topic_id': int,
            'target_scenario_id': int,
            'target_scenario_name': str,
            'trigger_type': 'step_complete' | 'topic_complete',
        }
        None: 不触发虫洞
    """
    # 【防沉迷检查】每个会话最多触发 MAX_WORMHOLES_PER_SESSION 次虫洞
    wormhole_count = session_ctx.get("wormhole_count", 0)
    if wormhole_count >= MAX_WORMHOLES_PER_SESSION:
        logger.info(
            f"[WORMHOLE] 跳过：本次会话已触发 {wormhole_count} 次虫洞，"
            f"达到上限 MAX_WORMHOLES_PER_SESSION={MAX_WORMHOLES_PER_SESSION}"
        )
        return None

    # 检查掌握度
    mastery = compute_scenario_mastery(user_id, current_scenario_id, db)
    if mastery < WORMHOLE_MASTERY_THRESHOLD:
        return None

    # 获取当前场景信息
    current_scenario = db.query(MicroScenario).filter(
        MicroScenario.id == current_scenario_id
    ).first()

    if not current_scenario:
        return None

    current_step = current_scenario.step_order
    current_depth = current_scenario.depth_level

    # 检查该 Step 已练习的场景数
    practiced_in_step = db.query(LearningSession).filter(
        LearningSession.user_id == user_id,
        LearningSession.topic_id == current_topic_id,
    ).count()

    if practiced_in_step < WORMHOLE_STEP_COMPLETE_MIN_SCENARIOS:
        return None

    # 查找跨话题虫洞边
    cross_topic_edge = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == current_scenario_id,
        ScenarioTransition.edge_type == "CROSS_TOPIC_MIGRATION",
    ).order_by(ScenarioTransition.overlap_ratio.desc()).first()

    if not cross_topic_edge:
        return None

    # 获取目标场景
    target_scenario = db.query(MicroScenario).filter(
        MicroScenario.id == cross_topic_edge.to_scenario_id
    ).first()

    if not target_scenario:
        return None

    target_topic = db.query(Topic).filter(Topic.id == target_scenario.topic_id).first()
    if not target_topic:
        return None

    logger.info(
        f"[WORMHOLE] 触发虫洞: 用户={user_id}, "
        f"从 {current_scenario.scenario_code}({current_topic_id}) "
        f"跳转至 {target_scenario.scenario_code}({target_topic.id}), "
        f"mastery={mastery:.1f}%"
    )

    return {
        "target_topic_id": target_topic.id,
        "target_topic_title": target_topic.title,
        "target_scenario_id": target_scenario.id,
        "target_scenario_name": target_scenario.scenario_name,
        "trigger_type": "step_complete",
        "similarity_score": cross_topic_edge.overlap_ratio,
    }


def should_depth_upgrade(
    user_id: str,
    topic_id: int,
    current_scenario_id: int,
    is_topic_exit: bool,
    db: Session,
) -> bool:
    """
    判断是否应该进入更深一层的 depth（难度晋级）。

    【修复】晋级检查只在话题最终出口触发，绝对不允许中途晋级。
    条件：
    1. 必须是话题出口场景（is_topic_exit=True）
    2. 当前话题当前 step 的平均掌握度 >= MASTERY_THRESHOLD_FOR_TIER_UP
    """
    # 【绝对禁止中途晋级】非出口场景直接返回 False
    if not is_topic_exit:
        logger.info(
            f"[DEPTH] 跳过晋级检查：非话题出口场景 (scenario_id={current_scenario_id})，"
            "禁止中途晋级"
        )
        return False

    current_scenario = db.query(MicroScenario).filter(
        MicroScenario.id == current_scenario_id
    ).first()

    if not current_scenario:
        return False

    current_step = current_scenario.step_order
    current_depth = current_scenario.depth_level

    # 获取同 step 同 depth 的所有场景
    same_level_scenarios = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.step_order == current_step,
        MicroScenario.depth_level == current_depth,
    ).all()

    if not same_level_scenarios:
        return False

    # 计算平均掌握度
    total_mastery = 0.0
    count = 0

    for sc in same_level_scenarios:
        mastery = compute_scenario_mastery(user_id, sc.id, db)
        total_mastery += mastery
        count += 1

    avg_mastery = total_mastery / count if count > 0 else 0.0

    if avg_mastery >= MASTERY_THRESHOLD_FOR_TIER_UP:
        logger.info(
            f"[DEPTH] 晋级条件满足：topic={topic_id}, step={current_step}, "
            f"avg_mastery={avg_mastery:.1f}% >= {MASTERY_THRESHOLD_FOR_TIER_UP}%"
        )
        return True

    return False


def get_next_depth_scenario(
    user_id: str,
    topic_id: int,
    current_scenario_id: int,
    db: Session,
) -> MicroScenario | None:
    """
    获取下一 depth 层的场景（用于难度晋级）。
    """
    current_scenario = db.query(MicroScenario).filter(
        MicroScenario.id == current_scenario_id
    ).first()

    if not current_scenario:
        return None

    current_step = current_scenario.step_order
    current_depth = current_scenario.depth_level
    next_depth = current_depth + 1

    if next_depth > MAX_DEPTH_TIER:
        return None

    # 查找同 step 的下一 depth 层入口场景
    next_scenario = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.step_order == current_step,
        MicroScenario.depth_level == next_depth,
        MicroScenario.is_entry_point == True,
    ).first()

    if next_scenario:
        return next_scenario

    # 没有 entry point，找任意一个
    next_scenario = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.step_order == current_step,
        MicroScenario.depth_level == next_depth,
    ).first()

    return next_scenario


def save_depth_upgrade_marker(
    user_id: str,
    topic_id: int,
    target_depth: int,
    db: Session,
) -> None:
    """
    将难度晋级标记持久化到用户设置中。

    存储位置：User.settings JSON 字段
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.warning(f"[DEPTH] Cannot save depth marker: user {user_id} not found")
        return

    settings = dict(user.settings or {})
    marker_key = f"depth_upgrade_to_{target_depth}"
    settings[marker_key] = True
    user.settings = settings
    db.commit()
    logger.info(
        f"[DEPTH] 晋级标记已保存: user={user_id}, topic={topic_id}, "
        f"depth={target_depth}"
    )


def get_depth_upgrade_marker(user_id: str, target_depth: int, db: Session) -> bool:
    """检查用户是否已标记晋级到某深度"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    settings = dict(user.settings or {})
    return settings.get(f"depth_upgrade_to_{target_depth}", False)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4 新增：Dynamic Twist 支持
# ═══════════════════════════════════════════════════════════════════════════

TWIST_POOL = [
    "The customer before you had the same order and their card was declined. Yours seems fine, but do you have a backup payment method just in case?",
    "Oh sorry, we're actually out of that item. Can you pick something else?",
    "One moment - there's a manager who wants to verify something with your order. Just stay here for a minute.",
    "I just realized we have a special promotion today. Would you like to hear about it?",
    "Sorry for the wait - the machine is being a bit slow today. Your total is ready when you are.",
    "Before I finalize this, do you have our loyalty card or app? You could earn points today!",
    "I need to double-check something about your order - is there any chance you meant decaf instead of regular?",
    "We're actually closing in 5 minutes, but I can definitely help you finish up quickly.",
]


def should_trigger_twist(
    scenario_mastery: float,
    turn_count: int,
    max_turns: int,
) -> bool:
    """
    判断是否应该触发 Dynamic Twist。

    策略：
    - 掌握度越高，越容易触发 Twist（挑战高难度）
    - 掌握度越低，越不容易触发（保护初学者）
    - 越接近最大轮次，越容易触发（收尾）
    """
    import random

    # 基础概率
    base_prob = TWIST_PROBABILITY

    # 根据掌握度调整（0.5 ~ 2.0 倍）
    mastery_factor = 0.5 + (scenario_mastery / 100.0) * 1.5

    # 根据轮次调整（后期更容易触发）
    progress_factor = 1.0 + (turn_count / max_turns) * 0.5

    final_prob = base_prob * mastery_factor * progress_factor
    final_prob = min(0.4, final_prob)  # 最多 40%

    return random.random() < final_prob


def select_twist(
    topic_id: int,
    current_scenario_code: str,
) -> str:
    """
    从 Twist Pool 中选择一个适合的 Twist。

    策略：随机选择（后续可扩展为基于 topic/scene 的智能匹配）
    """
    import random
    return random.choice(TWIST_POOL)


def build_twist_context(twist_message: str, current_event: str | None) -> str:
    """
    构建带 Twist 的事件上下文。

    用于替换 Jinja 模板中的 current_event。
    """
    if current_event:
        return f"{current_event}\n\nTWIST: {twist_message}"
    return twist_message


# ── 降级兜底 ──────────────────────────────────────────────────────────────

def _fallback_task_packet(db: Session) -> TaskPacket:
    """当 DB 为空或发生异常时，返回一个最小可用的 TaskPacket"""
    first_topic = db.query(Topic).first()
    if first_topic:
        entry = db.query(MicroScenario).filter(
            MicroScenario.topic_id == first_topic.id,
            MicroScenario.is_entry_point == True,
        ).first()

        if entry is None:
            entry = db.query(MicroScenario).filter(
                MicroScenario.topic_id == first_topic.id,
            ).first()

        if entry:
            return _build_micro_task_packet("default_user", first_topic, entry, db)

        topic_lv = first_topic.learner_level or "Intermediate"
        eff = effective_learner_label(None, topic_lv, topic_lv)
        tz = effective_topic_title_zh(first_topic)
        return TaskPacket(
            topic_id=first_topic.id,
            topic_title=first_topic.title,
            topic_title_zh=tz,
            scene_prompt=first_topic.system_prompt or first_topic.title,
            role_name=first_topic.role_name or "English Coach",
            learner_level=eff,
            voice=first_topic.voice or "Stanley",
            depth_tier=1,
            max_reply_sentences=compute_max_reply_sentences(eff, 1),
            session_goal=f"Practice basic {first_topic.title} conversation.",
            vocab_tags=first_topic.vocab_tags or [],
            sentence_patterns=first_topic.sentence_patterns or [],
        )

    # 数据库完全为空时的终极兜底
    return TaskPacket(
        topic_id=0,
        topic_title="Daily Conversation",
        topic_title_zh="日常对话",
        scene_prompt="Have a casual daily conversation to practice English.",
        role_name="English Coach",
        learner_level="Intermediate",
        max_reply_sentences=compute_max_reply_sentences(
            effective_learner_label(None, "Intermediate", "Intermediate"), 1
        ),
        session_goal="Practice speaking naturally in English.",
    )
