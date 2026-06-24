"""
ToolExecutor — Design Specification
═════════════════════════════════════
The ToolExecutor is the single gateway for all tool invocations in the system.
No agent or service calls tool functions directly. This provides:
  - Unified timeout enforcement
  - Retry logic with exponential backoff
  - Partial result capture before failure
  - Tool authorization (agent can only call allowed tools)
  - Metrics collection (tool duration, success rate per tool per audit)
  - Result caching (same inputs within an audit = cached result)

Position in the runtime stack:
  BaseAgent.run_tool(name, input)
    └── ToolExecutor.run(name, input, agent_type)
          ├── validate tool is registered + allowed for this agent
          ├── check cache
          ├── execute with timeout
          ├── retry if retryable error
          └── return ToolResult[T]

Tool Registration Model:
  Tools are registered as a ToolDefinition (name, function, default_timeout, retry_policy).
  Registration happens at application startup via ToolRegistry.register().
  Tools are NOT instantiated — they are async functions with typed inputs/outputs.

  ToolRegistry.register(
      name="PlaywrightCrawler",
      func=playwright_crawler_run,        # async (PlaywrightCrawlerInput) -> PlaywrightCrawlerOutput
      input_type=PlaywrightCrawlerInput,
      output_type=PlaywrightCrawlerOutput,
      default_timeout_ms=30_000,
      retry_policy=RetryPolicy(max_attempts=2, retryable_codes=[TIMEOUT, URL_UNREACHABLE]),
      allowed_agents=[ORCHESTRATOR, SEO, ACCESSIBILITY],
  )

Timeout Hierarchy (lowest wins):
  1. Per-tool default (in ToolDefinition)
  2. Per-agent-config override (in AuditPlan.agent_configs[agent].tool_timeouts)
  3. Per-call override (timeout_override_ms in run_tool())

Retry Policy:
  - Only errors with is_retryable=True in ToolError are retried
  - Max 2 retries with exponential backoff: 1s → 2s
  - Third attempt uses longer timeout (1.5× default)
  - If all attempts fail: ToolResult(success=False, error=last_error)
  - If partial_data_available=True on any attempt: return that attempt's result

Cache Strategy:
  - Cache key: sha256(tool_name + json(input_data))
  - Cache scope: per audit_id (different audits never share cache)
  - TTL: duration of audit (evicted on audit complete/fail)
  - Cached result: full ToolResult with cached=True, cache_age_seconds set
  - PlaywrightCrawler output is always cached — only crawled once per audit
  - LighthouseRunner is NOT cached (each run intentionally different for median calc)
  - BrokenLinkChecker is NOT cached (links may change state)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type
from uuid import UUID

from app.models.enums import AgentType
from app.tools.base import ToolError, ToolErrorCode, ToolResult


# ─── Retry Policy ─────────────────────────────────────────────────────────────

class RetryPolicy:
    """
    Defines retry behavior for a registered tool.

    Fields:
      max_attempts      : Total attempts including first try (1 = no retries)
      retryable_codes   : Only retry if ToolError.code is in this set
      base_delay_ms     : Initial wait before first retry
      backoff_multiplier: Each retry waits base_delay × (multiplier ^ attempt)
      timeout_multiplier: Multiply timeout by this on final attempt
    """
    max_attempts: int = 2
    retryable_codes: List[ToolErrorCode] = [
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.URL_UNREACHABLE,
        ToolErrorCode.CLAUDE_RATE_LIMITED,
        ToolErrorCode.RATE_LIMITED,
    ]
    base_delay_ms: int = 1_000
    backoff_multiplier: float = 2.0
    timeout_multiplier_on_final: float = 1.5


NO_RETRY = RetryPolicy()
NO_RETRY.max_attempts = 1

STANDARD_RETRY = RetryPolicy()

AI_TOOL_RETRY = RetryPolicy()
AI_TOOL_RETRY.max_attempts = 2
AI_TOOL_RETRY.retryable_codes = [
    ToolErrorCode.CLAUDE_API_ERROR,
    ToolErrorCode.CLAUDE_RATE_LIMITED,
    ToolErrorCode.CLAUDE_INVALID_OUTPUT,
    ToolErrorCode.TIMEOUT,
]
AI_TOOL_RETRY.base_delay_ms = 5_000


# ─── Tool Definition ──────────────────────────────────────────────────────────

class CacheStrategy(str, Enum):
    ALWAYS      = "always"    # Cache result for the audit duration
    NEVER       = "never"     # Always execute fresh (Lighthouse, BrokenLinkChecker)
    CONDITIONAL = "conditional"  # Cache only on success


class ToolDefinition:
    """
    Metadata for a registered tool. Created by ToolRegistry.register().

    func            : The async callable that implements the tool
    input_type      : Pydantic input model class (for validation before execution)
    output_type     : Pydantic output model class (for validation after execution)
    default_timeout_ms : Wall clock limit for a single attempt
    retry_policy    : How many times and when to retry
    allowed_agents  : Which AgentTypes may call this tool (enforced by ToolExecutor)
    cache_strategy  : Whether to cache this tool's output within an audit
    """
    name: str
    func: Callable
    input_type: Type
    output_type: Type
    default_timeout_ms: int
    retry_policy: RetryPolicy
    allowed_agents: List[AgentType]
    cache_strategy: CacheStrategy


# ─── ToolRegistry ─────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Maintains the canonical list of all registered tools.
    Singleton — initialized once at application startup.

    Registration order does not matter.
    Tools are looked up by exact string name.

    Complete MVP registration table:
    ┌────────────────────────────┬──────────────┬────────────┬────────────────────────────────────┐
    │ Tool Name                  │ Timeout (ms) │ Cache      │ Allowed Agents                     │
    ├────────────────────────────┼──────────────┼────────────┼────────────────────────────────────┤
    │ PlaywrightCrawler          │ 30_000       │ ALWAYS     │ ORCHESTRATOR                       │
    │ ScreenshotCapture          │ 15_000       │ ALWAYS     │ ORCHESTRATOR                       │
    │ TechStackDetector          │ 2_000        │ ALWAYS     │ ORCHESTRATOR                       │
    │ HeaderAnalyzer             │ 1_000        │ ALWAYS     │ ORCHESTRATOR                       │
    │ LinkExtractor              │ 5_000        │ ALWAYS     │ ORCHESTRATOR                       │
    │ MetaTagAnalyzer            │ 2_000        │ ALWAYS     │ SEO                                │
    │ StructuredDataAnalyzer     │ 3_000        │ ALWAYS     │ SEO                                │
    │ InternalLinkAnalyzer       │ 2_000        │ ALWAYS     │ SEO                                │
    │ LighthouseRunner           │ 120_000      │ NEVER      │ PERFORMANCE                        │
    │ AssetAnalyzer              │ 30_000       │ ALWAYS     │ PERFORMANCE                        │
    │ AxeCoreScanner             │ 45_000       │ ALWAYS     │ ACCESSIBILITY                      │
    │ ContrastChecker            │ 30_000       │ ALWAYS     │ ACCESSIBILITY                      │
    │ ContentExtractor           │ 5_000        │ ALWAYS     │ CONTENT                            │
    │ ClaudeContentAnalyzer      │ 60_000       │ ALWAYS     │ CONTENT                            │
    │ SecurityHeaderAnalyzer     │ 1_000        │ ALWAYS     │ TECHNICAL                          │
    │ BrokenLinkChecker          │ 120_000      │ NEVER      │ TECHNICAL                          │
    └────────────────────────────┴──────────────┴────────────┴────────────────────────────────────┘
    """

    @abstractmethod
    def register(self, definition: ToolDefinition) -> None:
        """Register a tool definition. Must be called before any agent runs."""
        ...

    @abstractmethod
    def get(self, name: str) -> Optional[ToolDefinition]:
        """Returns the ToolDefinition for the given name, or None if not registered."""
        ...

    @abstractmethod
    def is_allowed(self, tool_name: str, agent: AgentType) -> bool:
        """Returns True if the given agent is allowed to call this tool."""
        ...

    @abstractmethod
    def all_tool_names(self) -> List[str]:
        """Returns all registered tool names (for diagnostics)."""
        ...


