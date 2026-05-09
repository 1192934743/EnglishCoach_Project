"""
enums.py — 图谱引擎枚举类定义

包含：
- EdgeType: 边类型枚举（VERTICAL_CORE / HORIZONTAL_MIGRATION / CROSS_TOPIC_MIGRATION / MANUAL）
"""

from enum import Enum


class EdgeType(str, Enum):
    """
    ScenarioTransition 边类型枚举。

    v2.0 语义定义：
    - VERTICAL_CORE: 跨 Step 的主线推进 (Step N -> Step N+1)
    - HORIZONTAL_MIGRATION: 同 Step 内的平移/分支
    - CROSS_TOPIC_MIGRATION: 跨话题虫洞（仅连接话题入口节点）
    - MANUAL: 手动添加的边

    注意：同 Step 内的 Depth 难度晋级不需要连边，由 Session Planner 运行时动态决定。
    """

    VERTICAL_CORE = "VERTICAL_CORE"               # 垂直主线：跨 Step 流转
    HORIZONTAL_MIGRATION = "HORIZONTAL_MIGRATION" # 横向迁移：同 Step 内平移
    CROSS_TOPIC_MIGRATION = "CROSS_TOPIC_MIGRATION"  # 跨话题虫洞
    MANUAL = "MANUAL"                             # 手动边

    @classmethod
    def from_created_by(cls, created_by: str) -> "EdgeType":
        """
        兼容旧数据的 created_by 字符串。

        Args:
            created_by: 旧版 created_by 字符串

        Returns:
            对应的 EdgeType 枚举值
        """
        mapping = {
            "algorithm": cls.VERTICAL_CORE,
            "migration_algorithm": cls.HORIZONTAL_MIGRATION,
            "semantic_algorithm": cls.VERTICAL_CORE,
            "semantic_horizontal": cls.HORIZONTAL_MIGRATION,
            "cross_topic_algorithm": cls.CROSS_TOPIC_MIGRATION,
            "manual": cls.MANUAL,
        }
        return mapping.get(created_by, cls.VERTICAL_CORE)

    def to_created_by(self) -> str:
        """
        将 EdgeType 转换为兼容旧版的 created_by 字符串。

        Returns:
            旧版 created_by 字符串
        """
        mapping = {
            self.VERTICAL_CORE: "semantic_algorithm",
            self.HORIZONTAL_MIGRATION: "semantic_horizontal",
            self.CROSS_TOPIC_MIGRATION: "cross_topic_algorithm",
            self.MANUAL: "manual",
        }
        return mapping.get(self, "algorithm")
