"""
graph_config.py — 图谱引擎配置

集中管理所有图谱构建相关的阈值配置。

版本: v1.0 (2026-05-13)
"""

# ============================================================
# 图谱构建阈值
# ============================================================

# 垂直连线阈值（同一话题内的深度递进）
VERTICAL_SIM_THRESHOLD: float = 0.65
"""垂直连线最低相似度（防串线）"""

MAX_VERTICAL_EDGES_PER_NODE: int = 2
"""每个节点最多垂直出边数"""

# 横向迁移阈值（同一话题同一深度的平行场景）
HORIZONTAL_SIM_MIN: float = 0.85
"""横向迁移最低相似度"""

HORIZONTAL_SIM_MAX: float = 0.92
"""横向迁移最高相似度（避免过相似）"""

# 跨话题虫洞阈值
CROSS_TOPIC_SIM_THRESHOLD: float = 0.85
"""跨话题相似度阈值"""

MAX_CROSS_TOPIC_EDGES_PER_NODE: int = 3
"""每个入口节点最多跨话题边数"""

# ============================================================
# Embedding 配置
# ============================================================

EMBEDDING_BATCH_SIZE: int = 10
"""批量 embedding 每批处理数量"""

EMBEDDING_RATE_LIMIT_DELAY: float = 1.0
"""批量 embedding 批次间的延迟（秒）"""

# ============================================================
# 导出所有配置
# ============================================================

__all__ = [
    # 图谱构建
    "VERTICAL_SIM_THRESHOLD",
    "MAX_VERTICAL_EDGES_PER_NODE",
    "HORIZONTAL_SIM_MIN",
    "HORIZONTAL_SIM_MAX",
    "CROSS_TOPIC_SIM_THRESHOLD",
    "MAX_CROSS_TOPIC_EDGES_PER_NODE",
    # Embedding
    "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_RATE_LIMIT_DELAY",
]
