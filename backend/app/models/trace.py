from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from .enums import AgentType, TraceEventType


# ─── Sub-models ───────────────────────────────────────────────────────────────

class ToolCallPayload(BaseModel):
    """Structured payload for TOOL_CALL and TOOL_RESULT event types."""

    tool_name: str = Field(..., description="Name of the tool being called (from Tool Catalog)")
    input_summary: Dict[str, Any] = Field(
        ...,
        description=(
            "Summarized tool inputs — NOT the full payload. "
            "Omit large HTML blobs; include only what is meaningful for display. "
            "E.g. {'url': 'https://example.com', 'selector': 'h1'}"
        ),
    )
    output_summary: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Summarized tool output for TOOL_RESULT events. "
            "Null for TOOL_CALL events (result not yet available)."
        ),
    )
    duration_ms: Optional[int] = Field(
        None,
        description="How long the tool took to execute (set on TOOL_RESULT only)",
    )
    succeeded: Optional[bool] = Field(
        None,
        description="Whether the tool call succeeded (set on TOOL_RESULT only)",
    )
    error_message: Optional[str] = Field(
        None,
        description="Error detail if succeeded=False",
    )


class PlanUpdatePayload(BaseModel):
    """
    Structured payload for PLAN_UPDATE events.
    Emitted when the Orchestrator changes the AuditPlan mid-audit based on
    findings from already-running agents.
    """
    previous_state: str = Field(
        ...,
        description="What the plan said before this update (human-readable summary)",
    )
    new_state: str = Field(
        ...,
        description="What the plan says after this update",
    )
    trigger: str = Field(
        ...,
        description="The finding or observation that caused the plan to change",
    )
    affected_agents: List[AgentType] = Field(
        default_factory=list,
        description="Which agents had their configuration updated",
    )


# ─── Core Model ───────────────────────────────────────────────────────────────

