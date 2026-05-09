"""
migrate_add_scenario_embedding.py — 为已有 MicroScenario 补全 embedding + 升级 Transition 字段

Usage:
    # 预览（不实际写入）
    python scripts/migrate_add_scenario_embedding.py --dry-run

    # 执行迁移
    python scripts/migrate_add_scenario_embedding.py

    # 仅迁移 embedding（跳过 Transition 字段）
    python scripts/migrate_add_scenario_embedding.py --skip-transitions
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging

from database import SessionLocal, MicroScenario, ScenarioTransition
from infrastructure.embedding import SyncSemanticEmbedder, EMBEDDING_DIMENSION
from enums import EdgeType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("EnglishCoach")


def build_scenario_text(scenario: MicroScenario, constraints: list[str]) -> str:
    """
    构建用于 embedding 的综合文本。

    格式：[场景名称] | Intent: [意图] | Scene: [情境] | Keywords: [词汇]
    """
    parts = [
        scenario.scenario_name,
        f"Intent: {scenario.intent_desc}",
        f"Scene: {scenario.scene_desc}",
        f"Keywords: {', '.join(constraints)}",
    ]
    return " | ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="迁移：为已有 MicroScenario 补全 embedding")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不实际写入数据库",
    )
    parser.add_argument(
        "--skip-transitions",
        action="store_true",
        help="跳过 Transition 字段升级",
    )
    args = parser.parse_args()

    embedder = SyncSemanticEmbedder(cache_enabled=True)
    db = SessionLocal()

    try:
        # ── Step 1: 补全 embedding ───────────────────────────────────────
        scenarios = db.query(MicroScenario).filter(
            MicroScenario.embedding.is_(None)
        ).all()

        print(f"\n[Step 1] 找到 {len(scenarios)} 个缺少 embedding 的场景")

        if not scenarios:
            print("[Step 1] 所有场景已有 embedding，跳过")
        else:
            if args.dry_run:
                for sc in scenarios:
                    print(f"  - {sc.scenario_code}: {sc.scenario_name}")
            else:
                for i, sc in enumerate(scenarios, 1):
                    constraints = [
                        c.constraint_text
                        for c in sc.constraints if hasattr(sc, 'constraints')
                    ]
                    # 动态获取约束
                    from database import ScenarioConstraint
                    constraint_records = db.query(ScenarioConstraint).filter(
                        ScenarioConstraint.micro_scenario_id == sc.id
                    ).all()
                    constraint_texts = [c.constraint_text for c in constraint_records]

                    text = build_scenario_text(sc, constraint_texts)
                    vector = embedder.embed(text)

                    # 验证向量维度
                    if len(vector) != EMBEDDING_DIMENSION:
                        logger.warning(
                            f"[WARN] {sc.scenario_code} 向量维度异常: "
                            f"期望 {EMBEDDING_DIMENSION}, 实际 {len(vector)}"
                        )

                    sc.embedding = vector
                    print(f"  [{i}/{len(scenarios)}] Embedded: {sc.scenario_code}")

                db.commit()
                print("[Step 1] Embedding 迁移完成")

        # ── Step 2: 升级 Transition 字段 ─────────────────────────────────
        if args.skip_transitions:
            print("[Step 2] 已跳过（--skip-transitions）")
        else:
            transitions = db.query(ScenarioTransition).filter(
                ScenarioTransition.edge_type.is_(None)
            ).all()

            print(f"\n[Step 2] 找到 {len(transitions)} 个需要升级 edge_type 的边")

            if not transitions:
                print("[Step 2] 所有边已有 edge_type，跳过")
            else:
                if args.dry_run:
                    for t in transitions:
                        print(f"  - ID {t.id}: {t.from_scenario_id} -> {t.to_scenario_id} (created_by={t.created_by})")
                else:
                    for t in transitions:
                        t.edge_type = EdgeType.from_created_by(t.created_by).value
                        if t.transition_weight is None:
                            t.transition_weight = 1.0
                    db.commit()
                    print("[Step 2] Transition 字段升级完成")

        # ── 完成 ───────────────────────────────────────────────────────
        print("\n" + "=" * 50)
        if args.dry_run:
            print("DRY RUN 模式：未实际写入数据库")
        else:
            print("迁移完成")
        print("=" * 50)

    except Exception as e:
        logger.error(f"迁移失败: {e}", exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
