from typing import Optional
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool

from api.dependencies import get_db
from database import (
    Topic,
    LearningSession,
    MicroScenario,
    ScenarioConstraint,
    effective_topic_title_zh,
)

router = APIRouter()


def _get_topic_stats(db, topic_id: int, user_id: str | None):
    """获取话题的统计信息（基于微场景）"""
    constraints = db.query(ScenarioConstraint).join(
        MicroScenario,
        ScenarioConstraint.micro_scenario_id == MicroScenario.id,
    ).filter(MicroScenario.topic_id == topic_id).all()

    total_constraints = len(constraints)
    constraint_ids = {c.id for c in constraints}
    depth_levels = sorted(set(c.depth_level for c in constraints))

    avg_mastery = 0.0
    last_practiced = None
    if user_id and constraints:
        mastered_ids = set()
        sessions = db.query(LearningSession).filter(
            LearningSession.user_id == user_id,
            LearningSession.topic_id == topic_id,
        ).all()
        for s in sessions:
            if s.nodes_mastered:
                mastered_ids.update(s.nodes_mastered)

        mastered_count = len(constraint_ids & mastered_ids)
        avg_mastery = (mastered_count / total_constraints * 100) if total_constraints > 0 else 0.0

        dates = [s.start_time for s in sessions if s.start_time]
        last_practiced = max(dates).isoformat() if dates else None

    return {
        "total_constraints": total_constraints,
        "depth_levels": depth_levels,
        "avg_mastery": round(avg_mastery, 1),
        "last_practiced": last_practiced,
    }


@router.get("/api/topics")
async def get_topics(user_id: Optional[str] = Query(default=None)):
    def _query():
        with get_db() as db:
            topics = db.query(Topic).all()
            result = []
            for t in topics:
                stats = _get_topic_stats(db, t.id, user_id)
                result.append({
                    "id": t.id,
                    "title": t.title,
                    "title_zh": effective_topic_title_zh(t),
                    "category": t.category or "General",
                    "learner_level": t.learner_level or "Intermediate",
                    "role_name": t.role_name or "Coach",
                    "total_nodes": stats["total_constraints"],
                    "depth_levels": stats["depth_levels"],
                    "avg_mastery": stats["avg_mastery"],
                    "last_practiced": stats["last_practiced"],
                })
            return result

    data = await run_in_threadpool(_query)
    return JSONResponse(content={"topics": data})


@router.get("/api/stats")
async def get_stats(user_id: str = Query(...)):
    def _query():
        import datetime
        with get_db() as db:
            sessions = db.query(LearningSession).filter(
                LearningSession.user_id == user_id
            ).order_by(LearningSession.start_time.desc()).all()

            mastered_count = 0
            total_practiced = 0
            for s in sessions:
                if s.nodes_mastered:
                    mastered_count += len(s.nodes_mastered)
                    total_practiced += len(s.nodes_attempted or [])

            streak = 0
            if sessions:
                today = datetime.datetime.utcnow().date()
                day = today
                session_dates = set(s.start_time.date() for s in sessions if s.start_time)
                while day in session_dates:
                    streak += 1
                    day -= datetime.timedelta(days=1)

            topic_ids_practiced = list(set(s.topic_id for s in sessions if s.topic_id))

            recent = []
            for s in sessions[:7]:
                topic = db.query(Topic).filter(Topic.id == s.topic_id).first()
                recent.append({
                    "topic_title": topic.title if topic else "Unknown",
                    "topic_title_zh": (effective_topic_title_zh(topic) if topic else None),
                    "depth_tier": s.depth_tier_used or 1,
                    "nodes_mastered": len(s.nodes_mastered or []),
                    "date": s.start_time.isoformat() if s.start_time else None,
                    "quality": s.session_summary.get("avg_quality") if s.session_summary else None,
                })

            topics_summary = []
            all_topics = db.query(Topic).all()
            for t in all_topics:
                stats = _get_topic_stats(db, t.id, user_id)
                if stats["total_constraints"] == 0:
                    continue
                topics_summary.append({
                    "topic_title": t.title,
                    "topic_title_zh": effective_topic_title_zh(t),
                    "category": t.category or "General",
                    "avg_mastery": stats["avg_mastery"],
                    "nodes_practiced": stats["total_constraints"],
                    "total_nodes": stats["total_constraints"],
                })
            topics_summary.sort(key=lambda x: x["avg_mastery"], reverse=True)

            return {
                "total_sessions": len(sessions),
                "total_expressions_practiced": total_practiced,
                "total_expressions_mastered": mastered_count,
                "topics_touched": len(topic_ids_practiced),
                "current_streak_days": streak,
                "recent_sessions": recent,
                "topics_summary": topics_summary[:10],
            }

    data = await run_in_threadpool(_query)
    return JSONResponse(content=data)