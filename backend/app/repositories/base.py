from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, List, Optional, TypeVar
from uuid import UUID

T = TypeVar("T")


class AbstractRepository(ABC, Generic[T]):
    """
    Generic async repository interface. All persistence adapters implement this.

    Phase 0: Concrete implementations raise NotImplementedError.
    Phase 1: SQLAlchemy + asyncpg implementations replace the stubs.
    """

    @abstractmethod
    async def get_by_id(self, entity_id: UUID) -> Optional[T]:
        """Return entity by primary key, or None if not found."""
        ...

    @abstractmethod
    async def save(self, entity: T) -> T:
        """Persist (insert or upsert) an entity. Returns the saved entity."""
        ...

    @abstractmethod
    async def list(self, **filters: Any) -> List[T]:
        """Return entities matching all provided keyword filters."""
        ...

    @abstractmethod
    async def delete(self, entity_id: UUID) -> bool:
        """Delete entity by primary key. Returns True if deleted, False if not found."""
        ...
