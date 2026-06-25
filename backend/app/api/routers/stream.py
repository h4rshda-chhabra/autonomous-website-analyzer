from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_services
from app.models.enums import AuditStatus

router = APIRouter(prefix="/audits", tags=["stream"])

_TERMINAL_STATUSES = frozenset({
    AuditStatus.COMPLETE,
    AuditStatus.COMPLETE_WITH_WARNINGS,
    AuditStatus.FAILED,
})
_POLL_INTERVAL = 0.3  # seconds between polls


@router.get(
    "/{audit_id}/stream",
    summary="Stream trace events via Server-Sent Events",
    response_description="SSE stream of AgentTraceEvents",
)
async def stream_audit(audit_id: UUID, request: Request) -> EventSourceResponse:
    """
    Streams AgentTraceEvents in real time using Server-Sent Events.

    Each event carries:
    - id: sequence number (use as Last-Event-ID for reconnect)
    - event: "trace" for normal events, "done" for end-of-stream
    - data: JSON-serialised trace event payload

    Reconnect support: send the Last-Event-ID header with the last
    received sequence number — the stream will resume from that point.
    """
    services = get_services(request)
    state = await services.state.get_state(audit_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    # Support reconnect via Last-Event-ID header
    last_event_id = request.headers.get("last-event-id", "0")
    try:
        after_seq = int(last_event_id)
    except (ValueError, TypeError):
        after_seq = 0

    return EventSourceResponse(
        _event_generator(audit_id, services, after_seq, request),
        headers={"Cache-Control": "no-cache"},
    )


async def _event_generator(
    audit_id: UUID,
    services,
    after_seq: int,
    request: Request,
) -> AsyncGenerator[dict, None]:
    """
    Polls TraceServiceImpl for new events every _POLL_INTERVAL seconds.
    Closes when the audit reaches a terminal status and there are no more events.
    """
    consecutive_empty_terminal = 0

    while True:
        # Honour client disconnect
        if await request.is_disconnected():
            break

        state = await services.state.get_state(audit_id)
        if state is None:
            break

        events = await services.trace.get_events(
            audit_id,
            after_sequence=after_seq,
            limit=50,
        )

        for event in events:
            after_seq = event.sequence
            yield {
                "id": str(event.sequence),
                "event": "trace",
                "data": json.dumps(event.to_sse_dict()),
            }

        is_terminal = state.status in _TERMINAL_STATUSES

        if is_terminal and not events:
            consecutive_empty_terminal += 1
            if consecutive_empty_terminal >= 2:
                # Two consecutive polls with no new events after terminal → done
                yield {
                    "event": "done",
                    "data": json.dumps({
                        "audit_id": str(audit_id),
                        "status": state.status.value,
                        "total_sequence": after_seq,
                    }),
                }
                break
        else:
            consecutive_empty_terminal = 0

        await asyncio.sleep(_POLL_INTERVAL)
