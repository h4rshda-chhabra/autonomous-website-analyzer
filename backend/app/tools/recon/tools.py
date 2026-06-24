"""
Recon Tool Implementations — Phase 1A + 1B
==========================================
All five recon tools that feed the Orchestrator's reconnaissance phase.

Phase 1A (deterministic, no browser):
  HeaderAnalyzer    — Scores HTTP security and caching headers from existing data.
  TechStackDetector — Fingerprints frameworks, CMSs, CDNs, analytics via regex patterns.
  LinkExtractor     — Extracts and classifies hyperlinks and asset refs with BeautifulSoup.

Phase 1B (Playwright-dependent):
  PlaywrightCrawler  — Headless Chromium crawl: static HTML, rendered HTML, headers,
                       redirect chain, console errors, network requests, page timings.
  ScreenshotCapture  — Full-page PNG/JPEG screenshot saved to disk.

Contract for every tool function:
  Input:  pre-validated *Input Pydantic model (ToolExecutorImpl calls the function)
  Output: *Output Pydantic model
  Errors: raise exceptions — ToolExecutorImpl wraps them into ToolResult(success=False)
  Never:  read SharedState, write findings, emit trace events
"""

from __future__ import annotations

import os
import re
import time
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from bs4 import BeautifulSoup, Tag

from app.tools.base import ExtractedLink, HttpHeaders, PageTimings, RedirectHop

