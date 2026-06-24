from __future__ import annotations

from .schemas import (
    ContentExtractorInput, ContentExtractorOutput,
    ClaudeContentAnalyzerInput, ClaudeContentAnalyzerOutput,
)


async def run_content_extractor(inp: ContentExtractorInput) -> ContentExtractorOutput:
    raise NotImplementedError(
        "ContentExtractor is not implemented in Phase 0. "
        "Phase 1 implementation: use readability-lxml or trafilatura to extract "
        "main content from inp.html. Compute word count, reading grade level "
        "(Flesch-Kincaid), CTA count, above-fold CTA presence, and passive voice %."
    )


async def run_claude_content_analyzer(inp: ClaudeContentAnalyzerInput) -> ClaudeContentAnalyzerOutput:
    raise NotImplementedError(
        "ClaudeContentAnalyzer is not implemented in Phase 0. "
        "Phase 1 implementation: call Anthropic API with a structured prompt "
        "that scores quality (1-10), value proposition clarity (1-10), "
        "goal alignment (1-10), and suggests rewrites for weak CTAs/headlines."
    )
