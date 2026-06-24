"""
Agent Behavioral Specifications
═════════════════════════════════
This file documents the execution behavior, reasoning loop, and interaction
pattern for all 7 agents. This is the authoritative design reference —
implementations must match these specifications.

Each agent section covers:
  - Responsibilities
  - Tools available (from ToolRegistry)
  - Execution flow (numbered steps)
  - Reasoning loop narrative
  - Findings generated (types and conditions)
  - Trace events emitted (ordered)
  - Completion criteria
  - Failure recovery behavior

SharedState read/write access is governed by the rules in shared_state_rules.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 1: OrchestratorAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  The Orchestrator is the only agent that runs before specialist agents.
  It has three distinct phases within its execute():
    Phase A — Reconnaissance: collect raw site data
    Phase B — Planning: classify site + generate tailored AuditPlan
    Phase C — Coordination: dispatch agents, monitor, inject context, trigger synthesis

TOOLS AVAILABLE:
  PlaywrightCrawler, ScreenshotCapture, TechStackDetector,
  HeaderAnalyzer, LinkExtractor
  (Plus two AI tools called directly via Anthropic SDK — not through ToolRegistry:
   claude_site_classification, claude_audit_planner)

EXECUTION FLOW:

  Phase A — Reconnaissance (sequential, each step uses prior output):
    1. PlaywrightCrawler(url)
         → Produces static_html, rendered_html, headers, timings, network_requests
         → Store result in SharedState as recon artifact 'playwright_output'
         → Emit OBSERVATION: "Page loaded. Static: N words. Rendered: M words."
         → If rendered >> static: REASONING "CSR detected — audit must use rendered HTML"
         → CRITICAL: if status_code=4xx/5xx → emit ERROR, transition audit to FAILED

    2. ScreenshotCapture(url)
         → Store screenshot_path in SharedState
         → Runs concurrently with steps 3-5

    3. TechStackDetector(html, headers, scripts)
         → Detect framework, CMS, CDN, analytics
         → Emit OBSERVATION with detected technologies
         → Emit REASONING about what this implies for the audit

    4. HeaderAnalyzer(headers, url)
         → Store in SharedState as 'header_analysis' (Technical Agent reads this later)
         → Emit OBSERVATION: security score, caching score

    5. LinkExtractor(rendered_html, base_url)
         → Store in SharedState as 'link_extraction' (SEO + Technical read this)
         → Emit OBSERVATION: "Found N internal, M external links"

  Phase B — Planning (AI-powered, sequential):
    6. claude_site_classification(html, headers, screenshot, detected_tech)
         → Returns: category, rendering_strategy, primary_goals, confidence, reasoning
         → Construct SiteProfile from all recon data + classification
         → Write SiteProfile to SharedState (immutable from this point)
         → Emit OBSERVATION: "Classified as [category] with [confidence] confidence"
         → Emit REASONING: classification rationale

    7. claude_audit_planner(site_profile, recon_signals)
         → Returns: AuditPlan with per-agent configs, priorities, cross-dependencies
         → Write AuditPlan to SharedState
         → Emit PLAN_UPDATE: "Audit plan generated. [N] agents enabled. Deep focus on [domains]."
         → Emit REASONING: why agents are configured this way

  Phase C — Coordination:
    8. Transition audit status → AUDITING
    9. Dispatch all enabled specialist agents in parallel (asyncio.gather)
       Each agent receives: audit_id + dependencies (injected by AgentRuntime)
   10. Monitor loop (poll SharedState every 5s):
         → Check are_all_specialist_agents_terminal()
         → If new high-priority finding appears from any agent:
             read AuditPlan, check if other agents should be notified
             If yes: emit PLAN_UPDATE with what changed and why
             (e.g. SEO agent finds noindex → Performance agent gets deprioritized)
         → Continue until all agents terminal
   11. Trigger SynthesisAgent (dispatch as final agent)
   12. Wait for SynthesisAgent completion
   13. Transition audit status → COMPLETE

REASONING LOOP:
  The Orchestrator's reasoning is most active during Phase B.
  claude_site_classification receives: the page HTML (truncated), the screenshot,
  and the detected tech stack. It reasons about: "What kind of site is this?
  What does the owner want users to do? Where are the likely failure points?"
  claude_audit_planner receives the SiteProfile and reasons about: "Given this is
  a SaaS site with CSR rendering and lead generation as primary goal, which agents
  need to go deep? Performance matters most for conversion. SEO matters for acquisition.
  Accessibility matters for compliance. Let me configure each agent accordingly."

FINDINGS GENERATED:
  The Orchestrator does NOT write findings directly.
  The only exception: if PlaywrightCrawler returns status_code 4xx/5xx, the Orchestrator
  writes a single CRITICAL finding (category=TECHNICAL, title='Target URL is not reachable')
  and immediately transitions the audit to FAILED.

TRACE EVENTS EMITTED:
  AGENT_STARTED → (×5) TOOL_CALL → TOOL_RESULT → OBSERVATION × (for each tool)
  REASONING (after classification)
  PLAN_UPDATE (after planning)
  REASONING (after each monitoring cycle that produces insight)
  Optional PLAN_UPDATE (if mid-audit context injection occurs)
  AGENT_COMPLETE

COMPLETION CRITERIA:
  Orchestrator is "complete" when SynthesisAgent reaches COMPLETE status.
  It is the last agent to complete.

FAILURE RECOVERY:
  - PlaywrightCrawler hard failure → CRITICAL finding + audit FAILED
  - ScreenshotCapture failure → Skip screenshot (recon continues without it)
  - TechStackDetector failure → Use empty TechStack (all fields Unknown)
  - HeaderAnalyzer failure → Technical Agent will re-fetch headers directly
  - claude_classification failure → Default to category=OTHER, minimal plan
  - claude_planner failure → Use standard plan (all agents at STANDARD depth)
  - Specialist agent failure → Log, continue with remaining agents, mark failed agent
  - SynthesisAgent failure → Best-effort roadmap from partially synthesized data


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 2: SEOAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  Analyzes all signals that affect search engine indexability, crawlability,
  ranking potential, and rich result eligibility.

TOOLS AVAILABLE:
  MetaTagAnalyzer, StructuredDataAnalyzer, InternalLinkAnalyzer
  Reads from SharedState: playwright_output, link_extraction, audit_plan
  Reads prior findings from: TechnicalAgent (canonical + redirect context)

EXECUTION FLOW:
  1. Read SiteProfile and AuditPlan.agent_configs[SEO] from SharedState
     → Note: priority_areas, depth, special_instructions
  2. Read playwright_output (static_html) from SharedState recon artifacts
     → SEO tools use static_html (what search engines see)
  3. Run MetaTagAnalyzer(static_html, url, site_category, primary_goal)
     → IMMEDIATE check: if robots.is_indexable == False → CRITICAL finding + REASONING
       "noindex detected — this is the most severe possible SEO finding"
     → Emit OBSERVATION per analysis area (title, description, OG, robots)
  4. Evaluate MetaTagAnalyzer results → generate findings per issue
  5. Run StructuredDataAnalyzer(static_html, url, site_category)
     → Emit OBSERVATION: "Found N schemas. M are valid. K are Google rich-result eligible."
     → If expected schemas are missing: REASONING about business impact
  6. Read link_extraction from SharedState (no re-crawl)
  7. Run InternalLinkAnalyzer(internal_links, current_url, base_domain)
     → Emit OBSERVATION: anchor text quality, heading structure findings
  8. Attempt to read Technical findings from SharedState:
     → If TechnicalAgent complete: read findings, look for canonical/redirect findings
     → Cross-reference: canonical mismatch + redirect chain = compound issue
     → Note compound issue for Synthesis Agent (don't create finding — Synthesis does)
  9. Emit REASONING summary: overall SEO health assessment
 10. Complete

FINDINGS GENERATED (condition → finding):
  robots.is_indexable=False            → CRITICAL, FindingCategory.CRAWLABILITY
  title missing                        → HIGH, FindingCategory.META_TAGS
  title outside 30-60 chars           → MEDIUM, META_TAGS
  meta_description missing             → MEDIUM, META_TAGS
  og tags incomplete (content site)   → MEDIUM, META_TAGS
  schema missing (category-specific)  → MEDIUM/HIGH, STRUCTURED_DATA
  invalid JSON-LD                      → MEDIUM, STRUCTURED_DATA
  h1 count ≠ 1                        → HIGH, CONTENT_SIGNALS
  heading hierarchy invalid            → MEDIUM, CONTENT_SIGNALS
  generic anchor text > 20% of links  → LOW, INTERNAL_LINKING
  nofollow on internal links           → MEDIUM, INTERNAL_LINKING

TRACE EVENTS:
  AGENT_STARTED
  TOOL_CALL: MetaTagAnalyzer
  TOOL_RESULT: MetaTagAnalyzer
  OBSERVATION: meta tag summary
  [If noindex] REASONING: "noindex is a critical finding — reporting immediately"
  [If noindex] FINDING_WRITTEN: critical
  TOOL_CALL: StructuredDataAnalyzer
  TOOL_RESULT: StructuredDataAnalyzer
  OBSERVATION: schema summary
  [N × FINDING_WRITTEN for each issue found]
  TOOL_CALL: InternalLinkAnalyzer (via SharedState read, no network)
  TOOL_RESULT: InternalLinkAnalyzer
  OBSERVATION: link structure summary
  [N × FINDING_WRITTEN]
  REASONING: "Overall SEO assessment: [summary]"
  AGENT_COMPLETE

COMPLETION CRITERIA:
  All three tools have run (or gracefully failed).
  All findings from successful tool outputs have been written.

FAILURE RECOVERY:
  MetaTagAnalyzer failure   → Skip meta tag findings, continue with structured data
  StructuredDataAnalyzer failure → Skip schema findings, continue
  InternalLinkAnalyzer failure   → Skip internal link findings
  If all three fail → Write single LOW finding: "SEO analysis tools unavailable"
  Always reach AGENT_COMPLETE (never leaves audit in limbo)


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 3: PerformanceAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  Identifies performance bottlenecks using both Lighthouse metrics
  and granular asset-level analysis. Cross-references with caching headers.

TOOLS AVAILABLE:
  LighthouseRunner, AssetAnalyzer
  Reads from SharedState: site_profile (rendering_strategy), header_analysis,
                          playwright_output (network_requests for AssetAnalyzer)

EXECUTION FLOW:
  1. Read SiteProfile.rendering_strategy
     → If CSR: set LighthouseRunner timeout to 150s (JS parsing takes longer)
     → Emit REASONING: "CSR site detected — JS bundle analysis is a priority"
  2. Read header_analysis from SharedState (Orchestrator stored this during Recon)
     → Note caching scores for later cross-reference
  3. Run LighthouseRunner(url, form_factor='mobile', runs=2)
     → This is the longest-running tool (~60-120s) — emit OBSERVATION after each phase
     → On success: emit OBSERVATION with CWV scores
     → Generate findings for each metric outside the 'good' band
  4. Run AssetAnalyzer(html, base_url, network_requests)
     → network_requests comes from playwright_output (SharedState read)
     → Emit OBSERVATION: page weight, image format issues, render-blocking resources
  5. Cross-reference:
     → If caching score < 60 AND LighthouseRunner.opportunities includes 'cache': compound
     → If AssetAnalyzer.total_js_kb > 500 AND TBT > 300ms: note for Synthesis
  6. Emit REASONING: overall performance assessment with business framing
  7. Complete

FINDINGS GENERATED:
  LCP in 'poor' band (>4s)                → CRITICAL, CORE_WEB_VITALS
  LCP in 'needs-improvement' (2.5-4s)     → HIGH, CORE_WEB_VITALS
  CLS > 0.25                              → HIGH, CORE_WEB_VITALS
  CLS 0.1-0.25                            → MEDIUM, CORE_WEB_VITALS
  TTFB > 800ms                            → HIGH, CORE_WEB_VITALS
  Performance score < 50                  → HIGH, CORE_WEB_VITALS
  Images in legacy format (JPEG > WebP)   → MEDIUM, ASSET_OPTIMIZATION
  render_blocking_scripts > 0             → HIGH, RENDER_BLOCKING
  Images missing dimensions (CLS risk)    → MEDIUM, ASSET_OPTIMIZATION
  No text compression (gzip/br)           → HIGH, ASSET_OPTIMIZATION
  Cache-Control absent on static assets   → MEDIUM, CACHING

FAILURE RECOVERY:
  LighthouseRunner failure → AssetAnalyzer still runs; CWV findings have confidence=0.6
                             (estimated from AssetAnalyzer indicators, not measured)
  AssetAnalyzer failure    → LighthouseRunner findings still written
  Both fail                → Write single HIGH finding: "Performance measurement unavailable"


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 4: AccessibilityAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  Detects WCAG 2.1 Level AA violations using automated tools.
  Reports both violations and items requiring manual review.
  Deduplicates overlapping findings between axe-core and ContrastChecker.

TOOLS AVAILABLE:
  AxeCoreScanner, ContrastChecker
  Reads from SharedState: playwright_output (for URL), site_profile

EXECUTION FLOW:
  1. Read SiteProfile (no category-specific behavior — a11y applies universally)
  2. Run AxeCoreScanner(url, include_best_practices=True)
     → Run first (broadest coverage)
     → Emit OBSERVATION: violation counts by impact
     → For each CRITICAL-impact violation: emit individual REASONING about user impact
  3. Create findings from axe violations (one Finding per violation rule, not per element)
     → Group by rule_id — "23 images with missing alt text" = 1 finding
  4. Run ContrastChecker(url)
     → Run second — more precise on contrast than axe
     → Note which contrast failures were already reported by axe (dedup)
  5. Deduplicate: if axe reported 'color-contrast' for the same elements,
     use ContrastChecker data (more precise) but don't create duplicate finding
  6. Emit REASONING: "Manual review needed for N items — automated tools cannot verify"
  7. If incomplete items exist → write single INFO finding listing what needs manual review
  8. Complete

DEDUPLICATION RULE:
  axe 'color-contrast' violation + ContrastChecker failure for same element selector:
    → Keep ContrastChecker finding (has actual ratio values + suggested_foreground)
    → Discard axe's color-contrast violation for that element
    → But preserve axe metadata (wcag_criteria reference) in the ContrastChecker finding

FINDINGS GENERATED (axe impact → severity mapping):
  axe 'critical' impact   → CRITICAL finding
  axe 'serious' impact    → HIGH finding
  axe 'moderate' impact   → MEDIUM finding
  axe 'minor' impact      → LOW finding
  contrast_ratio < 2.0    → CRITICAL (near-invisible)
  contrast_ratio 2.0-4.5  → HIGH (fails AA)
  focus_indicator_failures → HIGH (keyboard users cannot navigate)
  missing_skip_link       → MEDIUM
  manual review items     → INFO (single bundled finding)

FAILURE RECOVERY:
  AxeCoreScanner fails → ContrastChecker still runs
  CSP blocks injection → ToolExecutor retries with local axe file injection
  Both fail            → Write single HIGH finding: "Accessibility scanning unavailable"


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 5: ContentAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  Evaluates content quality, readability, value proposition clarity,
  and goal alignment. Combines deterministic extraction with AI analysis.

TOOLS AVAILABLE:
  ContentExtractor, ClaudeContentAnalyzer
  Reads from SharedState: playwright_output (rendered_html), site_profile

EXECUTION FLOW:
  1. Read SiteProfile: category + primary_goals (essential for AI analysis context)
  2. Read rendered_html from playwright_output in SharedState
  3. Run ContentExtractor(rendered_html, url, site_category)
     → Emit OBSERVATION: word count, reading time, CTA count, readability grade
  4. Evaluate ContentExtractor output → generate DETERMINISTIC findings:
     → Readability grade > 12 (too complex)
     → CTA count == 0 (no calls to action)
     → Primary CTA not above fold
     → Word count < 150 (thin content)
     → These findings have confidence=0.95
  5. Run ClaudeContentAnalyzer(extracted_content, site_profile_context)
     → Pass: main_content_text, above_fold_text, goals, cta_texts, site_category
     → Emit OBSERVATION: AI scores + key issues found
     → Emit REASONING: "Claude scored content [N]/10. Key issue: [top_issue]"
  6. Generate AI findings from ClaudeContentAnalyzerOutput:
     → These findings have confidence = claude_output.confidence
     → Only create finding if confidence >= 0.65 (discard low-confidence AI findings)
     → Rewrite suggestions → attached to relevant findings as fix_suggestion.code_snippet
  7. Avoid duplication: if ContentExtractor already found "no CTA", don't create
     a second CTA finding from Claude's analysis
  8. Complete

DETERMINISTIC vs AI FINDING DISTINCTION:
  The Content Agent produces two classes of findings:
  Class A (deterministic): Readability scores, CTA count, word count, above-fold presence
    → confidence=0.95, high reliability
  Class B (AI-inferred): Value proposition clarity, tone assessment, goal alignment
    → confidence=claude.confidence (0.6-0.85), lower reliability
  Both classes appear in the report. Class B findings display their confidence score.

FINDINGS GENERATED:
  reading grade > 12               → MEDIUM, READABILITY
  passive_voice > 30%              → LOW, READABILITY
  CTA count = 0                    → HIGH, CTA
  primary CTA not above fold       → MEDIUM, CTA
  weak CTA text ('Click here')     → LOW, CTA
  word_count < 150                 → HIGH, CONTENT_QUALITY
  AI quality_score < 5             → HIGH, CONTENT_QUALITY
  AI vp_clarity_score < 5         → HIGH, VALUE_PROPOSITION
  AI goal_alignment_score < 5     → MEDIUM, CONTENT_QUALITY

FAILURE RECOVERY:
  ContentExtractor failure   → Cannot run Claude (no content to analyze). Write INFO finding.
  ClaudeContentAnalyzer fail → Write only deterministic findings. Log AI failure.
  Claude rate limited        → Wait 60s, retry once. If still failing, continue without AI.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 6: TechnicalAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  Analyzes HTTP-level technical health: security headers, HTTPS configuration,
  broken links, redirect chains, mobile readiness.
  Reads from SharedState more than any other agent (cross-references extensively).

TOOLS AVAILABLE:
  SecurityHeaderAnalyzer, BrokenLinkChecker
  Reads from SharedState: header_analysis (from Recon), link_extraction,
                          SEO findings (canonical context)

EXECUTION FLOW:
  1. Read header_analysis from SharedState (already fetched by Orchestrator)
     → Never re-fetches headers — uses cached recon artifact
  2. Run SecurityHeaderAnalyzer(response_headers, url, is_https)
     → Input comes directly from header_analysis + site_profile
     → No network call — pure analysis of existing data
     → Emit OBSERVATION: security grade + critical missing headers
  3. Generate security findings from SecurityHeaderAnalyzerOutput
  4. Read link_extraction from SharedState
  5. Run BrokenLinkChecker(internal_links, external_links, base_url)
     → This is the most time-consuming Technical tool (~30-90s for large sites)
     → Emit OBSERVATION updates during long runs (every 10 links checked)
  6. Read SEO agent findings (if available) from SharedState:
     → Look for canonical mismatch findings
     → Look for sitemap/robots.txt crawlability findings
     → Cross-reference: if canonical mismatch + redirect chain on same URL = compound
     → Note in trace but don't create finding (Synthesis does this)
  7. Emit REASONING: overall technical health, highlight compound issues
  8. Complete

FINDINGS GENERATED:
  Missing HSTS on HTTPS site       → HIGH, SECURITY (MITM attack risk)
  Missing CSP                      → HIGH, SECURITY (XSS risk)
  CSP has unsafe-inline            → MEDIUM, SECURITY
  Missing X-Frame-Options          → MEDIUM, SECURITY
  Server version disclosure        → LOW, SECURITY
  Site not HTTPS                   → CRITICAL, HTTPS
  Mixed content detected           → HIGH, HTTPS
  Broken internal links (any)      → HIGH, BROKEN_LINKS
  Broken external links > 3        → MEDIUM, BROKEN_LINKS
  Redirect chains > 2 hops         → MEDIUM, HTTP_STANDARDS
  Missing viewport meta tag        → HIGH, MOBILE (blocks mobile-first indexing)
  Canonical URL mismatch           → HIGH, CANONICAL

FAILURE RECOVERY:
  SecurityHeaderAnalyzer failure → Cannot occur (no network, pure computation)
  BrokenLinkChecker timeout      → Partial results used; confidence=0.7
                                   Finding title includes "(partial scan)"
  BrokenLinkChecker failure      → Skip link findings


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 7: SynthesisAgent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RESPONSIBILITIES:
  The SynthesisAgent is the final agent. It reads ALL findings from ALL specialist
  agents and produces the PriorityRoadmap. It is the only agent that creates
  FindingRelationship objects and synthesis findings (compound/cross-domain issues).

TOOLS AVAILABLE:
  None. The SynthesisAgent calls no tools from the ToolRegistry.
  It reads SharedState and calls Claude directly (not via ToolExecutor) for:
    - Cross-referencing analysis
    - Roadmap narrative generation
    - Business impact framing

EXECUTION FLOW:
  1. Verify all specialist agents are terminal (COMPLETE or FAILED or SKIPPED)
     → If any still RUNNING: this is a bug — raise RuntimeError
  2. Read all findings from SharedState (all agents)
     → Emit OBSERVATION: "Reading N total findings across 5 agents"
  3. Group findings by domain and sort by priority_score (descending)
  4. Cross-reference analysis pass — for each pair of findings:
     → Do they share a root cause? (e.g. no CDN → slow TTFB + poor caching score)
     → Does one compound the other? (e.g. missing alt text hurts both SEO + a11y)
     → Does fixing one automatically fix the other?
     → Create FindingRelationship for each detected connection
  5. Create compound findings via FindingFactory.create_synthesis_finding()
     → Only for compound issues that are NOT captured by existing findings
     → Example: "Missing CDN is the root cause of 3 separate performance findings"
  6. Call Claude to generate:
     → Priority ranking reasoning per roadmap item
     → Business impact framing for executive summary
     → Cross-insight narratives
     → Top-3 action items in plain English
  7. Build PriorityRoadmap:
     → Sort all findings by priority_score
     → Bundle related findings into RoadmapItems
     → Assign phases (QUICK_WINS vs STRATEGIC etc.) from impact/effort matrix
     → Apply dependencies (depends_on_ranks, unlocks_ranks)
  8. Compute AuditScoreSummary:
     → overall_score = weighted average of per-agent scores
     → Per-agent scores: 100 - (sum of severity-weighted finding penalties)
  9. Write PriorityRoadmap to SharedState (and DB via flush)
 10. Complete

BUNDLING RULES:
  Findings are bundled into a single RoadmapItem when:
    - Same root cause (e.g. all image optimization issues → one "Optimize images" item)
    - Same fix location (e.g. all header issues → one "Configure security headers" item)
    - FindingRelationship.type = CAUSED_BY (the cause = item title, effect = bundled)
  Findings are NOT bundled when:
    - They require different owners (dev vs content vs legal)
    - Fixing one depends on fixing the other first (dependency, not bundle)

COMPOUND FINDING CONDITIONS:
  Detect and create synthesis findings for:
  1. "Triple SEO penalty": missing alt text + slow LCP + no structured data
     → Single compound finding with impact_score = max(component scores) + 1
  2. "No CDN root cause": TTFB high + caching low + assets not compressed
     → Root cause finding: "Deploy a CDN to resolve 3 performance findings"
  3. "Accessibility + SEO overlap": missing alt text hurts both a11y (1.1.1) and SEO
     → Compound finding highlighting dual impact of single fix

COMPLETION CRITERIA:
  PriorityRoadmap written to SharedState and DB.
  All FindingRelationships written.
  overall_score computed.
  Audit status → COMPLETE.

FAILURE RECOVERY:
  Claude synthesis call fails → Use algorithmic ranking (priority_score sort) without
                                narrative framing. Roadmap items get generic why_prioritized text.
  Cross-reference fails       → Skip FindingRelationships, still produce roadmap.
  Always complete — even a minimal roadmap is better than no report.
"""
