"""
SEO Tool Implementations — Phase 1A
=====================================
Three deterministic SEO tools that analyse existing HTML data without network calls.

Implemented:
  MetaTagAnalyzer        — Parses and evaluates all metadata affecting search visibility.
  StructuredDataAnalyzer — Extracts and validates JSON-LD/Microdata against schema.org specs.
  InternalLinkAnalyzer   — Scores anchor text quality and heading hierarchy from link data.

Contract for every tool function:
  Input:  pre-validated *Input Pydantic model
  Output: *Output Pydantic model
  Errors: raise exceptions — ToolExecutorImpl wraps them into ToolResult(success=False)
  Never:  make network requests, read SharedState, emit trace events
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .schemas import (
    AnchorTextPattern,
    HeadingStructureItem,
    InternalLinkAnalyzerInput,
    InternalLinkAnalyzerOutput,
    MetaDescriptionAnalysis,
    MetaTagAnalyzerInput,
    MetaTagAnalyzerOutput,
    OpenGraphTags,
    RobotsDirective,
    SchemaOrgType,
    StructuredDataAnalyzerInput,
    StructuredDataAnalyzerOutput,
    TitleAnalysis,
    TwitterCardTags,
)


# ═══════════════════════════════════════════════════════════════════════════════
# MetaTagAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  MetaTagAnalyzerInput(html, url, site_category, primary_goal)
# Output: MetaTagAnalyzerOutput with title/description/og/twitter/robots analysis
#
# Failure modes:
#   PARSE_ERROR → Severely malformed HTML (BeautifulSoup is usually forgiving).
#   Empty html  → Returns all-absent analysis; is_present=False for all fields.
#
# Example output:
#   title.text = "Fast SaaS Analytics | Acme"
#   title.length_chars = 28, is_within_length = True
#   meta_description.has_cta_signal = True ("Start your free trial today")
#   open_graph.is_complete = True
#   robots.is_indexable = True
# ═══════════════════════════════════════════════════════════════════════════════

_GENERIC_TITLE_WORDS: Set[str] = {
    "home", "welcome", "untitled", "page", "default", "index",
    "new page", "test", "draft",
}
_CTA_WORDS: List[str] = [
    "learn", "get", "start", "discover", "find", "try", "download",
    "sign up", "register", "explore", "see", "check out", "read",
    "browse", "join", "book", "request", "buy", "shop", "order", "compare",
]


async def run_meta_tag_analyzer(inp: MetaTagAnalyzerInput) -> MetaTagAnalyzerOutput:
    """
    Extracts and evaluates all SEO-relevant metadata from the page HTML.

    Title is assessed against the 30–60 character Google display range. Meta
    description against 120–158 characters. A CTA signal is detected when the
    description contains an action verb known to improve click-through rate.

    Example input:  html="<html lang='en'><head>...</head></html>", url="https://acme.com/"
    Example output: title.is_within_length=True, robots.is_indexable=True,
                    open_graph.is_complete=True, charset_declared=True
    """
    soup = BeautifulSoup(inp.html, "html.parser")
    head = soup.find("head") or soup

    title = _analyze_title(head, inp.site_category)
    meta_description = _analyze_meta_description(head)
    canonical_url, canonical_matches = _analyze_canonical(head, inp.url)
    open_graph = _analyze_open_graph(head)
    twitter_card = _analyze_twitter_card(head)
    robots = _analyze_robots_directive(head)
    viewport = _get_meta_content(head, "name", "viewport")
    charset_declared = _detect_charset(head)
    lang_attr = _get_html_lang(soup)

    return MetaTagAnalyzerOutput(
        title=title,
        meta_description=meta_description,
        canonical_url=canonical_url,
        canonical_matches_page_url=canonical_matches,
        open_graph=open_graph,
        twitter_card=twitter_card,
        robots=robots,
        viewport_meta=viewport,
        charset_declared=charset_declared,
        lang_attribute=lang_attr,
    )


def _get_meta_content(head: Any, attr: str, value: str) -> Optional[str]:
    """Finds a <meta> by attribute name and returns its content value."""
    tag = head.find("meta", attrs={attr: re.compile(f"^{re.escape(value)}$", re.IGNORECASE)})
    if tag is None:
        return None
    content = tag.get("content", "").strip()
    return content if content else None


def _analyze_title(head: Any, site_category: Optional[str]) -> TitleAnalysis:
    tag = head.find("title")
    if not tag:
        return TitleAnalysis(
            text=None,
            length_chars=None,
            is_present=False,
            issues=["Title tag is missing from <head>"],
            recommendation="Add <title>Page Topic | Brand Name</title> to the <head> section.",
        )

    text = tag.get_text(strip=True)
    if not text:
        return TitleAnalysis(
            text="",
            length_chars=0,
            is_present=False,
            issues=["Title tag is present but empty"],
            recommendation="Add meaningful text to the <title> tag.",
        )

    length = len(text)
    is_within = 30 <= length <= 60
    issues: List[str] = []

    if length < 10:
        issues.append(f"Title is very short ({length} chars) — likely uninformative.")
    elif length < 30:
        issues.append(f"Title is short ({length} chars). Aim for 30–60 characters.")
    elif length > 70:
        issues.append(f"Title is too long ({length} chars). Google typically truncates beyond ~60 characters.")
    elif length > 60:
        issues.append(f"Title is slightly long ({length} chars). May be truncated in some SERPs.")

    # Heuristic: titles that are generic template defaults (single words, common filler phrases)
    text_lower = text.lower().strip()
    is_unique = text_lower not in _GENERIC_TITLE_WORDS and len(text.split()) >= 2

    if not is_unique:
        issues.append("Title appears to be a template default (e.g., 'Home', 'Welcome'). Make it page-specific.")

    rec = None
    if issues:
        if not is_within:
            rec = "Rewrite the title to be 30–60 characters. Include the primary keyword near the front."
        elif not is_unique:
            rec = "Replace the generic title with a descriptive, page-specific title."

    return TitleAnalysis(
        text=text,
        length_chars=length,
        is_present=True,
        is_within_length=is_within,
        is_unique_signal=is_unique,
        issues=issues,
        recommendation=rec,
    )


def _analyze_meta_description(head: Any) -> MetaDescriptionAnalysis:
    content = _get_meta_content(head, "name", "description")

    if content is None:
        return MetaDescriptionAnalysis(
            text=None,
            length_chars=None,
            is_present=False,
            issues=["Meta description is missing"],
            recommendation="Add <meta name='description' content='...'> with 120–158 characters.",
        )

    length = len(content)
    is_within = 120 <= length <= 158
    issues: List[str] = []

    if length < 50:
        issues.append(f"Meta description is very short ({length} chars). Google may auto-generate the snippet.")
    elif length < 120:
        issues.append(f"Meta description is short ({length} chars). Aim for 120–158 characters for full display.")
    elif length > 200:
        issues.append(f"Meta description is very long ({length} chars). Truncated in SERPs beyond ~158 chars.")
    elif length > 158:
        issues.append(f"Meta description is slightly long ({length} chars). Google may truncate it.")

    content_lower = content.lower()
    has_cta = any(cta in content_lower for cta in _CTA_WORDS)

    rec = None
    if issues:
        rec = "Rewrite to 120–158 characters. Open with the primary value proposition and include a CTA verb."

    return MetaDescriptionAnalysis(
        text=content,
        length_chars=length,
        is_present=True,
        is_within_length=is_within,
        has_cta_signal=has_cta,
        issues=issues,
        recommendation=rec,
    )


def _normalize_url_for_compare(url: str) -> str:
    """Strips trailing slash and lowercases scheme+host for canonical comparison."""
    try:
        p = urlparse(url)
        path = p.path.rstrip("/") or "/"
        return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"
    except Exception:
        return url.lower().rstrip("/")


def _analyze_canonical(head: Any, page_url: str) -> Tuple[Optional[str], Optional[bool]]:
    tag = head.find("link", rel=re.compile(r"canonical", re.IGNORECASE))
    if tag is None:
        return None, None

    href = (tag.get("href") or "").strip()
    if not href:
        return None, None

    matches = _normalize_url_for_compare(href) == _normalize_url_for_compare(page_url)
    return href, matches


def _analyze_open_graph(head: Any) -> OpenGraphTags:
    def og(prop: str) -> Optional[str]:
        tag = head.find("meta", attrs={"property": re.compile(f"^og:{re.escape(prop)}$", re.IGNORECASE)})
        return (tag.get("content") or "").strip() or None if tag else None

    title = og("title")
    description = og("description")
    image = og("image")
    og_url = og("url")
    og_type = og("type")
    og_site_name = og("site_name")

    # Dimensions may come from og:image:width and og:image:height
    def og_int(prop: str) -> Optional[int]:
        val = og(prop)
        try:
            return int(val) if val else None
        except (ValueError, TypeError):
            return None

    width = og_int("image:width")
    height = og_int("image:height")

    is_complete = all([title, description, image])
    dim_issues: List[str] = []

    if image and width is not None and height is not None:
        if width < 1200 or height < 630:
            dim_issues.append(
                f"og:image is {width}×{height}px — smaller than the recommended 1200×630px. "
                "May display poorly when shared on social media."
            )
    elif image and (width is None or height is None):
        dim_issues.append(
            "og:image:width / og:image:height not specified. "
            "Add them so social platforms can render the preview without fetching the image."
        )

    return OpenGraphTags(
        og_title=title,
        og_description=description,
        og_image=image,
        og_image_width=width,
        og_image_height=height,
        og_url=og_url,
        og_type=og_type,
        og_site_name=og_site_name,
        is_complete=is_complete,
        image_dimension_issues=dim_issues,
    )


def _analyze_twitter_card(head: Any) -> TwitterCardTags:
    def tw(name: str) -> Optional[str]:
        tag = head.find("meta", attrs={"name": re.compile(f"^twitter:{re.escape(name)}$", re.IGNORECASE)})
        if tag is None:
            # Some sites use property= instead of name= for Twitter tags
            tag = head.find("meta", attrs={"property": re.compile(f"^twitter:{re.escape(name)}$", re.IGNORECASE)})
        return (tag.get("content") or "").strip() or None if tag else None

    card_type = tw("card")
    title = tw("title")
    description = tw("description")
    image = tw("image")
    site = tw("site")

    is_complete = all([card_type, title, image])

    return TwitterCardTags(
        card_type=card_type,
        title=title,
        description=description,
        image=image,
        site_handle=site,
        is_complete=is_complete,
    )


def _analyze_robots_directive(head: Any) -> RobotsDirective:
    content = _get_meta_content(head, "name", "robots")
    if content is None:
        return RobotsDirective(
            meta_robots=None,
            is_indexable=True,
            is_followable=True,
        )

    c = content.lower()
    has_noindex = "noindex" in c
    has_nofollow = "nofollow" in c
    has_noarchive = "noarchive" in c
    has_nosnippet = "nosnippet" in c

    return RobotsDirective(
        meta_robots=content,
        is_indexable=not has_noindex,
        is_followable=not has_nofollow,
        has_noarchive=has_noarchive,
        has_nosnippet=has_nosnippet,
    )


def _detect_charset(head: Any) -> bool:
    # <meta charset="..."> (HTML5) or <meta http-equiv="Content-Type" ...>
    if head.find("meta", charset=True):
        return True
    tag = head.find("meta", attrs={"http-equiv": re.compile(r"content-type", re.IGNORECASE)})
    return tag is not None


def _get_html_lang(soup: Any) -> Optional[str]:
    html_tag = soup.find("html")
    if html_tag and isinstance(html_tag, Tag):
        lang = html_tag.get("lang", "").strip()
        return lang if lang else None
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# StructuredDataAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  StructuredDataAnalyzerInput(html, url, site_category)
# Output: StructuredDataAnalyzerOutput with validated SchemaOrgType objects
#
# Failure modes:
#   JSON_PARSE_ERROR → Malformed JSON in <script type="application/ld+json">.
#                      Sets has_json_ld_parse_errors=True; continues with remaining blocks.
#   Empty page       → Returns empty schemas_found list (not an error).
#
# Example output:
#   json_ld_count = 2, microdata_count = 0
#   schemas_found[0].schema_type = "Organization"
#   schemas_found[0].is_valid = True, google_rich_result_eligible = True
#   expected_schemas_missing = ["Product", "BreadcrumbList"]  (for ecommerce site)
# ═══════════════════════════════════════════════════════════════════════════════

# Required and recommended properties per schema.org type for Google rich results.
_SCHEMA_REQUIREMENTS: Dict[str, Dict[str, List[str]]] = {
    "Article": {
        "required": ["headline", "author", "datePublished"],
        "recommended": ["image", "dateModified", "description", "publisher"],
    },
    "NewsArticle": {
        "required": ["headline", "author", "datePublished"],
        "recommended": ["image", "dateModified", "description"],
    },
    "BlogPosting": {
        "required": ["headline", "author", "datePublished"],
        "recommended": ["image", "dateModified", "description"],
    },
    "Product": {
        "required": ["name"],
        "recommended": ["image", "offers", "description", "brand", "sku", "aggregateRating"],
    },
    "BreadcrumbList": {
        "required": ["itemListElement"],
        "recommended": [],
    },
    "Organization": {
        "required": ["name"],
        "recommended": ["url", "logo", "contactPoint", "sameAs", "address"],
    },
    "WebSite": {
        "required": ["name", "url"],
        "recommended": ["potentialAction", "description"],
    },
    "LocalBusiness": {
        "required": ["name"],
        "recommended": ["address", "telephone", "openingHours", "url", "geo"],
    },
    "FAQPage": {
        "required": ["mainEntity"],
        "recommended": [],
    },
    "HowTo": {
        "required": ["name", "step"],
        "recommended": ["image", "description", "totalTime", "supply", "tool"],
    },
    "Event": {
        "required": ["name", "startDate"],
        "recommended": ["endDate", "location", "description", "image", "organizer"],
    },
    "Recipe": {
        "required": ["name"],
        "recommended": ["image", "author", "datePublished", "description",
                        "prepTime", "cookTime", "recipeYield",
                        "recipeIngredient", "recipeInstructions"],
    },
    "Person": {
        "required": ["name"],
        "recommended": ["url", "sameAs", "jobTitle", "image"],
    },
    "JobPosting": {
        "required": ["title", "description", "datePosted", "hiringOrganization"],
        "recommended": ["validThrough", "jobLocation", "baseSalary"],
    },
    "Review": {
        "required": ["itemReviewed", "reviewRating", "author"],
        "recommended": ["datePublished", "reviewBody"],
    },
    "VideoObject": {
        "required": ["name", "description", "thumbnailUrl", "uploadDate"],
        "recommended": ["duration", "contentUrl", "embedUrl"],
    },
}

# Rich-result eligible types that Google actively uses for SERP features.
_RICH_RESULT_TYPES: Set[str] = {
    "Article", "NewsArticle", "BlogPosting", "Product", "BreadcrumbList",
    "FAQPage", "HowTo", "Event", "Recipe", "Review", "VideoObject",
    "JobPosting", "LocalBusiness", "WebSite",
}

# Expected schema types per site category.
_EXPECTED_SCHEMAS: Dict[str, List[str]] = {
    "ecommerce": ["Product", "BreadcrumbList"],
    "blog": ["Article"],
    "news": ["NewsArticle"],
    "corporate": ["Organization"],
    "agency": ["Organization"],
    "saas": ["Organization", "WebSite"],
    "portfolio": ["Person"],
    "nonprofit": ["Organization"],
    "documentation": ["WebSite"],
}


async def run_structured_data_analyzer(inp: StructuredDataAnalyzerInput) -> StructuredDataAnalyzerOutput:
    """
    Parses all structured data (JSON-LD, Microdata) from HTML and validates each
    schema.org type against Google rich result requirements.

    JSON parse errors in individual blocks are recorded but don't abort processing.
    Returns empty schemas_found when no structured data is present — this is a valid
    (though poor) state, not an error.

    Example input:  html="<script type='application/ld+json'>{'@type': 'Product', 'name': 'Acme Widget'}</script>",
                    url="https://shop.com/widget", site_category="ecommerce"
    Example output: schemas_found=[SchemaOrgType(schema_type="Product", is_valid=True,
                    missing_recommended_properties=["image", "offers", ...])],
                    expected_schemas_missing=["BreadcrumbList"]
    """
    soup = BeautifulSoup(inp.html, "html.parser")

    json_ld_schemas, has_parse_errors = _extract_json_ld(soup)
    microdata_schemas = _extract_microdata(soup)

    all_schemas = json_ld_schemas + microdata_schemas
    all_schemas = [_validate_schema(s) for s in all_schemas]

    # Detect duplicate schema types with potentially conflicting data.
    type_counts: Counter = Counter(s.schema_type for s in all_schemas)
    conflicting = [t for t, c in type_counts.items() if c > 1]

    present_types = {s.schema_type for s in all_schemas}
    category = (inp.site_category or "").lower()
    expected = _EXPECTED_SCHEMAS.get(category, [])
    missing = [t for t in expected if t not in present_types]

    has_org = any(s.schema_type in ("Organization", "LocalBusiness") for s in all_schemas)
    has_website = any(s.schema_type == "WebSite" for s in all_schemas)

    return StructuredDataAnalyzerOutput(
        schemas_found=all_schemas,
        json_ld_count=len(json_ld_schemas),
        microdata_count=len(microdata_schemas),
        rdfa_count=0,
        has_organization_schema=has_org,
        has_website_schema=has_website,
        expected_schemas_missing=missing,
        has_json_ld_parse_errors=has_parse_errors,
        conflicting_schemas=conflicting,
    )


def _extract_json_ld(soup: Any) -> Tuple[List[SchemaOrgType], bool]:
    schemas: List[SchemaOrgType] = []
    has_errors = False

    for script_tag in soup.find_all("script", type="application/ld+json"):
        raw_text = script_tag.get_text(strip=True)
        if not raw_text:
            continue

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            has_errors = True
            schemas.append(SchemaOrgType(
                schema_type="UNKNOWN",
                implementation="json-ld",
                raw_data={"parse_error": str(e), "raw_excerpt": raw_text[:200]},
                is_valid=False,
                validation_errors=[f"JSON parse error: {e}"],
            ))
            continue

        # @graph blocks contain multiple entities in a single script block
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]
        elif isinstance(data, list):
            items = data
        else:
            items = [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            schema_type = _extract_type(item)
            if schema_type:
                schemas.append(SchemaOrgType(
                    schema_type=schema_type,
                    implementation="json-ld",
                    raw_data=item,
                    is_valid=True,
                    missing_required_properties=[],
                    missing_recommended_properties=[],
                    google_rich_result_eligible=False,
                    validation_errors=[],
                    validation_warnings=[],
                ))

    return schemas, has_errors


def _extract_type(item: Dict[str, Any]) -> Optional[str]:
    """Extracts the schema.org type name from @type, stripping the schema.org prefix."""
    raw_type = item.get("@type")
    if not raw_type:
        return None
    if isinstance(raw_type, list):
        raw_type = raw_type[0]
    # Strip namespace: "https://schema.org/Product" → "Product"
    return str(raw_type).split("/")[-1]


def _extract_microdata(soup: Any) -> List[SchemaOrgType]:
    """Extracts schema.org entities from HTML Microdata attributes (itemscope/itemtype)."""
    schemas: List[SchemaOrgType] = []

    for tag in soup.find_all(itemscope=True):
        itemtype = tag.get("itemtype", "")
        if not itemtype or "schema.org" not in itemtype:
            continue

        schema_type = itemtype.rstrip("/").split("/")[-1]
        if not schema_type:
            continue

        # Collect itemprop values as a flat dict
        props: Dict[str, Any] = {}
        for prop_tag in tag.find_all(itemprop=True):
            name = prop_tag.get("itemprop", "")
            value = (
                prop_tag.get("content")
                or prop_tag.get("href")
                or prop_tag.get("src")
                or prop_tag.get_text(strip=True)
            )
            if name and value:
                props[name] = value

        schemas.append(SchemaOrgType(
            schema_type=schema_type,
            implementation="microdata",
            raw_data=props,
            is_valid=True,
            missing_required_properties=[],
            missing_recommended_properties=[],
            google_rich_result_eligible=False,
            validation_errors=[],
            validation_warnings=[],
        ))

    return schemas


def _validate_schema(schema: SchemaOrgType) -> SchemaOrgType:
    """
    Validates a SchemaOrgType against known required/recommended properties.
    Mutates-then-returns (Pydantic models are mutable by default in v2 unless frozen).
    """
    if schema.schema_type == "UNKNOWN":
        return schema

    spec = _SCHEMA_REQUIREMENTS.get(schema.schema_type)
    errors: List[str] = list(schema.validation_errors)
    warnings: List[str] = list(schema.validation_warnings)

    if spec is None:
        warnings.append(
            f"No validation spec available for '{schema.schema_type}'. "
            "Marking as valid without property checks."
        )
        schema = schema.model_copy(update={
            "is_valid": True,
            "validation_warnings": warnings,
        })
        return schema

    present = set(schema.raw_data.keys())
    missing_required = [p for p in spec["required"] if p not in present]
    missing_recommended = [p for p in spec["recommended"] if p not in present]

    is_valid = len(missing_required) == 0
    eligible = (
        schema.schema_type in _RICH_RESULT_TYPES
        and is_valid
        and len(missing_recommended) < len(spec["recommended"])
    )

    if missing_required:
        errors.append(
            f"Missing required properties: {', '.join(missing_required)}. "
            f"Schema type '{schema.schema_type}' is invalid without them."
        )
    if missing_recommended:
        warnings.append(
            f"Missing recommended properties: {', '.join(missing_recommended)}. "
            "These improve rich result eligibility."
        )

    return schema.model_copy(update={
        "is_valid": is_valid,
        "missing_required_properties": missing_required,
        "missing_recommended_properties": missing_recommended,
        "google_rich_result_eligible": eligible,
        "validation_errors": errors,
        "validation_warnings": warnings,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# InternalLinkAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════
#
# Input:  InternalLinkAnalyzerInput(internal_links, current_url, base_domain)
#         Note: internal_links is List[Dict] — serialized ExtractedLink objects.
# Output: InternalLinkAnalyzerOutput with anchor text quality and link equity scores
#
# Failure modes:
#   Empty list → Returns zeroed output (not an error — valid for a sparse page).
#   Malformed link dict → Missing keys are treated as empty/False defaults.
#
# Phase 1A limitation:
#   heading_structure, h1_count, h1_text, heading_hierarchy_valid remain at defaults.
#   The SEO Agent passes html=static_html as an extra kwarg, but InternalLinkAnalyzerInput
#   has no html field (Pydantic silently drops it). Heading analysis requires Phase 1B
#   schema update or a separate HeadingAnalyzer tool.
#
# Example output for a nav-heavy blog:
#   total_internal_links = 18, unique_internal_destinations = 12
#   generic_anchor_count = 3, generic_anchor_text_percentage = 0.167
#   navigational_link_count = 10, in_content_link_count = 8
#   nofollow_internal_count = 0
# ═══════════════════════════════════════════════════════════════════════════════

_GENERIC_ANCHOR_PHRASES: Set[str] = {
    "click here", "click", "here", "read more", "learn more", "more",
    "this", "link", "go", "go here", "this link", "this page", "view",
    "details", "info", "information", "page", "visit", "continue", "open",
    "see more", "find out more", "find out", "see here", "see this",
    "follow this link", "please click here",
}


async def run_internal_link_analyzer(inp: InternalLinkAnalyzerInput) -> InternalLinkAnalyzerOutput:
    """
    Analyses the internal link profile of a single page.

    Consumes the serialized ExtractedLink list from LinkExtractor — no re-crawl.
    Scores anchor text quality, identifies navigational vs. in-content links, and
    counts nofollow misuses.

    Example input:  internal_links=[{"href": "/about", "anchor_text": "click here",
                    "is_navigational": False, "rel_attributes": []}],
                    current_url="https://example.com/"
    Example output: total_internal_links=1, generic_anchor_count=1,
                    generic_anchor_text_percentage=1.0
    """
    links = inp.internal_links

    if not links:
        return InternalLinkAnalyzerOutput(
            total_internal_links=0,
            unique_internal_destinations=0,
        )

    total = len(links)
    current_normalized = _strip_url(inp.current_url)

    unique_dests: Set[str] = set()
    self_refs = 0
    nav_count = 0
    content_count = 0
    nofollow_count = 0
    generic_count = 0
    empty_count = 0

    anchor_types: Counter = Counter()
    generic_examples: List[str] = []
    empty_examples: List[str] = []
    descriptive_examples: List[str] = []

    for link in links:
        normalized = (link.get("normalized_url") or link.get("href") or "").strip()
        anchor_text = (link.get("anchor_text") or "").strip()
        is_nav: bool = link.get("is_navigational", False)
        rel_attrs: List[str] = link.get("rel_attributes", [])

        if normalized:
            unique_dests.add(_strip_url(normalized))
            if _strip_url(normalized) == current_normalized:
                self_refs += 1

        if is_nav:
            nav_count += 1
        else:
            content_count += 1

        if "nofollow" in rel_attrs:
            nofollow_count += 1

        if not anchor_text:
            empty_count += 1
            anchor_types["empty"] += 1
            empty_examples = _add_example(empty_examples, link.get("href") or normalized)
        elif _is_generic_anchor(anchor_text):
            generic_count += 1
            anchor_types["generic"] += 1
            generic_examples = _add_example(generic_examples, anchor_text)
        else:
            anchor_types["descriptive"] += 1
            descriptive_examples = _add_example(descriptive_examples, anchor_text)

    # Build AnchorTextPattern list
    patterns: List[AnchorTextPattern] = []
    if anchor_types["descriptive"]:
        patterns.append(AnchorTextPattern(
            pattern_type="descriptive",
            count=anchor_types["descriptive"],
            examples=descriptive_examples[:3],
        ))
    if anchor_types["generic"]:
        patterns.append(AnchorTextPattern(
            pattern_type="generic",
            count=anchor_types["generic"],
            examples=generic_examples[:3],
        ))
    if anchor_types["empty"]:
        patterns.append(AnchorTextPattern(
            pattern_type="empty",
            count=anchor_types["empty"],
            examples=empty_examples[:3],
        ))

    return InternalLinkAnalyzerOutput(
        total_internal_links=total,
        unique_internal_destinations=len(unique_dests),
        self_referential_links=self_refs,
        anchor_text_patterns=patterns,
        generic_anchor_count=generic_count,
        empty_anchor_count=empty_count,
        navigational_link_count=nav_count,
        in_content_link_count=content_count,
        nofollow_internal_count=nofollow_count,
        # Heading fields remain at schema defaults (require html — Phase 1A limitation).
        heading_structure=[],
        h1_count=0,
        h1_text=None,
        heading_hierarchy_valid=True,
        heading_hierarchy_issues=[],
    )


def _strip_url(url: str) -> str:
    """Normalizes a URL to scheme://host/path (no query, no fragment) for dedup."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url.rstrip("/")


def _is_generic_anchor(text: str) -> bool:
    """Returns True if the anchor text matches known low-quality link text patterns."""
    normalized = text.lower().strip().rstrip(".")
    return normalized in _GENERIC_ANCHOR_PHRASES


def _add_example(existing: List[str], new_example: str) -> List[str]:
    """Adds an example to the list if fewer than 3 are present."""
    if len(existing) < 3 and new_example and new_example not in existing:
        return existing + [new_example[:80]]
    return existing
