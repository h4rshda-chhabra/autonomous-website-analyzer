"""
TraceService — Design Specification
═════════════════════════════════════
The TraceService is the single write path for all trace events.
It is responsible for:
  1. Assigning monotonically increasing sequence numbers (per audit)
  2. Constructing AgentTraceEvent objects from agent-provided fields
  3. Publishing events to Redis pub/sub (for SSE streaming)
  4. Buffering events for batch persistence to PostgreSQL
  5. Generating the full audit timeline for the report

Design principles:
  - emit() is fire-and-forget from the agent's perspective (async, non-blocking)
  - Sequence numbers are assigned atomically via Redis INCR — never duplicated
  - Events are published to Redis BEFORE being written to DB — SSE latency is prioritized
  - DB writes are batched (every 10 events or every 2 seconds, whichever comes first)
  - If Redis is unavailable: events are written directly to DB (degraded mode, SSE breaks)
  - If DB write fails: events are logged and retried — audit continues unaffected

SSE Compatibility:
  The SSE endpoint at GET /api/v1/audits/{id}/stream does:
    1. Subscribe to Redis channel: audit:{id}:events
    2. For each message received: parse JSON → yield SSE event
    3. On audit complete: yield SSE 'complete' event → close stream

  Event format on Redis channel (JSON string):
    {
      "event": "trace" | "status" | "complete",
      "data": { ...AgentTraceEvent.to_sse_dict() }
    }

  The SSE endpoint itself does NOT query the DB — all live data comes from Redis.
  The DB is used for: historical trace retrieval, audit replay, reconnect recovery.

Reconnect Recovery:
  If the frontend SSE connection drops and reconnects with ?last_sequence=N,
  the SSE endpoint replays all events with sequence > N from the DB before
  re-subscribing to Redis. This prevents missed events on reconnect.

Sequence Number Guarantee:
  Redis key: audit:{id}:seq
  Operation: INCR audit:{id}:seq  (atomic, returns new value)
  The sequence starts at 0. First event gets sequence=1.
  Sequence numbers are gapless within a healthy audit.
  Gaps in a recovered audit (Redis failure during window) are documented in
  the audit's failure_reason if applicable.

Timeline Generation:
  get_timeline(audit_id) reads all trace events from DB ordered by sequence.
  Returns AgentTimeline: groups events by agent, calculates per-agent duration,
  identifies the critical path (longest agent chain), surfaces key moments
  (first finding written, plan updates, errors).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.models import AgentTraceEvent, AgentType, TraceEventType


# ─── Timeline Models ──────────────────────────────────────────────────────────

class AgentTimeline:
    """
    Per-agent timeline extracted from the full trace.
    Provides structured data for the frontend timeline component.
    """
    agent: AgentType
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    events: List[AgentTraceEvent]
    tool_calls: List[str]                   # Tool names called in order
    findings_written: int
    had_errors: bool
    error_messages: List[str]
    key_observations: List[str]             # OBSERVATION events — shown in summary


class AuditTimeline:
    """
    Full audit timeline synthesized from all trace events.
    Used by the report view's Timeline tab.
    """
    audit_id: UUID
    total_duration_seconds: Optional[float]
    agent_timelines: Dict[AgentType, AgentTimeline]
    plan_updates: List[AgentTraceEvent]     # All PLAN_UPDATE events
    critical_path_agents: List[AgentType]   # The agents on the longest dependency chain
    total_events: int
    total_tool_calls: int
    total_findings: int


# ─── TraceService ─────────────────────────────────────────────────────────────

class TraceService(ABC):
    """
    Manages the creation, sequencing, streaming, and persistence of trace events.

    Dependencies:
      - Redis client (for pub/sub + sequence counter)
      - DB session factory (for batch persistence)

    Shared across all agents in an audit — one instance per audit session.
    Thread-safe: emit() uses asyncio locks for sequence assignment.

    Emit flow:
    ┌──────────────────────────────────────────────────────────────────┐
    │ emit(audit_id, agent, event_type, **fields)                      │
    │   1. Acquire sequence lock for audit_id                          │
    │   2. INCR audit:{id}:seq → sequence number                       │
    │   3. Construct AgentTraceEvent (validates field requirements)    │
    │   4. Publish to Redis: PUBLISH audit:{id}:events <json>          │
    │   5. Add to in-memory buffer                                     │
    │   6. Release lock                                                │
    │   7. If buffer size ≥ 10 OR last_flush > 2s ago:                │
    │        flush_to_db() (async, does not block emit caller)        │
    └──────────────────────────────────────────────────────────────────┘
    """

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    async def flush_to_db(self, audit_id: UUID) -> int:
        """
        Flushes buffered events to PostgreSQL.
        Returns the number of events flushed.
        Called automatically by emit() when buffer threshold is reached.
        Also called explicitly by AgentRuntime on audit complete/fail.
        """
        ...

    @abstractmethod
    async def get_events(
        self,
        audit_id: UUID,
        *,
        after_sequence: int = 0,
        agent_filter: Optional[AgentType] = None,
        event_type_filter: Optional[TraceEventType] = None,
        limit: int = 500,
    ) -> List[AgentTraceEvent]:
        """
        Reads trace events from PostgreSQL.
        Used for: timeline generation, reconnect recovery, report export.
        For live streaming, events come from Redis pub/sub, not this method.
        """
        ...

    @abstractmethod
    async def get_timeline(self, audit_id: UUID) -> AuditTimeline:
        """
        Generates the full AuditTimeline from persisted trace events.
        Called after audit completes to generate the timeline for the report.
        """
        ...

    @abstractmethod
    async def get_current_sequence(self, audit_id: UUID) -> int:
        """Returns the current sequence counter (for reconnect recovery)."""
        ...


# ─── SSE Event Format Reference ───────────────────────────────────────────────

"""
SSE Wire Format
───────────────
Each SSE message is a UTF-8 text block with two newlines at the end.

Live trace event:
  event: trace\n
  data: {"id":"...","sequence":42,"agent":"seo","event_type":"tool_result",
         "display_label":"← MetaTagAnalyzer","observation":"Found title tag: 47 chars",
         "reasoning":null,"finding_id":null,...}\n
  \n

Status update (emitted every time any agent state changes):
  event: status\n
  data: {"status":"auditing","progress_pct":60,
         "agent_states":{"seo":"complete","performance":"running",...},
         "total_findings":12}\n
  \n

Audit complete:
  event: complete\n
  data: {"audit_id":"...","overall_score":67,"total_findings":28,"roadmap_items":11}\n
  \n

Error (non-fatal — audit continues):
  event: trace\n
  data: {"event_type":"error","agent":"performance","is_recoverable":true,
         "observation":"LighthouseRunner timed out after 120s — skipping CWV findings",
         "error_code":"timeout"}\n
  \n

Client reconnect with missed events:
  GET /api/v1/audits/{id}/stream?last_sequence=35
  Server replays events 36..N from DB, then subscribes to Redis for live events.
  The 'last_sequence' param prevents event loss during dropped connections.
"""
