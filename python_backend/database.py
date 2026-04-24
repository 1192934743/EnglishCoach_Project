import uuid
import datetime
from typing import Optional

from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, Text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///english_coach.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    level = Column(Integer, default=1)
    politeness_level = Column(Integer, default=1)
    assessment_done = Column(Boolean, default=False)
    # depth_preference (1.0~5.0), new_topic_appetite (0.0~1.0), learning_mode ("freeform"/"curriculum"),
    # learner_level (str, e.g. Beginner/Intermediate) — synced from app for cognitive-load prompts
    settings = Column(JSON, default=lambda: {
        "depth_preference": 1.0,
        "new_topic_appetite": 0.2,
        "learning_mode": "freeform"
    })


class Topic(Base):
    __tablename__ = 'topics'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    # 中文界面展示用；与 title（英文 canonical）对应，可为空（旧数据 / 未回填）
    title_zh = Column(String, nullable=True)
    category = Column(String)
    difficulty_base = Column(Integer, default=1)
    system_prompt = Column(String)
    tags = Column(String)

    # ── LMS 扩展字段（新增，nullable 保证旧数据兼容）────────────────────────
    # 对话 AI 所扮演的角色名称，e.g. "Fast-food Server"
    role_name = Column(String, nullable=True)
    # 学习者目标水平，e.g. "Beginner" / "Intermediate" / "Professional"
    learner_level = Column(String, nullable=True, default="Intermediate")
    # TTS 声音
    voice = Column(String, nullable=True, default="Stanley")
    # 话题核心词汇标签，用于向量化及相似度计算，e.g. ["burger", "fries", "combo"]
    vocab_tags = Column(JSON, nullable=True)
    # 话题核心句型标签，e.g. ["I would like to order", "for here or to go"]
    sentence_patterns = Column(JSON, nullable=True)
    # 按深度分层的规则配置：{"1": {"rules": [...]}, "2": {"rules": [...]}}
    difficulty_tiers = Column(JSON, nullable=True)
    # 场景专属护栏规则（直接注入 Prompt），e.g. ["If user..., then..."]
    scene_specific_rules = Column(JSON, nullable=True)
    # 话题特征向量（由 VectorStore 离线生成后写回），JSON 存 float 列表
    embedding = Column(JSON, nullable=True)


class TargetNode(Base):
    __tablename__ = 'target_nodes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    topic_id = Column(Integer, ForeignKey('topics.id'))
    node_text = Column(String, nullable=False)
    node_type = Column(String, default="word")   # word / phrase / sentence
    depth_level = Column(Integer, default=1)     # 1=基础, 2=进阶, 3=高阶
    weight = Column(Float, default=1.0)


class UserProgress(Base):
    __tablename__ = 'user_progress'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey('users.id'))
    node_id = Column(Integer, ForeignKey('target_nodes.id'))
    mastery_score = Column(Float, default=0.0)    # 0~100
    practice_count = Column(Integer, default=0)
    last_practiced_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChatSession(Base):
    """旧版 session 记录（保留兼容，新代码请用 LearningSession）"""
    __tablename__ = 'chat_sessions'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey('users.id'))
    topic_id = Column(Integer, ForeignKey('topics.id'))
    start_time = Column(DateTime, default=datetime.datetime.utcnow)
    summary = Column(JSON, nullable=True)


class LearningSession(Base):
    """
    LMS 核心记录表：每次完整对话的快照与结果。

    nodes_attempted: 本次尝试使用的节点 id 列表
    nodes_mastered:  本次判定掌握（mastery += 15 且过阈值）的节点 id 列表
    task_packet_snapshot: 本次 TaskPacket 的 JSON 快照（方便调试与回溯）
    session_summary: 评估 AI 事后异步填写（分数、总结、建议）
    """
    __tablename__ = 'learning_sessions'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey('users.id'))
    topic_id = Column(Integer, ForeignKey('topics.id'))
    start_time = Column(DateTime, default=datetime.datetime.utcnow)
    end_time = Column(DateTime, nullable=True)
    depth_tier_used = Column(Integer, default=1)
    # JSON list of node IDs
    nodes_attempted = Column(JSON, nullable=True, default=list)
    nodes_mastered = Column(JSON, nullable=True, default=list)
    # Full TaskPacket snapshot for debugging
    task_packet_snapshot = Column(JSON, nullable=True)
    # Filled asynchronously by assessment engine after session ends
    session_summary = Column(JSON, nullable=True)


