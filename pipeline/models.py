"""Pydantic schemas — canonical data contract for the pipeline.

All data shapes are defined here. No module defines its own ad-hoc dicts.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def slugify(text: str) -> str:
    """Convert text to a URL-safe slug (lowercase, hyphens, no special chars)."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def compute_weekly_id(week_of: str) -> str:
    """Convert a week_of date string (e.g. '2026-02-16') to ISO week ID (e.g. '2026-W8')."""
    dt = datetime.strptime(week_of, "%Y-%m-%d")
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week}"


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def runs_path(category_id: str, week_of: str, filename: str | None = None) -> Path:
    """Return the path to a run artifact directory or file.

    Creates the directory if it doesn't exist.
    """
    base = Path(__file__).resolve().parent.parent / "runs" / category_id / week_of
    base.mkdir(parents=True, exist_ok=True)
    if filename:
        return base / filename
    return base


# ---------------------------------------------------------------------------
# Category config (loaded from YAML)
# ---------------------------------------------------------------------------

class CategoryConfig(BaseModel):
    category_id: str
    display_name: str
    site_name: str
    site_url: str
    gender: str
    product_type: str
    search_index: str
    browse_node_id: str
    keywords: str
    min_reviews: int
    min_rating: float
    price_min_usd: int
    price_max_usd: int
    slug_prefix: str
    table_roundups: str
    table_rankings: str
    table_catalog: str
    table_faq: str
    assoc_tag: str
    geniuslink_group_id: str
    schedule: str
    # Google Trends config (optional — defaults allow existing configs to load)
    trends_keyword: Optional[str] = None  # Falls back to `keywords` if absent
    trends_timeframe: str = "now 7-d"
    trends_geo: str = "US"
    trends_max_supplemental_searches: int = 8


# ---------------------------------------------------------------------------
# Raw signals (from Amazon Creators API)
# ---------------------------------------------------------------------------

class RawProduct(BaseModel):
    asin: str
    title: str
    brand: Optional[str] = None
    bsr: Optional[int] = None
    review_count: Optional[int] = None
    rating: Optional[float] = None
    price_usd: Optional[float] = None
    image_url: Optional[str] = None
    detail_page_url: Optional[str] = None
    source: str = "primary"  # "primary" or "supplemental"


class RawSignals(BaseModel):
    category_id: str
    week_of: str
    collected_at: datetime
    search_keywords: str
    total_api_results: int
    products_before_filter: int
    products_after_filter: int
    products: list[RawProduct]


# ---------------------------------------------------------------------------
# Google Trends data
# ---------------------------------------------------------------------------

class TrendsQuery(BaseModel):
    query: str                              # Raw query from Google Trends
    search_interest: int                    # 0-100 relative score
    increase_percent: Optional[str] = None  # e.g., "20%" or "-10%"
    source: str                             # "rising" or "top"
    query_type: str                         # "brand_model", "brand_only", "generic"
    normalized_brand: Optional[str] = None
    normalized_model: Optional[str] = None


class TrendsData(BaseModel):
    category_id: str
    week_of: str
    collected_at: datetime
    trends_keyword: str
    rising_queries: list[TrendsQuery]
    top_queries: list[TrendsQuery]


# ---------------------------------------------------------------------------
# Ranked output (after normalization, dedup, scoring)
# ---------------------------------------------------------------------------

class RankedProduct(BaseModel):
    rank: int
    asin: str
    title: str
    brand: str
    model: str
    full_name: str
    bsr: Optional[int] = None
    review_count: Optional[int] = None
    rating: Optional[float] = None
    price_usd: Optional[float] = None
    image_url: Optional[str] = None
    detail_page_url: Optional[str] = None
    heat_score: float
    rank_change: str = "NEW"
    # Google Trends ranking fields
    trend_source: Optional[str] = None         # "rising" or "top"
    trend_match_type: Optional[str] = None     # "brand_model" or "brand_only"
    trend_query: Optional[str] = None          # Original Google Trends query
    trend_search_interest: Optional[int] = None
    selection_tier: int = 5                    # 1-5


class RankedOutput(BaseModel):
    category_id: str
    week_of: str
    ranked_at: datetime
    product_count: int
    products: list[RankedProduct]


# ---------------------------------------------------------------------------
# Linked output (after GeniusLink enrichment)
# ---------------------------------------------------------------------------

