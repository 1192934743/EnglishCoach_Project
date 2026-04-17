"""
Repository 基类（Phase 1 预留）

所有 Repository 继承此接口，保证未来可无缝切换 SQLite → PostgreSQL 等。
"""
from abc import ABC, abstractmethod
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class Repository(ABC, Generic[T]):
    @abstractmethod
    async def get_by_id(self, entity_id) -> Optional[T]: ...

    @abstractmethod
    async def save(self, entity: T) -> T: ...
