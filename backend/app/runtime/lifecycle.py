"""
Full Audit Execution Lifecycle
═══════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 1: Top-Level System Flow
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Frontend                 FastAPI             Redis/Celery       Agent Runtime
  ────────                 ───────             ────────────       ─────────────
     │                        │                     │                   │
     │  POST /audits {url}    │                     │                   │
     │ ──────────────────────▶│                     │                   │
     │                        │  Create audit row   │                   │
     │                        │  status=PENDING     │                   │
     │                        │  ─────────────────▶ │                   │
     │  {audit_id, status}    │                     │                   │
     │ ◀──────────────────────│                     │                   │
     │                        │  Enqueue job        │                   │
     │                        │ ────────────────────▶                   │
     │                        │                     │                   │
     │  GET /stream (SSE)     │                     │   Celery worker   │
     │ ──────────────────────▶│                     │   picks up job    │
     │  [connection open]     │                     │ ──────────────────▶
     │                        │                     │                   │
     │                        │                     │         AgentRuntime.run(audit_id)
     │                        │                     │                   │
     │  event: status         │  Subscribe Redis    │    Status: RECON  │
     │  progress: 0%  ◀───────────────────────────────────────────────  │
     │                        │                     │                   │

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 2: Audit State Machine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

                        ┌─────────┐
                        │ PENDING │
                        └────┬────┘
                             │ AgentRuntime.run() called
                             ▼
                        ┌─────────┐
                        │  RECON  │◀─────────────────────────────────┐
                        └────┬────┘                                   │
                             │ SiteProfile written                    │
                             │ PlaywrightCrawler succeeds             │
                             ▼                                        │
                        ┌──────────┐                                  │
                        │ PLANNING │                       ┌──────────┴──────┐
                        └────┬─────┘                       │     FAILED      │
                             │ AuditPlan written           │  (terminal)     │
                             ▼                             └─────────────────┘
                        ┌──────────┐                            ▲    ▲    ▲
                        │ AUDITING │────────────────────────────┘    │    │
                        └────┬─────┘  PlaywrightCrawler 4xx/5xx      │    │
                             │                              unrecov.  │    │
                             │ all specialist agents terminal         │    │
                             ▼                                        │    │
                        ┌─────────────┐                              │    │
                        │SYNTHESIZING │──────────────────────────────┘    │
                        └──────┬──────┘  synthesis total failure           │
                               │                                           │
                               │ PriorityRoadmap written                   │
                               ▼                                           │
                        ┌──────────┐                                       │
                        │ COMPLETE │                                       │
                        │(terminal)│──────────────────────────────────────┘
                        └──────────┘  (impossible — COMPLETE cannot fail)

Failure transitions:
  Any state → FAILED when:
    - PlaywrightCrawler returns 4xx/5xx (RECON phase)
    - claude_classification fails after retry (RECON phase)
    - AgentRuntime catches unhandled exception from OrchestratorAgent
  Note: individual specialist agent FAILED status does NOT fail the audit.
        Only Orchestrator or SynthesisAgent failure fails the whole audit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 3: Recon Phase (Sequential)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  OrchestratorAgent            ToolExecutor           SharedState          Redis (SSE)
  ─────────────────            ────────────           ───────────          ───────────
         │                          │                      │                    │
         │ run_tool(PlaywrightCrawler, url)                │                    │
         │ ────────────────────────▶│                      │                    │
         │                          │ execute with 30s TO  │                    │
         │                          │ [30s max]            │                    │
         │◀─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│                      │                    │
         │ ToolResult(success, data)│                      │                    │
         │                          │                      │                    │
         │ emit_trace(TOOL_RESULT)  │                      │                    │
         │ ─────────────────────────────────────────────────────────────────── ▶
         │                          │                      │   SSE: trace event │
         │                          │                      │                    │
         │ store_recon_artifact('playwright_output', data) │                    │
         │ ────────────────────────────────────────────── ▶│                    │
         │                          │                      │                    │
         │ emit_trace(OBSERVATION)  │                      │                    │
         │ ─────────────────────────────────────────────────────────────────── ▶
         │                          │                      │                    │
         │ [Parallel: ScreenshotCapture]                   │                    │
         │ [Sequential: TechStackDetector → HeaderAnalyzer → LinkExtractor]     │
         │                                                 │                    │
         │ [all artifacts stored]                          │                    │
         │ claude_site_classification(html, screenshot)    │                    │
         │ ────────────────────────▶│                      │                    │
         │ [AI call: 5-15s]         │                      │                    │
         │◀─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│                      │                    │
         │                          │                      │                    │
         │ set_site_profile(SiteProfile)                   │                    │
         │ ────────────────────────────────────────────── ▶│                    │
         │                          │                      │                    │
         │ emit_trace(REASONING: classification rationale) │                    │
         │ ─────────────────────────────────────────────────────────────────── ▶
         │                          │                      │                    │
         │ claude_audit_planner(site_profile)              │                    │
         │ [AI call: 5-10s]         │                      │                    │
         │                          │                      │                    │
         │ set_audit_plan(AuditPlan)│                      │                    │
         │ ────────────────────────────────────────────── ▶│                    │
         │                          │                      │                    │
         │ emit_trace(PLAN_UPDATE)  │                      │                    │
         │ ─────────────────────────────────────────────────────────────────── ▶
         │                          │                      │                    │

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 4: Parallel Specialist Agent Execution
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  AgentRuntime    SEOAgent   PerfAgent   A11yAgent  ContentAgent  TechnicalAgent
  ────────────    ────────   ─────────   ─────────  ────────────  ──────────────
       │               │          │           │           │               │
       │ asyncio.gather([...])    │           │           │               │
       │──────────────▶│          │           │           │               │
       │─────────────────────────▶│           │           │               │
       │──────────────────────────────────────▶           │               │
       │───────────────────────────────────────────────── ▶               │
       │──────────────────────────────────────────────────────────────────▶
       │               │          │           │           │               │
       │         [All run concurrently — independently — no blocking]     │
       │               │          │           │           │               │
       │               │ MetaTag  │ Lighthouse│  axe-core │ ContentExtract│
       │               │ Analyzer │ Runner    │  Scanner  │               │ SecurityHeader
       │               │ [2s]     │ [60-120s] │ [30-45s]  │ [5s]         │ Analyzer [1s]
       │               │          │           │           │               │
       │               │          │           │           │ ClaudeContent │
       │               │          │           │           │ Analyzer [30s]│ BrokenLink
       │               │          │           │           │               │ Checker [60s]
       │               │ StructuredData       │           │               │
       │               │ Analyzer [3s]        │           │               │
       │               │          │           │           │               │
       │               │ Internal │ AssetAna- │ Contrast  │               │
       │               │ LinkAnaly│ lyzer[30s]│ Checker   │               │
       │               │ zer [2s] │           │ [30s]     │               │
       │               │          │           │           │               │
  t=0s ├───────────────┼──────────┼───────────┼───────────┼───────────────┤
  t=15s│               │ COMPLETE │           │           │               │
  t=45s│               │          │           │ COMPLETE  │ COMPLETE      │
  t=60s│               │          │           │           │               │ COMPLETE
  t=90s│               │          │ COMPLETE  │           │               │
       │               │          │           │           │               │
       │ [all_terminal=True]      │           │           │               │
       │ dispatch SynthesisAgent  │           │           │               │

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 5: Single Agent Execution Loop (Reasoning Cycle)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Agent (e.g. SEOAgent)
  ─────────────────────
  execute():
      │
      ├── PLAN ──────────────────────────────────────────────────────────────────────
      │   read_state(site_profile)          "What kind of site is this?"
      │   read_state(audit_plan)            "What should I focus on?"
      │   read_recon_artifact(playwright)   "What HTML do I work with?"
      │
      ├── ACT (Tool 1) ───────────────────────────────────────────────────────────────
      │   emit(TOOL_CALL, tool='MetaTagAnalyzer')
      │   result = run_tool('MetaTagAnalyzer', input)
      │   emit(TOOL_RESULT, tool='MetaTagAnalyzer', succeeded=True)
      │
      ├── OBSERVE ──────────────────────────────────────────────────────────────────
      │   emit(OBSERVATION, "Title: 'Acme SaaS - Grow your business'. 52 chars. Within range.")
      │   emit(OBSERVATION, "Meta description: missing. robots: indexable. canonical: self-ref.")
      │
      ├── REASON ───────────────────────────────────────────────────────────────────
      │   emit(REASONING, "Missing meta description is a MEDIUM SEO issue for a SaaS site
      │                    targeting organic traffic. Will create finding.")
      │
      ├── WRITE (Finding 1) ────────────────────────────────────────────────────────
      │   finding = create_finding(category=META_TAGS, severity=MEDIUM, ...)
      │   emit(FINDING_WRITTEN, finding_id=..., title='Meta description missing', severity='medium')
      │
      ├── ACT (Tool 2) ─ [adaptive: triggered by MetaTagAnalyzer observation] ──────
      │   emit(REASONING, "Site is ecommerce (from site_profile). Checking for Product schema.")
      │   emit(TOOL_CALL, tool='StructuredDataAnalyzer')
      │   result = run_tool('StructuredDataAnalyzer', input)
      │   emit(TOOL_RESULT, ...)
      │
      ├── OBSERVE ──────────────────────────────────────────────────────────────────
      │   emit(OBSERVATION, "No Product schema found. No BreadcrumbList. Only Organization schema.")
      │
      ├── REASON ───────────────────────────────────────────────────────────────────
      │   emit(REASONING, "Missing Product schema on ecommerce site = lost rich result eligibility.
      │                    Google shows star ratings and prices for sites with Product schema.
      │                    This is HIGH severity for conversion.")
      │
      ├── WRITE (Finding 2) ────────────────────────────────────────────────────────
      │   finding = create_finding(category=STRUCTURED_DATA, severity=HIGH, ...)
      │   emit(FINDING_WRITTEN, ...)
      │
      ├── [Continue for remaining tools: InternalLinkAnalyzer...]
      │
      ├── CROSS-AGENT READ (optional, non-blocking) ──────────────────────────────
      │   tech_findings = get_prior_findings(AgentType.TECHNICAL)  # [] if not done yet
      │   [if findings exist] check for canonical mismatch context
      │
      └── complete()
          emit(AGENT_COMPLETE, observation="5 findings written.")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 6: SSE Streaming Flow (Live Trace Panel)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Agent          TraceService      Redis Pub/Sub      FastAPI SSE       Frontend
  ─────          ────────────      ─────────────      ───────────       ────────
    │                  │                  │                 │                │
    │ emit(event)      │                  │                 │                │
    │ ────────────────▶│                  │                 │                │
    │                  │ INCR seq counter │                 │                │
    │                  │ ─────────────────▶               │                │
    │                  │◀ seq=42          │                 │                │
    │                  │                  │                 │                │
    │                  │ PUBLISH audit:id:events {event}   │                │
    │                  │ ─────────────────▶               │                │
    │                  │                  │ message arrives │                │
    │                  │                  │ ──────────────▶ │                │
    │                  │                  │                 │ yield SSE data │
    │                  │                  │                 │ ──────────────▶│
    │                  │                  │                 │                │ update trace panel
    │                  │ [buffer > 10 events OR 2s elapsed]│                │
    │                  │ flush_to_db()    │                 │                │
    │                  │ [batch INSERT to PostgreSQL]       │                │

  Client reconnect scenario:
  Frontend detects dropped SSE connection.
  Reconnects to GET /stream?last_sequence=38
  FastAPI SSE:
    1. SELECT * FROM agent_traces WHERE audit_id=X AND sequence > 38 ORDER BY sequence
    2. Yield replayed events (39, 40, 41, 42) as SSE
    3. Subscribe to Redis pub/sub for live events (sequence ≥ 43)
    No events are lost.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIAGRAM 7: Synthesis Phase
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  SynthesisAgent         SharedState          Claude API          PostgreSQL
  ──────────────         ───────────          ──────────          ──────────
        │                     │                    │                   │
        │ get all findings     │                    │                   │
        │ ────────────────────▶│                    │                   │
        │◀ {seo:[], perf:[], a11y:[], cont:[], tech:[]}                │
        │                     │                    │                   │
        │ [cross-reference pass: detect relationships]                 │
        │ [sort by priority_score]                  │                   │
        │ [detect compound issues]                  │                   │
        │                     │                    │                   │
        │ claude_synthesis(all_findings, site_profile)                 │
        │ ──────────────────────────────────────── ▶│                   │
        │ [AI: 10-20s]        │                    │                   │
        │◀ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│                   │
        │ {roadmap_items, cross_insights, summary}  │                   │
        │                     │                    │                   │
        │ [build PriorityRoadmap]                   │                   │
        │ [compute AuditScoreSummary]               │                   │
        │                     │                    │                   │
        │ write PriorityRoadmap ──────────────────────────────────────▶│
        │ write FindingRelationships ──────────────────────────────── ▶│
        │ flush all trace events ─────────────────────────────────── ▶│
        │                     │                    │                   │
        │ complete()          │                    │                   │
        │ set audit_status=COMPLETE ──────────────▶│                   │
        │                     │                    │                   │
        │ emit(AGENT_COMPLETE)│                    │                   │
        │ emit SSE 'complete' event ─────────────────────────────────▶ Frontend

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ESTIMATED TIMING (for a standard-depth audit)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Phase                    Duration    Cumulative
  ─────                    ────────    ──────────
  Recon (5 tools parallel) 30–45s      0:30–0:45
  Classification (Claude)  5–10s       0:40–0:55
  Planning (Claude)        5–10s       0:45–1:05
  Auditing (parallel):
    SEO Agent              15–25s
    Performance Agent      90–120s     ← critical path
    Accessibility Agent    60–90s
    Content Agent          40–60s
    Technical Agent        60–90s
    [limited by slowest = Performance]  1:45–3:05 total
  Synthesis (Claude)       15–30s      2:00–3:35
  Report write             2–5s        2:02–3:40
  ────────────────────────────────────────────────
  Total (typical)          ~2:30       (2.5 minutes)
  Total (worst case)       ~5:00       (5 minutes, large site)
  Total (best case)        ~1:45       (fast site, Lighthouse quick)

  Lighthouse is the critical path. If Lighthouse is unavailable or times out,
  total audit time drops to ~90 seconds (other agents are much faster).
"""
