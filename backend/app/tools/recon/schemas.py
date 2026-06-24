"""
Recon Tool Schemas
──────────────────
Tools in this category are used exclusively during the Orchestrator's
Reconnaissance Phase, before any specialist agents are dispatched.

They collectively produce the raw material that becomes SiteProfile.
No AI is involved at this layer — these are all deterministic extractors.
The Orchestrator's AI layer (claude_site_classification, claude_audit_planner)
consumes their output to produce the SiteProfile and AuditPlan.

Execution order within Recon (sequential dependencies):
  1. PlaywrightCrawler    → produces html + headers + timings
  2. HeaderAnalyzer       → consumes headers from (1)
  3. TechStackDetector    → consumes html + headers from (1)
  4. LinkExtractor        → consumes html from (1)
  5. ScreenshotCapture    → can run in parallel with 2-4 (separate browser context)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.tools.base import ExtractedLink, HttpHeaders, PageTimings, RedirectHop


# ═══════════════════════════════════════════════════════════════
# 1. PlaywrightCrawler
# ═══════════════════════════════════════════════════════════════

class PlaywrightCrawlerInput(BaseModel):
    """
    Used by: Orchestrator (recon), SEO Agent, Accessibility Agent
    Purpose: Single source of truth for page HTML — both static and rendered.
             Most other tools consume this output rather than fetching independently.
    """
    url: str = Field(..., description="The URL to crawl (must be absolute)")
    wait_strategy: str = Field(
        "networkidle",
        description=(
            "Playwright wait condition before capturing rendered HTML. "
            "'networkidle' waits until no network requests for 500ms. "
            "'domcontentloaded' is faster but may miss lazy-loaded content. "
            "'load' is a middle ground."
        ),
    )
    wait_for_selector: Optional[str] = Field(
        None,
        description="CSS selector to wait for before capturing (overrides wait_strategy if set)",
    )
    timeout_ms: int = Field(
        30_000,
        ge=5_000,
        le=120_000,
        description="Maximum time to wait for page load before aborting",
    )
    viewport_width: int = Field(1440, description="Browser viewport width in px")
    viewport_height: int = Field(900, description="Browser viewport height in px")
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string. Defaults to standard Chrome UA if None.",
    )
    block_resource_types: List[str] = Field(
        default_factory=lambda: ["font", "media"],
        description=(
            "Playwright resource types to block for speed. "
            "Never block 'script' — it prevents CSR pages from rendering. "
            "Never block 'image' — needed for LCP analysis."
        ),
    )
    capture_network_requests: bool = Field(
        True,
        description="Whether to record all network requests made during page load",
    )

    @field_validator("url")
    @classmethod
    def must_be_absolute(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("PlaywrightCrawler requires an absolute URL")
        return v


class NetworkRequest(BaseModel):
    url: str
    resource_type: str      # document, script, stylesheet, image, xhr, fetch, etc.
    method: str
    status_code: Optional[int] = None
    response_size_bytes: Optional[int] = None
    duration_ms: Optional[int] = None
    initiator: Optional[str] = None


class ConsoleMessage(BaseModel):
    level: str              # log, warn, error, info
    text: str
    source_url: Optional[str] = None
    line_number: Optional[int] = None


class PlaywrightCrawlerOutput(BaseModel):
    """
    The foundational data packet consumed by almost all downstream tools.
    Treat this as the single crawl — tools should NOT re-crawl independently.
    """
    url: str = Field(..., description="The input URL")
    final_url: str = Field(..., description="URL after following all redirects")
    redirect_chain: List[RedirectHop] = Field(default_factory=list)
    http_status_code: int

    # ── HTML Content ────────────────────────────────────────────
    static_html: str = Field(
        ...,
        description=(
            "Raw HTML from the initial HTTP response, before any JS execution. "
            "Used for: fast parsing, SSR detection, robot-visible content analysis."
        ),
    )
    rendered_html: str = Field(
        ...,
        description=(
            "HTML captured after Playwright fully renders the page (JS executed). "
            "Used for: accessibility scanning (axe), content extraction, SPA analysis."
        ),
    )
    static_word_count: int = Field(..., description="Word count in static_html (for rendering classification)")
    rendered_word_count: int = Field(..., description="Word count in rendered_html")

    # ── Headers ──────────────────────────────────────────────────
    response_headers: HttpHeaders

    # ── Timings ──────────────────────────────────────────────────
    page_timings: PageTimings

    # ── Browser Telemetry ─────────────────────────────────────────
    console_messages: List[ConsoleMessage] = Field(default_factory=list)
    network_requests: List[NetworkRequest] = Field(default_factory=list)
    has_javascript_errors: bool = Field(
        False,
        description="True if any console messages had level='error'",
    )
    total_requests: int = Field(0)
    total_transfer_kb: Optional[float] = None

    def summarize(self) -> Dict[str, Any]:
        return {
            "final_url": self.final_url,
            "status_code": self.http_status_code,
            "redirect_count": len(self.redirect_chain),
            "static_word_count": self.static_word_count,
            "rendered_word_count": self.rendered_word_count,
            "js_errors": self.has_javascript_errors,
            "total_requests": self.total_requests,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # TIMEOUT          → page never reached networkidle within timeout_ms
    # URL_UNREACHABLE  → DNS failure, connection refused, or ECONNRESET
    # HTTP_ERROR       → 4xx/5xx on the initial response (status_code field still set)
    # REDIRECT_LOOP    → Playwright aborted after detecting circular redirects
    # PLAYWRIGHT_CRASH → Browser process died (OOM, segfault)
    # BLANK_PAGE       → Page loaded (200) but rendered_html has <100 words (gated content,
    #                    cookie consent wall, or full JS failure)
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Orchestrator, SEO, Accessibility, Content, Technical
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 2. ScreenshotCapture
# ═══════════════════════════════════════════════════════════════

class ScreenshotCaptureInput(BaseModel):
    """
    Used by: Orchestrator only.
    Purpose: Visual record attached to SiteProfile. Used for:
             (a) Evidence in the report UI
             (b) Input to claude_site_classification (multimodal)
    """
    url: str
    full_page: bool = Field(
        True,
        description="Capture full scrollable page height vs. viewport only",
    )
    viewport_width: int = Field(1440)
    viewport_height: int = Field(900)
    clip_to_above_fold: bool = Field(
        False,
        description="If True, captures only the first viewport height (above-the-fold view)",
    )
    output_format: str = Field("png", description="'png' or 'jpeg'")
    quality: Optional[int] = Field(
        None,
        description="JPEG quality 0–100 (jpeg only). None defaults to 80.",
    )


class ScreenshotCaptureOutput(BaseModel):
    file_path: str = Field(
        ...,
        description="Absolute path to the saved screenshot file on disk",
    )
    file_size_bytes: int
    page_width_px: int
    page_height_px: int
    viewport_height_px: int = Field(..., description="Above-fold boundary in pixels")
    format: str

    def summarize(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "page_dimensions": f"{self.page_width_px}×{self.page_height_px}px",
            "file_size_kb": round(self.file_size_bytes / 1024, 1),
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # PLAYWRIGHT_CRASH  → Browser died during screenshot
    # BLANK_PAGE        → Screenshot is entirely white/black (rendering failure)
    # TIMEOUT           → Page still loading when screenshot was attempted
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Orchestrator
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 3. TechStackDetector
# ═══════════════════════════════════════════════════════════════

class TechStackDetectorInput(BaseModel):
    """
    Used by: Orchestrator.
    Purpose: Fingerprints the technology stack using pattern matching against:
             HTML content (script src patterns, meta generator tags, CSS class patterns),
             HTTP response headers (X-Powered-By, X-Generator, Set-Cookie names),
             JavaScript global variable patterns (window.React, window.angular, etc.),
             Known script URL patterns (cdn.shopify.com, static.hotjar.com, etc.)
    """
    html: str = Field(..., description="Rendered HTML (post-JS execution preferred)")
    response_headers: HttpHeaders
    cookie_names: List[str] = Field(
        default_factory=list,
        description="Cookie names from Set-Cookie headers (useful for platform detection)",
    )
    script_urls: List[str] = Field(
        default_factory=list,
        description="All script src URLs from the page (from network_requests)",
    )


class DetectedTechnology(BaseModel):
    name: str
    version: Optional[str] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    signals: List[str] = Field(
        ...,
        min_length=1,
        description="The specific patterns that identified this technology",
    )
    category: str = Field(
        ...,
        description="frontend_framework | meta_framework | cms | ecommerce | cdn | analytics | other",
    )


class TechStackDetectorOutput(BaseModel):
    detected_technologies: List[DetectedTechnology] = Field(default_factory=list)

    # ── Convenience accessors (mirrors TechStack model in site_profile.py) ───
    frontend_framework: Optional[DetectedTechnology] = None
    meta_framework: Optional[DetectedTechnology] = None
    cms: Optional[DetectedTechnology] = None
    ecommerce_platform: Optional[DetectedTechnology] = None
    cdn: Optional[DetectedTechnology] = None
    analytics_tools: List[DetectedTechnology] = Field(default_factory=list)
    tag_manager: Optional[DetectedTechnology] = None
    ab_testing: Optional[DetectedTechnology] = None
    chat_widget: Optional[DetectedTechnology] = None
    error_tracking: Optional[DetectedTechnology] = None

    # ── Rendering classification signals ─────────────────────────────────────
    ssr_headers_present: bool = Field(
        False,
        description="x-nextjs-*, x-nuxt-*, x-powered-by: Next.js, etc. detected",
    )
    hydration_markers_present: bool = Field(
        False,
        description="data-reactroot, ng-version, data-server-rendered, etc. detected",
    )

    def summarize(self) -> Dict[str, Any]:
        return {
            "technologies_detected": len(self.detected_technologies),
            "frontend": self.frontend_framework.name if self.frontend_framework else None,
            "meta_framework": self.meta_framework.name if self.meta_framework else None,
            "cms": self.cms.name if self.cms else None,
            "ecommerce": self.ecommerce_platform.name if self.ecommerce_platform else None,
            "cdn": self.cdn.name if self.cdn else None,
            "analytics_count": len(self.analytics_tools),
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # No hard failures — returns empty lists if nothing detected.
    # Low-accuracy risks (not errors):
    #   - Obfuscated/minified script names (reduces signal count)
    #   - Custom self-hosted tools with no known fingerprint
    #   - Version detection is best-effort; version=None is common and acceptable
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Orchestrator
    # Type: Deterministic (pure pattern matching, no AI)


# ═══════════════════════════════════════════════════════════════
# 4. HeaderAnalyzer
# ═══════════════════════════════════════════════════════════════

class HeaderAnalyzerInput(BaseModel):
    """
    Used by: Orchestrator, Technical Agent, Performance Agent.
    Purpose: Parses and scores HTTP response headers across four domains:
             security, caching, content policy, and server metadata.
             Each specialist agent that needs header data calls this tool
             with the same raw headers — they do not re-fetch.
    """
    response_headers: HttpHeaders
    url: str = Field(..., description="Needed to assess HTTPS enforcement (HSTS)")


class HeaderPresence(BaseModel):
    """Analysis of a single HTTP header."""
    header_name: str
    present: bool
    value: Optional[str] = None
    score: int = Field(..., ge=0, le=10, description="0=missing critical, 10=optimal value")
    assessment: str = Field(..., description="Brief evaluation of the header's value")
    recommendation: Optional[str] = Field(
        None,
        description="What to set it to. Null if present and correct.",
    )


class SecurityHeadersGroup(BaseModel):
    content_security_policy: HeaderPresence
    strict_transport_security: HeaderPresence
    x_frame_options: HeaderPresence
    x_content_type_options: HeaderPresence
    referrer_policy: HeaderPresence
    permissions_policy: HeaderPresence
    overall_score: int = Field(..., ge=0, le=100)


class CachingHeadersGroup(BaseModel):
    cache_control: HeaderPresence
    etag: HeaderPresence
    last_modified: HeaderPresence
    expires: HeaderPresence
    vary: HeaderPresence
    cdn_cache_status: Optional[HeaderPresence] = Field(
        None,
        description="CDN-specific header (CF-Cache-Status, X-Cache, etc.) if detected",
    )
    overall_score: int = Field(..., ge=0, le=100)


class ServerInfoGroup(BaseModel):
    server_header: HeaderPresence = Field(..., description="Whether Server header leaks version info")
    x_powered_by: HeaderPresence = Field(..., description="Framework/version disclosure risk")
    is_https: bool
    http_version: Optional[str] = Field(None, description="HTTP/1.1, HTTP/2, or HTTP/3")


class HeaderAnalyzerOutput(BaseModel):
    security: SecurityHeadersGroup
    caching: CachingHeadersGroup
    server_info: ServerInfoGroup
    all_headers: Dict[str, str] = Field(..., description="All raw response headers for reference")

    def summarize(self) -> Dict[str, Any]:
        return {
            "security_score": self.security.overall_score,
            "caching_score": self.caching.overall_score,
            "is_https": self.server_info.is_https,
            "http_version": self.server_info.http_version,
            "csp_present": self.security.content_security_policy.present,
            "hsts_present": self.security.strict_transport_security.present,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # No hard failures — all header fields are Optional; absent = analyzed as missing.
    # Key nuance: CDN providers may strip or add headers at the edge vs. origin.
    #             This tool analyzes what the client receives, not what origin sends.
    #             The Technical Agent's findings should note this distinction.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Orchestrator, Technical Agent, Performance Agent
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 5. LinkExtractor
# ═══════════════════════════════════════════════════════════════

class LinkExtractorInput(BaseModel):
    """
    Used by: SEO Agent (internal link graph), Technical Agent (broken link detection).
    Purpose: Extracts, normalizes, and categorizes all hyperlinks from a page.
             Runs on rendered HTML — ensures JS-rendered navigation links are captured.
             Both agents receive the same output; SEO uses link graph, Technical uses
             the full list for reachability checking.
    """
    html: str = Field(..., description="Rendered HTML (post-JS preferred for SPAs)")
    base_url: str = Field(..., description="Base URL for resolving relative hrefs")
    include_asset_links: bool = Field(
        True,
        description="Whether to extract src attributes from img, script, link elements",
    )
    deduplicate: bool = Field(
        True,
        description="Return unique links by normalized_url (default True for analysis use cases)",
    )


class AssetReference(BaseModel):
    """A non-hyperlink asset reference (image, script, stylesheet, etc.)"""
    src: str = Field(..., description="Raw src/href attribute value")
    normalized_url: Optional[str] = None
    asset_type: str = Field(..., description="image | script | stylesheet | font | other")
    is_external: bool
    has_crossorigin: bool = False
    has_integrity: bool = Field(False, description="Subresource Integrity attribute present")


class LinkExtractorOutput(BaseModel):
    internal_links: List[ExtractedLink] = Field(
        default_factory=list,
        description="Links pointing to the same domain (normalized to exclude fragments)",
    )
    external_links: List[ExtractedLink] = Field(
        default_factory=list,
        description="Links pointing to other domains",
    )
    asset_references: List[AssetReference] = Field(
        default_factory=list,
        description="img src, script src, link href (stylesheets) — not hyperlinks",
    )

    # ── Computed Stats ─────────────────────────────────────────────────────────
    total_internal: int = 0
    total_external: int = 0
    internal_nofollow_count: int = Field(
        0,
        description="Internal links with rel=nofollow (usually unintentional — worth flagging)",
    )
    external_nofollow_count: int = 0
    new_tab_internal_count: int = Field(
        0,
        description="Internal links that open in a new tab (UX antipattern for internal nav)",
    )
    javascript_href_count: int = Field(
        0,
        description="Links with href='javascript:void(0)' or '#' — not crawlable",
    )

    def summarize(self) -> Dict[str, Any]:
        return {
            "internal_links": self.total_internal,
            "external_links": self.total_external,
            "assets": len(self.asset_references),
            "internal_nofollow": self.internal_nofollow_count,
            "js_hrefs": self.javascript_href_count,
        }

    @property
    def links(self) -> List[ExtractedLink]:
        """All links (internal + external). Compatibility accessor — agent code uses .links."""
        return self.internal_links + self.external_links

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # PARSE_ERROR        → BeautifulSoup could not parse the HTML (malformed)
    #                      Partial: returns whatever links were parsed before failure
    # (No network calls) → This tool only parses; it never fetches. Broken links
    #                      are detected by BrokenLinkChecker, not here.
    # SPA navigation     → Client-side routing using pushState won't produce
    #                      <a href> elements. javascript_href_count will be high.
    #                      Agents should note this when site is classified as CSR.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: SEO Agent, Technical Agent
    # Type: Deterministic
