import os
import uuid
import datetime
from typing import Optional

from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, Text, CheckConstraint
from sqlalchemy.orm import declarative_base, sessionmaker

# 使用绝对路径，确保无论从哪个目录运行都在 python_backend/ 下创建数据库
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "english_coach.db")
DATABASE_URL = f"sqlite:///{_DB_PATH}"
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
    # 话题领域分类，支撑 Migration Lock（防横向沉迷）
    domain = Column(String, nullable=True)  # e.g. "餐饮", "出行", "购物"

    # ── 质量达标字段（v1.3 新增）────────────────────────────────────────────
    # 质量等级：A/B/C/D，用于自动化质量控制
    quality_grade = Column(String, nullable=True)  # "A"/"B"/"C"/"D"
    # 质量分数：0-100
    quality_score = Column(Integer, nullable=True)
    # 质量问题详情（JSON 列表）
    quality_issues = Column(JSON, nullable=True)
    # 生成尝试次数
    generation_attempts = Column(Integer, default=1)
    # 是否已发布（未达标话题标记为未发布）
    is_published = Column(Boolean, default=True)


class MicroScenario(Base):
    """
    微场景 — 场景图的节点。

    一个 Topic 可包含多个 MicroScenario，形成子图。
    MicroScenario 之间通过 ScenarioTransition 表建立流转边。

    设计原则：
    - intent_desc：给 Director LLM 看的教学意图（Intent 校验标尺）
    - scene_desc：给 Actor LLM 看的自然情境描述
    - depth_level：CEFR 难度等级，流转时严格同层隔离
    - step_order：逻辑顺序（用于约束流转方向，禁止时光倒流）
    """
    __tablename__ = 'micro_scenarios'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属大话题
    topic_id = Column(Integer, ForeignKey('topics.id'), nullable=False)

    # 场景标识
    scenario_code = Column(String, unique=True, nullable=False)  # e.g., "MCD_01_CONFIRM_SIZE"
    scenario_name = Column(String, nullable=False)               # e.g., "Confirm Cup Size"

    # 描述层
    intent_desc = Column(Text, nullable=False)   # 教学意图："用户需要确认饮料杯尺寸（小/中/大）"
    scene_desc = Column(Text, nullable=False)    # 自然情境："咖啡师正在询问您的饮料杯型"

    # 难度等级（CEFR 对齐：1=Beginner, 2=Intermediate, 3=Advanced）
    # 流转时不得跨级跳转，严格隔离
    depth_level = Column(Integer, default=1)

    # 逻辑顺序（整数，越大越靠后，用于约束流转方向）
    # 流转方向：只允许 from.step_order <= to.step_order，防止时光倒流
    step_order = Column(Integer, default=0)

    # 元数据
    is_entry_point = Column(Boolean, default=False)  # 是否为话题入口微场景
    max_turns = Column(Integer, default=8)           # 建议最大轮数（超时强制流转）
    weight = Column(Float, default=1.0)               # 推荐权重（影响图谱构建）

    # 语义向量（图谱构建时使用 Cosine Similarity）
    # JSON 数组，存储 Gemini embedding-001 的 768 维向量
    embedding = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ScenarioConstraint(Base):
    """
    微场景的教学约束 — 双轨校验的「Constraints」标尺。

    一个 MicroScenario 可绑定多个 ScenarioConstraint。
    Director LLM 的双轨校验之一：检查这些 constraint_text 是否被用户使用。

    设计原则：
    - constraint_text：目标表达原文（双轨校验的核心检测目标）
    - constraint_type：检测粒度 (word/phrase/sentence)
    - depth_level：必须与父 MicroScenario.depth_level 一致或更低
    """
    __tablename__ = 'scenario_constraints'

    id = Column(Integer, primary_key=True, autoincrement=True)
    micro_scenario_id = Column(Integer, ForeignKey('micro_scenarios.id'), nullable=False)

    # 约束文本（目标表达）
    constraint_text = Column(String, nullable=False)  # e.g., "medium"
    constraint_type = Column(String, default="word")  # word / phrase / sentence

    # 难度等级（必须与父 MicroScenario.depth_level 一致或更低）
    depth_level = Column(Integer, default=1)

    # 权重（影响约束被命中的重要性）
    weight = Column(Float, default=1.0)

    # 提示（当约束未被命中时，Director 可参考此提示）
    hint_cn = Column(String, nullable=True)  # e.g., "咖啡中杯用 medium"

    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ScenarioTransition(Base):
    """
    微场景图谱的流转边 — 场景切换的桥梁。

    设计原则：
    - overlap_ratio：两场景间 Constraint 集合的 Jaccard IoU
    - 阈值 0.6 <= ratio <= 0.8 时建立流转边（支撑 70/30 平滑原则）
    - 流转方向由 step_order 约束：只允许 from.step_order <= to.step_order
    - 双向边独立记录，支持非对称流转（业务需要时）
    """
    __tablename__ = 'scenario_transitions'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 流转边的两个端点
    from_scenario_id = Column(Integer, ForeignKey('micro_scenarios.id'), nullable=False)
    to_scenario_id = Column(Integer, ForeignKey('micro_scenarios.id'), nullable=False)

    # 流转条件
    # overlap_ratio: [0.0, 1.0]，两场景间约束词汇的 Jaccard IoU
    overlap_ratio = Column(Float, nullable=False)

    # 流转触发方式
    trigger_type = Column(String, default="auto")  # auto / manual / intent_driven

    # 触发该流转所需的约束命中率阈值（如 0.8 = 80% 约束已命中）
    required_hit_rate = Column(Float, default=0.8)

    # 元数据
    shared_constraints = Column(JSON, nullable=True)  # 两场景共享的 constraint_id 列表
    created_by = Column(String, default="algorithm")  # "algorithm" / "manual"

    # 边类型（v2.0 新增，用于区分垂直主线和横向迁移）
    edge_type = Column(String, nullable=True)  # VERTICAL_CORE / HORIZONTAL_MIGRATION / MANUAL

    # 流转权重（v2.0 新增，用于处理双向迁移不对称性）
    transition_weight = Column(Float, nullable=True)  # 0.0-1.0，默认为 None

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # 约束：禁止自环
    __table_args__ = (
        CheckConstraint('from_scenario_id != to_scenario_id', name='no_self_loop'),
    )


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