class LinkedProduct(BaseModel):
    rank: int
    asin: str
    title: str
    brand: str
    model: str
    full_name: str
    model_slug: str = ""
    bsr: Optional[int] = None
    review_count: Optional[int] = None
    rating: Optional[float] = None
    price_usd: Optional[float] = None
    image_url: Optional[str] = None
    detail_page_url: Optional[str] = None
    heat_score: float
    rank_change: str = "NEW"
    affiliate_url: str = ""
    # Google Trends ranking fields (carried from RankedProduct)
    trend_source: Optional[str] = None         # "rising" or "top"
    trend_match_type: Optional[str] = None     # "brand_model" or "brand_only"
    trend_query: Optional[str] = None          # Original Google Trends query
    trend_search_interest: Optional[int] = None
    selection_tier: int = 5                    # 1-5

    def model_post_init(self, __context) -> None:
        if not self.model_slug:
            self.model_slug = slugify(f"{self.brand} {self.model}")
        if not self.affiliate_url and self.detail_page_url:
            self.affiliate_url = self.detail_page_url


# ---------------------------------------------------------------------------
# Content generation output
# ---------------------------------------------------------------------------

class ProductContent(BaseModel):
    rank: int
    asin: str
    brand: str
    model: str
    full_name: str
    model_slug: str
    geniuslink_url: str = ""
    amazon_url: Optional[str] = None
    primary_image_url: Optional[str] = None
    image_alt: str = ""
    price_usd: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    bsr: Optional[int] = None
    heat_score: float
    rank_change: str = "NEW"
    best_for: str = ""
    why_hot: str
    short_specs: str


class FaqEntry(BaseModel):
    question: str
    answer: str


class WeeklyRoundup(BaseModel):
    slug: str
    category_id: str
    week_of: str
    weekly_id: str = ""
    h1_title: str
    meta_title: str
    meta_description: str
    intro: str
    methodology: str
    trend_insight: str
    hub_summary: str = ""
    faqs: list[FaqEntry]
    affiliate_disclosure: str = ""
    products: list[ProductContent]


class CatalogEntry(BaseModel):
    model_slug: str
    category_id: str
    brand: str
    model: str
    asin: str
    default_geniuslink_url: str = ""
    default_image_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

class RunLog(BaseModel):
    category_id: str
    week_of: str
    run_started_at: datetime
    run_completed_at: Optional[datetime] = None
    status: str = "in_progress"
    # Trends tracking
    trends_rising_count: int = 0
    trends_top_count: int = 0
    trends_supplemental_searches: int = 0
    trends_failed: bool = False
    # Signals tracking
    products_found: int = 0
    products_after_filter: int = 0
    products_ranked: int = 0
    geniuslink_cached: int = 0
    geniuslink_created: int = 0
    geniuslink_failed: int = 0
    airtable_roundup_written: bool = False
    airtable_rankings_written: int = 0
    airtable_catalog_upserted: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# State activity pipeline models
# ---------------------------------------------------------------------------

def state_runs_path(state: str, activity: str, filename: str | None = None) -> Path:
    """Return the path to a state activity run artifact directory or file.

    Creates the directory if it doesn't exist.
    """
    state_slug = slugify(state)
    base = Path(__file__).resolve().parent.parent / "runs" / "state-activities" / state_slug / activity
    base.mkdir(parents=True, exist_ok=True)
    if filename:
        return base / filename
    return base


def state_activity_as_category_config(
    state: str,
    config: "StateActivityConfig",
) -> "CategoryConfig":
    """Build a CategoryConfig adapter from a StateActivityConfig + state.

    This lets the existing signals_collector, ranker, and geniuslink_client
    modules work with state activity data without modification to their
    public interfaces. The synthetic category_id partitions artifacts and
    Airtable rows per state+activity.
    """
    state_slug = slugify(state)
    synthetic_id = f"state-{config.activity_id}-{state_slug}"
    return CategoryConfig(
        category_id=synthetic_id,
        display_name=config.display_name,
        site_name=config.site_name,
        site_url=config.site_url,
        gender="women",
        product_type=f"{config.activity_id} gear",
        search_index=config.search_index,
        browse_node_id="",
        keywords=config.keywords,
        min_reviews=config.min_reviews,
        min_rating=config.min_rating,
        price_min_usd=config.price_min_usd,
        price_max_usd=config.price_max_usd,
        slug_prefix=f"state-{config.activity_id}",
        table_roundups=config.table_activities,
        table_rankings=config.table_products,
        table_catalog="catalog",
        table_faq="faq",
        assoc_tag=config.assoc_tag,
        geniuslink_group_id=config.geniuslink_group_id,
        schedule="daily",
    )


