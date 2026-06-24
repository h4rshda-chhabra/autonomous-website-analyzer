from __future__ import annotations

from typing import List

from app.models import (
    AgentType,
    FindingCategory,
    ImplementationEffort,
    Severity,
    TraceEventType,
)
from app.tools.technical.schemas import BrokenLinkCheckerInput, SecurityHeaderAnalyzerInput
from .base_agent import BaseAgent


class TechnicalAgent(BaseAgent):
    """
    Analyses HTTP-level health: security headers, HTTPS, broken links, and redirect chains.
    Reads header_analysis and link_extraction from SharedState (no independent network calls
    except for BrokenLinkChecker).
    """

    def agent_type(self) -> AgentType:
        return AgentType.TECHNICAL

    def allowed_tools(self) -> List[str]:
        return ["SecurityHeaderAnalyzer", "BrokenLinkChecker"]

    async def execute(self) -> None:
        site_profile = await self.get_site_profile()
        url = site_profile.final_url or site_profile.url
        is_https = url.startswith("https://")

        # Read header analysis from SharedState (cached from Recon — no re-fetch)
        header_analysis = await self.get_recon_artifact("header_analysis")
        response_headers = getattr(header_analysis, "response_headers", {}) if header_analysis else {}

        # ── Tool 1: SecurityHeaderAnalyzer (no network — pure header analysis) ─
        sec_result = await self.run_tool(
            "SecurityHeaderAnalyzer",
            SecurityHeaderAnalyzerInput(
                response_headers=response_headers,
                url=url,
                is_https=is_https,
            ),
            action_summary="Analysing HTTP security headers (no network — uses cached recon data)",
        )

        if sec_result.success:
            sec = sec_result.data
            grade = getattr(sec, "security_grade", "?")
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"Security grade: {grade}. "
                            f"Missing headers: {getattr(sec, 'missing_headers', [])}.",
            )

            if not is_https:
                await self.create_finding(
                    category=FindingCategory.HTTPS,
                    title="Site does not use HTTPS — all traffic is unencrypted",
                    description="The site is served over HTTP. All data transmitted between "
                                "the browser and server is unencrypted and can be intercepted.",
                    severity=Severity.CRITICAL,
                    business_impact="Modern browsers show 'Not Secure' warnings for HTTP sites, "
                                    "destroying trust. Google uses HTTPS as a ranking signal.",
                    impact_score=10,
                    effort=ImplementationEffort.MEDIUM,
                    effort_hours_min=2,
                    effort_hours_max=8,
                    fix_description="Install a TLS certificate (Let's Encrypt is free) and redirect all HTTP traffic to HTTPS.",
                    tool_name="SecurityHeaderAnalyzer",
                    evidence_raw_data={"is_https": False},
                    confidence=0.99,
                )

            missing_headers = getattr(sec, "missing_headers", [])

            if "strict-transport-security" in [h.lower() for h in missing_headers] and is_https:
                await self.create_finding(
                    category=FindingCategory.SECURITY,
                    title="HTTP Strict Transport Security (HSTS) header is missing",
                    description="HSTS is not set. Without HSTS, browsers will attempt HTTP connections "
                                "first, leaving users vulnerable to SSL-stripping attacks.",
                    severity=Severity.HIGH,
                    business_impact="MITM attacks can downgrade HTTPS connections to HTTP "
                                    "before the browser redirects. HSTS prevents this.",
                    impact_score=7,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Add: Strict-Transport-Security: max-age=31536000; includeSubDomains",
                    tool_name="SecurityHeaderAnalyzer",
                    evidence_raw_data={"missing": "strict-transport-security"},
                    confidence=0.99,
                    tags=["security", "quick-win"],
                )

            if "content-security-policy" in [h.lower() for h in missing_headers]:
                await self.create_finding(
                    category=FindingCategory.SECURITY,
                    title="Content Security Policy (CSP) header is missing",
                    description="No CSP header is set. CSP prevents XSS attacks by specifying "
                                "which content sources are allowed.",
                    severity=Severity.HIGH,
                    business_impact="Without CSP, successful XSS attacks can steal session tokens, "
                                    "inject malware, and deface the site.",
                    impact_score=7,
                    effort=ImplementationEffort.HARD,
                    effort_hours_min=8,
                    effort_hours_max=40,
                    fix_description="Implement a Content-Security-Policy header. "
                                    "Start in report-only mode to identify breakages before enforcing.",
                    tool_name="SecurityHeaderAnalyzer",
                    evidence_raw_data={"missing": "content-security-policy"},
                    confidence=0.99,
                    tags=["security"],
                )

            if "x-frame-options" in [h.lower() for h in missing_headers]:
                await self.create_finding(
                    category=FindingCategory.SECURITY,
                    title="X-Frame-Options header is missing (clickjacking risk)",
                    description="The page can be embedded in an iframe on any third-party site, "
                                "enabling clickjacking attacks.",
                    severity=Severity.MEDIUM,
                    business_impact="Attackers can overlay invisible iframes to trick users into "
                                    "clicking on malicious elements while thinking they're clicking on the real site.",
                    impact_score=5,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Add: X-Frame-Options: SAMEORIGIN\n"
                                    "Or use CSP frame-ancestors directive: "
                                    "Content-Security-Policy: frame-ancestors 'self'",
                    tool_name="SecurityHeaderAnalyzer",
                    evidence_raw_data={"missing": "x-frame-options"},
                    confidence=0.99,
                    tags=["security", "quick-win"],
                )

            # Missing viewport meta tag (mobile)
            viewport_missing = getattr(sec, "missing_viewport_meta", False)
            if viewport_missing:
                await self.create_finding(
                    category=FindingCategory.MOBILE,
                    title="Viewport meta tag is missing — mobile rendering will be broken",
                    description="Without <meta name='viewport'>, mobile browsers render the page "
                                "at desktop width and zoom out, making text tiny and unreadable.",
                    severity=Severity.HIGH,
                    business_impact="Google uses mobile-first indexing. Missing viewport meta tag "
                                    "will cause the mobile version to rank poorly or not at all.",
                    impact_score=8,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=0,
                    effort_hours_max=1,
                    fix_description="Add to <head>: <meta name='viewport' content='width=device-width, initial-scale=1'>",
                    tool_name="SecurityHeaderAnalyzer",
                    evidence_raw_data={"missing_viewport_meta": True},
                    confidence=0.99,
                    tags=["mobile", "quick-win"],
                )

        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"SecurityHeaderAnalyzer returned an error: "
                            f"{sec_result.error.message if sec_result.error else 'unknown'}.",
            )

        # ── Tool 2: BrokenLinkChecker ─────────────────────────────────────────
        link_extraction = await self.get_recon_artifact("link_extraction")
        all_links = getattr(link_extraction, "links", []) if link_extraction else []
        internal_links = [lk for lk in all_links if getattr(lk, "is_internal", False)]
        external_links = [lk for lk in all_links if not getattr(lk, "is_internal", False)]

        from app.infrastructure.settings import settings
        link_result = await self.run_tool(
            "BrokenLinkChecker",
            BrokenLinkCheckerInput(
                links=all_links,
                base_url=url,
                max_concurrent_requests=10,
                timeout_per_link_ms=8_000,
                max_links=settings.max_broken_link_checks,
            ),
            action_summary=f"Checking {len(all_links)} links for broken URLs",
            timeout_override_ms=120_000,
            allow_partial=True,
        )

        if link_result.success or (link_result.error and link_result.error.partial_data_available and link_result.data):
            lc = link_result.data
            broken_internal = getattr(lc, "broken_internal_links", [])
            broken_external = getattr(lc, "broken_external_links", [])
            is_partial = not link_result.success

            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"Link check {'(partial)' if is_partial else '(complete)'}: "
                            f"{len(broken_internal)} broken internal, {len(broken_external)} broken external.",
            )

            if broken_internal:
                await self.create_finding(
                    category=FindingCategory.BROKEN_LINKS,
                    title=f"{len(broken_internal)} broken internal link(s) found{' (partial scan)' if is_partial else ''}",
                    description=f"Found {len(broken_internal)} internal links returning 4xx/5xx HTTP status codes. "
                                "Broken internal links degrade user experience and waste crawl budget.",
                    severity=Severity.HIGH,
                    business_impact="Broken internal links prevent users from navigating the site, "
                                    "harm SEO crawl efficiency, and signal poor site maintenance to search engines.",
                    impact_score=7,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=1,
                    effort_hours_max=4,
                    fix_description="Fix or redirect the broken URLs. Common approach: "
                                    "redirect to the closest relevant page, or remove the broken links.",
                    tool_name="BrokenLinkChecker",
                    evidence_raw_data={
                        "broken_count": len(broken_internal),
                        "sample_urls": [getattr(lk, "url", str(lk)) for lk in broken_internal[:5]],
                        "partial_scan": is_partial,
                    },
                    confidence=0.70 if is_partial else 0.97,
                    affected_elements=[getattr(lk, "url", str(lk)) for lk in broken_internal[:10]],
                    affected_count=len(broken_internal),
                )

            if len(broken_external) > 3:
                await self.create_finding(
                    category=FindingCategory.BROKEN_LINKS,
                    title=f"{len(broken_external)} broken external link(s) found",
                    description=f"Found {len(broken_external)} external links returning errors. "
                                "While not as critical as internal links, they harm user experience.",
                    severity=Severity.MEDIUM,
                    business_impact="Broken external links make the site appear outdated and poorly maintained. "
                                    "They also waste user trust when links lead to dead pages.",
                    impact_score=3,
                    effort=ImplementationEffort.EASY,
                    effort_hours_min=1,
                    effort_hours_max=3,
                    fix_description="Update or remove external links that return errors. "
                                    "Use the Wayback Machine to find archived versions of important destinations.",
                    tool_name="BrokenLinkChecker",
                    evidence_raw_data={"broken_external_count": len(broken_external)},
                    confidence=0.90,
                    affected_count=len(broken_external),
                )

        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"BrokenLinkChecker failed: {link_result.error.message if link_result.error else 'unknown'}. Skipping link findings.",
            )

        # Cross-reference with SEO: canonical mismatch + redirect chain
        seo_findings = await self.get_prior_findings(AgentType.SEO)
        canonical_finding = next(
            (f for f in seo_findings if "canonical" in f.title.lower()),
            None,
        )
        if canonical_finding:
            redirect_findings = [
                f for f in seo_findings
                if "redirect" in f.title.lower()
            ]
            if redirect_findings:
                await self.emit_trace(
                    TraceEventType.REASONING,
                    reasoning="Canonical mismatch + redirect chain detected on same URL. "
                              "This is a compound SEO+Technical issue — flagging for Synthesis Agent.",
                )

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Technical analysis complete. {self._findings_written} finding(s) written.",
        )
        await self.complete()
