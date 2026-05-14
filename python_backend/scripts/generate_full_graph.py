"""
generate_full_graph.py — 图谱一键生成主调度器

实现"一键生成完整图谱"的终极目标。用户只需执行一条命令：

    python scripts/generate_full_graph.py --all

调度器会自动：
1. 动态查询数据库中的所有话题（不硬编码 ID）
2. 依次为每个话题 × 难度等级生成微场景
3. 自动限流，防止 API 限流
4. 实时显示进度
5. 生成后自动构建图谱边
6. 最后构建跨话题虫洞

Usage:
    # 一键生成所有话题的所有图谱
    python scripts/generate_full_graph.py --all

    # 增量模式：只为缺失场景的话题生成
    python scripts/generate_full_graph.py --missing-only

    # 预览模式（不实际调用 API）
    python scripts/generate_full_graph.py --all --dry-run

    # 强制重建
    python scripts/generate_full_graph.py --all --rebuild

    # 只生成节点（不构建边）
    python scripts/generate_full_graph.py --all --skip-edges

    # 指定话题范围
    python scripts/generate_full_graph.py --from-topic 1 --to-topic 5

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     generate_full_graph.py 调度流程                      │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │  1. 初始化（动态感知话题）                                               │
    │     └── 从 DB 查询: SELECT id, title FROM topics ORDER BY id            │
    │                                                                          │
    │  2. 节点生成阶段（Python 循环，动态感知话题）                             │
    │     └── for topic in topics:                                            │
    │         └── for depth in [1, 2, 3]:                                     │
    │             ├── 检查是否已存在（幂等）                                    │
    │             ├── [主动限流] RateLimiter 控制并发                           │
    │             ├── 调用 graph_generator 逻辑                                 │
    │             └── 进度日志：已完成 N/M                                      │
    │                                                                          │
    │  3. 边构建阶段（全局视角，一次性完成）                                   │
    │     └── build_scenario_graph.py --all --build-cross-topic                 │
    │                                                                          │
    │  4. 健康检查                                                             │
    │     └── 孤立节点检测 + 统计报告                                          │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘
"""

import os
import sys
import json
import asyncio
import argparse
import logging
import time
import signal
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

# 确保脚本可以从任何目录运行
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_script_dir)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(_backend_dir, "config.env"))

from sqlalchemy.orm import Session

from database import SessionLocal, Topic, MicroScenario, ScenarioConstraint, ScenarioTransition
from infrastructure.graph import DEPTH_DEFINITIONS, DEPTH_META
from enums import EdgeType
from scripts.graph_generator import (
    generate_and_save,
    TopicDTO,
    build_scenario_prompt,
    translate_prompt_summary,
    retrieve_existing_scenarios,
    get_other_depths_step_orders,
)


# ── 日志配置 ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("FullGraphGenerator")

# ── LLM 评审配置 ───────────────────────────────────────────────────────────────

import google.generativeai as genai

_api_key = os.getenv("GEMINI_API_KEY")
if _api_key:
    genai.configure(api_key=_api_key)
    review_model = genai.GenerativeModel("gemini-2.5-flash")
else:
    review_model = None
    logger.warning("[CONFIG] GEMINI_API_KEY not found, --llm-review will be disabled")


