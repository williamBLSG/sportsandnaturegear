"""Airtable client — upserts weekly roundup, rankings, and catalog entries.

All Airtable interaction happens in this module. No other module imports
the Airtable SDK or constructs API requests.

Table schema (as of 2026-02-18):
- weekly_roundups: slug (PK), category_id, week_of (date), h1_title,
    meta_title, meta_description, intro, methodology, trend_insight, faqs,
    affiliate_disclosure, status (select), created_at (auto)
- weekly_rankings: slug (PK), category_id, week_of (date), rank, rank_change,
    brand, model, model_slug, full_name, category_tags (multiselect),
    best_for, why_hot, heat_score, bsr, review_count, rating,
    price_usd (currency), asin, amazon_url (url), geniuslink_url (url),
    primary_image_url (url), image_alt, short_specs, roundup_slug
- catalog: model_slug (PK), category_id, brand, model, asin,
    first_seen (date), last_seen (date), appearances,
    default_geniuslink_url (url), default_image_url (url), evergreen_blurb
"""

from __future__ import annotations

import logging
import os
from datetime import date

from pyairtable import Api
from pyairtable.formulas import EQUAL, FIELD, STR_VALUE

from pipeline.models import (
    CategoryConfig,
    LinkedProduct,
    ProductContent,
    WeeklyRoundup,
)

logger = logging.getLogger(__name__)


class AirtableClientError(Exception):
    pass


def _get_api() -> tuple[Api, str]:
    """Create Airtable API client from env vars."""
    token = os.environ.get("AIRTABLE_ACCESS_TOKEN")
    base_id = os.environ.get("AIRTABLE_BASE_ID")

    if not token:
        raise AirtableClientError("AIRTABLE_ACCESS_TOKEN must be set")
    if not base_id:
        raise AirtableClientError("AIRTABLE_BASE_ID must be set")

    return Api(token), base_id


def _roundup_fields(roundup: WeeklyRoundup) -> dict:
    """Build Airtable fields dict for a weekly roundup row."""
    return {
        "slug": roundup.slug,
        "category_id": roundup.category_id,
        "week_of": roundup.week_of,
        "h1_title": roundup.h1_title,
        "meta_title": roundup.meta_title,
        "meta_description": roundup.meta_description,
        "intro": roundup.intro,
        "methodology": roundup.methodology,
        "trend_insight": roundup.trend_insight,
        "faqs": roundup.faqs,
        "affiliate_disclosure": roundup.affiliate_disclosure,
    }


def _ranking_fields(
    product: ProductContent,
    roundup: WeeklyRoundup,
) -> dict:
    """Build Airtable fields dict for a weekly ranking row."""
    slug = f"{roundup.week_of}-{roundup.category_id}-{product.model_slug}"
    fields: dict = {
        "slug": slug,
        "category_id": roundup.category_id,
        "week_of": roundup.week_of,
        "rank": product.rank,
        "rank_change": product.rank_change,
        "brand": product.brand,
        "model": product.model,
        "model_slug": product.model_slug,
        "full_name": product.full_name,
        "best_for": product.best_for,
        "why_hot": product.why_hot,
        "heat_score": product.heat_score,
        "asin": product.asin,
        "short_specs": product.short_specs,
        "roundup_slug": roundup.slug,
        "image_alt": product.image_alt,
    }
    # Only set numeric fields if they have values (Airtable rejects null for number/currency)
    if product.bsr is not None:
        fields["bsr"] = product.bsr
    if product.review_count is not None:
        fields["review_count"] = product.review_count
    if product.rating is not None:
        fields["rating"] = product.rating
    if product.price_usd is not None:
        fields["price_usd"] = product.price_usd
    # URL fields — only set if non-empty
    if product.amazon_url:
        fields["amazon_url"] = product.amazon_url
    if product.geniuslink_url:
        fields["geniuslink_url"] = product.geniuslink_url
    if product.primary_image_url:
        fields["primary_image_url"] = product.primary_image_url
    return fields


