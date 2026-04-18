"""
NumpyVectorStore — VectorStore 的 MVP 实现

特性：
- 纯 numpy，无需额外安装向量数据库
- 内存存储，服务启动时从 DB 加载（见 session_planner.py 的 _load_vector_store()）
- 支持两种向量来源：
    1. Topic.embedding（真实 embedding，由 embed_topics 脚本生成后写入 DB）
    2. BOW 向量（vocab_tags + sentence_patterns 的词袋向量，无需外部模型，自动降级）
- 升级路径：接口与 ChromaDB/pgvector 完全一致，只需换 import
"""

from __future__ import annotations

import logging
import math
from collections import Counter

import numpy as np

from .base import VectorStore

logger = logging.getLogger("EnglishCoach")


class NumpyVectorStore(VectorStore):
    """
    基于 numpy 的内存向量库，cosine similarity KNN 检索。

    内部存储：
        _ids:     list[int]          — topic_id 列表（与 _matrix 行对应）
        _matrix:  np.ndarray | None  — shape (N, D) 的 float32 矩阵
    """

    def __init__(self):
        self._ids: list[int] = []
        self._vectors: list[list[float]] = []
        self._matrix: np.ndarray | None = None  # 懒构建，add 后置 None 触发重建

    # ── 写入 ─────────────────────────────────────────────────────────────
    def add(self, topic_id: int, vector: list[float]) -> None:
        if topic_id in self._ids:
            idx = self._ids.index(topic_id)
            self._vectors[idx] = vector
        else:
            self._ids.append(topic_id)
            self._vectors.append(vector)
        self._matrix = None  # 标记矩阵需要重建

    def clear(self) -> None:
        self._ids.clear()
        self._vectors.clear()
        self._matrix = None

    def has_topic(self, topic_id: int) -> bool:
        return topic_id in self._ids

    def get_vector(self, topic_id: int) -> list[float] | None:
        """公开 API：按 topic_id 取向量，避免直接访问内部 _ids/_vectors。"""
        if topic_id not in self._ids:
            return None
        idx = self._ids.index(topic_id)
        return self._vectors[idx]

    # ── 检索 ─────────────────────────────────────────────────────────────
    def get_similar(self, query_vector: list[float], top_k: int = 5) -> list[tuple[int, float]]:
        if not self._ids:
            return []

        self._ensure_matrix()
        q = np.array(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm < 1e-9:
            return [(tid, 0.0) for tid in self._ids[:top_k]]

        q_unit = q / q_norm
        # 矩阵已行归一化，点积即余弦相似度
        scores = self._matrix @ q_unit  # shape (N,)
        k = min(top_k, len(self._ids))
        top_indices = np.argpartition(scores, -k)[-k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]
        return [(self._ids[i], float(scores[i])) for i in top_indices]

    def _ensure_matrix(self) -> None:
        if self._matrix is not None:
            return
        mat = np.array(self._vectors, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms < 1e-9, 1.0, norms)
        self._matrix = mat / norms


# ── BOW 向量生成器（无模型降级方案） ─────────────────────────────────────────

def build_bow_vector(vocab_tags: list[str], sentence_patterns: list[str], vocab_index: dict[str, int]) -> list[float]:
    """
    从话题的 vocab_tags + sentence_patterns 构建词袋向量。

    vocab_index: {word: dim_index}，全局词表（由 build_global_vocab() 构建）
    句型按词拆分后权重加 1.5（句型比单词更具区分度）。
    """
    dim = len(vocab_index)
    vec = [0.0] * dim

    for tag in (vocab_tags or []):
        token = tag.lower().strip()
        if token in vocab_index:
            vec[vocab_index[token]] += 1.0

    for pattern in (sentence_patterns or []):
        for word in pattern.lower().split():
            token = word.strip(".,!?")
            if token in vocab_index:
                vec[vocab_index[token]] += 1.5

    return vec


def build_global_vocab(topics_data: list[dict]) -> dict[str, int]:
    """
    从所有话题的 vocab_tags + sentence_patterns 构建全局词表。

    topics_data: [{"vocab_tags": [...], "sentence_patterns": [...]}, ...]
    返回 {word: index} 字典
    """
    counter: Counter = Counter()
    for t in topics_data:
        for tag in (t.get("vocab_tags") or []):
            counter[tag.lower().strip()] += 1
        for pat in (t.get("sentence_patterns") or []):
            for word in pat.lower().split():
                counter[word.strip(".,!?")] += 1

    vocab = sorted(counter.keys())
    return {word: i for i, word in enumerate(vocab)}


def embed_topics_bow(topics_data: list[dict]) -> dict[int, list[float]]:
    """
    批量为所有话题生成 BOW 向量。

    topics_data: [{"id": int, "vocab_tags": [...], "sentence_patterns": [...]}, ...]
    返回 {topic_id: vector}
    """
    vocab_index = build_global_vocab(topics_data)
    if not vocab_index:
        logger.warning("⚠️ VectorStore: 全局词表为空，无法生成 BOW 向量。")
        return {}

    result = {}
    for t in topics_data:
        vec = build_bow_vector(
            t.get("vocab_tags") or [],
            t.get("sentence_patterns") or [],
            vocab_index
        )
        result[t["id"]] = vec

    logger.info(f"✅ VectorStore: 已为 {len(result)} 个话题生成 BOW 向量（词表维度={len(vocab_index)}）")
    return result
