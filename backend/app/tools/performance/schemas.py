"""
Performance Tool Schemas
────────────────────────
Two tools, run sequentially by the Performance Agent:
  1. LighthouseRunner  — authoritative Core Web Vitals + opportunities
  2. AssetAnalyzer     — detailed asset-level breakdown that Lighthouse summarizes

The Performance Agent reads site_profile.rendering_strategy from SharedState:
  - CSR sites: LighthouseRunner uses longer timeout (JS bundle parse time)
  - CSR sites: AssetAnalyzer weights JS bundle size more heavily
  - SSG sites: CachingHeadersGroup from HeaderAnalyzer is cross-referenced (cache miss = wasted SSG)

Environment note on LighthouseRunner:
  Lighthouse results vary by machine load, network, and browser version.
  The ToolExecutor runs it twice and takes the median metric values to reduce noise.
  This is noted in the output's measurement_note field.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# 9. LighthouseRunner
# ═══════════════════════════════════════════════════════════════

class LighthouseRunnerInput(BaseModel):
    """
    Used by: Performance Agent.
    Purpose: Executes Google Lighthouse via CLI subprocess and parses the JSON report.
             This is the single authoritative source for Core Web Vitals scores.
             Lighthouse covers: performance, accessibility, SEO, and best-practices scores,
             but the Performance Agent only consumes the performance section — other
             agents have dedicated tools for their domains.
    """
    url: str
    form_factor: str = Field(
        "mobile",
        description=(
            "Mobile is Lighthouse default and aligns with Google's mobile-first indexing. "
            "Desktop run is optional — provide both if audit_depth=deep."
        ),
    )
    throttling_method: str = Field(
        "simulate",
        description=(
            "'simulate' (default): applies simulated CPU/network throttling in software. "
            "'devtools': uses Chrome DevTools Protocol for real throttling (more accurate, slower). "
            "Use 'simulate' for speed; 'devtools' for deep audits."
        ),
    )
    categories: List[str] = Field(
        default_factory=lambda: ["performance"],
        description=(
            "Which Lighthouse categories to run. Limiting to ['performance'] "
            "cuts run time by ~60% — other categories are covered by dedicated tools."
        ),
    )
    timeout_ms: int = Field(
        120_000,
        description="Lighthouse has its own internal timeout; this is the subprocess kill timeout",
    )
    extra_headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers to inject (useful for auth-protected pages)",
    )
    runs: int = Field(
        2,
        ge=1,
        le=3,
        description="Number of Lighthouse runs. Median metrics are reported. Min 2 recommended.",
    )


class CoreWebVital(BaseModel):
    """A single Core Web Vital or performance metric."""
    metric_id: str = Field(..., description="E.g. 'largest-contentful-paint'")
    display_name: str = Field(..., description="E.g. 'Largest Contentful Paint'")
    value: float = Field(..., description="Raw numeric value")
    unit: str = Field(..., description="millisecond | second | score | unitless")
    display_value: str = Field(..., description="Formatted value: '4.2 s', '0.23', etc.")
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Lighthouse normalized score: 0.0–0.49 poor, 0.5–0.89 needs work, 0.9–1.0 good",
    )
    band: str = Field(..., description="poor | needs-improvement | good")
    threshold_good: str = Field(..., description="E.g. '< 2.5 s'")
    threshold_poor: str = Field(..., description="E.g. '> 4 s'")


class LighthouseOpportunity(BaseModel):
    """A Lighthouse-identified improvement opportunity with estimated savings."""
    id: str = Field(..., description="Lighthouse audit ID: 'render-blocking-resources', etc.")
    title: str
    description: str
    estimated_savings_ms: Optional[int] = None
    estimated_savings_kb: Optional[float] = None
    score: float = Field(..., ge=0.0, le=1.0)
    details: Optional[Dict[str, Any]] = Field(
        None,
        description="Lighthouse audit details table — truncated for storage",
    )


class LighthouseDiagnostic(BaseModel):
    """A Lighthouse diagnostic (informational, no direct score impact)."""
    id: str
    title: str
    description: str
    display_value: Optional[str] = None


class LighthouseRunnerOutput(BaseModel):
    # ── Scores ─────────────────────────────────────────────────────────────────
    performance_score: int = Field(
        ...,
        ge=0,
        le=100,
        description="0–49 poor, 50–89 needs improvement, 90–100 good",
    )

    # ── Core Web Vitals (these 3 are Google ranking signals) ──────────────────
    lcp: CoreWebVital = Field(..., description="Largest Contentful Paint — loading speed")
    fid: CoreWebVital = Field(..., description="First Input Delay — interactivity (legacy metric)")
    cls: CoreWebVital = Field(..., description="Cumulative Layout Shift — visual stability")

    # ── Additional Metrics ─────────────────────────────────────────────────────
    inp: Optional[CoreWebVital] = Field(
        None,
        description="Interaction to Next Paint — new Core Web Vital replacing FID in 2024",
    )
    ttfb: CoreWebVital = Field(..., description="Time to First Byte — server response speed")
    fcp: CoreWebVital = Field(..., description="First Contentful Paint — perceived load start")
    speed_index: CoreWebVital = Field(..., description="How quickly content is visually populated")
    total_blocking_time: CoreWebVital = Field(
        ...,
        description="Sum of long task blocking time — strong FID/INP predictor",
    )
    time_to_interactive: Optional[CoreWebVital] = None

    # ── Opportunities & Diagnostics ────────────────────────────────────────────
    opportunities: List[LighthouseOpportunity] = Field(
        default_factory=list,
        description="Ordered by estimated savings (highest impact first)",
    )
    diagnostics: List[LighthouseDiagnostic] = Field(default_factory=list)

    # ── Run Metadata ───────────────────────────────────────────────────────────
    runs_performed: int
    form_factor: str
    measurement_note: str = Field(
        ...,
        description=(
            "Explains measurement conditions. "
            "E.g. 'Median of 2 mobile runs with simulated 4G throttling. "
            "Results may vary by up to ±15% depending on server load.'"
        ),
    )
    lighthouse_version: str
    chrome_version: str

    def summarize(self) -> Dict[str, Any]:
        return {
            "performance_score": self.performance_score,
            "lcp": self.lcp.display_value,
            "lcp_band": self.lcp.band,
            "cls": self.cls.display_value,
            "cls_band": self.cls.band,
            "ttfb": self.ttfb.display_value,
            "tbt": self.total_blocking_time.display_value,
            "top_opportunity": self.opportunities[0].title if self.opportunities else None,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # LIGHTHOUSE_UNAVAILABLE → `lighthouse` CLI not found in PATH or node not installed
    # LIGHTHOUSE_FAILED      → Lighthouse process exited with non-zero code
    #                          (network errors, page crash, headless Chrome issues)
    # TIMEOUT                → Lighthouse subprocess exceeded timeout_ms
    #                          Common on very large SPAs or slow servers
    # HTTP_ERROR             → Page returned 4xx/5xx (Lighthouse still runs but scores 0)
    #
    # Graceful degradation: If Lighthouse fails, Performance Agent falls back to
    # AssetAnalyzer only and marks CWV findings with confidence=0.5 (estimated).
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Performance Agent
    # Type: Deterministic (but environment-dependent — see measurement_note)


# ═══════════════════════════════════════════════════════════════
# 10. AssetAnalyzer
# ═══════════════════════════════════════════════════════════════

class AssetAnalyzerInput(BaseModel):
    """
    Used by: Performance Agent.
    Purpose: Granular asset-level analysis that Lighthouse summarizes but doesn't detail.
             Identifies specific files that should be optimized, compressed, converted,
             or deferred — with file-level evidence for each recommendation.
             Consumes PlaywrightCrawler.network_requests (no re-fetch for listed assets)
             but fetches individual asset headers to check compression/cache.
    """
    html: str = Field(..., description="Rendered HTML for link/script/img tag analysis")
    base_url: str
    network_requests: List[Dict[str, Any]] = Field(
        ...,
        description="NetworkRequest list from PlaywrightCrawlerOutput (serialized)",
    )
    fetch_asset_headers: bool = Field(
        True,
        description=(
            "If True, sends HEAD requests to each asset URL to get Content-Encoding, "
            "Cache-Control, etc. Set False in testing to skip network calls."
        ),
    )
    max_assets_to_fetch: int = Field(
        50,
        description="Cap on HEAD requests to avoid excessive network calls",
    )


class ImageAsset(BaseModel):
    src: str
    alt_text: Optional[str] = None
    format: str = Field(..., description="jpeg | png | webp | avif | gif | svg | unknown")
    file_size_bytes: Optional[int] = None
    display_width_px: Optional[int] = Field(None, description="Rendered width in viewport")
    natural_width_px: Optional[int] = Field(None, description="Intrinsic image width")
    is_oversized: bool = Field(
        False,
        description="True if natural_width > 2× display_width (serving too large an image)",
    )
    has_lazy_loading: bool = False
    has_explicit_dimensions: bool = Field(
        False,
        description="width and height attributes set (prevents CLS)",
    )
    is_lcp_candidate: bool = Field(
        False,
        description="True if this image is above-fold and likely the LCP element",
    )
    is_modern_format: bool = Field(
        False,
        description="True if WebP or AVIF — significant size savings over JPEG/PNG",
    )
    optimization_potential_kb: Optional[float] = Field(
        None,
        description="Estimated KB savings from format conversion + compression",
    )


class ScriptAsset(BaseModel):
    src: Optional[str] = Field(None, description="None if inline script")
    is_inline: bool = False
    is_render_blocking: bool = Field(
        False,
        description="True if <script> without async or defer in <head>",
    )
    has_async: bool = False
    has_defer: bool = False
    is_module: bool = False
    file_size_bytes: Optional[int] = None
    is_minified: bool = Field(False, description="Heuristic based on avg line length")
    is_third_party: bool = Field(False, description="src domain differs from base_url domain")
    third_party_category: Optional[str] = Field(
        None,
        description="analytics | advertising | chat | social | other",
    )


class StylesheetAsset(BaseModel):
    href: Optional[str] = None
    is_inline: bool = False
    is_render_blocking: bool = Field(
        False,
        description="True if <link rel='stylesheet'> without media='print' or preload pattern",
    )
    file_size_bytes: Optional[int] = None
    is_minified: bool = False


class CompressionInfo(BaseModel):
    encoding: Optional[str] = Field(None, description="gzip | br | zstd | identity (uncompressed)")
    is_compressed: bool = False
    compressed_size_bytes: Optional[int] = None
    uncompressed_size_bytes: Optional[int] = None
    compression_ratio: Optional[float] = None


class AssetAnalyzerOutput(BaseModel):
    # ── Images ─────────────────────────────────────────────────────────────────
    images: List[ImageAsset] = Field(default_factory=list)
    total_image_count: int = 0
    images_missing_dimensions: int = Field(0, description="CLS risk")
    images_without_lazy_load: int = Field(0, description="Below-fold images that load eagerly")
    images_in_legacy_format: int = Field(0, description="JPEG/PNG that should be WebP/AVIF")
    oversized_images: int = 0
    total_image_kb: Optional[float] = None
    potential_image_savings_kb: Optional[float] = None

    # ── JavaScript ─────────────────────────────────────────────────────────────
    scripts: List[ScriptAsset] = Field(default_factory=list)
    render_blocking_scripts: int = 0
    unminified_scripts: int = 0
    third_party_scripts: int = 0
    total_js_kb: Optional[float] = None

    # ── Stylesheets ────────────────────────────────────────────────────────────
    stylesheets: List[StylesheetAsset] = Field(default_factory=list)
    render_blocking_stylesheets: int = 0
    unminified_stylesheets: int = 0
    total_css_kb: Optional[float] = None

    # ── Compression ────────────────────────────────────────────────────────────
    compression_info: Optional[CompressionInfo] = Field(
        None,
        description="Compression status of the main HTML document response",
    )
    assets_without_compression: int = Field(
        0,
        description="Text assets (JS, CSS, HTML) served without gzip/br",
    )

    # ── Page Weight Summary ────────────────────────────────────────────────────
    total_page_weight_kb: Optional[float] = None
    total_requests: int = 0
    third_party_requests: int = 0
    third_party_kb: Optional[float] = None

    def summarize(self) -> Dict[str, Any]:
        return {
            "total_page_kb": self.total_page_weight_kb,
            "total_image_kb": self.total_image_kb,
            "total_js_kb": self.total_js_kb,
            "render_blocking_scripts": self.render_blocking_scripts,
            "images_legacy_format": self.images_in_legacy_format,
            "potential_image_savings_kb": self.potential_image_savings_kb,
            "third_party_requests": self.third_party_requests,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # ASSET_FETCH_FAILED → HEAD requests for asset headers fail (403, timeout, CDN block)
    #                      Partial: image/script/stylesheet lists still populated from HTML;
    #                      compression and exact sizes will be None.
    # PARSE_ERROR        → HTML parsing error (partial data available)
    #
    # Auth-protected assets: assets behind login will return 401/403 on HEAD requests.
    #                        These are skipped (not counted as broken).
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Performance Agent
    # Type: Deterministic