def init_db(safe: bool = True):
    """
    初始化数据库表结构。

    话题数据由 scripts/seed_topics.py 生成，不要在这里硬编码。
    """
    db = SessionLocal()
    try:
        # 确保表已创建
        Base.metadata.create_all(bind=engine)
        
        if safe:
            return

        print("[INFO] Rebuilding database with LMS schema...")
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
    finally:
        db.close()


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
    """
    在 ORM 访问前调用：为已有 english_coach.db 追加缺失字段。

    v2.0 新增字段：
    - micro_scenarios.embedding: JSON，存储 768 维语义向量
    - scenario_transitions.edge_type: VARCHAR，区分边类型
    - scenario_transitions.transition_weight: FLOAT，处理双向迁移不对称性

    v1.3 新增字段：
    - topics.quality_grade: VARCHAR，质量等级 A/B/C/D
    - topics.quality_score: INTEGER，质量分数 0-100
    - topics.quality_issues: JSON，质量问题详情
    - topics.generation_attempts: INTEGER，生成尝试次数
    - topics.is_published: BOOLEAN，是否已发布
    """
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

        # 检查 topics 表
        rows = conn.execute(text("PRAGMA table_info(topics)")).fetchall()
        colnames = {r[1] for r in rows}

        if "domain" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN domain VARCHAR(64)"))
            print("[Schema] Added topics.domain column")

        # v1.3 新增字段：质量相关
        if "quality_grade" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN quality_grade VARCHAR(8)"))
            print("[Schema] Added topics.quality_grade column")

        if "quality_score" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN quality_score INTEGER"))
            print("[Schema] Added topics.quality_score column")

        if "quality_issues" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN quality_issues JSON"))
            print("[Schema] Added topics.quality_issues column")

        if "generation_attempts" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN generation_attempts INTEGER DEFAULT 1"))
            print("[Schema] Added topics.generation_attempts column")

        if "is_published" not in colnames:
            conn.execute(text("ALTER TABLE topics ADD COLUMN is_published BOOLEAN DEFAULT 1"))
            print("[Schema] Added topics.is_published column")

        # 检查 micro_scenarios 表
        rows = conn.execute(text("PRAGMA table_info(micro_scenarios)")).fetchall()
        colnames = {r[1] for r in rows}

        if "embedding" not in colnames:
            conn.execute(text("ALTER TABLE micro_scenarios ADD COLUMN embedding JSON"))
            print("[Schema] Added micro_scenarios.embedding column")

        # 检查 scenario_transitions 表
        rows = conn.execute(text("PRAGMA table_info(scenario_transitions)")).fetchall()
        colnames = {r[1] for r in rows}

        if "edge_type" not in colnames:
            conn.execute(text("ALTER TABLE scenario_transitions ADD COLUMN edge_type VARCHAR(32)"))
            print("[Schema] Added scenario_transitions.edge_type column")

        if "transition_weight" not in colnames:
            conn.execute(text("ALTER TABLE scenario_transitions ADD COLUMN transition_weight FLOAT"))
            print("[Schema] Added scenario_transitions.transition_weight column")

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
