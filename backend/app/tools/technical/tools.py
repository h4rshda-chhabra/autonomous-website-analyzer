from __future__ import annotations

from .schemas import (
    SecurityHeaderAnalyzerInput, SecurityHeaderAnalyzerOutput,
    BrokenLinkCheckerInput, BrokenLinkCheckerOutput,
)


async def run_security_header_analyzer(inp: SecurityHeaderAnalyzerInput) -> SecurityHeaderAnalyzerOutput:
    raise NotImplementedError(
        "SecurityHeaderAnalyzer is not implemented in Phase 0. "
        "Phase 1 implementation: parse inp.response_headers, check for presence and "
        "correctness of: Strict-Transport-Security, Content-Security-Policy, "
        "X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy. "
        "Compute a 0-100 security grade. No network call needed — pure header analysis."
    )


async def run_broken_link_checker(inp: BrokenLinkCheckerInput) -> BrokenLinkCheckerOutput:
    raise NotImplementedError(
        "BrokenLinkChecker is not implemented in Phase 0. "
        "Phase 1 implementation: use aiohttp with a semaphore-controlled connection pool "
        "to HEAD-check each link in inp.links. Return status codes, identify 4xx/5xx, "
        "detect redirect chains longer than 2 hops. Respect inp.max_concurrent_requests."
    )
