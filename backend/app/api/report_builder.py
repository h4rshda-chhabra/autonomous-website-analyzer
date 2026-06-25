"""
report_builder.py — Builds the full JSON report from SharedState.

Reads recon artifacts, agent findings, and synthesis insights from SharedState.
Called by GET /audits/{id}/report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from app.models.enums import Severity
from app.services.shared_state_service import SharedStateService
from app.services.trace_service import TraceServiceImpl


async def build_report(
    audit_id: UUID,
    state_svc: SharedStateService,
    trace_svc: TraceServiceImpl,
) -> Optional[Dict[str, Any]]:
    """
    Returns the complete audit report dict, or None if audit_id is unknown.
    """
    state = await state_svc.get_state(audit_id)
    if state is None:
        return None

    all_findings = await state_svc.get_all_findings(audit_id)
    total_events = await trace_svc.get_current_sequence(audit_id)

    # Severity breakdown
    n_critical = sum(1 for f in all_findings if f.severity == Severity.CRITICAL)
    n_high     = sum(1 for f in all_findings if f.severity == Severity.HIGH)
    n_medium   = sum(1 for f in all_findings if f.severity == Severity.MEDIUM)
    n_low      = sum(1 for f in all_findings if f.severity == Severity.LOW)

    # Recon artifacts (may be None if audit failed during recon)
    crawl       = await state_svc.get_recon_data(audit_id, "playwright_output")
    headers     = await state_svc.get_recon_data(audit_id, "header_analysis")
    links       = await state_svc.get_recon_data(audit_id, "link_extraction")
    meta        = await state_svc.get_recon_data(audit_id, "meta_analysis")
    schema_data = await state_svc.get_recon_data(audit_id, "structured_data")

    recon: Dict[str, Any] = {}
    if crawl:
        recon["performance"] = {
            "ttfb_ms": getattr(getattr(crawl, "page_timings", None), "ttfb_ms", None),
            "dom_content_loaded_ms": getattr(getattr(crawl, "page_timings", None), "dom_content_loaded_ms", None),
            "load_event_ms": getattr(getattr(crawl, "page_timings", None), "load_event_ms", None),
            "total_requests": getattr(crawl, "total_requests", None),
        }
        js_errors = [m for m in getattr(crawl, "console_messages", []) if m.level == "error"]
        recon["technical"] = {
            "http_status": getattr(crawl, "http_status_code", None),
            "redirect_count": len(getattr(crawl, "redirect_chain", [])),
            "js_error_count": len(js_errors),
        }
    if headers:
        sec = getattr(headers, "security", None)
        cach = getattr(headers, "caching", None)
        recon["headers"] = {
            "security_score": getattr(sec, "overall_score", None),
            "caching_score": getattr(cach, "overall_score", None),
            "is_https": getattr(getattr(headers, "server_info", None), "is_https", None),
        }
    if links:
        recon["links"] = {
            "internal": getattr(links, "total_internal", 0),
            "external": getattr(links, "total_external", 0),
        }
    if meta:
        title = getattr(meta, "title", None)
        md = getattr(meta, "meta_description", None)
        recon["seo"] = {
            "title": getattr(title, "text", None),
            "title_length": getattr(title, "length_chars", None),
            "title_in_range": getattr(title, "is_within_length", None),
            "meta_description": getattr(md, "is_present", None),
            "canonical_url": getattr(meta, "canonical_url", None),
            "is_indexable": getattr(getattr(meta, "robots", None), "is_indexable", None),
            "og_complete": getattr(getattr(meta, "open_graph", None), "is_complete", None),
            "twitter_card": getattr(getattr(meta, "twitter_card", None), "card_type", None),
            "lang": getattr(meta, "lang_attribute", None),
        }
    if schema_data:
        recon["structured_data"] = {
            "json_ld_count": getattr(schema_data, "json_ld_count", 0),
            "has_organization_schema": getattr(schema_data, "has_organization_schema", False),
            "has_website_schema": getattr(schema_data, "has_website_schema", False),
            "schemas_found": [s.schema_type for s in getattr(schema_data, "schemas_found", [])],
            "expected_missing": getattr(schema_data, "expected_schemas_missing", []),
        }

    # Findings
    findings_out = [
        {
            "id": str(f.id),
            "agent": f.agent.value,
            "category": f.category.value,
            "severity": f.severity.value,
            "title": f.title,
            "description": f.description,
            "business_impact": f.business_impact,
            "priority_score": round(f.priority_score, 3),
            "effort": f.effort.value,
            "effort_hours_min": f.effort_hours_min,
            "effort_hours_max": f.effort_hours_max,
            "fix_description": f.fix_suggestion.description,
            "confidence": f.confidence,
            "tags": f.tags,
        }
        for f in sorted(all_findings, key=lambda f: f.priority_score, reverse=True)
    ]

    # Execution stats
    elapsed: Optional[float] = None
    if state.created_at and state.completed_at:
        elapsed = (state.completed_at - state.created_at).total_seconds()

    from app.models.enums import AgentStatus
    agent_stats = {}
    for agent_type, entry in state.agent_states.items():
        if agent_type.value in ("orchestrator",):
            continue
        agent_stats[agent_type.value] = {
            "status": entry.status.value,
            "findings_written": entry.findings_written,
            "duration_seconds": entry.duration_seconds,
        }

    profile = state.site_profile
    plan = state.audit_plan

    return {
        "audit_id": str(audit_id),
        "url": state.url,
        "status": state.status.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "site_profile": profile.model_dump(mode="json") if profile else None,
        "audit_plan": {
            "enabled_agents": [a.value for a in plan.enabled_agents] if plan else [],
            "parallel_agents": [a.value for a in (plan.parallel_agents if plan else [])],
            "deep_agents": [a.value for a in (plan.deep_agents if plan else [])],
            "rationale": plan.rationale.model_dump(mode="json") if plan and plan.rationale else None,
        } if plan else None,
        "findings": findings_out,
        "synthesis": state.synthesis_insights,
        "scores": {
            "critical": n_critical,
            "high": n_high,
            "medium": n_medium,
            "low": n_low,
            "total": len(all_findings),
        },
        "recon": recon,
        "warnings": state.ai_warnings,
        "execution_stats": {
            "elapsed_seconds": elapsed,
            "total_trace_events": total_events,
            "agents": agent_stats,
        },
    }
