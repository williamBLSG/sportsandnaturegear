"""Ranker — normalizes brand/model names, deduplicates, computes Heat Scores."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import anthropic

from pipeline.models import (
    CategoryConfig,
    RankedOutput,
    RankedProduct,
    RawProduct,
    RawSignals,
    TrendsData,
    TrendsQuery,
    runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"


class RankerError(Exception):
    pass


def compute_heat_score(
    bsr: int | None,
    review_count: int | None,
    rating: float | None,
) -> float:
    """Compute Heat Score from BSR, review count, and rating.

    Formula:
        (BSR_component × weight) + (review_velocity × weight) + (rating_component × weight)

    When reviews/ratings are unavailable (Creators API limitation), BSR takes
    full weight so products are still meaningfully differentiated.

    BSR component:   max(0, 100 - log10(bsr) * 20)    — lower BSR = higher score
                     BSR 1 → 100, BSR 100 → 60, BSR 1000 → 40, BSR 10000 → 20
    Review velocity: min(100, review_count / 20)       — more reviews = higher score
    Rating component: (rating / 5) × 100               — higher rating = higher score
    Missing BSR → 0 for that component.
    """
    import math

    bsr_component = max(0.0, 100.0 - math.log10(bsr) * 20.0) if bsr and bsr > 0 else 0.0
    review_velocity = min(100.0, (review_count or 0) / 20.0)
    rating_component = ((rating or 0.0) / 5.0) * 100.0

    # When reviews/ratings are unavailable, redistribute weight to BSR
    has_reviews = review_count is not None
    has_rating = rating is not None

    if has_reviews and has_rating:
        score = (bsr_component * 0.50) + (review_velocity * 0.30) + (rating_component * 0.20)
    elif has_reviews:
        score = (bsr_component * 0.65) + (review_velocity * 0.35)
    elif has_rating:
        score = (bsr_component * 0.70) + (rating_component * 0.30)
    else:
        score = bsr_component  # BSR only

    return round(score, 2)


def _normalize_brands(products: list[RawProduct], config: CategoryConfig) -> list[dict]:
    """Use Claude to extract normalized brand and model names, and classify category match.

    Returns a list of dicts: [{asin, brand, model, full_name, is_category_match}, ...]
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RankerError("ANTHROPIC_API_KEY must be set for brand normalization")

    client = anthropic.Anthropic(api_key=api_key)

    product_list = []
    for p in products:
        product_list.append({
            "asin": p.asin,
            "title": p.title,
            "brand_hint": p.brand,
        })

    prompt = f"""Extract the brand name and model name from each product title below, and classify whether each product is a "{config.product_type}".

Rules:
- "brand" = the manufacturer (e.g., "Brooks", "Nike", "ASICS", "New Balance")
- "model" = the product line name without size/color/gender (e.g., "Ghost 16", "Pegasus 41", "Gel-Kayano 31")
- "full_name" = "brand model" combined (e.g., "Brooks Ghost 16")
- "is_category_match" = true if the product is a {config.product_type}, false if it's a different type of footwear (e.g., lifestyle sneaker, recovery slide, tactical boot, track spike, basketball shoe, casual shoe, sandal)
- Use the brand_hint field if the title is ambiguous
- Normalize capitalization: title case for brand and model
- Strip color names, size info, gender labels, and "running shoe" from the model name

Return a JSON array with one object per product:
[{{"asin": "...", "brand": "...", "model": "...", "full_name": "...", "is_category_match": true/false}}]

Products:
{json.dumps(product_list, indent=2)}

Return ONLY the JSON array, no other text."""

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text.strip()

        # Handle markdown code blocks
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            # Remove first and last lines (``` markers)
            lines = [l for l in lines if not l.strip().startswith("```")]
            response_text = "\n".join(lines)

        normalized = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise RankerError(f"Claude returned invalid JSON for brand normalization: {e}") from e
    except anthropic.APIError as e:
        raise RankerError(f"Anthropic API error during brand normalization: {e}") from e

    # Validate we got the right ASINs back
    input_asins = {p.asin for p in products}
    output_asins = {n["asin"] for n in normalized}
    missing = input_asins - output_asins
    if missing:
        logger.warning("Brand normalization missing ASINs: %s", missing)

    return normalized


def _deduplicate(
    products: list[RawProduct],
    normalized: list[dict],
) -> list[tuple[RawProduct, dict]]:
    """Group by normalized brand+model, keep the entry with highest Heat Score."""
    # Build lookup from ASIN to normalized info
    norm_by_asin = {n["asin"]: n for n in normalized}

    # Group by brand+model key
    groups: dict[str, list[tuple[RawProduct, dict]]] = {}
    for p in products:
        norm = norm_by_asin.get(p.asin)
        if not norm:
            continue
        key = f"{norm['brand'].lower()}|{norm['model'].lower()}"
        groups.setdefault(key, []).append((p, norm))

    # Keep best per group
    deduped = []
    for key, entries in groups.items():
        best = max(
            entries,
            key=lambda e: compute_heat_score(e[0].bsr, e[0].review_count, e[0].rating),
        )
        deduped.append(best)

    return deduped


