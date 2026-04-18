"""
一键为 topics.title_zh 为空的记录补中文展示名。

用法（在 python_backend 目录下）：
    python scripts/backfill_topic_title_zh.py
    python scripts/backfill_topic_title_zh.py --dry-run
    python scripts/backfill_topic_title_zh.py --sleep 0.8

逻辑：
  1. ensure_schema_upgrades()（保证有 title_zh 列）
  2. 对每条 title_zh 为空的 Topic：
     - 先尝试 database.topic_title_zh_fallback（内置英文→中文，无 API 费用）
     - 仍为空则调用 DeepSeek 短请求（与线上新生成话题同一套 llm_fill_title_zh_only）

依赖：config.env 中的 DEEPSEEK_KEY。仅 --dry-run 时：只展示内置映射结果，
      对仍需 LLM 的行只打印「将调用 LLM」、不写库、不调 API。

与「后续新 topic / 用户搜索」的关系：
  - 新话题：get_or_generate_topic → 主 JSON 含 title_zh，漏了则 _fill_title_zh_if_missing
    （内部即 llm_fill_title_zh_only）
  - 用户搜索命中已有话题（Tier-1）：若 DB 无 title_zh，复用路径会用 seed 尝试写回 DB
  - 本脚本：一次性把历史空值补进 DB；之后与新生成写入的 title_zh 一致走同一展示链路
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import openai

load_dotenv("config.env")

from database import SessionLocal, Topic, ensure_schema_upgrades, topic_title_zh_fallback
from infrastructure.topic_generator import llm_fill_title_zh_only


async def _run(*, dry_run: bool, sleep_s: float) -> int:
    ensure_schema_upgrades()

    key = os.getenv("DEEPSEEK_KEY")
    base = os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com")
    client: openai.AsyncOpenAI | None = None
    if not dry_run:
        if not key:
            print("错误: 未设置 DEEPSEEK_KEY。可加 --dry-run 仅预览内置映射。")
            return 1
        client = openai.AsyncOpenAI(api_key=key, base_url=base)

    db = SessionLocal()
    try:
        rows = db.query(Topic).order_by(Topic.id).all()
        missing = [t for t in rows if not (getattr(t, "title_zh", None) or "").strip()]
        print(f"共 {len(rows)} 条话题，其中 title_zh 为空: {len(missing)} 条\n")

        filled_seed = 0
        filled_llm = 0
        skipped = 0

        for t in missing:
            title = (t.title or "").strip()
            zh = topic_title_zh_fallback(title)
            if zh:
                print(f"  [seed] id={t.id}  {title!r}  ->  {zh!r}")
                filled_seed += 1
                if not dry_run:
                    t.title_zh = zh
                continue

            if dry_run:
                print(f"  [llm?] id={t.id}  {title!r}  (--dry-run：将调用 LLM 补全，未执行)")
                filled_llm += 1
                continue

            assert client is not None
            zh = await llm_fill_title_zh_only(client, title) or ""
            if zh:
                print(f"  [llm ] id={t.id}  {title!r}  ->  {zh!r}")
                t.title_zh = zh
                filled_llm += 1
            else:
                print(f"  [skip] id={t.id}  {title!r}  (LLM 未返回有效 title_zh)")
                skipped += 1
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

        if dry_run:
            print(
                f"\n--dry-run：未写入数据库。"
                f" 内置映射可解决 {filled_seed} 条；另有 {filled_llm} 条需正式运行时调 LLM。"
            )
            return 0

        db.commit()
        print(f"\n已提交：内置映射 {filled_seed} 条，LLM {filled_llm} 条；未补成 {skipped} 条。")
        return 0
    finally:
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill topics.title_zh for Chinese UI.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="不写库、不调 LLM；仅打印 seed 结果并标出哪些将需 LLM",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        metavar="SEC",
        help="两次 LLM 调用之间的间隔秒数，默认 0.5",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(dry_run=args.dry_run, sleep_s=args.sleep)))


if __name__ == "__main__":
    main()
