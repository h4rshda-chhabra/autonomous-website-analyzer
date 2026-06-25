from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional
from uuid import UUID

from app.models import (
    AgentType,
    AuditStatus,
    FindingCategory,
    ImplementationEffort,
    Severity,
    TraceEventType,
)
from app.models.finding import Finding
from .base_agent import BaseAgent


_SEVERITY_WEIGHT = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class SynthesisAgent(BaseAgent):
    """
    Reads all findings from SharedState, detects cross-domain patterns,
    builds compound findings, computes scores, and emits a structured reasoning
    trace that documents the full synthesis.

    Phase 0: compound detection and scoring run; PriorityRoadmap construction
    is deferred to Phase 1 (requires Claude to generate narratives).
    Makes no tool calls.
    """

    def agent_type(self) -> AgentType:
        return AgentType.SYNTHESIS

    def allowed_tools(self) -> List[str]:
        return []

    async def execute(self) -> None:
        all_findings = await self._reader.get_all_findings(self.audit_id)
        site_profile = await self.get_site_profile()
        audit_plan = await self.get_audit_plan()

        if not all_findings:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=(
                    "No findings from specialist agents — either all tools failed "
                    "(Phase 0 stubs) or the site has no detectable issues. "
                    "Transitioning audit to COMPLETE."
                ),
            )
            await self._writer.transition_status(self.audit_id, AuditStatus.COMPLETE)
            await self.complete()
            return

        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation=f"Synthesising {len(all_findings)} finding(s) across "
                        f"{len(set(f.agent for f in all_findings))} agent(s).",
        )

        # ── Step 1: Detect compound issues ────────────────────────────────────
        compound_findings = await self._detect_compound_issues(all_findings)

        # ── Step 2: Compute per-domain and overall scores ─────────────────────
        scores = self._compute_scores(all_findings, compound_findings)

        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation=(
                f"Scores — overall: {scores['overall']}/100 | "
                f"SEO: {scores['seo']}/100 | "
                f"Performance: {scores['performance']}/100 | "
                f"Accessibility: {scores['accessibility']}/100 | "
                f"Content: {scores['content']}/100 | "
                f"Technical: {scores['technical']}/100"
            ),
        )

        # ── Step 3: Rank all findings (including compounds) ───────────────────
        all_ranked = sorted(
            all_findings + compound_findings,
            key=lambda f: f.priority_score,
            reverse=True,
        )

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=self._summarise_top_findings(all_ranked[:5]),
        )

        # ── Step 4: Generate cross-domain insights ────────────────────────────
        cross_insight_observations = self._generate_cross_insight_text(
            all_findings, compound_findings, scores
        )
        for obs in cross_insight_observations:
            await self.emit_trace(TraceEventType.OBSERVATION, observation=obs)

        # ── Step 5: Emit quick-wins cluster ──────────────────────────────────
        quick_wins = [
            f for f in all_ranked
            if f.effort == ImplementationEffort.EASY
            and f.severity in (Severity.HIGH, Severity.CRITICAL)
        ]
        if quick_wins:
            await self.emit_trace(
                TraceEventType.REASONING,
                reasoning=(
                    f"{len(quick_wins)} quick-win(s) identified "
                    f"(HIGH/CRITICAL severity + EASY effort): "
                    + ", ".join(f"'{f.title[:60]}'" for f in quick_wins[:3])
                    + ("..." if len(quick_wins) > 3 else "")
                ),
            )

        # ── Phase 0: Emit roadmap stub observation ────────────────────────────
        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation=(
                f"Phase 0: PriorityRoadmap construction deferred to Phase 1 "
                f"(requires Claude to generate executive_summary, top_3_actions, and why_prioritized "
                f"per roadmap item). {len(all_ranked)} finding(s) would map to roadmap items. "
                f"{len(quick_wins)} quick-win(s) would be in IMMEDIATE phase."
            ),
        )

        # ── Step 5b: AI executive summary (optional) ─────────────────────────────
        insights = await self._build_insights(all_findings, compound_findings, scores, quick_wins)
        if hasattr(self._writer, "set_synthesis_insights"):
            await self._writer.set_synthesis_insights(self.audit_id, insights)

        # ── Determine final status ────────────────────────────────────────────
        final_status = AuditStatus.COMPLETE
        if hasattr(self._writer, "add_warning"):
            # Read back warnings that were recorded by any agent
            raw_state = None
            if hasattr(self._reader, "get_state"):
                raw_state = await self._reader.get_state(self.audit_id)
            if raw_state and getattr(raw_state, "ai_warnings", []):
                final_status = AuditStatus.COMPLETE_WITH_WARNINGS

        await self._writer.transition_status(self.audit_id, final_status)
        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Synthesis complete. {len(all_findings)} specialist finding(s) + "
                      f"{len(compound_findings)} compound finding(s) processed. "
                      f"Status: {final_status.value}.",
        )
        await self.complete()

    # ─── Compound Issue Detection ─────────────────────────────────────────────

    async def _detect_compound_issues(self, findings: List[Finding]) -> List[Finding]:
        """
        Detects cross-domain finding patterns that compound each other.
        Returns new Finding objects representing compound issues (stored under SYNTHESIS agent).
        """
        compound: List[Finding] = []

        # Pattern 1: noindex + broken links → "invisible and broken"
        noindex = next(
            (f for f in findings if "noindex" in f.title.lower() and f.severity == Severity.CRITICAL),
            None,
        )
        broken = [f for f in findings if f.category == FindingCategory.BROKEN_LINKS]
        if noindex and broken:
            finding = self._factory.create_synthesis_finding(
                audit_id=self.audit_id,
                title="Site is invisible to search engines AND has broken internal links",
                description=(
                    "A noindex directive is present (hiding the page from Google) "
                    f"and {len(broken)} broken internal link(s) exist. "
                    "Even after noindex is removed, crawlers will hit dead links and waste crawl budget."
                ),
                severity=Severity.CRITICAL,
                business_impact=(
                    "Fixing noindex alone is insufficient — the broken links must be resolved "
                    "simultaneously to ensure clean indexation on re-submission."
                ),
                impact_score=10,
                effort=ImplementationEffort.MEDIUM,
                effort_hours_min=1,
                effort_hours_max=5,
                fix_description=(
                    "1. Remove noindex directive. "
                    "2. Fix all broken internal links before requesting re-indexation via Google Search Console."
                ),
                source_finding_ids=[f.id for f in broken] + [noindex.id],
                insight_type="noindex_plus_broken_links",
            )
            compound.append(finding)
            await self._writer.append_finding(self.audit_id, finding)
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation="Compound: noindex + broken links. Both must be fixed before re-indexation request.",
            )

        # Pattern 2: Slow LCP + render-blocking resources → render-blocking is root cause
        slow_lcp = next(
            (f for f in findings if "lcp" in f.title.lower() and f.severity in (Severity.CRITICAL, Severity.HIGH)),
            None,
        )
        render_blocking = next(
            (f for f in findings if f.category == FindingCategory.RENDER_BLOCKING),
            None,
        )
        if slow_lcp and render_blocking:
            finding = self._factory.create_synthesis_finding(
                audit_id=self.audit_id,
                title="Render-blocking resources are a likely root cause of the slow LCP",
                description=(
                    "LCP is slow AND render-blocking scripts/stylesheets are present. "
                    "Render-blocking resources delay the first paint, pushing LCP element discovery later."
                ),
                severity=Severity.HIGH,
                business_impact=(
                    "Fixing render-blocking resources alone could move LCP into the 'Good' band "
                    "without requiring infrastructure changes."
                ),
                impact_score=9,
                effort=ImplementationEffort.MEDIUM,
                effort_hours_min=3,
                effort_hours_max=12,
                fix_description=(
                    "Defer all non-critical scripts. Move non-critical CSS to async loading. "
                    "Add preload hints for the LCP element."
                ),
                source_finding_ids=[slow_lcp.id, render_blocking.id],
                insight_type="render_blocking_causes_slow_lcp",
            )
            compound.append(finding)
            await self._writer.append_finding(self.audit_id, finding)
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation="Compound: slow LCP ← render-blocking resources.",
            )

        # Pattern 3: No CTA + unclear value proposition → conversion dead zone
        no_cta = next(
            (f for f in findings if f.category == FindingCategory.CTA and "no calls" in f.title.lower()),
            None,
        )
        weak_vp = next(
            (f for f in findings if f.category == FindingCategory.VALUE_PROPOSITION),
            None,
        )
        if no_cta and weak_vp:
            finding = self._factory.create_synthesis_finding(
                audit_id=self.audit_id,
                title="Unclear value proposition + missing CTA creates a conversion dead zone",
                description=(
                    "Visitors cannot understand the offer (weak value proposition) "
                    "and have no clear next step (no CTA). Both barriers compound each other."
                ),
                severity=Severity.HIGH,
                business_impact=(
                    "Fixing only one of these will not move conversion rate. "
                    "Both must be addressed together."
                ),
                impact_score=9,
                effort=ImplementationEffort.MEDIUM,
                effort_hours_min=4,
                effort_hours_max=12,
                fix_description=(
                    "1. Write a hero headline that names the problem solved, for whom, and the differentiator. "
                    "2. Add a primary CTA button above the fold."
                ),
                source_finding_ids=[no_cta.id, weak_vp.id],
                insight_type="no_cta_plus_weak_value_prop",
                tags=["conversion"],
            )
            compound.append(finding)
            await self._writer.append_finding(self.audit_id, finding)

        # Pattern 4: Multiple missing security headers → layered attack surface
        security_findings = [
            f for f in findings
            if f.category == FindingCategory.SECURITY
            and f.severity in (Severity.HIGH, Severity.CRITICAL)
        ]
        no_https = next((f for f in findings if f.category == FindingCategory.HTTPS), None)
        if not no_https and len(security_findings) >= 2:
            finding = self._factory.create_synthesis_finding(
                audit_id=self.audit_id,
                title=f"{len(security_findings)} missing security headers create a layered attack surface",
                description=(
                    f"Multiple high-severity security headers are absent: "
                    f"{', '.join(f.title.split('(')[0].strip() for f in security_findings[:3])}. "
                    "Together they expose the site to XSS, MITM, and clickjacking."
                ),
                severity=Severity.HIGH,
                business_impact=(
                    "Each missing header allows a different attack class. "
                    "Combined, they significantly increase the attack surface."
                ),
                impact_score=8,
                effort=ImplementationEffort.EASY,
                effort_hours_min=1,
                effort_hours_max=4,
                fix_description=(
                    "Add all missing security headers in one deployment. "
                    "Most can be set in a single server/CDN configuration change."
                ),
                source_finding_ids=[f.id for f in security_findings],
                insight_type="security_header_cluster",
                tags=["security"],
            )
            compound.append(finding)
            await self._writer.append_finding(self.audit_id, finding)

        return compound

    # ─── Scoring ──────────────────────────────────────────────────────────────

    def _compute_scores(
        self,
        findings: List[Finding],
        compound_findings: List[Finding],
    ) -> Dict[str, int]:
        domain_agents = {
            "seo": AgentType.SEO,
            "performance": AgentType.PERFORMANCE,
            "accessibility": AgentType.ACCESSIBILITY,
            "content": AgentType.CONTENT,
            "technical": AgentType.TECHNICAL,
        }

        def score_for(domain_findings: List[Finding]) -> int:
            if not domain_findings:
                return 100
            penalty = sum(_SEVERITY_WEIGHT[f.severity] * 5 for f in domain_findings)
            return max(0, min(100, 100 - penalty))

        scores = {
            domain: score_for([f for f in findings if f.agent == agent])
            for domain, agent in domain_agents.items()
        }

        all_penalty = sum(_SEVERITY_WEIGHT[f.severity] * 3 for f in findings + compound_findings)
        scores["overall"] = max(0, min(100, 100 - all_penalty // len(domain_agents)))
        return scores

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _summarise_top_findings(self, top_findings: List[Finding]) -> str:
        if not top_findings:
            return "No findings to summarise."
        lines = ["Top-priority findings to address first:"]
        for i, f in enumerate(top_findings, 1):
            lines.append(
                f"  {i}. [{f.severity.value.upper()}] {f.title[:80]} "
                f"(priority_score={f.priority_score:.2f})"
            )
        return "\n".join(lines)

    def _generate_cross_insight_text(
        self,
        findings: List[Finding],
        compound_findings: List[Finding],
        scores: Dict[str, int],
    ) -> List[str]:
        insights: List[str] = []

        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        if len(critical) >= 3:
            insights.append(
                f"{len(critical)} CRITICAL findings across "
                f"{len(set(f.agent for f in critical))} domain(s) — "
                "these indicate fundamental site health problems and should be addressed before any lower-priority work."
            )

        quick_wins = [
            f for f in findings
            if f.effort == ImplementationEffort.EASY
            and f.severity in (Severity.HIGH, Severity.CRITICAL)
        ]
        if quick_wins:
            total_hours = sum(
                (f.effort_hours_max if f.effort_hours_max is not None else 2)
                for f in quick_wins
            )
            insights.append(
                f"{len(quick_wins)} high-impact quick-win(s) fixable in approximately {total_hours} hours — "
                "address these first for maximum ROI."
            )

        domain_name_map = {
            "seo": "SEO", "performance": "Performance",
            "accessibility": "Accessibility", "content": "Content", "technical": "Technical",
        }
        weakest = min(
            ((k, v) for k, v in scores.items() if k != "overall"),
            key=lambda kv: kv[1],
            default=None,
        )
        if weakest and weakest[1] < 50:
            insights.append(
                f"{domain_name_map.get(weakest[0], weakest[0])} is the weakest domain "
                f"({weakest[1]}/100) — a focused sprint here yields the largest overall score gain."
            )

        if compound_findings:
            insights.append(
                f"{len(compound_findings)} compound issue(s) detected where findings from different "
                "agents reinforce each other — see SYNTHESIS findings for root-cause analysis."
            )

        return insights

    # ─── AI Insights ──────────────────────────────────────────────────────────

    async def _build_insights(
        self,
        findings: List[Finding],
        compound_findings: List[Finding],
        scores: Dict[str, int],
        quick_wins: List[Finding],
    ) -> Dict:
        """
        Produces the synthesis_insights dict that main.py uses for the summary panel.
        Calls LLM when available for an executive summary + top 3 actions.
        Falls back to deterministic text if LLM is unavailable.
        """
        all_ranked = sorted(
            findings + compound_findings,
            key=lambda f: f.priority_score,
            reverse=True,
        )

        scores_text = " | ".join(
            f"{k.capitalize()}: {v}/100"
            for k, v in scores.items()
        )
        top_findings_text = "\n".join(
            f"  [{f.severity.value.upper()}][{f.agent.value}] {f.title}"
            for f in all_ranked[:8]
        )
        n_critical = sum(1 for f in findings + compound_findings if f.severity.value == "critical")
        n_high     = sum(1 for f in findings + compound_findings if f.severity.value == "high")
        n_medium   = sum(1 for f in findings + compound_findings if f.severity.value == "medium")
        n_low      = sum(1 for f in findings + compound_findings if f.severity.value == "low")
        total_fix_hours = sum(
            (f.effort_hours_max or 2) for f in all_ranked if f.severity.value in ("critical", "high")
        )

        # Base dict that's always populated deterministically
        base = {
            "scores": scores,
            "finding_counts": {"critical": n_critical, "high": n_high, "medium": n_medium, "low": n_low},
            "total_findings": len(findings) + len(compound_findings),
            "quick_wins_count": len(quick_wins),
            "estimated_fix_hours": total_fix_hours,
            "executive_summary": None,   # filled by LLM or fallback
            "top_actions": [],
        }

        if self._llm and self._llm.is_available:
            llm_result = await self._call_summary_llm(
                scores_text=scores_text,
                top_findings_text=top_findings_text,
                n_critical=n_critical, n_high=n_high,
                total_fix_hours=total_fix_hours,
                quick_wins_count=len(quick_wins),
            )
            if llm_result:
                base["executive_summary"] = llm_result.get("executive_summary")
                base["top_actions"] = llm_result.get("top_actions", [])
                return base

        # Deterministic fallback summary
        if not findings:
            summary = "No significant issues were detected. The site is performing well across all audited domains."
        else:
            worst_domain = min(
                ((k, v) for k, v in scores.items() if k != "overall"),
                key=lambda kv: kv[1],
                default=("unknown", 100),
            )
            summary = (
                f"The audit identified {len(findings) + len(compound_findings)} findings across all domains. "
                f"The weakest area is {worst_domain[0].capitalize()} ({worst_domain[1]}/100). "
                f"There are {n_critical} critical and {n_high} high-priority issues requiring immediate attention. "
                f"Addressing the {len(quick_wins)} quick-win(s) alone would yield significant improvements."
            )

        top_actions = [
            {
                "action": f.title,
                "why": f.business_impact[:100] if f.business_impact else "",
                "expected_impact": f"Severity: {f.severity.value}, effort: {f.effort.value}",
            }
            for f in all_ranked[:3]
        ]

        base["executive_summary"] = summary
        base["top_actions"] = top_actions
        return base

    async def _call_summary_llm(
        self,
        *,
        scores_text: str,
        top_findings_text: str,
        n_critical: int,
        n_high: int,
        total_fix_hours: int,
        quick_wins_count: int,
    ) -> Optional[Dict]:
        """Calls LLM for executive summary. Returns parsed dict or None on any failure."""
        import json as _json
        from app.llm.base import LLMMessage
        from app.infrastructure.settings import settings

        system = (
            "You are a senior web performance and SEO consultant writing an executive audit summary. "
            "Be concise, specific, and business-focused. Return ONLY a valid JSON object."
        )
        user = (
            f"Website audit results:\n\n"
            f"Scores: {scores_text}\n\n"
            f"Finding counts: {n_critical} critical, {n_high} high\n"
            f"Quick wins available: {quick_wins_count}\n"
            f"Estimated fix effort for critical+high issues: {total_fix_hours} hours\n\n"
            f"Top findings:\n{top_findings_text}\n\n"
            "Return a JSON object with:\n"
            '  "executive_summary": "3-4 sentences summarising the audit, key risks, and business impact",\n'
            '  "top_actions": [\n'
            '    {"action": "...", "why": "...", "expected_impact": "..."},\n'
            "    (exactly 3 items)\n"
            "  ]\n"
        )

        resp = await self._llm.complete(
            [LLMMessage(role="system", content=system),
             LLMMessage(role="user", content=user)],
            max_tokens=settings.openrouter_synthesis_max_tokens,
            temperature=0.2,
            json_mode=True,
        )

        if not resp.success:
            await self.add_ai_warning(
                f"Synthesis Agent: AI summary failed — {resp.error}. Using deterministic fallback."
            )
            return None

        try:
            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return _json.loads(raw)
        except Exception as exc:
            await self.add_ai_warning(
                f"Synthesis Agent: AI summary parse error — {exc}. Using deterministic fallback."
            )
            return None
