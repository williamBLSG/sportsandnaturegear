"""Content generator — calls Anthropic API to produce 10 HTML widget blocks + FAQs.

Generates a complete SoftballArticleContent with:
- 10 HTML widget slots (widget_1 through widget_10) for Duda CMS
- Product content data (editorial blurbs, standout features, etc.)
- FAQ entries as PLAIN TEXT (no HTML) for a separate Duda FAQ widget
- Social content (BlueSky posts, Pinterest pins)
- SEO metadata

The LLM receives structured product data (ranked, scored, with trends metadata)
and the article config (editorial notes, keywords, etc.) and returns JSON
validated against the Pydantic schema.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import anthropic
from pydantic import ValidationError

from softball_pipeline.models import (
    SoftballArticleConfig,
    SoftballArticleContent,
    SoftballFaqEntry,
    SoftballLinkedProduct,
    SoftballProductContent,
    softball_runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"


class ContentGeneratorError(Exception):
    pass


# ---------------------------------------------------------------------------
# Build prompt data
# ---------------------------------------------------------------------------

def _build_product_data(products: list[SoftballLinkedProduct]) -> list[dict]:
    """Build product data payload for the LLM prompt."""
    product_data = []
    for p in products:
        entry = {
            "rank": p.rank,
            "asin": p.asin,
            "brand": p.brand,
            "model": p.model,
            "full_name": p.full_name,
            "price_usd": p.price_usd,
            "rating": p.rating,
            "review_count": p.review_count,
            "bsr": p.bsr,
            "composite_score": p.composite_score,
            "price_tier": p.price_tier,
            "role": p.role,
            "affiliate_url": p.affiliate_url,
            "image_url": p.image_url,
            "is_top_brand": p.is_top_brand,
        }
        # Include trends metadata if available
        if p.trend_source:
            entry["trend_source"] = p.trend_source
            entry["trend_match_type"] = p.trend_match_type
            entry["trend_query"] = p.trend_query
            entry["trend_search_interest"] = p.trend_search_interest

        product_data.append(entry)

    return product_data


def _build_prompt(
    products: list[SoftballLinkedProduct],
    config: SoftballArticleConfig,
) -> str:
    """Build the full LLM prompt for content generation."""
    product_data = _build_product_data(products)
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Identify role assignments
    roles = {}
    for p in products:
        if p.role in ("top_pick", "budget_pick", "midrange_pick", "premium_pick"):
            roles[p.role] = p.full_name

    internal_links_text = ""
    if config.internal_links:
        links = [f'- anchor: "{il.anchor}", slug: "{il.slug}"' for il in config.internal_links]
        internal_links_text = f"""
Internal links to include where natural:
{chr(10).join(links)}
"""

    prompt = f"""You are writing a buying guide article for {config.site_name} ({config.site_url}).

TODAY'S DATE: {today}
ARTICLE: {config.display_name}
PAGE URL: {config.site_url}{config.slug}
PRIMARY KEYWORD: {config.primary_keyword}
SECONDARY KEYWORDS: {', '.join(config.secondary_keywords)}
TARGET WORD COUNT: {config.target_word_count_min}-{config.target_word_count_max} words total across all widgets

EDITORIAL NOTES:
{config.editorial_notes}

{internal_links_text}

AUDIENCE: Active Amy — women 25-50, middle-income, beginner-friendly. Many are mothers
with daughters starting sports. She's comfortable with online shopping but not a gear expert.
Write casually, be encouraging, avoid jargon (or explain it immediately). No salesy language.
No invented specs. No health/biomechanical claims. 8th-grade reading level.

PRODUCT DATA (ranked by composite score — DO NOT change the rank order):
{json.dumps(product_data, indent=2)}

ROLE ASSIGNMENTS:
- Top Pick: {roles.get('top_pick', 'N/A')}
- Budget Pick: {roles.get('budget_pick', 'N/A')}
- Mid-Range Pick: {roles.get('midrange_pick', 'N/A')}
- Premium Pick: {roles.get('premium_pick', 'N/A')}

Generate a JSON object with the following structure. ALL HTML fields must be valid HTML.
FAQ answers must be PLAIN TEXT ONLY (no HTML tags whatsoever).

