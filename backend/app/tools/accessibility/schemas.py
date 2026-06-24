"""
Accessibility Tool Schemas
───────────────────────────
Both tools run within the Accessibility Agent against the rendered HTML.
Execution order: AxeCoreScanner first (breadth), then ContrastChecker (depth on visuals).

Standard targeted: WCAG 2.1 Level AA (the legal compliance baseline for most jurisdictions).
WCAG 2.2 additions (2.5.7, 2.5.8, 3.2.6, 3.3.7, 3.3.8) are flagged as 'wcag22' tag
where supported by axe-core.

Architecture note on axe-core:
  axe-core is a JavaScript library — it must be injected into the Playwright browser context
  and executed via page.evaluate(). The ToolExecutor handles this injection.
  The tool receives the already-rendered page handle, not just HTML string.
  This means AxeCoreScanner's actual implementation differs from pure HTML tools —
  but its INPUT schema (from the agent's perspective) is the same pattern: provide the URL
  and the executor handles the browser context internally.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# 11. AxeCoreScanner
# ═══════════════════════════════════════════════════════════════

class AxeCoreScannerInput(BaseModel):
    """
    Used by: Accessibility Agent.
    Purpose: Runs axe-core accessibility engine against the live rendered page.
             Detects WCAG violations across all four principles (POUR):
             Perceivable, Operable, Understandable, Robust.

    axe-core covers ~57% of WCAG 2.1 AA criteria automatically.
    The remaining ~43% require manual review (color perception, cognitive load, etc.).
    AxeCoreScanner flags 'incomplete' items that need manual verification.
    """
    url: str = Field(
        ...,
        description=(
            "The URL to audit. The ToolExecutor will open this in a Playwright browser, "
            "inject axe-core, and evaluate it. The rendered HTML is used automatically."
        ),
    )
    include_best_practices: bool = Field(
        True,
        description="Include axe 'best-practice' tag (beyond strict WCAG — recommended)",
    )
    tags: List[str] = Field(
        default_factory=lambda: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"],
        description="axe-core rule tags to include in the scan",
    )
    exclude_selectors: List[str] = Field(
        default_factory=list,
        description="CSS selectors to exclude from axe scan (e.g. third-party chat widgets)",
    )
    timeout_ms: int = Field(
        30_000,
        description="Time budget for axe evaluation (complex pages with many nodes can be slow)",
    )


class AxeElement(BaseModel):
    """A specific DOM element that violates an axe rule."""
    html_snippet: str = Field(
        ...,
        description="The opening tag of the violating element (truncated to 200 chars)",
    )
    target: List[str] = Field(
        ...,
        description="CSS selector path to the element: ['#main > article > img:nth-child(2)']",
    )
    failure_summary: str = Field(
        ...,
        description="axe's explanation of what exactly failed for this element",
    )
    any_checks: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="axe 'any' check results (element passes if any check passes)",
    )
    all_checks: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="axe 'all' check results (element must pass all checks)",
    )


class AxeViolation(BaseModel):
    """A single WCAG rule violation found by axe-core."""
    rule_id: str = Field(..., description="axe rule identifier: 'image-alt', 'color-contrast', etc.")
    impact: str = Field(
        ...,
        description="critical | serious | moderate | minor — axe's severity classification",
    )
    wcag_criteria: List[str] = Field(
        default_factory=list,
        description="WCAG success criteria violated: ['1.1.1', '1.4.3']",
    )
    wcag_level: str = Field(..., description="A | AA | AAA")
    description: str = Field(..., description="What rule is violated")
    help_text: str = Field(..., description="Short explanation of the requirement")
    help_url: str = Field(..., description="axe documentation URL for this rule")
    affected_elements: List[AxeElement] = Field(..., min_length=1)
    affected_element_count: int


class AxeIncomplete(BaseModel):
    """An item axe could not automatically determine — requires manual review."""
    rule_id: str
    description: str
    reason: str = Field(
        ...,
        description="Why axe couldn't auto-detect: 'Needs manual color check', 'Ambiguous ARIA', etc.",
    )
    affected_element_count: int


class AxeCoreScannerOutput(BaseModel):
    violations: List[AxeViolation] = Field(default_factory=list)
    incomplete: List[AxeIncomplete] = Field(
        default_factory=list,
        description="Items requiring manual review — not confirmed violations",
    )
    passes_count: int = Field(
        0,
        description="Number of rules that passed (for scoring context)",
    )
    inapplicable_count: int = Field(
        0,
        description="Rules not applicable to this page (no relevant elements present)",
    )

    # ── Violation Counts by Impact ─────────────────────────────────────────────
    critical_count: int = 0
    serious_count: int = 0
    moderate_count: int = 0
    minor_count: int = 0

    # ── Counts by WCAG Principle ───────────────────────────────────────────────
    perceivable_violations: int = Field(0, description="WCAG Principle 1 (1.x.x)")
    operable_violations: int = Field(0, description="WCAG Principle 2 (2.x.x)")
    understandable_violations: int = Field(0, description="WCAG Principle 3 (3.x.x)")
    robust_violations: int = Field(0, description="WCAG Principle 4 (4.x.x)")

    # ── High-Signal Individual Rules ───────────────────────────────────────────
    missing_alt_text_count: int = Field(0, description="Images without alt attribute (WCAG 1.1.1)")
    missing_form_labels_count: int = Field(0, description="Inputs without associated labels (WCAG 1.3.1)")
    keyboard_trap_detected: bool = Field(False, description="Focus trap violations (WCAG 2.1.2)")
    missing_skip_link: bool = Field(False, description="No skip navigation link (WCAG 2.4.1)")
    missing_page_title: bool = Field(False, description="<title> missing or empty (WCAG 2.4.2)")
    missing_lang_attribute: bool = Field(False, description="<html lang> missing (WCAG 3.1.1)")

    # ── Metadata ──────────────────────────────────────────────────────────────
    axe_core_version: str
    total_dom_elements_scanned: Optional[int] = None

    def summarize(self) -> Dict[str, Any]:
        return {
            "total_violations": len(self.violations),
            "critical": self.critical_count,
            "serious": self.serious_count,
            "moderate": self.moderate_count,
            "minor": self.minor_count,
            "manual_review_needed": len(self.incomplete),
            "missing_alt_text": self.missing_alt_text_count,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # AXE_INJECTION_FAILED → axe-core CDN unavailable or Content-Security-Policy
    #                        blocks inline script injection.
    #                        Mitigation: inject axe from local file via addScriptTag path.
    # TIMEOUT              → Complex DOM (10k+ elements) takes >30s to scan
    #                        Partial: axe may return partial results before timeout
    # RENDER_TIMEOUT       → Page didn't finish rendering before axe injection
    # BLANK_PAGE           → No DOM to scan (returns empty violations, passes_count=0)
    #
    # CSP mitigation: if CSP blocks script injection, ToolExecutor falls back to
    # serving the page through a local Playwright proxy with CSP headers stripped.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Accessibility Agent
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 12. ContrastChecker
# ═══════════════════════════════════════════════════════════════

class ContrastCheckerInput(BaseModel):
    """
    Used by: Accessibility Agent.
    Purpose: Checks color contrast ratios against WCAG 1.4.3 (Normal Text: 4.5:1)
             and WCAG 1.4.11 (Non-text Contrast: 3:1 for UI components).

    Note: axe-core has a color-contrast rule, but it has high false-negative rates
          on dynamically-styled elements and CSS custom properties.
          ContrastChecker uses Playwright's getComputedStyle to resolve actual rendered colors,
          giving it higher accuracy than axe's static analysis.

    Relationship with AxeCoreScanner:
          axe catches obvious contrast failures (static colors).
          ContrastChecker catches CSS-variable, theme-aware, and pseudo-class contrast issues.
          Both tools run — findings are deduplicated by the Accessibility Agent before writing.
    """
    url: str = Field(
        ...,
        description="The ToolExecutor opens this URL in Playwright to extract computed styles",
    )
    include_placeholder_text: bool = Field(
        True,
        description="Check contrast of input placeholder text (often overlooked)",
    )
    include_disabled_elements: bool = Field(
        False,
        description=(
            "WCAG technically exempts disabled UI controls from contrast requirements. "
            "Set True for thorough audits that want to flag these anyway."
        ),
    )
    check_focus_indicators: bool = Field(
        True,
        description="Check that focus outline/ring meets 3:1 contrast (WCAG 2.4.11 in 2.2)",
    )
    sample_limit: int = Field(
        200,
        description="Maximum number of text elements to check (performance cap)",
    )


class ContrastFailure(BaseModel):
    """A single element that fails the contrast requirement."""
    element_selector: str = Field(..., description="CSS selector path to the failing element")
    html_snippet: str = Field(..., description="Opening tag of the element (truncated)")
    text_sample: Optional[str] = Field(
        None,
        max_length=100,
        description="First 100 chars of the element's text content",
    )
    foreground_color: str = Field(..., description="Computed foreground color as hex: '#333333'")
    background_color: str = Field(..., description="Computed background color as hex: '#e0e0e0'")
    contrast_ratio: float = Field(
        ...,
        ge=1.0,
        le=21.0,
        description="Actual contrast ratio (1:1 = no contrast, 21:1 = max contrast)",
    )
    required_ratio: float = Field(
        ...,
        description="4.5 for normal text, 3.0 for large text and UI components",
    )
    wcag_criterion: str = Field(
        ...,
        description="'1.4.3' for text contrast, '1.4.11' for non-text contrast, '2.4.11' for focus",
    )
    is_large_text: bool = Field(
        False,
        description="Large text (18pt or 14pt bold) has lower requirement (3:1 vs 4.5:1)",
    )
    failure_type: str = Field(
        ...,
        description="text | placeholder | focus_indicator | ui_component",
    )
    suggested_foreground: Optional[str] = Field(
        None,
        description=(
            "Nearest accessible color that meets the ratio requirement "
            "(hex, computed by adjusting lightness). Provided as a starting point only."
        ),
    )


class ContrastCheckerOutput(BaseModel):
    failures: List[ContrastFailure] = Field(default_factory=list)
    total_elements_checked: int = 0
    total_failures: int = 0

    # ── Breakdown by Type ──────────────────────────────────────────────────────
    text_contrast_failures: int = 0
    ui_component_failures: int = 0
    focus_indicator_failures: int = 0
    placeholder_failures: int = 0

    # ── Severity Signals ───────────────────────────────────────────────────────
    worst_contrast_ratio: Optional[float] = Field(
        None,
        description="The single worst ratio found (lowest number = worst contrast)",
    )
    worst_contrast_element: Optional[str] = Field(
        None,
        description="Selector of the element with the worst contrast ratio",
    )
    critical_failure_count: int = Field(
        0,
        description=(
            "Failures with ratio < 2.0 — significantly below even the AA threshold. "
            "Near-invisible text for users with visual impairments."
        ),
    )

    # ── False Positive Flags ───────────────────────────────────────────────────
    skipped_gradient_backgrounds: int = Field(
        0,
        description=(
            "Elements skipped because background is a gradient (cannot reliably measure). "
            "Reported for transparency — these should be manually reviewed."
        ),
    )
    skipped_image_backgrounds: int = Field(
        0,
        description="Elements skipped because text sits on a background image",
    )
    skipped_css_variable_backgrounds: int = Field(
        0,
        description=(
            "Elements where background color resolved to a CSS variable that "
            "couldn't be computed at analysis time (dark mode toggle not activated, etc.)"
        ),
    )

    def summarize(self) -> Dict[str, Any]:
        return {
            "total_failures": self.total_failures,
            "text_failures": self.text_contrast_failures,
            "focus_failures": self.focus_indicator_failures,
            "critical_failures": self.critical_failure_count,
            "worst_ratio": self.worst_contrast_ratio,
            "elements_checked": self.total_elements_checked,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # PLAYWRIGHT_CRASH    → Browser crashed during getComputedStyle evaluation
    # TIMEOUT             → Too many elements to process within time budget
    #                       (sample_limit cap prevents this in practice)
    # Partial results     → If timeout occurs, failures found so far are returned
    #                       with a note that the scan was incomplete
    #
    # Known limitations:
    #   - Dynamic themes (dark/light toggle) require two runs to capture both modes
    #   - SVG text contrast is not supported by this tool (covered by axe partially)
    #   - Canvas elements cannot be analyzed
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Accessibility Agent
    # Type: Deterministic
