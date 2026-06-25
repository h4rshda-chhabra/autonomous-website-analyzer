"""
BaseAgent — Abstract Contract
══════════════════════════════
Every specialist agent inherits from BaseAgent. This class defines the entire
interface that the AgentRuntime, ToolExecutor, TraceService, and FindingFactory
expect from any agent. No agent implementation should bypass these methods.

Position in the runtime stack:
  AgentRuntime (lifecycle orchestration)
    └── BaseAgent.execute()            ← agent-specific logic lives here
          ├── run_tool()               ← always goes through ToolExecutor
          ├── create_finding()         ← always goes through FindingFactory
          ├── emit_trace()             ← always goes through TraceService
          ├── read_state()             ← always through SharedStateReader
          └── write_finding()          ← always through SharedStateWriter

Design constraints enforced by this class:
  1. Agents CANNOT call tools directly — only through run_tool()
  2. Agents CANNOT write arbitrary state — only findings and their own agent_state
  3. Agents CANNOT read another agent's raw tool outputs — only processed findings
  4. All trace events flow through emit_trace() — no silent agent actions
  5. execute() is the only entry point — AgentRuntime calls nothing else
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, Type, TypeVar
from uuid import UUID

from app.models import (
    AgentStatus,
    AgentType,
    AuditPlan,
    AuditStatus,
    Finding,
    FindingCategory,
    ImplementationEffort,
    Severity,
    SiteProfile,
    TraceEventType,
)
from app.models.shared_state import AgentStateEntry
from app.tools.base import ToolResult

T = TypeVar("T")


# ─── Dependency Interfaces (injected — never instantiated inside agents) ───────

class IToolExecutor(ABC):
    """Interface the agent uses to call tools. Implemented by ToolExecutor."""

    @abstractmethod
    async def run(
        self,
        tool_name: str,
        input_data: Any,
        *,
        timeout_override_ms: Optional[int] = None,
        allow_partial: bool = False,
    ) -> ToolResult:
        """
        Executes a registered tool by name. Returns ToolResult[T].
        Never raises — all failures are encapsulated in ToolResult.error.
        """
        ...


class ITraceService(ABC):
    """Interface for emitting trace events. Implemented by TraceService."""

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


class ISharedStateReader(ABC):
    """Read-only interface into SharedState. Agents receive this, not the full SharedState."""

    @abstractmethod
    async def get_site_profile(self, audit_id: UUID) -> Optional[SiteProfile]: ...

    @abstractmethod
    async def get_audit_plan(self, audit_id: UUID) -> Optional[AuditPlan]: ...

    @abstractmethod
    async def get_findings_by_agent(self, audit_id: UUID, agent: AgentType) -> List[Finding]: ...

    @abstractmethod
    async def get_all_findings(self, audit_id: UUID) -> List[Finding]: ...

    @abstractmethod
    async def get_recon_data(self, audit_id: UUID, key: str) -> Optional[Any]:
        """
        Retrieve a specific recon artifact by key.
        Keys: 'playwright_output', 'header_analysis', 'link_extraction', 'screenshot_path'
        These are read-only snapshots from the Orchestrator's recon phase.
        """
        ...

    @abstractmethod
    async def is_agent_complete(self, audit_id: UUID, agent: AgentType) -> bool: ...

    @abstractmethod
    async def are_all_specialist_agents_terminal(self, audit_id: UUID) -> bool: ...


class ISharedStateWriter(ABC):
    """Write interface for agents. Narrowly scoped — agents can only write findings + own state."""

    @abstractmethod
    async def append_finding(self, audit_id: UUID, finding: Finding) -> None:
        """Append a finding to the agent's findings list. Thread-safe atomic append."""
        ...

    @abstractmethod
    async def update_agent_state(
        self,
        audit_id: UUID,
        agent: AgentType,
        *,
        status: Optional[AgentStatus] = None,
        current_tool: Optional[str] = None,
        current_action_summary: Optional[str] = None,
        increment_findings: bool = False,
    ) -> None: ...

    @abstractmethod
    async def store_recon_artifact(self, audit_id: UUID, key: str, data: Any) -> None:
        """Orchestrator-only. Stores a recon artifact for downstream agent consumption."""
        ...

    @abstractmethod
    async def set_site_profile(self, audit_id: UUID, profile: SiteProfile) -> None:
        """Orchestrator-only. Written once, immutable after."""
        ...

    @abstractmethod
    async def set_audit_plan(self, audit_id: UUID, plan: AuditPlan) -> None:
        """Orchestrator-only. Replaces plan if called again (PLAN_UPDATE scenario)."""
        ...

    @abstractmethod
    async def transition_status(self, audit_id: UUID, new_status: AuditStatus) -> None:
        """Updates the audit's AuditStatus. Called by Orchestrator and Synthesis."""
        ...


