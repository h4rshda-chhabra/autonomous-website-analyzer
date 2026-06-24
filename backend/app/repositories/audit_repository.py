from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.models.enums import AuditStatus
from .base import AbstractRepository


class AuditRecord(BaseModel):
    """Lightweight DB projection of an audit session."""

    id: UUID = Field(default_factory=uuid4)
    url: str
    status: AuditStatus = AuditStatus.PENDING
    overall_score: Optional[float] = None
    total_findings: int = 0
    failure_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class IAuditRepository(AbstractRepository[AuditRecord]):

    @abstractmethod
    async def create(self, url: str) -> AuditRecord:
        """Create a new audit row in PENDING status. Returns the created record."""
        ...

    @abstractmethod
    async def update_status(
        self,
        audit_id: UUID,
        status: AuditStatus,
        *,
        failure_reason: Optional[str] = None,
        overall_score: Optional[float] = None,
        total_findings: Optional[int] = None,
    ) -> None:
        """Update audit lifecycle status and optional summary fields."""
        ...

    @abstractmethod
    async def get_by_status(self, status: AuditStatus) -> List[AuditRecord]:
        """Return all audits in a given status (e.g. PENDING for queue recovery)."""
        ...

    @abstractmethod
    async def get_recent(self, limit: int = 20) -> List[AuditRecord]:
        """Return the most recently created audits."""
        ...
