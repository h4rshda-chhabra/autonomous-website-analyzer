"""
Technical Tool Schemas
───────────────────────
Two tools run within the Technical Agent. Unlike other agents, the Technical Agent
reads from SharedState before calling its tools:

  - Reads SEO Agent findings (if available) to cross-reference canonical + redirect issues.
    A page with both a canonical mismatch AND a redirect chain has a compound issue.
  - Reads HeaderAnalyzer output from SharedState (Orchestrator ran this during Recon).
    The Technical Agent does NOT re-call HeaderAnalyzer — it reads the cached result.

Execution order within Technical Agent:
  1. SecurityHeaderAnalyzer  → reads HeaderAnalyzer.security from SharedState (no re-fetch)
  2. BrokenLinkChecker       → reads LinkExtractor.internal_links from SharedState (no re-parse)
                               makes actual HTTP requests to check reachability
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# 15. SecurityHeaderAnalyzer
# ═══════════════════════════════════════════════════════════════

class SecurityHeaderAnalyzerInput(BaseModel):
    """
    Used by: Technical Agent.
    Purpose: Evaluates HTTP security headers against OWASP recommendations
             and assigns per-header and overall security scores.

    Important: This tool does NOT make HTTP requests. It consumes the
    raw headers already collected by PlaywrightCrawler and HeaderAnalyzer
    during Recon. The Technical Agent reads these from SharedState and
    passes them here.

    Scope: Evaluates the root URL's headers only. Headers may differ per
    page (e.g., CSP relaxed on certain paths). Deep audits should check
    multiple pages — deferred to post-MVP.
    """
    response_headers: Dict[str, str] = Field(
        ...,
        description="Raw response headers from PlaywrightCrawlerOutput (lowercase keys)",
    )
    url: str = Field(..., description="The URL these headers were collected from")
    is_https: bool = Field(..., description="From HeaderAnalyzer — needed for HSTS assessment")


class SecurityHeaderEvaluation(BaseModel):
    """Detailed evaluation of a single security header."""
    header_name: str
    present: bool
    current_value: Optional[str] = None
    score: int = Field(..., ge=0, le=10)
    grade: str = Field(..., description="A | B | C | D | F")
    assessment: str = Field(
        ...,
        description="Plain-language assessment of the current value (or absence)",
    )
    risk_if_absent: str = Field(
        ...,
        description="What attack vector this header mitigates",
    )
    recommended_value: Optional[str] = Field(
        None,
        description="The recommended header value. None if current value is already good.",
    )
    caveats: Optional[str] = Field(
        None,
        description=(
            "Implementation nuances. E.g. 'CSP requires testing — an overly strict policy "
            "can break third-party scripts. Start with report-only mode.'"
        ),
    )


class CSPAnalysis(BaseModel):
    """Detailed breakdown of the Content-Security-Policy header."""
    present: bool
    value: Optional[str] = None
    has_unsafe_inline: bool = Field(
        False,
        description="'unsafe-inline' in script-src — significantly weakens XSS protection",
    )
    has_unsafe_eval: bool = Field(
        False,
        description="'unsafe-eval' in script-src — allows dynamic JS execution",
    )
    has_wildcard_sources: bool = Field(
        False,
        description="Wildcard (*) in any directive — defeats the purpose of allowlisting",
    )
    is_report_only: bool = Field(
        False,
        description="Content-Security-Policy-Report-Only header detected (not enforced)",
    )
    directives_present: List[str] = Field(
        default_factory=list,
        description="List of CSP directives detected: ['default-src', 'script-src', ...]",
    )
    missing_recommended_directives: List[str] = Field(
        default_factory=list,
        description="Directives not present that OWASP recommends: ['frame-ancestors', 'base-uri']",
    )
    effective_score: int = Field(
        ...,
        ge=0,
        le=10,
        description="0 = absent, 1-3 = present but weak, 7-10 = well-configured",
    )


class HSTSAnalysis(BaseModel):
    """Detailed breakdown of the Strict-Transport-Security header."""
    present: bool
    value: Optional[str] = None
    max_age_seconds: Optional[int] = Field(
        None,
        description="Recommended minimum: 31536000 (1 year)",
    )
    includes_subdomains: bool = False
    includes_preload: bool = Field(
        False,
        description="Required for HSTS preload list submission",
    )
    is_preload_eligible: bool = Field(
        False,
        description="True if max-age ≥ 31536000 and includeSubDomains and preload are set",
    )


class SecurityHeaderAnalyzerOutput(BaseModel):
    # ── Per-Header Analysis ────────────────────────────────────────────────────
    content_security_policy: CSPAnalysis
    strict_transport_security: HSTSAnalysis
    x_frame_options: SecurityHeaderEvaluation
    x_content_type_options: SecurityHeaderEvaluation
    referrer_policy: SecurityHeaderEvaluation
    permissions_policy: SecurityHeaderEvaluation

    # ── Information Disclosure ─────────────────────────────────────────────────
    server_header_leaks_version: bool = Field(
        False,
        description="Server header reveals software version (e.g. 'Apache/2.4.51')",
    )
    x_powered_by_present: bool = Field(
        False,
        description="X-Powered-By reveals framework/language (e.g. 'PHP/8.1.0')",
    )

    # ── HTTPS Assessment ───────────────────────────────────────────────────────
    is_https: bool
    uses_http_on_https_site: bool = Field(
        False,
        description="Detected mixed content — HTTP resources on an HTTPS page",
    )

    # ── Overall Score ─────────────────────────────────────────────────────────
    overall_security_score: int = Field(..., ge=0, le=100)
    security_grade: str = Field(..., description="A+ | A | B | C | D | F — aligned with securityheaders.com grading")
    critical_missing: List[str] = Field(
        default_factory=list,
        description="Header names that are absent and create significant risk",
    )
    summary: str = Field(
        ...,
        description="2–3 sentence human-readable security posture summary",
    )

    def summarize(self) -> Dict[str, Any]:
        return {
            "overall_score": self.overall_security_score,
            "grade": self.security_grade,
            "csp_present": self.content_security_policy.present,
            "hsts_present": self.strict_transport_security.present,
            "critical_missing": self.critical_missing,
            "leaks_version": self.server_header_leaks_version or self.x_powered_by_present,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # No network calls → no network failures possible
    # Input validation only: empty headers dict returns all-absent analysis
    #
    # Known nuance: CDN providers (Cloudflare, Fastly) add security headers at the
    # edge that the origin server doesn't set. This tool analyzes what the browser
    # receives (edge-level headers). The Technical Agent's finding should clarify:
    # "These headers are added by Cloudflare — configure them at origin to ensure
    # they apply if you change CDN providers."
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Technical Agent
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 16. BrokenLinkChecker
# ═══════════════════════════════════════════════════════════════

class BrokenLinkCheckerInput(BaseModel):
    """
    Used by: Technical Agent.
    Purpose: Checks reachability of all links extracted from the page.
             Makes actual HTTP requests (HEAD, falling back to GET) to each URL.
             Identifies broken links (4xx), server errors (5xx), and redirect chains.

    Consumes LinkExtractor output from SharedState — does NOT re-parse HTML.
    Only checks links that have a resolved normalized_url.
    JavaScript-href links (href='#' or 'javascript:void(0)') are skipped.

    Rate limiting strategy:
      - Internal links: concurrent requests (same domain, no rate limit risk)
      - External links: sequential with 200ms delay (be polite to third-party servers)
      - Timeout per link: 10s HEAD, 15s GET fallback
    """
    internal_links: List[Dict[str, Any]] = Field(
        ...,
        description="Serialized ExtractedLink list from LinkExtractorOutput.internal_links",
    )
    external_links: List[Dict[str, Any]] = Field(
        ...,
        description="Serialized ExtractedLink list from LinkExtractorOutput.external_links",
    )
    base_url: str
    check_external_links: bool = Field(
        True,
        description="Set False to skip external link checking (faster, but less thorough)",
    )
    max_concurrent_internal: int = Field(
        10,
        description="Concurrent requests for internal links",
    )
    timeout_per_link_ms: int = Field(
        10_000,
        description="Per-link timeout. Links exceeding this are reported as SLOW, not broken.",
    )
    slow_threshold_ms: int = Field(
        3_000,
        description="Response time above this is flagged as 'slow' even if not broken",
    )
    follow_redirects: bool = Field(
        True,
        description="Follow redirect chains to find the final destination",
    )
    max_redirects_per_link: int = Field(
        5,
        description="Abort and report if a link has more than this many redirects",
    )
    skip_urls_matching: List[str] = Field(
        default_factory=lambda: ["mailto:", "tel:", "javascript:", "#"],
        description="URL prefixes to skip entirely (not network-checkable)",
    )


class LinkCheckResult(BaseModel):
    """Result of checking a single link's reachability."""
    url: str
    anchor_text: Optional[str] = None
    is_internal: bool
    status_code: Optional[int] = None
    is_broken: bool = Field(
        False,
        description="True if status_code is 4xx or 5xx, or if request timed out",
    )
    is_redirect: bool = False
    redirect_count: int = 0
    final_url: Optional[str] = Field(
        None,
        description="Final destination URL after following redirects",
    )
    redirect_chain: List[str] = Field(
        default_factory=list,
        description="Full sequence of URLs in the redirect chain",
    )
    response_time_ms: Optional[int] = None
    is_slow: bool = Field(False, description="True if response_time_ms > slow_threshold_ms")
    error_type: Optional[str] = Field(
        None,
        description=(
            "timeout | dns_failure | connection_refused | ssl_error | too_many_redirects | none"
        ),
    )
    method_used: str = Field("HEAD", description="HEAD | GET (GET used as HEAD fallback)")


