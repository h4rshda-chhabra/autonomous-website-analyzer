from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from app.services.shared_state_service import SharedStateService
from app.services.trace_service import TraceServiceImpl
from app.services.finding_factory import FindingFactoryImpl
from app.tools.registry import ToolRegistry
from app.llm.base import LLMClient


@dataclass
class AppServices:
    state: SharedStateService
    trace: TraceServiceImpl
    factory: FindingFactoryImpl
    registry: ToolRegistry
    llm: Optional[LLMClient] = None


def get_services(request: Request) -> AppServices:
    return request.app.state.services
