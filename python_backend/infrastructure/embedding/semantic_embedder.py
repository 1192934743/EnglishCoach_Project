"""
semantic_embedder.py — 语义向量化模块

⚠️  硬绑定警告 (ARCHITECTURE CONSTRAINT)
─────────────────────────────────────────────────────────────────────────────
当前仅支持 Gemini embedding-001 (768维)。
向量空间是模型强绑定的，切换模型将导致 DB 中的旧向量完全失效。
若需更换模型：
  1. 修改 EMBEDDING_MODEL 常量
  2. 执行 scripts/reembed_all.py（全量重新生成）
  3. 验证迁移前后相似度分布无显著漂移
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import json
import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

from dotenv import load_dotenv

# Gemini embedding 配置
try:
    from google import genai as google_genai
    _HAS_NEW_GEMINI = True
except ImportError:
    import google.generativeai as google_genai
    _HAS_NEW_GEMINI = False

# ─────────────────────────────────────────────────────────────────────────────
# ⚠️  模型硬绑定常量 (禁止修改)
# ─────────────────────────────────────────────────────────────────────────────
EMBEDDING_MODEL = "models/gemini-embedding-001"   # 禁止热切换（正确模型名）
EMBEDDING_DIMENSION = 768                    # Gemini embedding-001 输出维度（768维）
EMBEDDING_TASK_TYPE = "RETRIEVAL_DOCUMENT"

# ─────────────────────────────────────────────────────────────────────────────

_config_loaded = False


def _load_config() -> None:
    """确保环境变量已加载（惰性加载）"""
    global _config_loaded
    if not _config_loaded:
        # 计算 config.env 路径：../.. 跳出 infrastructure/embedding/ 到达 python_backend/
        _current_dir = os.path.dirname(os.path.abspath(__file__))
        _backend_dir = os.path.dirname(os.path.dirname(_current_dir))
        _config_path = os.path.join(_backend_dir, "config.env")
        load_dotenv(_config_path, override=True)

        # 获取 API Key 并配置 Gemini
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            # 同时设置两个环境变量（兼容性）
            os.environ.setdefault("GOOGLE_API_KEY", gemini_key)
            google_genai.configure(api_key=gemini_key)

        # 代理配置
        proxy_url = os.getenv("HTTP_PROXY")
        if proxy_url:
            os.environ["http_proxy"] = proxy_url
            os.environ["https_proxy"] = proxy_url

        _config_loaded = True


# ─────────────────────────────────────────────────────────────────────────────

class SemanticEmbedder:
    """
    语义向量化器（Gemini embedding-001）。

    Usage:
        embedder = SemanticEmbedder()
        vector = await embedder.embed("Hello world")
    """

    def __init__(self, cache_enabled: bool = False):
        """
        Args:
            cache_enabled: 开发模式启用本地缓存，避免重复请求 API。
                          生产环境建议 False，避免本地缓存过期导致数据不一致。
        """
        self.cache_enabled = cache_enabled

    async def embed(self, text: str) -> list[float]:
        """
        将文本转为 768 维向量。

        Args:
            text: 输入文本

        Returns:
            768 维浮点向量列表
        """
        if self.cache_enabled:
            cached = self._load_from_cache(text)
            if cached is not None:
                return cached

        result = await asyncio.to_thread(self._call_embedding, text)

        if self.cache_enabled:
            self._save_to_cache(text, result)

        return result

    def _call_embedding(self, text: str) -> list[float]:
        """
        同步调用 Gemini embedding API。

        Args:
            text: 输入文本

        Returns:
            向量列表
        """
        # 确保环境变量已加载
        _load_config()

        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY 未配置，无法生成 embedding")

        if _HAS_NEW_GEMINI:
            response = google_genai.client.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type=EMBEDDING_TASK_TYPE,
            )
            return response.values[0]
        else:
            response = google_genai.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type=EMBEDDING_TASK_TYPE,
            )
            return response["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        批量 embedding（串行调用）。

        Args:
            texts: 文本列表

        Returns:
            向量列表
        """
        results = []
        for text in texts:
            vector = await self.embed(text)
            results.append(vector)
        return results

    def _get_cache_path(self, text: str) -> str:
        """根据文本内容生成缓存文件路径"""
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        cache_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "data", "embeddings"
        )
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{key}.json")

    def _load_from_cache(self, text: str) -> Optional[list[float]]:
        """从缓存加载向量"""
        if not self.cache_enabled:
            return None
        path = self._get_cache_path(text)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def _save_to_cache(self, text: str, vector: list[float]) -> None:
        """保存向量到缓存"""
        if not self.cache_enabled:
            return
        path = self._get_cache_path(text)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(vector, f)
        except Exception:
            pass


def _run_in_thread(func):
    """在新线程中运行（避免嵌套事件循环）"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func)
        return future.result()


class SyncSemanticEmbedder:
    """
    SemanticEmbedder 的同步包装器。

    build_scenario_graph.py 是同步脚本，使用此包装器可兼容。
    """

    def __init__(self, cache_enabled: bool = False):
        self._async = SemanticEmbedder(cache_enabled=cache_enabled)

    def embed(self, text: str) -> list[float]:
        """同步 embedding"""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self._async.embed(text))
            finally:
                loop.close()
        return _run_in_thread(_run)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """同步批量 embedding"""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self._async.embed_batch(texts))
            finally:
                loop.close()
        return _run_in_thread(_run)


class ScenarioVectorIndex:
    """
    场景向量索引（内存计算，非持久化）。

    在 build_scenario_graph.py 启动时从 DB 加载所有向量，
    内部转为 Numpy 矩阵，使用 numpy.dot 批量计算余弦相似度。
    """

    def __init__(self, mappings: dict[int, list[float]]):
        """
        Args:
            mappings: {scenario_id: embedding_vector} 映射
        """
        self._ids = list(mappings.keys())
        self._vectors = [mappings[sid] for sid in self._ids]
        self._matrix = np.array(self._vectors, dtype=np.float32)

        # 行归一化：点积即余弦相似度
        norms = np.linalg.norm(self._matrix, axis=1, keepdims=True)
        norms = np.where(norms < 1e-9, 1.0, norms)
        self._normalized = self._matrix / norms

    @property
    def scenario_ids(self) -> list[int]:
        """返回所有场景 ID"""
        return self._ids

    def compute_similarities(
        self, from_ids: list[int], to_ids: list[int]
    ) -> np.ndarray:
        """
        批量计算两组场景之间的余弦相似度矩阵。

        Args:
            from_ids: 源场景 ID 列表
            to_ids: 目标场景 ID 列表

        Returns:
            np.ndarray shape: (len(from_ids), len(to_ids))
            matrix[i, j] = similarity(from_ids[i], to_ids[j])
        """
        from_indices = [self._ids.index(fid) for fid in from_ids]
        to_indices = [self._ids.index(tid) for tid in to_ids]

        from_mat = self._normalized[from_indices]      # (N_from, D)
        to_mat = self._normalized[to_indices].T       # (D, N_to)

        return from_mat @ to_mat                       # (N_from, N_to)

    def compute_all_pairwise(self) -> np.ndarray:
        """
        计算所有场景之间的余弦相似度矩阵。

        Returns:
            np.ndarray shape: (N, N)
        """
        return self._normalized @ self._normalized.T


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    计算两个向量的余弦相似度。

    Args:
        a: 向量 A
        b: 向量 B

    Returns:
        余弦相似度 ∈ [-1, 1]
    """
    vec_a = np.array(a, dtype=np.float32)
    vec_b = np.array(b, dtype=np.float32)

    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)

    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0

    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))
