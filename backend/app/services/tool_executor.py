from __future__ import annotations

import asyncio
import hashlib
import json
import time
import traceback
from typing import Any, Dict, Optional

from app.tools.base import ToolError, ToolErrorCode, ToolResult
from app.tools.registry import CacheStrategy, ToolRegistry
from app.runtime.base_agent import IToolExecutor


class ToolExecutorImpl(IToolExecutor):
    """
    Executes registered tools with timeout enforcement, retry-with-backoff, and per-input caching.

    Phase 0: fully functional runtime. Tool functions themselves raise NotImplementedError,
    so run() returns a structured ToolResult(success=False) with a clear error message.
    Phase 1: tool functions are implemented; run() produces real outputs.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        # Cache key: sha256(tool_name + json(input)) → ToolResult
        self._cache: Dict[str, ToolResult] = {}
        self._cache_locks: Dict[str, asyncio.Lock] = {}

    async def run(
        self,
        tool_name: str,
        input_data: Any,
        *,
        timeout_override_ms: Optional[int] = None,
        allow_partial: bool = False,
    ) -> ToolResult:
        definition = self._registry.get(tool_name)
        if definition is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(
                    code=ToolErrorCode.UNKNOWN,
                    message=f"Tool '{tool_name}' is not registered in the ToolRegistry",
                    is_retryable=False,
                ),
                duration_ms=0,
            )

        timeout_ms = timeout_override_ms or definition.default_timeout_ms

        # ── Cache check ────────────────────────────────────────────────────────
        if definition.cache_strategy == CacheStrategy.ALWAYS:
            cache_key = self._make_cache_key(tool_name, input_data)
            cached = self._cache.get(cache_key)
            if cached is not None:
                return ToolResult(
                    tool_name=cached.tool_name,
                    success=cached.success,
                    data=cached.data,
                    error=cached.error,
                    duration_ms=cached.duration_ms,
                    executed_at=cached.executed_at,
                    cached=True,
                    cache_age_seconds=int(
                        (time.time() - cached.executed_at.timestamp())
                    ),
                )
        else:
            cache_key = None

        # ── Execute with retry ─────────────────────────────────────────────────
        policy = definition.retry_policy
        last_result: Optional[ToolResult] = None

        for attempt in range(1, policy.max_attempts + 1):
            is_final_attempt = (attempt == policy.max_attempts)
            attempt_timeout_ms = (
                int(timeout_ms * policy.timeout_multiplier_on_final)
                if is_final_attempt and attempt > 1
                else timeout_ms
            )

            result = await self._execute_once(
                tool_name, definition.func, input_data, attempt_timeout_ms
            )
            last_result = result

            if result.success:
                break

            # Check if we should retry
            if (
                result.error
                and result.error.is_retryable
                and result.error.code in policy.retryable_codes
                and not is_final_attempt
            ):
                delay_s = (policy.base_delay_ms / 1000.0) * (
                    policy.backoff_multiplier ** (attempt - 1)
                )
                await asyncio.sleep(delay_s)
                continue

            # Partial result available and caller accepts it
            if result.error and result.error.partial_data_available and allow_partial:
                break

            break

        assert last_result is not None

        # ── Cache successful result ───────────────────────────────────────────
        if (
            last_result.success
            and cache_key is not None
            and definition.cache_strategy in (CacheStrategy.ALWAYS, CacheStrategy.CONDITIONAL)
        ):
            self._cache[cache_key] = last_result

        return last_result

    # ─── Internals ────────────────────────────────────────────────────────────

    async def _execute_once(
        self,
        tool_name: str,
        func: Any,
        input_data: Any,
        timeout_ms: int,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            output = await asyncio.wait_for(
                func(input_data),
                timeout=timeout_ms / 1000.0,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                tool_name=tool_name,
                success=True,
                data=output,
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(
                    code=ToolErrorCode.TIMEOUT,
                    message=f"{tool_name} timed out after {timeout_ms}ms",
                    is_retryable=True,
                ),
                duration_ms=duration_ms,
            )

        except NotImplementedError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(
                    code=ToolErrorCode.UNKNOWN,
                    message=f"{tool_name} is not yet implemented (Phase 0 stub): {exc}",
                    detail=str(exc),
                    is_retryable=False,
                ),
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(
                    code=ToolErrorCode.UNKNOWN,
                    message=f"{tool_name} raised an unexpected error: {type(exc).__name__}: {exc}",
                    detail=traceback.format_exc(),
                    is_retryable=False,
                ),
                duration_ms=duration_ms,
            )

    @staticmethod
    def _make_cache_key(tool_name: str, input_data: Any) -> str:
        try:
            payload = json.dumps(
                {"tool": tool_name, "input": input_data.model_dump() if hasattr(input_data, "model_dump") else input_data},
                sort_keys=True,
                default=str,
            )
        except Exception:
            payload = f"{tool_name}:{repr(input_data)}"
        return hashlib.sha256(payload.encode()).hexdigest()
