"""
audit_runner.py — Full audit pipeline for the FastAPI server.

Replicates main.py's recon + AgentRuntime execution as a single async function
that can be dispatched as a background task. All output goes to SharedState;
callers poll GET /audits/{id} for progress.
"""
from __future__ import annotations

import asyncio
import json as _json
import textwrap
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.api.deps import AppServices
from app.infrastructure.logging import get_logger
from app.infrastructure.settings import settings
from app.models import (
    AgentConfig,
    AgentType,
    AuditDepth,
    AuditPlan,
    AuditStatus,
    PlanRationale,
    RenderingStrategy,
    Severity,
    SiteCategory,
    SiteProfile,
    TechStack,
)
from app.models.site_profile import RenderingEvidence, ReconSignal, SiteGoal
from app.runtime.agent_runtime import AgentRuntime
from app.tools.recon.schemas import (
    HeaderAnalyzerInput,
    LinkExtractorInput,
    PlaywrightCrawlerInput,
    ScreenshotCaptureInput,
    TechStackDetectorInput,
)
from app.tools.recon.tools import (
    run_header_analyzer,
    run_link_extractor,
    run_playwright_crawler,
    run_screenshot_capture,
    run_tech_stack_detector,
)
from app.tools.seo.schemas import (
    MetaTagAnalyzerInput,
    StructuredDataAnalyzerInput,
)
from app.tools.seo.tools import (
    run_meta_tag_analyzer,
    run_structured_data_analyzer,
)

_log = get_logger(__name__)

# ─── Deep-focus maps (same as main.py) ────────────────────────────────────────

_DEEP_AGENTS: Dict[SiteCategory, List[AgentType]] = {
    SiteCategory.SAAS:       [AgentType.SEO, AgentType.PERFORMANCE, AgentType.CONTENT],
    SiteCategory.ECOMMERCE:  [AgentType.SEO, AgentType.PERFORMANCE, AgentType.TECHNICAL],
    SiteCategory.BLOG:       [AgentType.SEO, AgentType.CONTENT],
    SiteCategory.NEWS:       [AgentType.SEO, AgentType.PERFORMANCE, AgentType.CONTENT],
    SiteCategory.PORTFOLIO:  [AgentType.SEO, AgentType.ACCESSIBILITY, AgentType.CONTENT],
    SiteCategory.AGENCY:     [AgentType.SEO, AgentType.PERFORMANCE, AgentType.CONTENT],
    SiteCategory.CORPORATE:  [AgentType.SEO, AgentType.ACCESSIBILITY],
    SiteCategory.NONPROFIT:  [AgentType.SEO, AgentType.ACCESSIBILITY, AgentType.CONTENT],
    SiteCategory.DOCS:       [AgentType.SEO, AgentType.TECHNICAL, AgentType.CONTENT],
    SiteCategory.OTHER:      [],
}

_PRIORITY_AREAS: Dict[SiteCategory, Dict[AgentType, List[str]]] = {
    SiteCategory.SAAS: {
        AgentType.SEO: ["meta_tags", "structured_data"],
        AgentType.PERFORMANCE: ["core_web_vitals", "render_blocking"],
        AgentType.CONTENT: ["value_proposition", "cta"],
    },
    SiteCategory.ECOMMERCE: {
        AgentType.SEO: ["structured_data", "internal_linking"],
        AgentType.PERFORMANCE: ["core_web_vitals", "asset_optimization"],
        AgentType.TECHNICAL: ["security", "broken_links"],
    },
    SiteCategory.BLOG: {
        AgentType.SEO: ["meta_tags", "content_signals"],
        AgentType.CONTENT: ["readability", "content_quality"],
    },
    SiteCategory.NEWS: {
        AgentType.SEO: ["structured_data", "meta_tags"],
        AgentType.PERFORMANCE: ["core_web_vitals"],
        AgentType.CONTENT: ["content_quality"],
    },
    SiteCategory.PORTFOLIO: {
        AgentType.SEO: ["meta_tags"],
        AgentType.ACCESSIBILITY: ["wcag_perceivable", "wcag_operable"],
    },
    SiteCategory.AGENCY: {
        AgentType.SEO: ["meta_tags", "structured_data"],
        AgentType.PERFORMANCE: ["core_web_vitals"],
        AgentType.CONTENT: ["value_proposition", "cta"],
    },
    SiteCategory.CORPORATE: {
        AgentType.SEO: ["structured_data", "meta_tags"],
        AgentType.ACCESSIBILITY: ["wcag_perceivable"],
    },
    SiteCategory.NONPROFIT: {
        AgentType.SEO: ["meta_tags"],
        AgentType.ACCESSIBILITY: ["wcag_perceivable", "wcag_operable"],
        AgentType.CONTENT: ["value_proposition", "cta"],
    },
    SiteCategory.DOCS: {
        AgentType.SEO: ["meta_tags", "internal_linking"],
        AgentType.TECHNICAL: ["broken_links", "http_standards"],
        AgentType.CONTENT: ["readability"],
    },
}