class AgentTraceEvent(BaseModel):
    """
    A single observable event emitted by an agent during its execution.

    This is the model that powers the Live Trace Panel.
    Events are written to Redis pub/sub and consumed by the SSE endpoint
    in real time, then persisted to PostgreSQL for historical access.

    Design principle: every meaningful action an agent takes must emit an event.
    Silence in the trace = a gap in observability.

    Field population by event_type:
    ┌─────────────────┬──────────────┬────────────┬─────────────┬─────────────┬─────────────────┐
    │ event_type      │ tool_payload │ observation│ reasoning   │ plan_update │ finding_id      │
    ├─────────────────┼──────────────┼────────────┼─────────────┼─────────────┼─────────────────┤
    │ AGENT_STARTED   │ -            │ optional   │ -           │ -           │ -               │
    │ TOOL_CALL       │ required     │ -          │ optional    │ -           │ -               │
    │ TOOL_RESULT     │ required     │ required   │ -           │ -           │ -               │
    │ OBSERVATION     │ -            │ required   │ optional    │ -           │ -               │
    │ REASONING       │ -            │ -          │ required    │ -           │ -               │
    │ PLAN_UPDATE     │ -            │ -          │ required    │ required    │ -               │
    │ FINDING_WRITTEN │ -            │ optional   │ -           │ -           │ required        │
    │ AGENT_COMPLETE  │ -            │ optional   │ -           │ -           │ -               │
    │ ERROR           │ optional     │ required   │ optional    │ -           │ -               │
    └─────────────────┴──────────────┴────────────┴─────────────┴─────────────┴─────────────────┘
    """

    id: UUID = Field(default_factory=uuid4)
    audit_id: UUID = Field(..., description="Parent audit session")
    sequence: int = Field(
        ...,
        ge=0,
        description=(
            "Monotonically increasing sequence number within the audit. "
            "Used to reconstruct event order on the client — do not rely on timestamp alone."
        ),
    )
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # ── Agent Context ─────────────────────────────────────────────────────────
    agent: AgentType = Field(..., description="The agent emitting this event")
    event_type: TraceEventType

    # ── Tool Events (TOOL_CALL, TOOL_RESULT) ─────────────────────────────────
    tool_payload: Optional[ToolCallPayload] = Field(
        None,
        description="Populated for TOOL_CALL and TOOL_RESULT events only",
    )

    # ── Human-readable Content ────────────────────────────────────────────────
    observation: Optional[str] = Field(
        None,
        max_length=500,
        description=(
            "What the agent just discovered. Written in plain English for the trace panel. "
            "E.g. 'Found 23 images missing alt text across 4 pages.'"
        ),
    )
    reasoning: Optional[str] = Field(
        None,
        max_length=500,
        description=(
            "Why this matters and what the agent will do next. "
            "E.g. 'This many missing alt attributes will produce multiple WCAG 1.1.1 violations — "
            "will now check if any are decorative images that should use alt=\"\".'"
        ),
    )

    # ── Plan Update (PLAN_UPDATE only) ────────────────────────────────────────
    plan_update: Optional[PlanUpdatePayload] = Field(
        None,
        description="Populated for PLAN_UPDATE events only. Orchestrator-emitted.",
    )

    # ── Finding Reference (FINDING_WRITTEN only) ──────────────────────────────
    finding_id: Optional[UUID] = Field(
        None,
        description="The ID of the finding that was just written to shared state",
    )
    finding_title: Optional[str] = Field(
        None,
        description="Finding title — duplicated here so the trace panel can display it without a DB lookup",
    )
    finding_severity: Optional[str] = Field(
        None,
        description="Severity string for color-coding in the trace panel",
    )

    # ── Error Detail (ERROR only) ─────────────────────────────────────────────
    error_code: Optional[str] = Field(None, description="Machine-readable error code")
    is_recoverable: Optional[bool] = Field(
        None,
        description=(
            "True if the agent is continuing despite this error. "
            "False means the agent is halting and marking itself as FAILED."
        ),
    )

    # ── Validators ───────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def validate_required_fields_per_event_type(self) -> "AgentTraceEvent":
        t = self.event_type

        if t in (TraceEventType.TOOL_CALL, TraceEventType.TOOL_RESULT):
            if self.tool_payload is None:
                raise ValueError(f"tool_payload is required for event_type={t}")

        if t in (TraceEventType.TOOL_RESULT, TraceEventType.OBSERVATION, TraceEventType.ERROR):
            if self.observation is None:
                raise ValueError(f"observation is required for event_type={t}")

        if t == TraceEventType.REASONING:
            if self.reasoning is None:
                raise ValueError("reasoning is required for REASONING events")

        if t == TraceEventType.PLAN_UPDATE:
            if self.plan_update is None:
                raise ValueError("plan_update is required for PLAN_UPDATE events")
            if self.reasoning is None:
                raise ValueError("reasoning is required for PLAN_UPDATE events")

        if t == TraceEventType.FINDING_WRITTEN:
            if self.finding_id is None:
                raise ValueError("finding_id is required for FINDING_WRITTEN events")

        return self

    @property
    def display_label(self) -> str:
        """Short label for trace panel display."""
        labels = {
            TraceEventType.AGENT_STARTED:   f"{self.agent.value} started",
            TraceEventType.TOOL_CALL:       f"→ {self.tool_payload.tool_name if self.tool_payload else 'tool'}",
            TraceEventType.TOOL_RESULT:     f"← {self.tool_payload.tool_name if self.tool_payload else 'result'}",
            TraceEventType.OBSERVATION:     "Observed",
            TraceEventType.REASONING:       "Reasoning",
            TraceEventType.PLAN_UPDATE:     "Plan updated",
            TraceEventType.FINDING_WRITTEN: f"Finding: {self.finding_severity or '?'} — {self.finding_title or ''}",
            TraceEventType.AGENT_COMPLETE:  f"{self.agent.value} complete",
            TraceEventType.ERROR:           f"Error ({'recoverable' if self.is_recoverable else 'fatal'})",
        }
        return labels.get(self.event_type, self.event_type.value)

    def to_sse_dict(self) -> Dict[str, Any]:
        """
        Serializes the event for Server-Sent Events delivery.
        Excludes raw tool payloads — only human-readable content is streamed.
        """
        return {
            "id":              str(self.id),
            "audit_id":        str(self.audit_id),
            "sequence":        self.sequence,
            "timestamp":       self.timestamp.isoformat(),
            "agent":           self.agent.value,
            "event_type":      self.event_type.value,
            "display_label":   self.display_label,
            "observation":     self.observation,
            "reasoning":       self.reasoning,
            "finding_id":      str(self.finding_id) if self.finding_id else None,
            "finding_title":   self.finding_title,
            "finding_severity": self.finding_severity,
            "plan_update":     self.plan_update.model_dump() if self.plan_update else None,
            "tool_name":       self.tool_payload.tool_name if self.tool_payload else None,
            "tool_succeeded":  self.tool_payload.succeeded if self.tool_payload else None,
            "tool_duration_ms": self.tool_payload.duration_ms if self.tool_payload else None,
            "error_code":      self.error_code,
            "is_recoverable":  self.is_recoverable,
        }
