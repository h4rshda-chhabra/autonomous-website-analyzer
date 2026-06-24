from __future__ import annotations

from abc import abstractmethod
from typing import List, Optional
from uuid import UUID

from app.models.enums import AgentType, FindingCategory, Severity
from app.models.finding import Finding
from .base import AbstractRepository


class IFindingRepository(AbstractRepository[Finding]):

    @abstractmethod
    async def bulk_insert(self, findings: List[Finding]) -> int:
        """Persist a batch of findings. Returns the count inserted."""
        ...

    @abstractmethod
    async def get_by_audit(
        self,
        audit_id: UUID,
        *,
        agent: Optional[AgentType] = None,
        severity: Optional[Severity] = None,
        category: Optional[FindingCategory] = None,
    ) -> List[Finding]:
        """Return findings for an audit with optional filters."""
        ...

    @abstractmethod
    async def get_by_ids(self, finding_ids: List[UUID]) -> List[Finding]:
        """Return multiple findings by ID (used by Synthesis Agent)."""
        ...

    @abstractmethod
    async def count_by_audit(self, audit_id: UUID) -> int:
        """Return total finding count for an audit."""
        ...
