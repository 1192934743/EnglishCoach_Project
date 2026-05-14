"""
infrastructure.graph — 图谱构建基础设施模块

包含:
- depth_config: 难度层级配置
- prompt_builder: Prompt 模板构建 + 批量 embedding (DTO 模式)
- graph_config: 图谱构建阈值配置
"""

from .depth_config import DEPTH_DEFINITIONS, DEPTH_META
from .prompt_builder import (
    parse_json_response,
    build_rag_context,
    build_scenario_prompt,
    fetch_embeddings_sync,
    batch_embeddings,
)
from .graph_config import (
    VERTICAL_SIM_THRESHOLD,
    MAX_VERTICAL_EDGES_PER_NODE,
    HORIZONTAL_SIM_MIN,
    HORIZONTAL_SIM_MAX,
    CROSS_TOPIC_SIM_THRESHOLD,
    MAX_CROSS_TOPIC_EDGES_PER_NODE,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_RATE_LIMIT_DELAY,
)

__all__ = [
    # depth_config
    "DEPTH_DEFINITIONS",
    "DEPTH_META",
    # graph_config
    "VERTICAL_SIM_THRESHOLD",
    "MAX_VERTICAL_EDGES_PER_NODE",
    "HORIZONTAL_SIM_MIN",
    "HORIZONTAL_SIM_MAX",
    "CROSS_TOPIC_SIM_THRESHOLD",
    "MAX_CROSS_TOPIC_EDGES_PER_NODE",
    "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_RATE_LIMIT_DELAY",
    # prompt_builder
    "parse_json_response",
    "build_rag_context",
    "build_scenario_prompt",
    "fetch_embeddings_sync",
    "batch_embeddings",
]
