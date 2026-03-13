"""Airtable client — upserts softball article content, products, and FAQs.

Writes to three tables in the softball Airtable base:
  1. softball-articles  — article content (10 HTML widgets, SEO, ASIN assignments)
  2. softball-products   — per-product data (editorial blurbs, affiliate links)
  3. softball-faq        — FAQ entries (plain text only, no HTML)

All writes are upserts keyed by article_id (articles) or composite keys
(products: article_id + asin, faqs: article_id + sort_order).

Airtable schema (field names from existing base):
  softball-articles (tbl4p48LV1YDJ7teu):
    article_id, category_id, slug, page_title, meta_title, meta_description,
    primary_keyword, secondary_keywords, target_word_count_min, target_word_count_max,
    editorial_notes, widget_1..widget_10, top_pick_asin, budget_asin, midrange_asin,
    premium_asin, comparison_asins, build_date, last_refresh, published_url

  softball-products (tbl1CmZmPHhLSF9Pk):
    product_id, article_id, asin, role, brand, model, price_usd, rating,
    review_count, bsr, image_url, affiliate_url, best_for, standout_feature,
    editorial_blurb, last_updated

  softball-faq (tblFIRrFz1SKCxVeU):
    faq_id, article_id, question, answer, sort_order
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from pyairtable import Api

from softball_pipeline.models import (
    SoftballArticleConfig,
    SoftballArticleContent,
)

logger = logging.getLogger(__name__)


class AirtableClientError(Exception):
    pass


def _strip_html(html: str) -> str:
    """Strip HTML tags for display-only fields."""
    return re.sub(r"<[^>]+>", "", html).strip()


def _get_api() -> Api:
    """Create Airtable API client."""
    token = os.environ.get("AIRTABLE_ACCESS_TOKEN")
    if not token:
        raise AirtableClientError("AIRTABLE_ACCESS_TOKEN must be set")
    return Api(token)


# ---------------------------------------------------------------------------
# Article upsert
# ---------------------------------------------------------------------------

def _upsert_article(
    content: SoftballArticleContent,
    config: SoftballArticleConfig,
    run_date: str,
    api: Api,
) -> None:
    """Upsert the article row in softball-articles table.

    Keyed by article_id (unique per article).
    """
    table = api.table(config.airtable_base_id, config.table_articles)

    now_iso = datetime.now(timezone.utc).isoformat()

    fields = {
        "article_id": config.article_id,
        "category_id": config.category_id,
        "slug": config.slug,
        "page_title": config.page_title,
        "meta_title": content.meta_title,
        "meta_description": content.meta_description,
        "primary_keyword": config.primary_keyword,
        "secondary_keywords": ", ".join(config.secondary_keywords),
        "target_word_count_min": config.target_word_count_min,
        "target_word_count_max": config.target_word_count_max,
        "editorial_notes": config.editorial_notes,
        # 10 HTML widget slots
        "widget_1": content.widget_1,
        "widget_2": content.widget_2,
        "widget_3": content.widget_3,
        "widget_4": content.widget_4,
        "widget_5": content.widget_5,
        "widget_6": content.widget_6,
        "widget_7": content.widget_7,
        "widget_8": content.widget_8,
        "widget_9": content.widget_9,
        "widget_10": content.widget_10,
        # ASIN role assignments
        "top_pick_asin": content.top_pick_asin,
        "budget_asin": content.budget_asin,
        "midrange_asin": content.midrange_asin,
        "premium_asin": content.premium_asin,
        "comparison_asins": content.comparison_asins,
        # Dates
        "build_date": run_date,
        "last_refresh": now_iso,
    }

    # Upsert by article_id
    existing = table.all(formula=f"{{article_id}}='{config.article_id}'")

    if existing:
        record_id = existing[0]["id"]
        table.update(record_id, fields)
        logger.info("Updated article row: %s", config.article_id)
    else:
        table.create(fields)
        logger.info("Created article row: %s", config.article_id)


# ---------------------------------------------------------------------------
# Products upsert
# ---------------------------------------------------------------------------

def _upsert_products(
    content: SoftballArticleContent,
    config: SoftballArticleConfig,
    api: Api,
) -> int:
    """Upsert product rows in softball-products table.

    Keyed by article_id + asin (composite key via product_id field).
    Deletes stale products from previous runs first.
    """
    table = api.table(config.airtable_base_id, config.table_products)

    now_iso = datetime.now(timezone.utc).isoformat()

    # Find existing products for this article
    existing = table.all(formula=f"{{article_id}}='{config.article_id}'")
    existing_by_asin = {r["fields"].get("asin"): r["id"] for r in existing}

    # Current ASINs
    current_asins = {p.asin for p in content.products}

    # Delete stale products (no longer in current run)
    stale_ids = [
        rec_id for asin, rec_id in existing_by_asin.items()
        if asin not in current_asins
    ]
    if stale_ids:
        table.batch_delete(stale_ids)
        logger.info("Deleted %d stale products for %s", len(stale_ids), config.article_id)

    # Upsert current products
    written = 0
    for product in content.products:
        product_id = f"{config.article_id}-{product.asin}"

        fields = {
            "product_id": product_id,
            "article_id": config.article_id,
            "asin": product.asin,
            "role": product.role,
            "brand": product.brand,
            "model": product.model,
            "price_usd": product.price_usd,
            "rating": product.rating,
            "review_count": product.review_count,
            "bsr": product.bsr,
            "image_url": product.image_url or "",
            "affiliate_url": product.affiliate_url,
            "best_for": product.best_for,
            "standout_feature": product.standout_feature,
            "editorial_blurb": product.editorial_blurb,
            "last_updated": now_iso,
        }

        if product.asin in existing_by_asin:
            table.update(existing_by_asin[product.asin], fields)
        else:
            table.create(fields)
        written += 1

    logger.info("Upserted %d products for %s", written, config.article_id)
    return written


# ---------------------------------------------------------------------------
# FAQ upsert
# ---------------------------------------------------------------------------

def _upsert_faqs(
    content: SoftballArticleContent,
    config: SoftballArticleConfig,
    api: Api,
) -> int:
    """Upsert FAQ rows in softball-faq table.

    Keyed by faq_id (article_id + sort_order).
    FAQs are PLAIN TEXT only — no HTML tags.
    Deletes stale FAQs from previous runs first.
    """
    table = api.table(config.airtable_base_id, config.table_faq)

    # Find existing FAQs for this article
    existing = table.all(formula=f"{{article_id}}='{config.article_id}'")
    existing_by_faq_id = {r["fields"].get("faq_id"): r["id"] for r in existing}

    # Current FAQ IDs
    current_faq_ids = set()
    for faq in content.faqs:
        faq_id = f"{config.article_id}-faq-{faq.sort_order}"
        current_faq_ids.add(faq_id)

    # Delete stale FAQs
    stale_ids = [
        rec_id for faq_id, rec_id in existing_by_faq_id.items()
        if faq_id not in current_faq_ids
    ]
    if stale_ids:
        table.batch_delete(stale_ids)
        logger.info("Deleted %d stale FAQs for %s", len(stale_ids), config.article_id)

    # Upsert current FAQs
    written = 0
    for faq in content.faqs:
        faq_id = f"{config.article_id}-faq-{faq.sort_order}"

        fields = {
            "faq_id": faq_id,
            "article_id": config.article_id,
            "question": faq.question,
            "answer": faq.answer,  # PLAIN TEXT — no HTML
            "sort_order": faq.sort_order,
        }

        if faq_id in existing_by_faq_id:
            table.update(existing_by_faq_id[faq_id], fields)
        else:
            table.create(fields)
        written += 1

    logger.info("Upserted %d FAQs for %s", written, config.article_id)
    return written


# ---------------------------------------------------------------------------
# Row count validation
# ---------------------------------------------------------------------------

def _validate_row_counts(
    content: SoftballArticleContent,
    config: SoftballArticleConfig,
    api: Api,
    expected_products: int,
    expected_faqs: int,
) -> None:
    """Validate that the expected number of rows exist after writes."""

    # Article row
    articles_table = api.table(config.airtable_base_id, config.table_articles)
    article_rows = articles_table.all(formula=f"{{article_id}}='{config.article_id}'")
    if len(article_rows) != 1:
        raise AirtableClientError(
            f"Expected 1 article row for '{config.article_id}', found {len(article_rows)}"
        )

    # Product rows
    products_table = api.table(config.airtable_base_id, config.table_products)
    product_rows = products_table.all(formula=f"{{article_id}}='{config.article_id}'")
    if len(product_rows) != expected_products:
        raise AirtableClientError(
            f"Expected {expected_products} product rows for '{config.article_id}', "
            f"found {len(product_rows)}"
        )

    # FAQ rows
    faq_table = api.table(config.airtable_base_id, config.table_faq)
    faq_rows = faq_table.all(formula=f"{{article_id}}='{config.article_id}'")
    if len(faq_rows) != expected_faqs:
        raise AirtableClientError(
            f"Expected {expected_faqs} FAQ rows for '{config.article_id}', "
            f"found {len(faq_rows)}"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def write(
    content: SoftballArticleContent,
    config: SoftballArticleConfig,
    run_date: str,
) -> dict:
    """Write article content, products, and FAQs to Airtable.

    Returns a summary dict for the run log.
    """
    api = _get_api()

    # Upsert article
    _upsert_article(content, config, run_date, api)

    # Upsert products
    products_written = _upsert_products(content, config, api)

    # Upsert FAQs
    faqs_written = _upsert_faqs(content, config, api)

    # Validate row counts
    _validate_row_counts(
        content, config, api,
        expected_products=len(content.products),
        expected_faqs=len(content.faqs),
    )

    logger.info(
        "Airtable write complete: article=%s, products=%d, faqs=%d",
        config.article_id, products_written, faqs_written,
    )

    return {
        "airtable_article_written": True,
        "airtable_products_written": products_written,
        "airtable_faqs_written": faqs_written,
    }


# ---------------------------------------------------------------------------
# Price check helpers (for Sunday workflow)
# ---------------------------------------------------------------------------

def update_product_price(
    config: SoftballArticleConfig,
    asin: str,
    new_price: float,
) -> None:
    """Update a single product's price in softball-products table."""
    api = _get_api()
    table = api.table(config.airtable_base_id, config.table_products)

    existing = table.all(
        formula=f"AND({{article_id}}='{config.article_id}', {{asin}}='{asin}')"
    )

    if not existing:
        logger.warning("Product not found for price update: %s / %s", config.article_id, asin)
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    table.update(existing[0]["id"], {
        "price_usd": new_price,
        "last_updated": now_iso,
    })
    logger.info("Updated price for %s: $%.2f", asin, new_price)


def update_article_widget(
    config: SoftballArticleConfig,
    widget_number: int,
    html: str,
) -> None:
    """Update a single widget HTML in softball-articles table."""
    api = _get_api()
    table = api.table(config.airtable_base_id, config.table_articles)

    existing = table.all(formula=f"{{article_id}}='{config.article_id}'")

    if not existing:
        logger.warning("Article not found for widget update: %s", config.article_id)
        return

    field_name = f"widget_{widget_number}"
    now_iso = datetime.now(timezone.utc).isoformat()
    table.update(existing[0]["id"], {
        field_name: html,
        "last_refresh": now_iso,
    })
    logger.info("Updated %s for %s", field_name, config.article_id)


def get_current_products(
    config: SoftballArticleConfig,
) -> list[dict]:
    """Fetch current products for an article from Airtable.

    Used by the Sunday price check workflow.
    """
    api = _get_api()
    table = api.table(config.airtable_base_id, config.table_products)

    rows = table.all(formula=f"{{article_id}}='{config.article_id}'")
    return [r["fields"] for r in rows]
