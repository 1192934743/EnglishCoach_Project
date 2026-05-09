"""
graph_generator.py — AI 驱动的离线微场景图谱节点生成器 (v2.0 RAG 增强版)

⚠️  架构约束：
    - 使用 Gemini embedding-001 生成向量（与 build_scenario_graph.py 共用）
    - RAG 检索：生成新节点前先查询已存在场景，避免重复

Usage:
    # 针对指定话题 ID 和深度层级生成节点
    python scripts/graph_generator.py --topic-id 1 --depth 1

    # 强制重建（先清理旧节点数据）
    python scripts/graph_generator.py --topic-id 1 --depth 1 --rebuild

    # 生成后自动构建图谱
    python scripts/graph_generator.py --topic-id 1 --depth 1 --build-graph
"""

import os
import sys
import json
import asyncio
import argparse
import logging

# 确保脚本可以从任何目录运行
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_script_dir)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv(os.path.join(_backend_dir, "config.env"))

import google.generativeai as genai

from database import SessionLocal, Topic, MicroScenario, ScenarioConstraint
from infrastructure.embedding import SyncSemanticEmbedder, cosine_similarity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("GraphGenerator")

# ================= LLM 配置 =================
proxy_url = os.getenv("HTTP_PROXY")
if proxy_url:
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("未配置 GEMINI_API_KEY，请在 config.env 中设置")

genai.configure(api_key=api_key)

model = genai.GenerativeModel(
    "gemini-2.5-pro",
    generation_config={"response_mime_type": "application/json"},
)

# ============================================================
# DEPTH 定义常量（v1.2 已定稿）
# 核心约束：Depth 仅控制"词汇/句法复杂度"，不控制对话轮数或故事情节
# 同一 Topic 的不同 Depth 必须保持相同的 step_order
# ============================================================

DEPTH_DEFINITIONS = """
## Difficulty Level Definitions (Strictly Follow)

IMPORTANT: All depths within the same topic must maintain the SAME step_order. Only expression complexity increases, NOT the basic storyline!

### Depth 1 - Foundation (Core Survival Phrases)
- Goal: Complete the core communicative function with simplest vocabulary.
- Vocabulary: High-frequency basic words, phrases preferred (e.g., I want, a coffee, how much).
- Constraints: Generate only 2-3 most essential nouns or basic verb phrases.
- Example: (Ordering Step) Constraints: ["I want", "coffee", "large"]

### Depth 2 - Intermediate (Politeness & Detail Modifiers)
- Goal: Add detail modifiers, variation options, and basic polite expressions on top of basic function.
- Vocabulary: Advanced compound words, complete simple sentences (e.g., I would like, instead of, with oat milk).
- Constraints: Generate 3-4 constraints, MUST include at least one polite expression or modifier.
- Example: (Ordering Step) Constraints: ["I would like", "a latte", "with oat milk", "please"]

### Depth 3 - Advanced (Native Expressions & Complex Sentences)
- Goal: Use native idioms, indirect requests, or complex clauses commonly used by native speakers.
- Vocabulary: Advanced vocabulary, subjunctive mood, complex sentences (e.g., I was wondering if, would it be possible to, I'd appreciate it if).
- Constraints: Generate 4-5 constraints, MUST include advanced communicative patterns.
- Example: (Ordering Step) Constraints: ["I was wondering if", "could possibly make it", "decaf", "extra shot"]
"""

DEPTH_META = {
    1: {
        "desc": "Foundation",
        "keyword": "core survival phrases",
        "constraint_range": (2, 3),
        "scenario_count": "3-5",
    },
    2: {
        "desc": "Intermediate",
        "keyword": "politeness and detail modifiers",
        "constraint_range": (3, 4),
        "scenario_count": "2-4",
    },
    3: {
        "desc": "Advanced",
        "keyword": "native expressions and complex sentences",
        "constraint_range": (4, 5),
        "scenario_count": "1-2",
    },
}

# ================= RAG 检索 =================

def retrieve_existing_scenarios(db: Session, topic_id: int, depth: int) -> list[dict]:
    """
    检索当前 Topic + Depth 下已存在的场景摘要。

    用于 RAG 增强生成：让 LLM 了解已有场景，避免生成重复内容。
    """
    scenarios = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.depth_level == depth,
    ).all()

    result = []
    for sc in scenarios:
        constraints = db.query(ScenarioConstraint).filter(
            ScenarioConstraint.micro_scenario_id == sc.id
        ).all()
        constraint_texts = [c.constraint_text for c in constraints]
        result.append({
            "scenario_code": sc.scenario_code,
            "scenario_name": sc.scenario_name,
            "intent_desc": sc.intent_desc,
            "constraints": constraint_texts,
        })

    return result