{{
  "article_id": "{config.article_id}",
  "category_id": "{config.category_id}",

  "widget_1": "<HTML> Page header/intro. Include an <h1> with the page title. 2-3 intro paragraphs
    that explain what this guide covers, who it's for, and how products were selected.
    Mention rankings are based on sales data, buyer ratings, and search trends. Be welcoming to beginners.",

  "widget_2": "<HTML> 'Why You Need This Gear' section. <h2> heading. 2-3 paragraphs explaining
    why this gear matters, common use cases, and what beginners should know. Relatable scenarios.",

  "widget_3": "<HTML> 'Top Features to Look For' section. <h2> heading. Break down the most
    important features/specs for this category. Explain each in plain English. Use <h3> for each
    feature. Include how each feature affects the buying decision.",

  "widget_4": "<HTML> Comparison table. <h2> heading 'Our Top Picks at a Glance'.
    Create an HTML <table> with columns: Rank, Product, Price, Rating, Best For.
    Each product name should link to its affiliate_url. Include all {len(products)} products.",

  "widget_5": "<HTML> Top Pick spotlight. <h2> with a badge like '🏆 Top Pick'. Detailed card for
    the top-ranked product. Include: product name (linked), price, rating, review count, 2-3 sentence
    editorial blurb about why it earned the top spot. Mention trends data if available.",

  "widget_6": "<HTML> Budget Pick spotlight. <h2> with '💰 Budget Pick'. Same format as widget_5
    but emphasizing value for money.",

  "widget_7": "<HTML> Mid-Range Pick spotlight. <h2> with '⚖️ Best Mid-Range'. Same format,
    emphasizing the balance of quality and price.",

  "widget_8": "<HTML> Premium Pick spotlight. <h2> with '👑 Premium Pick'. Same format,
    emphasizing what justifies the higher price.",

  "widget_9": "<HTML> 'How to Choose' buying guidance. <h2> heading. Practical advice on
    selecting the right product. Address fit considerations, size guides, use-case differences.
    Include any category-specific advice from the editorial notes.",

  "widget_10": "<HTML> Final thoughts + CTA. <h2> heading. 1-2 paragraph wrap-up summarizing
    key takeaways. Include a clear CTA linking to the top pick. Mention the guide updates regularly.",

  "meta_title": "{config.meta_title}",
  "meta_description": "{config.meta_description}",

  "bluesky_posts": [
    "Post 1 (<300 chars): Hook + top pick mention + hashtags with emojis",
    "Post 2 (<300 chars): Different angle, mention budget pick + hashtags",
    "Post 3 (<300 chars): Seasonal/trend angle + hashtags"
  ],

  "pinterest_pins": [
    {{
      "product": "Product full name",
      "title": "Pin title (benefit-focused)",
      "description": "Pin description with CTA and 2-3 hashtags",
      "hashtags": ["#Softball", "#SoftballGear"]
    }}
  ],

  "products": [
    {{
      "rank": 1,
      "asin": "ASIN",
      "brand": "Brand",
      "model": "Model",
      "full_name": "Brand Model",
      "model_slug": "brand-model",
      "price_usd": 99.99,
      "rating": 4.5,
      "review_count": 1000,
      "bsr": 500,
      "composite_score": 75.5,
      "price_tier": "mid-range",
      "role": "top_pick",
      "affiliate_url": "https://geni.us/...",
      "image_url": "https://...",
      "image_alt": "Brand Model softball glove on white background",
      "best_for": "Short phrase: 'Best for recreational players'",
      "editorial_blurb": "2-3 sentences about the product. What makes it stand out. Who it's ideal for.",
      "standout_feature": "One standout feature in plain English"
    }}
  ],

  "faqs": [
    {{
      "question": "Plain text question that Active Amy would actually ask?",
      "answer": "Plain text answer — NO HTML TAGS. Direct, friendly, practical. 2-4 sentences.",
      "sort_order": 1
    }}
  ],

  "top_pick_asin": "ASIN of top_pick",
  "budget_asin": "ASIN of budget_pick",
  "midrange_asin": "ASIN of midrange_pick",
  "premium_asin": "ASIN of premium_pick",
  "comparison_asins": "Comma-separated ASINs of comparison products"
}}

CRITICAL RULES:
1. DO NOT invent product specifications, weights, materials, or features not implied by the product data.
2. DO NOT make health or biomechanical claims.
3. DO NOT change the rank order of products.
4. DO NOT add products not in the input data.
5. FAQ answers must be PLAIN TEXT ONLY — absolutely no HTML tags.
6. Every affiliate_url in the products array must match the input data exactly.
7. Include all {len(products)} products in the products array with their correct data.
8. Generate exactly 3 FAQ entries relevant to the category and audience.
9. Generate exactly 3 BlueSky posts.
10. Generate one Pinterest pin per product.

