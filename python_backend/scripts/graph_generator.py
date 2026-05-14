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
from dataclasses import dataclass
from typing import Optional

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
from infrastructure.embedding import cosine_similarity
from infrastructure.graph import DEPTH_DEFINITIONS, DEPTH_META
from infrastructure.graph.prompt_builder import parse_json_response, batch_embeddings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("GraphGenerator")


# ================= DTO 模式 =================

@dataclass
class TopicDTO:
    """话题数据传输对象（避免跨 Session 传递 ORM 对象）"""
    id: int
    title: str
    title_zh: Optional[str]
    role_name: Optional[str]
    vocab_tags: Optional[list]
    sentence_patterns: Optional[list]

    @classmethod
    def from_orm(cls, topic: Topic) -> "TopicDTO":
        """从 ORM 对象创建 DTO"""
        return cls(
            id=topic.id,
            title=topic.title,
            title_zh=getattr(topic, 'title_zh', None),
            role_name=topic.role_name,
            vocab_tags=topic.vocab_tags,
            sentence_patterns=topic.sentence_patterns,
        )

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
        "- step_order indicates sequence in vertical flow (smaller numbers come first)"
    )

    return context_str, instruction


# ================= Prompt 构建 =================

def get_other_depths_step_orders(db: Session, topic_id: int, current_depth: int) -> dict[int, list[int]]:
    """
    获取同一话题下其他 Depth 的 step_order 分布。

    Args:
        db: 数据库会话
        topic_id: 话题 ID
        current_depth: 当前正在生成的 Depth（排除自身）

    Returns:
        {depth: [step_orders]} 其他 Depth 的 step_order 列表
    """
    other_depths = [d for d in [1, 2, 3] if d != current_depth]
    result = {}

    for depth in other_depths:
        scenarios = db.query(MicroScenario).filter(
            MicroScenario.topic_id == topic_id,
            MicroScenario.depth_level == depth,
        ).all()
        if scenarios:
            # 获取去重后的 step_order 列表并排序
            step_orders = sorted(set(s.step_order for s in scenarios))
            if step_orders:
                result[depth] = step_orders

    return result


def build_scenario_prompt(
    topic: "TopicDTO",
    depth: int,
    existing_scenarios: list[dict],
    other_depths_info: Optional[dict] = None,
) -> str:
    """
    构建微场景生成的 Prompt（供 dry-run 和 LLM 评审使用）。

    这个函数不调用 API，只构建 Prompt 字符串。
    调用方负责决定是打印、评审还是发送给 LLM。

    Args:
        topic: TopicDTO（话题信息）
        depth: 难度层级 (1/2/3)
        existing_scenarios: 已存在场景列表（RAG 上下文）
        other_depths_info: 其他 Depth 的 step_order 分布，用于保持一致性

    Returns:
        完整的英文 Prompt 字符串
    """
    context_str, instruction_str = build_rag_context(existing_scenarios)
    meta = DEPTH_META[depth]
    constraint_range = meta["constraint_range"]

    # 构建 step_order 一致性约束
    if other_depths_info:
        max_step = max(len(steps) for steps in other_depths_info.values()) if other_depths_info else 3
        all_steps = list(range(1, max_step + 1)) if max_step > 0 else [1]
        step_order_block = f"""
## CRITICAL: Step Order Consistency Across Depths
If this topic has multiple depths (1, 2, 3), ALL depths MUST have the SAME number of steps and MATCHING step_order values.
Only vocabulary complexity varies across depths, NOT the storyline structure.

Example: If Depth 1 has step_order [1, 2, 3], then Depth 2 and Depth 3 MUST also have exactly [1, 2, 3].

This generation MUST use: step_order values from {all_steps}
"""
    else:
        step_order_block = """
## Step Order Structure
- step_order values: MUST be [1, 2, 3] (3 steps total)
- Step 1: Entry/basic step (e.g., greeting, basic request)
- Step 2: Core functional step (e.g., main task)
- Step 3: Completion/exit step (e.g., payment, farewell)
"""

    prompt = f"""[Task: Generate functional migration micro-scenarios for {topic.title}]

## Scene Context
- Topic: {topic.title}
- Topic (Chinese): {getattr(topic, 'title_zh', 'N/A')}
- Learner Role: {topic.role_name or 'N/A'}

{DEPTH_DEFINITIONS}

{step_order_block}

## Generation Target
**This Generation: Depth {depth} ({meta['desc']})**
Keyword: {meta['keyword']}

## Existing Scenarios Reference (for avoiding duplicates)
{context_str}

## Generation Requirements
{instruction_str}
- Constraint count must be {constraint_range[0]}-{constraint_range[1]}
- For `is_entry_point`: Set the first scenario in the array to `true`, all others to `false`

## Output Format (JSON only, no other text)
{{
    "scenarios": [
        {{
            "scenario_code": "UNIQUE_CODE",
            "scenario_name": "Scenario Name",
            "intent_desc": "Teaching intent description",
            "scene_desc": "Scene description (must reflect {meta['keyword']})",
            "step_order": 1,
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
- [ ] Does the scenario match the "{topic.title}" domain?
- [ ] Does vocabulary complexity match "{meta['keyword']}"?
- [ ] Does the scenario teach practical, real-world expressions?
- [ ] Is step_order within [1, 2, 3] and consistent with other depths?{"\n- [ ] Is there no overlap with existing scenarios?" if existing_scenarios else ""}
- [ ] Is constraint count within {constraint_range[0]}-{constraint_range[1]} range?
"""
    return prompt


