"""
prompt_builder.py — Prompt 模板构建器

包含：
- Prompt 模板构建
- RAG 上下文构建
- JSON 解析容错
- 批量 embedding（DTO 模式）

版本: v1.0 (2026-05-13)
"""

import json
import re
import time
import asyncio
import logging
from typing import Optional

from sqlalchemy.orm import Session

from database import MicroScenario, ScenarioConstraint
from infrastructure.embedding import SyncSemanticEmbedder
from infrastructure.graph import DEPTH_DEFINITIONS, DEPTH_META
from infrastructure.graph.graph_config import EMBEDDING_BATCH_SIZE, EMBEDDING_RATE_LIMIT_DELAY

logger = logging.getLogger("GraphGenerator")


# ================= JSON 解析容错 =================

def parse_json_response(text: str) -> dict:
    """
    解析 LLM 输出，支持从 markdown 中提取 JSON。

    解析策略（按优先级）：
    1. 直接解析纯 JSON
    2. 从 ```json 代码块提取
    3. 从 ``` 代码块提取
    4. 正则提取 JSON 对象
    """
    if not text or not text.strip():
        raise ValueError("LLM 返回了空文本")

    text = text.strip()

    # 策略 1: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 策略 2: 从 ```json 代码块提取
    match = re.search(r'```json\s*([\s\S]*?)```', text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 策略 3: 从 ``` 代码块提取
    match = re.search(r'```\s*([\s\S]*?)```', text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 策略 4: 正则提取 JSON 对象
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法解析 LLM 输出: {text[:200]}...")


# ================= RAG 上下文构建 =================

def build_rag_context(db: Session, topic_id: int, depth: int) -> str:
    """
    构建 RAG 上下文，用于在 Prompt 中提供已存在场景的参考。

    Returns:
        格式化的上下文字符串，如果没有已存在场景则返回空字符串
    """
    existing = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.depth_level == depth,
    ).all()

    if not existing:
        return ""

    lines = ["=== Existing Scenarios (Generate new parallel scenarios, avoid vocabulary overlap) ==="]
    for i, sc in enumerate(existing, 1):
        constraints = db.query(ScenarioConstraint).filter(
            ScenarioConstraint.micro_scenario_id == sc.id
        ).all()
        constraint_texts = [c.constraint_text for c in constraints]
        lines.append(f"[Scenario {i}] {sc.scenario_name}")
        lines.append(f"  Intent: {sc.intent_desc}")
        lines.append(f"  Keywords: {', '.join(constraint_texts)}")

    return "\n".join(lines)


# ================= Prompt 模板构建 =================

def build_scenario_prompt(
    topic_title: str,
    topic_title_zh: Optional[str],
    role_name: Optional[str],
    vocab_tags: Optional[list],
    sentence_patterns: Optional[list],
    depth: int,
    rag_context: str,
    other_depths_info: Optional[dict] = None,
) -> str:
    """
    构建微场景生成的 Prompt。

    Args:
        topic_title: 话题英文标题
        topic_title_zh: 话题中文标题
        role_name: 用户角色名称
        vocab_tags: 词汇标签列表
        sentence_patterns: 句型列表
        depth: 难度层级 (1/2/3)
        rag_context: RAG 上下文（已存在场景）
        other_depths_info: 其他 Depth 的 step_order 分布，用于保持一致性
                          格式: {1: [1, 2, 3], 2: [1, 2, 3]} 表示每个 Depth 的 step_order 列表

    Returns:
        格式化的 Prompt 字符串
    """
    meta = DEPTH_META[depth]
    vocab_list = ', '.join(vocab_tags) if vocab_tags else "N/A"
    pattern_list = ', '.join(sentence_patterns) if sentence_patterns else "N/A"

    # 构建 step_order 一致性约束
    step_order_constraint = ""
    if other_depths_info:
        # 计算所有 Depth 的最大 step_order（取最大值以保证一致性）
        max_step = max(len(steps) for steps in other_depths_info.values()) if other_depths_info else 3
        all_steps = [1, 2, 3] if max_step >= 3 else (list(range(1, max_step + 1)) if max_step > 0 else [1])

        step_order_constraint = f"""
## CRITICAL: Step Order Consistency Across Depths
If this topic has multiple depths (1, 2, 3), ALL depths MUST have the SAME number of steps and MATCHING step_order values.
Only vocabulary complexity varies across depths, NOT the storyline structure.

Example: If Depth 1 has step_order [1, 2, 3], then Depth 2 and Depth 3 MUST also have exactly [1, 2, 3].

This generation MUST use: step_order values from {all_steps}
"""
    else:
        # 默认生成 3 个 step（符合 DEPTH_META 的 scenario_count 总计约 6-12 个场景）
        step_order_constraint = """
## Step Order Structure
- step_order values: MUST be [1, 2, 3] (3 steps total)
- Step 1: Entry/basic step (e.g., greeting, basic request)
- Step 2: Core functional step (e.g., main task)
- Step 3: Completion/exit step (e.g., payment, farewell)
- Each step should have {scenario_count} scenario(s).
""".format(scenario_count=meta['scenario_count'])

    return f"""[Task: Generate functional migration micro-scenarios for {topic_title}]

## Topic Information
- Topic: {topic_title}
- Topic (Chinese): {topic_title_zh or 'N/A'}
- User Role: {role_name or 'N/A'}

## Topic Vocabulary Reference
- Vocab Tags: {vocab_list}
- Sentence Patterns: {pattern_list}

## Generation Guidance
- Constraints should align with and expand upon the topic's vocab_tags
- Prioritize vocabulary from the topic's sentence_patterns where semantically appropriate

## Difficulty Level Definitions
{DEPTH_DEFINITIONS}
{step_order_constraint}

## Generation Target
**This Generation: Depth {depth} ({meta['desc']})**
Keyword: {meta['keyword']}

## Existing Scenarios Reference (for avoiding duplicates)
{rag_context or '(No existing scenarios, feel free to generate scenarios per step)'}

## Generation Requirements
- Generate {meta['scenario_count']} parallel scenario(s) per step
- Similar communicative function within each step
- No vocabulary overlap with existing scenarios
- Constraint count must be {meta['constraint_range'][0]}-{meta['constraint_range'][1]}

## Output Format (JSON only)
{{
    "scenarios": [
        {{
            "scenario_code": "UNIQUE_CODE",
            "scenario_name": "Scenario Name",
            "intent_desc": "Teaching intent description",
            "scene_desc": "Scene description",
            "step_order": 1-3,
            "flow_explanation": "Position of this scenario in the overall flow",
            "is_entry_point": true/false,
            "constraints": [
                {{
                    "constraint_text": "Target vocabulary/phrase",
                    "constraint_type": "word or phrase or sentence",
                    "weight": 1.0,
                    "hint_cn": "Chinese hint for user"
                }}
            ]
        }}
    ]
}}

## Quality Checklist
- [ ] Does the scenario match Depth {depth} definition?
- [ ] Is vocabulary complexity appropriate?
- [ ] Is step_order within [1, 2, 3] and consistent with other depths?
- [ ] Is constraint count within range?
"""


# ================= 批量 Embedding (DTO 模式) =================

def fetch_embeddings_sync(
    texts_batch: list[tuple[int, str]],
    embedder: SyncSemanticEmbedder,
    chunk_size: int = 10,
    rate_limit_delay: float = 1.0,
) -> dict[int, list[float]]:
    """
    在独立线程中运行：纯同步网络请求 + sleep 限流。

    这是 DTO 模式的核心函数：
    - 不接触任何 DB Session 和 ORM 对象
    - 只接收和返回纯 Python 数据结构

    Args:
        texts_batch: [(scenario_id, text), ...] 纯数据结构
        embedder: SyncSemanticEmbedder 实例
        chunk_size: 每批处理的数量
        rate_limit_delay: 批次间的延迟（秒）

    Returns:
        {scenario_id: vector} 纯数据结构
    """
    logger.info(f"[fetch_embeddings_sync] 线程启动，文本数量: {len(texts_batch)}, chunk_size={chunk_size}")
    vectors_map: dict[int, list[float]] = {}
    start_time = time.time()

    for i in range(0, len(texts_batch), chunk_size):
        chunk = texts_batch[i:i + chunk_size]
        chunk_num = i // chunk_size + 1

        logger.debug(f"[fetch_embeddings_sync] 处理批次 {chunk_num}: {len(chunk)} 条")
        for scenario_id, text in chunk:
            try:
                vector = embedder.embed(text)
                vectors_map[scenario_id] = vector
                logger.debug(f"[fetch_embeddings_sync] ID={scenario_id}: 向量维度={len(vector) if vector else 0}")
            except Exception as e:
                logger.error(f"[fetch_embeddings_sync] ID={scenario_id} 生成失败: {e}")
                vectors_map[scenario_id] = None

        # 批次间限流（只阻塞当前子线程，不影响主事件循环）
        if i + chunk_size < len(texts_batch):
            logger.debug(f"[fetch_embeddings_sync] 批次 {chunk_num} 完成，等待 {rate_limit_delay}s 限流...")
            time.sleep(rate_limit_delay)

    elapsed = time.time() - start_time
    success_count = sum(1 for v in vectors_map.values() if v is not None)
    logger.info(f"[fetch_embeddings_sync] 线程完成: {success_count}/{len(texts_batch)} 成功, 耗时 {elapsed:.2f}s")
    return vectors_map


async def batch_embeddings(
    scenario_ids: list[int],
    texts: list[str],
    cache_enabled: bool = True,
    chunk_size: int = EMBEDDING_BATCH_SIZE,
    rate_limit_delay: float = EMBEDDING_RATE_LIMIT_DELAY,
) -> dict[int, list[float]]:
    """
    异步批量生成 embedding（DTO 模式）。

    将耗时的同步网络请求扔进线程池，且只传递 tuple，完美隔离！

    Args:
        scenario_ids: 场景 ID 列表
        texts: 对应的文本列表
        cache_enabled: 是否启用缓存
        chunk_size: 每批处理的数量
        rate_limit_delay: 批次间的延迟（秒）

    Returns:
        {scenario_id: vector} 纯数据结构
    """
    logger.info(f"[batch_embeddings] ========== 入口 ==========")
    logger.info(f"[batch_embeddings] 场景数量: {len(scenario_ids)}, cache_enabled: {cache_enabled}")
    logger.info(f"[batch_embeddings] chunk_size: {chunk_size}, rate_limit_delay: {rate_limit_delay}s")
    for i, (sid, txt) in enumerate(zip(scenario_ids, texts)):
        logger.debug(f"[batch_embeddings] [{i+1}] ID={sid}: {txt[:50]}...")

    # 构建纯数据结构（只传递 tuple，不传递 ORM 对象）
    texts_batch = list(zip(scenario_ids, texts))

    embedder = SyncSemanticEmbedder(cache_enabled=cache_enabled)
    logger.info(f"[batch_embeddings] SyncSemanticEmbedder 创建完成")

    # 🌟 核心点：将耗时的同步网络请求扔进线程池
    logger.info(f"[batch_embeddings] 启动 to_thread 调用 fetch_embeddings_sync...")
    vectors_map = await asyncio.to_thread(
        fetch_embeddings_sync,
        texts_batch,
        embedder,
        chunk_size,
        rate_limit_delay,
    )
    logger.info(f"[batch_embeddings] to_thread 返回，生成 {len(vectors_map)} 个向量")

    # 统计结果
    success = sum(1 for v in vectors_map.values() if v is not None)
    failed = sum(1 for v in vectors_map.values() if v is None)
    logger.info(f"[batch_embeddings] 结果统计: 成功={success}, 失败={failed}")
    
    return vectors_map