class IFindingFactory(ABC):
    """Interface for creating Finding objects from tool outputs."""

    @abstractmethod
    def create(
        self,
        audit_id: UUID,
        agent: AgentType,
        category: FindingCategory,
        title: str,
        description: str,
        severity: Severity,
        business_impact: str,
        impact_score: int,
        effort: ImplementationEffort,
        effort_hours_min: int,
        effort_hours_max: int,
        fix_description: str,
        tool_name: str,
        evidence_raw_data: Dict[str, Any],
        *,
        confidence: float,
        affected_elements: Optional[List[str]] = None,
        affected_count: Optional[int] = None,
        metric_value: Optional[str] = None,
        metric_threshold: Optional[str] = None,
        code_snippet: Optional[str] = None,
        snippet_language: Optional[str] = None,
        documentation_url: Optional[str] = None,
        tags: Optional[List[str]] = None,
        wcag_criteria: Optional[str] = None,
    ) -> Finding: ...

    @abstractmethod
    def create_synthesis_finding(
        self,
        *,
        audit_id: UUID,
        title: str,
        description: str,
        severity: Severity,
        business_impact: str,
        impact_score: int,
        effort: ImplementationEffort,
        effort_hours_min: int,
        effort_hours_max: int,
        fix_description: str,
        source_finding_ids: List[UUID],
        insight_type: str = "compound",
        tags: Optional[List[str]] = None,
    ) -> Finding: ...


