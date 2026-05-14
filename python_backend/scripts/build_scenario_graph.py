"""
build_scenario_graph.py — 离线微场景图谱构建脚本 (v2.0 语义版)

⚠️  架构约束：
    - 使用 Gemini embedding-001 (768维) 生成向量
    - 向量空间是模型强绑定的，不可热切换模型
    - Cosine Similarity 阈值需根据实际数据标定（初始值 0.65/0.85）

Usage:
    # 构建单个话题 + 难度级别的图谱
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1

    # 预览（dry-run，不写入数据库）
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1 --dry-run

    # 构建所有话题 + 所有难度级别
    python scripts/build_scenario_graph.py --all

    # 强制重建（删除旧 auto 边后重建）
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1 --rebuild

    # 打印详细相似度分布（用于阈值标定）
    python scripts/build_scenario_graph.py --topic-id 1 --depth 1 --dry-run --verbose

Algorithm:
    1. 遍历同 Topic + 同 Depth 下所有 MicroScenario
    2. 加载每个 MicroScenario 绑定的 ScenarioConstraint
    3. 从 DB 加载场景向量，构建 ScenarioVectorIndex
    4. 垂直连线（Step N -> Step N+1）：
       - 计算前驱与后继场景的余弦相似度
       - 仅当相似度 >= VERTICAL_SIM_THRESHOLD 时建立连接
       - 每个节点最多保留 Top-K (默认 2) 条垂直边
    5. 横向迁移（同 Step 内）：
       - 当 0.85 <= similarity <= 0.92 时建立双向迁移边
    6. 输出图谱健康度报告（节点数、边数、连通分量、异常节点）
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging
import itertools
from collections import Counter
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from database import (
    SessionLocal,
    MicroScenario,
    ScenarioConstraint,
    ScenarioTransition,
    Topic,
)
from infrastructure.embedding import ScenarioVectorIndex, SyncSemanticEmbedder, cosine_similarity
from infrastructure.graph import graph_config
from enums import EdgeType

# 从配置模块导入阈值
VERTICAL_SIM_THRESHOLD = graph_config.VERTICAL_SIM_THRESHOLD
HORIZONTAL_SIM_MIN = graph_config.HORIZONTAL_SIM_MIN
HORIZONTAL_SIM_MAX = graph_config.HORIZONTAL_SIM_MAX
MAX_VERTICAL_EDGES_PER_NODE = graph_config.MAX_VERTICAL_EDGES_PER_NODE
CROSS_TOPIC_SIM_THRESHOLD = graph_config.CROSS_TOPIC_SIM_THRESHOLD
MAX_CROSS_TOPIC_EDGES_PER_NODE = graph_config.MAX_CROSS_TOPIC_EDGES_PER_NODE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("EnglishCoach")

# ── 构建参数（已迁移到 infrastructure.graph.graph_config）──────────────────────


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _load_scenario_constraints(db: Session, scenario_ids: list[int]) -> dict[int, list[str]]:
    """加载指定场景的约束文本集合。返回 {scenario_id: [constraint_text, ...]}"""
    constraints = db.query(ScenarioConstraint).filter(
        ScenarioConstraint.micro_scenario_id.in_(scenario_ids)
    ).all()

    result: dict[int, list[str]] = {sid: [] for sid in scenario_ids}
    for c in constraints:
        result[c.micro_scenario_id].append(c.constraint_text)
    return result


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


def _build_vector_index(
    db: Session,
    scenarios: list[MicroScenario],
    embedder: SyncSemanticEmbedder,
) -> ScenarioVectorIndex:
    """
    构建场景向量索引。

    策略：
    1. 优先使用 DB 中已有的 embedding
    2. 若无 embedding，调用 Gemini API 生成
    """
    scenario_ids = [s.id for s in scenarios]
    missing_ids = []
    mappings: dict[int, list[float]] = {}

    for sc in scenarios:
        if sc.embedding:
            mappings[sc.id] = sc.embedding
        else:
            missing_ids.append(sc.id)

    if missing_ids:
        logger.info(f"[VECTOR] 补全 {len(missing_ids)} 个场景的 embedding...")
        missing_scenarios = db.query(MicroScenario).filter(
            MicroScenario.id.in_(missing_ids)
        ).all()
        constraints_map = _load_scenario_constraints(db, missing_ids)

        for sc in missing_scenarios:
            text = build_scenario_text(sc, constraints_map.get(sc.id, []))
            vector = embedder.embed(text)
            sc.embedding = vector
            mappings[sc.id] = vector
            logger.info(f"  [VECTOR] Embedded: {sc.scenario_code}")

        db.commit()

    return ScenarioVectorIndex(mappings)


# ── 图谱健康度 ───────────────────────────────────────────────────────────────

def compute_graph_metrics(
    scenarios: list[MicroScenario],
    transitions: list[dict],
) -> dict:
    """
    计算图谱全局健康度指标。
    """
    all_from_ids = [t["from_scenario_id"] for t in transitions]
    all_to_ids = [t["to_scenario_id"] for t in transitions]

    out_degree = Counter(all_from_ids)
    in_degree = Counter(all_to_ids)

    n_nodes = len(scenarios)
    n_edges = len(transitions)

    avg_out = sum(out_degree.values()) / n_nodes if n_nodes else 0
    avg_in = sum(in_degree.values()) / n_nodes if n_nodes else 0

    isolated = sum(
        1 for sc in scenarios
        if out_degree.get(sc.id, 0) == 0 and in_degree.get(sc.id, 0) == 0
    )

    return {
        "total_nodes": n_nodes,
        "total_edges": n_edges,
        "avg_out_degree": round(avg_out, 2),
        "avg_in_degree": round(avg_in, 2),
        "isolated_nodes": isolated,
    }


def _detect_graph_anomalies(
    db: Session,
    scenarios: list[MicroScenario],
    transitions: list[dict],
) -> tuple[list[MicroScenario], list[MicroScenario]]:
    """
    完整的图谱健康度检测。

    Returns:
        (sink_nodes, source_nodes)
    """
    all_from_ids = [t["from_scenario_id"] for t in transitions]
    all_to_ids = [t["to_scenario_id"] for t in transitions]

    out_degree = Counter(all_from_ids)
    in_degree = Counter(all_to_ids)

    max_step = max(sc.step_order for sc in scenarios) if scenarios else 0

    sink_nodes = []
    source_nodes = []

    for sc in scenarios:
        if out_degree.get(sc.id, 0) == 0 and sc.step_order < max_step:
            sink_nodes.append(sc)
        if in_degree.get(sc.id, 0) == 0 and not sc.is_entry_point:
            source_nodes.append(sc)

    return sink_nodes, source_nodes


def print_graph_health_report(
    metrics: dict,
    sink_nodes: list[MicroScenario],
    source_nodes: list[MicroScenario],
    dry_run: bool = False,
) -> None:
    """
    打印图谱健康度报告。
    """
    mode = "DRY RUN" if dry_run else "COMMIT"
    print("\n" + "=" * 60)
    print(f"              图谱健康度报告 ({mode})")
    print("=" * 60)
    print(f"  节点数: {metrics['total_nodes']}          边数: {metrics['total_edges']}")
    print(f"  平均出度: {metrics['avg_out_degree']}       平均入度: {metrics['avg_in_degree']}")
    print(f"  孤立节点: {metrics['isolated_nodes']}")
    print("-" * 60)

    if sink_nodes or source_nodes:
        print("  [WARN] 异常节点:")
        for sn in sink_nodes:
            print(f"     - [孤立终点] {sn.scenario_code}: {sn.scenario_name}")
        for sn in source_nodes:
            print(f"     - [无头节点] {sn.scenario_code}: {sn.scenario_name}")
    else:
        print("  [OK] 无异常节点")

    print("=" * 60 + "\n")


def print_similarity_distribution(
    all_similarities: list[float],
    vertical_candidates: int,
    horizontal_candidates: int,
) -> None:
    """
    打印相似度分布统计（用于阈值标定）。
    """
    if not all_similarities:
        print("\n[SIM-DIST] 无相似度数据")
        return

    arr = np.array(all_similarities)
    print("\n" + "-" * 50)
    print("  相似度分布统计（用于阈值标定）")
    print("-" * 50)
    print(f"  样本数: {len(arr)}")
    print(f"  最小值: {arr.min():.4f}")
    print(f"  最大值: {arr.max():.4f}")
    print(f"  平均值: {arr.mean():.4f}")
    print(f"  中位数: {np.median(arr):.4f}")
    print(f"  P25:    {np.percentile(arr, 25):.4f}")
    print(f"  P75:    {np.percentile(arr, 75):.4f}")
    print("-" * 50)
    print(f"  垂直边候选数 (sim >= {VERTICAL_SIM_THRESHOLD}): {vertical_candidates}")
    print(f"  横向边候选数 (0.85 <= sim <= 0.92): {horizontal_candidates}")
    print("-" * 50 + "\n")


# ── 核心算法 ─────────────────────────────────────────────────────────────────

def _build_vertical_edges(
    step_scenarios: dict[int, list[MicroScenario]],
    vector_index: ScenarioVectorIndex,
    all_similarities: list[float],
    verbose: bool = False,
) -> list[dict]:
    """
    构建垂直主线边（Step N -> Step N+1）。

    策略：
    1. 对于 Step N 的每个节点，找到 Step N+1 中相似度 >= VERTICAL_SIM_THRESHOLD 的节点
    2. 仅保留相似度 Top-MAX_VERTICAL_EDGES_PER_NODE 的节点进行连接
    3. 如果没有节点超过阈值，fallback 到最高相似度的 1 个节点
    """
    transitions = []
    sorted_steps = sorted(step_scenarios.keys())

    for i in range(len(sorted_steps) - 1):
        current_step = sorted_steps[i]
        next_step = sorted_steps[i + 1]

        current_scenarios = step_scenarios[current_step]
        next_scenarios = step_scenarios[next_step]

        from_ids = [sc.id for sc in current_scenarios]
        to_ids = [sc.id for sc in next_scenarios]

        sim_matrix = vector_index.compute_similarities(from_ids, to_ids)

        for idx_from, sc_from in enumerate(current_scenarios):
            similarities = sim_matrix[idx_from]
            all_similarities.extend(similarities.tolist())

            candidates = [
                (idx_to, float(sim))
                for idx_to, sim in enumerate(similarities)
                if sim >= VERTICAL_SIM_THRESHOLD
            ]

            if not candidates:
                best_idx = int(np.argmax(similarities))
                best_sim = float(similarities[best_idx])
                logger.info(
                    f"[V-FALLBACK] {sc_from.scenario_code} -> "
                    f"{next_scenarios[best_idx].scenario_code} "
                    f"(best={best_sim:.4f} < {VERTICAL_SIM_THRESHOLD})"
                )
                candidates = [(best_idx, best_sim)]

            candidates.sort(key=lambda x: x[1], reverse=True)
            top_candidates = candidates[:MAX_VERTICAL_EDGES_PER_NODE]

            for idx_to, similarity in top_candidates:
                sc_to = next_scenarios[idx_to]
                transitions.append({
                    "from_scenario_id": sc_from.id,
                    "to_scenario_id": sc_to.id,
                    "overlap_ratio": round(similarity, 4),
                    "trigger_type": "auto",
                    "required_hit_rate": 0.5,
                    "edge_type": EdgeType.VERTICAL_CORE.value,
                    "created_by": EdgeType.VERTICAL_CORE.to_created_by(),
                })
                if verbose:
                    logger.info(
                        f"[VERTICAL] {sc_from.scenario_code} -> {sc_to.scenario_code} "
                        f"(sim={similarity:.4f})"
                    )

    return transitions


def _build_horizontal_edges(
    step_scenarios: dict[int, list[MicroScenario]],
    vector_index: ScenarioVectorIndex,
    all_similarities: list[float],
    verbose: bool = False,
) -> list[dict]:
    """
    构建横向迁移边（同 step_order 之间）。

    规则：
    - 仅当 HORIZONTAL_SIM_MIN <= similarity <= HORIZONTAL_SIM_MAX 时建立双向迁移边
    """
    transitions = []

    for step_order, step_list in step_scenarios.items():
        if len(step_list) < 2:
            continue

        from_ids = [sc.id for sc in step_list]
        to_ids = [sc.id for sc in step_list]
        sim_matrix = vector_index.compute_similarities(from_ids, to_ids)

        n = len(step_list)
        for i in range(n):
            for j in range(i + 1, n):
                similarity = float(sim_matrix[i, j])
                all_similarities.append(similarity)

                if HORIZONTAL_SIM_MIN <= similarity <= HORIZONTAL_SIM_MAX:
                    for from_id, to_id in [(step_list[i].id, step_list[j].id), (step_list[j].id, step_list[i].id)]:
                        transitions.append({
                            "from_scenario_id": from_id,
                            "to_scenario_id": to_id,
                            "overlap_ratio": round(similarity, 4),
                            "trigger_type": "auto",
                            "required_hit_rate": 0.7,
                            "edge_type": EdgeType.HORIZONTAL_MIGRATION.value,
                            "created_by": EdgeType.HORIZONTAL_MIGRATION.to_created_by(),
                        })
                    if verbose:
                        logger.info(
                            f"[HORIZONTAL] {step_list[i].scenario_code} <-> {step_list[j].scenario_code} "
                            f"(sim={similarity:.4f})"
                        )

    return transitions


# ── 跨话题虫洞构建 ──────────────────────────────────────────────────────────


def _build_cross_topic_edges(
    db: Session,
    all_similarities: list[float],
    verbose: bool = False,
) -> list[dict]:
    """
    构建跨话题虫洞边（仅连接话题入口节点：step_order=1, depth_level=1）。

    性能要求：
    1. 使用矩阵运算，禁止双重循环
    2. 使用 Mask 过滤同话题节点对
    3. 禁止运行时调用 embed()

    算法：
    1. 加载所有话题的入口节点（step_order=1, depth_level=1, is_entry_point=True）
    2. 构建 (N x N) 完整相似度矩阵
    3. 用 Mask 过滤同话题节点对
    4. 按相似度排序，保留 Top-K
    5. 建立双向边
    """
    transitions = []

    # Step 1: 获取所有入口节点
    entry_nodes = db.query(MicroScenario).filter(
        MicroScenario.step_order == 1,
        MicroScenario.depth_level == 1,
        MicroScenario.is_entry_point == True,
    ).all()

    if len(entry_nodes) < 2:
        logger.info("[CROSS-TOPIC] Entry nodes < 2, skip")
        return []

    entry_ids = [n.id for n in entry_nodes]
    entry_topic_ids = {n.id: n.topic_id for n in entry_nodes}
    entry_code_map = {n.id: n.scenario_code for n in entry_nodes}

    logger.info(f"[CROSS-TOPIC] Found {len(entry_nodes)} entry nodes")

    # Step 2: 从 DB 加载已有向量，构建向量索引
    entry_with_vectors = []
    for node in entry_nodes:
        if node.embedding:
            entry_with_vectors.append(node)

    if len(entry_with_vectors) < 2:
        logger.warning("[CROSS-TOPIC] Not enough entry nodes with embeddings, skip")
        return []

    # 构建向量索引（仅使用已有向量）
    mappings = {n.id: n.embedding for n in entry_with_vectors}
    vector_index = ScenarioVectorIndex(mappings)

    # 获取有效 ID（只有有向量的节点）
    valid_ids = list(mappings.keys())
    valid_topic_ids = [entry_topic_ids[nid] for nid in valid_ids]

    logger.info(f"[CROSS-TOPIC] Building similarity matrix for {len(valid_ids)} nodes with vectors")

    # Step 3: 构建完整相似度矩阵（矩阵运算，无循环）
    sim_matrix = vector_index.compute_similarities(valid_ids, valid_ids)
    all_similarities.extend(sim_matrix[np.triu_indices(len(valid_ids), k=1)].tolist())

    # Step 4: 构建 Mask（同话题过滤）
    topic_array = np.array(valid_topic_ids)
    same_topic_mask = topic_array[:, np.newaxis] == topic_array[np.newaxis, :]

    # 保留上三角（避免重复计算）且不同话题
    upper_tri_indices = np.triu_indices(len(valid_ids), k=1)
    candidate_mask = ~same_topic_mask[upper_tri_indices]

    # Step 5: 提取候选边
    candidate_sims = sim_matrix[upper_tri_indices]
    candidate_indices = np.where(candidate_mask)[0]

    if len(candidate_indices) == 0:
        logger.info("[CROSS-TOPIC] No cross-topic candidates found")
        return []

    from_indices = upper_tri_indices[0][candidate_indices]
    to_indices = upper_tri_indices[1][candidate_indices]
    similarities = candidate_sims[candidate_indices]

    # Step 6: 过滤阈值 + 排序
    above_threshold = similarities >= CROSS_TOPIC_SIM_THRESHOLD
    valid_from = from_indices[above_threshold]
    valid_to = to_indices[above_threshold]
    valid_sims = similarities[above_threshold]

    if len(valid_sims) == 0:
        logger.info(f"[CROSS-TOPIC] No edges above threshold {CROSS_TOPIC_SIM_THRESHOLD}")
        return []

    # 按相似度降序排序
    sort_order = np.argsort(valid_sims)[::-1]
    valid_from = valid_from[sort_order]
    valid_to = valid_to[sort_order]
    valid_sims = valid_sims[sort_order]

    # Step 7: 每个节点最多保留 Top-K（双向边）
    edges_per_node: dict[int, int] = {}
    new_transitions: list[dict] = []

    for i in range(len(valid_from)):
        from_idx = int(valid_from[i])
        to_idx = int(valid_to[i])
        sim = float(valid_sims[i])

        from_id = valid_ids[from_idx]
        to_id = valid_ids[to_idx]

        # 检查是否超限
        from_count = edges_per_node.get(from_id, 0)
        to_count = edges_per_node.get(to_id, 0)

        if from_count >= MAX_CROSS_TOPIC_EDGES_PER_NODE or \
           to_count >= MAX_CROSS_TOPIC_EDGES_PER_NODE:
            continue

        # 双向边
        new_transitions.append({
            "from_scenario_id": from_id,
            "to_scenario_id": to_id,
            "overlap_ratio": round(sim, 4),
            "trigger_type": "auto",
            "required_hit_rate": 0.8,
            "edge_type": EdgeType.CROSS_TOPIC_MIGRATION.value,
            "created_by": EdgeType.CROSS_TOPIC_MIGRATION.to_created_by(),
        })

        new_transitions.append({
            "from_scenario_id": to_id,
            "to_scenario_id": from_id,
            "overlap_ratio": round(sim, 4),
            "trigger_type": "auto",
            "required_hit_rate": 0.8,
            "edge_type": EdgeType.CROSS_TOPIC_MIGRATION.value,
            "created_by": EdgeType.CROSS_TOPIC_MIGRATION.to_created_by(),
        })

        # 更新计数
        edges_per_node[from_id] = from_count + 1
        edges_per_node[to_id] = to_count + 1

        if verbose:
            from_code = entry_code_map.get(from_id, str(from_id))
            to_code = entry_code_map.get(to_id, str(to_id))
            logger.info(f"[CROSS-TOPIC] {from_code} <-> {to_code} (sim={sim:.4f})")

    logger.info(f"[CROSS-TOPIC] Built {len(new_transitions)} edges")
    return new_transitions


def build_cross_topic_graph(
    db: Session,
    dry_run: bool = False,
    rebuild: bool = False,
    verbose: bool = False,
) -> list[dict]:
    """
    构建跨话题虫洞图（全量构建，跨所有话题）。

    Args:
        db: SQLAlchemy session
        dry_run: True=只预览，不写数据库
        rebuild: True=先删除旧跨话题边，再重建
        verbose: True=打印详细相似度信息

    Returns:
        拟创建的跨话题边列表
    """
    logger.info("[CROSS-TOPIC] Starting cross-topic graph build...")

    all_similarities: list[float] = []
    cross_topic_edges = _build_cross_topic_edges(db, all_similarities, verbose)

    if verbose and all_similarities:
        arr = np.array(all_similarities)
        print("\n" + "-" * 50)
        print("  跨话题相似度分布")
        print("-" * 50)
        print(f"  样本数: {len(arr)}")
        print(f"  最小值: {arr.min():.4f}")
        print(f"  最大值: {arr.max():.4f}")
        print(f"  平均值: {arr.mean():.4f}")
        print(f"  P75: {np.percentile(arr, 75):.4f}")
        print(f"  阈值 {CROSS_TOPIC_SIM_THRESHOLD} 以上: {sum(arr >= CROSS_TOPIC_SIM_THRESHOLD)}")
        print("-" * 50 + "\n")

    if dry_run:
        logger.info("[DRY RUN] Skipping database write")
        _print_cross_topic_candidates(cross_topic_edges)
        return cross_topic_edges

    if rebuild:
        deleted = db.query(ScenarioTransition).filter(
            ScenarioTransition.edge_type == EdgeType.CROSS_TOPIC_MIGRATION.value,
        ).delete(synchronize_session=False)
        logger.info(f"[REBUILD] Deleted {deleted} old cross-topic transitions")
        db.commit()

    if cross_topic_edges:
        db.bulk_insert_mappings(ScenarioTransition, cross_topic_edges)
        db.commit()
        logger.info(f"[OK] Wrote {len(cross_topic_edges)} cross-topic transitions to DB")

    return cross_topic_edges


def _print_cross_topic_candidates(transitions: list[dict]) -> None:
    """打印跨话题边候选预览（dry-run 模式）。"""
    cross_topic = [t for t in transitions if t.get("edge_type") == EdgeType.CROSS_TOPIC_MIGRATION.value]

    print(f"\n{'='*60}")
    print(f"  DRY RUN — {len(cross_topic)} cross-topic transition candidates")
    print(f"{'='*60}")

    if cross_topic:
        print(f"\n  跨话题虫洞边 ({len(cross_topic)} 条):")
        for t in cross_topic:
            print(f"    {t['from_scenario_id']} -> {t['to_scenario_id']}  [sim={t['overlap_ratio']:.4f}]")

    print(f"{'='*60}\n")


def build_scenario_graph(
    db: Session,
    topic_id: int,
    depth_level: int,
    dry_run: bool = False,
    rebuild: bool = False,
    verbose: bool = False,
) -> list[dict]:
    """
    构建指定话题 + 难度级别的微场景流转图（v2.0 语义版）。

    Args:
        db: SQLAlchemy session
        topic_id: 目标话题 ID
        depth_level: 目标难度等级（1/2/3）
        dry_run: True=只预览，不写数据库
        rebuild: True=先删除旧 auto 边，再重建
        verbose: True=打印详细相似度信息

    Returns:
        拟创建的流转边列表
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

    # ── Step 2: 构建向量索引 ──────────────────────────────────────────────
    embedder = SyncSemanticEmbedder(cache_enabled=True)
    vector_index = _build_vector_index(db, scenarios, embedder)
    logger.info(f"[STEP 2] Vector index built: {len(vector_index.scenario_ids)} vectors")

    # ── Step 3: 按 step_order 分组 ───────────────────────────────────────
    step_scenarios: dict[int, list] = {}
    for sc in scenarios:
        step_scenarios.setdefault(sc.step_order, []).append(sc)
    logger.info(
        f"[STEP 3] Step distribution: "
        f"{ {k: len(v) for k, v in sorted(step_scenarios.items())} }"
    )

    # ── Step 4: 构建边 ───────────────────────────────────────────────────
    all_similarities: list[float] = []
    transitions_to_create: list[dict] = []

    # 4.1 垂直主线边
    vertical_edges = _build_vertical_edges(step_scenarios, vector_index, all_similarities, verbose)
    transitions_to_create.extend(vertical_edges)
    logger.info(f"[STEP 4.1] Vertical edges: {len(vertical_edges)}")

    # 4.2 横向迁移边
    horizontal_edges = _build_horizontal_edges(step_scenarios, vector_index, all_similarities, verbose)
    transitions_to_create.extend(horizontal_edges)
    logger.info(f"[STEP 4.2] Horizontal edges: {len(horizontal_edges)}")

    logger.info(f"[STEP 4] Total transitions: {len(transitions_to_create)}")

    # ── Step 5: 打印健康度报告 ───────────────────────────────────────────
    metrics = compute_graph_metrics(scenarios, transitions_to_create)
    sink_nodes, source_nodes = _detect_graph_anomalies(db, scenarios, transitions_to_create)
    print_graph_health_report(metrics, sink_nodes, source_nodes, dry_run)

    if verbose:
        vertical_candidates = sum(1 for s in all_similarities if s >= VERTICAL_SIM_THRESHOLD)
        horizontal_candidates = sum(
            1 for s in all_similarities
            if HORIZONTAL_SIM_MIN <= s <= HORIZONTAL_SIM_MAX
        )
        print_similarity_distribution(all_similarities, vertical_candidates, horizontal_candidates)

    # ── Step 6: 写入数据库 ───────────────────────────────────────────────
    if dry_run:
        logger.info("[DRY RUN] Skipping database write")
        _print_transition_candidates(transitions_to_create, scenarios)
        return transitions_to_create

    if rebuild:
        deleted = db.query(ScenarioTransition).filter(
            ScenarioTransition.created_by.in_(["semantic_algorithm", "semantic_horizontal", "algorithm", "migration_algorithm"]),
            ScenarioTransition.from_scenario_id.in_(scenario_ids),
        ).delete(synchronize_session=False)
        logger.info(f"[REBUILD] Deleted {deleted} old auto transitions")
        db.commit()

    if transitions_to_create:
        db.bulk_insert_mappings(ScenarioTransition, transitions_to_create)
        db.commit()
        logger.info(f"[OK] Wrote {len(transitions_to_create)} transitions to DB")

    return transitions_to_create


