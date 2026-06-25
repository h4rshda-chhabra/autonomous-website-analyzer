from __future__ import annotations

from typing import List

from app.models import (
    AgentType,
    FindingCategory,
    ImplementationEffort,
    RenderingStrategy,
    Severity,
    TraceEventType,
)
from app.tools.performance.schemas import AssetAnalyzerInput, LighthouseRunnerInput
from .base_agent import BaseAgent


class PerformanceAgent(BaseAgent):
    """
    Measures Core Web Vitals via Lighthouse and analyses page weight/asset issues.
    Reads rendering_strategy from SiteProfile and header_analysis + playwright_output from SharedState.
    """

    def agent_type(self) -> AgentType:
        return AgentType.PERFORMANCE

    def allowed_tools(self) -> List[str]:
        return ["LighthouseRunner", "AssetAnalyzer"]

    async def execute(self) -> None:
        site_profile = await self.get_site_profile()
        audit_plan = await self.get_audit_plan()
        agent_config = audit_plan.get_config(AgentType.PERFORMANCE)

        is_csr = site_profile.rendering_strategy == RenderingStrategy.CSR
        lighthouse_timeout_ms = 150_000 if is_csr else 120_000

        if is_csr:
            await self.emit_trace(
                TraceEventType.REASONING,
                reasoning="CSR rendering detected — increasing Lighthouse timeout to 150s "
                          "and prioritising JS bundle analysis.",
            )

        # Read caching context from header analysis
        header_analysis = await self.get_recon_artifact("header_analysis")
        # HeaderAnalyzerOutput nests caching under .caching.overall_score
        _caching = getattr(header_analysis, "caching", None) if header_analysis else None
        caching_score = getattr(_caching, "overall_score", None) if _caching else None

        playwright_output = await self.get_recon_artifact("playwright_output")
        url = site_profile.final_url or site_profile.url

        # ── Tool 1: LighthouseRunner ──────────────────────────────────────────
        lh_result = await self.run_tool(
            "LighthouseRunner",
            LighthouseRunnerInput(
                url=url,
                form_factor="mobile",
                runs=2,
                categories=["performance"],
            ),
            action_summary=f"Running Lighthouse (mobile, 2 runs) — expected 60–120s",
            timeout_override_ms=lighthouse_timeout_ms,
        )

        cwv_available = False
        if lh_result.success:
            lh = lh_result.data
            cwv_available = True

            lcp_ms = getattr(lh, "lcp_ms", None)
            cls_score = getattr(lh, "cls_score", None)
            tbt_ms = getattr(lh, "tbt_ms", None)
            ttfb_ms = getattr(lh, "ttfb_ms", None)
            perf_score = getattr(lh, "performance_score", None)

            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"Lighthouse complete. Score: {perf_score}/100. "
                            f"LCP: {lcp_ms}ms, CLS: {cls_score}, TBT: {tbt_ms}ms, TTFB: {ttfb_ms}ms.",
            )

            if lcp_ms is not None:
                if lcp_ms > 6000:
                    await self.create_finding(
                        category=FindingCategory.CORE_WEB_VITALS,
                        title=f"LCP is critically slow at {lcp_ms / 1000:.1f}s (threshold: 2.5s)",
                        description=f"Largest Contentful Paint measured at {lcp_ms / 1000:.1f}s on mobile. "
                                    "LCP > 4s is Google's 'Poor' band and directly suppresses organic ranking.",
                        severity=Severity.CRITICAL,
                        business_impact="LCP is a Core Web Vital ranking signal. Sites in the 'Poor' band "
                                        "receive a ranking penalty and experience higher bounce rates. "
                                        "A 1s improvement typically reduces bounce rate by ~7%.",
                        impact_score=9,
                        effort=ImplementationEffort.HARD,
                        effort_hours_min=8,
                        effort_hours_max=40,
                        fix_description="Investigate largest element: likely a hero image, banner, or above-fold text block. "
                                        "Apply: image CDN, lazy loading, font preloading, server-side caching.",
                        tool_name="LighthouseRunner",
                        evidence_raw_data={"lcp_ms": lcp_ms, "threshold_good_ms": 2500, "threshold_poor_ms": 4000},
                        confidence=0.95,
                        metric_value=f"{lcp_ms / 1000:.2f}s",
                        metric_threshold="< 2.5s",
                        tags=["core-web-vital", "ranking"],
                    )
                elif lcp_ms > 4000:
                    await self.create_finding(
                        category=FindingCategory.CORE_WEB_VITALS,
                        title=f"LCP is slow at {lcp_ms / 1000:.1f}s — in the 'Poor' band (threshold: 2.5s)",
                        description=f"LCP of {lcp_ms / 1000:.1f}s puts this page in Google's 'Poor' band. "
                                    "Affects both ranking and user experience.",
                        severity=Severity.HIGH,
                        business_impact="Google uses LCP as a ranking signal. Poor LCP pages rank lower "
                                        "than competitors with faster LCP, all else being equal.",
                        impact_score=8,
                        effort=ImplementationEffort.HARD,
                        effort_hours_min=8,
                        effort_hours_max=24,
                        fix_description="Profile the largest element (usually a hero image). "
                                        "Add preload hints, serve modern formats (WebP/AVIF), and use a CDN.",
                        tool_name="LighthouseRunner",
                        evidence_raw_data={"lcp_ms": lcp_ms},
                        confidence=0.95,
                        metric_value=f"{lcp_ms / 1000:.2f}s",
                        metric_threshold="< 2.5s",
                        tags=["core-web-vital"],
                    )
                elif lcp_ms > 2500:
                    await self.create_finding(
                        category=FindingCategory.CORE_WEB_VITALS,
                        title=f"LCP needs improvement at {lcp_ms / 1000:.1f}s (target: < 2.5s)",
                        description=f"LCP of {lcp_ms / 1000:.1f}s is in the 'Needs Improvement' band (2.5–4s).",
                        severity=Severity.MEDIUM,
                        business_impact="On the borderline of a Google ranking signal penalty. "
                                        "Improving to < 2.5s provides a ranking boost.",
                        impact_score=5,
                        effort=ImplementationEffort.MEDIUM,
                        effort_hours_min=2,
                        effort_hours_max=8,
                        fix_description="Audit the largest above-fold element. Common fixes: image compression, "
                                        "CDN offloading, render-blocking resource removal.",
                        tool_name="LighthouseRunner",
                        evidence_raw_data={"lcp_ms": lcp_ms},
                        confidence=0.95,
                        metric_value=f"{lcp_ms / 1000:.2f}s",
                        metric_threshold="< 2.5s",
                    )

            if cls_score is not None and cls_score > 0.1:
                sev = Severity.HIGH if cls_score > 0.25 else Severity.MEDIUM
                await self.create_finding(
                    category=FindingCategory.CORE_WEB_VITALS,
                    title=f"CLS score of {cls_score:.2f} exceeds the 0.1 threshold",
                    description=f"Cumulative Layout Shift score: {cls_score:.2f}. "
                                "Elements shift visibly during page load, causing misclicks.",
                    severity=sev,
                    business_impact="Layout shifts cause accidental clicks on moved buttons/links, "
                                    "directly harming conversion rates and user trust.",
                    impact_score=7 if sev == Severity.HIGH else 4,
                    effort=ImplementationEffort.MEDIUM,
                    effort_hours_min=2,
                    effort_hours_max=8,
                    fix_description="Set explicit width/height on images. Avoid inserting DOM content above existing content. "
                                    "Reserve space for ads/embeds. Use CSS transforms instead of top/left changes.",
                    tool_name="LighthouseRunner",
                    evidence_raw_data={"cls_score": cls_score},
                    confidence=0.95,
                    metric_value=str(cls_score),
                    metric_threshold="< 0.1",
                    tags=["core-web-vital"],
                )

            if ttfb_ms is not None and ttfb_ms > 800:
                sev = Severity.CRITICAL if ttfb_ms > 1800 else Severity.HIGH
                await self.create_finding(
                    category=FindingCategory.CORE_WEB_VITALS,
                    title=f"TTFB is {ttfb_ms}ms — server response is slow (target: < 200ms)",
                    description=f"Time to First Byte: {ttfb_ms}ms. "
                                "Slow TTFB indicates server processing or network latency issues.",
                    severity=sev,
                    business_impact="Slow TTFB delays every subsequent page load metric. "
                                    "It is the single most impactful metric to fix for overall page speed.",
                    impact_score=8,
                    effort=ImplementationEffort.HARD,
                    effort_hours_min=4,
                    effort_hours_max=24,
                    fix_description="Investigate: server-side rendering time, database queries, "
                                    "no CDN deployed, lack of response caching (Varnish/Redis), or hosting region.",
                    tool_name="LighthouseRunner",
                    evidence_raw_data={"ttfb_ms": ttfb_ms},
                    confidence=0.95,
                    metric_value=f"{ttfb_ms}ms",
                    metric_threshold="< 200ms",
                )

        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"LighthouseRunner failed: {lh_result.error.message if lh_result.error else 'unknown'}. "
                            "CWV findings will be skipped.",
            )

        # ── Tool 2: AssetAnalyzer ─────────────────────────────────────────────
        network_requests = getattr(playwright_output, "network_requests", []) if playwright_output else []
        rendered_html = getattr(playwright_output, "rendered_html", "") if playwright_output else ""

        asset_result = await self.run_tool(
            "AssetAnalyzer",
            AssetAnalyzerInput(
                html=rendered_html,
                base_url=url,
                network_requests=network_requests,
            ),
            action_summary="Analysing page assets for weight, format, and render-blocking issues",
        )

        if asset_result.success:
            assets = asset_result.data
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"Asset analysis: "
                            f"total_js={getattr(assets, 'total_js_kb', '?')}KB, "
                            f"render_blocking={getattr(assets, 'render_blocking_count', '?')}, "
                            f"legacy_images={getattr(assets, 'legacy_image_count', '?')}.",
            )

            rb_count = getattr(assets, "render_blocking_count", 0)
            if rb_count > 0:
                await self.create_finding(
                    category=FindingCategory.RENDER_BLOCKING,
                    title=f"{rb_count} render-blocking resource(s) delay initial page paint",
                    description=f"Found {rb_count} render-blocking script(s) or stylesheet(s) in the <head>. "
                                "These pause HTML parsing until fully downloaded and executed.",
                    severity=Severity.HIGH,
                    business_impact="Render-blocking resources directly delay FCP and LCP, "
                                    "increasing perceived load time and bounce rate.",
                    impact_score=7,
                    effort=ImplementationEffort.MEDIUM,
                    effort_hours_min=2,
                    effort_hours_max=8,
                    fix_description="Add 'defer' or 'async' to non-critical scripts. "
                                    "Move non-critical CSS to be loaded asynchronously.",
                    tool_name="AssetAnalyzer",
                    evidence_raw_data={"render_blocking_count": rb_count},
                    confidence=0.92,
                    metric_value=str(rb_count),
                    metric_threshold="0",
                )

            # Cross-reference: high JS + slow TBT → compound issue for Synthesis
            total_js_kb = getattr(assets, "total_js_kb", 0)
            if total_js_kb > 500 and cwv_available:
                await self.emit_trace(
                    TraceEventType.REASONING,
                    reasoning=f"JS bundle is large ({total_js_kb}KB). If TBT is also high, "
                              "this is a compound issue: the bundle is the likely TBT cause. "
                              "Flagging for Synthesis Agent to create a compound finding.",
                )

        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"AssetAnalyzer failed: {asset_result.error.message if asset_result.error else 'unknown'}. Skipping asset findings.",
            )

        # Cross-reference caching
        if caching_score is not None and caching_score < 60:
            await self.emit_trace(
                TraceEventType.REASONING,
                reasoning=f"Caching score from header analysis is {caching_score}/100. "
                          "If Lighthouse also recommends cache improvements, this is a compound issue.",
            )

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Performance analysis complete. {self._findings_written} finding(s) written.",
        )
        await self.complete()
