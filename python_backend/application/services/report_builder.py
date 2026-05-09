"""
SessionReportBuilder — 学习报告构建器

基于 LearningSession.nodes_mastered 构建报告。
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from database import LearningSession
from domain.entities.task_packet import TaskPacket

logger = logging.getLogger("EnglishCoach")

MASTERY_THRESHOLD = 75.0


def build_preliminary_report(
    task_packet: TaskPacket,
    session_id: str,
    session_hits: set,
    session_ctx: dict,
    mastery_snapshot: dict,
    db: Session,
    user_id: str,
) -> dict:
    """
    构建初步报告。
    进度基于 LearningSession.nodes_mastered。
    """
    all_constraints = task_packet.constraints + task_packet.review_constraints
    if not all_constraints:
        return _empty_report("preliminary", session_id, task_packet)

    constraint_ids = [c.constraint_id for c in all_constraints if c.constraint_id]

    nodes_data = []
    hit_count = 0
    for c in all_constraints:
        nid = c.constraint_id
        if nid is None:
            continue
        was_hit = nid in session_hits
        if was_hit:
            hit_count += 1
        nodes_data.append({
            "id": nid,
            "text": c.constraint_text,
            "type": c.constraint_type,
            "depth_level": c.depth_level,
            "hit": was_hit,
        })

    session_score = min(100.0, session_ctx.get("chat_score", 0.0) + session_ctx.get("task_score", 0.0))

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
        "newly_mastered": [],
        "tier_status": {
            "tier": task_packet.depth_tier,
            "avg_effective_mastery": 0.0,
            "threshold": MASTERY_THRESHOLD,
            "unlocked_next_tier": False,
        },
        "encouragement": "Great job! Keep practicing!",
        "encouragement_zh": "做得好！继续加油！",
    }


def build_final_report(
    task_packet: TaskPacket,
    session_id: str,
    l2_results: list[dict],
    db: Session,
    user_id: str,
) -> Optional[dict]:
    """构建增强报告（L2 校正版本）。"""
    if not l2_results:
        return None

    all_constraints = task_packet.constraints + task_packet.review_constraints
    constraint_text_map = {c.constraint_id: c.constraint_text for c in all_constraints if c.constraint_id}

    quality_breakdown = []
    for r in l2_results:
        nid = r.get("node_id")
        if nid not in constraint_text_map:
            continue
        q = float(r.get("quality", 0.0))
        attempted = bool(r.get("attempted", False))

        quality_breakdown.append({
            "text": constraint_text_map[nid],
            "attempted": attempted,
            "correct": bool(r.get("correct", False)),
            "quality": round(q, 2),
            "quality_label": _quality_label(q, attempted),
        })

    avg_quality = sum(r.get("quality", 0) for r in l2_results if r.get("attempted")) / max(1, sum(1 for r in l2_results if r.get("attempted")))

    return {
        "event": "session_report",
        "stage": "final",
        "session_id": session_id,
        "topic_title": task_packet.topic_title,
        "topic_title_zh": getattr(task_packet, "topic_title_zh", None),
        "depth_tier": task_packet.depth_tier,
        "avg_quality": round(avg_quality, 2),
        "quality_label": _quality_label(avg_quality, bool(l2_results)),
        "quality_breakdown": quality_breakdown,
        "l2_assessed": True,
    }


def _quality_label(quality: float, attempted: bool) -> str:
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
