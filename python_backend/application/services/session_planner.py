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
    TargetNode,
    UserProgress,
    LearningSession,
    effective_topic_title_zh,
)

from domain.entities.task_packet import (
    TaskPacket,
    DifficultyConfig,
    compute_max_reply_sentences,
    effective_learner_label,
)
from infrastructure.vector_store.numpy_store import NumpyVectorStore, embed_topics_bow
from application.services.mastery_scorer import effective_mastery

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
        all_target_ids = [n["id"] for n in task_packet.all_practice_nodes if n.get("id")]
        mastered_ids = [
            nid for nid in all_target_ids
            if _get_node_mastery(user_id, nid, db) >= MASTERY_THRESHOLD_FOR_TIER_UP
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
    Shared core: build TaskPacket for a specific topic.
    Called by both build_task_packet (after scoring) and build_task_packet_for_topic (direct).
    """
    earned_tier = _compute_depth_tier(user_id, topic.id, db)
    max_allowed = max(1, min(MAX_DEPTH_TIER, int(depth_preference)))
    depth_tier = min(earned_tier, max_allowed)

    all_nodes = db.query(TargetNode).filter(TargetNode.topic_id == topic.id).all()
    target_nodes = [n for n in all_nodes if n.depth_level == depth_tier]
    bonus_nodes  = [n for n in all_nodes if n.depth_level == depth_tier + 1]

    progresses = db.query(UserProgress).filter(UserProgress.user_id == user_id).all()
    low_mastery = {p.node_id for p in progresses if p.mastery_score < 80.0}
    review_nodes = [n for n in all_nodes if n.id in low_mastery and n.depth_level < depth_tier]

    diff_config = DifficultyConfig(
        first_turn_depth=depth_tier,
        bonus_node_unlock_after=3,
        correction_frequency=min(0.5, 0.1 + depth_tier * 0.1),
    )

    difficulty_tiers_cfg: dict = topic.difficulty_tiers or {}
    tier_rules: list = difficulty_tiers_cfg.get(str(depth_tier), {}).get("rules", [])
    scene_rules: list = (topic.scene_specific_rules or []) + tier_rules

    def _nd(n: TargetNode) -> dict:
        return {"id": n.id, "node_text": n.node_text, "node_type": n.node_type, "depth_level": n.depth_level}

    row = db.query(User).filter(User.id == user_id).first()
    settings_dict = dict(row.settings or {}) if row else None
    topic_lv = topic.learner_level or "Intermediate"
    eff_label = effective_learner_label(settings_dict, topic_lv, topic_lv)
    max_reply = compute_max_reply_sentences(eff_label, depth_tier)

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
        target_nodes=[_nd(n) for n in target_nodes],
        bonus_nodes=[_nd(n) for n in bonus_nodes],
        review_nodes=[_nd(n) for n in review_nodes],
        difficulty_config=diff_config,
        scene_specific_rules=scene_rules,
        session_goal=_build_session_goal(topic, target_nodes, review_nodes, depth_tier),
    )


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
        f"avg_mastery={avg_pick:.1f}% tier={packet.depth_tier} "
        f"target={len(packet.target_nodes)} review={len(packet.review_nodes)}"
    )
    return packet



# ── 话题评分 ──────────────────────────────────────────────────────────────

def _topic_avg_mastery_display(topic_id: int, user_id: str, db: Session) -> float:
    """
    与 GET /api/topics 中 avg_mastery 一致：已记录进度的节点分数之和 / 该话题总节点数。
    从未练过（无 UserProgress）视为 0。
    """
    nodes = db.query(TargetNode).filter(TargetNode.topic_id == topic_id).all()
    if not nodes:
        return 0.0
    node_ids = [n.id for n in nodes]
    progresses = (
        db.query(UserProgress)
        .filter(UserProgress.user_id == user_id, UserProgress.node_id.in_(node_ids))
        .all()
    )
    if not progresses:
        return 0.0
    return sum(float(p.mastery_score) for p in progresses) / float(len(nodes))


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

    Phase 2 升级：使用 effective_mastery（含时间衰减）代替原始 mastery_score，
    防止久未练习的用户被误判为已掌握而跳过复习。

    逻辑：
    - 从 tier=1 开始，计算该 tier 所有节点的有效掌握度均值
    - 若均值 >= MASTERY_THRESHOLD_FOR_TIER_UP，晋级到 tier+1
    - 直到达到 MAX_DEPTH_TIER 或未达到晋级阈值
    """
    progresses = db.query(UserProgress).filter(UserProgress.user_id == user_id).all()
    # 构建 node_id → (mastery, last_practiced_at) 映射
    progress_map: dict[int, tuple[float, object]] = {
        p.node_id: (p.mastery_score, p.last_practiced_at) for p in progresses
    }

    for tier in range(1, MAX_DEPTH_TIER + 1):
        nodes_at_tier = (
            db.query(TargetNode)
            .filter(TargetNode.topic_id == topic_id, TargetNode.depth_level == tier)
            .all()
        )
        if not nodes_at_tier:
            return tier  # 该话题没有这个 tier 的节点

        # 有效掌握度 = 经过时间衰减修正后的掌握度
        eff_scores = []
        for n in nodes_at_tier:
            if n.id in progress_map:
                raw_mastery, last_practiced = progress_map[n.id]
                eff_scores.append(effective_mastery(raw_mastery, last_practiced))
            else:
                eff_scores.append(0.0)

        avg_effective = sum(eff_scores) / len(eff_scores)
        logger.debug(f"[DepthTier] topic={topic_id} tier={tier} avg_effective={avg_effective:.1f}")

        if avg_effective < MASTERY_THRESHOLD_FOR_TIER_UP:
            return tier  # 当前 tier 未达标

    return MAX_DEPTH_TIER


def _get_node_mastery(user_id: str, node_id: int, db: Session) -> float:
    """返回节点的「有效掌握度」（含时间衰减）"""
    progress = (
        db.query(UserProgress)
        .filter(UserProgress.user_id == user_id, UserProgress.node_id == node_id)
        .first()
    )
    if not progress:
        return 0.0
    return effective_mastery(progress.mastery_score, progress.last_practiced_at)


# ── 会话目标描述 ──────────────────────────────────────────────────────────

def _build_session_goal(
    topic: Topic,
    target_nodes: list[TargetNode],
    review_nodes: list[TargetNode],
    depth_tier: int,
) -> str:
    """为 TaskPacket.session_goal 生成人类可读的目标描述（注入进 Prompt）"""
    parts = []
    if target_nodes:
        expressions = [f"'{n.node_text}'" for n in target_nodes[:3]]
        parts.append(f"Practice using: {', '.join(expressions)}")
    if review_nodes:
        review_exprs = [f"'{n.node_text}'" for n in review_nodes[:2]]
        parts.append(f"Also reinforce: {', '.join(review_exprs)}")
    if not parts:
        parts.append(f"Practice {topic.title} at depth level {depth_tier}.")
    return " | ".join(parts)


# ── 降级兜底 ──────────────────────────────────────────────────────────────

def _fallback_task_packet(db: Session) -> TaskPacket:
    """当 DB 为空或发生异常时，返回一个最小可用的 TaskPacket"""
    first_topic = db.query(Topic).first()
    if first_topic:
        nodes = db.query(TargetNode).filter(
            TargetNode.topic_id == first_topic.id,
            TargetNode.depth_level == 1
        ).all()
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
            target_nodes=[{"id": n.id, "node_text": n.node_text, "node_type": n.node_type, "depth_level": n.depth_level} for n in nodes],
            scene_specific_rules=first_topic.scene_specific_rules or [],
            session_goal=f"Practice basic {first_topic.title} conversation.",
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
