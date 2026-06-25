from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import get_services
from app.api.report_builder import build_report
from app.api.schemas import (
    AgentSummary,
    AuditCreateRequest,
    AuditCreateResponse,
    AuditReportResponse,
    AuditStatusResponse,
    FindingOut,
)
from app.models.enums import AgentStatus, AuditStatus, Severity
from app.api.audit_runner import run_full_audit

router = APIRouter(prefix="/audits", tags=["audits"])


# ─── POST /audits ─────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=AuditCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new audit",
)
async def create_audit(body: AuditCreateRequest, request: Request) -> AuditCreateResponse:
    """
    Validates the URL, creates a new audit session, and starts the audit
    pipeline in the background. Returns immediately with the audit_id.
    Poll GET /audits/{audit_id} to check progress.
    """
    services = get_services(request)
    audit_id = uuid4()
    url = body.url

    await services.state.create_session(audit_id, url)

    asyncio.create_task(
        run_full_audit(audit_id, url, services=services)
    )

    return AuditCreateResponse(audit_id=audit_id, status=AuditStatus.PENDING.value)


# ─── GET /audits/{audit_id} ───────────────────────────────────────────────────

@router.get(
    "/{audit_id}",
    response_model=AuditStatusResponse,
    summary="Get audit status",
)
async def get_audit_status(audit_id: UUID, request: Request) -> AuditStatusResponse:
    """
    Returns current audit status, per-agent summaries, finding counts, and warnings.
    Reads directly from SharedState — safe to poll frequently.
    """
    services = get_services(request)
    state = await services.state.get_state(audit_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    now = datetime.utcnow()
    elapsed: Optional[float] = None
    if state.status in (AuditStatus.COMPLETE, AuditStatus.COMPLETE_WITH_WARNINGS, AuditStatus.FAILED):
        ref = state.completed_at or state.failed_at
        if ref:
            elapsed = (ref - state.created_at).total_seconds()
    else:
        elapsed = (now - state.created_at).total_seconds()

    all_findings = state.get_all_findings()
    n_critical = sum(1 for f in all_findings if f.severity == Severity.CRITICAL)
    n_high = sum(1 for f in all_findings if f.severity == Severity.HIGH)

    completed_agents = []
    failed_agents = []
    agent_summaries = []

    for agent_type, entry in state.agent_states.items():
        if agent_type.value in ("orchestrator",):
            continue
        if entry.status == AgentStatus.COMPLETE:
            completed_agents.append(agent_type.value)
        elif entry.status == AgentStatus.FAILED:
            failed_agents.append(agent_type.value)
        if entry.status != AgentStatus.PENDING:
            agent_summaries.append(AgentSummary(
                agent=agent_type.value,
                status=entry.status.value,
                findings_written=entry.findings_written,
                duration_seconds=entry.duration_seconds,
                error_message=entry.error_message,
            ))

    return AuditStatusResponse(
        audit_id=audit_id,
        url=state.url,
        status=state.status.value,
        created_at=state.created_at,
        elapsed_seconds=round(elapsed, 1) if elapsed is not None else None,
        completed_at=state.completed_at,
        failed_at=state.failed_at,
        failure_reason=state.failure_reason,
        total_findings=len(all_findings),
        critical_findings=n_critical,
        high_findings=n_high,
        completed_agents=completed_agents,
        failed_agents=failed_agents,
        agent_summaries=agent_summaries,
        warnings=state.ai_warnings,
    )


# ─── GET /audits/{audit_id}/report ───────────────────────────────────────────

@router.get(
    "/{audit_id}/report",
    response_model=AuditReportResponse,
    summary="Get the complete audit report",
)
async def get_audit_report(audit_id: UUID, request: Request) -> AuditReportResponse:
    """
    Returns the full audit report. Only available after the audit reaches
    COMPLETE or COMPLETE_WITH_WARNINGS status.
    """
    services = get_services(request)
    state = await services.state.get_state(audit_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    if state.status not in (
        AuditStatus.COMPLETE,
        AuditStatus.COMPLETE_WITH_WARNINGS,
        AuditStatus.FAILED,
    ):
        raise HTTPException(
            status_code=status.HTTP_425_TOO_EARLY,
            detail=f"Audit is still running (status={state.status.value}). Try again when complete.",
        )

    report = await build_report(audit_id, services.state, services.trace)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    all_findings = await services.state.get_all_findings(audit_id)
    findings_out = [
        FindingOut(
            id=f.id,
            agent=f.agent.value,
            category=f.category.value,
            severity=f.severity.value,
            title=f.title,
            description=f.description,
            business_impact=f.business_impact,
            priority_score=round(f.priority_score, 3),
            effort=f.effort.value,
            effort_hours_min=f.effort_hours_min,
            effort_hours_max=f.effort_hours_max,
            fix_description=f.fix_suggestion.description,
            confidence=f.confidence,
            tags=f.tags,
        )
        for f in sorted(all_findings, key=lambda f: f.priority_score, reverse=True)
    ]

    return AuditReportResponse(
        audit_id=audit_id,
        url=report["url"],
        status=report["status"],
        timestamp=report["timestamp"],
        site_profile=report.get("site_profile"),
        audit_plan=report.get("audit_plan"),
        findings=findings_out,
        synthesis=report.get("synthesis"),
        scores=report.get("scores", {}),
        recon=report.get("recon"),
        warnings=report.get("warnings", []),
        execution_stats=report.get("execution_stats", {}),
    )
