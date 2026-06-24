from __future__ import annotations

import asyncio
import hashlib
import json
import time
import traceback as tb
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type
from uuid import UUID

from app.models.enums import AgentType
from app.tools.base import ToolError, ToolErrorCode, ToolResult


# ─── Retry Policy ─────────────────────────────────────────────────────────────

@dataclass
class RetryPolicy:
    max_attempts: int = 2
    retryable_codes: List[ToolErrorCode] = field(default_factory=lambda: [
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.URL_UNREACHABLE,
        ToolErrorCode.CLAUDE_RATE_LIMITED,
        ToolErrorCode.RATE_LIMITED,
    ])
    base_delay_ms: int = 1_000
    backoff_multiplier: float = 2.0
    timeout_multiplier_on_final: float = 1.5


NO_RETRY = RetryPolicy(max_attempts=1)

STANDARD_RETRY = RetryPolicy(max_attempts=2)

AI_TOOL_RETRY = RetryPolicy(
    max_attempts=2,
    retryable_codes=[
        ToolErrorCode.CLAUDE_API_ERROR,
        ToolErrorCode.CLAUDE_RATE_LIMITED,
        ToolErrorCode.CLAUDE_INVALID_OUTPUT,
        ToolErrorCode.TIMEOUT,
    ],
    base_delay_ms=5_000,
)


# ─── Cache Strategy ───────────────────────────────────────────────────────────

class CacheStrategy(str, Enum):
    ALWAYS = "always"
    NEVER = "never"
    CONDITIONAL = "conditional"  # cache only on success


# ─── Tool Definition ──────────────────────────────────────────────────────────

@dataclass
class ToolDefinition:
    name: str
    func: Callable
    input_type: Type
    output_type: Type
    default_timeout_ms: int
    retry_policy: RetryPolicy
    allowed_agents: List[AgentType]
    cache_strategy: CacheStrategy = CacheStrategy.ALWAYS


# ─── Tool Registry ────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Singleton-style registry that holds all registered tool definitions.
    Initialized once at application startup before any agents run.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool '{definition.name}' is already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def is_allowed(self, tool_name: str, agent: AgentType) -> bool:
        defn = self._tools.get(tool_name)
        return defn is not None and agent in defn.allowed_agents

    def all_tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)


# ─── Registry Initializer ─────────────────────────────────────────────────────

