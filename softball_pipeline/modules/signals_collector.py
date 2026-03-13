"""Signals collector — queries Amazon Creators API for softball products.

Reuses the same Creators API pattern as the weekly trending pipeline but
with softball-specific config (SportingGoods search index, no browse node,
no gender filter).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from softball_pipeline.models import (
    SoftballArticleConfig,
    SoftballRawProduct,
    SoftballRawSignals,
    softball_runs_path,
)

logger = logging.getLogger(__name__)


class SignalsCollectorError(Exception):
    pass


def _extract_product(item: dict) -> SoftballRawProduct | None:
    """Extract a SoftballRawProduct from an Amazon Creators API result item.

    Returns None if the item is missing critical data (ASIN or title).
    """
    asin = item.get("asin")
    title = item.get("title")

    if not asin or not title:
        return None

    # Extract BSR from salesRankings
    bsr = None
    rankings = item.get("salesRankings", [])
    if rankings:
        # Prefer the first ranking (most specific category)
        bsr = rankings[0].get("rank")

    # Extract price
    price_usd = None
    price_info = item.get("price", {})
    if price_info:
        amount = price_info.get("amount")
        if amount is not None:
            price_usd = float(amount)

    # Extract rating and review count
    rating = None
    review_count = None
    customer_reviews = item.get("customerReviews", {})
    if customer_reviews:
        rating = customer_reviews.get("starRating")
        review_count = customer_reviews.get("count")

    # Extract images
    image_url = None
    images = item.get("images", [])
    if images:
        # Prefer "large" or first available
        for img in images:
            if img.get("size") == "large":
                image_url = img.get("url")
                break
        if not image_url and images:
            image_url = images[0].get("url")

    # Extract detail page URL
    detail_page_url = item.get("detailPageUrl")

    # Extract brand
    brand = item.get("brand")

    return SoftballRawProduct(
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


def _fetch_products(
    api,
    config: SoftballArticleConfig,
) -> list[SoftballRawProduct]:
    """Fetch products from Amazon Creators API with pagination.

    Uses SearchIndex (SportingGoods) and keyword search.
    No browse_node_id or gender filter for softball.
    """
    all_products: list[SoftballRawProduct] = []
    seen_asins: set[str] = set()

    for page in range(1, 3):  # 2 pages max
        try:
            # Build search params
            params = {
                "Keywords": config.keywords,
                "SearchIndex": config.search_index,
                "ItemCount": 10,
                "ItemPage": page,
                "Resources": [
                    "ItemInfo.Title",
                    "ItemInfo.ByLineInfo",
                    "BrowseNodeInfo.BrowseNodes",
                    "Offers.Listings.Price",
                    "CustomerReviews.StarRating",
                    "CustomerReviews.Count",
                    "Images.Primary.Large",
                    "Images.Primary.Medium",
                    "BrowseNodeInfo.BrowseNodes.SalesRank",
                ],
            }

            result = api.search_products(**params)

            items = result.get("items", [])
            if not items:
                logger.info("No more items on page %d", page)
                break

            for item in items:
                product = _extract_product(item)
                if product and product.asin not in seen_asins:
                    all_products.append(product)
                    seen_asins.add(product.asin)

            logger.info("Page %d: fetched %d products", page, len(items))

        except Exception as e:
            logger.warning("Error fetching page %d: %s", page, e)
            if page == 1:
                raise SignalsCollectorError(
                    f"Failed to fetch first page of products: {e}"
                ) from e
            break

    return all_products


def _apply_filters(
    products: list[SoftballRawProduct],
    config: SoftballArticleConfig,
) -> list[SoftballRawProduct]:
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
    config: SoftballArticleConfig,
    run_date: str,
    supplemental_keywords: list[str] | None = None,
    force: bool = False,
) -> SoftballRawSignals:
    """Collect product signals from Amazon Creators API.

    Idempotent: skips work if raw_signals.json exists (unless force=True).
    Returns SoftballRawSignals with filtered products.
    """
    artifact_path = softball_runs_path(config.article_id, run_date, "raw_signals.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached raw_signals.json")
        return SoftballRawSignals.model_validate_json(artifact_path.read_text())

    # Import here to avoid import error when not installed
    try:
        from amazon_creatorsapi import AmazonCreatorsAPI
    except ImportError:
        raise SignalsCollectorError(
            "amazon_creatorsapi package required. pip install amazon-creatorsapi"
        )

    access_key = os.environ.get("AMZ_CREATORS_ACCESS_KEY")
    secret_key = os.environ.get("AMZ_CREATORS_SECRET_KEY")
    assoc_tag = config.assoc_tag

    if not access_key or not secret_key:
        raise SignalsCollectorError(
            "AMZ_CREATORS_ACCESS_KEY and AMZ_CREATORS_SECRET_KEY must be set"
        )

    api = AmazonCreatorsAPI(
        access_key=access_key,
        secret_key=secret_key,
        partner_tag=assoc_tag,
        country="US",
    )

    # Primary search
    logger.info("Fetching products for '%s' (index: %s)", config.keywords, config.search_index)
    products = _fetch_products(api, config)
    total_api_results = len(products)
    logger.info("Primary search: %d products", total_api_results)

    # Supplemental brand searches for top brands
    if supplemental_keywords:
        for kw in supplemental_keywords:
            logger.info("Supplemental search: '%s'", kw)
            supp_config = config.model_copy(update={"keywords": kw})
            try:
                supp_products = _fetch_products(api, supp_config)
                seen_asins = {p.asin for p in products}
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

    signals = SoftballRawSignals(
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