def translate_prompt_summary(prompt: str, topic_title: str, depth: int, existing_count: int, meta: dict) -> str:
    """
    将 Prompt 关键信息翻译为中文摘要（用于 dry-run 打印）。

    Args:
        prompt: 原始英文 Prompt
        topic_title: 话题标题
        depth: 难度层级
        existing_count: 已存在场景数量
        meta: DEPTH_META 字典

    Returns:
        中文摘要字符串
    """
    depth_labels = {1: "基础", 2: "进阶", 3: "高级"}
    depth_label = depth_labels.get(depth, depth)

    # 提取 Common Expression Patterns 部分（只取顶级的 - 开头的行）
    pattern_lines = []
    in_patterns_section = False
    section_indent = None
    for line in prompt.split('\n'):
        if '## Common Expression Patterns' in line:
            in_patterns_section = True
            continue
        if in_patterns_section:
            stripped = line.strip()
            if not stripped:  # 空行，继续
                continue
            # 遇到下一个 ## 标题，停止
            if stripped.startswith('##'):
                break
            # 只收集顶级的 - 开头的行（不带缩进的）
            if stripped.startswith('- '):
                indent = len(line) - len(line.lstrip())
                if section_indent is None:
                    section_indent = indent
                if indent == section_indent:
                    pattern_lines.append(stripped[2:])
                elif indent < section_indent:
                    break

    patterns_preview = ', '.join(pattern_lines[:4]) if pattern_lines else "N/A"
    if len(pattern_lines) > 4:
        patterns_preview += f" ... (+{len(pattern_lines) - 4} more)"

    constraint_range = meta.get("constraint_range", (0, 0))
    scenario_range = meta.get("scenario_count", "?")

    summary_parts = [
        f"话题: {topic_title}",
        f"难度: Depth {depth} ({depth_label}) — {meta.get('keyword', 'N/A')}",
        f"目标: 生成 {scenario_range} 个场景",
        f"约束数: 每个场景 {constraint_range[0]}-{constraint_range[1]} 个约束",
        f"已有场景: {existing_count} 个（用于 RAG 查重）",
    ]

    if patterns_preview and patterns_preview != "N/A":
        summary_parts.append(f"句型参考: {patterns_preview}")

    return "\n".join(summary_parts)


# ================= 核心生成逻辑 =================

