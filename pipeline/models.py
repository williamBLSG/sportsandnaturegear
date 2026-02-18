"""Pydantic schemas — canonical data contract for the pipeline.

All data shapes are defined here. No module defines its own ad-hoc dicts.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


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
    assoc_tag: str
    geniuslink_group_id: str
    schedule: str


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


class RankedOutput(BaseModel):
    category_id: str
    week_of: str
    ranked_at: datetime
    product_count: int
    products: list[RankedProduct]


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

class RunLog(BaseModel):
    category_id: str
    week_of: str
    run_started_at: datetime
    run_completed_at: Optional[datetime] = None
    status: str = "in_progress"
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
