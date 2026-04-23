from typing import Optional
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool

from api.dependencies import get_db
from database import (
    Topic,
    TargetNode,
    UserProgress,
    LearningSession,
    effective_topic_title_zh,
)

router = APIRouter()

@router.get("/api/topics")
async def get_topics(user_id: Optional[str] = Query(default=None)):
    def _query():
        with get_db() as db:
            topics = db.query(Topic).all()
            result = []
            for t in topics:
                nodes = db.query(TargetNode).filter(TargetNode.topic_id == t.id).all()
                total_nodes = len(nodes)
                avg_mastery = 0.0
                last_practiced = None
                if user_id and nodes:
                    node_ids = [n.id for n in nodes]
                    progresses = db.query(UserProgress).filter(
                        UserProgress.user_id == user_id,
                        UserProgress.node_id.in_(node_ids),
                    ).all()
                    if progresses:
                        avg_mastery = sum(p.mastery_score for p in progresses) / len(nodes)
                        dates = [p.last_practiced_at for p in progresses if p.last_practiced_at]
                        last_practiced = max(dates).isoformat() if dates else None
                depth_levels = sorted(set(n.depth_level for n in nodes))
                result.append({
                    "id": t.id,
                    "title": t.title,
                    "title_zh": effective_topic_title_zh(t),
                    "category": t.category or "General",
                    "learner_level": t.learner_level or "Intermediate",
                    "role_name": t.role_name or "Coach",
                    "total_nodes": total_nodes,
                    "depth_levels": depth_levels,
                    "avg_mastery": round(avg_mastery, 1),
                    "last_practiced": last_practiced,
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

            all_progress = db.query(UserProgress).filter(
                UserProgress.user_id == user_id
            ).all()

            mastered_nodes = [p for p in all_progress if p.mastery_score >= 60.0]
            total_practiced = len(all_progress)

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
                nodes = db.query(TargetNode).filter(TargetNode.topic_id == t.id).all()
                if not nodes:
                    continue
                node_ids = [n.id for n in nodes]
                progs = db.query(UserProgress).filter(
                    UserProgress.user_id == user_id,
                    UserProgress.node_id.in_(node_ids),
                ).all()
                if not progs:
                    continue
                avg = sum(p.mastery_score for p in progs) / len(nodes)
                topics_summary.append({
                    "topic_title": t.title,
                    "topic_title_zh": effective_topic_title_zh(t),
                    "category": t.category or "General",
                    "avg_mastery": round(avg, 1),
                    "nodes_practiced": len(progs),
                    "total_nodes": len(nodes),
                })
            topics_summary.sort(key=lambda x: x["avg_mastery"], reverse=True)

            return {
                "total_sessions": len(sessions),
                "total_expressions_practiced": total_practiced,
                "total_expressions_mastered": len(mastered_nodes),
                "topics_touched": len(topic_ids_practiced),
                "current_streak_days": streak,
                "recent_sessions": recent,
                "topics_summary": topics_summary[:10],
            }

    data = await run_in_threadpool(_query)
    return JSONResponse(content=data)