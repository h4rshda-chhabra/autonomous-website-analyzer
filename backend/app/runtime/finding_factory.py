"""
FindingFactory — Design Specification
═══════════════════════════════════════
The FindingFactory converts agent observations and tool outputs into
structured Finding objects. It standardizes:
  - Confidence scoring rules
  - Severity assignment based on metric thresholds
  - Impact scoring against a calibrated rubric
  - FixSuggestion construction
  - Evidence packaging

Design principle: agents provide semantic descriptions and raw data.
The factory applies the scoring rules consistently across all agents.
No agent should hard-code confidence values or compute priority_score directly
(priority_score is a computed_field on Finding — it is never set manually).

Confidence Scoring Rules
─────────────────────────
Confidence is not a free field. It is determined by the source of the finding:

  DETERMINISTIC TOOL OUTPUT (axe, Lighthouse, header checks):
    Confidence = 0.95   base
    - if metric is boundary-case (within 10% of threshold): -0.10
    - if tool indicated partial_data: -0.15
    → Typical range: 0.70 – 0.95

  AI TOOL OUTPUT (ClaudeContentAnalyzer):
    Confidence = claude_output.confidence
    - if word_count < 200 (very short page): min(output_conf, 0.65)
    - if content was truncated: min(output_conf, 0.75)
    → Typical range: 0.55 – 0.85

  CROSS-AGENT / SYNTHESIS FINDING:
    Confidence = min(confidence of all source findings)
    - if 3+ findings compound: +0.05 (more evidence = higher confidence)
    → Typical range: 0.65 – 0.92

  HEURISTIC FINDING (pattern matching, no ground truth):
    Confidence = 0.70 base
    → Typical range: 0.55 – 0.75

Severity Assignment Rubric
───────────────────────────
Severity is assigned by the factory based on metric thresholds and business impact.
Agents pass the raw metric values; the factory maps them to severity.

  CRITICAL  — Currently causing measurable business harm or legal exposure
              Examples: noindex on live page, 0/100 a11y score, missing HTTPS,
                        WCAG Level A violation, LCP > 6s

  HIGH      — Significant impact on ranking, conversion, or user experience
              Examples: LCP 4–6s, missing H1, no meta description, CSP absent,
                        5+ critical axe violations, content score < 4/10

  MEDIUM    — Meaningful impact, fix in next cycle
              Examples: LCP 2.5–4s, og:image missing, minor WCAG AA violations,
                        slow links, no structured data on ecommerce, generic anchors

  LOW       — Minor improvements with marginal impact
              Examples: meta description slightly long, HSTS max-age suboptimal,
                        passive voice overuse, low-priority missing headers

  INFO      — Observations worth noting but no immediate action needed
              Examples: Server header present (non-versioned), external links
                        open in new tab, optional schema types missing

Impact Score Rubric (1–10)
───────────────────────────
Impact is calibrated to business outcomes, not just technical severity.

  9–10: Directly harms the site's primary goal at scale
        E.g. noindex (kills all organic traffic), missing HTTPS (browser blocks page)
  7–8:  Significant harm to a key business metric (ranking, conversion, trust)
        E.g. LCP > 4s (ranking penalty + high bounce), missing alt text (a11y + SEO)
  5–6:  Moderate harm to a secondary metric
        E.g. missing OG image (social sharing suffers), no H1 (weak keyword signal)
  3–4:  Low harm, mostly polish and best practice
        E.g. meta description too long, minor caching header misconfiguration
  1–2:  Informational — no measurable harm
        E.g. optional schema types absent, non-versioned Server header

Severity → Impact Default Mapping (starting point, agents can adjust ±2):
  CRITICAL  → impact_score: 8–10
  HIGH      → impact_score: 6–8
  MEDIUM    → impact_score: 4–6
  LOW       → impact_score: 2–4
  INFO      → impact_score: 1–2

Effort Calibration
───────────────────
  EASY   (< 2h):  Config change, single HTML attribute, single file edit
                  E.g. add alt text, add meta description, set cache-control header
  MEDIUM (2–8h):  Multi-file change, requires testing
                  E.g. implement structured data, compress all images, fix heading hierarchy
  HARD   (> 8h):  Architectural, requires coordination, or touches multiple systems
                  E.g. implement CSP, fix LCP (requires image CDN changes), full a11y audit fix
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.models import (
    AgentType,
    Finding,
    FindingCategory,
    ImplementationEffort,
    Severity,
)
from app.tools.base import ToolResult


# ─── Confidence Rules (applied by factory, not agents) ────────────────────────

class ConfidenceContext:
    """
    Input to the confidence calculation. Agents construct this when calling create().
    The factory uses it to determine the final confidence value.
    """
    source_type: str                        # "deterministic" | "ai" | "heuristic" | "synthesis"
    tool_result_was_partial: bool = False   # ToolResult.error.partial_data_available
    metric_is_boundary_case: bool = False   # Metric within 10% of threshold
    input_was_truncated: bool = False       # Content was cut for context length
    ai_reported_confidence: Optional[float] = None  # From ClaudeContentAnalyzerOutput.confidence
    supporting_finding_count: int = 0       # For synthesis findings


# ─── Severity Thresholds (reference only — factory maps these) ────────────────

"""
Per-metric severity thresholds. These are the values the factory compares
against ToolOutput metrics to assign severity. Agents pass raw values;
the factory does the comparison.

