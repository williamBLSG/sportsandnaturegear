"""GeniusLink client — creates or retrieves cached geni.us affiliate links.

Reuses the same GeniusLink API pattern as the weekly trending pipeline but
with biking-specific config (global per-category cache in runs/biking/).

GeniusLink has NO server-side dedup — posting the same URL twice creates two
separate short URLs. All dedup is handled client-side via the ASIN cache.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

from biking_pipeline.models import (
    BikingArticleConfig,
    BikingLinkedProduct,
    BikingRankedOutput,
    BikingRankedProduct,
    biking_cache_path,
    biking_runs_path,
    slugify,
)

logger = logging.getLogger(__name__)


class GeniusLinkError(Exception):
    pass


# ---------------------------------------------------------------------------
# Global ASIN cache (shared across all biking articles)
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, str]:
    """Load the global biking ASIN → geni.us URL cache."""
    cache_file = biking_cache_path("geniuslink_cache.json")
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read GeniusLink cache — starting fresh")
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    """Save the global biking ASIN → geni.us URL cache."""
    cache_file = biking_cache_path("geniuslink_cache.json")
    cache_file.write_text(json.dumps(cache, indent=2))


# ---------------------------------------------------------------------------
# GeniusLink API calls
# ---------------------------------------------------------------------------

def _resolve_group_id(
    api_key: str,
    api_secret: str,
    group_name: str,
    group_numeric_id: int,
) -> int:
    """Resolve group numeric ID. Uses the config value directly.

    The group must pre-exist in the GeniusLink dashboard.
    """
    # We trust the config value — the group must be created manually
    return group_numeric_id


def _auth_headers(api_key: str, api_secret: str) -> dict:
    """Build auth headers matching the working weekly pipeline pattern."""
    return {
        "X-Api-Key": api_key,
        "X-Api-Secret": api_secret,
        "Content-Type": "application/json",
    }


def _create_link(
    api_key: str,
    api_secret: str,
    amazon_url: str,
    group_id: int,
    asin: str,
) -> str:
    """Create a new geni.us short URL via the GeniusLink API.

    POST /v3/shorturls
    Auth via X-Api-Key/X-Api-Secret headers, URL as query param.
    Retries once with exponential backoff on 429/5xx.
    """
    url = "https://api.geni.us/v3/shorturls"

    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                params={"url": amazon_url, "groupId": group_id},
                headers=_auth_headers(api_key, api_secret),
                timeout=30,
            )

            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.warning("GeniusLink 429 — waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = 15 * (attempt + 1)
                logger.warning(
                    "GeniusLink %d — waiting %ds (attempt %d)",
                    resp.status_code, wait, attempt + 1,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()

            data = resp.json()
            short_url_obj = data.get("shortUrl") or data.get("ShortUrl") or {}
            if isinstance(short_url_obj, str):
                return short_url_obj
            code = short_url_obj.get("code")
            domain = short_url_obj.get("domain", "geni.us")
            if code:
                return f"https://{domain}/{code}"
            raise GeniusLinkError(f"No short URL code in GeniusLink response for ASIN {asin}: {data}")

        except requests.RequestException as e:
            if attempt == 0:
                logger.warning("GeniusLink request failed — retrying: %s", e)
                time.sleep(5)
                continue
            raise GeniusLinkError(f"GeniusLink API failed for ASIN {asin}: {e}") from e

    raise GeniusLinkError(f"GeniusLink API exhausted retries for ASIN {asin}")


def _set_utm_tags(
    api_key: str,
    api_secret: str,
    group_id: int,
) -> None:
    """Set UTM post-processing rules for the biking group.

    Only needs to be called once per group — idempotent.
    POST /v2/postprocessingrules
    """
    url = "https://api.geni.us/v2/postprocessingrules"
    payload = {
        "postProcessingLevel": "group",
        "groupId": group_id,
        "parameterKeyValues": {
            "utm_source": "sportsandnaturegear",
            "utm_medium": "affiliate",
            "utm_campaign": "biking",
        },
    }

    try:
        resp = requests.post(
            url,
            headers=_auth_headers(api_key, api_secret),
            json=payload,
            timeout=30,
        )
        if resp.status_code < 400:
            logger.info("UTM tags set for group %d", group_id)
        else:
            logger.warning("UTM tag setup returned %d — non-fatal", resp.status_code)
    except requests.RequestException as e:
        logger.warning("UTM tag setup failed — non-fatal: %s", e)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def enrich(
    ranked: BikingRankedOutput,
    config: BikingArticleConfig,
    run_date: str,
    force: bool = False,
) -> list[BikingLinkedProduct]:
    """Create or retrieve GeniusLink affiliate URLs for each ranked product.

    Uses a global per-category ASIN cache (runs/biking/geniuslink_cache.json)
    to avoid creating duplicate links.

    Idempotent: skips work if linked.json exists (unless force=True).
    Returns list of BikingLinkedProduct with affiliate_url populated.
    """
    artifact_path = biking_runs_path(config.article_id, run_date, "linked.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached linked.json")
        data = json.loads(artifact_path.read_text())
        return [BikingLinkedProduct(**p) for p in data]

    api_key = os.environ.get("GENIUSLINK_API_KEY")
    api_secret = os.environ.get("GENIUSLINK_API_SECRET")

    if not api_key or not api_secret:
        raise GeniusLinkError(
            "GENIUSLINK_API_KEY and GENIUSLINK_API_SECRET must be set"
        )

    group_id = _resolve_group_id(
        api_key, api_secret,
        config.geniuslink_group_id,
        config.geniuslink_group_numeric_id,
    )

    # Set UTM tags (idempotent)
    _set_utm_tags(api_key, api_secret, group_id)

    # Load global cache
    cache = _load_cache()

    linked_products: list[BikingLinkedProduct] = []
    cached_count = 0
    created_count = 0
    failed_count = 0

    for product in ranked.products:
        # Check cache first
        if product.asin in cache:
            affiliate_url = cache[product.asin]
            cached_count += 1
            logger.debug("Cache hit for ASIN %s", product.asin)
        elif product.detail_page_url:
            # Create new link
            try:
                affiliate_url = _create_link(
                    api_key, api_secret,
                    product.detail_page_url,
                    group_id,
                    product.asin,
                )
                cache[product.asin] = affiliate_url
                created_count += 1
                logger.info("Created GeniusLink for ASIN %s", product.asin)
                time.sleep(1)  # Rate limit between creates
            except GeniusLinkError as e:
                logger.warning("GeniusLink failed for ASIN %s: %s", product.asin, e)
                affiliate_url = product.detail_page_url  # Fallback to raw URL
                failed_count += 1
        else:
            affiliate_url = ""
            failed_count += 1
            logger.warning("No detail_page_url for ASIN %s", product.asin)

        # Build LinkedProduct from RankedProduct + affiliate URL
        linked = BikingLinkedProduct(
            rank=product.rank,
            asin=product.asin,
            title=product.title,
            brand=product.brand,
            model=product.model,
            full_name=product.full_name,
            model_slug=slugify(f"{product.brand} {product.model}"),
            bsr=product.bsr,
            review_count=product.review_count,
            rating=product.rating,
            price_usd=product.price_usd,
            image_url=product.image_url,
            detail_page_url=product.detail_page_url,
            composite_score=product.composite_score,
            bsr_score=product.bsr_score,
            brand_authority_score=product.brand_authority_score,
            trends_score=product.trends_score,
            review_quality_score=product.review_quality_score,
            price_tier=product.price_tier,
            role=product.role,
            trend_source=product.trend_source,
            trend_match_type=product.trend_match_type,
            trend_query=product.trend_query,
            trend_search_interest=product.trend_search_interest,
            is_top_brand=product.is_top_brand,
            affiliate_url=affiliate_url,
        )
        linked_products.append(linked)

    # Save cache
    _save_cache(cache)

    logger.info(
        "GeniusLink: %d cached, %d created, %d failed",
        cached_count, created_count, failed_count,
    )

    # Save artifact
    artifact_data = [lp.model_dump() for lp in linked_products]
    artifact_path.write_text(json.dumps(artifact_data, indent=2, default=str))
    logger.info("Saved linked.json for %s", config.article_id)

    return linked_products
