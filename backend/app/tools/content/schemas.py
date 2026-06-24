"""
Content Tool Schemas
────────────────────
Two tools run sequentially within the Content Agent:
  1. ContentExtractor        — deterministic: extracts and structures page content
  2. ClaudeContentAnalyzer   — AI-powered: evaluates quality, alignment, and persuasion

The split is intentional:
  ContentExtractor handles all the structural parsing (readability algorithms,
  CTA detection, section boundaries) so that ClaudeContentAnalyzer receives
  clean, structured input rather than raw HTML. This reduces token cost and
  improves analysis quality.

ClaudeContentAnalyzer is the only tool in the MVP that calls Claude directly.
It uses structured output (response_format / tool use) to guarantee a typed response.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# 13. ContentExtractor
# ═══════════════════════════════════════════════════════════════

class ContentExtractorInput(BaseModel):
    """
    Used by: Content Agent.
    Purpose: Extracts and structures the meaningful content from a page,
             separating editorial content from boilerplate (nav, footer, ads, sidebars).
             Uses a readability algorithm (Mozilla Readability port) + custom heuristics.
             Also detects CTA elements and above-fold content boundaries.
    """
    html: str = Field(
        ...,
        description=(
            "Rendered HTML preferred (captures JS-injected content). "
            "Falls back to static HTML if rendered is unavailable."
        ),
    )
    url: str = Field(..., description="Used for relative URL resolution and domain context")
    site_category: Optional[str] = Field(
        None,
        description=(
            "From SiteProfile. Adjusts extraction heuristics: "
            "ecommerce → extract product descriptions and specs; "
            "blog → extract article body and author bio; "
            "saas → extract hero copy and feature descriptions."
        ),
    )
    viewport_height_px: int = Field(
        900,
        description="Viewport height for above-fold boundary calculation",
    )


class ContentSection(BaseModel):
    """A discrete content block within the page."""
    section_type: str = Field(
        ...,
        description="hero | feature | testimonial | pricing | faq | cta_block | article_body | other",
    )
    heading: Optional[str] = None
    text_content: str
    word_count: int
    is_above_fold: bool = Field(
        False,
        description="True if this section begins within the first viewport height",
    )
    estimated_position_px: Optional[int] = Field(
        None,
        description="Estimated top pixel position of this section",
    )


class CTAElement(BaseModel):
    """A detected Call-to-Action element."""
    element_type: str = Field(..., description="button | link | form_submit | banner")
    text: str = Field(..., description="Visible CTA text")
    href: Optional[str] = Field(None, description="Link destination if applicable")
    is_above_fold: bool
    is_primary: bool = Field(
        False,
        description=(
            "Heuristic: prominent size, high-contrast color, or positioned in hero section"
        ),
    )
    action_type: str = Field(
        ...,
        description=(
            "signup | contact | purchase | download | learn_more | get_started | other. "
            "Classified from CTA text patterns."
        ),
    )


class ReadabilityMetrics(BaseModel):
    """Computed readability scores for the main content."""
    flesch_reading_ease: Optional[float] = Field(
        None,
        description=(
            "0–100. Higher = easier. "
            "90–100: 5th grade. 60–70: 8th-9th grade (plain English target). "
            "0–30: college graduate."
        ),
    )
    flesch_kincaid_grade: Optional[float] = Field(
        None,
        description="US grade level equivalent. Grade 8 is the target for general audiences.",
    )
    average_sentence_length_words: Optional[float] = None
    average_word_length_chars: Optional[float] = None
    passive_voice_percentage: Optional[float] = Field(
        None,
        description="Estimated percentage of sentences using passive voice (>20% is notable)",
    )
    long_sentence_count: int = Field(
        0,
        description="Sentences with >30 words — often too complex",
    )
    paragraph_count: int = 0
    average_paragraph_length_words: Optional[float] = None


class ContentExtractorOutput(BaseModel):
    # ── Main Content ───────────────────────────────────────────────────────────
    main_content_text: str = Field(
        ...,
        description="Cleaned, boilerplate-free text of the main content area",
    )
    main_content_html: str = Field(
        ...,
        description="HTML of the main content area (preserves structure for analysis)",
    )
    word_count: int
    reading_time_minutes: float = Field(
        ...,
        description="Estimated reading time at 238 WPM (average adult reading speed)",
    )

    # ── Structure ─────────────────────────────────────────────────────────────
    sections: List[ContentSection] = Field(default_factory=list)
    cta_elements: List[CTAElement] = Field(default_factory=list)
    primary_cta: Optional[CTAElement] = Field(
        None,
        description="The single most prominent CTA (highest prominence heuristic score)",
    )
    above_fold_text: str = Field(
        ...,
        description="All visible text within the first viewport (most important for conversion)",
    )
    above_fold_word_count: int

    # ── Readability ────────────────────────────────────────────────────────────
    readability: ReadabilityMetrics

    # ── Structural Signals ─────────────────────────────────────────────────────
    has_hero_section: bool = False
    has_social_proof: bool = Field(
        False,
        description="Testimonials, review counts, logos, trust badges detected",
    )
    has_pricing_section: bool = False
    has_faq_section: bool = False
    lists_count: int = Field(0, description="Number of <ul>/<ol> elements in main content")
    images_in_content: int = 0
    total_cta_count: int = 0
    primary_cta_above_fold: bool = False

    # ── Boilerplate Stats ──────────────────────────────────────────────────────
    nav_word_count: int = Field(0, description="Words in navigation elements (excluded from main)")
    footer_word_count: int = Field(0, description="Words in footer (excluded from main)")

    def summarize(self) -> Dict[str, Any]:
        return {
            "word_count": self.word_count,
            "reading_time_min": self.reading_time_minutes,
            "cta_count": self.total_cta_count,
            "primary_cta": self.primary_cta.text if self.primary_cta else None,
            "primary_cta_above_fold": self.primary_cta_above_fold,
            "flesch_score": self.readability.flesch_reading_ease,
            "grade_level": self.readability.flesch_kincaid_grade,
            "sections_detected": len(self.sections),
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # PARSE_ERROR  → HTML cannot be parsed (returns empty/partial content)
    # BLANK_PAGE   → main_content_text is empty or <50 words
    #                Agent should note: possible cookie consent wall, auth gate,
    #                or JS-only content that wasn't captured in rendered HTML
    #
    # Low-quality extraction (not errors):
    #   - Single-page apps that render content after scroll events
    #   - Pages where main content is canvas or WebGL
    #   - Dynamically personalized content (returns what was visible at render time)
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Content Agent
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 14. ClaudeContentAnalyzer
# ═══════════════════════════════════════════════════════════════

class ClaudeContentAnalyzerInput(BaseModel):
    """
    Used by: Content Agent.
    Purpose: AI-powered qualitative analysis of content quality, clarity, tone,
             goal alignment, and persuasive effectiveness. This is the only tool
             that invokes Claude as a sub-call within an agent.

    Implementation detail:
      Calls Claude via the Anthropic SDK using tool_use / structured output mode.
      The response schema is ClaudeContentAnalyzerOutput — Claude is instructed
      to fill this schema exactly. The ToolExecutor validates the response
      against the Pydantic model before returning it.

    Input construction:
      The agent constructs this input AFTER ContentExtractor runs.
      It passes the structured extraction (not raw HTML) to minimize tokens.
    """
    main_content_text: str = Field(
        ...,
        max_length=15_000,
        description=(
            "Cleaned content text from ContentExtractor. "
            "Truncated to 15k chars if longer — sufficient for quality analysis."
        ),
    )
    above_fold_text: str = Field(
        ...,
        max_length=2_000,
        description="Above-fold copy from ContentExtractor (most critical for analysis)",
    )
    site_category: str = Field(..., description="From SiteProfile")
    primary_goals: List[str] = Field(..., description="Top goals from SiteProfile")
    cta_texts: List[str] = Field(
        default_factory=list,
        description="CTA button/link texts from ContentExtractor",
    )
    has_social_proof: bool = False
    word_count: int = Field(..., description="Total word count from ContentExtractor")
    flesch_reading_ease: Optional[float] = None
    page_url: str = Field(..., description="URL for context in Claude's analysis")


class ContentIssue(BaseModel):
    """A specific content quality issue identified by Claude."""
    issue_type: str = Field(
        ...,
        description=(
            "value_proposition_unclear | weak_cta | wrong_tone | jargon_overuse | "
            "goal_misalignment | missing_social_proof | readability_poor | "
            "content_too_thin | content_too_long | headline_weak | other"
        ),
    )
    description: str = Field(..., description="Specific explanation of the issue")
    evidence: str = Field(
        ...,
        description="Direct quote or specific element from the content that demonstrates the issue",
    )
    recommendation: str = Field(..., description="Specific fix recommendation")
    severity: str = Field(..., description="critical | high | medium | low")


class RewriteSuggestion(BaseModel):
    """An AI-generated rewrite for a specific content element."""
    element_type: str = Field(
        ...,
        description="headline | hero_subtext | cta_button | meta_description | value_proposition",
    )
    original_text: str
    suggested_text: str
    improvement_rationale: str


class ClaudeContentAnalyzerOutput(BaseModel):
    """
    Structured output from Claude's content analysis.
    Claude is instructed to populate this exact schema via tool_use mode.
    The ToolExecutor validates the response — if validation fails, it retries once.
    """
    # ── Scores ─────────────────────────────────────────────────────────────────
    overall_quality_score: int = Field(
        ...,
        ge=1,
        le=10,
        description="1–10 overall content quality. 7+ = publishable, <5 = needs significant work.",
    )
    goal_alignment_score: int = Field(
        ...,
        ge=1,
        le=10,
        description="How well the content serves the detected primary goals",
    )
    clarity_score: int = Field(
        ...,
        ge=1,
        le=10,
        description="How clear and understandable the content is for the target audience",
    )
    persuasion_score: int = Field(
        ...,
        ge=1,
        le=10,
        description="Effectiveness of value proposition, CTAs, and social proof",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Claude's confidence in this analysis. "
            "Lower when content is very short (<200 words), highly technical, "
            "or outside Claude's evaluation context."
        ),
    )

    # ── Assessments ────────────────────────────────────────────────────────────
    tone_assessment: str = Field(
        ...,
        description=(
            "One paragraph describing the content's tone and whether it matches "
            "the site category and goals. E.g. 'Professional but approachable — "
            "appropriate for a B2B SaaS targeting technical buyers. "
            "Could be warmer to reduce friction for first-time visitors.'"
        ),
    )
    value_proposition_summary: str = Field(
        ...,
        description=(
            "Claude's interpretation of the page's value proposition. "
            "If unclear: 'The value proposition is not clearly stated above the fold. "
            "Visitors must scroll to the third section to understand what this product does.'"
        ),
    )
    audience_fit: str = Field(
        ...,
        description="Who this content appears written for and whether that matches the likely visitor",
    )
    content_gap_summary: Optional[str] = Field(
        None,
        description=(
            "What important information appears to be missing for the detected goals. "
            "E.g. 'No pricing information or pricing CTA — common friction point for SaaS trials.'"
        ),
    )

    # ── Specific Issues ────────────────────────────────────────────────────────
    issues: List[ContentIssue] = Field(
        default_factory=list,
        description="Specific, evidenced content issues (not vague observations)",
    )

    # ── Rewrite Suggestions ────────────────────────────────────────────────────
    rewrite_suggestions: List[RewriteSuggestion] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Maximum 3 high-value rewrite suggestions. "
            "Focus on above-fold elements and primary CTA. "
            "Quality over quantity — only suggest rewrites Claude is confident about."
        ),
    )

    # ── Meta ──────────────────────────────────────────────────────────────────
    model_used: str = Field(..., description="Claude model ID used for this analysis")
    input_tokens: int
    output_tokens: int

    def summarize(self) -> Dict[str, Any]:
        return {
            "quality_score": self.overall_quality_score,
            "goal_alignment": self.goal_alignment_score,
            "clarity": self.clarity_score,
            "persuasion": self.persuasion_score,
            "confidence": self.confidence,
            "issues_found": len(self.issues),
            "rewrites_suggested": len(self.rewrite_suggestions),
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # CLAUDE_API_ERROR      → Anthropic API returned 5xx
    # CLAUDE_RATE_LIMITED   → 429 — ToolExecutor waits 60s and retries once
    # CLAUDE_INVALID_OUTPUT → Claude didn't return valid schema (rare with tool_use mode)
    #                         ToolExecutor retries once with stricter instructions
    # CONTEXT_TOO_LONG      → content text + prompt exceeds model context limit
    #                         Mitigation: input is pre-truncated to 15k chars
    #
    # Graceful degradation:
    #   If Claude fails after retry, Content Agent skips AI analysis findings.
    #   ContentExtractor findings (readability, CTA count, structure) still produce
    #   deterministic findings with confidence=1.0. AI findings are confidence-gated.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Content Agent
    # Type: AI-powered (Claude API)