Performance (Lighthouse/AssetAnalyzer):
  LCP: critical >6s | high 4-6s | medium 2.5-4s | good <2.5s
  CLS: critical >0.5 | high 0.25-0.5 | medium 0.1-0.25 | good <0.1
  TBT: critical >600ms | high 300-600ms | medium 200-300ms | good <200ms
  TTFB: critical >1800ms | high 800-1800ms | medium 200-800ms | good <200ms
  Performance Score: critical <25 | high 25-49 | medium 50-74 | good ≥90

Accessibility (AxeCoreScanner):
  critical impact violations → CRITICAL severity
  serious impact violations  → HIGH severity
  moderate impact violations → MEDIUM severity
  minor impact violations    → LOW severity

Security (SecurityHeaderAnalyzer):
  Missing CSP            → HIGH (information disclosure + XSS risk)
  Missing HSTS on HTTPS  → HIGH (MITM risk)
  Missing X-Frame-Options → MEDIUM (clickjacking risk)
  Missing X-Content-Type  → LOW
  Server version disclosure → LOW

SEO (MetaTagAnalyzer / StructuredDataAnalyzer):
  noindex on live page   → CRITICAL
  Missing H1             → HIGH
  Missing title tag      → HIGH
  Missing meta description → MEDIUM
  Title too long/short   → MEDIUM
  OG tags missing        → LOW (for non-social sites), MEDIUM (for content sites)

Content (ClaudeContentAnalyzer):
  Quality score 1-3      → HIGH
  Quality score 4-5      → MEDIUM
  Quality score 6-7      → LOW
  Quality score 8-10     → INFO (positive, may still have minor issues)
"""


# ─── FindingFactory ───────────────────────────────────────────────────────────

class FindingFactory(ABC):
    """
    Factory for constructing validated Finding objects.

    Two creation paths:

    Path 1 — Structured (most common):
      Agents provide all semantic fields explicitly.
      Factory assigns confidence, constructs Evidence, builds FixSuggestion.

    Path 2 — From Tool Result (convenience shortcut for common patterns):
      Agent provides the ToolResult and a Finding template.
      Factory extracts evidence automatically from tool output.
      Only use when the finding maps 1:1 with a tool output field.

    Both paths return a fully valid Finding with computed priority_score.
    """

    @abstractmethod
    def create(
        self,
        *,
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
        confidence_context: ConfidenceContext,
        # Optional evidence enrichment
        affected_elements: Optional[List[str]] = None,
        affected_count: Optional[int] = None,
        metric_value: Optional[str] = None,
        metric_threshold: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        # Optional fix enrichment
        code_snippet: Optional[str] = None,
        snippet_language: Optional[str] = None,
        documentation_url: Optional[str] = None,
        verification_steps: Optional[List[str]] = None,
        # Classification
        tags: Optional[List[str]] = None,
        wcag_criteria: Optional[str] = None,
        is_synthesis_finding: bool = False,
    ) -> Finding:
        """
        Creates and returns a fully validated Finding.
        Automatically:
          - Generates finding.id (uuid4)
          - Sets finding.audit_id
          - Sets finding.agent
          - Sets finding.created_at
          - Computes confidence from confidence_context
          - Constructs FindingEvidence from tool_name + evidence_raw_data
          - Constructs FixSuggestion from fix_description + optional fields
          - Validates effort_hours against effort category
          - Does NOT write to SharedState (caller does that via BaseAgent.create_finding)
        """
        ...

    @abstractmethod
    def compute_confidence(self, context: ConfidenceContext) -> float:
        """
        Applies the confidence rules defined in this file's header.
        Returns a float in [0.0, 1.0].
        Pure function — no side effects.
        """
        ...

    @abstractmethod
    def infer_severity(
        self,
        metric_name: str,
        metric_value: float,
        site_category: Optional[str] = None,
    ) -> Severity:
        """
        Maps a named metric and its value to a Severity using the thresholds above.
        Used for tool outputs with numeric metrics (LCP, CLS, contrast ratio, etc.).
        For non-numeric findings, agents specify severity directly.

        metric_name examples: 'lcp_ms', 'cls_score', 'contrast_ratio',
                              'performance_score', 'critical_axe_violations'
        """
        ...

    @abstractmethod
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
        """
        Creates a synthesis-only finding (compound issue or cross-domain insight).
        is_synthesis_finding=True automatically.
        confidence = min(confidence of source findings) + 0.05 if 3+ sources.
        evidence_raw_data = {"source_finding_ids": [...], "insight_type": "compound"}
        """
        ...