def _catalog_fields(
    product: ProductContent,
    roundup: WeeklyRoundup,
    linked: LinkedProduct | None,
) -> dict:
    """Build Airtable fields dict for a catalog row."""
    fields: dict = {
        "model_slug": product.model_slug,
        "category_id": roundup.category_id,
        "brand": product.brand,
        "model": product.model,
        "asin": product.asin,
        "last_seen": roundup.week_of,
    }
    geniuslink = product.geniuslink_url or (linked.affiliate_url if linked else "")
    image = product.primary_image_url or (linked.image_url if linked else "")
    if geniuslink:
        fields["default_geniuslink_url"] = geniuslink
    if image:
        fields["default_image_url"] = image
    return fields


def write(
    roundup: WeeklyRoundup,
    config: CategoryConfig,
    linked_products: list[LinkedProduct] | None = None,
) -> None:
    """Write roundup, rankings, and catalog entries to Airtable.

    All writes are upserts keyed on the primary key field.
    Every row includes category_id.

    Raises AirtableClientError on write failure or row count mismatch.
    """
    api, base_id = _get_api()

    # Build lookup for linked product data
    linked_lookup: dict[str, LinkedProduct] = {}
    if linked_products:
        for lp in linked_products:
            linked_lookup[lp.asin] = lp

    try:
        # --- Upsert weekly_roundups ---
        roundups_table = api.table(base_id, config.table_roundups)
        roundup_record = {"fields": _roundup_fields(roundup)}

        logger.info("Upserting roundup: %s", roundup.slug)
        roundups_table.batch_upsert(
            [roundup_record],
            key_fields=["slug"],
            replace=True,
        )

        # --- Upsert weekly_rankings ---
        rankings_table = api.table(base_id, config.table_rankings)
        ranking_records = []
        for product in roundup.products:
            ranking_records.append({
                "fields": _ranking_fields(product, roundup),
            })

        logger.info("Upserting %d rankings", len(ranking_records))
        rankings_table.batch_upsert(
            ranking_records,
            key_fields=["slug"],
            replace=True,
        )

        # --- Upsert catalog ---
        catalog_table = api.table(base_id, config.table_catalog)
        catalog_records = []
        for product in roundup.products:
            lp = linked_lookup.get(product.asin)
            catalog_records.append({
                "fields": _catalog_fields(product, roundup, lp),
            })

        logger.info("Upserting %d catalog entries", len(catalog_records))
        catalog_table.batch_upsert(
            catalog_records,
            key_fields=["model_slug"],
            replace=True,
        )

    except AirtableClientError:
        raise
    except Exception as e:
        raise AirtableClientError(f"Airtable write failed: {e}") from e

    # --- Post-write validation ---
    try:
        _validate_row_counts(api, base_id, config, roundup)
    except AssertionError as e:
        raise AirtableClientError(f"Row count validation failed: {e}") from e

    logger.info(
        "Airtable writes complete: 1 roundup, %d rankings, %d catalog",
        len(roundup.products), len(roundup.products),
    )


def _validate_row_counts(
    api: Api,
    base_id: str,
    config: CategoryConfig,
    roundup: WeeklyRoundup,
) -> None:
    """Validate expected row counts after writes."""
    # Check roundups
    roundups_table = api.table(base_id, config.table_roundups)
    roundup_rows = roundups_table.all(
        formula=EQUAL(FIELD("slug"), STR_VALUE(roundup.slug))
    )
    assert len(roundup_rows) >= 1, (
        f"Expected at least 1 roundup row for slug '{roundup.slug}', "
        f"found {len(roundup_rows)}"
    )

    # Check rankings for this roundup via roundup_slug (text field, avoids date comparison issues)
    rankings_table = api.table(base_id, config.table_rankings)
    rankings_rows = rankings_table.all(
        formula=EQUAL(FIELD("roundup_slug"), STR_VALUE(roundup.slug))
    )
    assert len(rankings_rows) == len(roundup.products), (
        f"Expected {len(roundup.products)} ranking rows for "
        f"roundup_slug '{roundup.slug}', found {len(rankings_rows)}"
    )
