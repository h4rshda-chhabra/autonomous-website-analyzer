"""
LLMClient — provider-agnostic LLM interface.

No agent or service may import a concrete provider (OpenRouterClient, Anthropic, etc.)
directly. All AI calls go through this interface so the underlying provider can be
swapped without touching agent code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LLMMessage:
    role: str   # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    success: bool
    content: Optional[str] = None
    error: Optional[str] = None
    model: Optional[str] = None
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMClient(ABC):
    """
    Abstract interface for all LLM providers.

    Contract:
    - complete() NEVER raises — all failures are returned as LLMResponse(success=False).
    - Retries and backoff are the provider's responsibility, not the caller's.
    - Callers must handle success=False gracefully (deterministic fallback).
    """

    @abstractmethod
    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: Conversation history. Always starts with system or user message.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0 = deterministic, 1 = creative).
            json_mode: Request a structured JSON response object.

        Returns:
            LLMResponse. Never raises.
        """
        ...

    @property
    @abstractmethod
    def model(self) -> str:
        """The model identifier string, e.g. 'anthropic/claude-sonnet-4'."""
        ...

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """False if no API key is configured — callers can skip AI without a round-trip."""
        ...
