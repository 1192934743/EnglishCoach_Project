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

from sqlalchemy.orm import Session

from database import SessionLocal, Topic, MicroScenario, ScenarioConstraint, ScenarioTransition
from infrastructure.embedding import SyncSemanticEmbedder
from enums import EdgeType

# ── 导入子模块的函数和常量 ─────────────────────────────────────────────────────
# 注意：由于 graph_generator.py 使用 asyncio，这里需要导入其核心逻辑
# 但为了保持独立性，我们直接复用其 DEPTH_META 和相关常量

# 延迟导入避免循环依赖
_GRAPH_GENERATOR_IMPORTED = False


def _import_graph_generator():
    """延迟导入 graph_generator 的依赖项"""
    global _GRAPH_GENERATOR_IMPORTED
    if _GRAPH_GENERATOR_IMPORTED:
        return

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "graph_generator_module",
        os.path.join(_backend_dir, "scripts", "graph_generator.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["graph_generator_module"] = module

    # 手动设置需要的常量（避免执行整个模块的 main）
    module.DEPTH_META = {
        1: {"desc": "Foundation", "keyword": "core survival phrases", "constraint_range": (2, 3)},
        2: {"desc": "Intermediate", "keyword": "politeness and detail modifiers", "constraint_range": (3, 4)},
        3: {"desc": "Advanced", "keyword": "native expressions and complex sentences", "constraint_range": (4, 5)},
    }

    _GRAPH_GENERATOR_IMPORTED = True


# ── 日志配置 ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("FullGraphGenerator")

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
        为单个话题 × 难度生成场景。

        Returns:
            True=成功，False=失败
        """
        task_name = f"{topic.title} (Depth {depth})"

        if self.missing_only and self.has_existing_scenarios(topic.id, depth):
            logger.info(f"[SKIP] Missing-only: {task_name} already exists")
            self.progress.mark_complete(task_name, success=True)
            return True

        if self.dry_run:
            logger.info(f"[DRY RUN] Would generate: {task_name}")
            self.progress.mark_complete(task_name, success=True)
            return True

        try:
            # 限流获取
            async with self.rate_limiter:
                logger.info(f"[START] Generating: {task_name}")

                # 调用生成逻辑（复用 graph_generator 的核心代码）
                success = await self._call_generator(topic, depth)

                if success:
                    logger.info(f"[OK] Completed: {task_name}")
                else:
                    logger.error(f"[FAIL] Failed: {task_name}")

                self.progress.mark_complete(task_name, success=success)
                return success

        except Exception as e:
            logger.error(f"[ERROR] Exception: {task_name} - {e}")
            self.progress.mark_complete(task_name, success=False)
            return False

    async def _call_generator(self, topic: Topic, depth: int) -> bool:
        """调用实际的生成器（封装 graph_generator 的核心逻辑）"""
        _import_graph_generator()

        # 延迟导入 Gemini
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_backend_dir, "config.env"))

        import google.generativeai as genai

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.error("未配置 GEMINI_API_KEY")
            return False

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            "gemini-2.5-pro",
            generation_config={"response_mime_type": "application/json"},
        )

        # 复用 graph_generator 的 DEPTH_DEFINITIONS 和 Prompt 模板
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
- Vocabulary: Advanced vocabulary, subjunctive mood, complex sentences (e.g., I was wondering if, would it be possible to).
- Constraints: Generate 4-5 constraints, MUST include advanced communicative patterns.
- Example: (Ordering Step) Constraints: ["I was wondering if", "could possibly make it", "decaf", "extra shot"]
"""

        DEPTH_META = {
            1: {"desc": "Foundation", "keyword": "core survival phrases", "constraint_range": (2, 3), "scenario_count": "3-5"},
            2: {"desc": "Intermediate", "keyword": "politeness and detail modifiers", "constraint_range": (3, 4), "scenario_count": "2-4"},
            3: {"desc": "Advanced", "keyword": "native expressions and complex sentences", "constraint_range": (4, 5), "scenario_count": "1-2"},
        }

        # 检索已存在场景（RAG）
        existing = self.db.query(MicroScenario).filter(
            MicroScenario.topic_id == topic.id,
            MicroScenario.depth_level == depth,
        ).all()

        existing_context = ""
        if existing:
            existing_context = "=== Existing Scenarios (Generate new parallel scenarios, avoid vocabulary overlap) ===\n"
            for i, sc in enumerate(existing, 1):
                constraints = self.db.query(ScenarioConstraint).filter(
                    ScenarioConstraint.micro_scenario_id == sc.id
                ).all()
                constraint_texts = [c.constraint_text for c in constraints]
                existing_context += f"[Scenario {i}] {sc.scenario_name}\n  Intent: {sc.intent_desc}\n  Keywords: {', '.join(constraint_texts)}\n"

        meta = DEPTH_META[depth]
        vocab_list = ', '.join(topic.vocab_tags or []) or "N/A"
        pattern_list = ', '.join(topic.sentence_patterns or []) or "N/A"

        prompt = f"""[Task: Generate functional migration micro-scenarios for {topic.title}]

## Topic Information
- Topic: {topic.title}
- Topic (Chinese): {getattr(topic, 'title_zh', 'N/A')}
- User Role: {topic.role_name or 'N/A'}

## Topic Vocabulary Reference
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
{existing_context or '(No existing scenarios, feel free to generate 3-5 parallel scenarios)'}

## Generation Requirements
- Generate {meta['scenario_count']} parallel scenarios
- Similar communicative function
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
- [ ] Is step_order consistent with other depths?
- [ ] Is constraint count within range?
"""

        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            data = json.loads(response.text)
            scenarios_data = data.get("scenarios", [])

            if not scenarios_data:
                logger.warning("[WARN] LLM returned empty scenario list")
                return False

            # 清理旧数据（如果 rebuild）
            if self.rebuild:
                old_scenarios = self.db.query(MicroScenario).filter(
                    MicroScenario.topic_id == topic.id,
                    MicroScenario.depth_level == depth,
                ).all()
                if old_scenarios:
                    old_ids = [s.id for s in old_scenarios]
                    self.db.query(ScenarioConstraint).filter(
                        ScenarioConstraint.micro_scenario_id.in_(old_ids)
                    ).delete(synchronize_session=False)
                    self.db.query(MicroScenario).filter(
                        MicroScenario.topic_id == topic.id,
                        MicroScenario.depth_level == depth,
                    ).delete(synchronize_session=False)
                    self.db.commit()
                    logger.info(f"[CLEANUP] Cleared {len(old_scenarios)} old scenarios")

            # 写入数据库
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
                    embedding=None,
                )
                self.db.add(scenario)
                self.db.commit()
                self.db.refresh(scenario)

                # 生成 embedding
                text = f"{scenario.scenario_name} | Intent: {scenario.intent_desc} | Scene: {scenario.scene_desc}"
                vector = embedder.embed(text)
                scenario.embedding = vector
                self.db.commit()

                # 写入约束
                constraints_data = s_data.get("constraints", [])
                for c_data in constraints_data:
                    constraint = ScenarioConstraint(
                        micro_scenario_id=scenario.id,
                        constraint_text=c_data["constraint_text"],
                        constraint_type=c_data.get("constraint_type", "phrase"),
                        depth_level=depth,
                        weight=c_data.get("weight", 1.0),
                        hint_cn=c_data.get("hint_cn", ""),
                    )
                    self.db.add(constraint)

                self.db.commit()
                logger.info(f"  [OK] {scenario.scenario_code} ({len(constraints_data)} constraints)")

            return True

        except json.JSONDecodeError as e:
            logger.error(f"[ERROR] JSON parse failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[ERROR] Generation exception: {e}")
            return False

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
        success_count = 0
        fail_count = 0

        for topic, depth in tasks:
            if self._canceled:
                logger.warning("[USER INTERRUPT] Saving progress...")
                break

            task_name = f"{topic.title} (Depth {depth})"
            logger.info(f"\n[{self.progress.completed_tasks + 1}/{self.progress.total_tasks}] 处理：{task_name}")

            success = await self.generate_topic_scenarios(topic, depth)
            if success:
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

    args = parser.parse_args()

    # 验证参数
    if not args.all and (args.from_topic is None or args.to_topic is None):
        parser.error("请使用 --all 或同时指定 --from-topic 和 --to-topic")

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
        )
        await generator.run()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
