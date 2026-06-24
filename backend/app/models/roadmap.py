from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, computed_field, model_validator

from .enums import AgentType, FindingCategory, ImplementationEffort, InsightType, RoadmapPhase, Severity


# ─── Sub-models ───────────────────────────────────────────────────────────────

class RoadmapItem(BaseModel):
    """
    A single prioritized action in the roadmap.

    May represent one finding or a bundle of related findings that share
    a root cause or a fix. Bundling is the Synthesis Agent's decision —
    it prevents the report from listing 10 "add alt text" items separately.
    """

    id: UUID = Field(default_factory=uuid4)
    rank: int = Field(..., ge=1, description="1-indexed position in the priority order")
    phase: RoadmapPhase = Field(
        ...,
        description=(
            "Quadrant this item falls into based on impact/effort matrix. "
            "quick_wins = high impact + easy. strategic = high impact + hard."
        ),
    )

    # ── What ──────────────────────────────────────────────────────────────────
    title: str = Field(
        ...,
        max_length=120,
        description="Action-oriented title (starts with a verb). E.g. 'Add alt text to 23 product images'",
    )
    description: str = Field(
        ...,
        description="Full explanation of what needs to change and where",
    )
    finding_ids: List[UUID] = Field(
        ...,
        min_length=1,
        description="The Finding IDs this roadmap item addresses (1+ findings may be bundled)",
    )
    primary_agent: AgentType = Field(
        ...,
        description="The agent that owns the majority of findings in this item",
    )
    categories_involved: List[FindingCategory] = Field(
        default_factory=list,
        description="Finding categories spanned by this item (multi-agent items span multiple)",
    )
    is_cross_agent: bool = Field(
        False,
        description="True if this item bundles findings from more than one agent",
    )

    # ── Why this rank ─────────────────────────────────────────────────────────
    why_prioritized: str = Field(
        ...,
        description=(
            "The Synthesis Agent's specific reasoning for this rank. "
            "Must reference business impact and concrete trade-offs. "
            "E.g. 'Ranked #1 because LCP affects Google ranking directly, "
            "affects 100% of visitors, and the fix (compress 3 images) takes under 2 hours.'"
        ),
    )
    priority_score: float = Field(
        ...,
        ge=0.0,
        description="Computed aggregate priority score from the bundled findings",
    )

    # ── Impact ────────────────────────────────────────────────────────────────
    combined_impact_score: float = Field(
        ...,
        ge=1.0,
        le=10.0,
        description="Weighted average impact score across bundled findings",
    )
    highest_severity: Severity = Field(
        ...,
        description="Severity of the most critical finding in this bundle",
    )
    business_outcome: str = Field(
        ...,
        description=(
            "Expected business result if this item is fixed. "
            "Written in business language, not technical. "
            "E.g. 'Expected to improve LCP from 4.2s to under 2.5s, moving from Poor to Good band, "
            "which reduces estimated bounce rate by 20–30%.'"
        ),
    )

    # ── Effort ────────────────────────────────────────────────────────────────
    effort: ImplementationEffort = Field(
        ...,
        description="Effort category for the bundled fix (worst-case of bundled findings)",
    )
    effort_hours_min: int = Field(..., ge=0)
    effort_hours_max: int = Field(..., ge=0)
    fix_summary: str = Field(
        ...,
        description=(
            "Concise fix instructions for this roadmap item. "
            "Synthesized from individual finding fix_suggestions. "
            "Should be actionable without reading the full finding detail."
        ),
    )

    # ── Dependencies ──────────────────────────────────────────────────────────
    depends_on_ranks: List[int] = Field(
        default_factory=list,
        description=(
            "Ranks that should be completed before this item. "
            "E.g. [1] means fix rank #1 first. Used to render dependency arrows in UI."
        ),
    )
    unlocks_ranks: List[int] = Field(
        default_factory=list,
        description="Ranks that become easier or more impactful after this item is fixed",
    )

    # ── Tags ──────────────────────────────────────────────────────────────────
    tags: List[str] = Field(
        default_factory=list,
        description="E.g. ['quick-win', 'seo', 'mobile', 'conversion', 'technical-debt']",
    )

    @model_validator(mode="after")
    def validate_effort_hours(self) -> "RoadmapItem":
        if self.effort_hours_min > self.effort_hours_max:
            raise ValueError("effort_hours_min must be <= effort_hours_max")
        return self

    @computed_field
    @property
    def effort_display(self) -> str:
        if self.effort_hours_min == self.effort_hours_max:
            return f"{self.effort_hours_min}h"
        return f"{self.effort_hours_min}–{self.effort_hours_max}h"


class CrossInsight(BaseModel):
    """
    A synthesis-level observation that spans multiple agents or findings.
    These are the insights that only become visible when all findings are read together.

    Examples:
      - COMPOUND: "Missing alt text + slow LCP + no structured data = triple SEO penalty"
      - OPPORTUNITY: "Fixing HTTPS will also resolve mixed content warnings and 2 CSP violations"
      - PATTERN: "6 of your 8 critical issues trace back to the absence of a CDN"
    """

    id: UUID = Field(default_factory=uuid4)
    title: str = Field(..., max_length=120)
    description: str = Field(
        ...,
        description="Full explanation of the insight and its implications",
    )
    insight_type: InsightType
    finding_ids: List[UUID] = Field(
        ...,
        min_length=2,
        description="The findings this insight connects (minimum 2 — insights are relational)",
    )
    agents_involved: List[AgentType] = Field(
        ...,
        min_length=1,
        description="Which agents produced the findings in this insight",
    )
    combined_impact: str = Field(
        ...,
        description="How the combination changes the total impact vs. treating findings in isolation",
    )
    recommendation: str = Field(
        ...,
        description="What to do about it, framed around the insight (not just the individual fixes)",
    )
    related_roadmap_ranks: List[int] = Field(
        default_factory=list,
        description="Which roadmap items address the findings in this insight",
    )


