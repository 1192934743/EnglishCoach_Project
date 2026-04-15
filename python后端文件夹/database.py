import uuid
import datetime
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///english_coach.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    level = Column(Integer, default=0)
    # 🌟 新增：全局礼貌度设置 (0: Rude/Impatient, 1: Normal, 2: Very Polite)
    politeness_level = Column(Integer, default=1)
    settings = Column(JSON, default={"depth_preference": 1.0, "new_topic_ratio": 0.2})


class Topic(Base):
    __tablename__ = 'topics'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    category = Column(String)
    difficulty_base = Column(Integer, default=1)
    system_prompt = Column(String)  # 基础人设
    tags = Column(String)


class TargetNode(Base):
    __tablename__ = 'target_nodes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    topic_id = Column(Integer, ForeignKey('topics.id'))
    node_text = Column(String, nullable=False)
    node_type = Column(String, default="word")  # word, phrase, sentence
    depth_level = Column(Integer, default=1)
    weight = Column(Float, default=1.0)


class UserProgress(Base):
    __tablename__ = 'user_progress'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey('users.id'))
    node_id = Column(Integer, ForeignKey('target_nodes.id'))
    mastery_score = Column(Float, default=0.0)
    practice_count = Column(Integer, default=0)
    last_practiced_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChatSession(Base):
    __tablename__ = 'chat_sessions'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey('users.id'))
    topic_id = Column(Integer, ForeignKey('topics.id'))
    start_time = Column(DateTime, default=datetime.datetime.utcnow)
    summary = Column(JSON, nullable=True)


def init_db():
    print("⏳ 正在重建数据库...")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # 注入麦当劳话题
    mcdonalds = Topic(
        title="McDonald's Ordering",
        category="Food & Drink",
        system_prompt="You are in a McDonald's restaurant. Follow the specific persona instructions provided in the dynamic prompt."
    )
    db.add(mcdonalds)
    db.commit()
    db.refresh(mcdonalds)

    # 注入大池子节点
    nodes = [
        TargetNode(topic_id=mcdonalds.id, node_text="burger", node_type="word", depth_level=1, weight=1.0),
        TargetNode(topic_id=mcdonalds.id, node_text="fries", node_type="word", depth_level=1, weight=1.0),
        TargetNode(topic_id=mcdonalds.id, node_text="I would like to order", node_type="sentence", depth_level=1,
                   weight=3.0),
        TargetNode(topic_id=mcdonalds.id, node_text="for here or to go", node_type="phrase", depth_level=2, weight=2.0),
        TargetNode(topic_id=mcdonalds.id, node_text="combo meal", node_type="word", depth_level=2, weight=2.0),
    ]
    db.add_all(nodes)
    db.commit()
    db.close()
    print("🚀 数据库及初始数据准备就绪！")


if __name__ == "__main__":
    init_db()