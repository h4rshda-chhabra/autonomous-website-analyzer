from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.models import AgentType, TraceEventType
from typing import Any as _Any


def _format_summary(summary: dict) -> str:
    if not summary:
        return ""
    parts = [f"{k}={v}" for k, v in list(summary.items())[:3]]
    return " ".join(parts)
from app.models.trace import AgentTraceEvent, PlanUpdatePayload, ToolCallPayload
from app.runtime.base_agent import ITraceService
from app.infrastructure.logging import get_logger

_log = get_logger(__name__)


class TraceServiceImpl(ITraceService):
    """
    In-memory trace service with sequence-guaranteed event ordering.

    Phase 0: events are buffered in a list per audit; no Redis, no DB flush.
    Phase 1: publish to Redis pub/sub and batch-flush to PostgreSQL.

    emit() is the only write path. Sequence numbers are assigned atomically
    per audit via asyncio.Lock, guaranteeing monotonic order even with
    concurrent specialist agents.
    """

    def __init__(self) -> None:
        self._sequences: Dict[UUID, int] = {}
        self._buffers: Dict[UUID, List[AgentTraceEvent]] = {}
        self._seq_locks: Dict[UUID, asyncio.Lock] = {}

    # ─── ITraceService ────────────────────────────────────────────────────────

    async def emit(
        self,
        audit_id: UUID,
        agent: AgentType,
        event_type: TraceEventType,
        *,
        tool_name: Optional[str] = None,
        tool_input_summary: Optional[Dict[str, Any]] = None,
        tool_output_summary: Optional[Dict[str, Any]] = None,
        tool_duration_ms: Optional[int] = None,
        tool_succeeded: Optional[bool] = None,
        observation: Optional[str] = None,
        reasoning: Optional[str] = None,
        plan_update: Optional[Any] = None,
        finding_id: Optional[UUID] = None,
        finding_title: Optional[str] = None,
        finding_severity: Optional[str] = None,
        error_code: Optional[str] = None,
        is_recoverable: Optional[bool] = None,
    ) -> None:
        async with self._seq_lock(audit_id):
            seq = self._sequences.get(audit_id, 0) + 1
            self._sequences[audit_id] = seq

            # Build optional structured payloads
            tool_payload: Optional[ToolCallPayload] = None
            if event_type in (TraceEventType.TOOL_CALL, TraceEventType.TOOL_RESULT):
                tool_payload = ToolCallPayload(
                    tool_name=tool_name or "unknown",
                    input_summary=tool_input_summary or {},
                    output_summary=tool_output_summary,
                    duration_ms=tool_duration_ms,
                    succeeded=tool_succeeded,
                    error_message=observation if not tool_succeeded else None,
                )

            plan_payload: Optional[PlanUpdatePayload] = None
            if event_type == TraceEventType.PLAN_UPDATE and isinstance(plan_update, PlanUpdatePayload):
                plan_payload = plan_update

            # TOOL_RESULT requires observation; provide a default when agents omit it
            effective_observation = observation
            if (
                event_type == TraceEventType.TOOL_RESULT
                and effective_observation is None
            ):
                if tool_succeeded:
                    summary = tool_output_summary or {}
                    effective_observation = f"Tool completed. {_format_summary(summary)}"
                else:
                    effective_observation = "Tool failed (no error message provided)"

            event = AgentTraceEvent(
                id=uuid4(),
                audit_id=audit_id,
                agent=agent,
                event_type=event_type,
                sequence=seq,
                tool_payload=tool_payload,
                plan_update=plan_payload,
                observation=effective_observation,
                reasoning=reasoning,
                finding_id=finding_id,
                finding_title=finding_title,
                finding_severity=finding_severity,
                error_code=error_code,
                is_recoverable=is_recoverable,
                timestamp=datetime.utcnow(),
            )

            self._buffers.setdefault(audit_id, []).append(event)

            _log.debug(
                "[%s] seq=%d agent=%-15s event=%-20s %s",
                str(audit_id)[:8],
                seq,
                agent.value,
                event_type.value,
                observation or reasoning or finding_title or "",
            )

    async def flush_to_db(self, audit_id: UUID) -> int:
        # Phase 0: no DB; returns count of buffered events
        return len(self._buffers.get(audit_id, []))

    async def get_events(
        self,
        audit_id: UUID,
        *,
        after_sequence: int = 0,
        agent_filter: Optional[AgentType] = None,
        event_type_filter: Optional[TraceEventType] = None,
        limit: int = 500,
    ) -> List[AgentTraceEvent]:
        events = self._buffers.get(audit_id, [])
        result = [
            e for e in events
            if e.sequence > after_sequence
            and (agent_filter is None or e.agent == agent_filter)
            and (event_type_filter is None or e.event_type == event_type_filter)
        ]
        return result[:limit]

    async def get_current_sequence(self, audit_id: UUID) -> int:
        return self._sequences.get(audit_id, 0)

    async def get_timeline(self, audit_id: UUID) -> Dict[str, Any]:
        # Simplified timeline for Phase 0; full AuditTimeline in Phase 1
        events = self._buffers.get(audit_id, [])
        by_agent: Dict[str, List[AgentTraceEvent]] = {}
        for e in events:
            by_agent.setdefault(e.agent.value, []).append(e)
        return {
            "audit_id": str(audit_id),
            "total_events": len(events),
            "agents": {
                agent_name: {
                    "event_count": len(agent_events),
                    "tools_called": [
                        e.tool_call.tool_name
                        for e in agent_events
                        if e.tool_call and e.event_type == TraceEventType.TOOL_CALL
                    ],
                }
                for agent_name, agent_events in by_agent.items()
            },
        }

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _seq_lock(self, audit_id: UUID) -> asyncio.Lock:

        if audit_id not in self._seq_locks:
            self._seq_locks[audit_id] = asyncio.Lock()
        return self._seq_locks[audit_id]
