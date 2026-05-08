"""
build_scenario_graph.py — 离线微场景图谱构建脚本

Usage:
    # 构建单个话题 + 难度级别的图谱
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1

    # 预览（dry-run，不写入数据库）
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1 --dry-run

    # 构建所有话题 + 所有难度级别
    python scripts/build_scenario_graph.py --all

    # 强制重建（删除旧 auto 边后重建）
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1 --rebuild

Algorithm:
    1. 遍历同 Topic + 同 Depth 下所有 MicroScenario
    2. 加载每个 MicroScenario 绑定的 ScenarioConstraint
    3. 两两计算 Constraint 集合的 Jaccard IoU（使用词干归一化提升匹配召回率）
    4. 当 0.60 <= IoU <= 0.80 时，建立 ScenarioTransition 流转边
    5. 流转方向由 step_order 约束：只允许 from.step_order <= to.step_order（禁止时光倒流）
    6. 输出孤岛微场景（out_degree == 0），提示教研人员手动干预
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging
from typing import Optional

from sqlalchemy.orm import Session

from database import (
    SessionLocal,
    MicroScenario,
    ScenarioConstraint,
    ScenarioTransition,
    Topic,
)
from application.services.mastery_scorer import normalize_text, simple_stem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("EnglishCoach")

# ── 构建参数（可覆盖）────────────────────────────────────────────────────────

MIN_IOU_THRESHOLD: float = 0.60  # 最低共享比例（低于此=场景跳跃过大）
MAX_IOU_THRESHOLD: float = 0.80  # 最高共享比例（高于此=两场景实质相同，冗余）
ENTRY_POINT_BOOST: float = 0.05  # 若 to_scenario.is_entry_point，则 IoU 门槛降低 5%


# ── 核心算法 ─────────────────────────────────────────────────────────────────

# 词形归一化映射表：常见不规则/特殊词形的词根映射
# 键值均为小写，用于 IoU 计算时的预处理。
# 覆盖场景：coffee/coffees, decaf/decaffeinated 等常见教学词汇。
_IRREGULAR_WORD_ROOTS: dict[str, str] = {
    "coffee":  "coffee",
    "coffees": "coffee",
    "decaf":   "decaf",
    "decaffeinated": "decaf",
    "medium":  "medium",
    "mediums": "medium",
    "large":   "large",
    "larges":  "large",
    "small":   "small",
    "smalls":  "small",
    "burger":   "burger",
    "burgers":  "burger",
    "fries":   "fry",
    "fry":     "fry",
    "order":   "order",
    "orders":  "order",
    "ordering": "order",
    "ordered": "order",
    "drink":   "drink",
    "drinks":  "drink",
    "combo":   "combo",
    "combos":  "combo",
    "meal":    "meal",
    "meals":   "meal",
    "upsize":  "upsize",
    "upsized": "upsize",
}


def _stem_set(text: str) -> set[str]:
    """
    将约束文本归一化为词干集合（用于 IoU 计算）。

    处理逻辑：
    1. normalize_text：小写 + 缩写展开
    2. 分词 + 过滤停用词
    3. 词干提取 + 归一化映射：先查映射表，再做词干提取
       - 映射表处理 "coffees"/"coffee" → "coffee" 等不规则情况
       - 词干提取处理 "ordering"/"ordered" → "order" 等规则变形
    """
    _STOP_WORDS = {
        "a", "an", "the", "and", "or", "in", "at", "to", "for",
        "of", "with", "on", "is", "are", "i", "you", "my", "your",
        "would", "like", "get", "have", "want", "need",
    }
    tokens = normalize_text(text).split()
    result: set[str] = set()
    for tok in tokens:
        if len(tok) < 2 or tok in _STOP_WORDS:
            continue
        # 优先查归一化映射表（处理不规则词形）
        root = _IRREGULAR_WORD_ROOTS.get(tok)
        if root:
            result.add(root)
        else:
            result.add(simple_stem(tok))
    return result


def compute_constraint_iou(constraints_a: list[str], constraints_b: list[str]) -> float:
    """
    计算两场景约束集合的 Jaccard IoU（使用词干 + 归一化映射）。

    两场景的每个 constraint_text 分别归一化为词干集合后取交集/并集，
    保证 "coffees"/"coffee"、"ordered"/"order" 等变形词都能正确匹配。

    Returns:
        float: Jaccard IoU ∈ [0.0, 1.0]
    """
    set_a: set[str] = set()
    for c in constraints_a:
        set_a.update(_stem_set(c))

    set_b: set[str] = set()
    for c in constraints_b:
        set_b.update(_stem_set(c))

    if not set_a and not set_b:
        return 0.0

    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _load_scenario_constraints(db: Session, scenario_ids: list[int]) -> dict[int, list[str]]:
    """加载指定场景的约束文本集合。返回 {scenario_id: [constraint_text, ...]}"""
    constraints = db.query(ScenarioConstraint).filter(
        ScenarioConstraint.micro_scenario_id.in_(scenario_ids)
    ).all()

    result: dict[int, list[str]] = {sid: [] for sid in scenario_ids}
    for c in constraints:
        result[c.micro_scenario_id].append(c.constraint_text)
    return result


def build_scenario_graph(
    db: Session,
    topic_id: int,
    depth_level: int,
    dry_run: bool = False,
    rebuild: bool = False,
) -> list[dict]:
    """
    构建指定话题 + 难度级别的微场景流转图。

    Args:
        db: SQLAlchemy session
        topic_id: 目标话题 ID
        depth_level: 目标难度等级（1/2/3）
        dry_run: True=只预览，不写数据库
        rebuild: True=先删除旧 auto 边，再重建

    Returns:
        拟创建的流转边列表（每条边为一个 dict）
    """
    # ── Step 1: 获取微场景 ──────────────────────────────────────────────────
    scenarios = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.depth_level == depth_level,
    ).order_by(MicroScenario.step_order).all()

    if not scenarios:
        logger.warning(f"[SKIP] No MicroScenario found for topic_id={topic_id}, depth={depth_level}")
        return []

    scenario_ids = [s.id for s in scenarios]
    logger.info(
        f"[STEP 1] Found {len(scenarios)} micro-scenarios for "
        f"topic_id={topic_id}, depth={depth_level}"
    )

    # ── Step 2: 加载约束集合 ────────────────────────────────────────────────
    scenario_constraints = _load_scenario_constraints(db, scenario_ids)
    logger.info(
        f"[STEP 2] Loaded constraints: "
        f"{ {sid: len(clist) for sid, clist in scenario_constraints.items()} }"
    )

    # ── Step 3: 建立流转边 ─────────────────────────────────────────────────
    # 【修复】优先级策略：
    # 1. 相邻 step_order 的场景强制连边（主线流程）
    # 2. 同 step_order 的场景通过 IoU 判断是否横向拓展
    # 3. 移除 IoU 对主线流程的硬性阻断
    transitions_to_create: list[dict] = []

    # 构建 step_order → scenario 映射
    step_scenarios: dict[int, list] = {}
    for sc in scenarios:
        step_scenarios.setdefault(sc.step_order, []).append(sc)

    # ── 3.1 强制连边：相邻 step_order 之间 ────────────────────────────────
    # 主线流程必须连通，确保"点单→确认→支付"等正常流程不断链
    sorted_steps = sorted(step_scenarios.keys())
    for i in range(len(sorted_steps) - 1):
        current_step = sorted_steps[i]
        next_step = sorted_steps[i + 1]

        for sc_from in step_scenarios[current_step]:
            for sc_to in step_scenarios[next_step]:
                # 加载约束用于计算 shared_constraints
                constraints_from = scenario_constraints.get(sc_from.id, [])
                constraints_to = scenario_constraints.get(sc_to.id, [])

                # 计算共享约束
                from_set = set(c.lower().strip() for c in constraints_from)
                to_set = set(c.lower().strip() for c in constraints_to)
                shared_texts = from_set & to_set
                shared_ids = []
                if shared_texts:
                    results = db.query(ScenarioConstraint.id).filter(
                        ScenarioConstraint.micro_scenario_id.in_([sc_from.id, sc_to.id]),
                        ScenarioConstraint.constraint_text.in_(shared_texts),
                    ).all()
                    shared_ids = [r[0] for r in results]

                # 计算 IoU（用于日志和记录）
                iou = compute_constraint_iou(constraints_from, constraints_to)

                transitions_to_create.append({
                    "from_scenario_id": sc_from.id,
                    "to_scenario_id": sc_to.id,
                    "overlap_ratio": round(iou, 4),
                    "trigger_type": "auto",
                    "required_hit_rate": 0.5,  # 主线流程降低门槛
                    "shared_constraints": shared_ids,
                    "created_by": "algorithm",
                })
                logger.info(
                    f"[STEP 3.1] Force-connect: {sc_from.scenario_code} -> {sc_to.scenario_code} "
                    f"(step {current_step} -> {next_step}, IoU={iou:.2f})"
                )

    # ── 3.2 横向拓展：同 step_order 之间用 IoU 判断 ─────────────────────
    # IoU 用于判断同级别的不同分支是否应该互通
    for step_order, step_list in step_scenarios.items():
        if len(step_list) < 2:
            continue

        for i, sc_a in enumerate(step_list):
            for sc_b in step_list[i + 1:]:
                constraint_list_a = scenario_constraints.get(sc_a.id, [])
                constraint_list_b = scenario_constraints.get(sc_b.id, [])

                iou = compute_constraint_iou(constraint_list_a, constraint_list_b)

                # 只有 IoU 在有效范围内才连横向边
                if MIN_IOU_THRESHOLD <= iou <= MAX_IOU_THRESHOLD:
                    # 双向边
                    for sc_from, sc_to in [(sc_a, sc_b), (sc_b, sc_a)]:
                        shared_ids = _get_shared_constraint_ids(
                            db, sc_from.id, sc_to.id,
                            constraint_list_a, constraint_list_b,
                        )
                        transitions_to_create.append({
                            "from_scenario_id": sc_from.id,
                            "to_scenario_id": sc_to.id,
                            "overlap_ratio": round(iou, 4),
                            "trigger_type": "auto",
                            "required_hit_rate": 0.8,
                            "shared_constraints": shared_ids,
                            "created_by": "algorithm",
                        })
                    logger.info(
                        f"[STEP 3.2] Horizontal-connect: {sc_a.scenario_code} <-> {sc_b.scenario_code} "
                        f"(step {step_order}, IoU={iou:.2f})"
                    )

    logger.info(f"[STEP 3] Generated {len(transitions_to_create)} transition candidates")

    # ── Step 4: 写入数据库 ─────────────────────────────────────────────────
    if dry_run:
        logger.info("[DRY RUN] Skipping database write")
        _print_transition_candidates(transitions_to_create, scenarios)
        return transitions_to_create

    if rebuild:
        deleted = db.query(ScenarioTransition).filter(
            ScenarioTransition.created_by == "algorithm",
            ScenarioTransition.from_scenario_id.in_(scenario_ids),
        ).delete(synchronize_session=False)
        logger.info(f"[REBUILD] Deleted {deleted} old auto transitions")
        db.commit()

    if transitions_to_create:
        db.bulk_insert_mappings(ScenarioTransition, transitions_to_create)
        db.commit()
        logger.info(f"[OK] Wrote {len(transitions_to_create)} transitions to DB")

    # ── Step 5: 孤岛检测 ───────────────────────────────────────────────────
    _detect_sink_nodes(db, scenarios, transitions_to_create)

    return transitions_to_create


def _get_shared_constraint_ids(
    db: Session,
    from_id: int,
    to_id: int,
    constraints_from: list[str],
    constraints_to: list[str],
) -> list[int]:
    """获取两场景共享的约束 ID 列表。"""
    from_set = set(c.lower().strip() for c in constraints_from)
    to_set = set(c.lower().strip() for c in constraints_to)
    shared_texts = from_set & to_set

    if not shared_texts:
        return []

    results = db.query(ScenarioConstraint.id).filter(
        ScenarioConstraint.micro_scenario_id.in_([from_id, to_id]),
        ScenarioConstraint.constraint_text.in_(shared_texts),
    ).all()
    return [r[0] for r in results]


def _detect_sink_nodes(
    db: Session,
    scenarios: list[MicroScenario],
    transitions_created: list[dict],
) -> None:
    """
    孤岛检测：找出 out_degree == 0 的微场景，打印警告日志。
    这些场景没有可流转的下游节点，需要教研人员手动干预。
    """
    # 合并 dry-run 候选边和已存在数据库中的边
    all_outgoing: dict[int, set[int]] = {s.id: set() for s in scenarios}

    # dry-run 候选边
    for t in transitions_created:
        all_outgoing.setdefault(t["from_scenario_id"], set()).add(t["to_scenario_id"])

    # 数据库中已存在的边（排除本次 dry-run 候选，避免重复计算）
    dry_run_to_ids = {t["to_scenario_id"] for t in transitions_created}
    existing = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id.in_(list(all_outgoing.keys())),
        ScenarioTransition.created_by == "algorithm",
    ).all()
    for e in existing:
        if e.to_scenario_id not in dry_run_to_ids:
            all_outgoing.setdefault(e.from_scenario_id, set()).add(e.to_scenario_id)

    sink_nodes = [s for s in scenarios if len(all_outgoing.get(s.id, set())) == 0]

    if sink_nodes:
        logger.warning(
            f"[!] Found {len(sink_nodes)} SINK (isolated) micro-scenarios "
            f"(no outgoing transitions):"
        )
        for sn in sink_nodes:
            logger.warning(
                f"    - [{sn.scenario_code}] \"{sn.scenario_name}\" "
                f"(step_order={sn.step_order}, depth={sn.depth_level})"
            )
        logger.warning(
            f"[!] Use scripts/add_manual_transition.py to add manual edges "
            f"for the sink nodes above."
        )
    else:
        logger.info("[OK] No sink (isolated) micro-scenarios detected")


def _print_transition_candidates(
    transitions: list[dict],
    scenarios: list[MicroScenario],
) -> None:
    """打印流转边候选预览（dry-run 模式）。"""
    scenario_map = {s.id: s for s in scenarios}
    print(f"\n{'='*60}")
    print(f"  DRY RUN — {len(transitions)} transition candidates")
    print(f"{'='*60}")
    for t in transitions:
        from_sc = scenario_map.get(t["from_scenario_id"])
        to_sc = scenario_map.get(t["to_scenario_id"])
        from_name = from_sc.scenario_code if from_sc else str(t["from_scenario_id"])
        to_name = to_sc.scenario_code if to_sc else str(t["to_scenario_id"])
        print(
            f"  {from_name} -> {to_name}  "
            f"[IoU={t['overlap_ratio']:.2f}, hit_rate={t['required_hit_rate']}]"
        )
    print(f"{'='*60}\n")


# ── 手动干预接口 ─────────────────────────────────────────────────────────────

def add_manual_transition(
    db: Session,
    from_id: int,
    to_id: int,
    reason: str = "",
) -> Optional[ScenarioTransition]:
    """
    手动添加一条流转边（教学专家干预）。

    适用场景：
    - 为孤岛微场景补充连线
    - 覆盖算法误判（业务认为应该流转但 IoU 未达标的场景）
    """
    existing = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == from_id,
        ScenarioTransition.to_scenario_id == to_id,
    ).first()
    if existing:
        logger.info(f"[SKIP] Transition {from_id}->{to_id} already exists")
        return existing

    from_sc = db.query(MicroScenario).get(from_id)
    to_sc = db.query(MicroScenario).get(to_id)
    if not from_sc or not to_sc:
        logger.error(f"[ERROR] Scenario not found: from_id={from_id}, to_id={to_id}")
        return None

    if from_sc.depth_level != to_sc.depth_level:
        logger.warning(
            f"[WARN] Crossing depth levels: {from_sc.depth_level} -> {to_sc.depth_level} "
            f"(would violate depth isolation principle)"
        )

    t = ScenarioTransition(
        from_scenario_id=from_id,
        to_scenario_id=to_id,
        overlap_ratio=0.0,  # 手动边不计算 IoU
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


def remove_transition(db: Session, from_id: int, to_id: int) -> bool:
    """删除一条流转边（移除算法误判的边）。"""
    deleted = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == from_id,
        ScenarioTransition.to_scenario_id == to_id,
    ).delete()
    db.commit()
    logger.info(f"[OK] Removed {deleted} transition(s): {from_id} -> {to_id}")
    return deleted > 0


# ── CLI 入口 ─────────────────────────────────────────────────────────────────

def main() -> None:
    global MIN_IOU_THRESHOLD, MAX_IOU_THRESHOLD

    parser = argparse.ArgumentParser(
        description="Build micro-scenario transition graph for a topic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--topic-id", type=int,
        help="Target topic ID (required unless --all is used)",
    )
    parser.add_argument(
        "--depth", type=int, default=1, choices=[1, 2, 3],
        help="Target depth level (default: 1)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Build graphs for all topics and all depths",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview transitions without writing to DB",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Delete old auto transitions before rebuilding",
    )
    parser.add_argument(
        "--min-iou", type=float, default=MIN_IOU_THRESHOLD,
        help=f"Minimum IoU threshold (default: {MIN_IOU_THRESHOLD})",
    )
    parser.add_argument(
        "--max-iou", type=float, default=MAX_IOU_THRESHOLD,
        help=f"Maximum IoU threshold (default: {MAX_IOU_THRESHOLD})",
    )
    args = parser.parse_args()

    MIN_IOU_THRESHOLD = args.min_iou
    MAX_IOU_THRESHOLD = args.max_iou

    if not args.all and not args.topic_id:
        parser.error("--topic-id is required (or use --all)")

    db = SessionLocal()
    try:
        if args.all:
            topics = db.query(Topic).all()
            total_transitions = 0
            for topic in topics:
                for depth in [1, 2, 3]:
                    n = len(build_scenario_graph(
                        db, topic.id, depth,
                        dry_run=args.dry_run,
                        rebuild=args.rebuild,
                    ))
                    total_transitions += n
            logger.info(
                f"[DONE] Built {total_transitions} transitions across "
                f"{len(topics)} topics x 3 depths"
            )
        else:
            build_scenario_graph(
                db, args.topic_id, args.depth,
                dry_run=args.dry_run,
                rebuild=args.rebuild,
            )
            logger.info("[DONE] Graph build complete")
    finally:
        db.close()


if __name__ == "__main__":
    main()