def build_rag_context(existing_scenarios: list[dict]) -> tuple[str, str]:
    """
    Build RAG context and generation instructions.

    Returns:
        (context_str, instruction_str)
    """
    if not existing_scenarios:
        return (
            "(No existing scenarios, feel free to generate 3-5 parallel scenarios)",
            "Please generate 3-5 parallel functional migration scenarios."
        )

    context_parts = [
        "=== Existing Scenarios (Generate new parallel scenarios, avoid vocabulary overlap) ==="
    ]
    for i, ex in enumerate(existing_scenarios, 1):
        context_parts.append(
            f"[Scenario {i}] {ex['scenario_name']}\n"
            f"  Intent: {ex['intent_desc']}\n"
            f"  Keywords: {', '.join(ex['constraints'])}"
        )
    context_str = "\n".join(context_parts)

    instruction = (
        "Please generate 2-3 new parallel scenarios.\n"
        "Requirements:\n"
        "- Similar communicative function (e.g., all ordering scenarios)\n"
        "- No vocabulary overlap with existing scenarios (if coffee exists, try milk tea or juice instead)\n"
        "- Each scenario must include 'flow_explanation' describing its position in the overall flow\n"
        "- step_order indicates sequence in vertical flow (smaller numbers come first)"
    )

    return context_str, instruction


# ================= 核心生成逻辑 =================

async def generate_micro_scenarios(topic: Topic, depth: int, db: Session) -> dict:
    """
    RAG 增强的微场景生成。

    流程：
    1. 检索已存在的场景
    2. 注入上下文到 Prompt
    3. 调用 Gemini 生成新场景
    """
    # Step 1: 检索已存在场景
    existing = retrieve_existing_scenarios(db, topic.id, depth)
    context_str, instruction_str = build_rag_context(existing)

    # Step 2: 获取 Depth 元数据
    meta = DEPTH_META[depth]
    constraint_range = meta["constraint_range"]

    # Step 3: 构建 Prompt（使用 DEPTH_DEFINITIONS）
    vocab_list = ', '.join(topic.vocab_tags or []) or "N/A"
    pattern_list = ', '.join(topic.sentence_patterns or []) or "N/A"
    prompt = f"""[Task: Generate functional migration micro-scenarios for {topic.title}]

## Topic Information
- Topic: {topic.title}
- Topic (Chinese): {getattr(topic, 'title_zh', 'N/A')}
- User Role: {topic.role_name or 'N/A'}

## Topic Vocabulary Reference (upstream vocab_tags — for constraint generation guidance)
- Vocab Tags: {vocab_list}
- Sentence Patterns: {pattern_list}

## Generation Guidance
- Constraints should align with and expand upon the topic's vocab_tags
- Prioritize vocabulary from the topic's sentence_patterns where semantically appropriate

## Difficulty Level Definitions
{DEPTH_DEFINITIONS}

## Generation Target
**This Generation: Depth {depth} ({meta['desc']})**
Keyword: {meta['keyword']}

## Existing Scenarios Reference (for avoiding duplicates)
{context_str}

## Generation Requirements
{instruction_str}
- Constraint count must be {constraint_range[0]}-{constraint_range[1]}

## Output Format (JSON only, no other text)
{{
    "scenarios": [
        {{
            "scenario_code": "UNIQUE_CODE",
            "scenario_name": "Scenario Name",
            "intent_desc": "Teaching intent description",
            "scene_desc": "Scene description (must reflect {meta['keyword']})",
            "step_order": 1-3,
            "flow_explanation": "Position of this scenario in the overall flow (must match step_order of other depths for same topic)",
            "is_entry_point": true/false,
            "constraints": [
                {{
                    "constraint_text": "Target vocabulary/phrase",
                    "constraint_type": "word" or "phrase" or "sentence",
                    "weight": 1.0,
                    "hint_cn": "Chinese hint for user"
                }}
            ]
        }}
    ]
}}

## Quality Checklist
- [ ] Does the scenario match Depth {depth} definition?
- [ ] Does vocabulary complexity match "{meta['keyword']}"?
- [ ] Is step_order consistent with other depths (only expression differs)?
- [ ] Is there no overlap with existing scenarios?
- [ ] Is constraint count within {constraint_range[0]}-{constraint_range[1]} range?
"""

    logger.info(f"🧠 正在呼叫 Gemini 生成 '{topic.title}' (Depth {depth}) 的功能迁移节点...")

    if existing:
        logger.info(f"   RAG 上下文：已存在 {len(existing)} 个场景，将注入到 Prompt")

    response = await asyncio.to_thread(model.generate_content, prompt)

    try:
        data = json.loads(response.text)
        return data
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON 解析失败: {e}\n原始输出:\n{response.text}")
        raise