def init_db():
    """
    重建数据库并注入完整的种子数据（含新 LMS 字段）。
    调用前请删除旧的 english_coach.db 文件。
    """
    print("[INFO] Rebuilding database with LMS schema...")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # ── 话题1：麦当劳点餐（初级）────────────────────────────────────────────
    mcdonalds = Topic(
        title="McDonald's Ordering",
        title_zh="麦当劳点餐",
        category="Food & Drink",
        role_name="Fast-food Server",
        learner_level="Beginner",
        voice="Stanley",
        system_prompt=(
            "You are a friendly fast-food server at McDonald's drive-thru. "
            "Follow the specific persona instructions provided in the dynamic prompt."
        ),
        vocab_tags=["burger", "fries", "combo", "meal", "drink", "order", "receipt", "change"],
        sentence_patterns=[
            "I would like to order",
            "Can I get",
            "for here or to go",
            "Would you like to upsize",
            "That will be",
        ],
        scene_specific_rules=[
            "If the user says they are not hungry or do not want food, suggest a small side item or a drink instead of ending the conversation.",
            "Always confirm the complete order before proceeding to payment.",
            "If the user's order is unclear, politely ask them to repeat or clarify each item.",
        ],
        difficulty_tiers={
            "1": {"rules": ["Focus only on basic food ordering vocabulary. Keep sentences short."]},
            "2": {"rules": ["Introduce combo meals, upsizing, and payment options."]},
            "3": {"rules": ["Add dietary restrictions, customizations, and complaint handling."]},
        },
    )
    db.add(mcdonalds)
    db.commit()
    db.refresh(mcdonalds)

    nodes_mcdonalds = [
        # depth_level=1: 绝对基础，第一次练习必须覆盖
        TargetNode(topic_id=mcdonalds.id, node_text="burger", node_type="word", depth_level=1, weight=1.0),
        TargetNode(topic_id=mcdonalds.id, node_text="fries", node_type="word", depth_level=1, weight=1.0),
        TargetNode(topic_id=mcdonalds.id, node_text="I would like to order", node_type="sentence", depth_level=1, weight=3.0),
        TargetNode(topic_id=mcdonalds.id, node_text="Can I get", node_type="phrase", depth_level=1, weight=2.0),
        # depth_level=2: 进阶表达，掌握 tier-1 后解锁
        TargetNode(topic_id=mcdonalds.id, node_text="for here or to go", node_type="phrase", depth_level=2, weight=2.0),
        TargetNode(topic_id=mcdonalds.id, node_text="combo meal", node_type="phrase", depth_level=2, weight=2.0),
        TargetNode(topic_id=mcdonalds.id, node_text="upsize", node_type="word", depth_level=2, weight=1.5),
        # depth_level=3: 高阶，能处理意外情况
        TargetNode(topic_id=mcdonalds.id, node_text="I have a food allergy", node_type="sentence", depth_level=3, weight=2.0),
        TargetNode(topic_id=mcdonalds.id, node_text="Could you make that without", node_type="phrase", depth_level=3, weight=2.0),
    ]
    db.add_all(nodes_mcdonalds)

    # ── 话题2：技术面试（专业级）────────────────────────────────────────────
    interview = Topic(
        title="Technical Job Interview",
        title_zh="技术岗位面试",
        category="Career & Professional",
        role_name="Senior Tech Lead",
        learner_level="Professional",
        voice="Stanley",
        system_prompt=(
            "You are conducting a technical interview for a Python Algorithm Engineer position. "
            "Follow the specific persona instructions provided in the dynamic prompt."
        ),
        vocab_tags=["algorithm", "complexity", "optimize", "implement", "trade-off", "scalable", "edge case"],
        sentence_patterns=[
            "Could you walk me through your approach",
            "What is the time complexity",
            "How would you handle edge cases",
            "In my experience",
            "I would approach this by",
        ],
        scene_specific_rules=[
            "If the user gives a one-word answer, probe for more detail: 'Could you walk me through your reasoning?'",
            "Acknowledge correct technical answers with brief positive feedback before moving on.",
            "If the user seems stuck, offer a single small hint rather than giving the full answer.",
        ],
        difficulty_tiers={
            "1": {"rules": ["Ask simple behavioral questions. Focus on past experience."]},
            "2": {"rules": ["Introduce algorithm questions. Expect Big-O analysis."]},
            "3": {"rules": ["Add system design questions. Expect trade-off discussions."]},
        },
    )
    db.add(interview)
    db.commit()
    db.refresh(interview)

    nodes_interview = [
        TargetNode(topic_id=interview.id, node_text="In my experience", node_type="phrase", depth_level=1, weight=2.0),
        TargetNode(topic_id=interview.id, node_text="I would approach this by", node_type="sentence", depth_level=1, weight=3.0),
        TargetNode(topic_id=interview.id, node_text="time complexity", node_type="phrase", depth_level=2, weight=2.5),
        TargetNode(topic_id=interview.id, node_text="edge case", node_type="phrase", depth_level=2, weight=2.0),
        TargetNode(topic_id=interview.id, node_text="trade-off", node_type="word", depth_level=2, weight=2.0),
        TargetNode(topic_id=interview.id, node_text="scalable", node_type="word", depth_level=3, weight=1.5),
        TargetNode(topic_id=interview.id, node_text="bottleneck", node_type="word", depth_level=3, weight=1.5),
    ]
    db.add_all(nodes_interview)

    # ── 话题3：日常闲聊（中级）──────────────────────────────────────────────
    casual = Topic(
        title="Daily Casual Conversation",
        title_zh="日常闲聊",
        category="Daily Life",
        role_name="Language Partner",
        learner_level="Intermediate",
        voice="Stanley",
        system_prompt=(
            "You are a friendly British language partner practicing daily conversation. "
            "Follow the specific persona instructions provided in the dynamic prompt."
        ),
        vocab_tags=["weekend", "hobby", "plan", "recommend", "prefer", "actually", "honestly"],
        sentence_patterns=[
            "What do you think about",
            "To be honest",
            "Have you ever tried",
            "That reminds me of",
            "I was wondering if",
        ],
        scene_specific_rules=[
            "Occasionally echo back a rephrased version of what the user said to model natural British English.",
            "If the user makes a grammatical error, gently model the correct version in your own reply without explicitly pointing it out.",
        ],
        difficulty_tiers={
            "1": {"rules": ["Keep topics simple: weather, hobbies, food."]},
            "2": {"rules": ["Discuss opinions, preferences, and past experiences."]},
            "3": {"rules": ["Debate abstract topics, hypotheticals, and current events."]},
        },
    )
    db.add(casual)
    db.commit()
    db.refresh(casual)

    nodes_casual = [
        TargetNode(topic_id=casual.id, node_text="What do you think about", node_type="sentence", depth_level=1, weight=2.5),
        TargetNode(topic_id=casual.id, node_text="To be honest", node_type="phrase", depth_level=1, weight=2.0),
        TargetNode(topic_id=casual.id, node_text="Have you ever tried", node_type="sentence", depth_level=2, weight=2.5),
        TargetNode(topic_id=casual.id, node_text="That reminds me of", node_type="phrase", depth_level=2, weight=2.0),
        TargetNode(topic_id=casual.id, node_text="I was wondering if", node_type="sentence", depth_level=3, weight=2.0),
        TargetNode(topic_id=casual.id, node_text="hypothetically speaking", node_type="phrase", depth_level=3, weight=1.5),
    ]
    db.add_all(nodes_casual)

    db.commit()
    db.close()
    print("[INFO] Database ready: 3 topics seeded with full LMS fields.")


