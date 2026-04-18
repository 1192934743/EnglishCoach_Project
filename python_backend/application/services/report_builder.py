"""
SessionReportBuilder — 学习报告构建器

两阶段推送设计：
  preliminary（初步报告）
    - 触发：WRAP_UP 阶段完成，状态机回到 ICE_BREAKING 时立刻发送
    - 数据：L1 命中记录 + SM-2 掌握度变化 + 深度层级进度
    - 延迟：< 50ms（纯 DB 读取，无 LLM 调用）

  final（增强报告）
    - 触发：L2 异步评估完成后发送（depth_tier >= 2 时才有）
    - 数据：在 preliminary 基础上叠加 LLM 质量分 + 自然度评估
    - 延迟：L2 完成后（通常 3~8 秒）

WebSocket 事件格式：
  {"event": "session_report", "stage": "preliminary"|"final", "session_id": "...", ...}

前端处理建议：
  - 收到 preliminary 时立即展示报告卡
  - 收到 final 时更新质量分（可做平滑动画）
  - 用 session_id 匹配，防止旧会话的 final 报告覆盖新会话
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from database import UserProgress
from domain.entities.task_packet import TaskPacket
from application.services.mastery_scorer import effective_mastery

logger = logging.getLogger("EnglishCoach")

# 与 session_planner.MASTERY_THRESHOLD_FOR_TIER_UP 保持一致
MASTERY_THRESHOLD = 75.0
# 节点掌握度超过此值视为"已掌握"（用于新解锁判断）
MASTERY_MILESTONE = 60.0


# ═══════════════════════════════════════════════════════════════════════════
# 公开构建函数
# ═══════════════════════════════════════════════════════════════════════════

def build_preliminary_report(
    task_packet: TaskPacket,
    session_id: str,
    session_hits: set,
    session_ctx: dict,
    mastery_snapshot: dict,      # {node_id: mastery_at_session_start}
    db: Session,
    user_id: str,
) -> dict:
    """
    构建初步报告（L1 数据版本，零 LLM 调用）。

    mastery_snapshot: 会话开始时快照的 node_id → mastery 值，
                      用于计算本次练习带来的掌握度增量。
    """
    all_nodes = task_packet.target_nodes + task_packet.review_nodes
    if not all_nodes:
        return _empty_report("preliminary", session_id, task_packet)

    node_ids = [n["id"] for n in all_nodes if n.get("id")]

    # 批量拉取当前掌握度（L1 已更新过）
    progresses = db.query(UserProgress).filter(
        UserProgress.user_id == user_id,
        UserProgress.node_id.in_(node_ids),
    ).all()
    current_map: dict[int, tuple[float, object]] = {
        p.node_id: (p.mastery_score, p.last_practiced_at) for p in progresses
    }

    nodes_data: list[dict] = []
    newly_mastered: list[str] = []

    for node in all_nodes:
        nid = node.get("id")
        if nid is None:
            continue

        was_hit = nid in session_hits
        mastery_before = mastery_snapshot.get(nid, 0.0)
        mastery_now, last_prac = current_map.get(nid, (mastery_before, None))
        eff_now = effective_mastery(mastery_now, last_prac)
        delta = mastery_now - mastery_before

        is_milestone = mastery_before < MASTERY_MILESTONE <= mastery_now
        if is_milestone:
            newly_mastered.append(node["node_text"])

        nodes_data.append({
            "id": nid,
            "text": node["node_text"],
            "type": node.get("node_type", "word"),
            "depth_level": node.get("depth_level", task_packet.depth_tier),
            "hit": was_hit,
            "mastery_before": round(mastery_before, 1),
            "mastery_now": round(mastery_now, 1),
            "mastery_delta": round(delta, 1),
            "milestone_reached": is_milestone,
        })

    # ── 深度层级进度（当前 tier 的有效掌握度均值）────────────────────────
    tier_nodes = [
        n for n in all_nodes
        if n.get("depth_level") == task_packet.depth_tier
    ]
    tier_eff_scores: list[float] = []
    for node in tier_nodes:
        nid = node.get("id")
        if nid and nid in current_map:
            mv, lp = current_map[nid]
            tier_eff_scores.append(effective_mastery(mv, lp))
        else:
            tier_eff_scores.append(0.0)

    avg_eff = sum(tier_eff_scores) / len(tier_eff_scores) if tier_eff_scores else 0.0
    tier_unlocked = avg_eff >= MASTERY_THRESHOLD

    # ── 会话得分（chat + task，上限 100）────────────────────────────────
    session_score = min(100.0, session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0))
    hit_count = sum(1 for n in nodes_data if n["hit"])

    enc_en, enc_zh = _encouragement_pair(
        avg_eff, task_packet.depth_tier, newly_mastered, tier_unlocked
    )

    return {
        "event": "session_report",
        "stage": "preliminary",
        "session_id": session_id,
        "topic_title": task_packet.topic_title,
        "topic_title_zh": getattr(task_packet, "topic_title_zh", None),
        "depth_tier": task_packet.depth_tier,
        "session_score": round(session_score, 1),
        "hit_count": hit_count,
        "total_nodes": len(nodes_data),
        "nodes": nodes_data,
        "newly_mastered": newly_mastered,
        "tier_status": {
            "tier": task_packet.depth_tier,
            "avg_effective_mastery": round(avg_eff, 1),
            "threshold": MASTERY_THRESHOLD,
            "unlocked_next_tier": tier_unlocked,
        },
        "encouragement": enc_en,
        "encouragement_zh": enc_zh,
    }


def build_final_report(
    task_packet: TaskPacket,
    session_id: str,
    l2_results: list[dict],
    db: Session,
    user_id: str,
) -> Optional[dict]:
    """
    构建增强报告（L2 校正版本）。
    在 preliminary 报告之后发送，前端可用动画平滑更新质量分。
    若 L2 结果为空则返回 None（调用方跳过发送）。
    """
    if not l2_results:
        return None

    all_nodes = task_packet.target_nodes + task_packet.review_nodes
    node_text_map = {n["id"]: n["node_text"] for n in all_nodes if n.get("id")}

    # 拉取 L2 写回后的最新掌握度
    node_ids = list(node_text_map.keys())
    progresses = db.query(UserProgress).filter(
        UserProgress.user_id == user_id,
        UserProgress.node_id.in_(node_ids),
    ).all()
    mastery_map = {p.node_id: p.mastery_score for p in progresses}

    quality_breakdown: list[dict] = []
    attempted_qualities: list[float] = []

    for r in l2_results:
        nid = r.get("node_id")
        if nid not in node_text_map:
            continue

        q = float(r.get("quality", 0.0))
        attempted = bool(r.get("attempted", False))
        correct = bool(r.get("correct", False))

        if attempted:
            attempted_qualities.append(q)

        quality_breakdown.append({
            "text": node_text_map[nid],
            "attempted": attempted,
            "correct": correct,
            "quality": round(q, 2),
            "mastery_after_l2": round(mastery_map.get(nid, 0.0), 1),
            "quality_label": _quality_label(q, attempted),
        })

    avg_quality = (
        sum(attempted_qualities) / len(attempted_qualities)
        if attempted_qualities else 0.0
    )

    return {
        "event": "session_report",
        "stage": "final",
        "session_id": session_id,
        "topic_title": task_packet.topic_title,
        "topic_title_zh": getattr(task_packet, "topic_title_zh", None),
        "depth_tier": task_packet.depth_tier,
        "avg_quality": round(avg_quality, 2),
        "quality_label": _quality_label(avg_quality, bool(attempted_qualities)),
        "quality_breakdown": quality_breakdown,
        "l2_assessed": True,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════

def _encouragement_pair(
    avg_eff: float,
    tier: int,
    newly_mastered: list[str],
    tier_unlocked: bool,
) -> tuple[str, str]:
    """英 / 中鼓励语（纯模板，零 LLM）。"""
    if tier_unlocked:
        en = (
            f"Outstanding! You've mastered Tier {tier}. "
            f"Your next session will unlock deeper Tier {tier + 1} expressions!"
        )
        zh = (
            f"太棒了！你已完成第 {tier} 层。"
            f"下次对练将解锁更深一层的第 {tier + 1} 层表达！"
        )
        return en, zh
    if newly_mastered:
        words = ", ".join(f'"{w}"' for w in newly_mastered[:2])
        suffix = " and more" if len(newly_mastered) > 2 else ""
        en = f"Great job! You've now mastered {words}{suffix}. Keep it up!"
        words_zh = "、".join(f'「{w}」' for w in newly_mastered[:2])
        suf = "等" if len(newly_mastered) > 2 else ""
        zh = f"做得好！你已掌握 {words_zh}{suf}，继续保持！"
        return en, zh

    gap = MASTERY_THRESHOLD - avg_eff
    if avg_eff >= 60:
        en = (
            f"Almost there! Just {gap:.0f} more mastery points "
            f"to unlock Tier {tier + 1} content."
        )
        zh = (
            f"就差一点！再积累约 {gap:.0f} 点掌握度，"
            f"即可解锁第 {tier + 1} 层内容。"
        )
        return en, zh
    if avg_eff >= 30:
        en = (
            "Good progress! Repeat these expressions a few more times "
            "to build lasting confidence."
        )
        zh = "进展不错！把这些表达多练几遍，会更有底气。"
        return en, zh
    en = (
        "Every practice session counts! "
        "You're building a solid foundation — keep going!"
    )
    zh = "每一练都有用！你在打牢基础，加油！"
    return en, zh


def _quality_label(quality: float, attempted: bool) -> str:
    """将 0~1 质量分转为人类可读标签"""
    if not attempted:
        return "not_used"
    if quality >= 0.85:
        return "excellent"
    if quality >= 0.65:
        return "good"
    if quality >= 0.40:
        return "fair"
    return "needs_work"


def _empty_report(stage: str, session_id: str, task_packet: TaskPacket) -> dict:
    return {
        "event": "session_report",
        "stage": stage,
        "session_id": session_id,
        "topic_title": task_packet.topic_title,
        "topic_title_zh": getattr(task_packet, "topic_title_zh", None),
        "depth_tier": task_packet.depth_tier,
        "session_score": 0.0,
        "hit_count": 0,
        "total_nodes": 0,
        "nodes": [],
        "newly_mastered": [],
        "tier_status": {
            "tier": task_packet.depth_tier,
            "avg_effective_mastery": 0.0,
            "threshold": MASTERY_THRESHOLD,
            "unlocked_next_tier": False,
        },
        "encouragement": "Keep practicing! Every session brings you closer to fluency.",
        "encouragement_zh": "继续练习！每一局都让你离流利更近一步。",
    }
