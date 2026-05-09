"""
infrastructure.embedding — 语义向量化模块

导出主要类和常量供外部使用。
"""

from .semantic_embedder import (
    SemanticEmbedder,
    SyncSemanticEmbedder,
    ScenarioVectorIndex,
    cosine_similarity,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSION,
)

__all__ = [
    "SemanticEmbedder",
    "SyncSemanticEmbedder",
    "ScenarioVectorIndex",
    "cosine_similarity",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
]
