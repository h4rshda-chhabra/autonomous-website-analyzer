"""
SEO Tool Schemas
────────────────
All three tools run sequentially within the SEO Agent.
Execution order:
  1. MetaTagAnalyzer     — lightweight, runs first, informs which structured data to look for
  2. StructuredDataAnalyzer — parses JSON-LD/microdata
  3. InternalLinkAnalyzer   — consumes LinkExtractor output from Recon (no re-crawl)

The SEO Agent reads site_profile from SharedState before calling any tool:
  - category = ecommerce → StructuredDataAnalyzer prioritizes Product + BreadcrumbList schemas
  - category = blog      → StructuredDataAnalyzer prioritizes Article + Person schemas
  - primary_goals include 'lead generation' → MetaTagAnalyzer checks conversion-oriented title patterns
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


# ═══════════════════════════════════════════════════════════════
# 6. MetaTagAnalyzer
# ═══════════════════════════════════════════════════════════════

class MetaTagAnalyzerInput(BaseModel):
    """
    Used by: SEO Agent.
    Purpose: Extracts and qualitatively evaluates all metadata elements that
             affect search engine indexing and social sharing appearance.
             Title and description are evaluated against known length guidelines
             and keyword signal strength — not just presence.
    """
    html: str = Field(..., description="Static HTML preferred (represents search-engine-visible content)")
    url: str = Field(..., description="Current page URL (for canonical comparison)")
    site_category: Optional[str] = Field(
        None,
        description="From SiteProfile — used to apply category-specific title/description heuristics",
    )
    primary_goal: Optional[str] = Field(
        None,
        description="Top goal from SiteProfile — used to assess keyword-goal alignment",
    )


class TitleAnalysis(BaseModel):
    text: Optional[str]
    length_chars: Optional[int]
    is_present: bool
    is_within_length: bool = Field(
        False,
        description="True if 30–60 characters (Google's display range)",
    )
    is_unique_signal: bool = Field(
        False,
        description="Heuristic: does the title appear to be page-specific vs. a template default?",
    )
    issues: List[str] = Field(default_factory=list)
    recommendation: Optional[str] = None


class MetaDescriptionAnalysis(BaseModel):
    text: Optional[str]
    length_chars: Optional[int]
    is_present: bool
    is_within_length: bool = Field(
        False,
        description="True if 120–158 characters",
    )
    has_cta_signal: bool = Field(
        False,
        description="Heuristic: contains an action word (Learn, Get, Start, Discover, etc.)",
    )
    issues: List[str] = Field(default_factory=list)
    recommendation: Optional[str] = None


class OpenGraphTags(BaseModel):
    og_title: Optional[str] = None
    og_description: Optional[str] = None
    og_image: Optional[str] = None
    og_image_width: Optional[int] = None
    og_image_height: Optional[int] = None
    og_url: Optional[str] = None
    og_type: Optional[str] = None
    og_site_name: Optional[str] = None
    is_complete: bool = Field(
        False,
        description="True if og:title, og:description, and og:image are all present",
    )
    image_dimension_issues: List[str] = Field(
        default_factory=list,
        description="E.g. 'og:image is smaller than recommended 1200×630px'",
    )


class TwitterCardTags(BaseModel):
    card_type: Optional[str] = Field(None, description="summary | summary_large_image | app | player")
    title: Optional[str] = None
    description: Optional[str] = None
    image: Optional[str] = None
    site_handle: Optional[str] = None
    is_complete: bool = False


class RobotsDirective(BaseModel):
    meta_robots: Optional[str] = Field(None, description="Content of <meta name='robots'>")
    is_indexable: bool = Field(
        True,
        description="False if noindex is present — critical flag",
    )
    is_followable: bool = Field(
        True,
        description="False if nofollow is present",
    )
    has_noarchive: bool = False
    has_nosnippet: bool = False


class MetaTagAnalyzerOutput(BaseModel):
    title: TitleAnalysis
    meta_description: MetaDescriptionAnalysis
    canonical_url: Optional[str] = Field(
        None,
        description="Value of <link rel='canonical' href='...'> if present",
    )
    canonical_matches_page_url: Optional[bool] = Field(
        None,
        description="True if canonical href matches the current page URL (self-referential is correct)",
    )
    open_graph: OpenGraphTags
    twitter_card: TwitterCardTags
    robots: RobotsDirective
    viewport_meta: Optional[str] = Field(None, description="Content of <meta name='viewport'>")
    charset_declared: bool = False
    lang_attribute: Optional[str] = Field(None, description="<html lang='...'> value")

    def summarize(self) -> Dict[str, Any]:
        return {
            "title_present": self.title.is_present,
            "title_length": self.title.length_chars,
            "description_present": self.meta_description.is_present,
            "og_complete": self.open_graph.is_complete,
            "canonical_present": self.canonical_url is not None,
            "is_indexable": self.robots.is_indexable,
            "lang_set": self.lang_attribute is not None,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # PARSE_ERROR  → Malformed HTML prevents tag extraction
    #                Partial: returns whatever was extracted before parse failure
    # No network   → This tool is pure HTML parsing; no external calls
    #
    # Key nuance: noindex on the page being audited is an IMMEDIATE critical finding.
    #             The SEO Agent must surface this regardless of other findings.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: SEO Agent
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 7. StructuredDataAnalyzer
# ═══════════════════════════════════════════════════════════════

class StructuredDataAnalyzerInput(BaseModel):
    """
    Used by: SEO Agent.
    Purpose: Extracts and validates all structured data implementations.
             Supports JSON-LD (primary), Microdata (legacy), and RDFa (rare).
             Validates against schema.org type requirements for the detected site category.
             Covers Google's rich result eligibility (Article, Product, FAQPage,
             LocalBusiness, BreadcrumbList, HowTo, Organization, WebSite).
    """
    html: str = Field(..., description="Static HTML — structured data is almost always in static DOM")
    url: str
    site_category: Optional[str] = Field(
        None,
        description=(
            "From SiteProfile. Used to determine which schemas are EXPECTED vs. OPTIONAL. "
            "ecommerce → Product schema expected. blog → Article expected. "
            "local_business → LocalBusiness expected."
        ),
    )


class SchemaOrgType(BaseModel):
    """A single schema.org entity found on the page."""
    schema_type: str = Field(..., description="E.g. 'Product', 'Article', 'BreadcrumbList'")
    implementation: str = Field(..., description="json-ld | microdata | rdfa")
    raw_data: Dict[str, Any] = Field(..., description="The raw parsed schema object")
    is_valid: bool = Field(
        ...,
        description="True if all required properties for this type are present",
    )
    missing_required_properties: List[str] = Field(
        default_factory=list,
        description="Required schema.org properties that are absent",
    )
    missing_recommended_properties: List[str] = Field(
        default_factory=list,
        description="Recommended properties whose absence reduces rich result eligibility",
    )
    google_rich_result_eligible: bool = Field(
        False,
        description="True if this schema meets Google's rich result requirements",
    )
    validation_errors: List[str] = Field(default_factory=list)
    validation_warnings: List[str] = Field(default_factory=list)


class StructuredDataAnalyzerOutput(BaseModel):
    schemas_found: List[SchemaOrgType] = Field(default_factory=list)
    json_ld_count: int = 0
    microdata_count: int = 0
    rdfa_count: int = 0
    has_organization_schema: bool = False
    has_website_schema: bool = Field(
        False,
        description="WebSite schema with SearchAction enables Google sitelinks searchbox",
    )
    expected_schemas_missing: List[str] = Field(
        default_factory=list,
        description=(
            "Schema types that should be present for this site category but aren't. "
            "E.g. ['Product', 'BreadcrumbList'] for an ecommerce site."
        ),
    )
    has_json_ld_parse_errors: bool = Field(
        False,
        description="True if any <script type='application/ld+json'> blocks contain invalid JSON",
    )
    conflicting_schemas: List[str] = Field(
        default_factory=list,
        description="Schema types that appear more than once with conflicting data",
    )

    def summarize(self) -> Dict[str, Any]:
        return {
            "total_schemas": len(self.schemas_found),
            "json_ld_count": self.json_ld_count,
            "valid_schemas": sum(1 for s in self.schemas_found if s.is_valid),
            "rich_result_eligible": sum(1 for s in self.schemas_found if s.google_rich_result_eligible),
            "expected_missing": self.expected_schemas_missing,
            "parse_errors": self.has_json_ld_parse_errors,
        }

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # PARSE_ERROR          → Malformed HTML or invalid JSON-LD (partial data available)
    # (no failures for absence — no structured data is a valid (poor) state)
    #
    # Key nuance: Google only parses JSON-LD in <head> or <body>. Structured data
    #             injected via JS after load may not be indexed. The SEO Agent
    #             should cross-reference with PlaywrightCrawler.static_html vs.
    #             rendered_html to detect JS-injected schemas.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: SEO Agent
    # Type: Deterministic


# ═══════════════════════════════════════════════════════════════
# 8. InternalLinkAnalyzer
# ═══════════════════════════════════════════════════════════════

class InternalLinkAnalyzerInput(BaseModel):
    """
    Used by: SEO Agent.
    Purpose: Analyzes the internal linking structure from the perspective of
             SEO value distribution (PageRank flow), crawl depth, and anchor text quality.
             Consumes LinkExtractor output — does NOT re-crawl.

    Note: This is a single-page analysis, not a full site crawl.
          It evaluates the link profile of the audited URL only.
          Full site crawl analysis is deferred (post-MVP).
    """
    internal_links: List[Dict[str, Any]] = Field(
        ...,
        description="internal_links from LinkExtractorOutput (serialized ExtractedLink list)",
    )
    current_url: str = Field(..., description="The page being analyzed")
    base_domain: str = Field(..., description="The root domain (for classifying subdomains)")


class AnchorTextPattern(BaseModel):
    pattern_type: str = Field(
        ...,
        description="descriptive | generic | keyword_rich | empty | image_only",
    )
    count: int
    examples: List[str] = Field(default_factory=list, max_length=3)


class HeadingStructureItem(BaseModel):
    level: int = Field(..., ge=1, le=6)
    text: str
    position_in_document: int = Field(..., description="Order index in the document")


class InternalLinkAnalyzerOutput(BaseModel):
    total_internal_links: int
    unique_internal_destinations: int
    self_referential_links: int = Field(
        0,
        description="Links that point back to the current page (usually nav items)",
    )

    # ── Anchor Text Quality ────────────────────────────────────────────────────
    anchor_text_patterns: List[AnchorTextPattern] = Field(default_factory=list)
    generic_anchor_count: int = Field(
        0,
        description="'Click here', 'Read more', 'Learn more' — poor for SEO",
    )
    empty_anchor_count: int = Field(
        0,
        description="Links with no anchor text (common with icon-only links)",
    )

    # ── Link Equity Signals ────────────────────────────────────────────────────
    navigational_link_count: int = Field(
        0,
        description="Links in nav/header/footer elements",
    )
    in_content_link_count: int = Field(
        0,
        description="Links within main content — higher SEO value",
    )
    nofollow_internal_count: int = Field(
        0,
        description="Internal links with rel=nofollow — usually unintentional and harmful",
    )

    # ── Heading Structure (analyzed here since both heading + linking form SEO content structure) ──
    heading_structure: List[HeadingStructureItem] = Field(default_factory=list)
    h1_count: int = 0
    h1_text: Optional[str] = Field(None, description="Text of first H1 (should be unique and keyword-rich)")
    heading_hierarchy_valid: bool = Field(
        True,
        description="False if headings skip levels (H1→H3 without H2, etc.)",
    )
    heading_hierarchy_issues: List[str] = Field(default_factory=list)

    def summarize(self) -> Dict[str, Any]:
        return {
            "total_internal_links": self.total_internal_links,
            "unique_destinations": self.unique_internal_destinations,
            "generic_anchors": self.generic_anchor_count,
            "empty_anchors": self.empty_anchor_count,
            "nofollow_internal": self.nofollow_internal_count,
            "h1_count": self.h1_count,
            "heading_hierarchy_valid": self.heading_hierarchy_valid,
        }

    @property
    def generic_anchor_text_percentage(self) -> float:
        """Fraction of internal links using generic anchor text. Used by SEO Agent findings threshold."""
        if self.total_internal_links == 0:
            return 0.0
        return self.generic_anchor_count / self.total_internal_links

    # ── Failure Modes ──────────────────────────────────────────────────────────
    # Input validation: empty internal_links list is valid (sparse page)
    # PARSE_ERROR: malformed link data from upstream LinkExtractor
    #
    # SPA caveat: JavaScript-rendered navigation (React Router, Next.js Link) may
    #             produce <a href> elements, but hash-based routers (#/page) will
    #             appear as self-referential links. js_href_count from LinkExtractor
    #             provides the signal to qualify this.
    # ──────────────────────────────────────────────────────────────────────────
    # Agents use it: SEO Agent
    # Type: Deterministic
