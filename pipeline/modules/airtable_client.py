"""Airtable client — upserts weekly roundup, rankings, catalog, and FAQ entries.

All Airtable interaction happens in this module. No other module imports
the Airtable SDK or constructs API requests.

Table schema (as of 2026-02-18):
- weekly_roundups: slug (PK), category_id, week_of (date), weekly_id,
    h1_title, meta_title, meta_description, intro, methodology,
    trend_insight, hub_summary, affiliate_disclosure, status (select),
    created_at (auto)
- weekly_rankings: slug (PK), category_id, week_of (date), weekly_id,
    rank, rank_change, brand, model, model_slug, full_name,
    display_title, display_detail, cta_text,
    category_tags (multiselect), best_for, why_hot, heat_score, bsr,
    review_count, rating, price_usd (currency), asin, amazon_url (url),
    geniuslink_url (url), primary_image_url (url), image_alt,
    short_specs, roundup_slug
- catalog: model_slug (PK), category_id, brand, model, asin,
    first_seen (date), last_seen (date), appearances,
    default_geniuslink_url (url), default_image_url (url), evergreen_blurb
- faq: slug (PK), weekly_id, question, answer, category_id, roundup_slug
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date

from pyairtable import Api
from pyairtable.formulas import EQUAL, FIELD, STR_VALUE

from pipeline.models import (
    CategoryConfig,
    FaqEntry,
    LinkedProduct,
    ProductContent,
    WeeklyRoundup,
    slugify,
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
        "weekly_id": roundup.weekly_id,
        "h1_title": roundup.h1_title,
        "meta_title": roundup.meta_title,
        "meta_description": roundup.meta_description,
        "intro": roundup.intro,
        "methodology": roundup.methodology,
        "trend_insight": roundup.trend_insight,
        "hub_summary": roundup.hub_summary,
        "affiliate_disclosure": roundup.affiliate_disclosure,
    }


def _ranking_fields(
    product: ProductContent,
    roundup: WeeklyRoundup,
) -> dict:
    """Build Airtable fields dict for a weekly ranking row."""
    slug = f"{roundup.week_of}-{roundup.category_id}-{product.model_slug}"

    # Composite fields for list widget display
    display_title = f"#{product.rank} / {product.rank_change} | {product.full_name}"

    def _strip_html(text: str) -> str:
        # Replace block-level closing tags with double newlines for paragraph breaks
        text = re.sub(r"</(?:p|div)>", "\n\n", text)
        text = re.sub(r"</(?:li|ul|ol)>", "\n", text)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Collapse 3+ newlines to 2, then strip
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    detail_parts = []
    if product.best_for:
        detail_parts.append(f"Best for: {_strip_html(product.best_for)}")
    if product.why_hot:
        detail_parts.append(_strip_html(product.why_hot))
    # Structured stats list
    stats = []
    stats.append(f"Heat Score: {product.heat_score}")
    is_new = product.rank_change == "NEW"
    stats.append(f"New this Week: {'Yes' if is_new else 'No'}")
    if product.bsr is not None:
        stats.append(f"ABSR: #{product.bsr:,}")
    if product.price_usd is not None:
        stats.append(f"Price: ${product.price_usd:.2f}")
    detail_parts.append("\n".join(stats))
    display_detail = "\n\n".join(detail_parts)

    cta_text = f"Shop {product.brand} {product.model} on Amazon →"

    fields: dict = {
        "slug": slug,
        "category_id": roundup.category_id,
        "week_of": roundup.week_of,
        "weekly_id": roundup.weekly_id,
        "rank": product.rank,
        "rank_change": product.rank_change,
        "brand": product.brand,
        "model": product.model,
        "model_slug": product.model_slug,
        "full_name": product.full_name,
        "display_title": display_title,
        "display_detail": display_detail,
        "best_for": product.best_for,
        "why_hot": product.why_hot,
        "heat_score": product.heat_score,
        "asin": product.asin,
        "short_specs": product.short_specs,
        "roundup_slug": roundup.slug,
        "image_alt": product.image_alt,
    }
    fields["cta_text"] = cta_text
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


def _faq_slug(weekly_id: str, category_id: str, question: str) -> str:
    """Build a deterministic slug for a FAQ row."""
    # Use first 50 chars of slugified question to keep it readable but bounded
    q_slug = slugify(question)[:50].rstrip("-")
    return f"{weekly_id}-{category_id}-{q_slug}"


def _faq_fields(
    faq: FaqEntry,
    roundup: WeeklyRoundup,
    config: CategoryConfig,
) -> dict:
    """Build Airtable fields dict for a FAQ row."""
    return {
        "slug": _faq_slug(roundup.weekly_id, config.category_id, faq.question),
        "weekly_id": roundup.weekly_id,
        "question": faq.question,
        "answer": faq.answer,
        "category_id": config.category_id,
        "roundup_slug": roundup.slug,
    }


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

        # Delete stale ranking rows for this roundup (handles re-runs where
        # the product list changed, e.g. after filter updates)
        new_slugs = {
            f"{roundup.week_of}-{roundup.category_id}-{p.model_slug}"
            for p in roundup.products
        }
        existing = rankings_table.all(
            formula=EQUAL(FIELD("roundup_slug"), STR_VALUE(roundup.slug))
        )
        stale = [r["id"] for r in existing if r["fields"].get("slug") not in new_slugs]
        if stale:
            logger.info("Deleting %d stale ranking rows", len(stale))
            rankings_table.batch_delete(stale)

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

        # --- Upsert FAQ entries ---
        faq_table = api.table(base_id, config.table_faq)

        # Delete stale FAQ rows for this roundup
        new_faq_slugs = {
            _faq_slug(roundup.weekly_id, config.category_id, faq.question)
            for faq in roundup.faqs
        }
        existing_faqs = faq_table.all(
            formula=EQUAL(FIELD("roundup_slug"), STR_VALUE(roundup.slug))
        )
        stale_faqs = [
            r["id"] for r in existing_faqs
            if r["fields"].get("slug") not in new_faq_slugs
        ]
        if stale_faqs:
            logger.info("Deleting %d stale FAQ rows", len(stale_faqs))
            faq_table.batch_delete(stale_faqs)

        faq_records = []
        for faq in roundup.faqs:
            faq_records.append({
                "fields": _faq_fields(faq, roundup, config),
            })

        if faq_records:
            logger.info("Upserting %d FAQ entries", len(faq_records))
            faq_table.batch_upsert(
                faq_records,
                key_fields=["slug"],
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

    # Check FAQ rows
    if roundup.faqs:
        faq_table = api.table(base_id, config.table_faq)
        faq_rows = faq_table.all(
            formula=EQUAL(FIELD("roundup_slug"), STR_VALUE(roundup.slug))
        )
        assert len(faq_rows) == len(roundup.faqs), (
            f"Expected {len(roundup.faqs)} FAQ rows for "
            f"roundup_slug '{roundup.slug}', found {len(faq_rows)}"
        )
