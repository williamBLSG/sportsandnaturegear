"""Content generator — calls Anthropic API to produce 10 HTML widget blocks + FAQs.

Generates a complete BikingArticleContent with:
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

from biking_pipeline.models import (
    BikingArticleConfig,
    BikingArticleContent,
    BikingFaqEntry,
    BikingLinkedProduct,
    BikingProductContent,
    biking_runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"


class ContentGeneratorError(Exception):
    pass


# ---------------------------------------------------------------------------
# Build prompt data
# ---------------------------------------------------------------------------

def _build_product_data(products: list[BikingLinkedProduct]) -> list[dict]:
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
    products: list[BikingLinkedProduct],
    config: BikingArticleConfig,
) -> str:
    """Build the full LLM prompt for content generation."""
    product_data = _build_product_data(products)
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Identify role assignments
    roles = {}
    role_products = {}
    for p in products:
        if p.role in ("top_pick", "budget_pick", "midrange_pick", "premium_pick"):
            roles[p.role] = p.full_name
            role_products[p.role] = p

    internal_links_text = ""
    if config.internal_links:
        links = [f'- anchor: "{il.anchor}", slug: "{il.slug}"' for il in config.internal_links]
        internal_links_text = f"""
Internal links to include where natural:
{chr(10).join(links)}
"""

    # Build top pick data for the Our Top Pick widget example
    tp = role_products.get("top_pick")
    top_pick_example = ""
    if tp:
        tp_rating = f"{tp.rating} stars" if tp.rating else "Not yet rated"
        top_pick_example = f"""