def _build_llm_review_prompt(
    prompt: str,
    depth_config_content: str,
    instruction_code: str,
) -> str:
    """
    构建 LLM 评审 Prompt。

    传递源头上下文，让 LLM 能精准指出在哪个文件哪一行修改。
    """
    return f"""你是一个专业的微场景 Prompt 工程评审员。

## 你的任务
分析以下微场景生成 Prompt，从"源头可优化性"角度给出评审意见。

## 评审维度
1. **指令清晰度**：Prompt 中的指令是否无歧义、可执行
2. **约束一致性**：难度定义、场景数量、约束数量是否自洽
3. **示例质量**：Depth 定义中的示例是否足够清晰
4. **RAG 上下文有效性**：已有场景的格式是否便于 LLM 理解
5. **输出格式完整性**：JSON Schema 是否包含所有必要字段

## 源头配置文件内容（评审时参考）
### DEPTH_DEFINITIONS (infrastructure/graph/depth_config.py):
```
{depth_config_content}
```

### 生成指令代码片段 (scripts/graph_generator.py ~line 148-157):
```python
{instruction_code}
```

## 当前 Prompt（待评审）
```
{prompt}
```

## 输出格式（严格按此格式输出）
```
【Prompt 摘要】
- 话题: <话题名称>
- 难度: Depth <N> (<描述>)
- 已有场景数: <N> 个
- 将生成: <N> 个场景，每个场景 <M>-<K> 个约束

【问题列表】
1. [<严重程度>] <问题描述>
   → 建议: <优化建议>

【源头优化建议】
1. [depth_config.py:<行号>] <具体修改建议>
2. [graph_generator.py:<行号>] <具体修改建议>
```
"""


async def review_prompt_with_llm(prompt: str) -> str:
    """
    使用 LLM 评审 Prompt。

    Args:
        prompt: 待评审的英文 Prompt

    Returns:
        LLM 评审结果（中英文混合）
    """
    if review_model is None:
        return "[LLM REVIEW] 跳过（未配置 GEMINI_API_KEY）"

    try:
        # 读取源头配置文件内容（用于给 LLM 参考）
        depth_config_path = os.path.join(_backend_dir, "infrastructure", "graph", "depth_config.py")
        instruction_code = '''context_str, instruction_str = build_rag_context(existing_scenarios)
if not existing_scenarios:
    return (
        "(No existing scenarios, feel free to generate 3-5 parallel scenarios)",
        "Please generate 3-5 parallel functional migration scenarios."
    )
context_parts = [...]
instruction = (
    "Please generate 2-3 new parallel scenarios.\\n"
    "- Similar communicative function\\n"
    "- No vocabulary overlap with existing scenarios"
)'''

        depth_config_content = ""
        if os.path.exists(depth_config_path):
            with open(depth_config_path, "r", encoding="utf-8") as f:
                depth_config_content = f.read()

        review_prompt = _build_llm_review_prompt(
            prompt=prompt,
            depth_config_content=depth_config_content[:2000],
            instruction_code=instruction_code,
        )

        # 调用 LLM 评审（同步调用，用 to_thread 包装）
        response = await asyncio.to_thread(
            review_model.generate_content,
            review_prompt,
        )

        return response.text.strip()

    except Exception as e:
        return f"[LLM REVIEW] 评审失败: {e}"

# ── 限流器配置 ────────────────────────────────────────────────────────────────

# Gemini API 限流建议值（根据实际 API 调整）
DEFAULT_RPM = 60          # 每分钟请求数上限
DEFAULT_TPM = 1_000_000  # 每分钟 Token 数上限（Gemini 2.5 Pro）

# 推荐配置：保守一些，避免触发 429
RECOMMENDED_CONCURRENT = 3       # 同时最多 3 个请求
RECOMMENDED_MIN_INTERVAL = 1.5   # 最小请求间隔（秒）


@dataclass
class RateLimiter:
    """
    主动限流器：控制 API 并发请求速率。

    提供两种限流策略：
    1. Semaphore（信号量）：控制最大并发数
    2. 固定间隔：保证请求间隔均匀

    推荐组合使用：Semaphore(3) + sleep(1.5)
    """
    max_concurrent: int = RECOMMENDED_CONCURRENT
    min_interval: float = RECOMMENDED_MIN_INTERVAL
    _semaphore: asyncio.Semaphore = field(default=None, init=False)
    _last_request_time: float = field(default=0.0, init=False)

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def acquire(self):
        """获取限流令牌（异步）"""
        await self._semaphore.acquire()

        # 检查时间间隔
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)

        self._last_request_time = time.time()

    def release(self):
        """释放限流令牌"""
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