def _match_trends(
    deduped: list[tuple[RawProduct, dict, float]],
    trends: TrendsData,
) -> list[tuple[RawProduct, dict, float, TrendsQuery | None, str | None]]:
    """Match deduped Amazon products against trends queries.

    Returns list of (product, norm, heat_score, matched_query, match_type).
    match_type is "brand_model", "brand_only", or None.
    """
    results = []

    # Index brand_model and brand_only queries from both rising and top
    all_queries = trends.rising_queries + trends.top_queries

    for product, norm, heat in deduped:
        brand_lower = norm["brand"].lower()
        model_lower = norm["model"].lower()

        best_match: TrendsQuery | None = None
        best_match_type: str | None = None

        # Try brand+model match first (higher priority)
        for tq in all_queries:
            if tq.query_type != "brand_model":
                continue
            if not tq.normalized_brand or not tq.normalized_model:
                continue
            if tq.normalized_brand.lower() == brand_lower:
                # Substring containment: handles "Clifton 10" matching "Clifton 10 Wide"
                if tq.normalized_model.lower() in model_lower or model_lower in tq.normalized_model.lower():
                    best_match = tq
                    best_match_type = "brand_model"
                    break

        # Fall back to brand-only match
        if not best_match:
            for tq in all_queries:
                if tq.query_type != "brand_only":
                    continue
                if not tq.normalized_brand:
                    continue
                if tq.normalized_brand.lower() == brand_lower:
                    best_match = tq
                    best_match_type = "brand_only"
                    break

        results.append((product, norm, heat, best_match, best_match_type))

    return results


def _priority_slot(
    matched: list[tuple[RawProduct, dict, float, TrendsQuery | None, str | None]],
) -> list[tuple[RawProduct, dict, float, TrendsQuery | None, str | None, int]]:
    """Assign 5-tier priority to each product.

    Tier 1: Rising + brand_model
    Tier 2: Top + brand_model
    Tier 3: Rising + brand_only (best BSR per brand)
    Tier 4: Top + brand_only (best BSR per brand)
    Tier 5: No trend match (fallback — Heat Score)
    """
    results = []
    for product, norm, heat, tq, match_type in matched:
        if tq is None:
            tier = 5
        elif match_type == "brand_model" and tq.source == "rising":
            tier = 1
        elif match_type == "brand_model" and tq.source == "top":
            tier = 2
        elif match_type == "brand_only" and tq.source == "rising":
            tier = 3
        elif match_type == "brand_only" and tq.source == "top":
            tier = 4
        else:
            tier = 5
        results.append((product, norm, heat, tq, match_type, tier))

    return results


def _select_top_5(
    slotted: list[tuple[RawProduct, dict, float, TrendsQuery | None, str | None, int]],
) -> list[tuple[RawProduct, dict, float, TrendsQuery | None, str | None, int]]:
    """Select top 5 products using tier priority, no duplicate brand+model.

    Within each tier:
    - Tiers 1-4: sort by search_interest desc, then heat_score desc
    - Tier 5: sort by heat_score desc
    For brand-only tiers (3, 4): keep only the best BSR product per brand.
    """
    # For brand-only tiers, keep best per brand.
    # Prefer primary-source products (from category browse node search) over
    # supplemental (from brand-name search) to avoid off-category picks like
    # lifestyle shoes when trending brand is "Nike".
    brand_only_best: dict[str, tuple] = {}
    for entry in slotted:
        product, norm, heat, tq, match_type, tier = entry
        if tier in (3, 4):
            brand_key = norm["brand"].lower()
            if brand_key not in brand_only_best:
                brand_only_best[brand_key] = entry
            else:
                existing_product = brand_only_best[brand_key][0]
                is_primary = product.source == "primary"
                existing_is_primary = existing_product.source == "primary"
                # Primary source wins; within same source, higher heat score wins
                if (is_primary and not existing_is_primary) or (
                    is_primary == existing_is_primary and heat > brand_only_best[brand_key][2]
                ):
                    brand_only_best[brand_key] = entry

    # Build candidate list: filter brand-only tiers to best-per-brand
    candidates = []
    for entry in slotted:
        _, norm, heat, tq, match_type, tier = entry
        if tier in (3, 4):
            brand_key = norm["brand"].lower()
            if brand_only_best.get(brand_key) is entry:
                candidates.append(entry)
        else:
            candidates.append(entry)

    # Sort: tier asc, then search_interest desc (for tiers 1-4), heat_score desc
    def sort_key(entry):
        _, _, heat, tq, _, tier = entry
        interest = tq.search_interest if tq else 0
        return (tier, -interest, -heat)

    candidates.sort(key=sort_key)

    # Pick top 5, no duplicate brand+model, max 2 per brand for diversity
    MAX_PER_BRAND = 2
    selected = []
    seen_keys: set[str] = set()
    brand_counts: dict[str, int] = {}
    for entry in candidates:
        if len(selected) >= 5:
            break
        _, norm, _, _, _, _ = entry
        key = f"{norm['brand'].lower()}|{norm['model'].lower()}"
        if key in seen_keys:
            continue
        brand_lower = norm["brand"].lower()
        if brand_counts.get(brand_lower, 0) >= MAX_PER_BRAND:
            continue
        seen_keys.add(key)
        brand_counts[brand_lower] = brand_counts.get(brand_lower, 0) + 1
        selected.append(entry)

    return selected