class BrokenLinkCheckerOutput(BaseModel):
    # ── Results ────────────────────────────────────────────────────────────────
    results: List[LinkCheckResult] = Field(default_factory=list)

    # ── Broken Links ──────────────────────────────────────────────────────────
    broken_internal_links: List[LinkCheckResult] = Field(default_factory=list)
    broken_external_links: List[LinkCheckResult] = Field(default_factory=list)
    total_broken: int = 0

    # ── Redirect Issues ────────────────────────────────────────────────────────
    links_with_long_redirect_chains: List[LinkCheckResult] = Field(
        default_factory=list,
        description="Links with 3+ redirects — each redirect adds latency",
    )
    redirect_loop_links: List[LinkCheckResult] = Field(
        default_factory=list,
        description="Links where max_redirects was hit (circular redirect suspected)",
    )

    # ── Performance Flags ─────────────────────────────────────────────────────
    slow_links: List[LinkCheckResult] = Field(
        default_factory=list,
        description="Links that responded but took longer than slow_threshold_ms",
    )

    # ── Summary Stats ──────────────────────────────────────────────────────────
    total_checked: int = 0
    total_internal_checked: int = 0
    total_external_checked: int = 0
    total_skipped: int = Field(
        0,
        description="Links skipped due to skip_urls_matching patterns",
    )
    total_timeouts: int = 0

    # ── Status Code Distribution ───────────────────────────────────────────────
    status_code_distribution: Dict[int, int] = Field(
        default_factory=dict,
        description="{'200': 45, '301': 8, '404': 3, '500': 1}",
    )
    not_found_count: int = Field(0, description="Links returning 404")
    server_error_count: int = Field(0, description="Links returning 5xx")
    unauthorized_count: int = Field(
        0,
        description=(
            "Links returning 401/403 — these are not broken but may be unintentionally "
            "linking to auth-protected content"
        ),
    )

    # ── False Positive Mitigation ──────────────────────────────────────────────
    likely_false_positives: List[str] = Field(
        default_factory=list,
        description=(
            "URLs that returned errors but may not truly be broken: "
            "e.g. LinkedIn always returns 999, some sites block HEAD requests with 405. "
            "These are noted but NOT counted in total_broken."
        ),
    )
    external_links_skipped: bool = Field(
        False,
        description="True if check_external_links=False was set",
    )

    def summarize(self) -> Dict[str, Any]:
        return {
            "total_checked": self.total_checked,
            "total_broken": self.total_broken,
            "broken_internal": len(self.broken_internal_links),
            "broken_external": len(self.broken_external_links),
            "redirect_chain_issues": len(self.links_with_long_redirect_chains),
            "slow_links": len(self.slow_links),
            "not_found": self.not_found_count,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # TIMEOUT          → Total time budget for all checks exceeded
    #                    Partial: results checked so far are returned; unchecked URLs
    #                    are omitted from results (not counted as broken)
    # RATE_LIMITED     → External server returns 429
    #                    These URLs are marked is_broken=False, error_type='rate_limited'
    #                    and noted in likely_false_positives
    #
    # Known false positive sources:
    #   - LinkedIn (returns 999 to bots)
    #   - Twitter/X (returns 403 to non-browsers)
    #   - Sites that block HEAD requests (return 405 — tool retries with GET)
    #   - Cloudflare-protected sites (may return 403 challenge page)
    #   The tool handles the HEAD→GET retry; other cases land in likely_false_positives.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: Technical Agent
    # Type: Deterministic