# ─── Deterministic helpers (mirrors main.py) ──────────────────────────────────

def _classify_rendering(
    crawl: Any,
    tech: Any,
) -> Tuple[RenderingStrategy, RenderingEvidence]:
    initial = getattr(crawl, "static_word_count", 0) or 0
    rendered = getattr(crawl, "rendered_word_count", 0) or 0
    ratio = rendered / max(1, initial)

    js_detected = bool(
        getattr(tech, "frontend_framework", None)
        or getattr(tech, "meta_framework", None)
    )
    ssr_headers = getattr(tech, "ssr_headers_present", False)
    hydration = getattr(tech, "hydration_markers_present", False)

    meta_fw = getattr(tech, "meta_framework", None)
    meta_fw_name = (meta_fw.name if meta_fw and hasattr(meta_fw, "name") else "").lower()

    evidence = RenderingEvidence(
        initial_html_word_count=initial,
        rendered_html_word_count=rendered,
        js_framework_detected=js_detected,
        ssr_headers_detected=ssr_headers,
        hydration_markers_detected=hydration,
        content_parity_ratio=round(ratio, 3),
    )

    if ratio > 0.80:
        if ssr_headers and hydration:
            strategy = RenderingStrategy.HYBRID
        elif "gatsby" in meta_fw_name or "astro" in meta_fw_name:
            strategy = RenderingStrategy.SSG
        else:
            strategy = RenderingStrategy.SSR
    elif ratio < 0.25:
        strategy = RenderingStrategy.CSR
    else:
        strategy = RenderingStrategy.HYBRID if hydration else RenderingStrategy.UNKNOWN

    return strategy, evidence


def _build_tech_stack(tech: Any) -> TechStack:
    def _name(dt: Any) -> Optional[str]:
        return dt.name if dt else None

    return TechStack(
        frontend_framework=_name(getattr(tech, "frontend_framework", None)),
        meta_framework=_name(getattr(tech, "meta_framework", None)),
        cms=_name(getattr(tech, "cms", None)),
        ecommerce_platform=_name(getattr(tech, "ecommerce_platform", None)),
        cdn=_name(getattr(tech, "cdn", None)),
        analytics=[t.name for t in getattr(tech, "analytics_tools", [])],
        tag_manager=_name(getattr(tech, "tag_manager", None)),
        ab_testing=_name(getattr(tech, "ab_testing", None)),
        chat_widget=_name(getattr(tech, "chat_widget", None)),
        error_tracking=_name(getattr(tech, "error_tracking", None)),
        detection_signals={
            t.name: t.signals
            for t in getattr(tech, "detected_technologies", [])
        },
    )


def _fallback_classification(url: str) -> Dict[str, Any]:
    return {
        "category": SiteCategory.OTHER.value,
        "category_confidence": 0.50,
        "category_reasoning": "Fallback classification — LLM unavailable.",
        "primary_goals": [
            {"goal": "unknown", "confidence": 0.50, "signals": ["No LLM classification available"]}
        ],
        "recon_signals": [],
    }