async def generate_micro_scenarios(topic: TopicDTO, depth: int, db: Session) -> dict:
    """
    RAG 增强的微场景生成。

    流程：
    1. 检索已存在的场景
    2. 查询其他 Depth 的 step_order 分布（用于保持一致性）
    3. 构建 Prompt 并调用 Gemini 生成新场景

    注意：使用 TopicDTO 避免跨 Session 访问 ORM 对象
    """
    logger.info(f"[generate_micro_scenarios] 入口: topic_id={topic.id}, depth={depth}")

    # Step 1: 检索已存在场景
    existing = retrieve_existing_scenarios(db, topic.id, depth)
    logger.info(f"[generate_micro_scenarios] RAG 检索结果: {len(existing)} 个已存在场景")
    for i, ex in enumerate(existing):
        logger.debug(f"  [{i+1}] {ex['scenario_name']}: {ex['intent_desc'][:50]}...")

    # Step 2: 查询其他 Depth 的 step_order 分布（用于保持一致性）
    other_depths_info = get_other_depths_step_orders(db, topic.id, depth)
    if other_depths_info:
        logger.info(f"[generate_micro_scenarios] 检测到其他 Depth 的 step_order: {other_depths_info}")
    else:
        logger.info(f"[generate_micro_scenarios] 未检测到其他 Depth，将使用默认 step_order=[1,2,3]")

    # Step 3: 构建 Prompt
    prompt = build_scenario_prompt(topic, depth, existing, other_depths_info)
    logger.info(f"[generate_micro_scenarios] Prompt 长度: {len(prompt)} 字符")

    # Step 3: 获取 Depth 元数据（用于日志）
    meta = DEPTH_META[depth]
    constraint_range = meta["constraint_range"]

    logger.info(f"🧠 正在呼叫 Gemini 生成 '{topic.title}' (Depth {depth}) 的功能迁移节点...")

    if existing:
        logger.info(f"   RAG 上下文：已存在 {len(existing)} 个场景，将注入到 Prompt")

    # 添加超时保护和重试机制
    TIMEOUT_SECONDS = 120
    MAX_RETRIES = 3
    RETRY_DELAY = 5

    logger.info(f"[generate_micro_scenarios] 调用 Gemini API，Prompt 长度: {len(prompt)} 字符")
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(model.generate_content, prompt),
                timeout=TIMEOUT_SECONDS
            )
            logger.info(f"[generate_micro_scenarios] Gemini 响应长度: {len(response.text)} 字符")
            break  # 成功，跳出重试循环
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ API 调用超时（第 {attempt}/{MAX_RETRIES} 次，{TIMEOUT_SECONDS}秒）")
            if attempt < MAX_RETRIES:
                logger.info(f"   等待 {RETRY_DELAY} 秒后重试...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error(f"❌ API 调用超时，已达最大重试次数（{MAX_RETRIES}）")
                raise
        except Exception as api_error:
            logger.error(f"❌ API 调用失败: {type(api_error).__name__}")
            logger.error(f"❌ 错误详情: {api_error}")
            if hasattr(api_error, 'response') and api_error.response:
                logger.error(f"❌ API 响应: {api_error.response.text}")
            raise
    logger.debug(f"[generate_micro_scenarios] Gemini 响应预览: {response.text[:200]}...")

    # 使用容错 JSON 解析（支持 markdown 代码块）
    try:
        data = parse_json_response(response.text)
        scenarios_count = len(data.get("scenarios", []))
        logger.info(f"[generate_micro_scenarios] JSON 解析成功: {scenarios_count} 个场景")
        for i, sc in enumerate(data.get("scenarios", [])):
            constraint_count = len(sc.get("constraints", []))
            logger.debug(f"  场景[{i+1}] {sc['scenario_name']}: {constraint_count} 个约束")
        return data
    except ValueError as e:
        logger.error(f"❌ JSON 解析失败: {e}")
        logger.error(f"[generate_micro_scenarios] 原始响应文本:\n{response.text[:1000]}")
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


async def ingest_graph_nodes(db: Session, topic: TopicDTO, depth: int, graph_data: dict) -> list[int]:
    """
    将生成的 JSON 数据实例化并写入数据库（DTO 模式）。

    架构设计：
    1. Step 1: DB I/O - 写入 MicroScenario（主线程，持有 Session）
    2. Step 2: Network I/O - 通过 to_thread 调用（子线程，传递纯数据）
    3. Step 3: DB I/O - 更新 embedding 并写入 Constraints（主线程）

    Args:
        db: 数据库会话（主线程持有）
        topic: TopicDTO（避免跨 Session 访问 ORM 对象）
        depth: 难度层级
        graph_data: LLM 返回的图数据

    Returns:
        新创建的场景 ID 列表
    """
    scenarios_data = graph_data.get("scenarios", [])
    logger.info(f"[ingest_graph_nodes] 入口: topic_id={topic.id}, depth={depth}, 收到 {len(scenarios_data)} 个场景数据")

    if not scenarios_data:
        logger.warning("⚠️ LLM 返回了空的场景列表。")
        return []

    logger.info(f"📥 开始落库 {len(scenarios_data)} 个平行微场景...")

    # Step 1: 先批量写入 MicroScenario（主线程，持有 DB Session）
    logger.info(f"[ingest_graph_nodes] Step 1: 写入 MicroScenario 表...")
    scenarios = []
    for s_data in scenarios_data:
        logger.debug(f"  写入场景: {s_data['scenario_code']} - {s_data['scenario_name']}")
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
            embedding=None,  # 后续批量生成
        )
        db.add(scenario)
        scenarios.append(scenario)

    logger.info(f"[ingest_graph_nodes] db.add() 完成，准备 commit...")
    db.commit()
    logger.info(f"[ingest_graph_nodes] commit 成功")
    
    # 验证 commit 后获取 ID（refresh 需要单个对象，逐个处理）
    logger.info(f"[ingest_graph_nodes] refresh 阶段：逐个刷新 {len(scenarios)} 个场景...")
    for s in scenarios:
        db.refresh(s)
        logger.debug(f"  刷新场景 ID: {s.id}, code: {s.scenario_code}")
    logger.info(f"[ingest_graph_nodes] refresh 完成，场景数量: {len(scenarios)}")

    # Step 2: 构建纯数据结构（只传递 tuple，不传递 ORM 对象）
    scenario_ids = [s.id for s in scenarios]
    texts = [
        f"{s.scenario_name} | Intent: {s.intent_desc} | Scene: {s.scene_desc}"
        for s in scenarios
    ]
    logger.info(f"[ingest_graph_nodes] Step 2: 准备生成 embedding, 场景数: {len(scenario_ids)}")
    for tid, txt in zip(scenario_ids, texts):
        logger.debug(f"  ID={tid}: {txt[:60]}...")

    # 🌟 DTO 模式：将耗时的同步网络请求扔进线程池
    logger.info(f"[ingest_graph_nodes] 调用 batch_embeddings (cache_enabled=True)...")
    vectors_map = await batch_embeddings(scenario_ids, texts, cache_enabled=True)
    logger.info(f"[ingest_graph_nodes] batch_embeddings 返回: {len(vectors_map)} 个向量")
    for tid in scenario_ids:
        has_vec = tid in vectors_map and vectors_map[tid] is not None
        logger.debug(f"  ID={tid}: embedding {'存在' if has_vec else '缺失'}")

    # Step 3: 更新 embedding（主线程，持有 DB Session）
    logger.info(f"[ingest_graph_nodes] Step 3: 更新 embedding 到数据库...")
    for scenario in scenarios:
        scenario.embedding = vectors_map.get(scenario.id)
    db.commit()
    logger.info(f"[ingest_graph_nodes] embedding 更新 commit 成功")

    # Step 4: 写入 Constraints
    logger.info(f"[ingest_graph_nodes] Step 4: 写入 ScenarioConstraint 表...")
    for s_data, scenario in zip(scenarios_data, scenarios):
        constraints_data = s_data.get("constraints", [])
        logger.debug(f"  场景 {scenario.id}: 写入 {len(constraints_data)} 个约束")
        for c_data in constraints_data:
            constraint = ScenarioConstraint(
                micro_scenario_id=scenario.id,
                constraint_text=c_data["constraint_text"],
                constraint_type=c_data.get("constraint_type", "phrase"),
                depth_level=depth,
                weight=c_data.get("weight", 1.0),
                hint_cn=c_data.get("hint_cn", ""),
            )
            db.add(constraint)
    db.commit()
    logger.info(f"[ingest_graph_nodes] constraints commit 成功")

    logger.info(f"  ✅ 落库完成：{len(scenarios)} 个场景，embedding 已生成")

    return [s.id for s in scenarios]