# ─── BaseAgent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Abstract base class for all audit agents.

    Lifecycle:
      AgentRuntime calls: initialize() → execute() → [complete() | fail()]
      The agent must not override complete() or fail() — they are sealed.
      All agent-specific logic lives in execute().

    Reasoning loop (implemented in each agent's execute()):
      1. PLAN   — read SharedState to understand context and scope
      2. ACT    — call tools via run_tool()
      3. OBSERVE — interpret tool outputs
      4. REASON  — decide what findings to create, what to investigate next
      5. WRITE   — write findings, emit trace events
      6. REPEAT  — loop if adaptive investigation is needed
      7. COMPLETE — mark self as done

    Failure contract:
      - Tool failures are handled gracefully within execute() via ToolResult.error
      - Unhandled exceptions in execute() are caught by AgentRuntime
      - AgentRuntime calls fail() with the exception — agent cannot prevent this
      - Partial findings written before failure ARE persisted — they are not rolled back
    """

    def __init__(
        self,
        audit_id: UUID,
        tool_executor: IToolExecutor,
        trace_service: ITraceService,
        state_reader: ISharedStateReader,
        state_writer: ISharedStateWriter,
        finding_factory: IFindingFactory,
        llm_client: Optional[Any] = None,
    ) -> None:
        self.audit_id = audit_id
        self._tools = tool_executor
        self._trace = trace_service
        self._reader = state_reader
        self._writer = state_writer
        self._factory = finding_factory
        self._llm = llm_client   # Optional[LLMClient] — None when AI is not configured
        self._findings_written: int = 0
        self._started_at: Optional[datetime] = None

    # ── Abstract Interface ────────────────────────────────────────────────────

    @abstractmethod
    def agent_type(self) -> AgentType:
        """Returns this agent's AgentType. Used for state updates and trace events."""
        ...

    @abstractmethod
    def allowed_tools(self) -> List[str]:
        """
        Returns the list of tool names this agent is permitted to call.
        ToolExecutor enforces this — calling an unregistered or unauthorized tool raises ToolNotAllowedError.
        """
        ...

    @abstractmethod
    async def execute(self) -> None:
        """
        Agent-specific execution logic. The entire reasoning loop lives here.
        Must call complete() before returning. Must not catch BaseException.
        All tool calls go through run_tool(). All findings go through create_finding().
        """
        ...

    # ── Lifecycle (sealed — do not override) ──────────────────────────────────

    async def initialize(self) -> None:
        """Called by AgentRuntime before execute(). Sets up state and emits AGENT_STARTED."""
        self._started_at = datetime.utcnow()
        await self._writer.update_agent_state(
            self.audit_id,
            self.agent_type(),
            status=AgentStatus.RUNNING,
        )
        await self.emit_trace(
            TraceEventType.AGENT_STARTED,
            observation=f"{self.agent_type().value} agent initializing",
        )

    async def complete(self) -> None:
        """Seals the agent as successfully complete. Called at end of execute()."""
        await self._writer.update_agent_state(
            self.audit_id,
            self.agent_type(),
            status=AgentStatus.COMPLETE,
            current_tool=None,
            current_action_summary=None,
        )
        await self.emit_trace(
            TraceEventType.AGENT_COMPLETE,
            observation=f"Completed. {self._findings_written} findings written.",
        )

    async def fail(self, error: Exception, is_recoverable: bool = False) -> None:
        """
        Called by AgentRuntime when execute() raises an unhandled exception.
        Partial findings already written are preserved.
        """
        await self._writer.update_agent_state(
            self.audit_id,
            self.agent_type(),
            status=AgentStatus.FAILED,
            current_tool=None,
        )
        await self.emit_trace(
            TraceEventType.ERROR,
            observation=f"Agent failed: {type(error).__name__}: {str(error)[:200]}",
            error_code="agent_execution_error",
            is_recoverable=is_recoverable,
        )

    # ── Tool Execution ────────────────────────────────────────────────────────

    async def run_tool(
        self,
        tool_name: str,
        input_data: Any,
        *,
        action_summary: str,
        timeout_override_ms: Optional[int] = None,
        allow_partial: bool = False,
    ) -> ToolResult:
        """
        The ONLY way agents call tools. Never call IToolExecutor directly.

        Automatically emits TOOL_CALL before and TOOL_RESULT after.
        Updates agent_state.current_tool during execution.
        Returns ToolResult — agent must check .success before using .data.

        action_summary: human-readable description for the live trace panel.
                        E.g. "Checking 47 internal links for broken URLs"
        """
        await self._writer.update_agent_state(
            self.audit_id,
            self.agent_type(),
            current_tool=tool_name,
            current_action_summary=action_summary,
        )
        await self.emit_trace(
            TraceEventType.TOOL_CALL,
            tool_name=tool_name,
            tool_input_summary={"action": action_summary},
        )

        result = await self._tools.run(
            tool_name,
            input_data,
            timeout_override_ms=timeout_override_ms,
            allow_partial=allow_partial,
        )

        await self.emit_trace(
            TraceEventType.TOOL_RESULT,
            tool_name=tool_name,
            tool_output_summary=result.to_trace_summary(),
            tool_duration_ms=result.duration_ms,
            tool_succeeded=result.success,
            observation=(
                result.error.message
                if not result.success and result.error
                else None
            ),
        )
        await self._writer.update_agent_state(
            self.audit_id, self.agent_type(), current_tool=None
        )
        return result

    # ── Finding Creation ──────────────────────────────────────────────────────

    async def create_finding(self, **kwargs: Any) -> Finding:
        """
        Creates a Finding via FindingFactory and writes it to SharedState.
        Automatically injects audit_id and agent_type.
        Emits FINDING_WRITTEN trace event.
        Returns the created finding (with computed priority_score).
        """
        finding = self._factory.create(
            audit_id=self.audit_id,
            agent=self.agent_type(),
            **kwargs,
        )
        await self._writer.append_finding(self.audit_id, finding)
        await self._writer.update_agent_state(
            self.audit_id,
            self.agent_type(),
            increment_findings=True,
        )
        self._findings_written += 1
        await self.emit_trace(
            TraceEventType.FINDING_WRITTEN,
            finding_id=finding.id,
            finding_title=finding.title,
            finding_severity=finding.severity.value,
        )
        return finding

    # ── Trace Emission ────────────────────────────────────────────────────────

    async def emit_trace(
        self,
        event_type: TraceEventType,
        *,
        observation: Optional[str] = None,
        reasoning: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_input_summary: Optional[Dict[str, Any]] = None,
        tool_output_summary: Optional[Dict[str, Any]] = None,
        tool_duration_ms: Optional[int] = None,
        tool_succeeded: Optional[bool] = None,
        plan_update: Optional[Any] = None,
        finding_id: Optional[UUID] = None,
        finding_title: Optional[str] = None,
        finding_severity: Optional[str] = None,
        error_code: Optional[str] = None,
        is_recoverable: Optional[bool] = None,
    ) -> None:
        """Emits a trace event. Automatically injects audit_id and agent_type."""
        await self._trace.emit(
            audit_id=self.audit_id,
            agent=self.agent_type(),
            event_type=event_type,
            tool_name=tool_name,
            tool_input_summary=tool_input_summary,
            tool_output_summary=tool_output_summary,
            tool_duration_ms=tool_duration_ms,
            tool_succeeded=tool_succeeded,
            observation=observation,
            reasoning=reasoning,
            plan_update=plan_update,
            finding_id=finding_id,
            finding_title=finding_title,
            finding_severity=finding_severity,
            error_code=error_code,
            is_recoverable=is_recoverable,
        )

    # ── State Read Shortcuts ──────────────────────────────────────────────────

    async def get_site_profile(self) -> SiteProfile:
        """Reads SiteProfile from SharedState. Raises if not yet set (agent dispatched too early)."""
        profile = await self._reader.get_site_profile(self.audit_id)
        if profile is None:
            raise RuntimeError(f"SiteProfile not available — {self.agent_type()} dispatched before Recon completed")
        return profile

    async def get_audit_plan(self) -> AuditPlan:
        plan = await self._reader.get_audit_plan(self.audit_id)
        if plan is None:
            raise RuntimeError(f"AuditPlan not available — {self.agent_type()} dispatched before Planning completed")
        return plan

    async def get_recon_artifact(self, key: str) -> Optional[Any]:
        """Reads a recon artifact by key. Returns None if not available (not an error)."""
        return await self._reader.get_recon_data(self.audit_id, key)

    async def get_prior_findings(self, from_agent: AgentType) -> List[Finding]:
        """
        Reads findings already written by another agent.
        Used for cross-agent context injection.
        Returns empty list if agent hasn't completed (non-blocking).
        """
        return await self._reader.get_findings_by_agent(self.audit_id, from_agent)

    async def add_ai_warning(self, message: str) -> None:
        """
        Records a non-fatal AI warning (e.g. LLM unavailable, fell back to deterministic).
        Stored in SharedState.ai_warnings for the final audit summary.
        """
        if hasattr(self._writer, "add_warning"):
            await self._writer.add_warning(self.audit_id, message)
        await self.emit_trace(TraceEventType.OBSERVATION, observation=f"[AI WARNING] {message}")
