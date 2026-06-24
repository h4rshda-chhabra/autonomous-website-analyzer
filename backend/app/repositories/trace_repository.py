from __future__ import annotations

from abc import abstractmethod
from typing import List, Optional
from uuid import UUID

from app.models.enums import AgentType, TraceEventType
from app.models.trace import AgentTraceEvent
from .base import AbstractRepository


class ITraceRepository(AbstractRepository[AgentTraceEvent]):

    @abstractmethod
    async def bulk_insert(self, events: List[AgentTraceEvent]) -> int:
        """Persist a batch of trace events. Returns the count inserted."""
        ...

    @abstractmethod
    async def get_by_audit(
        self,
        audit_id: UUID,
        *,
        after_sequence: int = 0,
        agent: Optional[AgentType] = None,
        event_type: Optional[TraceEventType] = None,
        limit: int = 500,
    ) -> List[AgentTraceEvent]:
        """Return trace events for an audit with optional filters."""
        ...

    @abstractmethod
    async def get_max_sequence(self, audit_id: UUID) -> int:
        """Return the highest sequence number persisted for an audit (for reconnect)."""
        ...
