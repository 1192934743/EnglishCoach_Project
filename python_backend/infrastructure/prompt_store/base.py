"""
TopicProvider — 话题 / 场景数据来源抽象接口（Phase 3 预留）

当前实现：DatabaseTopicProvider（读 Topic DB，已完成迁移）。
未来可替换为：RemoteCMSTopicProvider / DatabaseTopicProvider，
调用方（dialogue_engine / server）无需改动。
"""
from abc import ABC, abstractmethod


class TopicProvider(ABC):
    @abstractmethod
    async def get_active_scene(self, user_id: str) -> dict:
        """返回场景字典 {scene, role, level, scene_specific_rules, ...}"""

    @abstractmethod
    async def is_topic_unlocked(self, user_id: str, topic_id: str) -> bool:
        """付费 / 课程解锁校验；MVP 阶段永远返回 True"""