def rank(
    signals: RawSignals,
    config: CategoryConfig,
    week_of: str,
    trends: TrendsData | None = None,
    force: bool = False,
) -> RankedOutput:
    """Normalize, deduplicate, score, and rank products.

    When trends data is provided, uses 5-tier priority slotting:
    rising brand+model > top brand+model > rising brand-only > top brand-only > heat score.
    When trends=None, falls back to pure Heat Score ranking (backward compatible).

    Idempotent: skips work if ranked.json exists (unless force=True).
    """
    artifact_path = runs_path(config.category_id, week_of, "ranked.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached ranked.json")
        return RankedOutput.model_validate_json(artifact_path.read_text())

    products = signals.products
    if not products:
        raise RankerError("No products to rank")

    # Step 1: Normalize brand/model names via Claude + classify category match
    logger.info("Normalizing %d product names via Claude", len(products))
    normalized = _normalize_brands(products, config)

    # Step 1b: Filter out off-category products (e.g., lifestyle sneakers, recovery slides)
    off_category = [n for n in normalized if not n.get("is_category_match", True)]
    if off_category:
        off_names = [f"{n['brand']} {n['model']}" for n in off_category]
        logger.info("Filtered %d off-category products: %s", len(off_category), off_names)
        off_asins = {n["asin"] for n in off_category}
        products = [p for p in products if p.asin not in off_asins]
        normalized = [n for n in normalized if n.get("is_category_match", True)]

    # Step 2: Deduplicate by brand+model
    deduped = _deduplicate(products, normalized)
    logger.info("After deduplication: %d unique products", len(deduped))

    # Step 3: Score all products
    scored = []
    for product, norm in deduped:
        heat = compute_heat_score(product.bsr, product.review_count, product.rating)
        scored.append((product, norm, heat))

    # Step 4: Trend-aware ranking or fallback
    if trends:
        logger.info("Applying trends-aware ranking (5-tier priority)")
        matched = _match_trends(scored, trends)
        slotted = _priority_slot(matched)
        top_5 = _select_top_5(slotted)

        tier_counts = {}
        for _, _, _, _, _, tier in top_5:
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        logger.info("Tier distribution: %s", tier_counts)
    else:
        logger.info("No trends data — falling back to Heat Score ranking")
        scored.sort(key=lambda x: x[2], reverse=True)
        top_5 = [(p, n, h, None, None, 5) for p, n, h in scored[:5]]

    ranked_products = []
    for i, (product, norm, heat, tq, match_type, tier) in enumerate(top_5, start=1):
        ranked_products.append(RankedProduct(
            rank=i,
            asin=product.asin,
            title=product.title,
            brand=norm["brand"],
            model=norm["model"],
            full_name=norm["full_name"],
            bsr=product.bsr,
            review_count=product.review_count,
            rating=product.rating,
            price_usd=product.price_usd,
            image_url=product.image_url,
            detail_page_url=product.detail_page_url,
            heat_score=heat,
            rank_change="NEW",
            trend_source=tq.source if tq else None,
            trend_match_type=match_type,
            trend_query=tq.query if tq else None,
            trend_search_interest=tq.search_interest if tq else None,
            selection_tier=tier,
        ))

    output = RankedOutput(
        category_id=config.category_id,
        week_of=week_of,
        ranked_at=datetime.now(timezone.utc),
        product_count=len(ranked_products),
        products=ranked_products,
    )

    # Save artifact
    artifact_path.write_text(output.model_dump_json(indent=2))
    logger.info(
        "Ranked %d products for %s (top: %s, tier %d)",
        len(ranked_products),
        config.category_id,
        ranked_products[0].full_name if ranked_products else "none",
        ranked_products[0].selection_tier if ranked_products else 0,
    )

    return output
