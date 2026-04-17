"""
seed_topics.py — 批量种子话题生成脚本

用法：
    cd python_backend
    python scripts/seed_topics.py

功能：
  - 读取下方 SEED_DESCRIPTIONS 列表
  - 对每个描述调用 TopicGenerator
  - 已存在同名话题则跳过（幂等）
  - 完成后重建 VectorStore embedding

注意：需要 config.env 中的 DEEPSEEK_KEY 才能运行
"""

import asyncio
import os
import sys
import time

# 确保 python_backend 在模块路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import openai

load_dotenv("config.env")

from database import SessionLocal, Topic
from infrastructure.topic_generator import get_or_generate_topic
from application.services.session_planner import warm_up

# ── 种子话题描述列表 ──────────────────────────────────────────────────────────
# 每行一个自然语言描述，TopicGenerator 负责理解并生成完整配置
SEED_DESCRIPTIONS = [
    # 餐饮
    "Ordering drinks and food at a coffee shop or café",
    "Sitting down at a restaurant, ordering from the menu, and paying the bill",

    # 出行
    "Checking in luggage and going through security at an airport",
    "Taking a taxi or rideshare like Uber — giving directions and making small talk",
    "Checking in at a hotel, asking for amenities, and checking out",

    # 购物
    "Shopping for clothes — asking for sizes, colors, and trying items on",
    "Grocery shopping and asking store staff where to find items",

    # 医疗日常
    "Visiting a doctor, describing symptoms, and understanding a prescription",

    # 职业
    "Calling customer service to report a problem or request a refund",
    "A business meeting — presenting ideas, giving feedback, and discussing next steps",

    # 社交
    "Making small talk at a party — introducing yourself and discussing hobbies",
    "Renting an apartment — asking the landlord about rules, utilities, and repairs",
]

# ── 主流程 ────────────────────────────────────────────────────────────────────

async def seed():
    client = openai.AsyncOpenAI(
        api_key=os.getenv("DEEPSEEK_KEY"),
        base_url=os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"),
    )
    db = SessionLocal()

    existing_titles = {t.title.lower() for t in db.query(Topic).all()}
    print(f"\n[Seed] Found {len(existing_titles)} existing topics in DB.")
    print(f"[Seed] Will generate {len(SEED_DESCRIPTIONS)} new topics.\n")

    success = 0
    skipped = 0
    failed = 0

    for i, desc in enumerate(SEED_DESCRIPTIONS, 1):
        print(f"[{i:2d}/{len(SEED_DESCRIPTIONS)}] '{desc}'")

        try:
            t0 = time.monotonic()
            topic = await get_or_generate_topic(desc, client, db)
            elapsed = time.monotonic() - t0

            if topic.title.lower() in existing_titles and topic.id is not None:
                pre_existing = db.query(Topic).filter(Topic.title == topic.title).first()
                if pre_existing and pre_existing.id == topic.id:
                    print(f"         -> Reused existing: '{topic.title}' ({elapsed:.1f}s)")
                    skipped += 1
                else:
                    print(f"         -> Generated: '{topic.title}' (id={topic.id}) ({elapsed:.1f}s)")
                    success += 1
            else:
                print(f"         -> Generated: '{topic.title}' (id={topic.id}) ({elapsed:.1f}s)")
                success += 1

        except Exception as e:
            print(f"         -> FAILED: {e}")
            failed += 1

        # Brief pause between calls to avoid rate limiting
        if i < len(SEED_DESCRIPTIONS):
            await asyncio.sleep(0.5)

    db.close()

    print(f"\n[Seed] Done. generated={success}, reused={skipped}, failed={failed}")

    # Rebuild VectorStore embeddings for all topics
    print("[Seed] Rebuilding VectorStore embeddings...")
    warm_up()
    print("[Seed] VectorStore ready.\n")


if __name__ == "__main__":
    asyncio.run(seed())
