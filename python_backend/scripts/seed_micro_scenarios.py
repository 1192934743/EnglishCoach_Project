#!/usr/bin/env python3
"""
Seed Micro-Scenarios — Starbucks Ordering Test Graph
===================================================
向数据库注入一个完整的微场景流转测试链路（星巴克点单）。

运行方式：
    python -m scripts.seed_micro_scenarios [--rebuild]

数据链路：
    Starbucks_A (入口)  →  Starbucks_B  →  Starbucks_C (尽头)

    A: 点咖啡  [coffee, Americano]
    B: 确认温度  [hot, iced]
    C: 支付  [credit card, receipt]

同一话题（Starbucks Ordering）下，Depth 1，同一深度层。

支持重复运行：
    --rebuild  先删除该话题下的所有旧 MicroScenario / ScenarioConstraint / ScenarioTransition
"""

import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, Topic, MicroScenario, ScenarioConstraint, ScenarioTransition


# ── 测试话题配置 ──────────────────────────────────────────────────────────────

TOPIC_TITLE = "Starbucks Ordering"
TOPIC_TITLE_ZH = "星巴克点单"
TOPIC_CATEGORY = "Food & Drink"
TOPIC_ROLE = "Starbucks Barista"
TOPIC_LEVEL = "Beginner"
TOPIC_VOCAB = ["coffee", "Americano", "latte", "espresso", "iced", "hot", "venti", "Grande", "Tall", "credit card", "cash", "receipt"]
TOPIC_PATTERNS = [
    "I'd like a",
    "Can I get",
    "For here or to go",
    "Hot or iced",
    "Would you like anything else",
]
TOPIC_SYSTEM_PROMPT = (
    "You are a friendly Starbucks barista. "
    "Help the customer order their drink step by step. "
    "Be warm, cheerful, and use simple English appropriate for a beginner."
)
TOPIC_RULES = [
    "Confirm drink type before asking about size.",
    "Always confirm the full order before processing payment.",
    "If the customer seems confused, offer a simple suggestion.",
]
TOPIC_DIFFICULTY = {
    "1": {"rules": ["Use only basic drink vocabulary. Keep sentences very short (2-3 words per sentence)."]},
    "2": {"rules": ["Introduce size options (Tall, Grande, Venti) and milk alternatives."]},
    "3": {"rules": ["Add customisation options: extra shot, syrup flavours, temperature."]},
}


# ── 微场景定义 ───────────────────────────────────────────────────────────────

MICRO_SCENARIOS_DEF = [
    {
        "scenario_code": "SBUX_01_ORDER_DRINK",
        "scenario_name": "Order a Coffee",
        "intent_desc": "The learner orders a coffee drink, using the target vocabulary 'coffee' and 'Americano' in context.",
        "scene_desc": (
            "You are a Starbucks barista. A customer approaches the counter. "
            "Greet them warmly and ask what they would like to order. "
            "Guide the customer to say 'coffee' and 'Americano' naturally."
        ),
        "step_order": 1,
        "is_entry_point": True,
        "max_turns": 8,
        "constraints": [
            {"constraint_text": "coffee", "constraint_type": "word", "depth_level": 1, "weight": 1.0, "hint_cn": "咖啡"},
            {"constraint_text": "Americano", "constraint_type": "word", "depth_level": 1, "weight": 1.0, "hint_cn": "美式咖啡"},
        ],
    },
    {
        "scenario_code": "SBUX_02_CONFIRM_TEMP",
        "scenario_name": "Confirm Drink Temperature",
        "intent_desc": "The learner confirms whether they want a hot or iced drink, using 'hot' and 'iced' naturally.",
        "scene_desc": (
            "You are a Starbucks barista. The customer just ordered an Americano. "
            "Ask them if they want it hot or iced. "
            "Guide them to say 'hot' or 'iced' in their reply."
        ),
        "step_order": 2,
        "is_entry_point": False,
        "max_turns": 6,
        "constraints": [
            {"constraint_text": "hot", "constraint_type": "word", "depth_level": 1, "weight": 1.0, "hint_cn": "热的"},
            {"constraint_text": "iced", "constraint_type": "word", "depth_level": 1, "weight": 1.0, "hint_cn": "冰的"},
        ],
    },
    {
        "scenario_code": "SBUX_03_PAYMENT",
        "scenario_name": "Pay for the Order",
        "intent_desc": "The learner confirms payment method, using 'credit card' and 'receipt' in context.",
        "scene_desc": (
            "You are a Starbucks barista. The customer's drink is ready. "
            "Ask them how they would like to pay. "
            "Guide them to say 'credit card' and ask for a 'receipt' naturally."
        ),
        "step_order": 3,
        "is_entry_point": False,
        "max_turns": 6,
        "constraints": [
            {"constraint_text": "credit card", "constraint_type": "phrase", "depth_level": 1, "weight": 1.5, "hint_cn": "信用卡"},
            {"constraint_text": "receipt", "constraint_type": "word", "depth_level": 1, "weight": 1.0, "hint_cn": "收据"},
        ],
    },
]

