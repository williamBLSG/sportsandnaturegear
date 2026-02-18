"""Content generator — calls Anthropic API to generate HTML content for roundups."""

from __future__ import annotations

import json
import logging
import os

import anthropic

from pipeline.models import (
    CategoryConfig,
    LinkedProduct,
    WeeklyRoundup,
    runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"


class ContentGeneratorError(Exception):
    pass


def _build_product_data(products: list[LinkedProduct]) -> str:
    """Build a JSON string of product data for the LLM prompt."""
    data = []
    for p in products:
        data.append({
            "rank": p.rank,
            "brand": p.brand,
            "model": p.model,
            "full_name": p.full_name,
            "model_slug": p.model_slug,
            "asin": p.asin,
            "price_usd": p.price_usd,
            "rating": p.rating,
            "review_count": p.review_count,
            "bsr": p.bsr,
            "heat_score": p.heat_score,
            "rank_change": p.rank_change,
            "geniuslink_url": p.affiliate_url,
            "amazon_url": p.detail_page_url,
            "primary_image_url": p.image_url,
        })
    return json.dumps(data, indent=2)


def _build_prompt(
    products: list[LinkedProduct],
    config: CategoryConfig,
    week_of: str,
) -> str:
    """Build the content generation prompt."""
    product_data = _build_product_data(products)
    slug = f"{config.category_id}-trending-{week_of}"

    return f"""You are a content writer for {config.site_name} ({config.site_url}).

Your audience is "Active Amy" — women 25-50, middle-income, often mothers with daughters starting sports, but about 30% are women without children pursuing personal growth. She is NOT a gear expert. She shops on Amazon, uses Pinterest and Instagram, and needs beginner-friendly, encouraging content. Never assume she is a mother.

You are generating content for the weekly trending {config.display_name} roundup page for the week of {week_of}.

PRODUCT DATA (ranked by Heat Score — do NOT change the rank order):
{product_data}

Generate a JSON object with this EXACT structure. Every field listed below is required:

{{
  "slug": "{slug}",
  "category_id": "{config.category_id}",
  "week_of": "{week_of}",
  "h1_title": "...",
  "meta_title": "...",
  "meta_description": "...",
  "intro": "...",
  "methodology": "...",
  "trend_insight": "...",
  "faqs": "...",
  "affiliate_disclosure": "...",
  "products": [
    {{
      "rank": 1,
      "asin": "...",
      "brand": "...",
      "model": "...",
      "full_name": "...",
      "model_slug": "...",
      "geniuslink_url": "...",
      "amazon_url": "...",
      "primary_image_url": "...",
      "image_alt": "...",
      "price_usd": ...,
      "rating": ...,
      "review_count": ...,
      "bsr": ...,
      "heat_score": ...,
      "rank_change": "...",
      "best_for": "...",
      "why_hot": "...",
      "short_specs": "..."
    }}
  ]
}}

FIELD INSTRUCTIONS:

h1_title: The main page heading. Example: "Top 5 {config.display_name} This Week". Friendly and clear.

meta_title: SEO title with week, category, and benefit phrase. Example format: "Top 5 {config.display_name} This Week — Ranked by Buyers | {config.site_name}". Under 70 chars.

meta_description: 140-155 characters. Mention the #1 brand, note weekly updates, light CTA. Example: "Brooks leads this week's top 5 {config.product_type}, ranked by Amazon sales and buyer ratings. Updated weekly. Find your fit →"

intro: 2-3 short paragraphs in <p> tags. Reference the specific week ({week_of}). Briefly explain rankings are based on sales momentum and buyer ratings (no formula details). Acknowledge choosing can feel overwhelming, then reassure. Casual, encouraging tone. Do NOT use jargon without explaining it.

methodology: 1-2 short paragraphs in <p> tags explaining how products are ranked. Mention Amazon sales data, buyer ratings, and review counts. Keep it simple and transparent for Amy.

trend_insight: 1-2 short paragraphs in <p> tags noting any interesting trends this week (e.g., a brand dominating, price trends, new entries). Use the product data to make observations.

faqs: 3-5 Q&A pairs using <details>/<summary> HTML. Address Amy's pain points: how rankings work, fit/sizing tips for {config.product_type}, beginner suitability, value vs. price. Do NOT make health/biomechanical claims. Do NOT invent specs.

affiliate_disclosure: A short, transparent affiliate disclosure statement in a <p> tag. Example: "<p>We may earn a small commission when you purchase through our links — at no extra cost to you. This helps us keep our rankings updated weekly.</p>"

For EACH product in the products array, copy these fields EXACTLY from the input data: rank, asin, brand, model, full_name, model_slug, geniuslink_url, amazon_url, primary_image_url, price_usd, rating, review_count, bsr, heat_score, rank_change.

image_alt: Descriptive alt text for the product image. Example: "Brooks Ghost 16 women's running shoe"

best_for: A short phrase describing who this shoe is best for. Example: "Everyday runners looking for comfort". Keep it beginner-friendly.

why_hot: 2-3 sentences explaining why this product is trending. Reference real data (rating, review count, BSR rank) — do NOT invent specs like weight, drop height, foam type, or stack height. Write in Amy's voice: casual, encouraging, beginner-friendly. Use <p> tags.

short_specs: A <ul> list with 3-5 items using ONLY data from the input: price, BSR rank, rating, review count. Do NOT invent specifications.

CRITICAL RULES:
- Do NOT invent product specifications (weight, drop, foam type, stack height, cushioning)
- Do NOT make health or biomechanical claims
- Do NOT change the rank order
- Do NOT add products not in the input data
- Brand and model names must match the input EXACTLY
- Copy model_slug, geniuslink_url, amazon_url, primary_image_url exactly from input
- Use ONLY data provided — rating, review_count, price_usd, bsr, heat_score
- If a technical term must appear, explain it in plain English in the same sentence
- Write for both mothers and women without children — never assume one or the other
- All numeric values (price_usd, rating, review_count, bsr, heat_score) must be numbers, not strings
- If a numeric value is null in the input, use null in the output

Return ONLY the JSON object, no other text or markdown formatting."""


def _call_anthropic(prompt: str) -> str:
    """Call the Anthropic API and return the response text."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ContentGeneratorError("ANTHROPIC_API_KEY must be set")

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        raise ContentGeneratorError(f"Anthropic API error: {e}") from e


def _parse_response(response_text: str) -> dict:
    """Parse the LLM response, handling optional markdown code blocks."""
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)


def _validate_brand_model_integrity(
    roundup: WeeklyRoundup,
    products: list[LinkedProduct],
) -> None:
    """Verify the LLM didn't invent any products."""
    input_names = {(p.brand, p.model) for p in products}
    for product in roundup.products:
        if (product.brand, product.model) not in input_names:
            raise ContentGeneratorError(
                f"LLM invented product: {product.brand} {product.model}"
            )


def generate(
    linked: list[LinkedProduct],
    config: CategoryConfig,
    week_of: str,
    force: bool = False,
) -> WeeklyRoundup:
    """Generate content for a weekly roundup using Claude.

    Idempotent: skips work if canonical.json exists (unless force=True).
    Validates LLM output via Pydantic and brand/model integrity check.
    Retries once on validation failure; aborts on second failure.
    """
    artifact_path = runs_path(config.category_id, week_of, "canonical.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached canonical.json")
        return WeeklyRoundup.model_validate_json(artifact_path.read_text())

    prompt = _build_prompt(linked, config, week_of)

    # First attempt
    logger.info("Generating content via Claude (%s)", MODEL_ID)
    response_text = _call_anthropic(prompt)

    try:
        data = _parse_response(response_text)
        roundup = WeeklyRoundup(**data)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("First LLM attempt invalid: %s. Retrying with correction.", e)

        # Retry with correction prompt
        correction_prompt = (
            f"Your previous response was invalid. Error: {e}\n\n"
            f"Original response:\n{response_text}\n\n"
            f"Please fix the JSON and return ONLY a valid JSON object. "
            f"Do not include markdown code blocks."
        )
        response_text = _call_anthropic(prompt + "\n\n" + correction_prompt)
        try:
            data = _parse_response(response_text)
            roundup = WeeklyRoundup(**data)
        except (json.JSONDecodeError, Exception) as e2:
            raise ContentGeneratorError(
                f"LLM output invalid after retry: {e2}"
            ) from e2

    # Validate brand/model integrity
    _validate_brand_model_integrity(roundup, linked)

    # Save artifact
    artifact_path.write_text(roundup.model_dump_json(indent=2))
    logger.info("Content generated: %s (%d products)", roundup.slug, len(roundup.products))

    return roundup