async def _classify_with_llm(
    url: str,
    crawl: Any,
    tech: Any,
    meta: Any,
    schema_data: Any,
    headers: Any,
    llm: Any,
) -> Dict[str, Any]:
    """
    Calls the injected LLMClient for site classification.
    Returns parsed dict. Falls back to _fallback_classification on any error.
    """
    from app.llm.base import LLMMessage

    tech_lines = []
    for attr in ("meta_framework", "frontend_framework", "cms", "ecommerce_platform", "cdn"):
        obj = getattr(tech, attr, None)
        if obj:
            tech_lines.append(f"  {attr}: {obj.name}")
    analytics = [t.name for t in getattr(tech, "analytics_tools", [])]
    if analytics:
        tech_lines.append(f"  analytics: {', '.join(analytics)}")

    title = getattr(meta, "title", None)
    title_text = title.text if title else "(none)"
    desc = getattr(meta, "meta_description", None)
    desc_text = (desc.text or "(none)") if desc else "(none)"
    schemas = [s.schema_type for s in getattr(schema_data, "schemas_found", [])]
    security_score = getattr(getattr(headers, "security", None), "overall_score", "?")
    caching_score = getattr(getattr(headers, "caching", None), "overall_score", "?")
    is_https = getattr(getattr(headers, "server_info", None), "is_https", True)
    html_excerpt = crawl.static_html[:3000]

    category_values = [c.value for c in SiteCategory]

    system_prompt = (
        "You are a website classification expert. "
        "Analyse the provided website data and return ONLY a valid JSON object. "
        "No explanation, no markdown — just the JSON."
    )
    user_prompt = textwrap.dedent(f"""\
        Classify this website. Return a JSON object with these exact keys:
          "category": one of {category_values},
          "category_confidence": float 0.0-1.0,
          "category_reasoning": string (1-2 sentences),
          "primary_goals": array of 1-3 objects, each with "goal" (string), "confidence" (float), "signals" (string array),
          "recon_signals": array of objects, each with "area" (one of: seo, performance, accessibility, content, technical), "signal" (string), "implication" (string), "suggested_priority" (one of: critical, high, medium, low, info)

        Data:
        URL: {url}
        Final URL: {crawl.final_url}
        HTTP Status: {crawl.http_status_code}
        HTTPS: {is_https}
        Title: {title_text}
        Meta Description: {desc_text}
        Tech Stack:
        {chr(10).join(tech_lines) if tech_lines else '  (none detected)'}
        Structured Data: {schemas or '(none)'}
        Static words: {crawl.static_word_count} | Rendered words: {crawl.rendered_word_count}
        Security score: {security_score} | Caching score: {caching_score}

        HTML excerpt:
        ---
        {html_excerpt}
        ---
    """)

    resp = await llm.complete(
        [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ],
        max_tokens=settings.openrouter_classification_max_tokens,
        temperature=0.0,
        json_mode=True,
    )

    if not resp.success:
        _log.warning("LLM classification failed: %s — using fallback", resp.error)
        return _fallback_classification(url)

    try:
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return _json.loads(raw)
    except Exception as exc:
        _log.warning("LLM classification parse error: %s — using fallback", exc)
        return _fallback_classification(url)


