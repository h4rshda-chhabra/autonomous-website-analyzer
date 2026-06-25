from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.models import (
    AgentConfig,
    AgentStatus,
    AgentType,
    AuditDepth,
    AuditPlan,
    AuditStatus,
    FindingCategory,
    ImplementationEffort,
    PlanRationale,
    RenderingStrategy,
    Severity,
    SiteCategory,
    SiteProfile,
    TraceEventType,
)
from app.models.site_profile import TechStack, SiteGoal
from app.tools.recon.schemas import (
    HeaderAnalyzerInput,
    LinkExtractorInput,
    PlaywrightCrawlerInput,
    ScreenshotCaptureInput,
    TechStackDetectorInput,
)
from .base_agent import BaseAgent


class OrchestratorAgent(BaseAgent):
    """
    Phase A — Reconnaissance: runs 5 recon tools sequentially/concurrently.
    Phase B — Planning: classifies site + generates AuditPlan via Claude (stubbed).
    Phase C — Coordination: dispatches specialist agents, monitors, triggers Synthesis.
    """

    SPECIALIST_AGENTS = [
        AgentType.SEO,
        AgentType.PERFORMANCE,
        AgentType.ACCESSIBILITY,
        AgentType.CONTENT,
        AgentType.TECHNICAL,
    ]

    def agent_type(self) -> AgentType:
        return AgentType.ORCHESTRATOR

    def allowed_tools(self) -> List[str]:
        return [
            "PlaywrightCrawler",
            "ScreenshotCapture",
            "TechStackDetector",
            "HeaderAnalyzer",
            "LinkExtractor",
        ]

    async def execute(self) -> None:
        await self._phase_a_recon()
        await self._phase_b_planning()
        await self._phase_c_coordination()
        await self.complete()

    # ─── Phase A: Reconnaissance ──────────────────────────────────────────────

    async def _phase_a_recon(self) -> None:
        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation="Starting reconnaissance phase — crawling target URL",
        )
        await self._writer.update_agent_state(
            self.audit_id, self.agent_type(),
            current_action_summary="Phase A: Reconnaissance",
        )

        # Step 1 — Playwright crawl (sequential; all downstream tools depend on it)
        crawler_result = await self.run_tool(
            "PlaywrightCrawler",
            PlaywrightCrawlerInput(url=await self._get_audit_url()),
            action_summary="Crawling target URL with Playwright",
        )

        if not crawler_result.success:
            error = crawler_result.error
            if error and error.code.value in ("http_error",):
                # Hard failure: target URL returned 4xx/5xx
                await self.create_finding(
                    category=FindingCategory.TECHNICAL,
                    title="Target URL returned an HTTP error — audit cannot proceed",
                    description=(
                        f"PlaywrightCrawler received a non-2xx HTTP response from the target URL. "
                        f"Error: {error.message}. The site may be down, behind authentication, "
                        "or the URL may be incorrect."
                    ),
                    severity=Severity.CRITICAL,
                    business_impact="No audit data can be collected while the target URL is unreachable.",
                    impact_score=10,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Verify the URL is publicly accessible and returns a 200 status.",
                    tool_name="PlaywrightCrawler",
                    evidence_raw_data={"error_code": error.code.value, "message": error.message},
                    confidence=0.99,
                )
                await self._writer.update_agent_state(
                    self.audit_id, self.agent_type(),
                    status=AgentStatus.FAILED,
                )
                return
            else:
                # Non-HTTP failure (timeout, not implemented, etc.) — continue with fallback
                await self.emit_trace(
                    TraceEventType.OBSERVATION,
                    observation=f"PlaywrightCrawler unavailable ({error.code.value if error else 'unknown'}) — proceeding with fallback recon",
                )
                await self._store_fallback_recon()
        else:
            # Store playwright output as recon artifact
            await self._writer.store_recon_artifact(
                self.audit_id, "playwright_output", crawler_result.data
            )
            crawl = crawler_result.data
            csr_hint = ""
            if hasattr(crawl, "rendered_word_count") and hasattr(crawl, "static_word_count"):
                if crawl.rendered_word_count > crawl.static_word_count * 2:
                    csr_hint = " CSR detected — JS renders significantly more content than static HTML."
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"Page crawled successfully. Status: {getattr(crawl, 'http_status_code', '?')}.{csr_hint}",
            )

        # Steps 2–5 run after playwright (screenshot can be concurrent, rest sequential)
        url = await self._get_audit_url()
        playwright_data = await self.get_recon_artifact("playwright_output")

        screenshot_task = asyncio.create_task(
            self.run_tool(
                "ScreenshotCapture",
                ScreenshotCaptureInput(url=url),
                action_summary="Capturing full-page screenshot",
            )
        )

        if playwright_data is not None:
            rendered_html = getattr(playwright_data, "rendered_html", "")
            static_html = getattr(playwright_data, "static_html", "")
            headers = getattr(playwright_data, "response_headers", None)
            script_urls = [
                r.url for r in getattr(playwright_data, "network_requests", [])
                if getattr(r, "resource_type", "") == "script"
            ]

            tech_result = await self.run_tool(
                "TechStackDetector",
                TechStackDetectorInput(
                    html=rendered_html,
                    response_headers=headers or {},
                    script_urls=script_urls,
                ),
                action_summary="Fingerprinting technology stack",
            )
            if tech_result.success:
                await self._writer.store_recon_artifact(
                    self.audit_id, "tech_stack", tech_result.data
                )
                await self.emit_trace(
                    TraceEventType.OBSERVATION,
                    observation=f"Tech stack detected: {tech_result.data.summarize() if hasattr(tech_result.data, 'summarize') else tech_result.data}",
                )

            header_result = await self.run_tool(
                "HeaderAnalyzer",
                HeaderAnalyzerInput(
                    response_headers=headers or {},
                    url=url,
                    is_https=url.startswith("https://"),
                ),
                action_summary="Analyzing HTTP response headers",
            )
            if header_result.success:
                await self._writer.store_recon_artifact(
                    self.audit_id, "header_analysis", header_result.data
                )

            link_result = await self.run_tool(
                "LinkExtractor",
                LinkExtractorInput(html=rendered_html, base_url=url),
                action_summary="Extracting all internal and external links",
            )
            if link_result.success:
                await self._writer.store_recon_artifact(
                    self.audit_id, "link_extraction", link_result.data
                )
                link_data = link_result.data
                n_internal = sum(1 for lk in getattr(link_data, "links", []) if getattr(lk, "is_internal", False))
                n_external = len(getattr(link_data, "links", [])) - n_internal
                await self.emit_trace(
                    TraceEventType.OBSERVATION,
                    observation=f"Found {n_internal} internal and {n_external} external links.",
                )

        # Wait for screenshot
        screenshot_result = await screenshot_task
        if screenshot_result.success:
            await self._writer.store_recon_artifact(
                self.audit_id, "screenshot_path",
                getattr(screenshot_result.data, "file_path", None)
            )

        await self._writer.transition_status(self.audit_id, AuditStatus.PLANNING)

    # ─── Phase B: Planning ────────────────────────────────────────────────────

    async def _phase_b_planning(self) -> None:
        await self._writer.update_agent_state(
            self.audit_id, self.agent_type(),
            current_action_summary="Phase B: Classifying site and generating audit plan",
        )

        playwright_data = await self.get_recon_artifact("playwright_output")
        tech_data = await self.get_recon_artifact("tech_stack")
        screenshot_path = await self.get_recon_artifact("screenshot_path")

        # Classify site (Claude call — stubbed in Phase 0)
        site_profile = await self._classify_site(
            playwright_data=playwright_data,
            tech_data=tech_data,
            screenshot_path=screenshot_path,
            url=await self._get_audit_url(),
        )
        await self._writer.set_site_profile(self.audit_id, site_profile)

        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation=f"Site classified as '{site_profile.category.value}' with rendering strategy '{site_profile.rendering_strategy.value}'.",
        )
        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Classification confidence: {site_profile.classification_confidence:.0%}. "
                      f"Primary goals: {[g.goal for g in site_profile.primary_goals]}.",
        )

        # Generate audit plan (Claude call — stubbed in Phase 0)
        audit_plan = await self._plan_audit(site_profile)
        await self._writer.set_audit_plan(self.audit_id, audit_plan)

        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation=f"Audit plan generated. {len(audit_plan.enabled_agents)} agents enabled. "
                        f"Deep focus agents: {[a.value for a in audit_plan.deep_agents]}.",
        )
        await self._writer.transition_status(self.audit_id, AuditStatus.AUDITING)

    # ─── Phase C: Coordination ────────────────────────────────────────────────

    async def _phase_c_coordination(self, agent_runner: Any = None) -> None:
        """
        Dispatch all enabled specialist agents in parallel, then monitor until
        all are terminal before triggering Synthesis.

        agent_runner is injected by AgentRuntime (not available in Phase 0 unit tests).
        """
        await self._writer.update_agent_state(
            self.audit_id, self.agent_type(),
            current_action_summary="Phase C: Coordinating specialist agents",
        )

        if agent_runner is None:
            # Phase 0: no runtime injected — emit a placeholder observation and return
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation="Agent coordination requires AgentRuntime injection (Phase 0 stub). "
                            "Specialist agents would be dispatched via asyncio.gather() here.",
            )
            return

        audit_plan = await self.get_audit_plan()
        coros = [
            agent_runner.dispatch(agent_type, self.audit_id)
            for agent_type in audit_plan.parallel_agents
        ]
        await asyncio.gather(*coros)

        # Monitor loop — poll until all specialists are terminal
        while True:
            all_done = await self._reader.are_all_specialist_agents_terminal(self.audit_id)
            if all_done:
                break

            # Check for high-priority findings that warrant PLAN_UPDATE
            await self._check_for_mid_audit_plan_updates()
            await asyncio.sleep(5)

        # Dispatch Synthesis
        await agent_runner.dispatch(AgentType.SYNTHESIS, self.audit_id)
        

    async def _check_for_mid_audit_plan_updates(self) -> None:
        """
        Examines findings from running agents.
        If a critical finding warrants changing remaining agent priorities, emits PLAN_UPDATE.
        """
        from app.models.trace import PlanUpdatePayload
        from app.models.enums import Severity as Sev

        seo_findings = await self._reader.get_findings_by_agent(self.audit_id, AgentType.SEO)
        noindex_finding = next(
            (f for f in seo_findings if f.severity == Sev.CRITICAL and "noindex" in f.title.lower()),
            None,
        )
        if noindex_finding:
            plan = await self.get_audit_plan()
            # Downgrade Performance and Accessibility depth — noindex makes ranking irrelevant
            for agent in (AgentType.PERFORMANCE, AgentType.ACCESSIBILITY):
                if agent in plan.agent_configs:
                    plan.agent_configs[agent].depth = AuditDepth.STANDARD
            await self._writer.set_audit_plan(self.audit_id, plan)
            await self.emit_trace(
                TraceEventType.PLAN_UPDATE,
                plan_update=PlanUpdatePayload(
                    previous_state="Performance and Accessibility agents running at configured depth",
                    new_state="Both downgraded to STANDARD depth",
                    trigger="SEO agent detected noindex on the target page — performance optimizations have no ranking impact until noindex is removed",
                    affected_agents=[AgentType.PERFORMANCE, AgentType.ACCESSIBILITY],
                ),
                reasoning="Noindex means no organic traffic regardless of performance or accessibility. Deprioritising those agents reduces audit time without losing signal.",
            )

    # ─── AI Stubs (Phase 0 placeholders) ─────────────────────────────────────

    async def _classify_site(
        self,
        playwright_data: Any,
        tech_data: Any,
        screenshot_path: Optional[str],
        url: str,
    ) -> SiteProfile:
        """
        Phase 0 stub. Phase 1: call Claude with HTML + screenshot for multimodal classification.
        Returns a default SiteProfile that allows the pipeline to continue.
        """
        from app.models.site_profile import ReconSignal

        return SiteProfile(
            id=__import__("uuid").uuid4(),
            audit_id=self.audit_id,
            url=url,
            final_url=url,
            category=SiteCategory.OTHER,
            rendering_strategy=RenderingStrategy.UNKNOWN,
            tech_stack=TechStack(),
            primary_goals=[SiteGoal(goal="unknown", confidence=0.5, signals=[])],
            recon_signals=[],
            classification_confidence=0.0,
            classification_reasoning="Phase 0 stub — classification not yet implemented",
            screenshot_path=screenshot_path,
        )

    async def _plan_audit(self, site_profile: SiteProfile) -> AuditPlan:
        """
        Phase 0 stub. Phase 1: call Claude with SiteProfile to generate a tailored plan.
        Returns a standard plan with all agents enabled at STANDARD depth.
        """
        specialist_agents = [a for a in AgentType if a not in (AgentType.ORCHESTRATOR, AgentType.SYNTHESIS)]
        agent_configs = {
            agent: AgentConfig(enabled=True, depth=AuditDepth.STANDARD)
            for agent in specialist_agents
        }
        return AuditPlan(
            audit_id=self.audit_id,
            site_profile_id=site_profile.id,
            agent_configs=agent_configs,
            parallel_agents=specialist_agents,
            rationale=PlanRationale(
                site_summary="Phase 0 default plan — AI-powered planning not yet implemented",
                estimated_duration_seconds=180,
            ),
        )

    # ─── Helpers ─────────────────────────────────────────────────────────────

    async def _get_audit_url(self) -> str:
        state = await self._reader.get_site_profile(self.audit_id)
        if state is not None:
            return state.url
        # Pre-classification: read from shared state directly
        from app.services.shared_state_service import SharedStateService
        if isinstance(self._reader, SharedStateService):
            raw_state = await self._reader.get_state(self.audit_id)
            if raw_state:
                return raw_state.url
        return ""

    async def _store_fallback_recon(self) -> None:
        """Stores placeholder recon artifacts when Playwright is unavailable."""
        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation="Using fallback recon data. All tool-dependent findings will be skipped.",
        )
