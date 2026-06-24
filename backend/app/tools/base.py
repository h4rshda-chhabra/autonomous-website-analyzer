"""
Tool Layer Base Contracts
─────────────────────────
All tools in the system return ToolResult[T] where T is the tool-specific output model.
This ensures every tool call site handles both success and failure uniformly,
and every tool execution produces trace-compatible metadata automatically.

Architecture position:
  Agent → ToolExecutor.run(tool, input) → ToolResult[Output]
                ↑
          wraps timing, error handling, trace event emission
          the tool itself only knows about Input → Output

Tools are pure data processors:
  - They NEVER read from SharedState
  - They NEVER write findings
  - They NEVER emit trace events directly
  - The agent + ToolExecutor service layer does all of that
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


# ─── Error Taxonomy ───────────────────────────────────────────────────────────

class ToolErrorCode(str, Enum):
    # Network errors
    URL_UNREACHABLE       = "url_unreachable"       # DNS, connection refused
    HTTP_ERROR            = "http_error"            # 4xx/5xx response
    TIMEOUT               = "timeout"               # Tool exceeded time limit
    REDIRECT_LOOP         = "redirect_loop"         # Infinite redirect chain
    RATE_LIMITED          = "rate_limited"          # 429 from target server

    # Rendering errors
    PLAYWRIGHT_CRASH      = "playwright_crash"      # Browser process died
    RENDER_TIMEOUT        = "render_timeout"        # JS never finished rendering
    BLANK_PAGE            = "blank_page"            # Page loaded but has no content

    # Tool-specific errors
    LIGHTHOUSE_UNAVAILABLE = "lighthouse_unavailable"  # Lighthouse CLI not found
    LIGHTHOUSE_FAILED      = "lighthouse_failed"       # Lighthouse run errored
    AXE_INJECTION_FAILED   = "axe_injection_failed"   # axe-core could not be injected
    PARSE_ERROR            = "parse_error"             # Could not parse HTML/JSON
    ASSET_FETCH_FAILED     = "asset_fetch_failed"      # Could not retrieve an asset

    # AI tool errors
    CLAUDE_API_ERROR      = "claude_api_error"      # Claude API returned an error
    CLAUDE_RATE_LIMITED   = "claude_rate_limited"   # Claude API 429
    CLAUDE_INVALID_OUTPUT = "claude_invalid_output" # Response didn't match expected schema
    CONTEXT_TOO_LONG      = "context_too_long"      # Input exceeds model context limit

    # Generic
    UNKNOWN               = "unknown"


class ToolError(BaseModel):
    """Structured error returned inside ToolResult when success=False."""

    code: ToolErrorCode
    message: str = Field(..., description="Human-readable error description")
    detail: Optional[str] = Field(None, description="Stack trace or raw error from the tool")
    is_retryable: bool = Field(
        False,
        description=(
            "True if retrying with the same inputs might succeed. "
            "E.g. TIMEOUT and RATE_LIMITED are retryable; PARSE_ERROR is not."
        ),
    )
    partial_data_available: bool = Field(
        False,
        description=(
            "True if the tool collected partial data before failing. "
            "When True, ToolResult.data will be partially populated even though success=False."
        ),
    )


# ─── Universal Tool Result Wrapper ────────────────────────────────────────────

class ToolResult(BaseModel, Generic[T]):
    """
    Universal wrapper returned by every tool in the system.

    The ToolExecutor service wraps every tool call in this model,
    handling timing, error capture, and trace event emission.

    Agents always receive ToolResult[T] — they never call tools directly.
    This means every agent must handle both success=True and success=False paths.

    Graceful degradation contract:
      If success=False and partial_data_available=True:
        → Agent uses whatever data is available
        → Agent emits a low-confidence finding with the partial data
        → Agent continues — it does NOT halt the audit

      If success=False and partial_data_available=False:
        → Agent skips this tool's findings
        → Agent emits an ERROR trace event with is_recoverable=True
        → Agent marks findings for this area as "tool_unavailable"
    """

    tool_name: str = Field(..., description="Matches the tool class name for trace logging")
    success: bool
    data: Optional[T] = Field(
        None,
        description="The tool's output model. Null only when success=False and no partial data.",
    )
    error: Optional[ToolError] = Field(None, description="Populated when success=False")
    duration_ms: int = Field(..., ge=0, description="Wall-clock time the tool took to execute")
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    cached: bool = Field(
        False,
        description="True if this result was served from cache rather than executing the tool",
    )
    cache_age_seconds: Optional[int] = Field(
        None,
        description="How old the cached result is (populated when cached=True)",
    )

    def to_trace_summary(self) -> Dict[str, Any]:
        """
        Produces the summarized output for ToolCallPayload.output_summary in trace events.
        Deliberately omits large fields (HTML blobs, full violation lists).
        Each tool's output model should implement a summarize() method that this calls.
        """
        if self.data is None:
            return {"error": self.error.code.value if self.error else "unknown"}
        if hasattr(self.data, "summarize"):
            return self.data.summarize()
        return {"tool": self.tool_name, "success": self.success}


# ─── Shared Primitive Types ───────────────────────────────────────────────────

class HttpHeaders(BaseModel):
    """Typed representation of HTTP response headers (case-normalized to lowercase)."""
    raw: Dict[str, str] = Field(..., description="All headers as key-value pairs")

    def get(self, key: str) -> Optional[str]:
        return self.raw.get(key.lower())

    def has(self, key: str) -> bool:
        return key.lower() in self.raw


class RedirectHop(BaseModel):
    url: str
    status_code: int
    location: Optional[str] = None


class ExtractedLink(BaseModel):
    """A single hyperlink extracted from an HTML page."""
    href: str = Field(..., description="Raw href value from the anchor element")
    normalized_url: Optional[str] = Field(
        None,
        description="Fully resolved absolute URL (None if href could not be resolved)",
    )
    anchor_text: Optional[str] = Field(None, description="Visible text of the link")
    is_internal: bool
    is_navigational: bool = Field(
        False,
        description="True for nav/header/footer links — distinct from in-content links",
    )
    rel_attributes: List[str] = Field(
        default_factory=list,
        description="Values from the rel attribute: nofollow, noopener, sponsored, ugc, etc.",
    )
    opens_new_tab: bool = Field(False, description="target=_blank present")


class PageTimings(BaseModel):
    """Browser-measured page load timings in milliseconds."""
    dns_ms: Optional[int] = None
    tcp_ms: Optional[int] = None
    ttfb_ms: Optional[int] = None
    dom_content_loaded_ms: Optional[int] = None
    load_event_ms: Optional[int] = None
    first_contentful_paint_ms: Optional[int] = None
