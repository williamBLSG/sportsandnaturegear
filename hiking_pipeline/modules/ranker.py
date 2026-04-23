"""Ranker — normalizes, deduplicates, scores, and ranks hiking products.

Composite scoring:
  40% BSR (Best Sellers Rank — lower is better)
  25% Brand Authority (is it a known top brand for this category?)
  20% Google Trends (search interest match)
  15% Review Quality (rating × log(review_count))

After scoring, assigns dynamic price terciles for budget/mid-range/premium
tier classification, then assigns roles (top_pick, budget_pick, midrange_pick,
premium_pick, comparison).
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone

import anthropic

from hiking_pipeline.models import (
    HikingArticleConfig,
    HikingRankedOutput,
    HikingRankedProduct,
    HikingRawProduct,
    HikingRawSignals,
    HikingTrendsData,
    HikingTrendsQuery,
    hiking_runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"


class RankerError(Exception):
    pass


# ---------------------------------------------------------------------------
# Brand / model normalization via Claude
# ---------------------------------------------------------------------------

def _normalize_brands(
    products: list[HikingRawProduct],
    config: HikingArticleConfig,
) -> list[dict]:
    """Use Claude to extract normalized brand + model from raw product titles.

    Returns a list of dicts with keys: asin, brand, model, full_name.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RankerError("ANTHROPIC_API_KEY must be set for brand normalization")

    product_data = []
    for p in products:
        product_data.append({
            "asin": p.asin,
            "title": p.title,
            "brand": p.brand or "",
        })

    known_brands = config.top_brands + [
        "Merrell", "Salomon", "Columbia", "KEEN", "Danner", "Vasque",
        "Osprey", "Deuter", "Gregory", "CamelBak", "MYSTERY RANCH",
        "The North Face", "Black Diamond", "LEKI", "TrailBuddy",
        "Cascade Mountain Tech", "Hydro Flask", "YETI", "Stanley",
        "Chaco", "Teva", "prAna", "Kuhl", "Arc'teryx", "Eddie Bauer",
        "REI Co-op", "Hi-Tec", "NORTIV 8", "Timberland", "La Sportiva",
        "Teton Sports", "Iron Flask", "Simple Modern", "Nalgene",
        "Adventure Medical Kits", "Surviveware", "BALEAF", "Willit",
        "New Balance", "Under Armour", "Nike", "Adidas",
    ]
    # Deduplicate
    known_brands = sorted(set(known_brands))

    prompt = f"""Extract the brand name and model name from each hiking product title.

Known hiking brands: {', '.join(known_brands)}

Rules:
- Brand should be title-case and match the known brand list when possible
- Model should be the specific product name WITHOUT color, size, gender, or "hiking" qualifier
  Example: "Merrell Women's Moab 3 Mid Waterproof Hiking Boot" → brand: "Merrell", model: "Moab 3 Mid"
  Example: "Osprey Daylite Plus Daypack" → brand: "Osprey", model: "Daylite Plus"
  Example: "Black Diamond Trail Trekking Poles" → brand: "Black Diamond", model: "Trail"
- full_name is "{{brand}} {{model}}"
- If brand can't be determined from the title or brand field, use the brand field value
- If model can't be determined, use a short descriptive name from the title

Return a JSON array:
[{{"asin": "...", "brand": "...", "model": "...", "full_name": "..."}}]

Products:
{json.dumps(product_data, indent=2)}

Return ONLY the JSON array, no other text."""

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=16384,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text.strip()

        # Handle markdown code blocks
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            response_text = "\n".join(lines)

        normalized = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise RankerError(f"Claude returned invalid JSON for brand normalization: {e}") from e
    except anthropic.APIError as e:
        raise RankerError(f"Anthropic API error during brand normalization: {e}") from e

    return normalized


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deduplicate(
    products: list[HikingRawProduct],
    normalized: list[dict],
) -> list[tuple[HikingRawProduct, dict]]:
    """Deduplicate products by brand+model, keeping the one with best BSR.

    Returns list of (product, norm_data) tuples.
    """
    norm_by_asin = {n["asin"]: n for n in normalized}

    # Group by (brand, model)
    groups: dict[tuple[str, str], list[tuple[HikingRawProduct, dict]]] = {}
    for p in products:
        norm = norm_by_asin.get(p.asin)
        if not norm:
            continue
        key = (norm["brand"].lower(), norm["model"].lower())
        groups.setdefault(key, []).append((p, norm))

    # Keep best BSR per group (lower BSR = better)
    deduped = []
    for key, group in groups.items():
        # Sort by BSR ascending (None last)
        group.sort(key=lambda x: x[0].bsr if x[0].bsr is not None else 999999999)
        deduped.append(group[0])

    return deduped


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def _compute_bsr_score(bsr: int | None, all_bsrs: list[int]) -> float:
    """Normalize BSR to 0-100 scale (lower BSR = higher score).

    Uses min-max normalization inverted so rank #1 gets 100.
    """
    if bsr is None or not all_bsrs:
        return 25.0  # Default for products without BSR data

    min_bsr = min(all_bsrs)
    max_bsr = max(all_bsrs)

    if max_bsr == min_bsr:
        return 75.0  # All same rank

    # Invert: lower BSR = higher score
    return ((max_bsr - bsr) / (max_bsr - min_bsr)) * 100.0