# 流转边定义：from_code → to_code
TRANSITIONS_DEF = [
    {
        "from_code": "SBUX_01_ORDER_DRINK",
        "to_code": "SBUX_02_CONFIRM_TEMP",
        "overlap_ratio": 0.65,
        "trigger_type": "auto",
        "required_hit_rate": 0.5,
        "shared_constraints": ["coffee"],
        "created_by": "seed_script",
    },
    {
        "from_code": "SBUX_02_CONFIRM_TEMP",
        "to_code": "SBUX_03_PAYMENT",
        "overlap_ratio": 0.65,
        "trigger_type": "auto",
        "required_hit_rate": 0.5,
        "shared_constraints": [],
        "created_by": "seed_script",
    },
]


# ── 核心函数 ────────────────────────────────────────────────────────────────

def _find_or_create_topic(db: SessionLocal, rebuild: bool = False) -> Topic:
    """
    查找或创建星巴克点单话题。
    若 rebuild=True，删除旧 MicroScenario / ScenarioConstraint / ScenarioTransition。
    """
    topic = db.query(Topic).filter(Topic.title == TOPIC_TITLE).first()

    if topic is None:
        topic = Topic(
            title=TOPIC_TITLE,
            title_zh=TOPIC_TITLE_ZH,
            category=TOPIC_CATEGORY,
            role_name=TOPIC_ROLE,
            learner_level=TOPIC_LEVEL,
            voice="Stanley",
            system_prompt=TOPIC_SYSTEM_PROMPT,
            vocab_tags=TOPIC_VOCAB,
            sentence_patterns=TOPIC_PATTERNS,
            scene_specific_rules=TOPIC_RULES,
            difficulty_tiers=TOPIC_DIFFICULTY,
        )
        db.add(topic)
        db.commit()
        db.refresh(topic)
        print(f"[TOPIC] Created new topic: id={topic.id} '{TOPIC_TITLE}'")
    else:
        print(f"[TOPIC] Found existing topic: id={topic.id} '{TOPIC_TITLE}'")

        if rebuild:
            # 删除旧的微场景数据
            scenario_ids = [
                s.id for s in
                db.query(MicroScenario.id).filter(MicroScenario.topic_id == topic.id).all()
            ]
            if scenario_ids:
                deleted_c = db.query(ScenarioConstraint).filter(
                    ScenarioConstraint.micro_scenario_id.in_(scenario_ids)
                ).delete(synchronize_session=False)
                deleted_t = db.query(ScenarioTransition).filter(
                    ScenarioTransition.from_scenario_id.in_(scenario_ids)
                ).delete(synchronize_session=False)
                db.query(MicroScenario).filter(
                    MicroScenario.topic_id == topic.id
                ).delete(synchronize_session=False)
                db.commit()
                print(f"[CLEANUP] Deleted {deleted_c} constraints, {deleted_t} transitions, old micro_scenarios")

    return topic


