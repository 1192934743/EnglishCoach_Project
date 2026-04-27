"""
add_manual_transition.py — 手动干预微场景流转边

Usage:
    # 添加一条手动流转边
    python scripts/add_manual_transition.py --from-id 1 --to-id 3 --reason "教学逻辑：先确认杯型再选饮品"

    # 删除一条流转边
    python scripts/add_manual_transition.py --from-id 1 --to-id 3 --remove

    # 查看某话题的所有流转边
    python scripts/add_manual_transition.py --topic-id 1 --list
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging

from database import SessionLocal, MicroScenario, ScenarioTransition, Topic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("EnglishCoach")


def add_manual(db, from_id: int, to_id: int, reason: str = "") -> ScenarioTransition | None:
    existing = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == from_id,
        ScenarioTransition.to_scenario_id == to_id,
    ).first()
    if existing:
        logger.info(f"[SKIP] Transition {from_id} -> {to_id} already exists (id={existing.id})")
        return existing

    from_sc = db.query(MicroScenario).get(from_id)
    to_sc = db.query(MicroScenario).get(to_id)
    if not from_sc or not to_sc:
        logger.error(f"[ERROR] Scenario not found: from_id={from_id} or to_id={to_id}")
        return None

    if from_sc.depth_level != to_sc.depth_level:
        logger.warning(
            f"[WARN] Crossing depth levels: {from_sc.depth_level} -> {to_sc.depth_level} "
            f"(would violate depth isolation principle)"
        )

    t = ScenarioTransition(
        from_scenario_id=from_id,
        to_scenario_id=to_id,
        overlap_ratio=0.0,
        trigger_type="manual",
        required_hit_rate=0.8,
        shared_constraints=None,
        created_by="manual",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    logger.info(
        f"[OK] Added manual transition: {from_sc.scenario_code} -> {to_sc.scenario_code} "
        f"(reason: {reason or 'not specified'})"
    )
    return t


def remove_manual(db, from_id: int, to_id: int) -> bool:
    deleted = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == from_id,
        ScenarioTransition.to_scenario_id == to_id,
    ).delete()
    db.commit()
    if deleted > 0:
        logger.info(f"[OK] Removed {deleted} transition(s): {from_id} -> {to_id}")
    else:
        logger.warning(f"[SKIP] No transition found: {from_id} -> {to_id}")
    return deleted > 0


def list_transitions(db, topic_id: int) -> None:
    scenarios = db.query(MicroScenario).filter(MicroScenario.topic_id == topic_id).all()
    if not scenarios:
        logger.warning(f"No micro-scenarios found for topic_id={topic_id}")
        return

    scenario_ids = [s.id for s in scenarios]
    scenario_map = {s.id: s for s in scenarios}

    transitions = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id.in_(scenario_ids),
    ).order_by(ScenarioTransition.from_scenario_id).all()

    print(f"\n{'='*70}")
    print(f"  Transitions for topic_id={topic_id} ({len(transitions)} edges)")
    print(f"{'='*70}")

    if not transitions:
        print("  (no transitions)")
    else:
        for t in transitions:
            from_sc = scenario_map.get(t.from_scenario_id)
            to_sc = scenario_map.get(t.to_scenario_id)
            from_code = from_sc.scenario_code if from_sc else str(t.from_scenario_id)
            to_code = to_sc.scenario_code if to_sc else str(t.to_scenario_id)
            print(
                f"  {from_code} -> {to_code}  "
                f"[IoU={t.overlap_ratio:.2f}, type={t.trigger_type}, by={t.created_by}]"
            )

    # 孤岛检测
    sink_ids = [
        s.id for s in scenarios
        if not any(t.from_scenario_id == s.id for t in transitions)
    ]
    if sink_ids:
        print(f"\n  [!] SINK nodes ({len(sink_ids)}):")
        for sid in sink_ids:
            s = scenario_map[sid]
            print(f"      - [{s.scenario_code}] \"{s.scenario_name}\" (step_order={s.step_order})")

    print(f"{'='*70}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manually add/remove/list scenario transitions")
    parser.add_argument("--from-id", type=int, help="Source scenario ID")
    parser.add_argument("--to-id", type=int, help="Target scenario ID")
    parser.add_argument("--reason", type=str, default="", help="Reason for manual addition")
    parser.add_argument("--remove", action="store_true", help="Remove the specified transition")
    parser.add_argument("--list", action="store_true", help="List all transitions")
    parser.add_argument("--topic-id", type=int, help="Topic ID for --list")
    args = parser.parse_args()

    if args.list:
        if not args.topic_id:
            parser.error("--topic-id is required for --list")
        db = SessionLocal()
        try:
            list_transitions(db, args.topic_id)
        finally:
            db.close()
        return

    if args.from_id is None or args.to_id is None:
        parser.error("--from-id and --to-id are required (or use --list)")

    db = SessionLocal()
    try:
        if args.remove:
            remove_manual(db, args.from_id, args.to_id)
        else:
            add_manual(db, args.from_id, args.to_id, args.reason)
    finally:
        db.close()


if __name__ == "__main__":
    main()
