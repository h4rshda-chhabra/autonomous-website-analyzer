"""
OpenRouterClient — LLMClient implementation backed by OpenRouter.

OpenRouter exposes an OpenAI-compatible /chat/completions endpoint that can
route to any model (Claude, GPT-4, Gemini, Llama, etc.) using a single API key.

Error handling:
  401 / 403 → invalid key or insufficient permissions — return immediately, no retry
  429       → rate limited — retry with exponential back-off (2 attempts max)
  timeout   → network stall — retry once
  other 4xx → non-retryable — return immediately
  5xx       → provider error — retry once
  network   → connection failure — retry once
"""
from __future__ import annotations

import asyncio
import json as _json
from typing import Any, List

import httpx

from .base import LLMClient, LLMMessage, LLMResponse, LLMUsage

_BASE_URL = "https://openrouter.ai/api/v1"
_RETRY_DELAYS = (0, 2, 5)   # seconds before each attempt (0 = immediate first try)


class OpenRouterClient(LLMClient):

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 90.0) -> None:
        self._api_key = api_key.strip()
        self._model = model
        self._timeout = timeout_seconds
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/autonomous-website-analyzer",
                "X-Title": "Autonomous Website Analyzer",
            },
        )

    # ── LLMClient interface ───────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self._api_key:
            return LLMResponse(
                success=False,
                error="OpenRouter: no API key configured (set OPENROUTER_API_KEY)",
            )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error = "Unknown error"
        max_attempts = len(_RETRY_DELAYS)

        for attempt, delay in enumerate(_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)

            try:
                resp = await self._http.post("/chat/completions", json=payload)
            except httpx.TimeoutException:
                last_error = "OpenRouter: request timed out"
                continue
            except httpx.ConnectError as exc:
                last_error = f"OpenRouter: connection failed — {exc}"
                continue
            except httpx.NetworkError as exc:
                last_error = f"OpenRouter: network error — {exc}"
                continue
            except Exception as exc:
                return LLMResponse(
                    success=False,
                    error=f"OpenRouter: unexpected client error — {type(exc).__name__}: {exc}",
                )

            status = resp.status_code

            if status == 200:
                return self._parse_success(resp)

            if status in (401, 403):
                msg = self._extract_error(resp)
                return LLMResponse(
                    success=False,
                    error=f"OpenRouter: authentication/permission error ({status}): {msg}",
                )

            if status == 422:
                msg = self._extract_error(resp)
                return LLMResponse(
                    success=False,
                    error=f"OpenRouter: invalid model or request ({status}): {msg}",
                )

            if status == 429:
                last_error = "OpenRouter: rate limited (429)"
                # 429 retries use the existing delay schedule — just continue
                continue

            if 500 <= status < 600:
                last_error = f"OpenRouter: provider error ({status})"
                continue

            last_error = f"OpenRouter: HTTP {status} — {self._extract_error(resp)}"
            break   # non-retryable 4xx

        return LLMResponse(success=False, error=last_error)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_success(self, resp: httpx.Response) -> LLMResponse:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return LLMResponse(
                success=True,
                content=content,
                model=data.get("model", self._model),
                usage=LLMUsage(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                ),
            )
        except Exception as exc:
            return LLMResponse(
                success=False,
                error=f"OpenRouter: failed to parse response — {exc}",
            )

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error") or {}
                if isinstance(err, dict):
                    return err.get("message", resp.text[:200])
                return str(err)[:200]
        except Exception:
            pass
        return resp.text[:200]

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()