def _seed_micro_scenarios(db: SessionLocal, topic: Topic) -> dict[str, MicroScenario]:
    """创建所有微场景及其约束，返回 {code: MicroScenario} 映射。"""
    code_map: dict[str, MicroScenario] = {}

    for sc_def in MICRO_SCENARIOS_DEF:
        # 删除旧的（按 scenario_code）
        old = db.query(MicroScenario).filter(
            MicroScenario.scenario_code == sc_def["scenario_code"]
        ).first()
        if old:
            db.query(ScenarioConstraint).filter(
                ScenarioConstraint.micro_scenario_id == old.id
            ).delete(synchronize_session=False)
            db.delete(old)
            db.commit()
            print(f"  [REPLACE] Removed old scenario: {sc_def['scenario_code']}")

        scenario = MicroScenario(
            topic_id=topic.id,
            scenario_code=sc_def["scenario_code"],
            scenario_name=sc_def["scenario_name"],
            intent_desc=sc_def["intent_desc"],
            scene_desc=sc_def["scene_desc"],
            depth_level=1,
            step_order=sc_def["step_order"],
            is_entry_point=sc_def["is_entry_point"],
            max_turns=sc_def["max_turns"],
            weight=1.0,
        )
        db.add(scenario)
        db.commit()
        db.refresh(scenario)
        code_map[sc_def["scenario_code"]] = scenario
        print(f"  [SCENARIO] Created: {scenario.scenario_code} (id={scenario.id}) — {scenario.scenario_name}")

        # 创建约束
        for i, con_def in enumerate(sc_def["constraints"]):
            constraint = ScenarioConstraint(
                micro_scenario_id=scenario.id,
                constraint_text=con_def["constraint_text"],
                constraint_type=con_def["constraint_type"],
                depth_level=con_def["depth_level"],
                weight=con_def["weight"],
                hint_cn=con_def.get("hint_cn"),
                legacy_node_id=None,
            )
            db.add(constraint)
        db.commit()
        print(f"           Constraints: {[c['constraint_text'] for c in sc_def['constraints']]}")

    return code_map


def _seed_transitions(db: SessionLocal, code_map: dict[str, MicroScenario]) -> None:
    """创建流转边。"""
    for t_def in TRANSITIONS_DEF:
        from_sc = code_map.get(t_def["from_code"])
        to_sc = code_map.get(t_def["to_code"])
        if not from_sc or not to_sc:
            print(f"  [WARN] Skipping transition {t_def['from_code']} → {t_def['to_code']} (not found)")
            continue

        # 删除旧边
        db.query(ScenarioTransition).filter(
            ScenarioTransition.from_scenario_id == from_sc.id,
            ScenarioTransition.to_scenario_id == to_sc.id,
        ).delete(synchronize_session=False)

        transition = ScenarioTransition(
            from_scenario_id=from_sc.id,
            to_scenario_id=to_sc.id,
            overlap_ratio=t_def["overlap_ratio"],
            trigger_type=t_def["trigger_type"],
            required_hit_rate=t_def["required_hit_rate"],
            shared_constraints=t_def.get("shared_constraints"),
            created_by=t_def.get("created_by", "seed_script"),
        )
        db.add(transition)
        db.commit()
        print(f"  [TRANSITION] {from_sc.scenario_code} → {to_sc.scenario_code} "
              f"(IoU={t_def['overlap_ratio']}, hit_rate={t_def['required_hit_rate']})")


def _print_graph(db: SessionLocal, topic: Topic) -> None:
    """打印完整的图谱结构（用于验证）。"""
    print()
    print("=" * 60)
    print(f"  微场景图谱预览 — {TOPIC_TITLE}")
    print("=" * 60)

    scenarios = (
        db.query(MicroScenario)
        .filter(MicroScenario.topic_id == topic.id)
        .order_by(MicroScenario.step_order)
        .all()
    )

    for sc in scenarios:
        entry_tag = " [ENTRY]" if sc.is_entry_point else ""
        print(f"\n  [{sc.step_order}]{entry_tag} {sc.scenario_code}")
        print(f"       Name: {sc.scenario_name}")
        print(f"       Intent: {sc.intent_desc[:60]}...")

        constraints = (
            db.query(ScenarioConstraint)
            .filter(ScenarioConstraint.micro_scenario_id == sc.id)
            .all()
        )
        print(f"       Constraints: {[c.constraint_text for c in constraints]}")

        out_edges = (
            db.query(ScenarioTransition)
            .filter(ScenarioTransition.from_scenario_id == sc.id)
            .all()
        )
        if out_edges:
            to_names = []
            for e in out_edges:
                to_sc = db.query(MicroScenario).get(e.to_scenario_id)
                if to_sc:
                    to_names.append(f"{to_sc.scenario_code} (IoU={e.overlap_ratio:.2f})")
            print(f"       → Outgoing: {', '.join(to_names)}")
        else:
            print(f"       → Outgoing: (none — 图谱尽头)")

    print()
    print("=" * 60)
    print("  图谱预览完成")
    print("=" * 60)


# ── 主入口 ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Starbucks micro-scenario test graph")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete existing micro-scenarios under this topic before inserting (idempotent)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        topic = _find_or_create_topic(db, rebuild=args.rebuild)
        code_map = _seed_micro_scenarios(db, topic)
        _seed_transitions(db, code_map)
        _print_graph(db, topic)
        print()
        print("[OK] seed_micro_scenarios.py completed successfully.")
    except Exception as e:
        print(f"[ERROR] {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
