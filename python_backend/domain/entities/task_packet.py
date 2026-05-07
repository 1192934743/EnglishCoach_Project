"""
TaskPacket — LMS 与对话引擎之间的唯一契约

职责分工：
- LMS（session_planner.py）负责构建 TaskPacket
- 对话引擎（dialogue_engine.py）只负责消费 TaskPacket，不做任何学习决策
- 一旦 TaskPacket 传入 build_prompts，引擎不读取任何 JSON 配置文件

字段设计原则：
- 所有字段均有默认值，确保部分字段缺失时系统能降级运行
- to_dict / from_dict 支持序列化，方便存入 LearningSession.task_packet_snapshot
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)

_CANONICAL_LEVELS = ("Beginner", "Elementary", "Intermediate", "Advanced")


def canonical_learner_level(raw: Optional[str], fallback: str = "Intermediate") -> str:
    """Map free-text / aliases to Beginner | Elementary | Intermediate | Advanced."""
    fb = fallback if fallback in _CANONICAL_LEVELS else "Intermediate"
    if not raw or not str(raw).strip():
        return fb
    key = str(raw).strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "beginner": "Beginner",
        "a1": "Beginner",
        "starter": "Beginner",
        "novice": "Beginner",
        "初级": "Beginner",
        "零基础": "Beginner",
        "elementary": "Elementary",
        "a2": "Elementary",
        "basic": "Elementary",
        "intermediate": "Intermediate",
        "b1": "Intermediate",
        "b2": "Intermediate",
        "medium": "Intermediate",
        "中级": "Intermediate",
        "advanced": "Advanced",
        "upper intermediate": "Advanced",
        "upper-intermediate": "Advanced",
        "c1": "Advanced",
        "c2": "Advanced",
        "proficient": "Advanced",
        "fluent": "Advanced",
        "熟练": "Advanced",
        "mastery": "Advanced",
        "expert": "Advanced",
    }
    if key in aliases:
        return aliases[key]
    for c in _CANONICAL_LEVELS:
        if c.lower() == key:
            return c
    for c in _CANONICAL_LEVELS:
        cl, kl = c.lower(), key
        if cl in kl or kl in cl:
            return c
    return fb


def effective_learner_label(
    user_settings: Optional[dict],
    task_learner_level: Optional[str],
    scene_default: str,
) -> str:
    """user.settings['learner_level'] > TaskPacket/scene label（与对话引擎共用）。"""
    fb = canonical_learner_level(scene_default or "Intermediate")
    if user_settings:
        r = user_settings.get("learner_level")
        if isinstance(r, str) and r.strip():
            return canonical_learner_level(r.strip(), fb)
    if task_learner_level:
        return canonical_learner_level(task_learner_level, fb)
    return canonical_learner_level(scene_default, fb)


def compute_max_reply_sentences(learner_level: str, depth_tier: int) -> int:
    """
    Max in-character sentences per coach turn (excluding a trailing [ADVANCE] token).

    This is **dialogue pacing / cognitive load**, not the same as TargetNode.depth_level
    or TaskPacket.depth_tier (those select *which expressions* to practice).

    Policy: shorter turns for beginner-labelled topics; more room for advanced learners,
    especially when practicing higher content tiers (deeper nodes / twists).
    """
    tier = max(1, min(5, int(depth_tier or 1)))
    canon = canonical_learner_level(learner_level)
    if canon == "Beginner":
        # 三句极短台词：如问候 + 简单承接 + 一个问题，避免模型压成「只说一句」
        return 3
    if canon == "Elementary":
        return 3
    if canon == "Advanced":
        cap = 5 + (1 if tier >= 2 else 0) + (1 if tier >= 3 else 0)
        return min(7, cap)
    # Intermediate
    cap = 3 + (1 if tier >= 3 else 0)
    return min(5, cap)


# ══════════════════════════════════════════════════════════════════════════════
# 阶段一新增：微场景图谱数据类
# 支撑「双轨校验·微场景图谱流转架构」
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ScenarioConstraintItem:
    """
    场景约束项 — 替代原有的 node dict，提供更结构化的约束描述。

    支撑 Director LLM 的双轨校验：
    - constraint_text：目标表达原文（Constraints 轨的检测目标）
    - constraint_type：检测粒度 (word/phrase/sentence)
    - depth_level：绝对难度（支撑难度隔离）
    - is_satisfied：当前轮是否已命中（运行时填充，不序列化）
    """

    constraint_id: int = 0
    constraint_text: str = ""
    constraint_type: str = "word"
    depth_level: int = 1
    weight: float = 1.0
    hint_cn: Optional[str] = None

    # 运行时字段（不参与序列化）
    is_satisfied: bool = False
    hit_quality: float = 0.0

    def to_dict(self) -> dict:
        return {
            "constraint_id": self.constraint_id,
            "constraint_text": self.constraint_text,
            "constraint_type": self.constraint_type,
            "depth_level": self.depth_level,
            "weight": self.weight,
            "hint_cn": self.hint_cn,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScenarioConstraintItem":
        return cls(
            constraint_id=d.get("constraint_id", 0),
            constraint_text=d.get("constraint_text", ""),
            constraint_type=d.get("constraint_type", "word"),
            depth_level=d.get("depth_level", 1),
            weight=d.get("weight", 1.0),
            hint_cn=d.get("hint_cn"),
        )


@dataclass
class MicroScenarioInfo:
    """
    当前微场景信息 — 支撑图谱流转。

    包含 MicroScenario 的基本信息以及可选的下一个微场景候选列表。
    """

    scenario_id: int = 0
    scenario_code: str = ""
    scenario_name: str = ""
    intent_desc: str = ""
    scene_desc: str = ""
    depth_level: int = 1
    step_order: int = 0
    max_turns: int = 8

    # 格式: [{"scenario_id": int, "scenario_name": str, "overlap_ratio": float,
    #          "required_hit_rate": float}, ...]
    available_transitions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_code": self.scenario_code,
            "scenario_name": self.scenario_name,
            "intent_desc": self.intent_desc,
            "scene_desc": self.scene_desc,
            "depth_level": self.depth_level,
            "step_order": self.step_order,
            "max_turns": self.max_turns,
            "available_transitions": self.available_transitions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MicroScenarioInfo":
        available_transitions = d.get("available_transitions", []) or []
        if not available_transitions:
            logger.warning(
                f"[MicroScenario] No transitions for scenario: {d.get('scenario_code')} "
                f"(is_exit_point={d.get('is_exit_point', False)})"
            )
        return cls(
            scenario_id=d.get("scenario_id", 0),
            scenario_code=d.get("scenario_code", ""),
            scenario_name=d.get("scenario_name", ""),
            intent_desc=d.get("intent_desc", ""),
            scene_desc=d.get("scene_desc", ""),
            depth_level=d.get("depth_level", 1),
            step_order=d.get("step_order", 0),
            max_turns=d.get("max_turns", 8),
            available_transitions=available_transitions,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 原有数据类
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class DifficultyConfig:
    """对话难度的控制开关集合，由 session_planner 根据用户设置生成。"""

    # 本次对话开场的深度层级（=depth_tier，首轮不超过此深度）
    first_turn_depth: int = 1
    # 经过多少轮对话后才开始带入 bonus_nodes（更深层表达）
    bonus_node_unlock_after: int = 3
    # 纠错频率：0=从不纠错，1=每次都纠错；商业推荐 0.2~0.4
    correction_frequency: float = 0.2
    # 用户沉默多少秒后触发提示（hint）
    hint_silence_threshold: float = 4.0
    # 正式度：0=非正式/口语，1=正常，2=正式/书面
    formality_level: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DifficultyConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TaskPacket:
    """
    LMS 下发给对话引擎的「作业单」— 阶段一升级版。

    字段设计原则：
    - 所有字段均有默认值，确保部分字段缺失时系统能降级运行
    - to_dict / from_dict 支持序列化，方便存入 LearningSession.task_packet_snapshot
    - 新增字段采用 Optional，运行时无数据时降级处理

    新增字段（阶段一）：
    - current_scenario: 当前微场景信息（来自 MicroScenario 表）
    - constraints / bonus_constraints / review_constraints: 结构化约束列表（来自 ScenarioConstraint 表）
    - current_intent: 当前微场景的教学意图描述（给 Director LLM 看的核心目标）

    废弃字段（标记 @deprecated，阶段二完全移除）：
    - target_nodes / bonus_nodes / review_nodes
      → 替换为 constraints / bonus_constraints / review_constraints
    """

    # ── 话题基础信息 ────────────────────────────────────────────────────────
    topic_id: int = 0
    topic_title: str = "Daily Conversation"
    topic_title_zh: Optional[str] = None
    scene_prompt: str = "A casual daily conversation"
    role_name: str = "English Coach"
    learner_level: str = "Intermediate"
    voice: str = "Stanley"

    # ── 深度控制 ────────────────────────────────────────────────────────────
    depth_tier: int = 1
    max_reply_sentences: int = 3

    # ── 【新增】微场景信息 ─────────────────────────────────────────────────
    current_scenario: MicroScenarioInfo = field(default_factory=MicroScenarioInfo)

    # ── 【新增】结构化约束列表（替代 target_nodes） ─────────────────────────
    constraints: list = field(default_factory=list)
    bonus_constraints: list = field(default_factory=list)
    review_constraints: list = field(default_factory=list)

    # ── 【新增】当前教学意图（给 Director LLM 看的 Intent 校验标尺） ─────────
    current_intent: str = ""

    # ── 【废弃 @deprecated】旧节点列表（向后兼容） ─────────────────────────
    # 阶段一：引擎优先读取 constraints，无则 fallback 到 target_nodes
    target_nodes: list = field(default_factory=list)
    bonus_nodes: list = field(default_factory=list)
    review_nodes: list = field(default_factory=list)

    # ── 难度开关 ────────────────────────────────────────────────────────────
    difficulty_config: DifficultyConfig = field(default_factory=DifficultyConfig)

    # ── 场景专属护栏 ────────────────────────────────────────────────────────
    scene_specific_rules: list = field(default_factory=list)
    vocab_tags: list = field(default_factory=list)
    sentence_patterns: list = field(default_factory=list)
    session_goal: str = ""

    # ── 序列化 ──────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TaskPacket":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        data = {k: v for k, v in d.items() if k in known}

        if "difficulty_config" in data and isinstance(data["difficulty_config"], dict):
            data["difficulty_config"] = DifficultyConfig.from_dict(data["difficulty_config"])

        if "current_scenario" in data and isinstance(data["current_scenario"], dict):
            data["current_scenario"] = MicroScenarioInfo.from_dict(data["current_scenario"])

        for field_name in ["constraints", "bonus_constraints", "review_constraints"]:
            if field_name in data and isinstance(data[field_name], list):
                data[field_name] = [
                    ScenarioConstraintItem.from_dict(c) if isinstance(c, dict) else c
                    for c in data[field_name]
                ]

        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TaskPacket":
        return cls.from_dict(json.loads(raw))

    # ── 辅助查询 ────────────────────────────────────────────────────────────
    @property
    def all_active_constraints(self) -> list:
        """当前轮次活跃的所有约束（用于双轨校验）"""
        return self.constraints + self.review_constraints

    @property
    def all_practice_nodes(self) -> list:
        """【废弃兼容】新版 constraints 优先；无则 fallback 到旧版 target_nodes"""
        if self.constraints:
            return [c.to_dict() if hasattr(c, "to_dict") else c for c in self.constraints]
        return self.target_nodes + self.review_nodes

    def has_constraints(self) -> bool:
        return bool(self.constraints or self.review_constraints)

    @property
    def has_nodes(self) -> bool:
        """兼容旧版 has_nodes 逻辑"""
        return self.has_constraints() or bool(self.target_nodes or self.review_nodes)
