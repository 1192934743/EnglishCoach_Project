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
import json

# 确保 python_backend 在模块路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import openai

# 使用绝对路径加载配置文件
script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(script_dir, "config.env"))

from database import SessionLocal, Topic
from infrastructure.topic_generator import get_or_generate_topic
from application.services.session_planner import warm_up

# ── 加载话题模板配置 ──────────────────────────────────────────────────────────
def load_topic_templates() -> list[dict]:
    """从配置文件加载话题模板"""
    config_path = os.path.join(script_dir, "config", "topic_templates.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        templates = []
        for domain, items in config.get("domains", {}).items():
            for item in items:
                templates.append({
                    "domain": domain,
                    "desc": item["description"],
                    "role_hint": item.get("role_hint"),
                })
        return templates
    except FileNotFoundError:
        print(f"[WARN] Config file not found: {config_path}")
        print("[WARN] Falling back to hardcoded SEED_DESCRIPTIONS")
        return _FALLBACK_SEED_DESCRIPTIONS
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON in {config_path}: {e}")
        print("[WARN] Falling back to hardcoded SEED_DESCRIPTIONS")
        return _FALLBACK_SEED_DESCRIPTIONS

# ── 备用硬编码列表（仅在配置文件加载失败时使用）──────────────────────────────
_FALLBACK_SEED_DESCRIPTIONS = [
    # 餐饮
    {"domain": "餐饮", "desc": "Ordering drinks and food at a coffee shop or café"},
    {"domain": "餐饮", "desc": "Sitting down at a restaurant, ordering from the menu, and paying the bill"},
    # 出行
    {"domain": "出行", "desc": "Checking in luggage and going through security at an airport"},
    {"domain": "出行", "desc": "Taking a taxi or rideshare like Uber — giving directions and making small talk"},
    {"domain": "出行", "desc": "Checking in at a hotel, asking for amenities, and checking out"},
    # 购物
    {"domain": "购物", "desc": "Shopping for clothes — asking for sizes, colors, and trying items on"},
    {"domain": "购物", "desc": "Grocery shopping and asking store staff where to find items"},
    # 医疗日常
    {"domain": "医疗", "desc": "Visiting a doctor, describing symptoms, and understanding a prescription"},
    # 职业
    {"domain": "职业", "desc": "Calling customer service to report a problem or request a refund"},
    {"domain": "职业", "desc": "A business meeting — presenting ideas, giving feedback, and discussing next steps"},
    # 社交
    {"domain": "社交", "desc": "Making small talk at a party — introducing yourself and discussing hobbies"},
    {"domain": "社交", "desc": "Renting an apartment — asking the landlord about rules, utilities, and repairs"},
]

# 从配置文件加载，失败时使用备用列表
SEED_DESCRIPTIONS = load_topic_templates()

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

    for i, item in enumerate(SEED_DESCRIPTIONS, 1):
        desc = item["desc"]
        domain = item["domain"]
        role_hint = item.get("role_hint")
        print(f"[{i:2d}/{len(SEED_DESCRIPTIONS)}] '{desc}' [domain={domain}, role={role_hint}]")

        try:
            t0 = time.monotonic()
            topic = await get_or_generate_topic(desc, client, db, domain=domain, role_hint=role_hint)
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