# ── 进度跟踪器 ────────────────────────────────────────────────────────────────

@dataclass
class ProgressTracker:
    """
    进度跟踪器：实时显示生成进度。

    显示信息：
    - 总任务数 / 已完成数（如 `3/11 topics`）
    - 当前正在处理的话题名称
    - 预估剩余时间
    """
    total_tasks: int = 0
    completed_tasks: int = 0
    start_time: float = field(default_factory=time.time)
    task_names: list = field(default_factory=list)
    failed_tasks: list = field(default_factory=list)

    def add_task(self, name: str):
        """添加一个任务"""
        self.task_names.append(name)
        self.total_tasks = len(self.task_names)

    def mark_complete(self, name: str, success: bool = True):
        """标记任务完成"""
        self.completed_tasks += 1
        if not success:
            self.failed_tasks.append(name)
        self._print_progress(name)

    def _print_progress(self, current_task: str):
        """打印进度信息"""
        elapsed = time.time() - self.start_time
        rate = self.completed_tasks / elapsed if elapsed > 0 else 0
        remaining = (self.total_tasks - self.completed_tasks) / rate if rate > 0 else 0

        percent = (self.completed_tasks / self.total_tasks * 100) if self.total_tasks > 0 else 0
        eta = f"{int(remaining)}s" if remaining < 60 else f"{int(remaining / 60)}m {int(remaining % 60)}s"

        bar_width = 30
        filled = int(bar_width * self.completed_tasks / self.total_tasks) if self.total_tasks > 0 else 0
        bar = "=" * filled + "-" * (bar_width - filled)

        logger.info(
            f"[PROGRESS] [{bar}] {self.completed_tasks}/{self.total_tasks} ({percent:.1f}%) | "
            f"ETA: {eta} | Current: {current_task[:40]}"
        )

    def print_summary(self):
        """打印最终汇总"""
        elapsed = time.time() - self.start_time
        success = self.total_tasks - len(self.failed_tasks)

        print("\n" + "=" * 70)
        print("                      [Summary] Graph Generation Report")
        print("=" * 70)
        print(f"  Total tasks:    {self.total_tasks}")
        print(f"  Success:         {success}")
        print(f"  Failed:          {len(self.failed_tasks)}")
        print(f"  Total time:      {elapsed:.1f}s ({elapsed / 60:.1f}m)")
        print(f"  Avg time/task:   {elapsed / self.total_tasks:.1f}s" if self.total_tasks > 0 else "")

        if self.failed_tasks:
            print("\n  Failed tasks:")
            for task in self.failed_tasks:
                print(f"    - {task}")

        print("=" * 70 + "\n")


# ── 核心调度逻辑 ──────────────────────────────────────────────────────────────