class StateActivityConfig(BaseModel):
    activity_id: str
    display_name: str
    site_name: str
    site_url: str
    audience: str
    table_activities: str
    table_products: str
    geniuslink_group_id: str
    search_index: str
    keywords: str
    min_reviews: int
    min_rating: float
    price_min_usd: int
    price_max_usd: int
    research_sources: list[str]
    h2_section_pool: list[str]
    assoc_tag: str = ""


class ResearchFact(BaseModel):
    type: str  # location, season, wildlife, culture, event, permit, general
    name: Optional[str] = None
    detail: str
    source: str


class ResearchOutput(BaseModel):
    state: str
    activity: str
    sources_consulted: list[str]
    facts: list[ResearchFact]
    seasonal_notes: str = ""
    permit_info: str = ""
    cultural_notes: str = ""
    wildlife_notes: str = ""


def _no_em_dashes(v: str) -> str:
    """Reject any string containing em dashes."""
    if "\u2014" in v:
        raise ValueError("Em dashes (\u2014) are not allowed")
    return v


class StateArticle(BaseModel):
    slug: str
    activity: str
    state_filter: str
    parent_page_description: str
    parent_page_cta: str
    meta_title: str
    meta_description: str
    h1: str
    intro: str
    h2_1: str = ""
    h2_1_body: str = ""
    h2_2: str = ""
    h2_2_body: str = ""
    h2_3: str = ""
    h2_3_body: str = ""
    h2_4: str = ""
    h2_4_body: str = ""
    h2_5: str = ""
    h2_5_body: str = ""
    h2_6: str = ""
    h2_6_body: str = ""
    h2_7: str = ""
    h2_7_body: str = ""
    h2_8: str = ""
    h2_8_body: str = ""
    product1: str = "1"
    product2: str = "2"
    status: str = "Draft"

    @field_validator("meta_title")
    @classmethod
    def meta_title_length(cls, v: str) -> str:
        if len(v) > 65:
            raise ValueError(f"meta_title must be <= 65 chars, got {len(v)}")
        return _no_em_dashes(v)

    @field_validator("meta_description")
    @classmethod
    def meta_description_length(cls, v: str) -> str:
        if len(v) > 165:
            raise ValueError(f"meta_description must be <= 165 chars, got {len(v)}")
        return _no_em_dashes(v)

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: str) -> str:
        if not re.match(r"^[a-z]+-in-[a-z-]+$", v):
            raise ValueError(f"slug must match '{{activity}}-in-{{state-slug}}', got '{v}'")
        return v

    @field_validator(
        "h1", "intro", "parent_page_description", "parent_page_cta",
        "h2_1", "h2_1_body", "h2_2", "h2_2_body", "h2_3", "h2_3_body",
        "h2_4", "h2_4_body", "h2_5", "h2_5_body", "h2_6", "h2_6_body",
        "h2_7", "h2_7_body", "h2_8", "h2_8_body",
    )
    @classmethod
    def no_em_dashes_in_fields(cls, v: str) -> str:
        return _no_em_dashes(v)

    @field_validator("h2_1_body", "h2_2_body", "h2_3_body", "h2_4_body",
                     "h2_5_body", "h2_6_body", "h2_7_body", "h2_8_body")
    @classmethod
    def h2_body_requires_heading(cls, v: str, info) -> str:
        if not v:
            return v
        # Extract heading field name from body field name (e.g. h2_1_body -> h2_1)
        heading_field = info.field_name.replace("_body", "")
        heading_val = info.data.get(heading_field, "")
        if not heading_val:
            raise ValueError(f"{info.field_name} has content but {heading_field} is empty")
        return v


class StateActivityProduct(BaseModel):
    slug: str
    state: str
    activity: str
    image_url: str = ""
    image_alt_text: str = ""
    title: str
    description: str
    link_text: str = ""
    affiliate_link: str = ""
    asin: str = ""
    bsr: Optional[int] = None
    product_group: str = ""
    status: str = "Draft"

    @field_validator("title")
    @classmethod
    def title_length(cls, v: str) -> str:
        if len(v) > 65:
            raise ValueError(f"title must be <= 65 chars, got {len(v)}")
        return _no_em_dashes(v)

    @field_validator("description")
    @classmethod
    def description_length(cls, v: str) -> str:
        if len(v) > 165:
            # Truncate at last word boundary within limit, add ellipsis
            v = v[:162].rsplit(" ", 1)[0] + "..."
        return _no_em_dashes(v)


class StateActivityRunLog(BaseModel):
    state: str
    run_date: str
    run_started_at: datetime
    run_completed_at: Optional[datetime] = None
    status: str = "in_progress"
    activities: dict[str, dict] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None