class AuditScoreSummary(BaseModel):
    """
    Overall scoring summary for the audit.
    Displayed prominently at the top of the report.
    """

    overall_score: int = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Composite score 0–100. Not a simple average — weighted by severity and business impact. "
            "100 = no findings. 0 = multiple critical findings with no mitigating factors."
        ),
    )
    score_label: str = Field(
        ...,
        description="Human label for the score band: Excellent / Good / Needs Work / Poor / Critical",
    )
    score_by_agent: Dict[str, int] = Field(
        ...,
        description="Per-agent score (0–100), keyed by AgentType.value string",
    )
    findings_summary: Dict[str, int] = Field(
        ...,
        description="Count per severity level: {'critical': 2, 'high': 5, 'medium': 8, 'low': 3, 'info': 1}",
    )
    total_findings: int
    total_roadmap_items: int
    quick_wins_available: int = Field(
        ...,
        description="Count of QUICK_WINS phase roadmap items (high impact, easy effort)",
    )


# ─── Core Model ───────────────────────────────────────────────────────────────

class PriorityRoadmap(BaseModel):
    """
    The Synthesis Agent's primary output.
    This is what the user reads — not a list of findings, but a structured action plan.

    Design principle: a user should be able to read this document top-to-bottom
    and know exactly what to fix, in what order, and why — without reading individual findings.
    """

    id: UUID = Field(default_factory=uuid4)
    audit_id: UUID
    site_profile_id: UUID

    # ── Scores ────────────────────────────────────────────────────────────────
    score_summary: AuditScoreSummary

    # ── Executive Summary ─────────────────────────────────────────────────────
    executive_summary: str = Field(
        ...,
        description=(
            "3–5 sentence narrative summary of the audit for non-technical stakeholders. "
            "States what the site does well, what its most critical issues are, "
            "and what fixing the top 3 items would achieve. No jargon."
        ),
    )
    top_3_actions: List[str] = Field(
        ...,
        min_length=1,
        max_length=3,
        description=(
            "The three most impactful actions, in plain English. "
            "These are extracted from the roadmap for display in the report header card. "
            "E.g. ['Compress your hero image to reduce LCP from 4.2s to under 2.5s', ...]"
        ),
    )

    # ── Ordered Roadmap ───────────────────────────────────────────────────────
    items: List[RoadmapItem] = Field(
        ...,
        description=(
            "Ordered list of action items, ranked by priority_score descending. "
            "rank=1 is the most impactful fix to do first."
        ),
    )

    # ── Cross-Agent Insights ──────────────────────────────────────────────────
    cross_insights: List[CrossInsight] = Field(
        default_factory=list,
        description=(
            "Synthesis-level observations that span multiple agents. "
            "These are the insights that only the Synthesis Agent can see "
            "because it reads all findings together."
        ),
    )

    # ── Phase Grouping ────────────────────────────────────────────────────────
    @computed_field
    @property
    def items_by_phase(self) -> Dict[str, List[RoadmapItem]]:
        """Groups roadmap items by RoadmapPhase for tab/section display in the UI."""
        result: Dict[str, List[RoadmapItem]] = {phase.value: [] for phase in RoadmapPhase}
        for item in self.items:
            result[item.phase.value].append(item)
        return result

    @computed_field
    @property
    def total_effort_hours_min(self) -> int:
        return sum(item.effort_hours_min for item in self.items)

    @computed_field
    @property
    def total_effort_hours_max(self) -> int:
        return sum(item.effort_hours_max for item in self.items)

    # ── Meta ──────────────────────────────────────────────────────────────────
    synthesis_reasoning: str = Field(
        ...,
        description=(
            "The Synthesis Agent's full reasoning trace for how it produced this roadmap. "
            "Stored for debugging and explainability — not shown in the primary UI."
        ),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # ── Validators ────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def validate_ranks_are_sequential(self) -> "PriorityRoadmap":
        ranks = [item.rank for item in self.items]
        expected = list(range(1, len(ranks) + 1))
        if sorted(ranks) != expected:
            raise ValueError(f"Roadmap item ranks must be sequential from 1. Got: {sorted(ranks)}")
        return self

    @model_validator(mode="after")
    def validate_top_3_references_real_items(self) -> "PriorityRoadmap":
        if len(self.items) > 0 and len(self.top_3_actions) == 0:
            raise ValueError("top_3_actions must not be empty when roadmap has items")
        return self

    @model_validator(mode="after")
    def validate_cross_insights_reference_real_findings(self) -> "PriorityRoadmap":
        all_finding_ids = {fid for item in self.items for fid in item.finding_ids}
        for insight in self.cross_insights:
            for fid in insight.finding_ids:
                if fid not in all_finding_ids:
                    raise ValueError(
                        f"CrossInsight '{insight.title}' references finding {fid} "
                        f"which is not in any roadmap item"
                    )
        return self
