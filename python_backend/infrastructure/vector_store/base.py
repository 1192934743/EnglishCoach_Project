"""
VectorStore 抽象接口

设计目标：
- MVP 阶段：NumpyVectorStore（零额外依赖，内存存储，< 200 个话题够用）
- 成长期：替换为 ChromaDB（pip install chromadb，本地文件型）
- 规模化：替换为 pgvector（迁移到 PostgreSQL 时一劳永逸）

切换时只需换掉 session_planner.py 中的 import，接口不变。
"""

from abc import ABC, abstractmethod


class VectorStore(ABC):

    @abstractmethod
    def add(self, topic_id: int, vector: list[float]) -> None:
        """将话题 ID 与其特征向量注册到向量库"""

    @abstractmethod
    def get_similar(self, query_vector: list[float], top_k: int = 5) -> list[tuple[int, float]]:
        """
        返回与 query_vector 最相似的 top_k 个话题。
        返回格式：[(topic_id, similarity_score), ...]，按相似度降序排列
        """

    @abstractmethod
    def has_topic(self, topic_id: int) -> bool:
        """检查话题 ID 是否已在向量库中"""

    @abstractmethod
    def clear(self) -> None:
        """清空向量库（重建时使用）"""
