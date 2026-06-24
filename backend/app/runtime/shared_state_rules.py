"""
SharedState Interaction Rules
══════════════════════════════
This document defines the complete access control and mutation rules for SharedState.
Every agent, service, and component in the system must follow these rules.
Violations cause data corruption, race conditions, and invalid audit results.

The rules are enforced at runtime by SharedStateService (the implementation of
ISharedStateReader and ISharedStateWriter). Agents that attempt unauthorized
reads/writes receive AccessDeniedError, not silent failures.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMMUTABILITY RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Once written, the following state fields are IMMUTABLE.
No agent or service may overwrite them.
Attempts to overwrite raise ImmutableStateError.

  Field                      | Written by         | Immutable after
  ───────────────────────────┼────────────────────┼──────────────────────
  site_profile               | Orchestrator       | Written once, ever
  recon_artifact[playwright] | Orchestrator       | Written once, ever
  recon_artifact[screenshot] | Orchestrator       | Written once, ever
  recon_artifact[tech_stack] | Orchestrator       | Written once, ever
  recon_artifact[headers]    | Orchestrator       | Written once, ever
  recon_artifact[links]      | Orchestrator       | Written once, ever
  audit_plan                 | Orchestrator       | Replaceable only via PLAN_UPDATE
                             |                    | (a full replacement, not a patch)
  findings[agent][finding]   | Owning agent       | Immutable after appended
                             |                    | (append-only list)
  agent_states[agent].status | AgentRuntime only  | Agents cannot set their own COMPLETE/FAILED
                             |                    | without going through BaseAgent.complete()/fail()
  trace_events[*]            | TraceService       | Append-only, never modified

EXCEPTION — audit_plan PLAN_UPDATE:
  The Orchestrator may replace the AuditPlan mid-audit to adjust agent configs.
  This is NOT a mutation of existing config — it is a full replacement.
  The old plan is archived (stored as previous_plan in audit_traces).
  All PLAN_UPDATE events reference the old and new plan states.
  PLAN_UPDATE only affects agents that have NOT yet started.
  Running agents are not interrupted — they complete with their original config.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
READ ACCESS MATRIX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

                         │ ORCH │ SEO  │ PERF │ A11Y │ CONT │ TECH │ SYNTH
─────────────────────────┼──────┼──────┼──────┼──────┼──────┼──────┼───────
site_profile             │  R   │  R   │  R   │  R   │  R   │  R   │  R
audit_plan               │  R   │  R   │  R   │  R   │  R   │  R   │  R
recon[playwright_output] │  R   │  R*  │  R*  │  R*  │  R   │  –   │  –
recon[header_analysis]   │  R   │  –   │  R   │  –   │  –   │  R   │  –
recon[link_extraction]   │  R   │  R   │  –   │  –   │  –   │  R   │  –
recon[screenshot]        │  R   │  –   │  –   │  –   │  –   │  –   │  –
recon[tech_stack]        │  R   │  –   │  R*  │  –   │  –   │  –   │  –
findings[seo]            │  R   │  W   │  –   │  –   │  –   │  R†  │  R
findings[performance]    │  R   │  –   │  W   │  –   │  –   │  –   │  R
findings[accessibility]  │  R   │  –   │  –   │  W   │  –   │  –   │  R
findings[content]        │  R   │  –   │  –   │  –   │  W   │  –   │  R
findings[technical]      │  R   │  R†  │  –   │  –   │  –   │  W   │  R
findings[synthesis]      │  –   │  –   │  –   │  –   │  –   │  –   │  W
agent_states             │  R   │  R*  │  R*  │  R*  │  R*  │  R*  │  R
audit_status             │  W   │  –   │  –   │  –   │  –   │  –   │  W†

R  = Read allowed
W  = Write allowed (agent may only write its OWN key)
R* = Read allowed but rarely needed (for monitoring context)
R† = Conditional read: only reads if that agent has reached COMPLETE status
W† = SynthesisAgent may set audit_status to COMPLETE (final step only)
–  = No access

Access enforcement:
  ISharedStateReader.get_recon_data(key) checks: is this agent allowed to read this key?
  ISharedStateWriter.append_finding(agent, finding) checks: finding.agent == calling agent?
  Violations → AccessDeniedError (logged + bubbled up to AgentRuntime → agent marked FAILED)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WRITE ACCESS RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. FINDINGS — Append-only per agent:
   - Each agent appends to findings[its_agent_type]
   - No agent may write to another agent's findings key
   - SynthesisAgent appends to findings[synthesis]
   - Once a finding is appended, it cannot be modified or deleted
   - Implementation: Redis RPUSH to audit:{id}:findings:{agent}
                    + PostgreSQL INSERT (via batch flush)

2. AGENT STATE — Update-only own state:
   - Agents update agent_states[their AgentType] only
   - Only allowed state transitions:
       PENDING → RUNNING (by AgentRuntime, not agent itself)
       RUNNING → COMPLETE (via BaseAgent.complete())
       RUNNING → FAILED (via BaseAgent.fail())
   - current_tool and current_action_summary: agent can update freely while RUNNING
   - findings_written: incremented by SharedStateService, not by agent directly

3. RECON ARTIFACTS — Orchestrator-only writes:
   - Only OrchestratorAgent may call store_recon_artifact()
   - Specialist agents may only read via get_recon_artifact()
   - All recon artifacts are immutable once written (first-write-wins)

4. AUDIT STATUS — Lifecycle states:
   - PENDING → RECON: AgentRuntime (when OrchestratorAgent starts)
   - RECON → PLANNING: OrchestratorAgent (via transition_phase())
   - PLANNING → AUDITING: OrchestratorAgent
   - AUDITING → SYNTHESIZING: OrchestratorAgent (when all specialists terminal)
   - SYNTHESIZING → COMPLETE: SynthesisAgent
   - ANY → FAILED: AgentRuntime (on unrecoverable error)
   No specialist agent may change audit_status directly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CROSS-AGENT COMMUNICATION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Agents CANNOT call each other. All cross-agent communication is asynchronous
and indirect, flowing through SharedState.

PATTERN 1 — Context Injection (Orchestrator → Specialist):
  The Orchestrator writes the AuditPlan with agent-specific instructions.
  Specialist agents read their AgentConfig to understand what the Orchestrator wants.
  Example: Orchestrator detects Shopify (recon) → AuditPlan sets SEO agent priority_areas
           to ['structured_data'] with instruction "Prioritize Product and BreadcrumbList schemas"

PATTERN 2 — Findings-Based Context (Specialist → Specialist):
  Specialist agents may read other agents' findings from SharedState.
  Reads are non-blocking: if the target agent hasn't completed, read returns [].
  The reading agent proceeds without that context (graceful degradation).
  Example: TechnicalAgent reads SEO findings to detect canonical+redirect compound issues.
           If SEO hasn't finished, Technical proceeds without that cross-reference.

PATTERN 3 — Orchestrator Mid-Audit Injection:
  The Orchestrator's monitoring loop reads all findings as they're written.
  If a high-priority finding warrants changing the plan for another agent:
    - Orchestrator issues PLAN_UPDATE (replaces AuditPlan)
    - Only agents not yet RUNNING receive the updated config
    - Running agents are not interrupted
  Example: SEO agent writes CRITICAL finding "noindex on homepage" →
           Orchestrator emits PLAN_UPDATE → Performance and A11y agents are
           deprioritized (depth set to STANDARD) since noindex must be fixed first anyway

PATTERN 4 — Synthesis Cross-Reference (Synthesis → All):
  SynthesisAgent reads all findings and creates FindingRelationship objects.
  These relationships are written back to findings (appended to each finding's
  relationships list in DB — not in Redis, only after synthesis completes).
  The relationships are then visible in the final report's finding detail views.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONCURRENCY GUARANTEES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

All specialist agents run concurrently. The following operations are atomic:

  append_finding:      Redis RPUSH is atomic — no two agents can interleave
  update_agent_state:  Redis HSET is atomic per field
  sequence increment:  Redis INCR is atomic — sequence numbers never duplicate
  get_findings_by_agent: Redis LRANGE is non-blocking read — always safe

Non-atomic but safe:
  Reading recon artifacts: all reads happen after writes (Recon phase completes
  before any specialist is dispatched) — no concurrent read/write possible.

  Writing recon artifacts: Orchestrator writes all artifacts sequentially before
  dispatching specialists — no concurrent artifact writes.

Potential race condition (managed explicitly):
  Orchestrator monitoring loop reads all findings to decide on PLAN_UPDATE.
  If SEO writes a finding at the same time Orchestrator reads findings, the
  Orchestrator may miss that finding in this polling cycle.
  This is acceptable — the next polling cycle (5s later) will catch it.
  PLAN_UPDATEs are not time-critical.
"""