Return ONLY the JSON object, no other text or markdown formatting."""

    return prompt


# ---------------------------------------------------------------------------
# API calls and parsing
# ---------------------------------------------------------------------------

def _call_anthropic(prompt: str) -> str:
    """Call Claude API with the generation prompt."""
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


def _call_anthropic_with_correction(
    original_response: str,
    error_msg: str,
    config: SoftballArticleConfig,
) -> str:
    """Retry with a correction prompt when the first attempt fails validation."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ContentGeneratorError("ANTHROPIC_API_KEY must be set")

    client = anthropic.Anthropic(api_key=api_key)

    correction_prompt = f"""Your previous JSON response for the "{config.display_name}" article
had a validation error:

{error_msg}

Here is your previous response:
{original_response[:4000]}

Please fix the error and return the COMPLETE corrected JSON object.
Remember: FAQ answers must be PLAIN TEXT ONLY (no HTML tags).
Return ONLY the JSON, no other text."""

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=8192,
            messages=[{"role": "user", "content": correction_prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        raise ContentGeneratorError(f"Anthropic API correction call failed: {e}") from e


def _parse_response(response_text: str) -> dict:
    """Parse the LLM response, handling markdown code blocks."""
    text = response_text.strip()

    # Remove markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last ``` lines
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ContentGeneratorError(f"LLM returned invalid JSON: {e}") from e


def _validate_brand_model_integrity(
    content: SoftballArticleContent,
    products: list[SoftballLinkedProduct],
) -> None:
    """Verify the LLM didn't invent any products."""
    input_asins = {p.asin for p in products}

    for product in content.products:
        if product.asin not in input_asins:
            raise ContentGeneratorError(
                f"LLM invented product with ASIN {product.asin}: "
                f"{product.brand} {product.model}"
            )

    # Verify ASIN assignments
    for field_name in ("top_pick_asin", "budget_asin", "midrange_asin", "premium_asin"):
        asin = getattr(content, field_name, "")
        if asin and asin not in input_asins:
            raise ContentGeneratorError(
                f"LLM assigned unknown ASIN to {field_name}: {asin}"
            )


def _validate_faq_plain_text(content: SoftballArticleContent) -> None:
    """Verify FAQ entries contain no HTML tags."""
    html_tag_pattern = re.compile(r"<[^>]+>")

    for faq in content.faqs:
        if html_tag_pattern.search(faq.answer):
            raise ContentGeneratorError(
                f"FAQ answer contains HTML tags (must be plain text): "
                f"Q: {faq.question[:50]}..."
            )
        if html_tag_pattern.search(faq.question):
            raise ContentGeneratorError(
                f"FAQ question contains HTML tags (must be plain text): "
                f"Q: {faq.question[:50]}..."
            )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate(
    products: list[SoftballLinkedProduct],
    config: SoftballArticleConfig,
    run_date: str,
    force: bool = False,
) -> SoftballArticleContent:
    """Generate article content via Claude API.

    Idempotent: skips work if content.json exists (unless force=True).
    Validates all output before returning. Retries once on validation failure.
    """
    artifact_path = softball_runs_path(config.article_id, run_date, "content.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached content.json")
        return SoftballArticleContent.model_validate_json(artifact_path.read_text())

    if not products:
        raise ContentGeneratorError(f"No products to generate content for '{config.article_id}'")

    # Build prompt
    prompt = _build_prompt(products, config)
    logger.info(
        "Generating content for '%s' (%d products, target %d-%d words)",
        config.display_name, len(products),
        config.target_word_count_min, config.target_word_count_max,
    )

    # First attempt
    response_text = _call_anthropic(prompt)
    data = _parse_response(response_text)

    try:
        content = SoftballArticleContent(**data)
    except ValidationError as e:
        logger.warning("LLM output validation failed: %s. Retrying with correction.", e)
        response_text = _call_anthropic_with_correction(response_text, str(e), config)
        data = _parse_response(response_text)
        try:
            content = SoftballArticleContent(**data)
        except ValidationError as e2:
            raise ContentGeneratorError(
                f"LLM output failed validation after correction: {e2}"
            ) from e2

    # Validate brand/model integrity
    _validate_brand_model_integrity(content, products)

    # Validate FAQ plain text
    _validate_faq_plain_text(content)

    # Save artifact
    artifact_path.write_text(content.model_dump_json(indent=2))
    logger.info("Saved content.json for %s", config.article_id)

    return content


# ---------------------------------------------------------------------------
# Price check widget regeneration (for Sunday workflow)
# ---------------------------------------------------------------------------

def regenerate_widget_for_price_change(
    product: SoftballLinkedProduct,
    widget_number: int,
    config: SoftballArticleConfig,
    old_price: float,
    new_price: float,
) -> str:
    """Regenerate a single widget HTML when a product's price changes.

    Used by the Sunday price check workflow. Only regenerates the specific
    widget slot (5-8) for the product whose price changed.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ContentGeneratorError("ANTHROPIC_API_KEY must be set")

    role_label = {
        5: "🏆 Top Pick",
        6: "💰 Budget Pick",
        7: "⚖️ Best Mid-Range",
        8: "👑 Premium Pick",
    }.get(widget_number, "Pick")

    prompt = f"""Regenerate the HTML for a product spotlight widget on {config.site_name}.

PRODUCT:
- Name: {product.full_name}
- Brand: {product.brand}
- Model: {product.model}
- OLD Price: ${old_price:.2f}
- NEW Price: ${new_price:.2f}
- Rating: {product.rating}
- Reviews: {product.review_count}
- Role: {role_label}
- Affiliate URL: {product.affiliate_url}

Generate an HTML block with:
- <h2> heading with the role badge ({role_label})
- Product name linked to affiliate URL
- Updated price (${new_price:.2f})
- Rating and review count
- 2-3 sentence editorial blurb
- CTA button/link

Voice: casual, encouraging, beginner-friendly. No invented specs. No health claims.

Return ONLY the HTML, no markdown formatting or code blocks."""

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        html = response.content[0].text.strip()

        # Strip markdown code blocks if present
        if html.startswith("```"):
            lines = html.split("\n")
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            html = "\n".join(lines)

        return html
    except anthropic.APIError as e:
        raise ContentGeneratorError(
            f"Failed to regenerate widget {widget_number} for {product.asin}: {e}"
        ) from e