# ================= 公共接口 =================

async def generate_and_save(
    topic: TopicDTO,
    depth: int,
    db: Session,
    rebuild: bool = False,
) -> list[int]:
    """
    公共接口：生成并保存微场景。

    事务原子性保证：
    1. 先调用 LLM 生成数据（可能失败）
    2. 确认数据有效后，再清理旧数据和写入新数据

    Args:
        topic: TopicDTO（避免跨 Session 访问 ORM 对象）
        depth: 难度层级 (1/2/3)
        db: 数据库会话
        rebuild: 是否先清理旧数据

    Returns:
        新创建的场景 ID 列表

    Raises:
        Exception: LLM 调用或数据解析失败时抛出异常
    """
    logger.info(f"[generate_and_save] ========== 开始执行 ==========")
    logger.info(f"[generate_and_save] 参数: topic_id={topic.id}, depth={depth}, rebuild={rebuild}")
    
    try:
        # Step 1: 先调用 LLM 生成数据（可能失败）
        logger.info(f"[generate_and_save] Step 1: 调用 generate_micro_scenarios...")
        graph_data = await generate_micro_scenarios(topic, depth, db)
        logger.info(f"[generate_and_save] Step 1 完成: graph_data 包含 {len(graph_data.get('scenarios', []))} 个场景")

        # Step 2: 确认数据有效后，清理旧数据
        if rebuild:
            logger.info(f"[generate_and_save] Step 2: 调用 clear_existing_graph (rebuild=True)...")
            clear_existing_graph(db, topic.id, depth)
        else:
            logger.info(f"[generate_and_save] Step 2: 跳过清理 (rebuild=False)")

        # Step 3: 写入新数据（包含批量 embedding）
        logger.info(f"[generate_and_save] Step 3: 调用 ingest_graph_nodes...")
        new_ids = await ingest_graph_nodes(db, topic, depth, graph_data)
        logger.info(f"[generate_and_save] Step 3 完成: 新增 {len(new_ids)} 个场景，IDs={new_ids}")
        
        logger.info(f"[generate_and_save] ========== 执行成功 ==========")
        return new_ids

    except Exception as e:
        logger.error(f"[generate_and_save] ========== 执行失败 ==========")
        logger.error(f"[generate_and_save] 异常类型: {type(e).__name__}")
        logger.error(f"[generate_and_save] 异常信息: {e}")
        db.rollback()  # 失败时必须回滚，防止 PendingRollbackError
        logger.error(f"[generate_and_save] 已执行 db.rollback()")
        raise