def _compute_brand_authority_score(brand: str, config: HikingArticleConfig) -> float:
    """Score based on whether the brand is in the config's top_brands list.

    Top brands get 100, others get 30.
    """
    brand_lower = brand.lower()
    for tb in config.top_brands:
        if tb.lower() == brand_lower:
            return 100.0
    return 30.0


def _compute_trends_score(
    brand: str,
    model: str,
    trends: HikingTrendsData | None,
) -> tuple[float, str | None, str | None, str | None, int | None]:
    """Score based on Google Trends match.

    Returns (score, trend_source, trend_match_type, trend_query, trend_search_interest).
    brand_model match = 100, brand_only match = 60, no match = 0.
    """
    if not trends:
        return 0.0, None, None, None, None

    all_queries = trends.rising_queries + trends.top_queries
    brand_lower = brand.lower()
    model_lower = model.lower()

    # Check for brand_model match first (highest value)
    for q in all_queries:
        if q.query_type == "brand_model":
            if (q.normalized_brand and q.normalized_brand.lower() == brand_lower and
                    q.normalized_model and q.normalized_model.lower() == model_lower):
                return 100.0, q.source, "brand_model", q.query, q.search_interest

    # Check for brand_only match
    for q in all_queries:
        if q.query_type in ("brand_model", "brand_only"):
            if q.normalized_brand and q.normalized_brand.lower() == brand_lower:
                return 60.0, q.source, "brand_only", q.query, q.search_interest

    return 0.0, None, None, None, None


def _compute_review_quality_score(
    rating: float | None,
    review_count: int | None,
) -> float:
    """Score based on rating × log(review_count).

    Normalized to ~0-100 range. Products with more and better reviews score higher.
    """
    if rating is None or review_count is None or review_count <= 0:
        return 10.0  # Minimal score for no review data

    # rating is typically 1-5, log10(review_count) typically 1-5
    # So raw score is roughly 1-25
    raw = rating * math.log10(max(review_count, 1))

    # Normalize: assume max realistic is 5.0 * log10(50000) ≈ 5 * 4.7 = 23.5
    normalized = min((raw / 23.5) * 100.0, 100.0)
    return normalized


def _compute_composite_score(
    bsr_score: float,
    brand_authority_score: float,
    trends_score: float,
    review_quality_score: float,
) -> float:
    """Weighted composite: 40% BSR, 25% brand authority, 20% trends, 15% review quality."""
    return (
        0.40 * bsr_score +
        0.25 * brand_authority_score +
        0.20 * trends_score +
        0.15 * review_quality_score
    )


# ---------------------------------------------------------------------------
# Price terciles and tier assignment
# ---------------------------------------------------------------------------

