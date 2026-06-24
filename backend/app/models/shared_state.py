from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from .audit_plan import AuditPlan
from .enums import AgentStatus, AgentType, AuditStatus
from .finding import Finding
from .site_profile import SiteProfile
from .trace import AgentTraceEvent


# ─── Sub-models ───────────────────────────────────────────────────────────────

class AgentStateEntry(BaseModel):
    """
    Runtime state of a single specialist agent, tracked by the Orchestrator.
    Written by agents as they progress; read by Orchestrator and SSE endpoint.
    """

    agent: AgentType
    status: AgentStatus = AgentStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    findings_written: int = Field(0, description="Running count of findings written to shared state")
    current_tool: Optional[str] = Field(None, description="Tool the agent is currently executing")
    current_action_summary: Optional[str] = Field(
        None,
        description="What the agent is doing right now (for live UI display)",
    )
    error_message: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (AgentStatus.COMPLETE, AgentStatus.FAILED, AgentStatus.SKIPPED)


class AuditProgress(BaseModel):
    """Snapshot of overall audit completion state for the status endpoint and SSE."""

    total_agents: int
    complete_agents: int
    failed_agents: int
    skipped_agents: int
    total_findings: int
    critical_findings: int
    high_findings: int
    current_phase: AuditStatus
    estimated_completion_at: Optional[datetime] = None

    @property
    def percent_complete(self) -> int:
        if self.total_agents == 0:
            return 0
        terminal = self.complete_agents + self.failed_agents + self.skipped_agents
        return int((terminal / self.total_agents) * 100)

    @property
    def all_agents_terminal(self) -> bool:
        terminal = self.complete_agents + self.failed_agents + self.skipped_agents
        return terminal >= self.total_agents


# ─── Core Model ───────────────────────────────────────────────────────────────

class SharedState(BaseModel):
    """
    The central in-memory state object for an active audit session.

    This is not a database model — it is the runtime contract that:
      1. The Orchestrator reads to manage the audit lifecycle
      2. Specialist agents read to get context (site_profile, audit_plan, other_findings)
      3. Specialist agents write to (findings, agent_states, trace_events)
      4. The SSE endpoint reads to serve live updates

    Persistence strategy:
      - This model is serialized to Redis as a JSON blob (keyed by audit_id)
        for fast cross-process access during the audit
      - On audit completion, all findings and trace events are flushed to PostgreSQL
      - Redis key TTL: 2 hours (sufficient for any audit to complete)

    Mutation discipline:
      - Agents NEVER mutate this object directly in Python
      - All writes go through SharedStateService methods, which apply
        optimistic locking via Redis atomic operations and emit trace events
      - Reading is always safe — agents get a snapshot via SharedStateService.get()
    """

    audit_id: UUID = Field(..., description="The audit session this state belongs to")
    url: str = Field(..., description="The URL being audited")
    status: AuditStatus = Field(AuditStatus.PENDING)

    # ── Phase Artifacts (set once, immutable after) ───────────────────────────
    site_profile: Optional[SiteProfile] = Field(
        None,
        description="Set once by Orchestrator after Reconnaissance. Never mutated after.",
    )
    audit_plan: Optional[AuditPlan] = Field(
        None,
        description="Set once by Orchestrator after Planning. May be replaced if PLAN_UPDATE occurs.",
    )

    # ── Agent Runtime State ───────────────────────────────────────────────────
    agent_states: Dict[AgentType, AgentStateEntry] = Field(
        default_factory=dict,
        description=(
            "Runtime state per agent. Initialized by Orchestrator before dispatch. "
            "Updated by each agent as it progresses."
        ),
    )

    # ── Findings Store ────────────────────────────────────────────────────────
    findings: Dict[AgentType, List[Finding]] = Field(
        default_factory=dict,
        description=(
            "All findings, indexed by the agent that produced them. "
            "Initialized as empty lists per agent when the plan is set. "
            "Synthesis findings are stored under AgentType.SYNTHESIS."
        ),
    )

    # ── Trace Events ─────────────────────────────────────────────────────────
    trace_events: List[AgentTraceEvent] = Field(
        default_factory=list,
        description=(
            "All trace events in sequence order. "
            "Agents append events — the list grows monotonically. "
            "In Redis, this is stored as a sorted set by sequence number."
        ),
    )
    next_sequence: int = Field(
        0,
        description="Monotonically increasing counter for trace event sequencing",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)
    recon_completed_at: Optional[datetime] = None
    planning_completed_at: Optional[datetime] = None
    auditing_started_at: Optional[datetime] = None
    synthesis_started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None

    # ── Read Helpers (used by agents to query other agents' findings) ─────────
    def get_findings_by_agent(self, agent: AgentType) -> List[Finding]:
        return self.findings.get(agent, [])

    def get_all_findings(self) -> List[Finding]:
        result = []
        for findings in self.findings.values():
            result.extend(findings)
        return result

    def get_findings_by_severity(self, *severities) -> List[Finding]:
        return [f for f in self.get_all_findings() if f.severity in severities]

    def get_agent_state(self, agent: AgentType) -> Optional[AgentStateEntry]:
        return self.agent_states.get(agent)

    def is_agent_complete(self, agent: AgentType) -> bool:
        state = self.agent_states.get(agent)
        return state is not None and state.status == AgentStatus.COMPLETE

    def are_all_specialist_agents_terminal(self) -> bool:
        specialist_agents = [a for a in AgentType if a not in (AgentType.ORCHESTRATOR, AgentType.SYNTHESIS)]
        return all(
            self.agent_states.get(a, AgentStateEntry(agent=a)).is_terminal
            for a in specialist_agents
        )

    # ── Progress Snapshot ─────────────────────────────────────────────────────
    @property
    def progress(self) -> AuditProgress:
        from .enums import Severity
        specialist_agents = [a for a in AgentType if a not in (AgentType.ORCHESTRATOR, AgentType.SYNTHESIS)]
        states = [self.agent_states.get(a, AgentStateEntry(agent=a)) for a in specialist_agents]
        all_findings = self.get_all_findings()

        return AuditProgress(
            total_agents=len(specialist_agents),
            complete_agents=sum(1 for s in states if s.status == AgentStatus.COMPLETE),
            failed_agents=sum(1 for s in states if s.status == AgentStatus.FAILED),
            skipped_agents=sum(1 for s in states if s.status == AgentStatus.SKIPPED),
            total_findings=len(all_findings),
            critical_findings=sum(1 for f in all_findings if f.severity == Severity.CRITICAL),
            high_findings=sum(1 for f in all_findings if f.severity == Severity.HIGH),
            current_phase=self.status,
        )

    # ── Validator ─────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def validate_findings_keys_are_agent_types(self) -> "SharedState":
        for key in self.findings:
            if not isinstance(key, AgentType):
                raise ValueError(f"findings key must be AgentType, got: {key}")
        return self

    class Config:
        # Allows AgentType enum as dict key in model serialization
        use_enum_values = False