def build_registry() -> ToolRegistry:
    """
    Registers all 16 production tools and returns the populated registry.
    Tool functions are imported lazily here so the registry can be built
    without executing any tool logic.
    """
    from app.tools.recon.tools import (
        run_playwright_crawler,
        run_screenshot_capture,
        run_tech_stack_detector,
        run_header_analyzer,
        run_link_extractor,
    )
    from app.tools.seo.tools import (
        run_meta_tag_analyzer,
        run_structured_data_analyzer,
        run_internal_link_analyzer,
    )
    from app.tools.performance.tools import (
        run_lighthouse_runner,
        run_asset_analyzer,
    )
    from app.tools.accessibility.tools import (
        run_axe_core_scanner,
        run_contrast_checker,
    )
    from app.tools.content.tools import (
        run_content_extractor,
        run_claude_content_analyzer,
    )
    from app.tools.technical.tools import (
        run_security_header_analyzer,
        run_broken_link_checker,
    )
    from app.tools.recon.schemas import (
        PlaywrightCrawlerInput, PlaywrightCrawlerOutput,
        ScreenshotCaptureInput, ScreenshotCaptureOutput,
        TechStackDetectorInput, TechStackDetectorOutput,
        HeaderAnalyzerInput, HeaderAnalyzerOutput,
        LinkExtractorInput, LinkExtractorOutput,
    )
    from app.tools.seo.schemas import (
        MetaTagAnalyzerInput, MetaTagAnalyzerOutput,
        StructuredDataAnalyzerInput, StructuredDataAnalyzerOutput,
        InternalLinkAnalyzerInput, InternalLinkAnalyzerOutput,
    )
    from app.tools.performance.schemas import (
        LighthouseRunnerInput, LighthouseRunnerOutput,
        AssetAnalyzerInput, AssetAnalyzerOutput,
    )
    from app.tools.accessibility.schemas import (
        AxeCoreScannerInput, AxeCoreScannerOutput,
        ContrastCheckerInput, ContrastCheckerOutput,
    )
    from app.tools.content.schemas import (
        ContentExtractorInput, ContentExtractorOutput,
        ClaudeContentAnalyzerInput, ClaudeContentAnalyzerOutput,
    )
    from app.tools.technical.schemas import (
        SecurityHeaderAnalyzerInput, SecurityHeaderAnalyzerOutput,
        BrokenLinkCheckerInput, BrokenLinkCheckerOutput,
    )

    ORCH = AgentType.ORCHESTRATOR
    SEO = AgentType.SEO
    PERF = AgentType.PERFORMANCE
    A11Y = AgentType.ACCESSIBILITY
    CONT = AgentType.CONTENT
    TECH = AgentType.TECHNICAL

    registry = ToolRegistry()

    registry.register(ToolDefinition(
        name="PlaywrightCrawler",
        func=run_playwright_crawler,
        input_type=PlaywrightCrawlerInput,
        output_type=PlaywrightCrawlerOutput,
        default_timeout_ms=30_000,
        retry_policy=STANDARD_RETRY,
        allowed_agents=[ORCH],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="ScreenshotCapture",
        func=run_screenshot_capture,
        input_type=ScreenshotCaptureInput,
        output_type=ScreenshotCaptureOutput,
        default_timeout_ms=15_000,
        retry_policy=NO_RETRY,
        allowed_agents=[ORCH],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="TechStackDetector",
        func=run_tech_stack_detector,
        input_type=TechStackDetectorInput,
        output_type=TechStackDetectorOutput,
        default_timeout_ms=2_000,
        retry_policy=NO_RETRY,
        allowed_agents=[ORCH],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="HeaderAnalyzer",
        func=run_header_analyzer,
        input_type=HeaderAnalyzerInput,
        output_type=HeaderAnalyzerOutput,
        default_timeout_ms=1_000,
        retry_policy=NO_RETRY,
        allowed_agents=[ORCH],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="LinkExtractor",
        func=run_link_extractor,
        input_type=LinkExtractorInput,
        output_type=LinkExtractorOutput,
        default_timeout_ms=5_000,
        retry_policy=NO_RETRY,
        allowed_agents=[ORCH],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="MetaTagAnalyzer",
        func=run_meta_tag_analyzer,
        input_type=MetaTagAnalyzerInput,
        output_type=MetaTagAnalyzerOutput,
        default_timeout_ms=2_000,
        retry_policy=NO_RETRY,
        allowed_agents=[SEO],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="StructuredDataAnalyzer",
        func=run_structured_data_analyzer,
        input_type=StructuredDataAnalyzerInput,
        output_type=StructuredDataAnalyzerOutput,
        default_timeout_ms=3_000,
        retry_policy=NO_RETRY,
        allowed_agents=[SEO],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="InternalLinkAnalyzer",
        func=run_internal_link_analyzer,
        input_type=InternalLinkAnalyzerInput,
        output_type=InternalLinkAnalyzerOutput,
        default_timeout_ms=2_000,
        retry_policy=NO_RETRY,
        allowed_agents=[SEO],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="LighthouseRunner",
        func=run_lighthouse_runner,
        input_type=LighthouseRunnerInput,
        output_type=LighthouseRunnerOutput,
        default_timeout_ms=120_000,
        retry_policy=NO_RETRY,
        allowed_agents=[PERF],
        cache_strategy=CacheStrategy.NEVER,
    ))
    registry.register(ToolDefinition(
        name="AssetAnalyzer",
        func=run_asset_analyzer,
        input_type=AssetAnalyzerInput,
        output_type=AssetAnalyzerOutput,
        default_timeout_ms=30_000,
        retry_policy=NO_RETRY,
        allowed_agents=[PERF],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="AxeCoreScanner",
        func=run_axe_core_scanner,
        input_type=AxeCoreScannerInput,
        output_type=AxeCoreScannerOutput,
        default_timeout_ms=45_000,
        retry_policy=STANDARD_RETRY,
        allowed_agents=[A11Y],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="ContrastChecker",
        func=run_contrast_checker,
        input_type=ContrastCheckerInput,
        output_type=ContrastCheckerOutput,
        default_timeout_ms=30_000,
        retry_policy=NO_RETRY,
        allowed_agents=[A11Y],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="ContentExtractor",
        func=run_content_extractor,
        input_type=ContentExtractorInput,
        output_type=ContentExtractorOutput,
        default_timeout_ms=5_000,
        retry_policy=NO_RETRY,
        allowed_agents=[CONT],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="ClaudeContentAnalyzer",
        func=run_claude_content_analyzer,
        input_type=ClaudeContentAnalyzerInput,
        output_type=ClaudeContentAnalyzerOutput,
        default_timeout_ms=60_000,
        retry_policy=AI_TOOL_RETRY,
        allowed_agents=[CONT],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="SecurityHeaderAnalyzer",
        func=run_security_header_analyzer,
        input_type=SecurityHeaderAnalyzerInput,
        output_type=SecurityHeaderAnalyzerOutput,
        default_timeout_ms=1_000,
        retry_policy=NO_RETRY,
        allowed_agents=[TECH],
        cache_strategy=CacheStrategy.ALWAYS,
    ))
    registry.register(ToolDefinition(
        name="BrokenLinkChecker",
        func=run_broken_link_checker,
        input_type=BrokenLinkCheckerInput,
        output_type=BrokenLinkCheckerOutput,
        default_timeout_ms=120_000,
        retry_policy=NO_RETRY,
        allowed_agents=[TECH],
        cache_strategy=CacheStrategy.NEVER,
    ))

    return registry
