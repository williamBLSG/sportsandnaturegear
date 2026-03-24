"""Signals collector — queries Amazon Creators API for biking products.

Reuses the same Creators API pattern as the weekly trending pipeline but
with biking-specific config (SportingGoods search index, no browse node,
no gender filter).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from biking_pipeline.models import (
    BikingArticleConfig,
    BikingRawProduct,
    BikingRawSignals,
    biking_runs_path,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds


class SignalsCollectorError(Exception):
    pass


def _retry(fn, retries=MAX_RETRIES, should_retry=lambda e: True):
    """Retry a function with exponential backoff on transient errors."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1 and should_retry(e):
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Attempt %d failed: %s. Retrying in %ds...", attempt + 1, e, wait)
                time.sleep(wait)
            else:
                raise


def _is_rate_limit_error(e: Exception) -> bool:
    """Check if an exception is a rate limit / throttling error."""
    msg = str(e).lower()
    return "rate limit" in msg or "throttl" in msg


def _get_api_client(config: BikingArticleConfig):
    """Build an AmazonCreatorsApi client from env vars + config."""
    from amazon_creatorsapi import AmazonCreatorsApi, Country

    access_key = os.environ.get("AMZ_CREATORS_ACCESS_KEY")
    secret_key = os.environ.get("AMZ_CREATORS_SECRET_KEY")

    if not access_key or not secret_key:
        raise SignalsCollectorError(
            "AMZ_CREATORS_ACCESS_KEY and AMZ_CREATORS_SECRET_KEY must be set"
        )

    return AmazonCreatorsApi(
        credential_id=access_key,
        credential_secret=secret_key,
        version="2.1",
        tag=config.assoc_tag,
        country=Country.US,
        throttling=1,
    )


def _extract_product(item) -> BikingRawProduct | None:
    """Extract a BikingRawProduct from an Amazon Creators API result item.

    The SDK returns objects with attribute access, not dicts.
    Returns None if the item is missing critical data (ASIN or title).
    """
    try:
        asin = item.asin
        if not asin:
            return None

        # Title
        title = None
        if item.item_info and item.item_info.title:
            title = item.item_info.title.display_value
        if not title:
            return None

        # Brand
        brand = None
        if item.item_info and item.item_info.by_line_info and item.item_info.by_line_info.brand:
            brand = item.item_info.by_line_info.brand.display_value

        # BSR (best sellers rank)
        bsr = None
        if item.browse_node_info and item.browse_node_info.website_sales_rank:
            bsr = item.browse_node_info.website_sales_rank.sales_rank

        # Reviews
        review_count = None
        rating = None
        if item.customer_reviews:
            review_count = item.customer_reviews.count
            if item.customer_reviews.star_rating:
                rating = item.customer_reviews.star_rating.value

        # Price
        price_usd = None
        if item.offers_v2 and item.offers_v2.listings:
            listing = item.offers_v2.listings[0]
            if listing.price and listing.price.money:
                price_usd = listing.price.money.amount

        # Image
        image_url = None
        if item.images and item.images.primary and item.images.primary.large:
            image_url = item.images.primary.large.url

        # Detail page URL
        detail_page_url = item.detail_page_url

        return BikingRawProduct(
            asin=asin,
            title=title,
            brand=brand,
            bsr=bsr,
            review_count=review_count,
            rating=rating,
            price_usd=price_usd,
            image_url=image_url,
            detail_page_url=detail_page_url,
        )
    except Exception as e:
        logger.warning("Failed to extract product from item %s: %s", getattr(item, "asin", "?"), e)
        return None


