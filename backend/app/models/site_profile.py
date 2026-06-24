from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from .enums import AgentType, RenderingStrategy, Severity, SiteCategory


# ─── Sub-models ───────────────────────────────────────────────────────────────

class TechStack(BaseModel):
    """
    Technology fingerprint of the target site.
    All fields are optional — absence means not detected, not necessarily absent.
    """
    frontend_framework: Optional[str] = Field(
        None,
        description="Detected JS framework (React, Vue, Angular, Svelte, etc.)",
    )
    meta_framework: Optional[str] = Field(
        None,
        description="SSR/SSG meta-framework (Next.js, Nuxt, SvelteKit, Astro, etc.)",
    )
    cms: Optional[str] = Field(
        None,
        description="Content management system if present (WordPress, Contentful, Sanity, etc.)",
    )
    ecommerce_platform: Optional[str] = Field(
        None,
        description="Ecommerce platform if detected (Shopify, WooCommerce, Magento, etc.)",
    )
    cdn: Optional[str] = Field(
        None,
        description="CDN provider inferred from response headers or DNS (Cloudflare, Fastly, etc.)",
    )
    analytics: List[str] = Field(
        default_factory=list,
        description="All detected analytics tools (GA4, Mixpanel, Amplitude, etc.)",
    )
    tag_manager: Optional[str] = Field(None, description="GTM, Tealium, etc.")
    ab_testing: Optional[str] = Field(None, description="Optimizely, VWO, etc.")
    chat_widget: Optional[str] = Field(None, description="Intercom, Drift, Crisp, etc.")
    error_tracking: Optional[str] = Field(None, description="Sentry, Datadog RUM, etc.")
    other_detected: List[str] = Field(
        default_factory=list,
        description="Any other third-party scripts or libraries detected",
    )
    detection_signals: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Raw evidence per tool: {'React': ['window.__REACT_DEVTOOLS_GLOBAL_HOOK__', ...]}",
    )


class SiteGoal(BaseModel):
    """
    A detected primary goal of the website.
    Goals are inferred from page structure, CTAs, content, and site category.
    """
    goal: str = Field(..., description="Human-readable goal label (e.g. 'lead generation')")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0–1.0 confidence in this goal")
    signals: List[str] = Field(
        default_factory=list,
        description="Observable signals that led to this inference (e.g. 'prominent Contact Sales CTA')",
    )


class ReconSignal(BaseModel):
    """
    An early anomaly or indicator detected during reconnaissance.
    These are passed to the Orchestrator's planning phase to influence agent depth and focus.
    """
    area: AgentType = Field(
        ...,
        description="Which specialist agent domain this signal belongs to",
    )
    signal: str = Field(..., description="What was observed (concise)")
    implication: str = Field(..., description="Why this warrants deeper investigation")
    suggested_priority: Severity = Field(
        ...,
        description="Estimated severity if this signal proves to be a real issue",
    )


class RenderingEvidence(BaseModel):
    """Evidence used to classify the rendering strategy."""
    initial_html_word_count: int = Field(
        ...,
        description="Word count in the raw HTML before JS execution",
    )
    rendered_html_word_count: int = Field(
        ...,
        description="Word count after Playwright renders the page",
    )
    js_framework_detected: bool
    ssr_headers_detected: bool = Field(
        False,
        description="Presence of x-powered-by, x-nextjs-*, or similar SSR headers",
    )
    hydration_markers_detected: bool = Field(
        False,
        description="Presence of React/Vue/Angular hydration data attributes",
    )
    content_parity_ratio: float = Field(
        ...,
        ge=0.0,
        description="rendered_word_count / initial_word_count. Near 1.0 = SSR/SSG. <<1.0 = CSR.",
    )


# ─── Core Model ───────────────────────────────────────────────────────────────

class SiteProfile(BaseModel):
    """
    Output of the Orchestrator's Reconnaissance Phase.
    This is the foundation of the entire audit — it drives the AuditPlan.
    Persisted once and referenced by all downstream agents.
    """

    id: UUID = Field(default_factory=uuid4)
    audit_id: UUID = Field(..., description="Parent audit session")
    url: str = Field(..., description="Normalized canonical URL that was audited")

    # ── Classification (AI-inferred) ────────────────────────────────────────
    category: SiteCategory = Field(
        ...,
        description="Detected website category. Drives audit plan specialization.",
    )
    category_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence in the category classification",
    )
    category_reasoning: str = Field(
        ...,
        description="Brief explanation of why this category was assigned",
    )

    # ── Rendering (deterministic + AI-confirmed) ────────────────────────────
    rendering_strategy: RenderingStrategy
    rendering_evidence: RenderingEvidence = Field(
        ...,
        description="Structured evidence that determined the rendering classification",
    )

    # ── Technology ──────────────────────────────────────────────────────────
    tech_stack: TechStack

    # ── Goals ───────────────────────────────────────────────────────────────
    primary_goals: List[SiteGoal] = Field(
        ...,
        min_length=1,
        description="Ordered list of detected goals (most confident first)",
    )

    # ── Early Signals ───────────────────────────────────────────────────────
    recon_signals: List[ReconSignal] = Field(
        default_factory=list,
        description="Anomalies detected during recon that inform audit depth and agent focus",
    )

    # ── Raw Page Data ───────────────────────────────────────────────────────
    page_title: Optional[str] = None
    meta_description: Optional[str] = None
    h1_text: Optional[str] = None
    response_time_ms: Optional[int] = None
    http_status_code: int = Field(..., description="Initial HTTP response code")
    final_url: str = Field(
        ...,
        description="URL after all redirects — may differ from input URL",
    )
    redirect_count: int = Field(0, description="Number of redirects followed")
    is_https: bool
    has_javascript_errors: bool = Field(
        False,
        description="Whether Playwright detected console errors during render",
    )
    screenshot_path: Optional[str] = Field(
        None,
        description="Local or cloud path to the full-page screenshot",
    )

    # ── Meta ─────────────────────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("url", "final_url", mode="before")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        return v.rstrip("/").lower()

    @model_validator(mode="after")
    def validate_goals_have_confidence(self) -> "SiteProfile":
        for goal in self.primary_goals:
            if goal.confidence < 0.0 or goal.confidence > 1.0:
                raise ValueError(f"Goal confidence out of range: {goal.goal}")
        return self

    @property
    def top_goal(self) -> SiteGoal:
        return max(self.primary_goals, key=lambda g: g.confidence)

    @property
    def is_spa(self) -> bool:
        return self.rendering_strategy == RenderingStrategy.CSR

    @property
    def recon_signal_areas(self) -> List[AgentType]:
        return list({s.area for s in self.recon_signals})

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://example.com",
                "category": "saas",
                "category_confidence": 0.91,
                "rendering_strategy": "csr",
                "primary_goals": [
                    {"goal": "trial signups", "confidence": 0.88, "signals": ["'Start Free Trial' CTA above fold"]}
                ],
            }
        }
