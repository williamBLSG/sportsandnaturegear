"""GeniusLink client — creates or retrieves cached geni.us affiliate links."""

from __future__ import annotations

import json
import logging
import os

import requests

from pipeline.models import (
    CategoryConfig,
    LinkedProduct,
    RankedOutput,
    StateActivityConfig,
    StateActivityProduct,
    runs_path,
    slugify,
    state_runs_path,
)

logger = logging.getLogger(__name__)

GENIUSLINK_BASE = "https://api.geni.us"


class GeniusLinkError(Exception):
    pass


def _auth_headers(api_key: str, api_secret: str) -> dict:
    return {
        "X-Api-Key": api_key,
        "X-Api-Secret": api_secret,
        "Content-Type": "application/json",
    }


def _load_cache(category_id: str, week_of: str) -> dict[str, str]:
    """Load ASIN → geni.us URL cache from disk."""
    cache_path = runs_path(category_id, week_of, "geniuslink_cache.json")
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return {}


def _save_cache(cache: dict[str, str], category_id: str, week_of: str) -> None:
    """Save ASIN → geni.us URL cache to disk."""
    cache_path = runs_path(category_id, week_of, "geniuslink_cache.json")
    cache_path.write_text(json.dumps(cache, indent=2))


def _resolve_group_id(
    group_name: str,
    api_key: str,
    api_secret: str,
) -> int:
    """Look up numeric group ID by name. Raises GeniusLinkError if not found."""
    try:
        resp = requests.get(
            f"{GENIUSLINK_BASE}/v1/groups/get-all-with-details",
            headers=_auth_headers(api_key, api_secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for group in data.get("Groups", []):
            if group["Name"] == group_name:
                return group["Id"]
        raise GeniusLinkError(
            f"GeniusLink group '{group_name}' not found. "
            f"Create it in the GeniusLink dashboard first."
        )
    except requests.RequestException as e:
        raise GeniusLinkError(f"Failed to look up GeniusLink groups: {e}") from e


def _create_link(
    detail_page_url: str,
    group_id: int,
    api_key: str,
    api_secret: str,
) -> tuple[str, str] | None:
    """Create a geni.us short URL. Returns (full_url, code) or None on failure."""
    try:
        resp = requests.post(
            f"{GENIUSLINK_BASE}/v3/shorturls",
            params={"url": detail_page_url, "groupId": group_id},
            headers=_auth_headers(api_key, api_secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        short_url = data.get("shortUrl", {})
        code = short_url.get("code")
        domain = short_url.get("domain", "geni.us")
        if code:
            return f"https://{domain}/{code}", code
        logger.warning("GeniusLink response missing 'code': %s", data)
        return None
    except requests.RequestException as e:
        logger.warning("GeniusLink API call failed: %s", e)
        return None


def _set_utm_tags(
    code: str,
    category_id: str,
    model_slug: str,
    week_of: str,
    api_key: str,
    api_secret: str,
) -> None:
    """Attach UTM tags to a geni.us short URL via post-processing rules."""
    try:
        resp = requests.post(
            f"{GENIUSLINK_BASE}/v2/postprocessingrules",
            headers=_auth_headers(api_key, api_secret),
            json={
                "postProcessingLevel": "link",
                "shortCodes": [code],
                "parameterKeyValues": {
                    "utm_source": "sportsandnaturegear",
                    "utm_medium": "affiliate",
                    "utm_campaign": f"{category_id}-trending",
                    "utm_term": model_slug,
                    "utm_content": week_of,
                },
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to set UTM tags for %s: %s", code, e)


def enrich(
    ranked: RankedOutput,
    config: CategoryConfig,
    week_of: str,
    force: bool = False,
) -> tuple[list[LinkedProduct], int, int, int]:
    """Enrich ranked products with GeniusLink affiliate URLs.

    Idempotent: skips work if linked.json exists (unless force=True).
    Returns (linked_products, cached_count, created_count, failed_count).
    """
    artifact_path = runs_path(config.category_id, week_of, "linked.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached linked.json")
        data = json.loads(artifact_path.read_text())
        products = [LinkedProduct(**p) for p in data]
        return products, len(products), 0, 0

    api_key = os.environ.get("GENIUSLINK_API_KEY")
    api_secret = os.environ.get("GENIUSLINK_API_SECRET")

    if not api_key or not api_secret:
        raise GeniusLinkError(
            "GENIUSLINK_API_KEY and GENIUSLINK_API_SECRET must be set"
        )

    # Resolve group name → numeric ID
    group_id = _resolve_group_id(config.geniuslink_group_id, api_key, api_secret)
    logger.info("Resolved GeniusLink group '%s' → ID %d", config.geniuslink_group_id, group_id)

    cache = _load_cache(config.category_id, week_of)
    linked: list[LinkedProduct] = []
    cached_count = 0
    created_count = 0
    failed_count = 0

    for product in ranked.products:
        affiliate_url = cache.get(product.asin)
        model_slug = slugify(f"{product.brand} {product.model}")

        if affiliate_url:
            cached_count += 1
        elif product.detail_page_url:
            result = _create_link(
                product.detail_page_url,
                group_id,
                api_key,
                api_secret,
            )
            if result:
                affiliate_url, code = result
                cache[product.asin] = affiliate_url
                created_count += 1
                _set_utm_tags(
                    code, config.category_id, model_slug, week_of,
                    api_key, api_secret,
                )
            else:
                affiliate_url = product.detail_page_url
                failed_count += 1

        linked.append(LinkedProduct(
            rank=product.rank,
            asin=product.asin,
            title=product.title,
            brand=product.brand,
            model=product.model,
            full_name=product.full_name,
            model_slug=model_slug,
            bsr=product.bsr,
            review_count=product.review_count,
            rating=product.rating,
            price_usd=product.price_usd,
            image_url=product.image_url,
            detail_page_url=product.detail_page_url,
            heat_score=product.heat_score,
            rank_change=product.rank_change,
            affiliate_url=affiliate_url or product.detail_page_url or "",
        ))

    _save_cache(cache, config.category_id, week_of)

    # Save artifact
    artifact_path.write_text(
        json.dumps([p.model_dump() for p in linked], indent=2)
    )

    logger.info(
        "GeniusLink enrichment: %d cached, %d created, %d failed",
        cached_count, created_count, failed_count,
    )

    return linked, cached_count, created_count, failed_count


# ---------------------------------------------------------------------------
# State activity product enrichment
# ---------------------------------------------------------------------------

def _set_state_utm_tags(
    code: str,
    state: str,
    activity: str,
    product_slug: str,
    api_key: str,
    api_secret: str,
) -> None:
    """Attach state-activity-specific UTM tags to a geni.us short URL."""
    state_slug = slugify(state)
    try:
        resp = requests.post(
            f"{GENIUSLINK_BASE}/v2/postprocessingrules",
            headers=_auth_headers(api_key, api_secret),
            json={
                "postProcessingLevel": "link",
                "shortCodes": [code],
                "parameterKeyValues": {
                    "utm_source": "sportsandnaturegear",
                    "utm_medium": "states",
                    "utm_campaign": state_slug,
                    "utm_term": activity,
                    "utm_content": product_slug,
                },
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to set state UTM tags for %s: %s", code, e)


def enrich_state_products(
    products: list[StateActivityProduct],
    state: str,
    config: StateActivityConfig,
    force: bool = False,
) -> tuple[list[StateActivityProduct], int, int, int]:
    """Enrich state activity products with GeniusLink affiliate URLs.

    Idempotent: skips work if products_linked.json exists (unless force=True).
    Returns (products, cached_count, created_count, failed_count).
    """
    artifact_path = state_runs_path(
        state, config.activity_id, "products_linked.json",
    )

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached products_linked.json")
        data = json.loads(artifact_path.read_text())
        enriched = [StateActivityProduct(**p) for p in data]
        return enriched, len(enriched), 0, 0

    api_key = os.environ.get("GENIUSLINK_API_KEY")
    api_secret = os.environ.get("GENIUSLINK_API_SECRET")

    if not api_key or not api_secret:
        raise GeniusLinkError(
            "GENIUSLINK_API_KEY and GENIUSLINK_API_SECRET must be set"
        )

    group_id = _resolve_group_id(
        config.geniuslink_group_id, api_key, api_secret,
    )
    logger.info(
        "Resolved GeniusLink group '%s' -> ID %d",
        config.geniuslink_group_id, group_id,
    )

    # Load per-activity cache
    cache_path = state_runs_path(
        state, config.activity_id, "geniuslink_cache.json",
    )
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    cached_count = 0
    created_count = 0
    failed_count = 0

    for product in products:
        # Check cache by ASIN
        cached_url = cache.get(product.asin)
        if cached_url:
            product.affiliate_link = cached_url
            cached_count += 1
            continue

        # Need to create a link from the raw Amazon URL
        amazon_url = product.affiliate_link  # set to detail_page_url during copy gen
        if not amazon_url or not amazon_url.startswith("http"):
            failed_count += 1
            continue

        result = _create_link(amazon_url, group_id, api_key, api_secret)
        if result:
            affiliate_url, code = result
            product.affiliate_link = affiliate_url
            cache[product.asin] = affiliate_url
            created_count += 1
            # Product slug for UTM: strip the slug prefix to get short name
            product_name_slug = slugify(product.title)
            _set_state_utm_tags(
                code, state, config.activity_id,
                product_name_slug, api_key, api_secret,
            )
        else:
            # Keep raw Amazon URL as fallback
            failed_count += 1

    # Save cache
    cache_path.write_text(json.dumps(cache, indent=2))

    # Save artifact
    artifact_path.write_text(
        json.dumps([p.model_dump() for p in products], indent=2)
    )

    logger.info(
        "State GeniusLink enrichment: %d cached, %d created, %d failed",
        cached_count, created_count, failed_count,
    )

    return products, cached_count, created_count, failed_count
