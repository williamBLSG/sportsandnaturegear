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


def _normalize_brands(products: list[RawProduct]) -> list[dict]:
    """Use Claude to extract normalized brand and model names.

    Returns a list of dicts: [{asin, brand, model, full_name}, ...]
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

    prompt = f"""Extract the brand name and model name from each product title below.

Rules:
- "brand" = the manufacturer (e.g., "Brooks", "Nike", "ASICS", "New Balance")
- "model" = the product line name without size/color/gender (e.g., "Ghost 16", "Pegasus 41", "Gel-Kayano 31")
- "full_name" = "brand model" combined (e.g., "Brooks Ghost 16")
- Use the brand_hint field if the title is ambiguous
- Normalize capitalization: title case for brand and model
- Strip color names, size info, gender labels, and "running shoe" from the model name

Return a JSON array with one object per product:
[{{"asin": "...", "brand": "...", "model": "...", "full_name": "..."}}]

Products:
{json.dumps(product_list, indent=2)}

Return ONLY the JSON array, no other text."""

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=2048,
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


def rank(signals: RawSignals, config: CategoryConfig, week_of: str, force: bool = False) -> RankedOutput:
    """Normalize, deduplicate, score, and rank products.

    Idempotent: skips work if ranked.json exists (unless force=True).
    """
    artifact_path = runs_path(config.category_id, week_of, "ranked.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached ranked.json")
        return RankedOutput.model_validate_json(artifact_path.read_text())

    products = signals.products
    if not products:
        raise RankerError("No products to rank")

    # Step 1: Normalize brand/model names via Claude
    logger.info("Normalizing %d product names via Claude", len(products))
    normalized = _normalize_brands(products)

    # Step 2: Deduplicate by brand+model
    deduped = _deduplicate(products, normalized)
    logger.info("After deduplication: %d unique products", len(deduped))

    # Step 3: Score and sort
    scored = []
    for product, norm in deduped:
        heat = compute_heat_score(product.bsr, product.review_count, product.rating)
        scored.append((product, norm, heat))

    scored.sort(key=lambda x: x[2], reverse=True)

    # Step 4: Take top 5
    top_5 = scored[:5]

    ranked_products = []
    for i, (product, norm, heat) in enumerate(top_5, start=1):
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
            rank_change="NEW",  # Phase 1 — no prior-week comparison
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
        "Ranked %d products for %s (top heat_score: %.2f)",
        len(ranked_products),
        config.category_id,
        ranked_products[0].heat_score if ranked_products else 0,
    )

    return output