def _build_audit_plan(audit_id: UUID, profile: SiteProfile) -> AuditPlan:
    category = profile.category
    deep = set(_DEEP_AGENTS.get(category, []))
    priority_map = _PRIORITY_AREAS.get(category, {})

    specialist_agents = [a for a in AgentType if a not in (AgentType.ORCHESTRATOR,)]
    agent_configs: Dict[AgentType, AgentConfig] = {}
    parallel: List[AgentType] = []

    for agent in specialist_agents:
        if agent == AgentType.SYNTHESIS:
            agent_configs[agent] = AgentConfig(enabled=True, depth=AuditDepth.STANDARD)
            continue
        depth = AuditDepth.DEEP if agent in deep else AuditDepth.STANDARD
        prio = priority_map.get(agent, [])
        agent_configs[agent] = AgentConfig(
            enabled=True,
            depth=depth,
            priority_areas=prio,
            special_instructions=(
                f"This is a {category.value} site. Focus on {prio[0] if prio else 'all areas'}."
                if prio else None
            ),
        )
        parallel.append(agent)

    rationale = PlanRationale(
        site_summary=(
            f"Classified as {category.value} (confidence {profile.category_confidence:.0%}). "
            f"{profile.category_reasoning}"
        ),
        key_recon_signals=[s.signal for s in profile.recon_signals[:3]],
        agents_prioritized=list(deep - {AgentType.SYNTHESIS}),
        agents_deprioritized=[a for a in parallel if a not in deep],
        agents_skipped=[],
        skip_reasons={},
        estimated_duration_seconds=90 + 30 * len(deep),
    )

    return AuditPlan(
        audit_id=audit_id,
        site_profile_id=profile.id,
        agent_configs=agent_configs,
        parallel_agents=parallel,
        rationale=rationale,
    )


# ─── Public entry point ───────────────────────────────────────────────────────

async def run_full_audit(
    audit_id: UUID,
    url: str,
    *,
    services: AppServices,
    screenshot: bool = False,
) -> None:
    """
    Full audit pipeline. Dispatched as asyncio.create_task() by POST /audits.
    Updates SharedState throughout; clients poll GET /audits/{id} for status.
    """
    try:
        await _run(audit_id, url, services=services, screenshot=screenshot)
    except Exception as exc:
        _log.error("Audit %s failed at top level: %s", str(audit_id)[:8], exc, exc_info=True)
        try:
            await services.state.transition_status(audit_id, AuditStatus.FAILED)
            await services.state.set_failure_reason(audit_id, str(exc))
        except Exception:
            pass


