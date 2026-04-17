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
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class SessionContext:
    # ── 阶段机 ────────────────────────────────────────────────────────────
    phase: str = "ICE_BREAKING"          # 当前阶段
    phase_turns: int = 0                 # 本阶段已互动轮数（防卡死用）
    loop_count: int = 1                  # 完成整局的次数（WRAP_UP 后 +1）
    llm_wants_to_advance: bool = False   # 上一轮 AI 是否在末尾输出 [ADVANCE]

    # ── 词表 ──────────────────────────────────────────────────────────────
    new_targets: list = field(default_factory=list)      # 本轮新词 [{"id", "node_text"}]
    history_targets: list = field(default_factory=list)  # 历史词（滚雪球累积）
    active_targets: list = field(default_factory=list)   # 预留字段
    current_event: str = ""                              # EVENT_EXTENSION 阶段的剧情文本

    # ── 段位与计分 ────────────────────────────────────────────────────────
    current_level: int = 1                    # 用户当前段位（Lv）
    chat_score: float = 0.0                  # 本轮闲聊分（上限 40）
    task_score: float = 0.0                  # 本轮任务分（上限 60）
    chat_interaction_count: int = 0          # 闲聊曲线已消费的步数
    completed_rounds_in_level: int = 0       # 本段位已完整跑完的局数（满 3 升级）

    # ── 兼容层：支持 ctx["key"] 读写，降低 server.py 迁移成本 ─────────────
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    # ── 序列化 ────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionContext":
        """从字典重建（兼容旧 session_ctx dict，忽略未知键）"""
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_json(cls, raw: str) -> "SessionContext":
        return cls.from_dict(json.loads(raw))