def _print_transition_candidates(
    transitions: list[dict],
    scenarios: list[MicroScenario],
) -> None:
    """打印流转边候选预览（dry-run 模式）。"""
    scenario_map = {s.id: s for s in scenarios}

    vertical = [t for t in transitions if t.get("edge_type") == EdgeType.VERTICAL_CORE.value]
    horizontal = [t for t in transitions if t.get("edge_type") == EdgeType.HORIZONTAL_MIGRATION.value]

    print(f"\n{'='*60}")
    print(f"  DRY RUN — {len(transitions)} transition candidates")
    print(f"{'='*60}")

    if vertical:
        print(f"\n  垂直主线边 ({len(vertical)} 条):")
        for t in vertical:
            from_sc = scenario_map.get(t["from_scenario_id"])
            to_sc = scenario_map.get(t["to_scenario_id"])
            from_name = from_sc.scenario_code if from_sc else str(t["from_scenario_id"])
            to_name = to_sc.scenario_code if to_sc else str(t["to_scenario_id"])
            print(f"    {from_name} -> {to_name}  [sim={t['overlap_ratio']:.4f}]")

    if horizontal:
        print(f"\n  横向迁移边 ({len(horizontal)} 条):")
        for t in horizontal:
            from_sc = scenario_map.get(t["from_scenario_id"])
            to_sc = scenario_map.get(t["to_scenario_id"])
            from_name = from_sc.scenario_code if from_sc else str(t["from_scenario_id"])
            to_name = to_sc.scenario_code if to_sc else str(t["to_scenario_id"])
            print(f"    {from_name} <-> {to_name}  [sim={t['overlap_ratio']:.4f}]")

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

    t = ScenarioTransition(
        from_scenario_id=from_id,
        to_scenario_id=to_id,
        overlap_ratio=0.0,
        trigger_type="manual",
        required_hit_rate=0.8,
        shared_constraints=None,
        created_by="manual",
        edge_type=EdgeType.MANUAL.value,
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
    global VERTICAL_SIM_THRESHOLD, HORIZONTAL_SIM_MIN, HORIZONTAL_SIM_MAX, CROSS_TOPIC_SIM_THRESHOLD

    parser = argparse.ArgumentParser(
        description="Build micro-scenario transition graph for a topic (v2.0 Semantic)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--verbose", action="store_true",
        help="Print detailed similarity information",
    )
    parser.add_argument(
        "--vertical-threshold", type=float, default=VERTICAL_SIM_THRESHOLD,
        help=f"Vertical edge similarity threshold (default: {VERTICAL_SIM_THRESHOLD})",
    )
    parser.add_argument(
        "--horizontal-min", type=float, default=HORIZONTAL_SIM_MIN,
        help=f"Horizontal edge similarity minimum (default: {HORIZONTAL_SIM_MIN})",
    )
    parser.add_argument(
        "--horizontal-max", type=float, default=HORIZONTAL_SIM_MAX,
        help=f"Horizontal edge similarity maximum (default: {HORIZONTAL_SIM_MAX})",
    )
    parser.add_argument(
        "--build-cross-topic",
        action="store_true",
        help="Build cross-topic wormhole edges (across all topics)",
    )
    parser.add_argument(
        "--cross-topic-threshold", type=float, default=CROSS_TOPIC_SIM_THRESHOLD,
        help=f"Cross-topic similarity threshold (default: {CROSS_TOPIC_SIM_THRESHOLD})",
    )
    args = parser.parse_args()

    VERTICAL_SIM_THRESHOLD = args.vertical_threshold
    HORIZONTAL_SIM_MIN = args.horizontal_min
    HORIZONTAL_SIM_MAX = args.horizontal_max
    CROSS_TOPIC_SIM_THRESHOLD = args.cross_topic_threshold

    if not args.all and not args.topic_id and not args.build_cross_topic:
        parser.error("--topic-id is required (or use --all or --build-cross-topic)")

    db = SessionLocal()
    try:
        if args.build_cross_topic:
            # 跨话题虫洞构建（独立运行）
            build_cross_topic_graph(
                db,
                dry_run=args.dry_run,
                rebuild=args.rebuild,
                verbose=args.verbose,
            )
            logger.info("[DONE] Cross-topic graph build complete")
        elif args.all:
            topics = db.query(Topic).all()
            total_transitions = 0
            for topic in topics:
                for depth in [1, 2, 3]:
                    n = len(build_scenario_graph(
                        db, topic.id, depth,
                        dry_run=args.dry_run,
                        rebuild=args.rebuild,
                        verbose=args.verbose,
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
                verbose=args.verbose,
            )
            logger.info("[DONE] Graph build complete")
    finally:
        db.close()


if __name__ == "__main__":
    main()