async def _run(
    audit_id: UUID,
    url: str,
    *,
    services: AppServices,
    screenshot: bool,
) -> None:
    state_svc = services.state
    trace_svc = services.trace

    await state_svc.transition_status(audit_id, AuditStatus.RECON)

    # ── Recon: Playwright crawl ───────────────────────────────────────────────
    _log.info("[%s] Recon: PlaywrightCrawler", str(audit_id)[:8])
    crawl = await run_playwright_crawler(PlaywrightCrawlerInput(url=url))

    # ── Recon: Screenshot (optional) ─────────────────────────────────────────
    screenshot_path: Optional[str] = None
    if screenshot:
        try:
            ss = await run_screenshot_capture(
                ScreenshotCaptureInput(url=crawl.final_url or url)
            )
            screenshot_path = ss.file_path
        except Exception as exc:
            _log.warning("[%s] Screenshot failed (non-fatal): %s", str(audit_id)[:8], exc)

    # ── Recon: Remaining tools (concurrent where independent) ────────────────
    script_urls = [
        r.url for r in crawl.network_requests
        if getattr(r, "resource_type", "") == "script"
    ]
    tech, headers, links, meta, schema_data = await asyncio.gather(
        run_tech_stack_detector(TechStackDetectorInput(
            html=crawl.rendered_html,
            response_headers=crawl.response_headers,
            cookie_names=[],
            script_urls=script_urls,
        )),
        run_header_analyzer(HeaderAnalyzerInput(
            response_headers=crawl.response_headers,
            url=crawl.final_url or url,
        )),
        run_link_extractor(LinkExtractorInput(
            html=crawl.rendered_html,
            base_url=crawl.final_url or url,
        )),
        run_meta_tag_analyzer(MetaTagAnalyzerInput(
            html=crawl.static_html,
            url=crawl.final_url or url,
        )),
        run_structured_data_analyzer(StructuredDataAnalyzerInput(
            html=crawl.static_html,
            url=crawl.final_url or url,
        )),
    )
    _log.info("[%s] Recon complete", str(audit_id)[:8])

    # ── Classification ────────────────────────────────────────────────────────
    await state_svc.transition_status(audit_id, AuditStatus.PLANNING)

    if services.llm and services.llm.is_available:
        cls_data = await _classify_with_llm(url, crawl, tech, meta, schema_data, headers, services.llm)
    else:
        cls_data = _fallback_classification(url)

    rendering_strategy, rendering_evidence = _classify_rendering(crawl, tech)

    # Build recon signals
    recon_signals = []
    for sig in cls_data.get("recon_signals", []):
        try:
            recon_signals.append(ReconSignal(
                area=AgentType(sig["area"]),
                signal=sig["signal"],
                implication=sig["implication"],
                suggested_priority=Severity(sig["suggested_priority"]),
            ))
        except (KeyError, ValueError):
            pass

    goals = [
        SiteGoal(
            goal=g["goal"],
            confidence=float(g.get("confidence", 0.5)),
            signals=g.get("signals", []),
        )
        for g in cls_data.get("primary_goals", [{"goal": "unknown", "confidence": 0.5, "signals": []}])
    ]

    from bs4 import BeautifulSoup as BS
    soup = BS(crawl.static_html, "html.parser")
    h1_tag = soup.find("h1")
    h1_text = h1_tag.get_text(strip=True)[:200] if h1_tag else None

    profile = SiteProfile(
        audit_id=audit_id,
        url=url,
        final_url=crawl.final_url or url,
        category=SiteCategory(cls_data["category"]),
        category_confidence=float(cls_data.get("category_confidence", 0.5)),
        category_reasoning=cls_data.get("category_reasoning", ""),
        rendering_strategy=rendering_strategy,
        rendering_evidence=rendering_evidence,
        tech_stack=_build_tech_stack(tech),
        primary_goals=goals,
        recon_signals=recon_signals,
        page_title=meta.title.text,
        meta_description=meta.meta_description.text if meta.meta_description.is_present else None,
        h1_text=h1_text,
        response_time_ms=crawl.page_timings.load_event_ms,
        http_status_code=crawl.http_status_code,
        redirect_count=len(crawl.redirect_chain),
        is_https=(crawl.final_url or url).startswith("https://"),
        has_javascript_errors=crawl.has_javascript_errors,
        screenshot_path=screenshot_path,
    )

    plan = _build_audit_plan(audit_id, profile)
    _log.info("[%s] Site: %s | Rendering: %s", str(audit_id)[:8], profile.category.value, rendering_strategy.value)

    # ── Populate SharedState ──────────────────────────────────────────────────
    await state_svc.store_recon_artifact(audit_id, "playwright_output", crawl)
    await state_svc.store_recon_artifact(audit_id, "header_analysis", headers)
    await state_svc.store_recon_artifact(audit_id, "link_extraction", links)
    await state_svc.store_recon_artifact(audit_id, "meta_analysis", meta)
    await state_svc.store_recon_artifact(audit_id, "structured_data", schema_data)
    if screenshot_path:
        await state_svc.store_recon_artifact(audit_id, "screenshot_path", screenshot_path)
    await state_svc.set_site_profile(audit_id, profile)
    await state_svc.set_audit_plan(audit_id, plan)

    # ── Agent Runtime ─────────────────────────────────────────────────────────
    await state_svc.transition_status(audit_id, AuditStatus.AUDITING)

    runtime = AgentRuntime(
        state=state_svc,
        trace=trace_svc,
        factory=services.factory,
        registry=services.registry,
        llm_client=services.llm,
    )
    _log.info("[%s] Running specialist agents", str(audit_id)[:8])
    await runtime.run_specialists(audit_id)

    _log.info("[%s] Running synthesis agent", str(audit_id)[:8])
    await runtime.run_synthesis(audit_id)

    final_state = await state_svc.get_state(audit_id)
    _log.info(
        "[%s] Audit complete — status=%s findings=%d",
        str(audit_id)[:8],
        final_state.status.value if final_state else "unknown",
        len(final_state.get_all_findings()) if final_state else 0,
    )