# ─── ToolExecutor ─────────────────────────────────────────────────────────────

class ToolExecutor:
    """
    Executes registered tools with full lifecycle management.

    This is injected into every BaseAgent as IToolExecutor.
    The same instance is shared across all agents in an audit.

    Execution flow for a single run() call:
    ┌─────────────────────────────────────────────────────────────────┐
    │ run(tool_name, input_data, agent_type)                          │
    │   1. Lookup tool in registry → ToolDefinition                  │
    │   2. Check agent is in allowed_agents → ToolNotAllowedError    │
    │   3. Validate input_data against definition.input_type         │
    │   4. Check cache → return cached ToolResult if hit             │
    │   5. attempt_loop:                                              │
    │        a. Start timer                                           │
    │        b. asyncio.wait_for(func(input), timeout)               │
    │        c. Validate output against definition.output_type       │
    │        d. If success → wrap in ToolResult, cache, return       │
    │        e. If error → check is_retryable                        │
    │             i.  If retryable and attempts_left → wait, retry   │
    │             ii. If partial_data in error → capture it          │
    │             iii.If no retries left → return ToolResult(fail)   │
    └─────────────────────────────────────────────────────────────────┘

    Partial result capture:
      Some tools (BrokenLinkChecker, LighthouseRunner) may timeout after
      collecting partial data. The tool function signals this by raising
      PartialResultError(partial_output, error_code). ToolExecutor catches this
      and returns ToolResult(success=False, data=partial_output, error=...).
      The agent must check both result.success AND result.data.

    Concurrency:
      run() is async and safe for concurrent calls.
      Multiple agents calling different tools in parallel is the normal case.
      Cache reads/writes are protected by asyncio locks per cache key.
    """

    @abstractmethod
    async def run(
        self,
        tool_name: str,
        input_data: Any,
        *,
        calling_agent: AgentType,
        timeout_override_ms: Optional[int] = None,
        allow_partial: bool = False,
    ) -> ToolResult:
        """
        Execute a tool and return ToolResult[T].
        Never raises. All outcomes, including auth failures, are in ToolResult.
        """
        ...


# ─── Execution State Tracking ─────────────────────────────────────────────────

class ToolExecutionRecord:
    """
    Immutable record of a single tool execution within an audit.
    Persisted to PostgreSQL for post-audit analysis and debugging.
    Not used for runtime decisions.
    """
    audit_id: UUID
    tool_name: str
    agent: AgentType
    attempt_number: int             # 1-indexed
    input_hash: str                 # sha256 of serialized input
    success: bool
    error_code: Optional[str]
    duration_ms: int
    was_cached: bool
    partial_result: bool
    executed_at: str                # ISO-8601