def _fetch_products(
    api,
    config: BikingArticleConfig,
) -> list[BikingRawProduct]:
    """Fetch products from Amazon Creators API with pagination.

    Uses SearchIndex (SportingGoods) and keyword search via the SDK's
    search_items() method with proper enum-based resources.
    No browse_node_id or gender filter for biking.
    """
    from amazon_creatorsapi.models import SearchItemsResource, SortBy

    resources = [
        SearchItemsResource.ITEM_INFO_DOT_TITLE,
        SearchItemsResource.ITEM_INFO_DOT_BY_LINE_INFO,
        SearchItemsResource.BROWSE_NODE_INFO_DOT_WEBSITE_SALES_RANK,
        SearchItemsResource.CUSTOMER_REVIEWS_DOT_COUNT,
        SearchItemsResource.CUSTOMER_REVIEWS_DOT_STAR_RATING,
        SearchItemsResource.OFFERS_V2_DOT_LISTINGS_DOT_PRICE,
        SearchItemsResource.IMAGES_DOT_PRIMARY_DOT_LARGE,
    ]

    all_products: list[BikingRawProduct] = []
    seen_asins: set[str] = set()

    for page in range(1, 3):  # Pages 1 and 2
        if page > 1:
            time.sleep(1)  # Amazon Creators API rate limit: 1 req/sec
        logger.info("Fetching page %d for '%s'", page, config.keywords)
        try:
            search_kwargs = dict(
                keywords=config.keywords,
                search_index=config.search_index,
                item_count=10,
                item_page=page,
                min_reviews_rating=int(config.min_rating),
                sort_by=SortBy.FEATURED,
                resources=resources,
            )
            if getattr(config, "browse_node_id", None):
                search_kwargs["browse_node_id"] = config.browse_node_id
            result = _retry(
                lambda: api.search_items(**search_kwargs),
                should_retry=_is_rate_limit_error,
            )
        except Exception as e:
            if page == 1:
                raise SignalsCollectorError(
                    f"Failed to fetch first page of products: {e}"
                ) from e
            logger.warning("Error fetching page %d: %s", page, e)
            break

        if not result or not result.items:
            logger.warning("No items returned for page %d", page)
            continue

        for item in result.items:
            product = _extract_product(item)
            if product and product.asin not in seen_asins:
                seen_asins.add(product.asin)
                all_products.append(product)

        logger.info("Page %d: fetched %d items", page, len(result.items))

    return all_products


def _apply_filters(
    products: list[BikingRawProduct],
    config: BikingArticleConfig,
) -> list[BikingRawProduct]:
    """Apply price, review, and rating filters from config."""
    filtered = []
    for p in products:
        # Price filter (skip if no price data)
        if p.price_usd is not None:
            if p.price_usd < config.price_min_usd or p.price_usd > config.price_max_usd:
                continue

        # Review count filter (skip if no review data — Creators API limitation)
        if p.review_count is not None and p.review_count < config.min_reviews:
            continue

        # Rating filter (skip if no rating data)
        if p.rating is not None and p.rating < config.min_rating:
            continue

        filtered.append(p)

    return filtered


def collect(
    config: BikingArticleConfig,
    run_date: str,
    supplemental_keywords: list[str] | None = None,
    force: bool = False,
) -> BikingRawSignals:
    """Collect product signals from Amazon Creators API.

    Idempotent: skips work if raw_signals.json exists (unless force=True).
    Returns BikingRawSignals with filtered products.
    """
    artifact_path = biking_runs_path(config.article_id, run_date, "raw_signals.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached raw_signals.json")
        return BikingRawSignals.model_validate_json(artifact_path.read_text())

    api = _get_api_client(config)

    # Primary search
    logger.info("Fetching products for '%s' (index: %s)", config.keywords, config.search_index)
    products = _fetch_products(api, config)
    total_api_results = len(products)
    logger.info("Primary search: %d products", total_api_results)

    # Supplemental brand searches for top brands
    if supplemental_keywords:
        seen_asins = {p.asin for p in products}
        for kw in supplemental_keywords:
            logger.info("Supplemental search: '%s'", kw)
            supp_config = config.model_copy(update={"keywords": kw})
            try:
                supp_products = _fetch_products(api, supp_config)
                for sp in supp_products:
                    if sp.asin not in seen_asins:
                        sp.source = "supplemental"
                        products.append(sp)
                        seen_asins.add(sp.asin)
            except Exception as e:
                logger.warning("Supplemental search '%s' failed: %s", kw, e)

    products_before_filter = len(products)

    # Apply filters
    filtered = _apply_filters(products, config)
    logger.info(
        "Filtered: %d -> %d products",
        products_before_filter, len(filtered),
    )

    if len(filtered) < 1:
        raise SignalsCollectorError(
            f"No products passed filters for '{config.article_id}'. "
            f"Had {products_before_filter} before filtering."
        )

    signals = BikingRawSignals(
        article_id=config.article_id,
        category_id=config.category_id,
        collected_at=datetime.now(timezone.utc),
        search_keywords=config.keywords,
        total_api_results=total_api_results,
        products_before_filter=products_before_filter,
        products_after_filter=len(filtered),
        products=filtered,
    )

    # Save artifact
    artifact_path.write_text(signals.model_dump_json(indent=2))
    logger.info("Raw signals saved: %s", artifact_path)

    return signals
