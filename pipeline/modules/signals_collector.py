"""Signals collector — queries Amazon Creators API and applies config filters."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from pipeline.models import CategoryConfig, RawProduct, RawSignals, runs_path

logger = logging.getLogger(__name__)


class SignalsCollectorError(Exception):
    pass


def _get_api_client(config: CategoryConfig):
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


def _extract_product(item) -> RawProduct | None:
    """Extract fields from an Amazon API item, handling deeply nested Nones."""
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

        return RawProduct(
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


def _fetch_products(api, config: CategoryConfig) -> list[RawProduct]:
    """Make paginated API calls to get ~20 candidate products."""
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

    all_products: list[RawProduct] = []
    seen_asins: set[str] = set()

    for page in range(1, 3):  # Pages 1 and 2
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
            if config.browse_node_id:
                search_kwargs["browse_node_id"] = config.browse_node_id
            result = api.search_items(**search_kwargs)
        except Exception as e:
            raise SignalsCollectorError(
                f"Amazon API call failed (page {page}): {e}"
            ) from e

        if not result or not result.items:
            logger.warning("No items returned for page %d", page)
            continue

        for item in result.items:
            product = _extract_product(item)
            if product and product.asin not in seen_asins:
                seen_asins.add(product.asin)
                all_products.append(product)

    return all_products


def _passes_gender_filter(title: str, gender: str) -> bool:
    """Check if a product title matches the target gender.

    Rules:
    - Unisex products (title has both genders, or "Unisex") pass for either gender
    - "Men's" or "for Men" without any women signal → fails for gender=women
    - "Women's" or "for Women" without any men signal → fails for gender=men
    """
    t = title.lower()
    has_women = "women" in t or "woman" in t
    has_men = "men's" in t or "for men" in t or " men " in t or "men," in t
    # "Unisex" always passes
    if "unisex" in t:
        return True
    # Both genders mentioned → unisex, passes for either
    if has_women and has_men:
        return True
    if gender == "women" and has_men and not has_women:
        return False
    if gender == "men" and has_women and not has_men:
        return False
    return True


def _apply_filters(products: list[RawProduct], config: CategoryConfig) -> list[RawProduct]:
    """Apply config-based filters: price range, min reviews, min rating, gender."""
    filtered = []
    for p in products:
        if not _passes_gender_filter(p.title, config.gender):
            logger.debug("Gender filter removed: %s", p.title)
            continue
        if p.price_usd is not None:
            if p.price_usd < config.price_min_usd or p.price_usd > config.price_max_usd:
                continue
        if p.review_count is not None and p.review_count < config.min_reviews:
            continue
        if p.rating is not None and p.rating < config.min_rating:
            continue
        filtered.append(p)
    return filtered


def _fetch_supplemental(
    api,
    keywords: list[str],
    config: CategoryConfig,
    seen_asins: set[str],
) -> list[RawProduct]:
    """Make single-page Amazon searches for supplemental keywords (from trends).

    Returns new products not already in the primary results, tagged with source="supplemental".
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

    new_products: list[RawProduct] = []

    for kw in keywords:
        logger.info("Supplemental search: '%s'", kw)
        try:
            # Supplemental searches omit browse_node_id — the browse node
            # constrains too aggressively for targeted brand/model searches and
            # can return wrong-gender or off-category products instead of the
            # exact product we're looking for.
            result = api.search_items(
                keywords=kw,
                search_index=config.search_index,
                item_count=10,
                item_page=1,
                min_reviews_rating=int(config.min_rating),
                sort_by=SortBy.FEATURED,
                resources=resources,
            )
        except Exception as e:
            logger.warning("Supplemental search failed for '%s': %s", kw, e)
            continue

        if not result or not result.items:
            continue

        for item in result.items:
            product = _extract_product(item)
            if product and product.asin not in seen_asins:
                seen_asins.add(product.asin)
                product.source = "supplemental"
                new_products.append(product)

    return new_products


def collect(
    config: CategoryConfig,
    week_of: str,
    supplemental_keywords: list[str] | None = None,
    force: bool = False,
) -> RawSignals:
    """Collect raw product signals from Amazon.

    Idempotent: skips API call if raw_signals.json already exists (unless force=True).

    When supplemental_keywords are provided (from trends data), makes additional
    single-page searches per keyword to find trending products not in primary results.
    """
    artifact_path = runs_path(config.category_id, week_of, "raw_signals.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached raw_signals.json")
        return RawSignals.model_validate_json(artifact_path.read_text())

    api = _get_api_client(config)
    all_products = _fetch_products(api, config)
    products_before_filter = len(all_products)

    if products_before_filter == 0:
        raise SignalsCollectorError("Amazon API returned 0 products — cannot proceed")

    # Supplemental searches from trends data
    if supplemental_keywords:
        seen_asins = {p.asin for p in all_products}
        supplemental = _fetch_supplemental(api, supplemental_keywords, config, seen_asins)
        logger.info(
            "Supplemental searches added %d new products from %d keywords",
            len(supplemental), len(supplemental_keywords),
        )
        all_products.extend(supplemental)

    filtered = _apply_filters(all_products, config)

    if len(filtered) == 0:
        raise SignalsCollectorError(
            f"All {len(all_products)} products were filtered out — "
            "check config filters (price range, min_reviews, min_rating)"
        )

    signals = RawSignals(
        category_id=config.category_id,
        week_of=week_of,
        collected_at=datetime.now(timezone.utc),
        search_keywords=config.keywords,
        total_api_results=products_before_filter,
        products_before_filter=len(all_products),
        products_after_filter=len(filtered),
        products=filtered,
    )

    # Save raw data first (before returning)
    artifact_path.write_text(signals.model_dump_json(indent=2))
    logger.info(
        "Collected %d products (%d after filtering, %d supplemental) for %s",
        len(all_products), len(filtered),
        len(supplemental_keywords) if supplemental_keywords else 0,
        config.category_id,
    )

    return signals
