"""
AgentRuntime — Parallel Specialist Agent Executor
══════════════════════════════════════════════════
Reads the AuditPlan from SharedState, instantiates all enabled specialist agents
with injected dependencies, runs them concurrently via asyncio.gather(), and then
runs SynthesisAgent after all specialists reach a terminal state.

Failure policy (matches lifecycle.py spec):
  - Individual specialist failure → agent marked FAILED, audit continues
  - SynthesisAgent failure        → marked FAILED, partial results preserved, no re-raise
  - Only OrchestratorAgent failure fails the whole audit (not managed here)

Usage:
    runtime = AgentRuntime(state, trace, factory, registry)
    await runtime.run_specialists(audit_id)
    await runtime.run_synthesis(audit_id)
"""

from __future__ import annotations

import asyncio
from typing import Dict, Type
from uuid import UUID

from typing import Optional

from app.llm.base import LLMClient
from app.models.enums import AgentType, AuditStatus
from app.runtime.base_agent import BaseAgent
from app.services.finding_factory import FindingFactoryImpl
from app.services.shared_state_service import SharedStateService
from app.services.tool_executor import ToolExecutorImpl
from app.services.trace_service import TraceServiceImpl
from app.tools.registry import ToolRegistry


def _build_agent_map() -> Dict[AgentType, Type[BaseAgent]]:
    from app.agents.seo_agent import SEOAgent
    from app.agents.performance_agent import PerformanceAgent
    from app.agents.accessibility_agent import AccessibilityAgent
    from app.agents.content_agent import ContentAgent
    from app.agents.technical_agent import TechnicalAgent
    from app.agents.synthesis_agent import SynthesisAgent

    return {
        AgentType.SEO: SEOAgent,
        AgentType.PERFORMANCE: PerformanceAgent,
        AgentType.ACCESSIBILITY: AccessibilityAgent,
        AgentType.CONTENT: ContentAgent,
        AgentType.TECHNICAL: TechnicalAgent,
        AgentType.SYNTHESIS: SynthesisAgent,
    }


class AgentRuntime:
    """
    Owns the specialist agent lifecycle for a single audit session.

    The runtime is intentionally thin — all persistent state lives in
    SharedStateService. This means the runtime can be created and discarded
    without data loss, and the same SharedState can be inspected externally
    while agents are running.

    Thread / concurrency safety:
      Each agent gets its own BaseAgent instance. The shared ToolExecutorImpl
      is safe to share (its cache is protected by per-key asyncio.Locks added
      on first access). SharedStateService writes are protected by per-audit
      asyncio.Locks internally.
    """

    def __init__(
        self,
        state: SharedStateService,
        trace: TraceServiceImpl,
        factory: FindingFactoryImpl,
        registry: ToolRegistry,
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        self._state = state
        self._trace = trace
        self._factory = factory
        self._llm = llm_client
        # One shared executor — its in-memory cache benefits all parallel agents.
        self._executor = ToolExecutorImpl(registry)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_specialists(self, audit_id: UUID) -> None:
        """
        Dispatches all enabled specialist agents (excluding Synthesis) in parallel.
        Waits for all to reach a terminal state before returning.
        Individual agent failures do NOT propagate — they are marked FAILED in
        SharedState and execution continues.
        """
        plan = await self._state.get_audit_plan(audit_id)
        if plan is None:
            raise RuntimeError(
                f"AgentRuntime.run_specialists: AuditPlan not found for audit {audit_id}. "
                "Ensure SharedState is populated before calling run_specialists()."
            )

        specialist_types = [
            agent_type
            for agent_type in plan.parallel_agents
            if agent_type not in (AgentType.SYNTHESIS, AgentType.ORCHESTRATOR)
        ]

        await asyncio.gather(
            *[self._run_agent(agent_type, audit_id) for agent_type in specialist_types],
        )

    async def run_synthesis(self, audit_id: UUID) -> None:
        """
        Transitions audit status to SYNTHESIZING, then runs SynthesisAgent.
        Call only after run_specialists() has returned.
        """
        await self._state.transition_status(audit_id, AuditStatus.SYNTHESIZING)
        await self._run_agent(AgentType.SYNTHESIS, audit_id)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_agent(self, agent_type: AgentType, audit_id: UUID) -> None:
        """
        Instantiates one agent, calls initialize() then execute().
        Catches any unhandled exception from execute() and calls agent.fail().
        Never re-raises — the caller (gather or run_synthesis) always continues.
        """
        agent_map = _build_agent_map()
        agent_cls = agent_map.get(agent_type)
        if agent_cls is None:
            return

        agent: BaseAgent = agent_cls(
            audit_id=audit_id,
            tool_executor=self._executor,
            trace_service=self._trace,
            state_reader=self._state,
            state_writer=self._state,
            finding_factory=self._factory,
            llm_client=self._llm,
        )

        try:
            await agent.initialize()
            await agent.execute()
        except Exception as exc:
            try:
                await agent.fail(exc, is_recoverable=False)
            except Exception:
                pass
