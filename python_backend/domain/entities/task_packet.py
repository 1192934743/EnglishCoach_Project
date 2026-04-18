"""
TaskPacket — LMS 与对话引擎之间的唯一契约

职责分工：
- LMS（session_planner.py）负责构建 TaskPacket
- 对话引擎（dialogue_engine.py）只负责消费 TaskPacket，不做任何学习决策
- 一旦 TaskPacket 传入 build_dynamic_prompt，引擎就不再读取 scenes.json

字段设计原则：
- 所有字段均有默认值，确保部分字段缺失时系统能降级运行
- to_dict / from_dict 支持序列化，方便存入 LearningSession.task_packet_snapshot
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional

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
    LMS 下发给对话引擎的「作业单」。

    target_nodes:   本次必须覆盖的核心节点（当前 depth_tier 的主练内容）
    bonus_nodes:    可选更深节点（对话够自然时 AI 自然带入，不强求）
    review_nodes:   根据遗忘曲线需要复习的旧节点（来自低于当前 tier 的历史）

    node 格式统一为：
        {"id": int, "node_text": str, "node_type": str, "depth_level": int}
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
    # depth_tier: 本次主练的「内容层级」(对齐 TargetNode.depth_level)，决定选哪些节点，不是每轮句数。
    depth_tier: int = 1
    # 每轮教练台词句数上限（与 depth_tier 正交；由 LMS 根据 learner_level + depth_tier 计算）
    max_reply_sentences: int = 3

    # ── 节点列表（纯数据字典，不持有 ORM 对象）───────────────────────────────
    target_nodes: list = field(default_factory=list)
    bonus_nodes: list = field(default_factory=list)
    review_nodes: list = field(default_factory=list)

    # ── 难度开关 ────────────────────────────────────────────────────────────
    difficulty_config: DifficultyConfig = field(default_factory=DifficultyConfig)

    # ── 场景专属护栏（来自 Topic.scene_specific_rules） ───────────────────────
    scene_specific_rules: list = field(default_factory=list)

    # ── 给 AI 看的自然语言目标（注入 Prompt，帮助 AI 理解本次练习意图）────────
    session_goal: str = ""

    # ── 序列化 ────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskPacket":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        data = {k: v for k, v in d.items() if k in known}
        if "difficulty_config" in data and isinstance(data["difficulty_config"], dict):
            data["difficulty_config"] = DifficultyConfig.from_dict(data["difficulty_config"])
        return cls(**data)

    @classmethod
    def from_json(cls, raw: str) -> "TaskPacket":
        return cls.from_dict(json.loads(raw))

    # ── 辅助查询 ──────────────────────────────────────────────────────────
    @property
    def all_practice_nodes(self) -> list:
        """target + review 节点的合集，用于传给对话状态机的 new_targets"""
        return self.target_nodes + self.review_nodes

    def has_nodes(self) -> bool:
        return bool(self.target_nodes or self.review_nodes)
