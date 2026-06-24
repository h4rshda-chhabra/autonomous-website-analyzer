from __future__ import annotations

from typing import List

from app.models import (
    AgentType,
    AuditDepth,
    FindingCategory,
    ImplementationEffort,
    Severity,
    TraceEventType,
)
from app.tools.seo.schemas import (
    InternalLinkAnalyzerInput,
    MetaTagAnalyzerInput,
    StructuredDataAnalyzerInput,
)
from .base_agent import BaseAgent


class SEOAgent(BaseAgent):
    """
    Analyzes indexability, meta tags, structured data, and internal link quality.
    Reads playwright_output (static_html) and link_extraction from SharedState.
    """

    def agent_type(self) -> AgentType:
        return AgentType.SEO

    def allowed_tools(self) -> List[str]:
        return ["MetaTagAnalyzer", "StructuredDataAnalyzer", "InternalLinkAnalyzer"]

    async def execute(self) -> None:
        site_profile = await self.get_site_profile()
        audit_plan = await self.get_audit_plan()
        agent_config = audit_plan.get_config(AgentType.SEO)

        # Read the recon artifacts this agent is allowed to access
        playwright_output = await self.get_recon_artifact("playwright_output")
        static_html = getattr(playwright_output, "static_html", "") if playwright_output else ""
        url = site_profile.final_url or site_profile.url

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Site category: {site_profile.category.value}. "
                      f"Depth: {agent_config.depth.value}. "
                      f"Priority areas: {agent_config.priority_areas or ['all']}.",
        )

        # ── Tool 1: MetaTagAnalyzer ───────────────────────────────────────────
        meta_result = await self.run_tool(
            "MetaTagAnalyzer",
            MetaTagAnalyzerInput(
                html=static_html,
                url=url,
                site_category=site_profile.category.value,
                primary_goal=site_profile.primary_goals[0].goal if site_profile.primary_goals else None,
            ),
            action_summary="Analyzing meta tags, robots, canonical, and OG tags",
        )

        if meta_result.success:
            meta = meta_result.data
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=(
                    f"Meta tags: title={'present' if meta.title.is_present else 'MISSING'} "
                    f"({meta.title.length_chars or 0} chars), "
                    f"description={'present' if meta.meta_description.is_present else 'MISSING'}, "
                    f"indexable={meta.robots.is_indexable}."
                ),
            )

            # Immediate critical check: noindex
            if not meta.robots.is_indexable:
                await self.emit_trace(
                    TraceEventType.REASONING,
                    reasoning="noindex detected — this is the most severe possible SEO finding. "
                              "Reporting immediately before any other findings.",
                )
                await self.create_finding(
                    category=FindingCategory.CRAWLABILITY,
                    title="Page is blocked from search engine indexing (noindex directive)",
                    description=(
                        f"The page has a robots meta tag or X-Robots-Tag that instructs search engines "
                        f"not to index it. Value: '{meta.robots.meta_robots}'. "
                        "This means the page will not appear in any search engine results."
                    ),
                    severity=Severity.CRITICAL,
                    business_impact=(
                        "This page is completely invisible to search engines. "
                        "Zero organic traffic is possible from this URL until noindex is removed."
                    ),
                    impact_score=10,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Remove the noindex directive from the robots meta tag or X-Robots-Tag header.",
                    tool_name="MetaTagAnalyzer",
                    evidence_raw_data=meta.model_dump() if hasattr(meta, "model_dump") else {},
                    confidence=0.99,
                    metric_value=meta.robots.meta_robots,
                    metric_threshold="index (no noindex directive)",
                )

            # Title findings
            if not meta.title.is_present:
                await self.create_finding(
                    category=FindingCategory.META_TAGS,
                    title="Page title tag is missing",
                    description="No <title> element was found in the page's <head>. "
                                "The title is the primary signal search engines use to understand page topic.",
                    severity=Severity.HIGH,
                    business_impact="Missing title tags cause Google to auto-generate titles, usually resulting "
                                    "in poor click-through rates in search results.",
                    impact_score=7,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Add a descriptive <title> tag to the <head> section. Aim for 30–60 characters.",
                    tool_name="MetaTagAnalyzer",
                    evidence_raw_data={"title_present": False},
                    confidence=0.99,
                    tags=["quick-win"],
                )
            elif not meta.title.is_within_length and meta.title.length_chars is not None:
                await self.create_finding(
                    category=FindingCategory.META_TAGS,
                    title=f"Page title is outside the optimal 30–60 character range ({meta.title.length_chars} chars)",
                    description=f"The title tag is {meta.title.length_chars} characters. "
                                "Google typically displays 50–60 characters, and truncates longer titles.",
                    severity=Severity.MEDIUM,
                    business_impact="Truncated or too-short titles reduce click-through rates from search results.",
                    impact_score=4,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description=f"Rewrite the title to be between 30 and 60 characters. "
                                    f"Current: {meta.title.text!r} ({meta.title.length_chars} chars).",
                    tool_name="MetaTagAnalyzer",
                    evidence_raw_data={"title_text": meta.title.text, "length": meta.title.length_chars},
                    confidence=0.95,
                    metric_value=f"{meta.title.length_chars} chars",
                    metric_threshold="30–60 chars",
                )

            # Meta description findings
            if not meta.meta_description.is_present:
                await self.create_finding(
                    category=FindingCategory.META_TAGS,
                    title="Meta description is missing",
                    description="No meta description tag was found. Google frequently uses the meta description "
                                "as the search result snippet.",
                    severity=Severity.MEDIUM,
                    business_impact="Without a meta description, Google generates one from page content — "
                                    "often producing an unhelpful or truncated snippet that reduces click-through rate.",
                    impact_score=5,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Add a <meta name='description' content='...'> tag. Aim for 120–158 characters "
                                    "with an action word and the page's primary value proposition.",
                    tool_name="MetaTagAnalyzer",
                    evidence_raw_data={"description_present": False},
                    confidence=0.99,
                    tags=["quick-win"],
                )
        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"MetaTagAnalyzer failed: {meta_result.error.message if meta_result.error else 'unknown error'}. Skipping meta tag findings.",
            )

        # ── Tool 2: StructuredDataAnalyzer ────────────────────────────────────
        schema_result = await self.run_tool(
            "StructuredDataAnalyzer",
            StructuredDataAnalyzerInput(
                html=static_html,
                url=url,
                site_category=site_profile.category.value,
            ),
            action_summary="Parsing and validating structured data (JSON-LD, Microdata)",
        )

        if schema_result.success:
            schema = schema_result.data
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"Found {len(schema.schemas_found)} schema(s). "
                            f"Expected missing: {schema.expected_schemas_missing or 'none'}. "
                            f"Parse errors: {schema.has_json_ld_parse_errors}.",
            )

            if schema.has_json_ld_parse_errors:
                await self.create_finding(
                    category=FindingCategory.STRUCTURED_DATA,
                    title="JSON-LD structured data contains parse errors",
                    description="One or more <script type='application/ld+json'> blocks contain invalid JSON. "
                                "Search engines will skip malformed structured data entirely.",
                    severity=Severity.MEDIUM,
                    business_impact="Invalid JSON-LD blocks are silently ignored by Google. "
                                    "You lose rich result eligibility for all types declared in the broken block.",
                    impact_score=5,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=2,
                    fix_description="Validate all JSON-LD blocks with Google's Rich Results Test tool. "
                                    "Fix JSON syntax errors (common issues: trailing commas, unescaped quotes).",
                    tool_name="StructuredDataAnalyzer",
                    evidence_raw_data={"has_json_ld_parse_errors": True},
                    confidence=0.99,
                )

            for missing_schema in schema.expected_schemas_missing:
                await self.create_finding(
                    category=FindingCategory.STRUCTURED_DATA,
                    title=f"Expected {missing_schema} schema is missing for this site type",
                    description=f"For a {site_profile.category.value} site, {missing_schema} schema is expected "
                                "but not present. This schema type unlocks Google rich results.",
                    severity=Severity.MEDIUM,
                    business_impact=f"Missing {missing_schema} schema reduces Google rich result eligibility, "
                                    "potentially lowering click-through rates compared to competitors who have it.",
                    impact_score=5,
                    effort=ImplementationEffort.MEDIUM,
                    effort_hours_min=1,
                    effort_hours_max=4,
                    fix_description=f"Add a {missing_schema} JSON-LD block to the page <head>. "
                                    "Use Google's Structured Data Markup Helper for a template.",
                    tool_name="StructuredDataAnalyzer",
                    evidence_raw_data={"missing_schema": missing_schema, "site_category": site_profile.category.value},
                    confidence=0.90,
                )
        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"StructuredDataAnalyzer failed: {schema_result.error.message if schema_result.error else 'unknown'}. Skipping schema findings.",
            )

        # ── Tool 3: InternalLinkAnalyzer (reads from SharedState, no re-crawl) ─
        link_extraction = await self.get_recon_artifact("link_extraction")
        internal_links = getattr(link_extraction, "links", []) if link_extraction else []
        internal_only = [lk for lk in internal_links if getattr(lk, "is_internal", False)]

        if internal_only:
            link_result = await self.run_tool(
                "InternalLinkAnalyzer",
                InternalLinkAnalyzerInput(
                    internal_links=internal_only,
                    current_url=url,
                    base_domain=url.split("//")[-1].split("/")[0] if "//" in url else url,
                    html=static_html,
                ),
                action_summary=f"Analyzing anchor text quality for {len(internal_only)} internal links",
            )

            if link_result.success:
                link_data = link_result.data
                generic_pct = getattr(link_data, "generic_anchor_text_percentage", 0.0)
                await self.emit_trace(
                    TraceEventType.OBSERVATION,
                    observation=f"Internal link analysis: {len(internal_only)} links, "
                                f"{generic_pct:.0%} with generic anchor text.",
                )

                if generic_pct > 0.20:
                    await self.create_finding(
                        category=FindingCategory.INTERNAL_LINKING,
                        title=f"Over {generic_pct:.0%} of internal links use generic anchor text ('click here', 'read more')",
                        description="Generic anchor text provides no keyword signal to search engines. "
                                    f"Found in {int(generic_pct * len(internal_only))} of {len(internal_only)} internal links.",
                        severity=Severity.LOW,
                        business_impact="Generic anchors dilute internal link equity and keyword signals. "
                                        "Descriptive anchors reinforce the linked page's topic.",
                        impact_score=2,
                        effort=ImplementationEffort.MEDIUM,
                        effort_hours_min=2,
                        effort_hours_max=6,
                        fix_description="Replace generic anchor text with descriptive phrases that describe the destination page's topic.",
                        tool_name="InternalLinkAnalyzer",
                        evidence_raw_data={"generic_anchor_pct": generic_pct},
                        confidence=0.88,
                        metric_value=f"{generic_pct:.0%}",
                        metric_threshold="<20% generic anchors",
                    )

        # ── Cross-agent: check Technical findings for canonical context ────────
        tech_findings = await self.get_prior_findings(AgentType.TECHNICAL)
        canonical_issue = next(
            (f for f in tech_findings if "canonical" in f.title.lower()),
            None,
        )
        if canonical_issue:
            await self.emit_trace(
                TraceEventType.REASONING,
                reasoning="TechnicalAgent found a canonical mismatch. This compounds any SEO findings "
                          "on this page. Synthesis Agent should create a compound finding.",
            )

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"SEO analysis complete. {self._findings_written} finding(s) written.",
        )
        await self.complete()