def clear_existing_graph(db: Session, topic_id: int, depth: int):
    """清理旧数据，防止重复执行造成数据污染"""
    scenarios = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.depth_level == depth,
    ).all()

    if not scenarios:
        logger.warning(f"🔍 话题 ID {topic_id} 下没有找到现存的 Depth={depth} 节点。")
        return

    scenario_ids = [s.id for s in scenarios]

    deleted_c = db.query(ScenarioConstraint).filter(
        ScenarioConstraint.micro_scenario_id.in_(scenario_ids),
    ).delete(synchronize_session=False)

    deleted_s = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.depth_level == depth,
    ).delete(synchronize_session=False)

    db.commit()
    logger.info(f"🗑️ 已清理该话题下 Depth={depth} 的旧数据: {deleted_s} 个场景, {deleted_c} 个约束。")


def ingest_graph_nodes(db: Session, topic: Topic, depth: int, graph_data: dict) -> list[int]:
    """
    将生成的 JSON 数据实例化并写入数据库。

    Returns:
        新创建的场景 ID 列表
    """
    scenarios_data = graph_data.get("scenarios", [])

    if not scenarios_data:
        logger.warning("⚠️ LLM 返回了空的场景列表。")
        return []

    logger.info(f"📥 开始落库 {len(scenarios_data)} 个平行微场景...")

    new_scenario_ids = []
    embedder = SyncSemanticEmbedder(cache_enabled=True)

    for s_data in scenarios_data:
        scenario = MicroScenario(
            topic_id=topic.id,
            scenario_code=s_data["scenario_code"],
            scenario_name=s_data["scenario_name"],
            intent_desc=s_data["intent_desc"],
            scene_desc=s_data["scene_desc"],
            depth_level=depth,
            step_order=s_data["step_order"],
            is_entry_point=s_data["is_entry_point"],
            max_turns=8,
            weight=1.0,
            embedding=None,  # 后续由 build_scenario_graph.py 补全
        )
        db.add(scenario)
        db.commit()
        db.refresh(scenario)
        new_scenario_ids.append(scenario.id)

        constraints_data = s_data.get("constraints", [])
        for c_data in constraints_data:
            constraint = ScenarioConstraint(
                micro_scenario_id=scenario.id,
                constraint_text=c_data["constraint_text"],
                constraint_type=c_data["constraint_type"],
                depth_level=depth,
                weight=c_data.get("weight", 1.0),
                hint_cn=c_data.get("hint_cn", ""),
            )
            db.add(constraint)

        db.commit()
        logger.info(
            f"  ✅ [SCENARIO] {scenario.scenario_code} "
            f"(Step {scenario.step_order}) 包含 {len(constraints_data)} 个约束词"
        )

    return new_scenario_ids


# ================= CLI 入口 =================

async def main():
    parser = argparse.ArgumentParser(
        description="AI 驱动的离线微场景图谱节点生成器 (v2.0 RAG 增强版)"
    )
    parser.add_argument("--topic-id", type=int, required=True, help="目标话题 ID")
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        choices=[1, 2, 3],
        help="生成难度等级 (1, 2, 3)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="是否先删除该层级下的旧节点",
    )
    parser.add_argument(
        "--build-graph",
        action="store_true",
        help="生成后自动构建图谱",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        topic = db.query(Topic).filter(Topic.id == args.topic_id).first()
        if not topic:
            logger.error(f"❌ 找不到 ID 为 {args.topic_id} 的话题！")
            return

        if args.rebuild:
            clear_existing_graph(db, topic.id, args.depth)

        # 1. 生成节点与约束
        graph_data = await generate_micro_scenarios(topic, args.depth, db)

        # 2. 数据落库
        new_ids = ingest_graph_nodes(db, topic, args.depth, graph_data)

        # 3. 可选：构建图谱
        if args.build_graph and new_ids:
            logger.info("=" * 60)
            logger.info("📊 开始构建图谱...")
            logger.info("=" * 60)

            # 延迟导入避免循环依赖
            from scripts.build_scenario_graph import build_scenario_graph

            transitions = build_scenario_graph(
                db, topic.id, args.depth,
                dry_run=False,
                rebuild=False,
                verbose=False,
            )
            logger.info(f"✅ 图谱构建完成：{len(transitions)} 条边")

        logger.info("=" * 60)
        logger.info(f"🎉 节点生成完毕！共 {len(new_ids)} 个场景")
        if args.build_graph:
            logger.info(f"✅ 图谱构建完毕")
        else:
            logger.info(f"👉 下一步操作建议：运行自动连边算法")
            logger.info(f"python scripts/build_scenario_graph.py --topic-id {topic.id} --depth {args.depth} --rebuild")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
