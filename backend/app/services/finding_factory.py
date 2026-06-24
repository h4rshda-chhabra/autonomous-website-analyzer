from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.models import (
    AgentType,
    Finding,
    FindingCategory,
    ImplementationEffort,
    Severity,
)
from app.models.finding import FindingEvidence, FixSuggestion
from app.runtime.base_agent import IFindingFactory


class FindingFactoryImpl(IFindingFactory):
    """
    Concrete factory that constructs validated Finding objects.

    Agents provide semantic fields (what, how bad, how to fix).
    The factory handles: confidence clamping, evidence packaging, FixSuggestion assembly.
    """

    def create(
        self,
        audit_id: UUID,
        agent: AgentType,
        category: FindingCategory,
        title: str,
        description: str,
        severity: Severity,
        business_impact: str,
        impact_score: int,
        effort: ImplementationEffort,
        effort_hours_min: int,
        effort_hours_max: int,
        fix_description: str,
        tool_name: str,
        evidence_raw_data: Dict[str, Any],
        *,
        confidence: float,
        affected_elements: Optional[List[str]] = None,
        affected_count: Optional[int] = None,
        metric_value: Optional[str] = None,
        metric_threshold: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        code_snippet: Optional[str] = None,
        snippet_language: Optional[str] = None,
        documentation_url: Optional[str] = None,
        verification_steps: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        wcag_criteria: Optional[str] = None,
        is_synthesis_finding: bool = False,
    ) -> Finding:
        confidence = self._clamp_confidence(confidence)

        evidence = FindingEvidence(
            tool_name=tool_name,
            raw_data=evidence_raw_data,
            affected_elements=affected_elements or [],
            affected_count=affected_count,
            screenshot_path=screenshot_path,
            metric_value=metric_value,
            metric_threshold=metric_threshold,
        )

        fix_suggestion = FixSuggestion(
            description=fix_description,
            code_snippet=code_snippet,
            snippet_language=snippet_language,
            documentation_url=documentation_url,
            verification_steps=verification_steps or [],
        )

        return Finding(
            id=uuid4(),
            audit_id=audit_id,
            agent=agent,
            category=category,
            title=title,
            description=description,
            severity=severity,
            business_impact=business_impact,
            impact_score=impact_score,
            effort=effort,
            effort_hours_min=effort_hours_min,
            effort_hours_max=effort_hours_max,
            fix_suggestion=fix_suggestion,
            confidence=confidence,
            evidence=evidence,
            tags=tags or [],
            wcag_criteria=wcag_criteria,
            is_synthesis_finding=is_synthesis_finding,
            created_at=datetime.utcnow(),
        )

    def create_synthesis_finding(
        self,
        *,
        audit_id: UUID,
        title: str,
        description: str,
        severity: Severity,
        business_impact: str,
        impact_score: int,
        effort: ImplementationEffort,
        effort_hours_min: int,
        effort_hours_max: int,
        fix_description: str,
        source_finding_ids: List[UUID],
        insight_type: str,
        tags: Optional[List[str]] = None,
    ) -> Finding:
        from app.models.finding import FindingRelationship
        from app.models.enums import FindingRelationshipType

        # Synthesis findings must reference at least one source to satisfy the model validator
        relationships = [
            FindingRelationship(
                related_finding_id=fid,
                relationship_type=FindingRelationshipType.CAUSED_BY,
                description=f"Compound issue derived from finding {fid}",
            )
            for fid in source_finding_ids
        ]

        evidence = FindingEvidence(
            tool_name="SynthesisAgent",
            raw_data={"source_finding_ids": [str(i) for i in source_finding_ids], "insight_type": insight_type},
        )
        fix_suggestion = FixSuggestion(description=fix_description)

        return Finding(
            id=uuid4(),
            audit_id=audit_id,
            agent=AgentType.SYNTHESIS,
            category=FindingCategory.COMPOUND_ISSUE,
            title=title,
            description=description,
            severity=severity,
            business_impact=business_impact,
            impact_score=impact_score,
            effort=effort,
            effort_hours_min=effort_hours_min,
            effort_hours_max=effort_hours_max,
            fix_suggestion=fix_suggestion,
            confidence=0.85,
            evidence=evidence,
            relationships=relationships,
            tags=tags or [],
            is_synthesis_finding=True,
            created_at=datetime.utcnow(),
        )

    # ─── Confidence Rules ─────────────────────────────────────────────────────

    @staticmethod
    def _clamp_confidence(value: float) -> float:
        return max(0.0, min(1.0, value))

    def confidence_for_deterministic(
        self,
        *,
        is_partial: bool = False,
        is_boundary_case: bool = False,
    ) -> float:
        base = 0.95
        if is_partial:
            base -= 0.15
        if is_boundary_case:
            base -= 0.10
        return self._clamp_confidence(base)

    def confidence_for_ai(
        self,
        ai_reported: float,
        *,
        content_was_truncated: bool = False,
        word_count: int = 999,
    ) -> float:
        conf = ai_reported
        if content_was_truncated:
            conf = min(conf, 0.75)
        if word_count < 200:
            conf = min(conf, 0.65)
        return self._clamp_confidence(conf)

    def confidence_for_synthesis(
        self, source_confidences: List[float]
    ) -> float:
        if not source_confidences:
            return 0.70
        base = min(source_confidences)
        if len(source_confidences) >= 3:
            base = min(1.0, base + 0.05)
        return self._clamp_confidence(base)

    # ─── Severity Inference ───────────────────────────────────────────────────

    def infer_severity_from_metric(
        self, metric_name: str, metric_value: float
    ) -> Severity:
        """
        Maps a named performance/accessibility metric to Severity.
        Used when agents have a numeric measurement and want the factory
        to apply the standard thresholds.
        """
        thresholds: Dict[str, List[tuple]] = {
            # (upper_bound_exclusive, severity) — first matching wins
            "lcp_ms": [
                (2500, Severity.INFO),
                (4000, Severity.MEDIUM),
                (6000, Severity.HIGH),
                (float("inf"), Severity.CRITICAL),
            ],
            "cls_score": [
                (0.1, Severity.INFO),
                (0.25, Severity.MEDIUM),
                (0.5, Severity.HIGH),
                (float("inf"), Severity.CRITICAL),
            ],
            "tbt_ms": [
                (200, Severity.INFO),
                (300, Severity.MEDIUM),
                (600, Severity.HIGH),
                (float("inf"), Severity.CRITICAL),
            ],
            "ttfb_ms": [
                (200, Severity.INFO),
                (800, Severity.MEDIUM),
                (1800, Severity.HIGH),
                (float("inf"), Severity.CRITICAL),
            ],
            "performance_score": [
                (50, Severity.CRITICAL),
                (75, Severity.HIGH),
                (90, Severity.MEDIUM),
                (float("inf"), Severity.INFO),
            ],
            "contrast_ratio": [
                (2.0, Severity.CRITICAL),
                (4.5, Severity.HIGH),
                (float("inf"), Severity.INFO),
            ],
        }

        rules = thresholds.get(metric_name)
        if rules is None:
            return Severity.MEDIUM  # safe default if metric not known

        for upper_bound, sev in rules:
            if metric_value < upper_bound:
                return sev

        return Severity.MEDIUM
