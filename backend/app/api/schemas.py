from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, HttpUrl, field_validator


# ─── Request models ───────────────────────────────────────────────────────────

class AuditCreateRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = f"https://{v}"
        if not v.replace("https://", "").replace("http://", ""):
            raise ValueError("URL must not be empty")
        return v


# ─── Response models ──────────────────────────────────────────────────────────

class AuditCreateResponse(BaseModel):
    audit_id: UUID
    status: str


class AgentSummary(BaseModel):
    agent: str
    status: str
    findings_written: int
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None


class AuditStatusResponse(BaseModel):
    audit_id: UUID
    url: str
    status: str
    created_at: datetime
    elapsed_seconds: Optional[float] = None
    completed_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None

    # Progress
    total_findings: int = 0
    critical_findings: int = 0
    high_findings: int = 0
    completed_agents: List[str] = []
    failed_agents: List[str] = []
    agent_summaries: List[AgentSummary] = []

    # Warnings
    warnings: List[str] = []


class FindingOut(BaseModel):
    id: UUID
    agent: str
    category: str
    severity: str
    title: str
    description: str
    business_impact: str
    priority_score: float
    effort: str
    effort_hours_min: Optional[int] = None
    effort_hours_max: Optional[int] = None
    fix_description: str
    confidence: float
    tags: List[str] = []


class AuditReportResponse(BaseModel):
    audit_id: UUID
    url: str
    status: str
    timestamp: str
    site_profile: Optional[Dict[str, Any]] = None
    audit_plan: Optional[Dict[str, Any]] = None
    findings: List[FindingOut] = []
    synthesis: Optional[Dict[str, Any]] = None
    scores: Dict[str, Any] = {}
    recon: Optional[Dict[str, Any]] = None
    warnings: List[str] = []
    execution_stats: Dict[str, Any] = {}
