"""
Tool Layer — Public API
───────────────────────
Import paths for all tool schemas.
Tool implementations (not schemas) live in tools/{category}/runner.py — Phase 1 deliverable.
"""

from .base import (
    ExtractedLink,
    HttpHeaders,
    PageTimings,
    RedirectHop,
    ToolError,
    ToolErrorCode,
    ToolResult,
)
from .recon.schemas import (
    ConsoleMessage,
    LinkExtractorInput,
    LinkExtractorOutput,
    NetworkRequest,
    PlaywrightCrawlerInput,
    PlaywrightCrawlerOutput,
    ScreenshotCaptureInput,
    ScreenshotCaptureOutput,
    TechStackDetectorInput,
    TechStackDetectorOutput,
    HeaderAnalyzerInput,
    HeaderAnalyzerOutput,
)
from .seo.schemas import (
    MetaTagAnalyzerInput,
    MetaTagAnalyzerOutput,
    StructuredDataAnalyzerInput,
    StructuredDataAnalyzerOutput,
    InternalLinkAnalyzerInput,
    InternalLinkAnalyzerOutput,
)
from .performance.schemas import (
    LighthouseRunnerInput,
    LighthouseRunnerOutput,
    AssetAnalyzerInput,
    AssetAnalyzerOutput,
)
from .accessibility.schemas import (
    AxeCoreScannerInput,
    AxeCoreScannerOutput,
    ContrastCheckerInput,
    ContrastCheckerOutput,
)
from .content.schemas import (
    ContentExtractorInput,
    ContentExtractorOutput,
    ClaudeContentAnalyzerInput,
    ClaudeContentAnalyzerOutput,
)
from .technical.schemas import (
    SecurityHeaderAnalyzerInput,
    SecurityHeaderAnalyzerOutput,
    BrokenLinkCheckerInput,
    BrokenLinkCheckerOutput,
)