The Our Top Pick product is: {tp.full_name}
- Price: ${tp.price_usd:.2f} if {tp.price_usd} else 'N/A'
- Rating: {tp_rating}
- Affiliate URL: {tp.affiliate_url}
"""

    # Build three-tier data
    bp = role_products.get("budget_pick")
    mp = role_products.get("midrange_pick")
    pp = role_products.get("premium_pick")

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

{top_pick_example}

=== HTML FORMATTING RULE (CRITICAL) ===
Every widget HTML value MUST be a single-line blob — NO line breaks, NO newlines, NO \\n characters.
All HTML goes on one continuous line. Example of CORRECT widget_1 output:
"widget_1": "<p>Whether your daughter just signed up for her first rec league...</p><p>This article covers the best fastpitch biking bats available right now...</p>"

Do NOT include any \\n or newline characters inside widget HTML strings. One unbroken string.

=== DO NOT INCLUDE H1 IN ANY WIDGET ===
The H1 is handled natively by Duda CMS. Do NOT put an <h1> tag in widget_1 or any other widget.
widget_1 should start directly with <p> intro paragraphs.

=== INLINE CSS FOR STYLED WIDGETS ===
Widgets 4, 5, and 6 require inline CSS styles since Duda HTML widgets don't load external stylesheets.
Use style='...' attributes directly on HTML elements.

Generate a JSON object with the following structure. ALL HTML fields must be valid HTML blobs (single line, no line breaks).
FAQ answers must be PLAIN TEXT ONLY (no HTML tags whatsoever).

{{
  "article_id": "{config.article_id}",
  "category_id": "{config.category_id}",

  "widget_1": "<HTML BLOB> Intro paragraphs only — NO H1. 2-3 paragraphs that explain what this guide covers,
    who it's for, and how products were selected. Mention rankings are based on sales data, buyer ratings,
    and search trends. Be welcoming to beginners. Start with <p>, not <h1>.",

  "widget_2": "<HTML BLOB> 'Our Top Pick' styled card module. This is a visually distinct callout card.
    Use this EXACT inline-CSS HTML structure:
    <div style='background:#f5f9f5;border:1px solid #e0e8e0;border-radius:10px;padding:24px;font-family:sans-serif;max-width:700px;'><div style='display:flex;align-items:flex-start;gap:16px;'><span style='background:#4a7c59;color:white;font-size:13px;font-weight:600;padding:4px 14px;border-radius:20px;white-space:nowrap;'>Our top pick</span><div><p style='margin:0 0 12px 0;font-size:16px;line-height:1.5;'><strong>[Product Name]</strong> — [1-2 sentence blurb about why it's the top pick. Mention key appeal, price point, who it's best for. Casual, encouraging tone.]</p><a href='[affiliate_url]' style='display:inline-block;border:1px solid #999;border-radius:6px;padding:8px 18px;text-decoration:none;color:#333;font-size:14px;'>Shop on Amazon &rarr;</a></div></div></div>
    Fill in the actual product name, blurb, and affiliate URL for the top_pick product.",

  "widget_3": "<HTML BLOB> 'Why You Need This Gear' section. <h2> heading. 2-3 paragraphs explaining
    why this gear matters, common use cases, and what beginners should know. Relatable scenarios.",

  "widget_4": "<HTML BLOB> 'The Three Tiers: budget, mid-range, and premium' module.
    Use this EXACT inline-CSS HTML structure:
    <h2 style='font-family:sans-serif;font-size:22px;margin-bottom:6px;'>The three tiers: budget, mid-range, and premium</h2><p style='font-family:sans-serif;color:#555;margin-bottom:20px;'>[1-2 sentences explaining why you organized picks by price — the right answer depends on budget and commitment level. Use a relatable example.]</p><div style='display:flex;gap:16px;flex-wrap:wrap;'><div style='flex:1;min-width:200px;border:1px solid #e0e0e0;border-radius:10px;padding:20px;font-family:sans-serif;'><p style='font-size:12px;color:#666;margin:0 0 8px 0;'>Budget &middot; under $XX</p><p style='font-weight:600;margin:0 0 4px 0;'>[Budget Product Name]</p><p style='font-size:22px;font-weight:700;margin:0 0 6px 0;'>$XX</p><p style='color:#c17328;font-size:14px;margin:0 0 10px 0;'>[star rating display] [rating number]</p><p style='font-size:14px;line-height:1.5;color:#444;margin:0 0 14px 0;'>[2-3 sentence blurb about this budget pick]</p><a href='[budget_affiliate_url]' style='display:inline-block;border:1px solid #999;border-radius:6px;padding:8px 18px;text-decoration:none;color:#333;font-size:14px;'>Shop on Amazon</a></div><div style='flex:1;min-width:200px;border:2px solid #c17328;border-radius:10px;padding:20px;font-family:sans-serif;'><p style='font-size:12px;color:#c17328;font-weight:600;margin:0 0 8px 0;'>Best value &middot; $XX&ndash;$XX</p><p style='font-weight:600;margin:0 0 4px 0;'>[Mid-Range Product Name]</p><p style='font-size:22px;font-weight:700;margin:0 0 6px 0;'>$XX</p><p style='color:#c17328;font-size:14px;margin:0 0 10px 0;'>[star rating display] [rating number]</p><p style='font-size:14px;line-height:1.5;color:#444;margin:0 0 14px 0;'>[2-3 sentence blurb about this mid-range pick]</p><a href='[midrange_affiliate_url]' style='display:inline-block;border:1px solid #999;border-radius:6px;padding:8px 18px;text-decoration:none;color:#333;font-size:14px;'>Shop on Amazon</a></div><div style='flex:1;min-width:200px;border:1px solid #e0e0e0;border-radius:10px;padding:20px;font-family:sans-serif;'><p style='font-size:12px;color:#666;margin:0 0 8px 0;'>Premium &middot; $XX+</p><p style='font-weight:600;margin:0 0 4px 0;'>[Premium Product Name]</p><p style='font-size:22px;font-weight:700;margin:0 0 6px 0;'>$XX</p><p style='color:#c17328;font-size:14px;margin:0 0 10px 0;'>[star rating display] [rating number]</p><p style='font-size:14px;line-height:1.5;color:#444;margin:0 0 14px 0;'>[2-3 sentence blurb about this premium pick]</p><a href='[premium_affiliate_url]' style='display:inline-block;border:1px solid #999;border-radius:6px;padding:8px 18px;text-decoration:none;color:#333;font-size:14px;'>Shop on Amazon</a></div></div>
    Fill in real product names, prices, ratings, blurbs, and affiliate URLs for the budget_pick, midrange_pick, and premium_pick products.
    For star ratings, use Unicode stars: ★ for full stars, ☆ for empty. Example: ★★★★☆ 4.1
    The mid-range card has a thicker orange border (2px solid #c17328) and orange tier label to highlight it as 'Best value'.",

  "widget_5": "<HTML BLOB> 'Top Features to Look For' section. <h2> heading. Break down the most
    important features/specs for this category. Explain each in plain English. Use <h3> for each
    feature. Include how each feature affects the buying decision.",

  "widget_6": "<HTML BLOB> Comparison table. <h2> heading 'Our Top Picks at a Glance'.
    Create an HTML <table> with inline CSS styling for readability. Columns: Rank, Product, Price, Rating, Best For.
    Each product name should be an <a> linking to its affiliate_url.
    Include all {len(products)} products.
    Table styling: use inline styles for borders, padding, alternating row colors, header background.
    Example structure: <h2 style='color:#c17328;font-family:sans-serif;'>Our Top Picks at a Glance</h2><table style='width:100%;border-collapse:collapse;font-family:sans-serif;'><thead><tr style='background:#f5f5f5;'><th style='padding:10px;text-align:left;border-bottom:2px solid #ddd;'>Rank</th>...</tr></thead><tbody>...</tbody></table>",

  "widget_7": "<HTML BLOB> 'How to Choose' buying guidance. <h2> heading. Practical advice on
    selecting the right product. Address fit considerations, size guides, use-case differences.
    Include any category-specific advice from the editorial notes.",

  "widget_8": "<HTML BLOB> Final thoughts + CTA. <h2> heading. 1-2 paragraph wrap-up summarizing
    key takeaways. Include a clear CTA linking to the top pick. Mention the guide updates regularly.",

  "widget_9": "",
  "widget_10": "",

  "meta_title": "SEO title under 65 chars using Amy's search language",
  "meta_description": "SEO description 140-155 chars, mention top brand, note weekly updates, light CTA",

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
      "hashtags": ["#Biking", "#BikingGear"]
    }}
  ],

  "products": [
    {{
      "rank": 1,
      "asin": "ASIN from input data",
      "brand": "Brand from input data",
      "model": "Model from input data",
      "full_name": "Brand Model from input data",
      "model_slug": "brand-model",
      "price_usd": 99.99,
      "rating": 4.5,
      "review_count": 1000,
      "bsr": 500,
      "composite_score": 75.5,
      "price_tier": "mid-range",
      "role": "top_pick",
      "affiliate_url": "https://geni.us/... from input data",
      "image_url": "https://... from input data",
      "image_alt": "Brand Model on white background",
      "best_for": "Short phrase: 'Best for recreational players'",
      "editorial_blurb": "2-3 sentences about the product. What makes it stand out. Who it's ideal for.",
      "standout_feature": "One standout feature in plain English",
      "list_title": "Role Label: Product Name (e.g. 'Top Pick: Easton Moxie')",
      "list_description": "Plain text (NO HTML). Format exactly like this example:\\nPrice: $36.98\\nRating: 4.5 stars (1,200 reviews) OR 'Not yet rated'\\n\\n[2-4 sentence editorial paragraph about why this product earned its role. Casual tone, same voice as the editorial_blurb but slightly longer and more detailed. Mention what makes it a good fit for the target audience.]",
      "list_cta_text": "Check price on Amazon"
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
11. DO NOT include an <h1> tag anywhere — Duda CMS handles the H1.
12. All widget HTML must be a SINGLE LINE with NO line breaks, NO \\n characters. One continuous HTML string.
13. list_title, list_description, and list_cta_text are ONLY required for products with roles:
    top_pick, budget_pick, midrange_pick, premium_pick. For comparison products, set them to empty strings.
14. list_description is PLAIN TEXT with \\n for line breaks (not HTML). Include Price, Rating, then an editorial paragraph.
15. widget_9 and widget_10 must be empty strings "".

JSON FORMATTING — THIS IS CRITICAL:
- All HTML in widget fields MUST use single quotes for HTML attributes (e.g., <a href='https://...'> NOT <a href="https://...">).
  This prevents double quotes inside JSON strings from breaking the JSON structure.
- Widget HTML values must be a single unbroken line — no newlines inside the HTML string.
- Escape any double quotes inside string values with a backslash: \\"
- The output must be valid, parseable JSON. If in doubt, use simpler HTML with fewer attributes.
- Do NOT wrap the output in markdown code blocks — return raw JSON only.

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
            max_tokens=16384,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        raise ContentGeneratorError(f"Anthropic API error: {e}") from e


def _call_anthropic_with_correction(
    original_response: str,
    error_msg: str,
    config: BikingArticleConfig,
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
            max_tokens=16384,
            messages=[{"role": "user", "content": correction_prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        raise ContentGeneratorError(f"Anthropic API correction call failed: {e}") from e


def _parse_response(response_text: str) -> dict:
    """Parse the LLM response, handling markdown code blocks and common issues."""
    text = response_text.strip()

    # Remove markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    # Attempt 1: try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt 2: try to extract JSON object from surrounding text
    # (LLM sometimes adds explanation before/after)
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        extracted = text[brace_start:brace_end + 1]
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    # Attempt 3: try fixing control characters in strings
    try:
        # Replace literal tabs and other control chars that break JSON
        cleaned = text.replace("\t", "\\t")
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Log first 500 chars for debugging
        logger.error("JSON parse failed. First 500 chars: %s", text[:500])
        raise ContentGeneratorError(f"LLM returned invalid JSON: {e}") from e


def _validate_brand_model_integrity(
    content: BikingArticleContent,
    products: list[BikingLinkedProduct],
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


def _validate_faq_plain_text(content: BikingArticleContent) -> None:
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
    products: list[BikingLinkedProduct],
    config: BikingArticleConfig,
    run_date: str,
    force: bool = False,
) -> BikingArticleContent:
    """Generate article content via Claude API.

    Idempotent: skips work if content.json exists (unless force=True).
    Validates all output before returning. Retries once on validation failure.
    """
    artifact_path = biking_runs_path(config.article_id, run_date, "content.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached content.json")
        return BikingArticleContent.model_validate_json(artifact_path.read_text())

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

    try:
        data = _parse_response(response_text)
        content = BikingArticleContent(**data)
    except (ContentGeneratorError, ValidationError) as e:
        logger.warning("LLM output failed (attempt 1): %s. Retrying with correction.", e)
        response_text = _call_anthropic_with_correction(response_text, str(e), config)
        try:
            data = _parse_response(response_text)
            content = BikingArticleContent(**data)
        except (ContentGeneratorError, ValidationError) as e2:
            raise ContentGeneratorError(
                f"LLM output failed after correction retry: {e2}"
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
    product: BikingLinkedProduct,
    widget_number: int,
    config: BikingArticleConfig,
    old_price: float,
    new_price: float,
    all_tier_products: list[BikingLinkedProduct] | None = None,
) -> str:
    """Regenerate a single widget HTML when a product's price changes.

    Used by the Sunday price check workflow.
    - widget_2 (Our Top Pick card): regenerates the styled top pick card
    - widget_4 (Three Tiers): regenerates all three tier cards (needs all_tier_products)

    Output is a single-line HTML blob with inline CSS (no line breaks).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ContentGeneratorError("ANTHROPIC_API_KEY must be set")

    if widget_number == 2:
        # Top Pick card
        prompt = f"""Regenerate the 'Our Top Pick' HTML card for {config.site_name}.

PRODUCT:
- Name: {product.full_name}
- NEW Price: ${new_price:.2f}
- Rating: {product.rating or 'Not yet rated'}
- Reviews: {product.review_count or 0}
- Affiliate URL: {product.affiliate_url}

Generate a single-line HTML blob (NO line breaks) using this structure:
<div style='background:#f5f9f5;border:1px solid #e0e8e0;border-radius:10px;padding:24px;font-family:sans-serif;max-width:700px;'><div style='display:flex;align-items:flex-start;gap:16px;'><span style='background:#4a7c59;color:white;font-size:13px;font-weight:600;padding:4px 14px;border-radius:20px;white-space:nowrap;'>Our top pick</span><div><p style='margin:0 0 12px 0;font-size:16px;line-height:1.5;'><strong>{product.full_name}</strong> — [blurb about why it's the top pick, mention ${new_price:.2f}]</p><a href='{product.affiliate_url}' style='display:inline-block;border:1px solid #999;border-radius:6px;padding:8px 18px;text-decoration:none;color:#333;font-size:14px;'>Shop on Amazon &rarr;</a></div></div></div>

Voice: casual, encouraging, beginner-friendly. No invented specs. Use single quotes for HTML attributes.
Return ONLY the single-line HTML blob, no markdown or code blocks."""

    elif widget_number == 4 and all_tier_products:
        # Three Tiers card — needs all three products
        tier_data = []
        for tp in all_tier_products:
            tier_data.append(f"- {tp.role}: {tp.full_name}, ${(tp.price_usd if tp.price_usd else 0):.2f}, "
                           f"rating {tp.rating or 'N/A'}, URL: {tp.affiliate_url}")

        prompt = f"""Regenerate the 'Three Tiers: budget, mid-range, and premium' HTML module for {config.site_name}.

PRODUCTS:
{chr(10).join(tier_data)}

The product whose price changed: {product.full_name} from ${old_price:.2f} to ${new_price:.2f}

Generate a single-line HTML blob (NO line breaks) with three side-by-side cards using inline CSS.
The mid-range card should have a thicker orange border (2px solid #c17328) as 'Best value'.
Use the same structure as the original Three Tiers widget.

Voice: casual, encouraging, beginner-friendly. No invented specs. Use single quotes for HTML attributes.
Return ONLY the single-line HTML blob, no markdown or code blocks."""
    else:
        # Fallback for unexpected widget numbers
        prompt = f"""Regenerate HTML content for a product on {config.site_name}.

PRODUCT: {product.full_name}
- NEW Price: ${new_price:.2f}
- Rating: {product.rating or 'Not yet rated'}
- Affiliate URL: {product.affiliate_url}

Generate a single-line HTML blob (NO line breaks) with the updated price.
Voice: casual, beginner-friendly. Use single quotes for HTML attributes.
Return ONLY the HTML, no markdown."""

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=4096,
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
            html = "".join(lines)  # Join without newlines — single-line blob

        # Ensure single-line blob
        html = html.replace("\n", "").replace("\r", "")

        return html
    except anthropic.APIError as e:
        raise ContentGeneratorError(
            f"Failed to regenerate widget {widget_number} for {product.asin}: {e}"
        ) from e
