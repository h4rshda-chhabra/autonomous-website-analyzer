from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.models import (
    AgentStatus,
    AgentType,
    AuditPlan,
    AuditStatus,
    Finding,
    SiteProfile,
)
from app.models.shared_state import AgentStateEntry, SharedState
from app.runtime.base_agent import ISharedStateReader, ISharedStateWriter

# Sentinel: distinguishes "don't touch this field" from "set to None"
_KEEP = object()


class SharedStateService(ISharedStateReader, ISharedStateWriter):
    """
    In-memory implementation of the shared state store.

    Phase 0: all state lives in Python dicts, protected by per-audit asyncio locks.
    Phase 1: replace _states with Redis HSET/GET and _recon_artifacts with separate Redis keys.

    One instance is created per application lifetime and shared across all audits.
    The audit_id key partitions all state, so concurrent audits do not interfere.
    """

    def __init__(self) -> None:
        self._states: Dict[UUID, SharedState] = {}
        self._recon_artifacts: Dict[UUID, Dict[str, Any]] = {}
        self._locks: Dict[UUID, asyncio.Lock] = {}

    # ─── Session Lifecycle ────────────────────────────────────────────────────

    async def create_session(self, audit_id: UUID, url: str) -> SharedState:
        state = SharedState(audit_id=audit_id, url=url)
        for agent in AgentType:
            state.agent_states[agent] = AgentStateEntry(agent=agent)
            state.findings[agent] = []
        self._states[audit_id] = state
        self._recon_artifacts[audit_id] = {}
        self._locks[audit_id] = asyncio.Lock()
        return state

    async def get_state(self, audit_id: UUID) -> Optional[SharedState]:
        return self._states.get(audit_id)

    async def transition_status(self, audit_id: UUID, new_status: AuditStatus) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            state.status = new_status
            now = datetime.utcnow()
            if new_status == AuditStatus.PLANNING:
                state.recon_completed_at = now
            elif new_status == AuditStatus.AUDITING:
                state.planning_completed_at = now
                state.auditing_started_at = now
            elif new_status == AuditStatus.SYNTHESIZING:
                state.synthesis_started_at = now
            elif new_status in (AuditStatus.COMPLETE, AuditStatus.COMPLETE_WITH_WARNINGS):
                state.completed_at = now
            elif new_status == AuditStatus.FAILED:
                state.failed_at = now

    async def add_warning(self, audit_id: UUID, message: str) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            state.ai_warnings.append(message)

    async def set_synthesis_insights(self, audit_id: UUID, insights: dict) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            state.synthesis_insights = insights

    async def set_failure_reason(self, audit_id: UUID, reason: str) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            state.failure_reason = reason

    # ─── ISharedStateReader ───────────────────────────────────────────────────

    async def get_site_profile(self, audit_id: UUID) -> Optional[SiteProfile]:
        state = self._states.get(audit_id)
        return state.site_profile if state else None

    async def get_audit_plan(self, audit_id: UUID) -> Optional[AuditPlan]:
        state = self._states.get(audit_id)
        return state.audit_plan if state else None

    async def get_findings_by_agent(
        self, audit_id: UUID, agent: AgentType
    ) -> List[Finding]:
        state = self._states.get(audit_id)
        if state is None:
            return []
        return list(state.findings.get(agent, []))

    async def get_all_findings(self, audit_id: UUID) -> List[Finding]:
        state = self._states.get(audit_id)
        if state is None:
            return []
        return state.get_all_findings()

    async def get_recon_data(self, audit_id: UUID, key: str) -> Optional[Any]:
        artifacts = self._recon_artifacts.get(audit_id, {})
        return artifacts.get(key)

    async def is_agent_complete(self, audit_id: UUID, agent: AgentType) -> bool:
        state = self._states.get(audit_id)
        if state is None:
            return False
        return state.is_agent_complete(agent)

    async def are_all_specialist_agents_terminal(self, audit_id: UUID) -> bool:
        state = self._states.get(audit_id)
        if state is None:
            return False
        return state.are_all_specialist_agents_terminal()

    # ─── ISharedStateWriter ───────────────────────────────────────────────────

    async def append_finding(self, audit_id: UUID, finding: Finding) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            bucket = state.findings.setdefault(finding.agent, [])
            bucket.append(finding)

    async def update_agent_state(
        self,
        audit_id: UUID,
        agent: AgentType,
        *,
        status: Optional[AgentStatus] = None,
        current_tool: Optional[str] = None,
        current_action_summary: Optional[str] = None,
        increment_findings: bool = False,
    ) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            entry = state.agent_states.setdefault(agent, AgentStateEntry(agent=agent))

            if status is not None:
                entry.status = status
                now = datetime.utcnow()
                if status == AgentStatus.RUNNING and entry.started_at is None:
                    entry.started_at = now
                elif status in (AgentStatus.COMPLETE, AgentStatus.FAILED, AgentStatus.SKIPPED):
                    if entry.completed_at is None:
                        entry.completed_at = now

            # None means "clear" for both tool fields
            entry.current_tool = current_tool
            entry.current_action_summary = current_action_summary

            if increment_findings:
                entry.findings_written += 1

    async def store_recon_artifact(self, audit_id: UUID, key: str, data: Any) -> None:
        async with self._lock(audit_id):
            artifacts = self._recon_artifacts.setdefault(audit_id, {})
            if key in artifacts:
                raise RuntimeError(
                    f"Recon artifact '{key}' for audit {audit_id} is immutable once written"
                )
            artifacts[key] = data

    async def set_site_profile(self, audit_id: UUID, profile: SiteProfile) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            if state.site_profile is not None:
                raise RuntimeError(
                    f"SiteProfile for audit {audit_id} is immutable once set"
                )
            state.site_profile = profile

    async def set_audit_plan(self, audit_id: UUID, plan: AuditPlan) -> None:
        async with self._lock(audit_id):
            state = self._get_required(audit_id)
            state.audit_plan = plan  # replacement is allowed for PLAN_UPDATE

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _lock(self, audit_id: UUID) -> asyncio.Lock:
        if audit_id not in self._locks:
            self._locks[audit_id] = asyncio.Lock()
        return self._locks[audit_id]

    def _get_required(self, audit_id: UUID) -> SharedState:
        state = self._states.get(audit_id)
        if state is None:
            raise KeyError(f"No active session for audit_id={audit_id}")
        return state