class FullGraphGenerator:
    """
    图谱一键生成调度器。

    职责：
    1. 动态查询数据库中的所有话题
    2. 依次为每个话题 × 难度等级生成微场景
    3. 自动限流，防止 API 限流
    4. 实时显示进度
    5. 生成后自动构建图谱边
    6. 最后构建跨话题虫洞
    """

    def __init__(
        self,
        db: Session,
        dry_run: bool = False,
        rebuild: bool = False,
        skip_edges: bool = False,
        missing_only: bool = False,
        from_topic: Optional[int] = None,
        to_topic: Optional[int] = None,
        rate_limiter: Optional[RateLimiter] = None,
        progress: Optional[ProgressTracker] = None,
        show_prompt: bool = False,
        llm_review: bool = False,
    ):
        self.db = db
        self.dry_run = dry_run
        self.rebuild = rebuild
        self.skip_edges = skip_edges
        self.missing_only = missing_only
        self.from_topic = from_topic
        self.to_topic = to_topic
        self.rate_limiter = rate_limiter or RateLimiter()
        self.progress = progress or ProgressTracker()
        self.show_prompt = show_prompt
        self.llm_review = llm_review
        self._canceled = False

        # 注册 Ctrl+C 处理器
        signal.signal(signal.SIGINT, self._handle_interrupt)

    def _handle_interrupt(self, signum, frame):
        """处理 Ctrl+C 中断"""
        logger.warning("[INTERRUPT] Received interrupt signal, saving progress...")
        self._canceled = True

    def get_topics(self) -> list[Topic]:
        """从数据库动态查询所有话题（不硬编码 ID）"""
        query = self.db.query(Topic).order_by(Topic.id)

        if self.from_topic:
            query = query.filter(Topic.id >= self.from_topic)
        if self.to_topic:
            query = query.filter(Topic.id <= self.to_topic)

        topics = query.all()
        logger.info(f"[DB] 查询到 {len(topics)} 个话题")

        for t in topics:
            title_zh = getattr(t, 'title_zh', None) or ""
            logger.info(f"  [{t.id}] {t.title} {title_zh}")

        return topics

    def has_existing_scenarios(self, topic_id: int, depth: int) -> bool:
        """检查话题 × 难度是否已有场景"""
        count = self.db.query(MicroScenario).filter(
            MicroScenario.topic_id == topic_id,
            MicroScenario.depth_level == depth,
        ).count()
        return count > 0

    def get_scenario_stats(self) -> dict:
        """获取图谱统计信息"""
        total_scenarios = self.db.query(MicroScenario).count()
        total_constraints = self.db.query(ScenarioConstraint).count()
        total_edges = self.db.query(ScenarioTransition).count()
        topics_with_scenarios = (
            self.db.query(MicroScenario.topic_id)
            .distinct()
            .count()
        )

        return {
            "total_scenarios": total_scenarios,
            "total_constraints": total_constraints,
            "total_edges": total_edges,
            "topics_with_scenarios": topics_with_scenarios,
        }

    async def generate_topic_scenarios(self, topic: Topic, depth: int) -> bool:
        """
        为单个话题 × 难度生成场景（串行调用，内部使用公共接口）。

        注意：推荐使用 run() 方法中的并发执行，性能更好。

        Returns:
            True=成功，False=失败
        """
        task_name = f"{topic.title} (Depth {depth})"

        if self.missing_only and self.has_existing_scenarios(topic.id, depth):
            logger.info(f"[SKIP] Missing-only: {task_name} already exists")
            self.progress.mark_complete(task_name, success=True)
            return True

        if self.dry_run:
            # dry-run 模式：构建并展示 Prompt（不调用 API）
            await self._dry_run_with_prompt(topic, depth)
            self.progress.mark_complete(task_name, success=True)
            return True

        try:
            async with self.rate_limiter:
                logger.info(f"[START] Generating: {task_name}")

                # 转换为 TopicDTO 避免跨 Session 访问 ORM 对象
                topic_dto = TopicDTO.from_orm(topic)
                ids = await generate_and_save(
                    topic=topic_dto,
                    depth=depth,
                    db=self.db,
                    rebuild=self.rebuild,
                )
                success = len(ids) > 0

                if success:
                    logger.info(f"[OK] Completed: {task_name}")
                else:
                    logger.error(f"[FAIL] Failed: {task_name}")

                self.progress.mark_complete(task_name, success=success)
                return success

        except Exception as e:
            logger.error(f"[ERROR] Exception: {task_name} - {e}")
            self.db.rollback()
            self.progress.mark_complete(task_name, success=False)
            return False

    async def _dry_run_with_prompt(self, topic: Topic, depth: int):
        """
        dry-run 模式：构建并展示 Prompt（可选 LLM 评审）。

        在 dry-run 模式下：
        1. 打印任务摘要（中文）
        2. 打印完整英文 Prompt（--show-prompt）
        3. 调用 LLM 评审（--llm-review）
        """
        topic_dto = TopicDTO.from_orm(topic)
        existing = retrieve_existing_scenarios(self.db, topic.id, depth)
        other_depths_info = get_other_depths_step_orders(self.db, topic.id, depth)
        prompt = build_scenario_prompt(topic_dto, depth, existing, other_depths_info)
        meta = DEPTH_META[depth]

        # 打印中文摘要
        summary = translate_prompt_summary(
            prompt=prompt,
            topic_title=topic.title,
            depth=depth,
            existing_count=len(existing),
            meta=meta,
        )

        print("\n" + "=" * 70)
        print(f"[DRY RUN] {topic.title} — Depth {depth}")
        print("=" * 70)
        print("【任务摘要】")
        for line in summary.split('\n'):
            print(f"  {line}")

        # 打印完整 Prompt（--show-prompt）
        if self.show_prompt:
            print("\n【完整 Prompt（英文）】")
            print("-" * 70)
            print(prompt)
            print("-" * 70)

        # LLM 评审（--llm-review）
        if self.llm_review:
            print("\n【LLM 评审中...】")
            review_result = await review_prompt_with_llm(prompt)
            print(review_result)

        print()

    def build_edges_for_topic(self, topic_id: int, depth: int, dry_run: bool = False) -> int:
        """为单个话题 × 难度构建边"""
        # 复用 build_scenario_graph 的逻辑
        from scripts.build_scenario_graph import build_scenario_graph

        transitions = build_scenario_graph(
            self.db, topic_id, depth,
            dry_run=dry_run or self.dry_run,
            rebuild=self.rebuild,
            verbose=False,
        )

        return len(transitions)

    def build_all_edges(self, topics: list[Topic]) -> dict:
        """构建所有话题 × 难度的边"""
        if self.dry_run:
            logger.info("[DRY RUN] Skipping edge building")
            return {"vertical": 0, "horizontal": 0, "cross_topic": 0}

        logger.info("\n" + "=" * 60)
        logger.info("[EDGE] Starting edge building...")
        logger.info("=" * 60)

        total_edges = 0

        for topic in topics:
            for depth in [1, 2, 3]:
                if self.missing_only and not self.has_existing_scenarios(topic.id, depth):
                    continue

                try:
                    n = self.build_edges_for_topic(topic.id, depth)
                    total_edges += n
                except Exception as e:
                    logger.error(f"[ERROR] Edge building failed: {topic.title} (Depth {depth}): {e}")

        return {"total_edges": total_edges}

    def build_cross_topic_edges(self) -> int:
        """构建跨话题虫洞边"""
        if self.dry_run:
            logger.info("[DRY RUN] Skipping cross-topic edge building")
            return 0

        logger.info("\n" + "=" * 60)
        logger.info("[CROSS-TOPIC] Starting cross-topic wormhole edge building...")
        logger.info("=" * 60)

        from scripts.build_scenario_graph import build_cross_topic_graph

        try:
            edges = build_cross_topic_graph(
                self.db,
                dry_run=self.dry_run,
                rebuild=self.rebuild,
                verbose=False,
            )
            return len(edges)
        except Exception as e:
            logger.error(f"[ERROR] Cross-topic edge building failed: {e}")
            return 0

    def print_health_report(self):
        """打印图谱健康度报告"""
        stats = self.get_scenario_stats()

        print("\n" + "=" * 70)
        print("                      [Health] Graph Status Report")
        print("=" * 70)
        print(f"  Micro-scenarios:  {stats['total_scenarios']}")
        print(f"  Constraints:       {stats['total_constraints']}")
        print(f"  Transitions:      {stats['total_edges']}")
        print(f"  Topics covered:   {stats['topics_with_scenarios']}")

        # 检查孤立节点
        all_scenarios = self.db.query(MicroScenario).all()
        if all_scenarios:
            scenario_ids = [s.id for s in all_scenarios]
            connected_ids = set()

            edges = self.db.query(ScenarioTransition).filter(
                ScenarioTransition.from_scenario_id.in_(scenario_ids)
            ).all()

            for e in edges:
                connected_ids.add(e.from_scenario_id)
                connected_ids.add(e.to_scenario_id)

            isolated = len(scenario_ids) - len(connected_ids)
            if isolated > 0:
                print(f"  Isolated nodes:    {isolated} [!]")
            else:
                print(f"  Isolated nodes:    0 [OK]")

        print("=" * 70 + "\n")

    async def run(self):
        """执行完整图谱生成流程"""
        logger.info("=" * 60)
        logger.info("[START] Graph generator started")
        logger.info("=" * 60)

        # 1. 动态查询话题
        topics = self.get_topics()
        if not topics:
            logger.error("[ERROR] No topics found in database!")
            return

        # 2. 构建任务列表
        tasks = []
        for topic in topics:
            for depth in [1, 2, 3]:
                task_name = f"{topic.title} (Depth {depth})"
                self.progress.add_task(task_name)
                tasks.append((topic, depth))

        logger.info(f"[TASK] Task list: {len(tasks)} generation tasks")
        if self.missing_only:
            logger.info("   模式：增量（只生成缺失的场景）")
        if self.dry_run:
            logger.info("   模式：预览（不实际调用 API）")

        # 3. 节点生成阶段
        logger.info("\n" + "=" * 60)
        logger.info("[PHASE 1] Node generation")
        logger.info("=" * 60)

        start_time = time.time()

        if self.dry_run:
            # dry-run 模式：串行执行（需要逐个打印 Prompt）
            for topic, depth in tasks:
                await self.generate_topic_scenarios(topic, depth)
        else:
            # 正常模式：并发执行
            logger.info("[PHASE 1] Mode: concurrent")
            semaphore = asyncio.Semaphore(self.rate_limiter.max_concurrent)

            async def bounded_generate(topic: Topic, depth: int) -> bool:
                """带并发限制的生成任务"""
                async with semaphore:
                    await self.rate_limiter.acquire()
                    task_name = f"{topic.title} (Depth {depth})"
                    logger.info(f"[START] {task_name}")

                    with SessionLocal() as task_db:
                        try:
                            topic_dto = TopicDTO.from_orm(topic)
                            ids = await generate_and_save(
                                topic=topic_dto,
                                depth=depth,
                                db=task_db,
                                rebuild=self.rebuild,
                            )
                            success = len(ids) > 0
                            if success:
                                logger.info(f"[OK] {task_name}")
                            else:
                                logger.error(f"[FAIL] {task_name}")
                            self.progress.mark_complete(task_name, success=success)
                            return success
                        except Exception as e:
                            logger.error(f"[ERROR] {task_name}: {e}")
                            task_db.rollback()
                            self.progress.mark_complete(task_name, success=False)
                            return False
                        finally:
                            self.rate_limiter.release()  # 确保释放限流器

            results = await asyncio.gather(
                *[bounded_generate(t, d) for t, d in tasks],
                return_exceptions=True,
            )

            # 处理异常结果
            success_count = 0
            fail_count = 0
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"[ERROR] Task {i} failed with exception: {result}")
                    fail_count += 1
                elif result is True:
                    success_count += 1
                else:
                    fail_count += 1

            elapsed = time.time() - start_time
            logger.info(f"[STATS] Node generation done: {success_count} success, {fail_count} failed, took {elapsed:.1f}s")

        # 4. 边构建阶段
        if not self.skip_edges:
            logger.info("\n" + "=" * 60)
            logger.info("[PHASE 2] Edge building")
            logger.info("=" * 60)

            # 4.1 构建所有话题的边
            edge_stats = self.build_all_edges(topics)
            logger.info(f"[EDGE] Edge build stats: {edge_stats.get('total_edges', 0)} edges")

            # 4.2 构建跨话题虫洞
            cross_topic_count = self.build_cross_topic_edges()
            logger.info(f"[CROSS-TOPIC] Cross-topic edges: {cross_topic_count}")

        # 5. 健康报告
        self.print_health_report()

        # 6. 打印进度汇总
        self.progress.print_summary()

        logger.info("=" * 60)
        logger.info("[DONE] Graph generation complete!")
        logger.info("=" * 60)


