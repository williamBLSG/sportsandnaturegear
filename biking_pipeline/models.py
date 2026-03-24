"""Pydantic schemas — canonical data contract for the biking article pipeline.

All data shapes are defined here. No module defines its own ad-hoc dicts.

Biking pipeline differs from the weekly trending pipeline:
- Per-article configs (not per-category weekly roundups)
- 10 HTML widget slots in Airtable (Duda CMS widgets)
- Separate FAQ table with plain text (no HTML)
- Three workflows: daily article builder, Sunday price check, manual product refresh
- Composite scoring: 40% BSR, 25% brand authority, 20% Google Trends, 15% review quality
- Dynamic price terciles for budget/mid-range/premium tier assignment
- 5-7 products per comparison table (flexible)
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


def biking_runs_path(
    article_id: str,
    run_date: str,
    filename: str | None = None,
) -> Path:
    """Return the path to a biking run artifact directory or file.

    Creates the directory if it doesn't exist.
    Structure: runs/biking/{article_id}/{run_date}/
    """
    base = Path(__file__).resolve().parent.parent / "runs" / "biking" / article_id / run_date
    base.mkdir(parents=True, exist_ok=True)
    if filename:
        return base / filename
    return base


def biking_cache_path(filename: str | None = None) -> Path:
    """Return the path to the global biking cache directory.

    Structure: runs/biking/
    Used for GeniusLink ASIN cache shared across all articles.
    """
    base = Path(__file__).resolve().parent.parent / "runs" / "biking"
    base.mkdir(parents=True, exist_ok=True)
    if filename:
        return base / filename
    return base


# ---------------------------------------------------------------------------
# Article config (loaded from YAML in config/biking/)
# ---------------------------------------------------------------------------

class InternalLink(BaseModel):
    anchor: str
    slug: str


class BikingArticleConfig(BaseModel):
    article_id: str
    category_id: str  # Always "biking"
    display_name: str
    site_name: str
    site_url: str

    # Amazon Creators API
    search_index: str
    keywords: str
    min_reviews: int
    min_rating: float
    price_min_usd: int
    price_max_usd: int
    product_count_target: int  # 5-7 products per article
    top_brands: list[str] = Field(default_factory=list)

    # Affiliate
    assoc_tag: str
    geniuslink_group_id: str
    geniuslink_group_numeric_id: int

    # SEO & Page
    slug: str
    page_title: str
    meta_title: str
    meta_description: str
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    target_word_count_min: int
    target_word_count_max: int

    # Google Trends
    trends_keyword: str

    # Editorial
    editorial_notes: str = ""

    # Airtable
    airtable_base_id: str  # Resolved from ${AIRTABLE_SOFTBALL_BASE_ID}
    table_articles: str  # "biking-articles"
    table_products: str  # "biking-products"
    table_faq: str  # "biking-faq"

    # Internal links
    internal_links: list[InternalLink] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Raw signals (from Amazon Creators API)
# ---------------------------------------------------------------------------

class BikingRawProduct(BaseModel):
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


class BikingRawSignals(BaseModel):
    article_id: str
    category_id: str
    collected_at: datetime
    search_keywords: str
    total_api_results: int
    products_before_filter: int
    products_after_filter: int
    products: list[BikingRawProduct]


# ---------------------------------------------------------------------------
# Google Trends data
# ---------------------------------------------------------------------------

class BikingTrendsQuery(BaseModel):
    query: str
    search_interest: int  # 0-100 relative score
    increase_percent: Optional[str] = None
    source: str  # "rising" or "top"
    query_type: str  # "brand_model", "brand_only", "generic"
    normalized_brand: Optional[str] = None
    normalized_model: Optional[str] = None


class BikingTrendsData(BaseModel):
    article_id: str
    collected_at: datetime
    trends_keyword: str
    rising_queries: list[BikingTrendsQuery]
    top_queries: list[BikingTrendsQuery]


# ---------------------------------------------------------------------------
# Ranked output (after normalization, dedup, scoring)
# ---------------------------------------------------------------------------

class BikingRankedProduct(BaseModel):
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
    # Composite score (40% BSR, 25% brand auth, 20% trends, 15% review quality)
    composite_score: float
    # Individual score components for transparency
    bsr_score: float = 0.0
    brand_authority_score: float = 0.0
    trends_score: float = 0.0
    review_quality_score: float = 0.0
    # Price tier (assigned after scoring via dynamic terciles)
    price_tier: str = ""  # "budget", "mid-range", "premium"
    # Role assignment
    role: str = ""  # "top_pick", "budget_pick", "midrange_pick", "premium_pick", "comparison"
    # Google Trends fields
    trend_source: Optional[str] = None
    trend_match_type: Optional[str] = None
    trend_query: Optional[str] = None
    trend_search_interest: Optional[int] = None
    # Brand authority flag
    is_top_brand: bool = False


class BikingRankedOutput(BaseModel):
    article_id: str
    category_id: str
    ranked_at: datetime
    product_count: int
    products: list[BikingRankedProduct]
    # Price tier boundaries for transparency
    budget_ceiling: Optional[float] = None
    premium_floor: Optional[float] = None


# ---------------------------------------------------------------------------
# Linked output (after GeniusLink enrichment)
# ---------------------------------------------------------------------------

class BikingLinkedProduct(BaseModel):
    """RankedProduct + affiliate URL and model_slug."""
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
    composite_score: float
    bsr_score: float = 0.0
    brand_authority_score: float = 0.0
    trends_score: float = 0.0
    review_quality_score: float = 0.0
    price_tier: str = ""
    role: str = ""
    trend_source: Optional[str] = None
    trend_match_type: Optional[str] = None
    trend_query: Optional[str] = None
    trend_search_interest: Optional[int] = None
    is_top_brand: bool = False
    affiliate_url: str = ""

    def model_post_init(self, __context) -> None:
        if not self.model_slug:
            self.model_slug = slugify(f"{self.brand} {self.model}")
        if not self.affiliate_url and self.detail_page_url:
            self.affiliate_url = self.detail_page_url


# ---------------------------------------------------------------------------
# Content generation output — 10 Duda widget slots
# ---------------------------------------------------------------------------

class BikingProductContent(BaseModel):
    """Content for one product in the article."""
    rank: int
    asin: str
    brand: str
    model: str
    full_name: str
    model_slug: str
    price_usd: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    bsr: Optional[int] = None
    composite_score: float
    price_tier: str
    role: str
    affiliate_url: str = ""
    image_url: Optional[str] = None
    image_alt: str = ""
    best_for: str = ""
    editorial_blurb: str = ""  # 2-3 sentence product description
    standout_feature: str = ""
    # Duda list widget fields (for Top Pick, Budget, Mid-Range, Premium)
    list_title: str = ""  # e.g. "Top Pick: Easton Moxie"
    list_description: str = ""  # Rich text (no HTML) — price, rating, editorial paragraph
    list_cta_text: str = ""  # e.g. "Check price on Amazon"


class BikingFaqEntry(BaseModel):
    """FAQ entry — plain text only (no HTML). Rendered by separate Duda widget."""
    question: str
    answer: str  # PLAIN TEXT ONLY — no HTML tags
    sort_order: int = 0


class BikingArticleContent(BaseModel):
    """Full article output — maps to 10 widget slots in Airtable."""
    article_id: str
    category_id: str

    # Widget 1: Intro paragraphs (NO H1 — Duda handles the H1 natively)
    widget_1: str  # HTML blob: intro paragraphs only, no heading

    # Widget 2: "Why You Need This Gear" section
    widget_2: str  # HTML blob

    # Widget 3: "Top Features to Look For" section
    widget_3: str  # HTML blob

    # Widget 4: Comparison table — "Our Top Picks at a Glance"
    widget_4: str  # HTML blob: table with inline CSS

    # Widget 5: "Our Top Pick" styled card (green badge, blurb, CTA button)
    widget_5: str  # HTML blob with inline CSS

    # Widget 6: "The Three Tiers" — budget / mid-range / premium side-by-side cards
    widget_6: str  # HTML blob with inline CSS

    # Widget 7: "How to Choose" buying guidance
    widget_7: str  # HTML blob

    # Widget 8: Final thoughts + CTA
    widget_8: str  # HTML blob

    # Widget 9: Reserved / additional content
    widget_9: str = ""  # HTML blob (optional)

    # Widget 10: Reserved / additional content
    widget_10: str = ""  # HTML blob (optional)

    # SEO fields
    meta_title: str
    meta_description: str

    # Social
    bluesky_posts: list[str] = Field(default_factory=list)  # 3 posts
    pinterest_pins: list[dict] = Field(default_factory=list)  # per product

    # Product data for Airtable product table
    products: list[BikingProductContent]

    # FAQ entries for Airtable FAQ table (plain text only)
    faqs: list[BikingFaqEntry]

    # ASIN role assignments for article table
    top_pick_asin: str = ""
    budget_asin: str = ""
    midrange_asin: str = ""
    premium_asin: str = ""
    comparison_asins: str = ""  # Comma-separated


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

class BikingRunLog(BaseModel):
    article_id: str
    category_id: str
    run_date: str
    run_type: str  # "daily_build", "price_check", "manual_refresh"
    run_started_at: datetime
    run_completed_at: Optional[datetime] = None
    status: str = "in_progress"
    # Signals
    products_found: int = 0
    products_after_filter: int = 0
    products_ranked: int = 0
    # Trends
    trends_rising_count: int = 0
    trends_top_count: int = 0
    trends_failed: bool = False
    # GeniusLink
    geniuslink_cached: int = 0
    geniuslink_created: int = 0
    geniuslink_failed: int = 0
    # Content
    widgets_generated: int = 0
    faqs_generated: int = 0
    # Airtable
    airtable_article_written: bool = False
    airtable_products_written: int = 0
    airtable_faqs_written: int = 0
    # Price check specific
    prices_checked: int = 0
    prices_changed: int = 0
    widgets_regenerated: int = 0
    # Errors
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None