def _assign_price_tiers(
    products: list[HikingRankedProduct],
) -> tuple[float | None, float | None]:
    """Assign price_tier based on dynamic terciles.

    Returns (budget_ceiling, premium_floor) for transparency.
    Products without price data are assigned "mid-range".
    """
    priced = [p for p in products if p.price_usd is not None]
    if len(priced) < 3:
        # Too few products for meaningful terciles — assign all as mid-range
        for p in products:
            p.price_tier = "mid-range"
        return None, None

    prices = sorted([p.price_usd for p in priced])
    n = len(prices)

    # Tercile boundaries
    budget_ceiling = prices[n // 3]
    premium_floor = prices[(2 * n) // 3]

    for p in products:
        if p.price_usd is None:
            p.price_tier = "mid-range"
        elif p.price_usd <= budget_ceiling:
            p.price_tier = "budget"
        elif p.price_usd >= premium_floor:
            p.price_tier = "premium"
        else:
            p.price_tier = "mid-range"

    return budget_ceiling, premium_floor


# ---------------------------------------------------------------------------
# Role assignment
# ---------------------------------------------------------------------------

def _assign_roles(products: list[HikingRankedProduct]) -> None:
    """Assign roles: top_pick, budget_pick, midrange_pick, premium_pick, comparison.

    top_pick = highest composite score overall
    budget_pick = highest composite score among "budget" tier
    midrange_pick = highest composite score among "mid-range" tier
    premium_pick = highest composite score among "premium" tier
    comparison = everything else
    """
    if not products:
        return

    # Sort by composite score descending
    sorted_products = sorted(products, key=lambda p: p.composite_score, reverse=True)

    # Top pick is #1 overall
    sorted_products[0].role = "top_pick"
    assigned_asins = {sorted_products[0].asin}

    # Budget pick — best score in budget tier (not already assigned)
    budget_candidates = [
        p for p in sorted_products
        if p.price_tier == "budget" and p.asin not in assigned_asins
    ]
    if budget_candidates:
        budget_candidates[0].role = "budget_pick"
        assigned_asins.add(budget_candidates[0].asin)

    # Mid-range pick
    midrange_candidates = [
        p for p in sorted_products
        if p.price_tier == "mid-range" and p.asin not in assigned_asins
    ]
    if midrange_candidates:
        midrange_candidates[0].role = "midrange_pick"
        assigned_asins.add(midrange_candidates[0].asin)

    # Premium pick
    premium_candidates = [
        p for p in sorted_products
        if p.price_tier == "premium" and p.asin not in assigned_asins
    ]
    if premium_candidates:
        premium_candidates[0].role = "premium_pick"
        assigned_asins.add(premium_candidates[0].asin)

    # Everyone else is "comparison"
    for p in products:
        if p.asin not in assigned_asins:
            p.role = "comparison"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def rank(
    signals: HikingRawSignals,
    config: HikingArticleConfig,
    run_date: str,
    trends: HikingTrendsData | None = None,
    force: bool = False,
) -> HikingRankedOutput:
    """Normalize, deduplicate, score, and rank hiking products.

    Idempotent: skips work if ranked.json exists (unless force=True).
    Returns HikingRankedOutput with products sorted by composite score.
    """
    artifact_path = hiking_runs_path(config.article_id, run_date, "ranked.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached ranked.json")
        return HikingRankedOutput.model_validate_json(artifact_path.read_text())

    if not signals.products:
        raise RankerError(f"No products to rank for '{config.article_id}'")

    # Step 1: Normalize brand/model names via Claude
    logger.info("Normalizing brand/model names for %d products", len(signals.products))
    normalized = _normalize_brands(signals.products, config)

    # Step 2: Deduplicate by brand+model
    deduped = _deduplicate(signals.products, normalized)
    logger.info("Deduplication: %d → %d products", len(signals.products), len(deduped))

    if not deduped:
        raise RankerError(f"No products survived deduplication for '{config.article_id}'")

    # Step 3: Compute individual scores
    all_bsrs = [p.bsr for p, _ in deduped if p.bsr is not None]

    scored_products: list[HikingRankedProduct] = []
    for product, norm in deduped:
        bsr_score = _compute_bsr_score(product.bsr, all_bsrs)
        brand_authority_score = _compute_brand_authority_score(norm["brand"], config)

        trends_score, trend_source, trend_match_type, trend_query, trend_search_interest = (
            _compute_trends_score(norm["brand"], norm["model"], trends)
        )

        review_quality_score = _compute_review_quality_score(product.rating, product.review_count)

        composite = _compute_composite_score(
            bsr_score, brand_authority_score, trends_score, review_quality_score,
        )

        is_top_brand = any(
            tb.lower() == norm["brand"].lower() for tb in config.top_brands
        )

        scored_products.append(HikingRankedProduct(
            rank=0,  # Assigned after sorting
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
            composite_score=round(composite, 2),
            bsr_score=round(bsr_score, 2),
            brand_authority_score=round(brand_authority_score, 2),
            trends_score=round(trends_score, 2),
            review_quality_score=round(review_quality_score, 2),
            trend_source=trend_source,
            trend_match_type=trend_match_type,
            trend_query=trend_query,
            trend_search_interest=trend_search_interest,
            is_top_brand=is_top_brand,
        ))

    # Step 4: Sort by composite score descending, take top N
    scored_products.sort(key=lambda p: p.composite_score, reverse=True)
    target_count = config.product_count_target
    selected = scored_products[:target_count]

    # Assign ranks
    for i, p in enumerate(selected, start=1):
        p.rank = i

    logger.info(
        "Selected top %d products (from %d scored)",
        len(selected), len(scored_products),
    )

    # Step 5: Assign price tiers via dynamic terciles
    budget_ceiling, premium_floor = _assign_price_tiers(selected)
    tier_counts = {}
    for p in selected:
        tier_counts[p.price_tier] = tier_counts.get(p.price_tier, 0) + 1
    logger.info("Price tiers: %s", tier_counts)

    # Step 6: Assign roles
    _assign_roles(selected)
    role_counts = {}
    for p in selected:
        role_counts[p.role] = role_counts.get(p.role, 0) + 1
    logger.info("Roles: %s", role_counts)

    output = HikingRankedOutput(
        article_id=config.article_id,
        category_id=config.category_id,
        ranked_at=datetime.now(timezone.utc),
        product_count=len(selected),
        products=selected,
        budget_ceiling=budget_ceiling,
        premium_floor=premium_floor,
    )

    # Save artifact
    artifact_path.write_text(output.model_dump_json(indent=2))
    logger.info("Saved ranked.json for %s", config.article_id)

    return output