# ── CLI 入口 ─────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="图谱一键生成器 — 动态生成完整微场景图谱",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 一键生成所有话题的所有图谱
  python scripts/generate_full_graph.py --all

  # 增量模式（只生成缺失的）
  python scripts/generate_full_graph.py --all --missing-only

  # 预览模式（不实际调用 API）
  python scripts/generate_full_graph.py --all --dry-run

  # 预览 + 打印中文摘要（不打印 Prompt）
  python scripts/generate_full_graph.py --all --dry-run

  # 预览 + 打印完整英文 Prompt
  python scripts/generate_full_graph.py --all --dry-run --show-prompt

  # 预览 + LLM 评审 Prompt
  python scripts/generate_full_graph.py --all --dry-run --llm-review

  # 预览 + 打印 Prompt + LLM 评审（完整预览模式）
  python scripts/generate_full_graph.py --all --dry-run --show-prompt --llm-review

  # 强制重建（先删除旧数据）
  python scripts/generate_full_graph.py --all --rebuild

  # 只生成节点，不构建边
  python scripts/generate_full_graph.py --all --skip-edges

  # 指定话题范围
  python scripts/generate_full_graph.py --from-topic 1 --to-topic 5
        """,
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="生成所有话题（必需，除非指定 --from-topic 和 --to-topic）",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="增量模式：只为缺失场景的话题生成",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式（不实际调用 API，不写入数据库）",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="强制重建（先删除该话题该层级的旧数据）",
    )
    parser.add_argument(
        "--skip-edges",
        action="store_true",
        help="跳过边构建（只生成节点）",
    )
    parser.add_argument(
        "--from-topic",
        type=int,
        help="指定起始话题 ID",
    )
    parser.add_argument(
        "--to-topic",
        type=int,
        help="指定结束话题 ID",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=RECOMMENDED_CONCURRENT,
        help=f"最大并发数（默认：{RECOMMENDED_CONCURRENT}）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=RECOMMENDED_MIN_INTERVAL,
        help=f"最小请求间隔秒数（默认：{RECOMMENDED_MIN_INTERVAL}）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出详细日志",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="dry-run 时打印完整英文 Prompt",
    )
    parser.add_argument(
        "--llm-review",
        action="store_true",
        help="dry-run 时调用 LLM 评审 Prompt（需要 GEMINI_API_KEY）",
    )

    args = parser.parse_args()

    # 验证参数
    if not args.all and (args.from_topic is None or args.to_topic is None):
        parser.error("请使用 --all 或同时指定 --from-topic 和 --to-topic")

    if args.llm_review and not os.getenv("GEMINI_API_KEY"):
        parser.error("--llm-review 需要配置 GEMINI_API_KEY 环境变量")

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 创建限流器和进度跟踪器
    rate_limiter = RateLimiter(
        max_concurrent=args.concurrent,
        min_interval=args.interval,
    )
    progress = ProgressTracker()

    # 执行
    db = SessionLocal()
    try:
        generator = FullGraphGenerator(
            db=db,
            dry_run=args.dry_run,
            rebuild=args.rebuild,
            skip_edges=args.skip_edges,
            missing_only=args.missing_only,
            from_topic=args.from_topic,
            to_topic=args.to_topic,
            rate_limiter=rate_limiter,
            progress=progress,
            show_prompt=args.show_prompt,
            llm_review=args.llm_review,
        )
        await generator.run()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