# 旧库升级 / API 展示：英文 canonical 标题 → 中文名（与 Flutter `_kTopicTitleZh` 对齐）
_TITLE_ZH_SEED = {
    "McDonald's Ordering": "麦当劳点餐",
    "Technical Job Interview": "技术岗位面试",
    "Daily Casual Conversation": "日常闲聊",
    "General English Conversation": "通用英语对话",
    "Daily Conversation": "日常对话",
    "Simulation Practice": "场景模拟对练",
}


def _normalize_topic_title_key(title: Optional[str]) -> str:
    """统一弯引号、首尾空格，便于与种子表匹配。"""
    if not title:
        return ""
    s = str(title).strip().replace("\u2019", "'").replace("\u2018", "'")
    return s


def topic_title_zh_fallback(english_title: Optional[str]) -> Optional[str]:
    """仅 DB 未存 title_zh 时使用：按英文标题查内置映射（大小写不敏感）。"""
    key = _normalize_topic_title_key(english_title)
    if not key:
        return None
    if key in _TITLE_ZH_SEED:
        return _TITLE_ZH_SEED[key]
    lower = key.lower()
    for k, v in _TITLE_ZH_SEED.items():
        if k.lower() == lower:
            return v
    return None


def effective_topic_title_zh(topic: Topic) -> Optional[str]:
    """展示用中文标题：优先 DB `title_zh`，否则内置英文→中文表。"""
    raw = getattr(topic, "title_zh", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return topic_title_zh_fallback(getattr(topic, "title", None))


def ensure_schema_upgrades() -> None:
    """在 ORM 访问前调用：为已有 english_coach.db 追加 topics.title_zh 并尽量回填。"""
    from sqlalchemy import text

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        # 先确认 topics 表已存在（全新 DB 时表可能尚未创建）
        table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='topics'")
        ).fetchone() is not None
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(topics)")).fetchall()
        colnames = {r[1] for r in rows}
        if "title_zh" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN title_zh VARCHAR"))

    db = SessionLocal()
    try:
        for t in db.query(Topic).all():
            if (getattr(t, "title_zh", None) or "").strip():
                continue
            zh = topic_title_zh_fallback(getattr(t, "title", None))
            if zh:
                t.title_zh = zh
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
