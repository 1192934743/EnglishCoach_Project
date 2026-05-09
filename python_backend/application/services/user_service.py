from api.dependencies import get_db
from database import User


def init_or_get_user(user_id: str):
    if not user_id:
        return None
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            db.add(user)
            db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


def _fetch_user_settings_dict(user_id: str) -> dict:
    if not user_id:
        return {}
    with get_db() as db:
        u = db.query(User).filter(User.id == user_id).first()
        return dict(u.settings or {}) if u else {}


def _update_lms_settings(user_id: str, depth_preference=None, new_topic_appetite=None, learner_level=None, tts_engine=None, tts_voice=None):
    if not user_id:
        return
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            settings = dict(user.settings or {})
            if depth_preference is not None:
                settings["depth_preference"] = float(depth_preference)
            if new_topic_appetite is not None:
                settings["new_topic_appetite"] = float(new_topic_appetite)
            if learner_level is not None:
                settings["learner_level"] = str(learner_level).strip()
            if tts_engine is not None:
                settings["tts_engine"] = str(tts_engine).strip()
            if tts_voice is not None:
                settings["tts_voice"] = str(tts_voice).strip()
            user.settings = settings
            db.commit()
            db.refresh(user)


def update_user_politeness(user_id: str, level: int):
    if not user_id:
        return
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.politeness_level = level
            db.commit()


def _take_mastery_snapshot(user_id: str, task_packet) -> dict:
    """
    返回空的 mastery snapshot。

    新架构下掌握度基于 LearningSession.nodes_mastered 追踪，
    不再需要从 UserProgress 获取之前的掌握度。
    """
    return {}
