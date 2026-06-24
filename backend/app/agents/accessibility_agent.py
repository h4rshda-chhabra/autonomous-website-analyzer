from __future__ import annotations

from typing import List

from app.models import (
    AgentType,
    FindingCategory,
    ImplementationEffort,
    Severity,
    TraceEventType,
)
from app.tools.accessibility.schemas import AxeCoreScannerInput, ContrastCheckerInput
from .base_agent import BaseAgent


class AccessibilityAgent(BaseAgent):
    """
    Detects WCAG 2.1 Level AA violations via axe-core and targeted contrast checking.
    Deduplicates overlapping findings between the two tools.
    """

    def agent_type(self) -> AgentType:
        return AgentType.ACCESSIBILITY

    def allowed_tools(self) -> List[str]:
        return ["AxeCoreScanner", "ContrastChecker"]

    async def execute(self) -> None:
        site_profile = await self.get_site_profile()
        url = site_profile.final_url or site_profile.url

        # ── Tool 1: AxeCoreScanner ────────────────────────────────────────────
        axe_result = await self.run_tool(
            "AxeCoreScanner",
            AxeCoreScannerInput(url=url, include_best_practices=True),
            action_summary="Injecting axe-core and running WCAG 2.1 rule suite",
            timeout_override_ms=45_000,
        )

        axe_color_contrast_selectors: List[str] = []

        if axe_result.success:
            axe = axe_result.data
            violations = getattr(axe, "violations", [])

            critical_count = sum(1 for v in violations if getattr(v, "impact", "") == "critical")
            serious_count = sum(1 for v in violations if getattr(v, "impact", "") == "serious")
            moderate_count = sum(1 for v in violations if getattr(v, "impact", "") == "moderate")

            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"axe-core found {len(violations)} violation(s): "
                            f"{critical_count} critical, {serious_count} serious, {moderate_count} moderate.",
            )

            for violation in violations:
                rule_id = getattr(violation, "rule_id", "unknown")
                impact = getattr(violation, "impact", "minor")
                description = getattr(violation, "description", rule_id)
                affected_elements = getattr(violation, "affected_elements", [])
                wcag_criteria = getattr(violation, "wcag_criteria", None)

                # Collect selectors for dedup with ContrastChecker
                if rule_id == "color-contrast":
                    axe_color_contrast_selectors.extend(affected_elements)
                    continue  # defer to ContrastChecker (more precise)

                severity_map = {
                    "critical": Severity.CRITICAL,
                    "serious": Severity.HIGH,
                    "moderate": Severity.MEDIUM,
                    "minor": Severity.LOW,
                }
                finding_severity = severity_map.get(impact, Severity.LOW)

                if finding_severity in (Severity.CRITICAL, Severity.SERIOUS if hasattr(Severity, "SERIOUS") else Severity.HIGH):
                    await self.emit_trace(
                        TraceEventType.REASONING,
                        reasoning=f"axe rule '{rule_id}' ({impact} impact, {len(affected_elements)} element(s)): "
                                  f"affects keyboard and screen reader users directly.",
                    )

                impact_score_map = {
                    Severity.CRITICAL: 9, Severity.HIGH: 7,
                    Severity.MEDIUM: 4, Severity.LOW: 2,
                }

                await self.create_finding(
                    category=FindingCategory.WCAG_PERCEIVABLE,
                    title=f"WCAG violation: {description} ({len(affected_elements)} element(s))",
                    description=f"axe-core rule '{rule_id}' failed on {len(affected_elements)} element(s). "
                                f"Impact: {impact}. {description}",
                    severity=finding_severity,
                    business_impact=f"Users who rely on assistive technology cannot use "
                                    f"{len(affected_elements)} element(s) on this page. "
                                    "WCAG AA compliance may be legally required in some jurisdictions.",
                    impact_score=impact_score_map.get(finding_severity, 4),
                    effort=ImplementationEffort.MEDIUM,
                    effort_hours_min=1,
                    effort_hours_max=8,
                    fix_description=f"Fix all elements matching rule '{rule_id}'. "
                                    "Consult WCAG 2.1 Success Criterion for the precise requirement.",
                    tool_name="AxeCoreScanner",
                    evidence_raw_data={
                        "rule_id": rule_id,
                        "impact": impact,
                        "affected_count": len(affected_elements),
                    },
                    confidence=0.95,
                    affected_elements=affected_elements[:10],
                    affected_count=len(affected_elements),
                    wcag_criteria=wcag_criteria,
                )

            # Items needing manual review
            incomplete = getattr(axe, "incomplete", [])
            if incomplete:
                await self.emit_trace(
                    TraceEventType.REASONING,
                    reasoning=f"{len(incomplete)} item(s) require manual review — "
                              "automated tools cannot definitively verify these.",
                )
                await self.create_finding(
                    category=FindingCategory.WCAG_PERCEIVABLE,
                    title=f"{len(incomplete)} accessibility item(s) require manual review",
                    description="These items cannot be verified automatically. "
                                "A human reviewer must check them against WCAG 2.1 criteria.",
                    severity=Severity.INFO,
                    business_impact="Unverified items may represent real accessibility barriers. "
                                    "Manual testing ensures comprehensive WCAG coverage.",
                    impact_score=2,
                    effort=ImplementationEffort.MEDIUM,
                    effort_hours_min=2,
                    effort_hours_max=8,
                    fix_description="Schedule a manual accessibility review session "
                                    "with a screen reader (NVDA, VoiceOver) to verify each item.",
                    tool_name="AxeCoreScanner",
                    evidence_raw_data={"incomplete_count": len(incomplete)},
                    confidence=0.80,
                )
        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"AxeCoreScanner failed: {axe_result.error.message if axe_result.error else 'unknown'}. Skipping axe findings.",
            )

        # ── Tool 2: ContrastChecker ───────────────────────────────────────────
        contrast_result = await self.run_tool(
            "ContrastChecker",
            ContrastCheckerInput(url=url),
            action_summary="Checking text contrast ratios against WCAG AA thresholds",
        )

        if contrast_result.success:
            contrast = contrast_result.data
            failures = getattr(contrast, "failures", [])

            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"ContrastChecker found {len(failures)} contrast failure(s).",
            )

            for failure in failures:
                selector = getattr(failure, "selector", "unknown")
                ratio = getattr(failure, "contrast_ratio", 0.0)
                is_large_text = getattr(failure, "is_large_text", False)

                # Deduplicate: if axe already reported this selector, skip
                if selector in axe_color_contrast_selectors:
                    continue

                threshold = "3:1" if is_large_text else "4.5:1"
                sev = Severity.CRITICAL if ratio < 2.0 else Severity.HIGH

                await self.create_finding(
                    category=FindingCategory.WCAG_PERCEIVABLE,
                    title=f"Contrast ratio {ratio:.2f}:1 fails WCAG AA ({threshold} required)",
                    description=f"Element '{selector}' has a contrast ratio of {ratio:.2f}:1, "
                                f"below the WCAG 2.1 AA requirement of {threshold} "
                                f"for {'large' if is_large_text else 'normal'} text.",
                    severity=sev,
                    business_impact="Low contrast text is illegible for users with low vision or colour blindness.",
                    impact_score=8 if sev == Severity.CRITICAL else 6,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=2,
                    fix_description=f"Increase contrast to at least {threshold}. "
                                    f"Suggested foreground color: {getattr(failure, 'suggested_foreground', 'use a contrast checker tool')}.",
                    tool_name="ContrastChecker",
                    evidence_raw_data={
                        "selector": selector,
                        "contrast_ratio": ratio,
                        "threshold": threshold,
                    },
                    confidence=0.97,
                    affected_elements=[selector],
                    metric_value=f"{ratio:.2f}:1",
                    metric_threshold=threshold,
                    wcag_criteria="1.4.3 Contrast (Minimum)",
                )
        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"ContrastChecker failed: {contrast_result.error.message if contrast_result.error else 'unknown'}. Skipping contrast findings.",
            )

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Accessibility analysis complete. {self._findings_written} finding(s) written.",
        )
        await self.complete()
