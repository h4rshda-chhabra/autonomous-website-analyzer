#!/usr/bin/env python3
"""
Autonomous Website Analyzer — CLI Smoke Test
============================================
Usage:
    python main.py <url>
    python main.py <url> --api-key <anthropic_api_key>
    python main.py <url> --no-screenshot

Runs the full Phase 1 recon + classification pipeline and prints:
  - PlaywrightCrawlerOutput   (raw page data)
  - TechStackDetectorOutput   (detected technologies)
  - HeaderAnalyzerOutput      (HTTP header security / caching scores)
  - LinkExtractorOutput       (internal + external link counts)
  - MetaTagAnalyzerOutput     (SEO meta data)
  - StructuredDataAnalyzerOutput (schema.org structured data)
  - SiteProfile               (Claude-classified site identity)
  - AuditPlan                 (agent configuration + priorities)

No FastAPI / Redis / PostgreSQL / Celery / frontend required.
All tool calls bypass ToolExecutorImpl — tools are invoked directly.
Claude is called once for site classification (SiteProfile.category + goals).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

# ── Force UTF-8 output on Windows (cmd.exe/PowerShell default to cp1252) ────
if sys.platform == "win32":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Path setup ───────────────────────────────────────────────────────────────
# Allow `python main.py` from the backend directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Tool imports ─────────────────────────────────────────────────────────────
from app.tools.recon.tools import (
    run_playwright_crawler,
    run_header_analyzer,
    run_tech_stack_detector,
    run_link_extractor,
    run_screenshot_capture,
)
from app.tools.seo.tools import (
    run_meta_tag_analyzer,
    run_structured_data_analyzer,
    run_internal_link_analyzer,
)
from app.tools.recon.schemas import (
    PlaywrightCrawlerInput,
    HeaderAnalyzerInput,
    TechStackDetectorInput,
    LinkExtractorInput,
    ScreenshotCaptureInput,
)
from app.tools.seo.schemas import (
    MetaTagAnalyzerInput,
    StructuredDataAnalyzerInput,
    InternalLinkAnalyzerInput,
)

# ── Model imports ─────────────────────────────────────────────────────────────
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

# ── Infrastructure ────────────────────────────────────────────────────────────
from app.infrastructure.settings import settings


# ═══════════════════════════════════════════════════════════════════════════════
# Terminal formatting helpers
# ═══════════════════════════════════════════════════════════════════════════════

_W = 72   # line width for section banners


def _banner(title: str) -> None:
    print(f"\n{'─' * _W}")
    print(f"  {title}")
    print(f"{'─' * _W}")


def _ok(label: str, value: Any) -> None:
    print(f"  {'✓' if value else '·'} {label:<38} {value}")


def _kv(label: str, value: Any, indent: int = 2) -> None:
    pad = " " * indent
    print(f"{pad}{label:<40} {value}")


def _warn(msg: str) -> None:
    print(f"  ⚠  {msg}")


def _step(n: int, total: int, msg: str) -> None:
    print(f"\n[{n}/{total}] {msg}...", flush=True)


def _elapsed(start: float) -> str:
    return f"{time.monotonic() - start:.1f}s"


def _fmt_tech(dt: Any) -> str:
    return f"{dt.name} ({dt.confidence:.0%})" if dt else "(none)"


def _generate_findings(meta: Any, schema_data: Any, crawl: Any, headers: Any) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    js_errors = [m for m in crawl.console_messages if m.level == "error"]

    if not meta.title.is_present:
        findings.append({"severity": "HIGH", "area": "SEO", "title": "Missing page title"})
    elif not meta.title.is_within_length:
        findings.append({"severity": "MEDIUM", "area": "SEO",
                         "title": f"Title length out of range ({meta.title.length_chars} chars, target 30–60)"})
    if not meta.meta_description.is_present:
        findings.append({"severity": "HIGH", "area": "SEO", "title": "Missing meta description"})
    if not meta.canonical_url:
        findings.append({"severity": "HIGH", "area": "SEO", "title": "Missing canonical URL"})
    if not meta.open_graph.is_complete:
        findings.append({"severity": "MEDIUM", "area": "SEO", "title": "Incomplete Open Graph tags (og:title/description/image)"})
    if not meta.twitter_card.card_type:
        findings.append({"severity": "LOW", "area": "SEO", "title": "Missing Twitter Card"})
    if not schema_data.has_organization_schema:
        findings.append({"severity": "MEDIUM", "area": "SEO", "title": "No Organization schema"})
    if not schema_data.has_website_schema:
        findings.append({"severity": "MEDIUM", "area": "SEO", "title": "No WebSite schema"})
    if js_errors:
        findings.append({"severity": "HIGH", "area": "Technical",
                         "title": f"{len(js_errors)} JavaScript error(s) on page load"})
    if headers.security.overall_score < 50:
        findings.append({"severity": "HIGH", "area": "Technical",
                         "title": f"Low security header score ({headers.security.overall_score}/100)"})
    if not headers.security.content_security_policy.present:
        findings.append({"severity": "MEDIUM", "area": "Technical", "title": "Missing Content-Security-Policy header"})
    if not headers.security.strict_transport_security.present:
        findings.append({"severity": "MEDIUM", "area": "Technical", "title": "Missing HSTS (Strict-Transport-Security)"})

    return findings


def _print_findings(findings: List[Dict[str, Any]]) -> None:
    if not findings:
        return
    _banner("Critical Findings")
    for severity in ("HIGH", "MEDIUM", "LOW"):
        group = [f for f in findings if f["severity"] == severity]
        if not group:
            continue
        print(f"\n  {severity}")
        for f in group:
            print(f"  - [{f['area']:<10}] {f['title']}")


def _build_json_report(
    audit_id: Any,
    url: str,
    profile: Any,
    meta: Any,
    schema_data: Any,
    headers: Any,
    links: Any,
    crawl: Any,
    findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    from datetime import datetime, timezone
    js_errors = [m for m in crawl.console_messages if m.level == "error"]
    return {
        "audit_id": str(audit_id),
        "url": url,
        "final_url": crawl.final_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "classification": {
            "category": profile.category.value,
            "confidence": profile.category_confidence,
            "rendering_strategy": profile.rendering_strategy.value,
        },
        "tech_stack": {
            "meta_framework": profile.tech_stack.meta_framework,
            "frontend_framework": profile.tech_stack.frontend_framework,
            "cms": profile.tech_stack.cms,
            "cdn": profile.tech_stack.cdn,
            "analytics": profile.tech_stack.analytics,
        },
        "seo": {
            "title": meta.title.text,
            "title_length": meta.title.length_chars,
            "meta_description": meta.meta_description.is_present,
            "canonical": meta.canonical_url,
            "canonical_matches_page": meta.canonical_matches_page_url,
            "og_complete": meta.open_graph.is_complete,
            "twitter_card": meta.twitter_card.card_type,
            "is_indexable": meta.robots.is_indexable,
            "lang": meta.lang_attribute,
        },
        "structured_data": {
            "json_ld_count": schema_data.json_ld_count,
            "microdata_count": schema_data.microdata_count,
            "has_organization_schema": schema_data.has_organization_schema,
            "has_website_schema": schema_data.has_website_schema,
            "schemas_found": [s.schema_type for s in schema_data.schemas_found],
            "expected_missing": schema_data.expected_schemas_missing,
        },
        "technical": {
            "http_status": crawl.http_status_code,
            "is_https": profile.is_https,
            "redirect_count": len(crawl.redirect_chain),
            "js_error_count": len(js_errors),
            "js_errors": [{"text": m.text, "source_url": m.source_url, "line": m.line_number}
                          for m in js_errors],
            "security_score": headers.security.overall_score,
            "caching_score": headers.caching.overall_score,
        },
        "performance": {
            "ttfb_ms": crawl.page_timings.ttfb_ms,
            "dom_content_loaded_ms": crawl.page_timings.dom_content_loaded_ms,
            "load_event_ms": crawl.page_timings.load_event_ms,
            "total_requests": crawl.total_requests,
        },
        "links": {
            "internal": links.total_internal,
            "external": links.total_external,
        },
        "findings": findings,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rendering strategy classifier (deterministic — no AI needed)
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_rendering(
    crawl: Any,
    tech: Any,
) -> Tuple[RenderingStrategy, RenderingEvidence]:
    """
    Classifies rendering strategy from word-count ratio + technology signals.

    Decision tree:
      parity > 0.80 → content exists in static HTML → server-rendered family
        + ssr_headers + hydration  → HYBRID  (Next.js App Router, Nuxt 3, etc.)
        + ssr_headers only         → SSR     (traditional server rendering)
        + meta_framework=Gatsby    → SSG     (pre-built static)
        + else                     → SSR     (plain server HTML)
      parity < 0.25 → almost no static content → client-side SPA → CSR
      0.25–0.80     → partial static content
        + hydration                → HYBRID
        + else                     → UNKNOWN (insufficient signal)
    """
    initial = getattr(crawl, "static_word_count", 0) or 0
    rendered = getattr(crawl, "rendered_word_count", 0) or 0
    ratio = rendered / max(1, initial)

    js_detected = bool(
        getattr(tech, "frontend_framework", None)
        or getattr(tech, "meta_framework", None)
    )
    ssr_headers = getattr(tech, "ssr_headers_present", False)
    hydration = getattr(tech, "hydration_markers_present", False)

    meta_fw_name = (getattr(tech, "meta_framework", None) or None)
    meta_fw_name = meta_fw_name.name if meta_fw_name and hasattr(meta_fw_name, "name") else ""

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
        elif "gatsby" in meta_fw_name.lower() or "astro" in meta_fw_name.lower():
            strategy = RenderingStrategy.SSG
        else:
            strategy = RenderingStrategy.SSR
    elif ratio < 0.25:
        strategy = RenderingStrategy.CSR
    else:
        strategy = RenderingStrategy.HYBRID if hydration else RenderingStrategy.UNKNOWN

    return strategy, evidence


# ═══════════════════════════════════════════════════════════════════════════════
# TechStack builder from TechStackDetectorOutput
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# Claude site classification
# ═══════════════════════════════════════════════════════════════════════════════

_CLASSIFICATION_TOOL = {
    "name": "classify_website",
    "description": (
        "Classify the website based on the provided crawl data, technology stack, "
        "and page content. Return structured classification."
    ),
    "input_schema": {
        "type": "object",
        "required": ["category", "category_confidence", "category_reasoning", "primary_goals"],
        "properties": {
            "category": {
                "type": "string",
                "enum": [c.value for c in SiteCategory],
                "description": "Primary website category",
            },
            "category_confidence": {
                "type": "number",
                "description": "Confidence in classification, 0.0–1.0",
            },
            "category_reasoning": {
                "type": "string",
                "description": "One or two sentences explaining the classification",
            },
            "primary_goals": {
                "type": "array",
                "description": "Top 1–3 primary goals of this website",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["goal", "confidence", "signals"],
                    "properties": {
                        "goal": {"type": "string"},
                        "confidence": {"type": "number"},
                        "signals": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "recon_signals": {
                "type": "array",
                "description": "Notable anomalies or opportunities worth investigating",
                "items": {
                    "type": "object",
                    "required": ["area", "signal", "implication", "suggested_priority"],
                    "properties": {
                        "area": {
                            "type": "string",
                            "enum": ["seo", "performance", "accessibility", "content", "technical"],
                        },
                        "signal": {"type": "string"},
                        "implication": {"type": "string"},
                        "suggested_priority": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low", "info"],
                        },
                    },
                },
            },
        },
    },
}


def _classify_with_claude(
    url: str,
    crawl: Any,
    tech: Any,
    meta: Any,
    schema_data: Any,
    headers: Any,
    api_key: str,
) -> Dict[str, Any]:
    """
    Calls Claude claude-sonnet-4-6 with the recon data to classify the website.
    Uses Anthropic tool_use for structured JSON output (guaranteed valid schema).
    Sync function — caller must use asyncio.to_thread() to avoid blocking the event loop.
    Returns the parsed tool input dict.
    """
    import anthropic

    # Build a compact but information-rich prompt.
    tech_lines = []
    for attr in ("meta_framework", "frontend_framework", "cms", "ecommerce_platform", "cdn"):
        obj = getattr(tech, attr, None)
        if obj:
            tech_lines.append(f"  {attr}: {obj.name} (confidence={obj.confidence:.0%})")
    analytics = [t.name for t in getattr(tech, "analytics_tools", [])]
    if analytics:
        tech_lines.append(f"  analytics: {', '.join(analytics)}")

    title = getattr(meta, "title", None)
    title_text = title.text if title else "(none)"
    desc = getattr(meta, "meta_description", None)
    desc_text = (desc.text or "(none)") if desc else "(none)"
    h1_text = "(not extracted)"  # heading extraction is in InternalLinkAnalyzer

    # Security score from header analysis
    security_score = getattr(getattr(headers, "security", None), "overall_score", "?")
    caching_score = getattr(getattr(headers, "caching", None), "overall_score", "?")
    is_https = getattr(getattr(headers, "server_info", None), "is_https", True)

    schemas = [s.schema_type for s in getattr(schema_data, "schemas_found", [])]

    # Truncate static HTML to stay well within token budget
    html_excerpt = crawl.static_html[:3000]

    prompt = textwrap.dedent(f"""\
        Analyse this website and classify it. Use the evidence below.

        URL: {url}
        Final URL: {crawl.final_url}
        HTTP Status: {crawl.http_status_code}
        HTTPS: {is_https}
        Redirects: {len(crawl.redirect_chain)}

        Page Metadata:
          Title: {title_text}
          Meta Description: {desc_text}

        Technology Stack:
        {chr(10).join(tech_lines) if tech_lines else '  (none detected)'}

        Structured Data Present: {schemas or '(none)'}

        Content Volume:
          Static word count (pre-JS):  {crawl.static_word_count}
          Rendered word count (post-JS): {crawl.rendered_word_count}
          JS errors on load: {crawl.has_javascript_errors}

        HTTP Header Scores (0–100):
          Security: {security_score}
          Caching:  {caching_score}

        Static HTML excerpt (first 3000 chars):
        ---
        {html_excerpt}
        ---

        Classify this website's category, primary goals (1–3), and any notable signals.
    """)

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_classification_max_tokens,
        tools=[_CLASSIFICATION_TOOL],
        tool_choice={"type": "tool", "name": "classify_website"},
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract the tool_use block from the response.
    for block in message.content:
        if block.type == "tool_use" and block.name == "classify_website":
            return block.input

    raise RuntimeError("Claude did not return a classify_website tool call")


def _fallback_classification(url: str) -> Dict[str, Any]:
    """Used when the Anthropic API key is not configured."""
    return {
        "category": SiteCategory.OTHER.value,
        "category_confidence": 0.50,
        "category_reasoning": (
            "Fallback classification — no Anthropic API key configured. "
            "Set ANTHROPIC_API_KEY in environment or pass --api-key."
        ),
        "primary_goals": [
            {
                "goal": "unknown",
                "confidence": 0.50,
                "signals": ["No Claude classification available"],
            }
        ],
        "recon_signals": [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Deterministic AuditPlan generator (category-aware)
# ═══════════════════════════════════════════════════════════════════════════════

# Maps site category to agents that deserve DEEP focus and their priority areas.
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


def _build_audit_plan(audit_id: Any, profile: SiteProfile) -> AuditPlan:
    """
    Builds a tailored AuditPlan from the SiteProfile.

    DEEP depth is given to agents whose domain is most important for the site category.
    STANDARD depth is given to the remaining specialist agents.
    The Synthesis agent is always enabled (runs after all specialists complete).
    """
    category = profile.category
    deep = set(_DEEP_AGENTS.get(category, []))
    priority_map = _PRIORITY_AREAS.get(category, {})

    specialist_agents = [a for a in AgentType if a not in (AgentType.ORCHESTRATOR,)]

    agent_configs: Dict[AgentType, AgentConfig] = {}
    parallel: List[AgentType] = []

    for agent in specialist_agents:
        if agent == AgentType.SYNTHESIS:
            # Synthesis always runs last — not parallel with specialists.
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

    deep_names = [a.value for a in deep if a != AgentType.SYNTHESIS]
    std_names = [a.value for a in parallel if a not in deep]

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


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

async def run_pipeline(url: str, api_key: str, screenshot: bool = True) -> None:
    audit_id = uuid4()
    total_steps = 8 if screenshot else 7
    pipeline_start = time.monotonic()

    print(f"\n{'═' * _W}")
    print(f"  Autonomous Website Analyzer — Phase 1 Smoke Test")
    print(f"  Audit ID: {audit_id}")
    print(f"  Target:   {url}")
    print(f"{'═' * _W}")

    # ── Step 1: Playwright crawl ───────────────────────────────────────────────
    _step(1, total_steps, "PlaywrightCrawler — headless Chromium navigation")
    t = time.monotonic()
    try:
        crawl = await run_playwright_crawler(PlaywrightCrawlerInput(url=url))
    except ConnectionError as e:
        print(f"\n  FATAL — URL unreachable: {e}")
        sys.exit(1)
    except TimeoutError as e:
        print(f"\n  FATAL — Crawl timed out: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n  FATAL — PlaywrightCrawler error: {type(e).__name__}: {e}")
        sys.exit(1)

    _banner("PlaywrightCrawler")
    _kv("URL crawled:", crawl.url)
    _kv("Final URL:", crawl.final_url)
    _kv("HTTP status:", crawl.http_status_code)
    _kv("Redirects:", len(crawl.redirect_chain))
    _kv("Static word count:", crawl.static_word_count)
    _kv("Rendered word count:", crawl.rendered_word_count)
    _kv("Content parity ratio:", f"{crawl.rendered_word_count / max(1, crawl.static_word_count):.2f}")
    _kv("Network requests:", crawl.total_requests)
    _js_page_errors = [m for m in crawl.console_messages if m.level == "error"]
    _kv("JS Errors Found:", f"{len(_js_page_errors)} error(s)" if _js_page_errors else "None")
    for _i, _e in enumerate(_js_page_errors[:5], 1):
        _src = f"  ({_e.source_url.rsplit('/', 1)[-1]})" if _e.source_url else ""
        print(f"    {_i}. {_e.text[:120]}{_src}")
    _kv("TTFB:", f"{crawl.page_timings.ttfb_ms}ms" if crawl.page_timings.ttfb_ms else "n/a")
    _kv("DOM Content Loaded:", f"{crawl.page_timings.dom_content_loaded_ms}ms" if crawl.page_timings.dom_content_loaded_ms else "n/a")
    _kv("Load event:", f"{crawl.page_timings.load_event_ms}ms" if crawl.page_timings.load_event_ms else "n/a")
    if crawl.redirect_chain:
        for hop in crawl.redirect_chain:
            _kv(f"  {hop.status_code} →", hop.location or hop.url, indent=4)
    print(f"  Elapsed: {_elapsed(t)}")

    # ── Step 2: Screenshot (concurrent in full pipeline, sequential in CLI) ───
    screenshot_path: Optional[str] = None
    if screenshot:
        _step(2, total_steps, "ScreenshotCapture — full-page PNG")
        t = time.monotonic()
        try:
            ss = await run_screenshot_capture(
                ScreenshotCaptureInput(url=crawl.final_url or url)
            )
            screenshot_path = ss.file_path
            _banner("ScreenshotCapture")
            _kv("Saved to:", ss.file_path)
            _kv("Dimensions:", f"{ss.page_width_px}×{ss.page_height_px}px")
            _kv("File size:", f"{ss.file_size_bytes / 1024:.1f} KB")
            print(f"  Elapsed: {_elapsed(t)}")
        except Exception as e:
            _warn(f"ScreenshotCapture failed (non-fatal): {type(e).__name__}: {e}")

    # ── Step 3: TechStackDetector ─────────────────────────────────────────────
    step = 3 if screenshot else 2
    _step(step, total_steps, "TechStackDetector — fingerprinting technology stack")
    t = time.monotonic()
    script_urls = [
        r.url for r in crawl.network_requests
        if getattr(r, "resource_type", "") == "script"
    ]
    tech = await run_tech_stack_detector(TechStackDetectorInput(
        html=crawl.rendered_html,
        response_headers=crawl.response_headers,
        cookie_names=[],
        script_urls=script_urls,
    ))
    _banner("TechStackDetector")
    _kv("Meta framework:", _fmt_tech(tech.meta_framework))
    _kv("Frontend framework:", _fmt_tech(tech.frontend_framework))
    _kv("CMS:", _fmt_tech(tech.cms))
    _kv("Ecommerce:", _fmt_tech(tech.ecommerce_platform))
    _kv("CDN:", _fmt_tech(tech.cdn))
    _kv("Analytics:", [f"{a.name} ({a.confidence:.0%})" for a in tech.analytics_tools] or "(none)")
    _kv("Tag manager:", _fmt_tech(tech.tag_manager))
    _kv("Chat widget:", _fmt_tech(tech.chat_widget))
    _kv("Error tracking:", _fmt_tech(tech.error_tracking))
    if tech.meta_framework and tech.cms:
        _warn(f"Migration likely: {tech.meta_framework.name} + {tech.cms.name} co-exist — verify rendering strategy")
    _kv("SSR headers present:", tech.ssr_headers_present)
    _kv("Hydration markers:", tech.hydration_markers_present)
    _kv("Total detected:", len(tech.detected_technologies))
    print(f"  Elapsed: {_elapsed(t)}")

    # ── Step 4: HeaderAnalyzer ────────────────────────────────────────────────
    step += 1
    _step(step, total_steps, "HeaderAnalyzer — scoring HTTP response headers")
    t = time.monotonic()
    headers = await run_header_analyzer(HeaderAnalyzerInput(
        response_headers=crawl.response_headers,
        url=crawl.final_url or url,
    ))
    _banner("HeaderAnalyzer")
    print(f"  {'Security score':<38} {headers.security.overall_score}/100")
    print(f"  {'Caching score':<38} {headers.caching.overall_score}/100")
    _kv("HTTPS:", headers.server_info.is_https)
    _kv("HTTP version:", headers.server_info.http_version or "HTTP/1.1 (assumed)")
    _kv("CSP:", f"{'✓' if headers.security.content_security_policy.present else '✗'}  score={headers.security.content_security_policy.score}/10")
    _kv("HSTS:", f"{'✓' if headers.security.strict_transport_security.present else '✗'}  score={headers.security.strict_transport_security.score}/10")
    _kv("X-Frame-Options:", f"{'✓' if headers.security.x_frame_options.present else '✗'}  score={headers.security.x_frame_options.score}/10")
    _kv("XCTO:", f"{'✓' if headers.security.x_content_type_options.present else '✗'}  score={headers.security.x_content_type_options.score}/10")
    _kv("Cache-Control:", f"{'✓' if headers.caching.cache_control.present else '✗'}  score={headers.caching.cache_control.score}/10")
    if headers.caching.cdn_cache_status:
        _kv("CDN cache:", headers.caching.cdn_cache_status.assessment[:60])
    print(f"  Elapsed: {_elapsed(t)}")

    # ── Step 5: LinkExtractor ─────────────────────────────────────────────────
    step += 1
    _step(step, total_steps, "LinkExtractor — mapping page link graph")
    t = time.monotonic()
    links = await run_link_extractor(LinkExtractorInput(
        html=crawl.rendered_html,
        base_url=crawl.final_url or url,
    ))
    _banner("LinkExtractor")
    _kv("Internal links:", links.total_internal)
    _kv("External links:", links.total_external)
    _kv("Asset references:", len(links.asset_references))
    _kv("Internal nofollow:", links.internal_nofollow_count)
    _kv("JS-only hrefs:", links.javascript_href_count)
    _kv("New-tab internal:", links.new_tab_internal_count)
    print(f"  Sample internal URLs:")
    for lk in links.internal_links[:5]:
        print(f"    {lk.normalized_url}")
    print(f"  Elapsed: {_elapsed(t)}")

    # ── Step 6: MetaTagAnalyzer ───────────────────────────────────────────────
    step += 1
    _step(step, total_steps, "MetaTagAnalyzer — SEO meta data analysis")
    t = time.monotonic()
    meta = await run_meta_tag_analyzer(MetaTagAnalyzerInput(
        html=crawl.static_html,
        url=crawl.final_url or url,
    ))
    _banner("MetaTagAnalyzer")
    _kv("Title:", f"{meta.title.text!r}" if meta.title.text else "(missing)")
    _kv("Title length:", f"{meta.title.length_chars} chars — {'OK' if meta.title.is_within_length else 'OUT OF RANGE'}" if meta.title.length_chars else "n/a")
    _kv("Meta description:", "present" if meta.meta_description.is_present else "MISSING")
    if meta.meta_description.is_present:
        _kv("  Length:", f"{meta.meta_description.length_chars} chars — {'OK' if meta.meta_description.is_within_length else 'out of range'}")
        _kv("  CTA signal:", meta.meta_description.has_cta_signal)
    _kv("Canonical URL:", meta.canonical_url or "(none)")
    _kv("Canonical matches page:", meta.canonical_matches_page_url)
    _kv("Indexable:", meta.robots.is_indexable)
    _kv("Followable:", meta.robots.is_followable)
    _kv("OG complete:", meta.open_graph.is_complete)
    _kv("Twitter card:", meta.twitter_card.card_type or "(none)")
    _kv("Viewport meta:", "present" if meta.viewport_meta else "missing")
    _kv("Charset declared:", meta.charset_declared)
    _kv("Lang attribute:", meta.lang_attribute or "(none)")
    print(f"  Elapsed: {_elapsed(t)}")

    # ── Step 7: StructuredDataAnalyzer ────────────────────────────────────────
    step += 1
    _step(step, total_steps, "StructuredDataAnalyzer — schema.org validation")
    t = time.monotonic()
    schema_data = await run_structured_data_analyzer(StructuredDataAnalyzerInput(
        html=crawl.static_html,
        url=crawl.final_url or url,
    ))
    _banner("StructuredDataAnalyzer")
    _kv("JSON-LD blocks:", schema_data.json_ld_count)
    _kv("Microdata blocks:", schema_data.microdata_count)
    _kv("Parse errors:", schema_data.has_json_ld_parse_errors)
    _kv("Has Organization schema:", schema_data.has_organization_schema)
    _kv("Has WebSite schema:", schema_data.has_website_schema)
    for s in schema_data.schemas_found:
        valid_str = "valid" if s.is_valid else f"INVALID (missing: {s.missing_required_properties})"
        rich_str = "rich-result eligible" if s.google_rich_result_eligible else ""
        print(f"    Schema: {s.schema_type:<30} {valid_str}  {rich_str}")
    if schema_data.expected_schemas_missing:
        _warn(f"Expected schemas missing: {schema_data.expected_schemas_missing}")
    print(f"  Elapsed: {_elapsed(t)}")

    # ── Step 8: Claude site classification ────────────────────────────────────
    step += 1
    using_claude = bool(api_key)
    _step(step, total_steps,
          f"Claude classification — {settings.anthropic_model}" if using_claude
          else "Deterministic fallback classification (no API key)")
    t = time.monotonic()

    if using_claude:
        try:
            # _classify_with_claude is sync (uses blocking Anthropic client).
            # asyncio.to_thread runs it in a thread pool so the event loop stays responsive.
            cls_data = await asyncio.to_thread(
                _classify_with_claude,
                url, crawl, tech, meta, schema_data, headers, api_key,
            )
        except Exception as e:
            _warn(f"Claude call failed ({type(e).__name__}: {e}) — using fallback classification")
            cls_data = _fallback_classification(url)
    else:
        cls_data = _fallback_classification(url)

    # Determine rendering strategy deterministically.
    rendering_strategy, rendering_evidence = _classify_rendering(crawl, tech)

    # Build SiteProfile.
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

    # Extract H1 from static HTML (quick BeautifulSoup pass)
    from bs4 import BeautifulSoup as BS
    soup_h1 = BS(crawl.static_html, "html.parser")
    h1_tag = soup_h1.find("h1")
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

    _banner("SiteProfile")
    _kv("Category:", f"{profile.category.value}  (confidence={profile.category_confidence:.0%})")
    _kv("Reasoning:", profile.category_reasoning[:100])
    _kv("Rendering strategy:", profile.rendering_strategy.value)
    _kv("Content parity ratio:", rendering_evidence.content_parity_ratio)
    _kv("SSR headers:", rendering_evidence.ssr_headers_detected)
    _kv("Hydration markers:", rendering_evidence.hydration_markers_detected)
    print(f"  {'Primary goals:':<40}")
    for g in profile.primary_goals:
        _kv(f"  {g.goal}", f"(confidence={g.confidence:.0%})", indent=4)
    if profile.recon_signals:
        print(f"  {'Recon signals:':<40}")
        for sig in profile.recon_signals:
            _kv(f"  [{sig.suggested_priority.value.upper()}] {sig.area.value}:", sig.signal[:80], indent=4)
    _kv("Page title:", profile.page_title or "(none)")
    _kv("H1:", profile.h1_text or "(none)")
    _kv("Is HTTPS:", profile.is_https)
    _kv("JS errors:", profile.has_javascript_errors)
    _kv("Screenshot:", profile.screenshot_path or "(skipped)")
    print(f"  Elapsed: {_elapsed(t)}")

    # ── AuditPlan ─────────────────────────────────────────────────────────────
    plan = _build_audit_plan(audit_id, profile)

    _banner("AuditPlan")
    _kv("Enabled agents:", len(plan.enabled_agents))
    _kv("Parallel agents:", [a.value for a in plan.parallel_agents])
    _kv("Deep-focus agents:", [a.value for a in plan.deep_agents])
    for agent, cfg in plan.agent_configs.items():
        if agent == AgentType.SYNTHESIS:
            continue
        depth_str = f"{'DEEP  ' if cfg.depth == AuditDepth.DEEP else 'std   '}"
        prio_str = f"  priority={cfg.priority_areas}" if cfg.priority_areas else ""
        print(f"    {agent.value:<20} {depth_str}{prio_str}")
    _kv("Plan rationale:", plan.rationale.site_summary[:100])
    _kv("Est. duration:", f"{plan.rationale.estimated_duration_seconds}s")

    # ── Findings ──────────────────────────────────────────────────────────────
    findings = _generate_findings(meta, schema_data, crawl, headers)
    _print_findings(findings)

    # ── JSON report ───────────────────────────────────────────────────────────
    report = _build_json_report(audit_id, url, profile, meta, schema_data, headers, links, crawl, findings)
    from urllib.parse import urlparse as _urlparse
    _domain = _urlparse(url).netloc.replace(".", "_").replace(":", "_")
    _json_path = f"audit_{_domain}_{str(audit_id)[:8]}.json"
    with open(_json_path, "w", encoding="utf-8") as _jf:
        json.dump(report, _jf, indent=2, ensure_ascii=False, default=str)
    print(f"\n  JSON report saved → {_json_path}")

    # ── Pipeline summary ──────────────────────────────────────────────────────
    print(f"\n{'═' * _W}")
    print(f"  Pipeline complete in {_elapsed(pipeline_start)}")
    print(f"  Site: {profile.category.value.upper()} — "
          f"{rendering_strategy.value.upper()} — "
          f"{'HTTPS' if profile.is_https else 'HTTP'}")
    print(f"  Tech: "
          f"{profile.tech_stack.meta_framework or profile.tech_stack.frontend_framework or profile.tech_stack.cms or 'unknown stack'}"
          f" / CDN: {profile.tech_stack.cdn or 'none'}")
    print(f"  SEO: title={'✓' if meta.title.is_present else '✗'}  "
          f"desc={'✓' if meta.meta_description.is_present else '✗'}  "
          f"canonical={'✓' if meta.canonical_url else '✗'}  "
          f"indexable={'✓' if meta.robots.is_indexable else '✗'}")
    print(f"  Security: {headers.security.overall_score}/100  "
          f"Caching: {headers.caching.overall_score}/100")
    print(f"  Links: {links.total_internal} internal / {links.total_external} external")
    print(f"  Structured data: {schema_data.json_ld_count} JSON-LD  "
          f"missing={schema_data.expected_schemas_missing or 'none'}")
    print(f"{'═' * _W}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Autonomous Website Analyzer — Phase 1 CLI smoke test",
    )
    p.add_argument("url", help="Target URL to analyze (must be absolute http:// or https://)")
    p.add_argument(
        "--api-key",
        default=os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key,
        help="Anthropic API key (defaults to ANTHROPIC_API_KEY env var or settings)",
    )
    p.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Skip screenshot capture (saves ~5s)",
    )
    return p.parse_args()


async def _async_main() -> None:
    args = _parse_args()
    url = args.url
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    api_key = args.api_key or ""
    if not api_key:
        print("\n  Note: No Anthropic API key — using deterministic fallback classification.")
        print("  Set ANTHROPIC_API_KEY or pass --api-key to enable Claude classification.\n")

    await run_pipeline(url, api_key=api_key, screenshot=not args.no_screenshot)


if __name__ == "__main__":
    # On Windows asyncio.run() with Playwright requires ProactorEventLoop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(_async_main())
