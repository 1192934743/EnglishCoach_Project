"""
SessionContext — 会话状态领域对象

职责：
- 作为单次 WebSocket 连接的会话状态载体，替代原来散落在 server.py 的裸 dict。
- 支持 to_dict() / from_dict() 序列化，未来迁移至 Redis 时只需改持久化层。
- 与现有 dict 完全兼容：server.py 仍可用 ctx["key"] 方式读写（通过 __getitem__ / __setitem__）。
  迁移期过后可改为直接访问属性。

注意：
- new_targets / history_targets 存储纯数据字典列表 [{"id": int, "node_text": str}]，
  不持有 ORM 对象，以避免跨 Session Detached 问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from typing import Any


@dataclass
class SessionContext:
    # ── 阶段机 ────────────────────────────────────────────────────────────
    phase: str = "ICE_BREAKING"
    phase_turns: int = 0
    loop_count: int = 1
    llm_wants_to_advance: bool = False

    # ── 词表 ──────────────────────────────────────────────────────────────
    new_targets: list = field(default_factory=list)
    history_targets: list = field(default_factory=list)
    active_targets: list = field(default_factory=list)
    current_event: str = ""

    # ── 段位与计分 ────────────────────────────────────────────────────────
    current_level: int = 1
    chat_score: float = 0.0
    task_score: float = 0.0
    chat_interaction_count: int = 0
    completed_rounds_in_level: int = 0

    # ── 【阶段二新增】微场景流转 ──────────────────────────────────────────
    turn_count_in_scenario: int = 0
    scenario_completed: bool = False
    _constraint_hits: set = field(default_factory=set)

    # ── 【阶段二新增】Director 双轨信号 ─────────────────────────────────
    director_scenario_completed: bool = False
    director_constraints_hit: bool = False
    director_intent_achieved: bool = False
    next_scenario: Any = None

    # ── 兼容层：支持 ctx["key"] 读写 ─────────────────────────────────────
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def pop(self, key: str, default: Any = ...) -> Any:
        """
        模拟 dict.pop() 行为：读取并删除属性。
        支持 declared 字段和 extra 字段。

        注意：dataclass 默认字段在声明时存在，所以 extra 字段需要用 hasattr 判断。
        """
        has_attr = hasattr(self, key)
        if default is ...:
            value = getattr(self, key)
        else:
            value = getattr(self, key, default)
        if has_attr:
            delattr(self, key)
        return value

    # ── 序列化 ────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionContext":
        """从字典重建（兼容旧 session_ctx dict，忽略未知键）"""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_json(cls, raw: str) -> "SessionContext":
        return cls.from_dict(json.loads(raw))