# ================= CLI 入口 =================

async def main():
    logger.info(f"[main] ========== graph_generator.py 启动 ==========")
    logger.info(f"[main] Python 版本: {sys.version}")
    logger.info(f"[main] 工作目录: {os.getcwd()}")
    
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
    
    logger.info(f"[main] 解析参数: topic-id={args.topic_id}, depth={args.depth}, rebuild={args.rebuild}, build-graph={args.build_graph}")

    logger.info(f"[main] 创建数据库会话...")
    db = SessionLocal()
    try:
        logger.info(f"[main] 查询话题 ID={args.topic_id}...")
        topic = db.query(Topic).filter(Topic.id == args.topic_id).first()
        if not topic:
            logger.error(f"❌ 找不到 ID 为 {args.topic_id} 的话题！")
            return
        
        logger.info(f"[main] 找到话题: {topic.title} (role_name={topic.role_name})")
        logger.info(f"[main] vocab_tags: {topic.vocab_tags}")
        logger.info(f"[main] sentence_patterns: {topic.sentence_patterns}")

        # 使用公共接口：生成并保存（事务原子性）
        # 注意：转换为 TopicDTO 避免跨 Session 访问 ORM 对象
        topic_dto = TopicDTO.from_orm(topic)
        logger.info(f"[main] 转换为 TopicDTO 完成，调用 generate_and_save...")
        new_ids = await generate_and_save(topic_dto, args.depth, db, rebuild=args.rebuild)

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
        logger.info(f"[main] ========== 正常结束 ==========")

    except Exception as e:
        logger.error(f"[main] ========== 异常退出 ==========")
        logger.error(f"[main] 异常类型: {type(e).__name__}")
        logger.error(f"[main] 异常信息: {e}")
        import traceback
        logger.error(f"[main] 堆栈跟踪:\n{traceback.format_exc()}")
        db.rollback()
    finally:
        logger.info(f"[main] 关闭数据库会话...")
        db.close()
        logger.info(f"[main] ========== 程序结束 ==========")


if __name__ == "__main__":
    asyncio.run(main())
