from enum import Enum


# ─── Site Classification ───────────────────────────────────────────────────────

class SiteCategory(str, Enum):
    SAAS        = "saas"
    ECOMMERCE   = "ecommerce"
    BLOG        = "blog"
    PORTFOLIO   = "portfolio"
    AGENCY      = "agency"
    NEWS        = "news"
    DOCS        = "documentation"
    CORPORATE   = "corporate"
    NONPROFIT   = "nonprofit"
    OTHER       = "other"


class RenderingStrategy(str, Enum):
    SSR     = "ssr"      # Server-side rendered on every request
    SSG     = "ssg"      # Pre-built static files
    CSR     = "csr"      # Client-side SPA, minimal initial HTML
    HYBRID  = "hybrid"   # Mix of SSR/SSG/CSR (e.g. Next.js App Router)
    UNKNOWN = "unknown"


# ─── Agent Identification ──────────────────────────────────────────────────────

class AgentType(str, Enum):
    ORCHESTRATOR  = "orchestrator"
    SEO           = "seo"
    PERFORMANCE   = "performance"
    ACCESSIBILITY = "accessibility"
    CONTENT       = "content"
    TECHNICAL     = "technical"
    SYNTHESIS     = "synthesis"


class AgentStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    COMPLETE = "complete"
    FAILED   = "failed"
    SKIPPED  = "skipped"   # Orchestrator decided not to run this agent


# ─── Audit Lifecycle ───────────────────────────────────────────────────────────

class AuditStatus(str, Enum):
    PENDING       = "pending"
    RECON         = "recon"
    PLANNING      = "planning"
    AUDITING      = "auditing"
    SYNTHESIZING  = "synthesizing"
    COMPLETE      = "complete"
    FAILED        = "failed"


class AuditDepth(str, Enum):
    STANDARD = "standard"   # Core checks, suitable for most sites
    DEEP     = "deep"       # Exhaustive — more tool calls, longer duration


# ─── Findings ─────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"   # Actively harming business outcomes right now
    HIGH     = "high"       # Significant impact, fix within a sprint
    MEDIUM   = "medium"     # Noticeable impact, fix in next cycle
    LOW      = "low"        # Minor — worth fixing when convenient
    INFO     = "info"       # Observation only, no action required

    @property
    def weight(self) -> int:
        """Numeric weight for priority scoring."""
        return {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}[self.value]


class ImplementationEffort(str, Enum):
    EASY   = "easy"    # < 2 hours, single-file or config change
    MEDIUM = "medium"  # 2–8 hours, moderate code change
    HARD   = "hard"    # > 8 hours, architectural or multi-system

    @property
    def divisor(self) -> float:
        """Used in priority score: higher effort reduces priority."""
        return {"easy": 1.0, "medium": 2.0, "hard": 3.5}[self.value]


class FindingCategory(str, Enum):
    # SEO
    META_TAGS        = "meta_tags"
    STRUCTURED_DATA  = "structured_data"
    CRAWLABILITY     = "crawlability"
    CONTENT_SIGNALS  = "content_signals"
    INTERNAL_LINKING = "internal_linking"

    # Performance
    CORE_WEB_VITALS     = "core_web_vitals"
    ASSET_OPTIMIZATION  = "asset_optimization"
    CACHING             = "caching"
    RENDER_BLOCKING     = "render_blocking"

    # Accessibility
    WCAG_PERCEIVABLE    = "wcag_perceivable"
    WCAG_OPERABLE       = "wcag_operable"
    WCAG_UNDERSTANDABLE = "wcag_understandable"
    WCAG_ROBUST         = "wcag_robust"

    # Content
    READABILITY       = "readability"
    CTA               = "cta"
    VALUE_PROPOSITION = "value_proposition"
    CONTENT_QUALITY   = "content_quality"

    # Technical
    SECURITY       = "security"
    HTTPS          = "https"
    MOBILE         = "mobile"
    BROKEN_LINKS   = "broken_links"
    HTTP_STANDARDS = "http_standards"
    CANONICAL      = "canonical"

    # Synthesis-only
    COMPOUND_ISSUE = "compound_issue"
    CROSS_DOMAIN   = "cross_domain"


class FindingRelationshipType(str, Enum):
    COMPOUNDS = "compounds"   # This finding amplifies the other
    CAUSED_BY = "caused_by"   # This finding is a symptom of the other
    BLOCKS    = "blocks"      # Fixing the other first is required
    RELATED   = "related"     # Thematically connected, no causal link


# ─── Trace Events ─────────────────────────────────────────────────────────────

class TraceEventType(str, Enum):
    AGENT_STARTED    = "agent_started"
    TOOL_CALL        = "tool_call"
    TOOL_RESULT      = "tool_result"
    OBSERVATION      = "observation"      # Agent notes something meaningful
    REASONING        = "reasoning"        # Agent explains what to do next
    PLAN_UPDATE      = "plan_update"      # Orchestrator adjusts the plan mid-audit
    FINDING_WRITTEN  = "finding_written"  # A finding was committed to shared state
    AGENT_COMPLETE   = "agent_complete"
    ERROR            = "error"


# ─── Roadmap ──────────────────────────────────────────────────────────────────

class RoadmapPhase(str, Enum):
    QUICK_WINS = "quick_wins"   # High impact + easy effort
    CORE_FIXES = "core_fixes"   # High impact + medium effort
    STRATEGIC  = "strategic"    # High impact + hard effort (plan carefully)
    CLEANUP    = "cleanup"      # Low impact + easy effort (do eventually)
    DEFER      = "defer"        # Low impact + hard effort (skip for now)


class InsightType(str, Enum):
    COMPOUND    = "compound"    # Two findings that make each other worse
    OPPORTUNITY = "opportunity" # Fixing one thing unlocks additional benefit
    PATTERN     = "pattern"     # Same root cause across multiple findings
    CONFLICT    = "conflict"    # Two findings that compete (fixing one may affect the other)