from .schemas import (
    AssetReference,
    CachingHeadersGroup,
    ConsoleMessage,
    DetectedTechnology,
    HeaderAnalyzerInput,
    HeaderAnalyzerOutput,
    HeaderPresence,
    LinkExtractorInput,
    LinkExtractorOutput,
    NetworkRequest,
    PlaywrightCrawlerInput,
    PlaywrightCrawlerOutput,
    ScreenshotCaptureInput,
    ScreenshotCaptureOutput,
    SecurityHeadersGroup,
    ServerInfoGroup,
    TechStackDetectorInput,
    TechStackDetectorOutput,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers shared between PlaywrightCrawler and ScreenshotCapture
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _count_words(html: str) -> int:
    """Strips HTML tags and counts whitespace-delimited tokens."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text.split()) if text else 0


def _screenshot_dir() -> str:
    """Returns a writable screenshot directory regardless of OS."""
    from app.infrastructure.settings import settings
    path = settings.screenshot_storage_dir
    # On Windows the Linux default /tmp/... would land at C:\tmp\... which is fine,
    # but we fall back to the real temp dir if it looks like an absolute Unix path
    # that the user hasn't overridden.
    if path.startswith("/tmp") and os.name == "nt":
        path = os.path.join(tempfile.gettempdir(), "auditor", "screenshots")
    os.makedirs(path, exist_ok=True)
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# PlaywrightCrawler
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  PlaywrightCrawlerInput(url, wait_strategy, timeout_ms, viewport_*, ...)
# Output: PlaywrightCrawlerOutput — the foundational data packet for all downstream
#         tools. Treat this as the single crawl; nothing else fetches the page.
#
# Failure modes (all raise — ToolExecutorImpl wraps into ToolResult(success=False)):
#   playwright.async_api.Error (TimeoutError subclass)  → TIMEOUT
#   DNS / connection refused                             → URL_UNREACHABLE
#   HTTP 4xx / 5xx (http_status_code still populated)   → HTTP_ERROR
#   Browser process dies                                 → PLAYWRIGHT_CRASH
#   Page loads but < 50 rendered words                  → BLANK_PAGE
#
# Key Playwright steps and why each is necessary:
#   1. async_playwright() context manager  — owns the browser process lifecycle;
#      ensures the browser is closed even on exception.
#   2. browser.new_context(viewport=...)  — isolates cookies/storage per crawl.
#   3. page.route("**/*", handler)        — abort blocked resource types before
#      any requests are made (fonts/media waste bandwidth, never needed for audit).
#   4. page.on("response", ...)           — tracks redirect chain and all responses
#      synchronously (Playwright fires events on the event loop thread).
#   5. page.on("console", ...)            — captures JS errors/warnings.
#   6. page.goto(url, wait_until="commit") — "commit" means the HTTP response has
#      been received and navigation has started, but JS has NOT run yet. This is
#      the earliest safe point to call response.body().
#   7. response.body()                    — returns the raw (decompressed) HTTP
#      response body, i.e. the server-rendered HTML before any client-side JS runs.
#      This is what search engine crawlers see.
#   8. page.wait_for_load_state(...)      — wait for full JS rendering (networkidle
#      = no new network requests for 500 ms). After this, page.content() returns
#      the JS-rendered DOM.
#   9. page.evaluate(performance.timing)  — extracts browser-measured navigation
#      timing from the W3C Navigation Timing API (always available in Chromium).
# ═══════════════════════════════════════════════════════════════════════════════

async def run_playwright_crawler(inp: PlaywrightCrawlerInput) -> PlaywrightCrawlerOutput:
    """
    Crawls a URL with headless Chromium and returns both static (server-rendered)
    and dynamic (JS-rendered) HTML, plus all browser telemetry the audit system needs.

    Two HTML captures — why both matter:
      static_html  = raw HTTP response body → what search engines index
      rendered_html = page.content() after JS → what users see

    The ratio between their word counts is used to detect rendering strategy (SSR vs CSR).

    Example output for https://example.com:
      http_status_code = 200, redirect_chain = []
      static_word_count = 95, rendered_word_count = 95
      response_headers.get("content-type") = "text/html; charset=UTF-8"
      page_timings.ttfb_ms ≈ 80, page_timings.load_event_ms ≈ 350
    """
    from playwright.async_api import async_playwright, Error as PlaywrightError

    crawl_start = time.monotonic()

    # Synchronous collectors populated by event handler callbacks.
    # Playwright fires events on the asyncio thread — sync lambdas are safe.
    redirect_hops: List[Dict[str, Any]] = []
    raw_responses: List[Dict[str, Any]] = []
    raw_console: List[Dict[str, Any]] = []

    def _on_response(response: Any) -> None:
        status = response.status
        if status in (301, 302, 303, 307, 308):
            redirect_hops.append({
                "url": response.url,
                "status_code": status,
                "location": response.headers.get("location"),
            })
        raw_responses.append({
            "url": response.url,
            "resource_type": response.request.resource_type,
            "method": response.request.method,
            "status_code": status,
        })

    def _on_console(msg: Any) -> None:
        raw_console.append({
            "level": msg.type,
            "text": msg.text[:500],
            "source_url": (msg.location or {}).get("url"),
            "line_number": (msg.location or {}).get("lineNumber"),
        })

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, slow_mo=0)
        try:
            context = await browser.new_context(
                viewport={"width": inp.viewport_width, "height": inp.viewport_height},
                user_agent=inp.user_agent or _DEFAULT_UA,
            )
            page = await context.new_page()

            # Step 3 — Block unwanted resource types before navigation starts.
            # Blocking fonts + media shaves 200–800 ms off typical page loads
            # while keeping all content needed for the audit.
            if inp.block_resource_types:
                blocked = set(inp.block_resource_types)

                async def _block_route(route: Any, request: Any) -> None:
                    if request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _block_route)

            # Step 4 — Register event listeners BEFORE goto so we never miss events.
            page.on("response", _on_response)
            page.on("console", _on_console)

            # Step 6 — Navigate, wait only until the response is committed (pre-JS).
            try:
                nav_response = await page.goto(
                    inp.url,
                    wait_until="commit",   # earliest safe point to call response.body()
                    timeout=inp.timeout_ms,
                )
            except PlaywrightError as exc:
                err = str(exc)
                if "net::ERR_NAME_NOT_RESOLVED" in err or "net::ERR_CONNECTION_REFUSED" in err:
                    raise ConnectionError(f"URL unreachable: {inp.url} — {err}")
                if "Timeout" in err or "timeout" in err:
                    raise TimeoutError(f"Navigation timed out after {inp.timeout_ms}ms: {inp.url}")
                raise RuntimeError(f"Playwright navigation error: {err}") from exc

            if nav_response is None:
                raise RuntimeError(f"Playwright returned no response for {inp.url}")

            http_status = nav_response.status
            final_url = page.url

            # Step 7 — Capture static HTML (raw server response, before any JS runs).
            # response.body() is a coroutine that reads the full response body bytes.
            # Must be called while the response is still reachable (before browser.close).
            try:
                static_html = (await nav_response.body()).decode("utf-8", errors="replace")
            except Exception:
                # Fallback: navigation already advanced too far; get early DOM snapshot.
                static_html = await page.content()

            # Raw response headers (Playwright normalises keys to lowercase).
            response_headers_dict: Dict[str, str] = dict(nav_response.headers)

            # Step 8 — Wait for full JS rendering.
            elapsed_ms = int((time.monotonic() - crawl_start) * 1000)
            remaining_ms = max(5_000, inp.timeout_ms - elapsed_ms)

            if inp.wait_for_selector:
                try:
                    await page.wait_for_selector(inp.wait_for_selector, timeout=remaining_ms)
                except Exception:
                    pass  # Best-effort; page continues even if selector never appears.

            try:
                wait_state = {
                    "networkidle": "networkidle",
                    "load": "load",
                    "domcontentloaded": "domcontentloaded",
                }.get(inp.wait_strategy, "networkidle")
                await page.wait_for_load_state(wait_state, timeout=remaining_ms)
            except Exception:
                pass  # Some SPAs never reach networkidle — capture what we have.

            rendered_html = await page.content()

            # Step 9 — W3C Navigation Timing API, always present in Chromium.
            try:
                t = await page.evaluate("""
                    () => {
                        const pt = window.performance.timing;
                        const fcp = window.performance.getEntriesByName('first-contentful-paint')[0];
                        const nav = window.performance.getEntriesByType('navigation')[0];
                        return {
                            dns_ms:  Math.max(0, pt.domainLookupEnd - pt.domainLookupStart),
                            tcp_ms:  Math.max(0, pt.connectEnd - pt.connectStart),
                            ttfb_ms: Math.max(0, pt.responseStart - pt.requestStart),
                            dcl_ms:  Math.max(0, pt.domContentLoadedEventEnd - pt.navigationStart),
                            load_ms: Math.max(0, pt.loadEventEnd - pt.navigationStart),
                            fcp_ms:  fcp ? Math.round(fcp.startTime) : null
                        };
                    }
                """)
                page_timings = PageTimings(
                    dns_ms=t.get("dns_ms") or None,
                    tcp_ms=t.get("tcp_ms") or None,
                    ttfb_ms=t.get("ttfb_ms") or None,
                    dom_content_loaded_ms=t.get("dcl_ms") or None,
                    load_event_ms=t.get("load_ms") or None,
                    first_contentful_paint_ms=t.get("fcp_ms"),
                )
            except Exception:
                page_timings = PageTimings()

        finally:
            # Step 1 — Always close the browser, even on exception.
            await browser.close()

    # ── Assemble output ──────────────────────────────────────────────────────

    static_wc = _count_words(static_html)
    rendered_wc = _count_words(rendered_html)

    has_js_errors = any(m["level"] == "error" for m in raw_console)

    console_out = [
        ConsoleMessage(
            level=m["level"],
            text=m["text"],
            source_url=m.get("source_url"),
            line_number=m.get("line_number"),
        )
        for m in raw_console
    ]
    network_out = [
        NetworkRequest(
            url=r["url"],
            resource_type=r["resource_type"],
            method=r["method"],
            status_code=r.get("status_code"),
        )
        for r in raw_responses
    ]
    redirect_out = [
        RedirectHop(
            url=h["url"],
            status_code=h["status_code"],
            location=h.get("location"),
        )
        for h in redirect_hops
    ]

    # Blank page guard: the orchestrator treats this as a soft warning, not a hard failure.
    # We return the output normally — the orchestrator inspects rendered_word_count.
    output = PlaywrightCrawlerOutput(
        url=inp.url,
        final_url=final_url,
        redirect_chain=redirect_out,
        http_status_code=http_status,
        static_html=static_html,
        rendered_html=rendered_html,
        static_word_count=static_wc,
        rendered_word_count=rendered_wc,
        response_headers=HttpHeaders(raw=response_headers_dict),
        page_timings=page_timings,
        console_messages=console_out,
        network_requests=network_out,
        has_javascript_errors=has_js_errors,
        total_requests=len(network_out),
        total_transfer_kb=None,  # Phase 2: accumulate response.body() sizes per request
    )
    return output


# ═══════════════════════════════════════════════════════════════════════════════
# ScreenshotCapture
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  ScreenshotCaptureInput(url, full_page, viewport_*, output_format, quality)
# Output: ScreenshotCaptureOutput(file_path, file_size_bytes, page_*_px, format)
#
# Failure modes:
#   Playwright navigation error  → PlaywrightError re-raised
#   Disk full / permission error → OSError re-raised
#
# Why a separate browser context from PlaywrightCrawler:
#   ScreenshotCapture is dispatched concurrently by the Orchestrator (asyncio.create_task)
#   while TechStackDetector + HeaderAnalyzer run. Sharing a page object across async
#   tasks is unsafe; a separate context ensures isolation.
# ═══════════════════════════════════════════════════════════════════════════════

async def run_screenshot_capture(inp: ScreenshotCaptureInput) -> ScreenshotCaptureOutput:
    """
    Captures a full-page (or viewport) screenshot of the given URL and saves it to disk.

    The screenshot is taken after networkidle — ensuring JS-rendered content is visible.
    File is named with a UUID hex to avoid collisions in concurrent audits.

    Example output:
      file_path = "/tmp/auditor/screenshots/a1b2c3d4.png"
      file_size_bytes = 248_320, page_width_px = 1440, page_height_px = 3200
    """
    from playwright.async_api import async_playwright

    storage_dir = _screenshot_dir()
    filename = f"{uuid4().hex}.{inp.output_format}"
    output_path = os.path.join(storage_dir, filename)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, slow_mo=0)
        try:
            context = await browser.new_context(
                viewport={"width": inp.viewport_width, "height": inp.viewport_height},
                user_agent=_DEFAULT_UA,
            )
            page = await context.new_page()

            # Navigate and wait for full render before screenshotting.
            await page.goto(inp.url, wait_until="networkidle", timeout=30_000)

            screenshot_kwargs: Dict[str, Any] = {
                "path": output_path,
                "full_page": inp.full_page,
                "type": inp.output_format,
            }
            if inp.output_format == "jpeg":
                screenshot_kwargs["quality"] = inp.quality if inp.quality is not None else 80

            await page.screenshot(**screenshot_kwargs)

            # Page dimensions — scrollWidth/Height gives the full content size.
            dims = await page.evaluate(
                "() => ({w: document.body.scrollWidth, h: document.body.scrollHeight})"
            )
        finally:
            await browser.close()

    file_size = os.path.getsize(output_path)

    return ScreenshotCaptureOutput(
        file_path=output_path,
        file_size_bytes=file_size,
        page_width_px=dims.get("w", inp.viewport_width),
        page_height_px=dims.get("h", inp.viewport_height),
        viewport_height_px=inp.viewport_height,
        format=inp.output_format,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HeaderAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  HeaderAnalyzerInput(response_headers: HttpHeaders, url: str)
# Output: HeaderAnalyzerOutput with security/caching/server scores
#
# Failure modes:
#   None — all header fields are optional; absent headers score 0 (not an error).
#   Calling code may pass empty headers; returns all-absent analysis gracefully.
#
# Example output (condensed):
#   security.overall_score = 62
#   security.content_security_policy.present = False, score = 0
#   security.strict_transport_security.present = True, score = 10
#   caching.overall_score = 55
#   server_info.is_https = True
# ═══════════════════════════════════════════════════════════════════════════════

async def run_header_analyzer(inp: HeaderAnalyzerInput) -> HeaderAnalyzerOutput:
    """
    Analyses HTTP response headers and returns per-header scores (0–10) and overall
    group scores (0–100) for security, caching, and server-info categories.

    is_https is derived from the URL scheme — the agent may also pass it as an extra
    kwarg which Pydantic silently drops; we re-derive it here for correctness.
    """
    h = inp.response_headers
    is_https = inp.url.startswith("https://")
    return HeaderAnalyzerOutput(
        security=_analyze_security_headers(h, is_https),
        caching=_analyze_caching_headers(h),
        server_info=_analyze_server_info(h, is_https),
        all_headers=dict(h.raw),
    )


# ── Security headers ──────────────────────────────────────────────────────────

def _analyze_security_headers(h: HttpHeaders, is_https: bool) -> SecurityHeadersGroup:
    csp = _score_csp(h)
    hsts = _score_hsts(h, is_https)
    xfo = _score_x_frame_options(h)
    xcto = _score_x_content_type_options(h)
    rp = _score_referrer_policy(h)
    pp = _score_permissions_policy(h)

    # Weighted: CSP carries the most weight (XSS is the most critical web risk).
    weights = (0.30, 0.25, 0.15, 0.15, 0.10, 0.05)
    scores = (csp.score, hsts.score, xfo.score, xcto.score, rp.score, pp.score)
    # Each score is 0–10; weighted mean × 10 → 0–100.
    overall = int(sum(s * w for s, w in zip(scores, weights)) * 10)

    return SecurityHeadersGroup(
        content_security_policy=csp,
        strict_transport_security=hsts,
        x_frame_options=xfo,
        x_content_type_options=xcto,
        referrer_policy=rp,
        permissions_policy=pp,
        overall_score=min(100, overall),
    )


def _score_csp(h: HttpHeaders) -> HeaderPresence:
    value = h.get("content-security-policy")
    report_only = h.get("content-security-policy-report-only")

    if value is None and report_only is None:
        return HeaderPresence(
            header_name="content-security-policy",
            present=False,
            score=0,
            assessment="CSP is absent. No protection against XSS or data-injection attacks.",
            recommendation="Add Content-Security-Policy: default-src 'self'; script-src 'self'; ...",
        )

    if value is None:
        return HeaderPresence(
            header_name="content-security-policy",
            present=False,
            value=f"report-only: {report_only[:100]}",
            score=3,
            assessment="CSP is in report-only mode — violations are logged but NOT blocked.",
            recommendation="Promote to Content-Security-Policy once the policy is stable.",
        )

    v = value.lower()
    has_unsafe_inline = "'unsafe-inline'" in v
    has_unsafe_eval = "'unsafe-eval'" in v
    # Wildcard in any source list (but not inside quotes, e.g. 'nonce-*' is fine)
    has_wildcard = bool(re.search(r"(?<!\S)\*(?!\S)", v))

    if has_unsafe_inline and has_unsafe_eval:
        score, note, rec = (
            2,
            "CSP contains both 'unsafe-inline' and 'unsafe-eval' — XSS protection is nearly nullified.",
            "Remove both directives. Use nonces or hashes for inline scripts.",
        )
    elif has_unsafe_inline:
        score, note, rec = (
            4,
            "CSP contains 'unsafe-inline' in script-src, significantly weakening XSS protection.",
            "Remove 'unsafe-inline'. Use nonces (nonce-{value}) or hashes for inline scripts.",
        )
    elif has_wildcard:
        score, note, rec = (
            5,
            "CSP contains a wildcard (*) source — any domain can supply scripts.",
            "Replace wildcard with an explicit allowlist of trusted domains.",
        )
    elif has_unsafe_eval:
        score, note, rec = (
            6,
            "CSP contains 'unsafe-eval' — dynamic JS execution (eval, Function()) is permitted.",
            "Remove 'unsafe-eval'. Refactor eval() / Function() calls to static alternatives.",
        )
    else:
        score, note, rec = (9, "CSP is present with no obvious dangerous directives.", None)

    return HeaderPresence(
        header_name="content-security-policy",
        present=True,
        value=value[:200],
        score=score,
        assessment=note,
        recommendation=rec,
    )


def _score_hsts(h: HttpHeaders, is_https: bool) -> HeaderPresence:
    if not is_https:
        return HeaderPresence(
            header_name="strict-transport-security",
            present=False,
            score=5,
            assessment="Not applicable — HSTS only makes sense on HTTPS sites.",
            recommendation="Migrate to HTTPS first, then configure HSTS.",
        )

    value = h.get("strict-transport-security")
    if value is None:
        return HeaderPresence(
            header_name="strict-transport-security",
            present=False,
            score=0,
            assessment="HSTS is absent on an HTTPS site. Browsers may follow HTTP downgrade links.",
            recommendation="Add Strict-Transport-Security: max-age=31536000; includeSubDomains",
        )

    v = value.lower()
    max_age = 0
    if m := re.search(r"max-age=(\d+)", v):
        max_age = int(m.group(1))
    has_subdomains = "includesubdomains" in v

    if max_age < 86_400:
        score = 3
        note = f"HSTS max-age={max_age}s is very short. Minimum recommended: 31536000 (1 year)."
        rec = f"Increase to max-age=31536000; includeSubDomains"
    elif max_age < 15_552_000:
        score = 5
        note = f"HSTS max-age={max_age // 86400} days. Recommended minimum is 1 year."
        rec = "Set max-age=31536000 and add includeSubDomains."
    elif not has_subdomains:
        score = 8
        note = f"HSTS max-age={max_age // 86400} days. Good, but subdomains are unprotected."
        rec = f"Add includeSubDomains: Strict-Transport-Security: max-age={max_age}; includeSubDomains"
    else:
        score = 10
        note = f"HSTS is well-configured: max-age={max_age // 86400} days with includeSubDomains."
        rec = None

    return HeaderPresence(
        header_name="strict-transport-security",
        present=True,
        value=value,
        score=score,
        assessment=note,
        recommendation=rec,
    )


def _score_x_frame_options(h: HttpHeaders) -> HeaderPresence:
    value = h.get("x-frame-options")

    if value is None:
        # CSP frame-ancestors is the modern equivalent — give partial credit.
        csp = (h.get("content-security-policy") or "").lower()
        if "frame-ancestors" in csp:
            return HeaderPresence(
                header_name="x-frame-options",
                present=False,
                score=8,
                assessment="X-Frame-Options absent, but CSP frame-ancestors provides equivalent clickjacking protection.",
                recommendation=None,
            )
        return HeaderPresence(
            header_name="x-frame-options",
            present=False,
            score=0,
            assessment="X-Frame-Options is absent. The page can be embedded in iframes (clickjacking risk).",
            recommendation="Add X-Frame-Options: SAMEORIGIN or use CSP: frame-ancestors 'self'",
        )

    v = value.upper().strip()
    if v == "DENY":
        score, note, rec = 10, "DENY — most restrictive, blocks all iframe embedding.", None
    elif v == "SAMEORIGIN":
        score, note, rec = 8, "SAMEORIGIN — same-origin embedding allowed, cross-origin blocked.", None
    elif v.startswith("ALLOW-FROM"):
        score = 5
        note = "ALLOW-FROM is deprecated and ignored by Chrome/Firefox. Use CSP frame-ancestors instead."
        rec = "Replace with Content-Security-Policy: frame-ancestors 'self' <trusted-origin>"
    else:
        score = 3
        note = f"Unrecognised X-Frame-Options value: '{value}'. Browsers may ignore it."
        rec = "Use DENY or SAMEORIGIN."

    return HeaderPresence(
        header_name="x-frame-options",
        present=True,
        value=value,
        score=score,
        assessment=note,
        recommendation=rec,
    )


def _score_x_content_type_options(h: HttpHeaders) -> HeaderPresence:
    value = h.get("x-content-type-options")

    if value is None:
        return HeaderPresence(
            header_name="x-content-type-options",
            present=False,
            score=0,
            assessment="X-Content-Type-Options is absent. Browsers may MIME-sniff responses, enabling content-injection attacks.",
            recommendation="Add X-Content-Type-Options: nosniff",
        )

    if value.lower().strip() == "nosniff":
        return HeaderPresence(
            header_name="x-content-type-options",
            present=True,
            value=value,
            score=10,
            assessment="nosniff — MIME-type sniffing is disabled. Correct.",
            recommendation=None,
        )

    return HeaderPresence(
        header_name="x-content-type-options",
        present=True,
        value=value,
        score=3,
        assessment=f"Unrecognised value '{value}'. The only valid value is 'nosniff'.",
        recommendation="Set X-Content-Type-Options: nosniff",
    )


def _score_referrer_policy(h: HttpHeaders) -> HeaderPresence:
    _SCORES: Dict[str, int] = {
        "no-referrer": 10,
        "strict-origin-when-cross-origin": 9,
        "same-origin": 8,
        "strict-origin": 8,
        "origin-when-cross-origin": 7,
        "no-referrer-when-downgrade": 6,
        "origin": 5,
        "unsafe-url": 0,
    }
    value = h.get("referrer-policy")

    if value is None:
        return HeaderPresence(
            header_name="referrer-policy",
            present=False,
            score=4,
            assessment="Referrer-Policy not set. Browsers apply their default (usually 'strict-origin-when-cross-origin'). Explicit policy is recommended.",
            recommendation="Add Referrer-Policy: strict-origin-when-cross-origin",
        )

    v = value.lower().strip()
    score = _SCORES.get(v, 4)

    if v == "unsafe-url":
        note = "unsafe-url sends the full URL (including path and query) to all origins, including HTTP. Severe privacy risk."
        rec = "Change to strict-origin-when-cross-origin."
    elif score >= 8:
        note = f"'{value}' is a strong referrer policy."
        rec = None
    else:
        note = f"'{value}' may leak referrer information to cross-origin requests."
        rec = "Consider strict-origin-when-cross-origin for better privacy."

    return HeaderPresence(
        header_name="referrer-policy",
        present=True,
        value=value,
        score=score,
        assessment=note,
        recommendation=rec,
    )


def _score_permissions_policy(h: HttpHeaders) -> HeaderPresence:
    value = h.get("permissions-policy") or h.get("feature-policy")

    if value is None:
        return HeaderPresence(
            header_name="permissions-policy",
            present=False,
            score=4,
            assessment="Permissions-Policy not set. Sensitive browser APIs (camera, mic, geolocation) are unrestricted.",
            recommendation="Add Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()",
        )

    v = value.lower()
    restricts_camera = "camera" in v
    restricts_mic = "microphone" in v
    restricts_geo = "geolocation" in v

    if restricts_camera and restricts_mic and restricts_geo:
        score, note, rec = 10, "Restricts camera, microphone, and geolocation. Well-configured.", None
    elif restricts_camera or restricts_mic or restricts_geo:
        score = 7
        note = "Permissions-Policy is present but doesn't restrict all sensitive browser APIs."
        rec = "Add camera=(), microphone=(), geolocation=() to restrict all sensitive features."
    else:
        score = 5
        note = "Permissions-Policy is present but doesn't appear to restrict any sensitive APIs."
        rec = "Include camera=(), microphone=(), geolocation=() in the policy."

    return HeaderPresence(
        header_name="permissions-policy",
        present=True,
        value=value[:200],
        score=score,
        assessment=note,
        recommendation=rec,
    )


# ── Caching headers ───────────────────────────────────────────────────────────

def _analyze_caching_headers(h: HttpHeaders) -> CachingHeadersGroup:
    cc = _score_cache_control(h)
    etag = _score_etag(h)
    last_mod = _score_last_modified(h)
    expires = _score_expires(h, cc)
    vary = _score_vary(h)
    cdn = _detect_cdn_cache_status(h)

    # Cache-Control is the most authoritative directive (35%).
    weights = (0.35, 0.20, 0.15, 0.10, 0.20)
    scores = (cc.score, etag.score, last_mod.score, expires.score, vary.score)
    overall = int(sum(s * w for s, w in zip(scores, weights)) * 10)

    return CachingHeadersGroup(
        cache_control=cc,
        etag=etag,
        last_modified=last_mod,
        expires=expires,
        vary=vary,
        cdn_cache_status=cdn,
        overall_score=min(100, overall),
    )


def _score_cache_control(h: HttpHeaders) -> HeaderPresence:
    value = h.get("cache-control")

    if value is None:
        return HeaderPresence(
            header_name="cache-control",
            present=False,
            score=0,
            assessment="Cache-Control is absent. Browsers may cache indefinitely or not at all.",
            recommendation="Add Cache-Control: public, max-age=3600 (tune to resource type).",
        )

    v = value.lower()
    max_age_match = re.search(r"max-age=(\d+)", v)
    max_age = int(max_age_match.group(1)) if max_age_match else 0

    if "immutable" in v:
        score, note, rec = 10, "Immutable + long max-age — ideal for fingerprinted static assets.", None
    elif "no-store" in v:
        score = 5
        note = "no-store prevents all caching. Appropriate for sensitive data, but reduces performance for public assets."
        rec = "Confirm this resource can't be cached. For static assets, use public, max-age=<seconds>."
    elif "no-cache" in v:
        score = 7
        note = "no-cache requires revalidation on every request. Reasonable for frequently-updated content."
        rec = None
    elif "public" in v and max_age >= 86_400:
        score, note, rec = 9, f"Public caching with max-age={max_age}s ({max_age // 3600}h). Good.", None
    elif "public" in v and max_age > 0:
        score = 7
        note = f"Public caching with short max-age={max_age}s. May cause excessive revalidation."
        rec = "Consider increasing max-age for static resources."
    elif "private" in v:
        score, note, rec = 6, "Private caching — browser only, no CDN caching. Correct for personalised content.", None
    else:
        score = 5
        note = f"Cache-Control is present but may be misconfigured: '{value}'."
        rec = "Review Cache-Control directive against your caching strategy."

    return HeaderPresence(
        header_name="cache-control",
        present=True,
        value=value,
        score=score,
        assessment=note,
        recommendation=rec,
    )


def _score_etag(h: HttpHeaders) -> HeaderPresence:
    value = h.get("etag")
    if value is None:
        return HeaderPresence(
            header_name="etag",
            present=False,
            score=3,
            assessment="ETag is absent. Cache revalidation will rely on Last-Modified alone.",
            recommendation="Configure the server to emit ETag headers for cacheable responses.",
        )
    is_weak = value.startswith('W/"') or value.startswith("W/'")
    return HeaderPresence(
        header_name="etag",
        present=True,
        value=value[:60],
        score=8 if is_weak else 10,
        assessment=f"ETag present ({'weak' if is_weak else 'strong'}). Efficient cache revalidation enabled.",
        recommendation=None,
    )


def _score_last_modified(h: HttpHeaders) -> HeaderPresence:
    value = h.get("last-modified")
    if value is None:
        return HeaderPresence(
            header_name="last-modified",
            present=False,
            score=3,
            assessment="Last-Modified is absent. ETag alone handles revalidation.",
            recommendation="Configure the server to emit Last-Modified for cacheable responses.",
        )
    return HeaderPresence(
        header_name="last-modified",
        present=True,
        value=value,
        score=8,
        assessment=f"Last-Modified present: {value}. Revalidation supported.",
        recommendation=None,
    )


def _score_expires(h: HttpHeaders, cc: HeaderPresence) -> HeaderPresence:
    value = h.get("expires")
    if value is None:
        return HeaderPresence(
            header_name="expires",
            present=False,
            score=4 if cc.present else 1,
            assessment="Expires header absent. Cache-Control max-age takes precedence when present.",
            recommendation=None,
        )
    if value.strip() in ("0", "-1"):
        return HeaderPresence(
            header_name="expires",
            present=True,
            value=value,
            score=2,
            assessment="Expires: 0 disables HTTP expiry caching. Resource re-fetched on every request.",
            recommendation="Set a positive Expires value or use Cache-Control: max-age instead.",
        )
    return HeaderPresence(
        header_name="expires",
        present=True,
        value=value,
        score=7,
        assessment=f"Expires present: {value}. Note: Cache-Control max-age takes precedence.",
        recommendation=None,
    )


def _score_vary(h: HttpHeaders) -> HeaderPresence:
    value = h.get("vary")
    if value is None:
        return HeaderPresence(
            header_name="vary",
            present=False,
            score=3,
            assessment="Vary header absent. CDNs cannot split compressed vs. uncompressed cache entries.",
            recommendation="Add Vary: Accept-Encoding to enable compression-aware CDN caching.",
        )
    v = value.lower()
    if v.strip() == "*":
        return HeaderPresence(
            header_name="vary",
            present=True,
            value=value,
            score=2,
            assessment="Vary: * prevents all CDN caching — every request is a cache miss.",
            recommendation="Replace Vary: * with specific headers such as Accept-Encoding, Accept-Language.",
        )
    has_encoding = "accept-encoding" in v
    return HeaderPresence(
        header_name="vary",
        present=True,
        value=value,
        score=8 if has_encoding else 6,
        assessment=(
            f"Vary: {value}. Accept-Encoding present — compression-aware caching enabled."
            if has_encoding else
            f"Vary: {value}. Consider adding Accept-Encoding."
        ),
        recommendation=None if has_encoding else "Add Accept-Encoding to Vary header.",
    )


def _detect_cdn_cache_status(h: HttpHeaders) -> Optional[HeaderPresence]:
    """Returns CDN-specific cache status HeaderPresence, or None if no CDN detected."""
    # Cloudflare
    if v := h.get("cf-cache-status"):
        return HeaderPresence(
            header_name="cf-cache-status",
            present=True,
            value=v,
            score=8,
            assessment=f"Cloudflare CDN detected. Cache status: {v}.",
            recommendation=None,
        )
    # AWS CloudFront
    if (cf_id := h.get("x-amz-cf-id")) or (
        (xc := h.get("x-cache")) and "cloudfront" in xc.lower()
    ):
        display = h.get("x-cache") or f"CloudFront (ID: {(cf_id or '')[:20]})"
        return HeaderPresence(
            header_name="x-cache",
            present=True,
            value=display,
            score=8,
            assessment=f"AWS CloudFront CDN detected. Cache: {display}.",
            recommendation=None,
        )
    # Fastly
    if h.get("x-fastly-request-id") or h.get("x-served-by"):
        sc = h.get("surrogate-control") or h.get("x-cache-status") or "Fastly CDN"
        return HeaderPresence(
            header_name="x-fastly-request-id",
            present=True,
            value=sc,
            score=8,
            assessment=f"Fastly CDN detected. Cache: {sc}.",
            recommendation=None,
        )
    # Generic X-Cache (Varnish, nginx cache, etc.)
    if xc := h.get("x-cache"):
        return HeaderPresence(
            header_name="x-cache",
            present=True,
            value=xc,
            score=8,
            assessment=f"CDN/proxy cache detected (X-Cache: {xc}).",
            recommendation=None,
        )
    return None


# ── Server info ───────────────────────────────────────────────────────────────

def _analyze_server_info(h: HttpHeaders, is_https: bool) -> ServerInfoGroup:
    server_val = h.get("server") or ""
    powered_by = h.get("x-powered-by")
    has_version = bool(re.search(r"\d+\.\d+", server_val))

    server_hp = HeaderPresence(
        header_name="server",
        present=bool(server_val),
        value=server_val or None,
        score=0 if has_version else (5 if server_val else 10),
        assessment=(
            f"Server header reveals version information: '{server_val}'" if has_version
            else (f"Server: '{server_val}' — no version disclosed." if server_val
                  else "Server header absent — no technology disclosure.")
        ),
        recommendation=(
            "Suppress version info (Apache: ServerTokens Prod; nginx: server_tokens off)."
            if has_version else None
        ),
    )

    xpb_hp = HeaderPresence(
        header_name="x-powered-by",
        present=powered_by is not None,
        value=powered_by,
        score=0 if powered_by else 10,
        assessment=(
            f"X-Powered-By discloses technology stack: '{powered_by}'" if powered_by
            else "X-Powered-By absent — no framework disclosed."
        ),
        recommendation=(
            "Remove X-Powered-By (Express: app.disable('x-powered-by'); PHP: expose_php=Off)."
            if powered_by else None
        ),
    )

    # HTTP version: alt-svc can advertise h3 or h2.
    http_version: Optional[str] = None
    alt_svc = (h.get("alt-svc") or "").lower()
    if "h3" in alt_svc:
        http_version = "HTTP/3"
    elif "h2" in alt_svc:
        http_version = "HTTP/2"

    return ServerInfoGroup(
        server_header=server_hp,
        x_powered_by=xpb_hp,
        is_https=is_https,
        http_version=http_version,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TechStackDetector
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  TechStackDetectorInput(html, response_headers, cookie_names, script_urls)
# Output: TechStackDetectorOutput with per-category DetectedTechnology objects
#
# Failure modes:
#   None — returns empty lists if no technologies matched.
#   Low-accuracy risks: minified/obfuscated scripts, custom self-hosted tools.
#
# Example output:
#   meta_framework = DetectedTechnology(name="Next.js", confidence=0.90,
#       signals=["html:__NEXT_DATA__", "script:/_next/static/"], category="meta_framework")
#   analytics_tools = [DetectedTechnology(name="Google Analytics 4", confidence=0.85, ...)]
#   ssr_headers_present = False  (no x-nextjs-* header found)
#   hydration_markers_present = True  (__NEXT_DATA__ in HTML)
# ═══════════════════════════════════════════════════════════════════════════════

# Fingerprint database. Each entry defines patterns across four signal types.
# Patterns are case-insensitive regexes. Order within a category doesn't matter.
_FINGERPRINTS: List[Dict[str, Any]] = [
    # ── Meta Frameworks ───────────────────────────────────────────────────────
    {
        "name": "Next.js", "category": "meta_framework",
        "html": [r"__NEXT_DATA__", r"/_next/static/"],
        "headers": [r"^x-nextjs-", r"server:.*next\.js"],
        "scripts": [r"/_next/static/"],
        "cookies": [],
        "ssr_header_pattern": r"^x-nextjs-",
        "hydration_pattern": r"__NEXT_DATA__",
    },
    {
        "name": "Nuxt.js", "category": "meta_framework",
        "html": [r"__NUXT__", r"/_nuxt/", r"window\.__NUXT__"],
        "headers": [r"^x-nuxt-", r"server:.*nuxt"],
        "scripts": [r"/_nuxt/"],
        "cookies": [],
        "ssr_header_pattern": r"^x-nuxt-",
        "hydration_pattern": r"data-server-rendered|__NUXT__",
    },
    {
        "name": "Gatsby", "category": "meta_framework",
        "html": [r"___gatsby", r"/static/gatsby-"],
        "headers": [],
        "scripts": [r"gatsby-browser\.js", r"/static/\w+/gatsby\."],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": r"___gatsby",
    },
    {
        "name": "Remix", "category": "meta_framework",
        "html": [r"__remixContext", r"__remixManifest"],
        "headers": [],
        "scripts": [r"/@remix-run/", r"/build/root-"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": r"__remixContext",
    },
    {
        "name": "SvelteKit", "category": "meta_framework",
        "html": [r"__sveltekit_", r"/_app/immutable/"],
        "headers": [],
        "scripts": [r"/_app/immutable/"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": r"__sveltekit_",
    },
    # ── Frontend Frameworks ────────────────────────────────────────────────────
    {
        "name": "React", "category": "frontend_framework",
        "html": [r"data-reactroot", r"__REACT_DEVTOOLS_GLOBAL_HOOK__"],
        "headers": [],
        "scripts": [r"react\.production\.min\.js", r"react\.development\.js", r"/react@\d"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": r"data-reactroot",
    },
    {
        "name": "Vue.js", "category": "frontend_framework",
        "html": [r"data-v-[a-f0-9]{6,}", r"__vue_app__"],
        "headers": [],
        "scripts": [r"vue\.global\.prod\.js", r"vue\.esm-browser", r"/vue@\d", r"vue\.min\.js"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": r"data-v-[a-f0-9]+|__vue_app__",
    },
    {
        "name": "Angular", "category": "frontend_framework",
        "html": [r"ng-version=", r"_nghost-", r"_ngcontent-", r"ng-app="],
        "headers": [],
        "scripts": [r"zone\.js", r"angular\.min\.js"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": r"ng-version=",
    },
    {
        "name": "Svelte", "category": "frontend_framework",
        "html": [r'class="s-[a-zA-Z0-9]{8}"', r"svelte-[a-z0-9]{6,}"],
        "headers": [],
        "scripts": [r"/svelte@", r"svelte\.js"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── CMS Platforms ──────────────────────────────────────────────────────────
    {
        "name": "WordPress", "category": "cms",
        "html": [r"/wp-content/", r"/wp-includes/", r'content="WordPress\s[\d\.]+', r"wp-json"],
        "headers": [r"link:.*wp-json", r"x-powered-by:.*wordpress"],
        "scripts": [r"/wp-content/themes/", r"/wp-includes/js/"],
        "cookies": [r"^wordpress_", r"^wp-settings-", r"^comment_author_"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Drupal", "category": "cms",
        "html": [r"Drupal\.settings", r"Drupal\.behaviors", r"/sites/default/files/", r'content="Drupal \d'],
        "headers": [r"x-generator:.*drupal", r"x-drupal-cache", r"x-drupal-dynamic-cache"],
        "scripts": [r"/misc/drupal\.js", r"/sites/default/files/js/"],
        "cookies": [r"^SESS[a-f0-9]{32}"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Joomla", "category": "cms",
        "html": [r'content="Joomla', r"/media/jui/", r"joomla\.js"],
        "headers": [],
        "scripts": [r"/media/jui/js/"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Ghost", "category": "cms",
        "html": [r'content="Ghost \d', r'class="gh-'],
        "headers": [r"x-ghost-cache-status"],
        "scripts": [r"/ghost/api/", r"ghost\.min\.js"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── Ecommerce ──────────────────────────────────────────────────────────────
    {
        "name": "Shopify", "category": "ecommerce",
        "html": [r"window\.Shopify", r"cdn\.shopify\.com", r"shopify-section"],
        "headers": [r"x-shopid", r"x-shopify-stage", r"server:.*shopify"],
        "scripts": [r"cdn\.shopify\.com/s/"],
        "cookies": [r"^_shopify_", r"^shopify_"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "WooCommerce", "category": "ecommerce",
        "html": [r'class="woocommerce', r"woocommerce"],
        "headers": [],
        "scripts": [r"/woocommerce/", r"wc-cart"],
        "cookies": [r"^woocommerce_", r"^wc_session_"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Magento", "category": "ecommerce",
        "html": [r"Mage\.", r"window\.mage", r'content="Magento'],
        "headers": [r"x-magento-cache"],
        "scripts": [r"/mage/", r"magento\.js"],
        "cookies": [r"^mage-", r"^frontend$"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "BigCommerce", "category": "ecommerce",
        "html": [r"window\.BCData", r"bigcommerce"],
        "headers": [r"server:.*bigcommerce"],
        "scripts": [r"cdn\d+\.bigcommerce\.com"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── Website Builders ───────────────────────────────────────────────────────
    {
        "name": "Wix", "category": "cms",
        "html": [r"static\.wixstatic\.com", r"wixsite\.com"],
        "headers": [r"server:.*wix"],
        "scripts": [r"static\.wixstatic\.com"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Squarespace", "category": "cms",
        "html": [r"SQUARESPACE_CONTEXT", r"static\.squarespace\.com"],
        "headers": [r"x-squarespace"],
        "scripts": [r"static\.squarespace\.com"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Webflow", "category": "cms",
        "html": [r"assets\.website-files\.com", r"data-wf-"],
        "headers": [r"x-wf-", r"server:.*webflow"],
        "scripts": [r"assets\.website-files\.com"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── CDN / Hosting ──────────────────────────────────────────────────────────
    {
        "name": "Cloudflare", "category": "cdn",
        "html": [],
        "headers": [r"cf-cache-status", r"cf-ray", r"server:.*cloudflare"],
        "scripts": [],
        "cookies": [r"^__cflb", r"^cf_clearance"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "AWS CloudFront", "category": "cdn",
        "html": [],
        "headers": [r"x-amz-cf-id", r"via.*cloudfront"],
        "scripts": [],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Fastly", "category": "cdn",
        "html": [],
        "headers": [r"x-fastly-request-id", r"x-served-by:.*cache-"],
        "scripts": [],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Vercel", "category": "cdn",
        "html": [],
        "headers": [r"x-vercel-id", r"server:.*vercel"],
        "scripts": [],
        "cookies": [],
        "ssr_header_pattern": r"x-vercel-id",
        "hydration_pattern": None,
    },
    {
        "name": "Netlify", "category": "cdn",
        "html": [],
        "headers": [r"x-nf-request-id", r"server:.*netlify"],
        "scripts": [],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── Analytics ─────────────────────────────────────────────────────────────
    {
        "name": "Google Analytics 4", "category": "analytics",
        "html": [r"G-[A-Z0-9]{10}", r"googletagmanager\.com/gtag"],
        "headers": [],
        "scripts": [r"googletagmanager\.com/gtag/js"],
        "cookies": [r"^_ga$", r"^_ga_", r"^_gid$"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Google Universal Analytics", "category": "analytics",
        "html": [r"UA-\d{6,9}-\d{1,2}", r"google-analytics\.com/analytics\.js"],
        "headers": [],
        "scripts": [r"google-analytics\.com/analytics\.js"],
        "cookies": [r"^__utma$", r"^__utmb$"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Google Tag Manager", "category": "tag_manager",
        "html": [r"GTM-[A-Z0-9]{6,8}", r"googletagmanager\.com/gtm\.js"],
        "headers": [],
        "scripts": [r"googletagmanager\.com/gtm\.js"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Hotjar", "category": "analytics",
        "html": [r"hjSiteSettings", r"hotjar\.com"],
        "headers": [],
        "scripts": [r"static\.hotjar\.com/c/hotjar"],
        "cookies": [r"^_hjid$", r"^_hjSession"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Segment", "category": "analytics",
        "html": [r"analytics\.load\("],
        "headers": [],
        "scripts": [r"cdn\.segment\.com/analytics\.js"],
        "cookies": [r"^ajs_"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── A/B Testing ───────────────────────────────────────────────────────────
    {
        "name": "Optimizely", "category": "ab_testing",
        "html": [r"optimizely\.push"],
        "headers": [],
        "scripts": [r"cdn\.optimizely\.com/js/"],
        "cookies": [r"^optimizelyEndUserId"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "VWO", "category": "ab_testing",
        "html": [r"visualwebsiteoptimizer\.com"],
        "headers": [],
        "scripts": [r"dev\.visualwebsiteoptimizer\.com", r"vwo\.com/j/"],
        "cookies": [r"^_vwo_uuid"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── Chat / Support ────────────────────────────────────────────────────────
    {
        "name": "Intercom", "category": "chat_widget",
        "html": [r"intercomSettings", r"widget\.intercom\.io"],
        "headers": [],
        "scripts": [r"widget\.intercom\.io/widget/", r"js\.intercomcdn\.com"],
        "cookies": [r"^intercom-"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "Drift", "category": "chat_widget",
        "html": [r"window\.drift"],
        "headers": [],
        "scripts": [r"js\.driftt\.com"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    {
        "name": "HubSpot", "category": "chat_widget",
        "html": [r"js\.hs-scripts\.com", r"js\.hubspot\.com"],
        "headers": [],
        "scripts": [r"js\.hs-scripts\.com"],
        "cookies": [r"^__hstc$", r"^hubspotutk$"],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
    # ── Error Tracking ────────────────────────────────────────────────────────
    {
        "name": "Sentry", "category": "error_tracking",
        "html": [r"Sentry\.init", r"browser\.sentry-cdn\.com"],
        "headers": [],
        "scripts": [r"browser\.sentry-cdn\.com", r"sentry\.io/"],
        "cookies": [],
        "ssr_header_pattern": None,
        "hydration_pattern": None,
    },
]

_CATEGORY_FIELD_MAP: Dict[str, str] = {
    "meta_framework": "meta_framework",
    "frontend_framework": "frontend_framework",
    "cms": "cms",
    "ecommerce": "ecommerce_platform",
    "cdn": "cdn",
    "analytics": "analytics_tools",
    "tag_manager": "tag_manager",
    "ab_testing": "ab_testing",
    "chat_widget": "chat_widget",
    "error_tracking": "error_tracking",
}


def _match_fingerprint(
    fp: Dict[str, Any],
    html: str,
    headers: Dict[str, str],
    script_urls: List[str],
    cookie_names: List[str],
) -> Optional[Tuple[List[str], float]]:
    """
    Attempts to match a fingerprint against the available signals.

    Returns (signal_list, confidence) if matched, None otherwise.
    Header/cookie signals carry higher confidence than HTML pattern matches
    because they are harder to spoof and less likely to appear by coincidence.
    """
    signals: List[str] = []

    for pat in fp.get("html", []):
        if re.search(pat, html, re.IGNORECASE):
            signals.append(f"html:{pat}")

    # Match against "header_name: header_value" combined strings
    for pat in fp.get("headers", []):
        for name, val in headers.items():
            if re.search(pat, f"{name}: {val}", re.IGNORECASE):
                signals.append(f"header:{name}")
                break

    for pat in fp.get("scripts", []):
        for url in script_urls:
            if re.search(pat, url, re.IGNORECASE):
                signals.append(f"script_url:{url[:80]}")
                break

    for pat in fp.get("cookies", []):
        for cookie in cookie_names:
            if re.search(pat, cookie, re.IGNORECASE):
                signals.append(f"cookie:{cookie}")
                break

    if not signals:
        return None

    reliable = sum(1 for s in signals if s.startswith(("header:", "cookie:")))
    confidence = min(0.95, 0.65 + len(signals) * 0.08 + reliable * 0.10)
    return signals, confidence


async def run_tech_stack_detector(inp: TechStackDetectorInput) -> TechStackDetectorOutput:
    """
    Fingerprints the technology stack by matching known patterns against HTML,
    response headers, script URLs, and cookie names.

    No network calls — all data comes from the PlaywrightCrawler recon artifact.
    Returns empty lists for all categories if nothing is detected (not an error).

    Example output for a Next.js + Vercel + GA4 + Intercom stack:
      meta_framework = DetectedTechnology(name="Next.js", confidence=0.91, ...)
      cdn = DetectedTechnology(name="Vercel", confidence=0.80, ...)
      analytics_tools = [DetectedTechnology(name="Google Analytics 4", confidence=0.73, ...)]
      chat_widget = DetectedTechnology(name="Intercom", confidence=0.73, ...)
      ssr_headers_present = True   (x-vercel-id detected)
      hydration_markers_present = True   (__NEXT_DATA__ in HTML)
    """
    html = inp.html
    headers = inp.response_headers.raw  # Dict[str, str], already lower-cased keys
    scripts = inp.script_urls
    cookies = inp.cookie_names

    detected: List[DetectedTechnology] = []
    ssr_headers_present = False
    hydration_markers_present = False

    for fp in _FINGERPRINTS:
        result = _match_fingerprint(fp, html, headers, scripts, cookies)
        if result is None:
            continue
        signals, confidence = result

        tech = DetectedTechnology(
            name=fp["name"],
            confidence=confidence,
            signals=signals,
            category=fp["category"],
        )
        detected.append(tech)

        # SSR header signal (e.g. x-nextjs-*, x-vercel-id)
        if fp.get("ssr_header_pattern"):
            for name in headers:
                if re.search(fp["ssr_header_pattern"], name, re.IGNORECASE):
                    ssr_headers_present = True
                    break

        # Hydration marker in HTML (e.g. __NEXT_DATA__, data-reactroot)
        if fp.get("hydration_pattern"):
            if re.search(fp["hydration_pattern"], html, re.IGNORECASE):
                hydration_markers_present = True

    # Assign to output slots; for multi-value slots (analytics) collect all matches;
    # for single-value slots take the highest-confidence match.
    def _best(category: str) -> Optional[DetectedTechnology]:
        candidates = [t for t in detected if t.category == category]
        return max(candidates, key=lambda t: t.confidence) if candidates else None

    all_analytics = [t for t in detected if t.category == "analytics"]

    return TechStackDetectorOutput(
        detected_technologies=detected,
        frontend_framework=_best("frontend_framework"),
        meta_framework=_best("meta_framework"),
        cms=_best("cms"),
        ecommerce_platform=_best("ecommerce"),
        cdn=_best("cdn"),
        analytics_tools=all_analytics,
        tag_manager=_best("tag_manager"),
        ab_testing=_best("ab_testing"),
        chat_widget=_best("chat_widget"),
        error_tracking=_best("error_tracking"),
        ssr_headers_present=ssr_headers_present,
        hydration_markers_present=hydration_markers_present,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LinkExtractor
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  LinkExtractorInput(html, base_url, include_asset_links, deduplicate)
# Output: LinkExtractorOutput with internal_links, external_links, asset_references
#
# Failure modes:
#   PARSE_ERROR  → BeautifulSoup raises on severely malformed HTML.
#                  Callers (ToolExecutorImpl) wrap this into ToolResult(success=False).
#   SPA routing  → pushState navigation won't produce <a href> elements; these
#                  appear as href="#" counted in javascript_href_count.
#
# Example output for a typical blog page:
#   total_internal = 12, total_external = 4
#   internal_nofollow_count = 0
#   javascript_href_count = 2
#   asset_references = 8 (images + stylesheets)
# ═══════════════════════════════════════════════════════════════════════════════

_JS_HREF_PATTERNS: Set[str] = {"javascript:", "#", "javascript:void(0)", "javascript:;"}
_NAVIGATIONAL_TAGS: Set[str] = {"nav", "header", "footer"}


def _normalize_url(href: str, base_url: str) -> Optional[str]:
    """
    Resolves a raw href against base_url and strips fragments.
    Returns None for non-HTTP schemes (mailto:, tel:, data:, etc.) or blank hrefs.
    """
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(tuple(_JS_HREF_PATTERNS)):
        return None
    if href.startswith(("mailto:", "tel:", "data:", "javascript:")):
        return None

    try:
        resolved = urljoin(base_url, href)
        parsed = urlparse(resolved)
        if parsed.scheme not in ("http", "https"):
            return None
        # Strip fragment
        return urlparse(resolved)._replace(fragment="").geturl()
    except Exception:
        return None


def _get_base_domain(url: str) -> str:
    """Extracts netloc (host:port) from a URL for internal/external classification."""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_internal(normalized_url: str, base_domain: str) -> bool:
    """Returns True if normalized_url is on the same domain (exact match on netloc)."""
    try:
        domain = urlparse(normalized_url).netloc.lower()
        # Strip 'www.' prefix for comparison so www.example.com == example.com
        return (
            domain == base_domain
            or domain.lstrip("www.") == base_domain.lstrip("www.")
        )
    except Exception:
        return False


def _has_navigational_ancestor(tag: Tag) -> bool:
    """Returns True if the tag has a nav/header/footer ancestor."""
    for parent in tag.parents:
        if isinstance(parent, Tag) and parent.name in _NAVIGATIONAL_TAGS:
            return True
    return False


def _extract_rel_attributes(tag: Tag) -> List[str]:
    """Extracts space-separated rel attribute values into a list."""
    rel = tag.get("rel", [])
    if isinstance(rel, str):
        rel = rel.split()
    return [r.lower() for r in rel]


async def run_link_extractor(inp: LinkExtractorInput) -> LinkExtractorOutput:
    """
    Extracts, normalises, and classifies all hyperlinks and asset references from HTML.
    Uses BeautifulSoup with the stdlib html.parser — no extra dependencies.

    Deduplication (when inp.deduplicate=True) keys on normalized_url, keeping the first
    occurrence. Self-links (href="#") and JavaScript hrefs are counted but excluded
    from the internal/external lists.

    Example input:  html="<html>...<a href='/about'>About</a>...</html>",
                    base_url="https://example.com"
    Example output: internal_links=[ExtractedLink(href="/about",
                    normalized_url="https://example.com/about", is_internal=True, ...)]
    """
    soup = BeautifulSoup(inp.html, "html.parser")
    base_domain = _get_base_domain(inp.base_url)

    internal_links: List[ExtractedLink] = []
    external_links: List[ExtractedLink] = []
    asset_refs: List[AssetReference] = []
    seen_urls: Set[str] = set()

    js_href_count = 0
    internal_nofollow = 0
    external_nofollow = 0
    new_tab_internal = 0

    # ── Hyperlinks (<a href>) ─────────────────────────────────────────────────
    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag.get("href", "").strip()
        anchor_text = a_tag.get_text(separator=" ", strip=True) or None

        # Count JavaScript / empty hrefs
        href_lower = href.lower()
        if href_lower.startswith("javascript:") or href_lower in ("#", ""):
            js_href_count += 1
            continue

        normalized = _normalize_url(href, inp.base_url)
        if normalized is None:
            continue

        if inp.deduplicate and normalized in seen_urls:
            continue
        seen_urls.add(normalized)

        rel_attrs = _extract_rel_attributes(a_tag)
        is_nav = _has_navigational_ancestor(a_tag)
        opens_new_tab = a_tag.get("target", "").lower() == "_blank"
        internal = _is_internal(normalized, base_domain)

        link = ExtractedLink(
            href=href,
            normalized_url=normalized,
            anchor_text=anchor_text,
            is_internal=internal,
            is_navigational=is_nav,
            rel_attributes=rel_attrs,
            opens_new_tab=opens_new_tab,
        )

        if internal:
            internal_links.append(link)
            if "nofollow" in rel_attrs:
                internal_nofollow += 1
            if opens_new_tab:
                new_tab_internal += 1
        else:
            external_links.append(link)
            if "nofollow" in rel_attrs:
                external_nofollow += 1

    # ── Asset references ──────────────────────────────────────────────────────
    if inp.include_asset_links:
        asset_selectors: List[Tuple[str, str, str]] = [
            ("img", "src", "image"),
            ("script", "src", "script"),
            ("link", "href", "stylesheet"),  # CSS and fonts
            ("source", "src", "other"),
        ]
        seen_assets: Set[str] = set()

        for tag_name, attr, asset_type in asset_selectors:
            for tag in soup.find_all(tag_name, **{attr: True}):
                src: str = tag.get(attr, "").strip()
                if not src:
                    continue
                normalized_src = _normalize_url(src, inp.base_url)
                key = normalized_src or src
                if key in seen_assets:
                    continue
                seen_assets.add(key)

                # Refine asset type for <link> tags
                if tag_name == "link":
                    rel = " ".join(tag.get("rel", []))
                    if "stylesheet" in rel:
                        asset_type = "stylesheet"
                    elif "font" in rel or "preload" in rel:
                        asset_type = "font"
                    else:
                        asset_type = "other"

                is_external_asset = not (
                    normalized_src and _is_internal(normalized_src, base_domain)
                )
                asset_refs.append(
                    AssetReference(
                        src=src,
                        normalized_url=normalized_src,
                        asset_type=asset_type,
                        is_external=is_external_asset,
                        has_crossorigin="crossorigin" in tag.attrs,
                        has_integrity=bool(tag.get("integrity")),
                    )
                )

    return LinkExtractorOutput(
        internal_links=internal_links,
        external_links=external_links,
        asset_references=asset_refs,
        total_internal=len(internal_links),
        total_external=len(external_links),
        internal_nofollow_count=internal_nofollow,
        external_nofollow_count=external_nofollow,
        new_tab_internal_count=new_tab_internal,
        javascript_href_count=js_href_count,
    )
