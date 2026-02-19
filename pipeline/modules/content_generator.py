"""Content generator — calls Anthropic API to generate HTML content for roundups."""

from __future__ import annotations

import json
import logging
import os

import anthropic

from pipeline.models import (
    CategoryConfig,
    LinkedProduct,
    RankedProduct,
    ResearchOutput,
    StateActivityConfig,
    StateActivityProduct,
    StateArticle,
    WeeklyRoundup,
    runs_path,
    slugify,
    state_runs_path,
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
  "hub_summary": "...",
  "faqs": [
    {{"question": "...", "answer": "..."}}
  ],
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

hub_summary: 1-2 sentence evergreen summary of this week's trends, suitable for a category hub page. Should read well weeks later — avoid "this week" language. Example: "Brooks and ASICS dominate the trending women's running shoes, with comfort-focused models leading buyer ratings."

faqs: A JSON array of 3-5 objects, each with "question" and "answer" string fields. Address Amy's pain points: how rankings work, fit/sizing tips for {config.product_type}, beginner suitability, value vs. price. Answers should be plain text (no HTML). Do NOT make health/biomechanical claims. Do NOT invent specs.

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


# ---------------------------------------------------------------------------
# State activity article generation
# ---------------------------------------------------------------------------

_STATE_ARTICLE_SYSTEM = """\
You are a warm, knowledgeable woman writing state activity guides for \
SportsAndNatureGear.com. You write for other women who love the outdoors \
or are just starting to. You've been to these places, done these activities, \
and you write the way you'd describe a weekend trip to a friend over coffee. \
You're not a gear expert rattling off specs. You're the friend who says \
"trust me, you need to see this lake" and actually makes her want to go.

Your reader is Active Amy, a woman aged 25-50, suburban or urban, who loves \
nature and sports but doesn't always know where to start. She's busy. She's \
practical. She wants real information, not fluff. She's often planning \
something for herself or her daughter, and she responds to warmth, specificity, \
and a voice that feels like it comes from a real person. About 30% of your \
readers are women without children pursuing personal growth. Write for both \
groups without assuming one or the other.

VOICE (follow without exception):

Warm but never gushing. Trust the detail. Don't inflate it. Say "the water \
is usually calm enough to look like glass" and let that image do the work. \
Never say "absolutely stunning" or "you'll be blown away."

Practical and specific, always. Every location gets real details: acreage, \
trail mileage, fees, permits, restrooms. This isn't vague inspiration. A \
reader should be able to plan a real trip from it.

Inviting without being salesy. Use "you" and "folks" naturally. Never \
"individuals," "one," "users," or "consumers." The affiliate and gear angles \
come through in separate sections, not in location descriptions.

Confident, not qualifying. No "some say" or "it might be worth checking out." \
Own the recommendation. If something is notable, say why specifically.

Conversational sentence rhythm. Short sentences. Then a longer one that \
breathes a little, gives some detail, earns its length. Then short again. \
Paragraphs are 3-5 sentences. They move. Nothing drags.

Use contractions naturally: it's, you'll, don't, that's.

Include at least one historical or cultural detail per article.

Gear section: shift to practical advisor mode. Lead with what matters most \
for this activity in this specific state (climate, terrain, season).

VOCABULARY TO USE NATURALLY:
For water: calm, clear, glassy, pristine, crystal-clear, sun-dappled
For experience: serene, soothing, peaceful, restorative, leisurely, unhurried
For activities: cast a line, commune with nature, wander, traverse, seize the chance
For reader: folks, you, visitors, anglers, land lovers, those who...
For quantities: be specific. "7-mile trail" not "miles of trails." "365 acres" \
not "a large lake."

HARD RULES (violations cause rejection):
- NEVER use em dashes (unicode character U+2014). Use commas, periods, or rewrite.
- NEVER use: nestled, vibrant, tapestry, boasts, showcasing, seamlessly, \
breathtaking, amazing, incredible, transformative, cutting-edge, groundbreaking, \
game-changer, leverage. "Stunning" max once per article. "Perfect" max once.
- NEVER use: Furthermore, Moreover, Additionally, In conclusion, It's worth \
noting that, Let's dive in, Here's the deal, Secret sauce, You'll love this.
- NEVER use: "Whether you're a beginner or an expert," "No matter your skill \
level," "There's something for everyone," "offers something for everyone."
- NEVER use: great place, wonderful spot, lovely destination, amazing experience, \
must-see.
- NEVER invent facts. If the research data does not include a specific detail \
(acreage, mileage, fee), omit it. Do not estimate or fabricate.
- NEVER use placeholder text.
- No sentence longer than 35 words.
- At least one sentence under 10 words per paragraph.
- No paragraph longer than 5 sentences.
- No two paragraphs start with the same word or phrase.
- Write in prose paragraphs. No bullet points inside article sections.
- meta_title must be 65 characters or fewer.
- meta_description must be 165 characters or fewer and must include a CTA."""


def _build_state_article_prompt(
    state: str,
    research: ResearchOutput,
    config: StateActivityConfig,
) -> str:
    """Build the user prompt for state activity article generation."""
    state_slug = slugify(state)
    slug = f"{config.activity_id}-in-{state_slug}"
    research_json = research.model_dump_json(indent=2)

    return f"""\
Write a state activity guide about {config.display_name} in {state}.
Target audience: women aged 25-50, beginners to casual participants, often \
planning trips with daughters or friends.

RESEARCH DATA (use only these facts, do not invent additional details):
{research_json}

H2 SECTION POOL (use only sections supported by the research, skip the rest):
{json.dumps(config.h2_section_pool, indent=2)}

OUTPUT FORMAT: return a single JSON object with these exact keys.
Omit any h2/body pair where the research does not support a full, specific section.
Do not include empty strings for omitted sections. Do not include placeholders.

"slug": "{slug}"

"activity": "{config.activity_id}"

"state_filter": "{state}"

"parent_page_description": One sentence (max 120 characters) for the state hub \
page. Specific and inviting. No generic phrases.

"parent_page_cta": 3-5 word CTA for the hub page button. e.g. "Explore \
{config.display_name} in {state}"

"meta_title": Max 65 characters. Include activity and state. Beginner/family \
angle where natural.

"meta_description": Max 165 characters. Include activity, state, and a clear \
CTA. e.g. "Discover {state}'s best {config.activity_id} spots for women and \
families. Find top sites, gear tips, and when to go. Start planning your trip."

"h1": The article headline. Specific and inviting. Not a restatement of the \
meta title.

"intro": 2-3 short paragraphs. Open with a human truth or feeling, why people \
love this activity, what memory or emotion it connects to. Do not open with a \
fact or definition. Second paragraph orients the reader to what the article \
covers.

"h2_1" through "h2_8": Section headings. Use only the sections supported by \
the research. Each heading should be specific to {state} and {config.activity_id}, \
not generic. For example, not "Best Spots" but "Where {state} Campers Actually Go."

"h2_1_body" through "h2_8_body": Section body content. Each section: 2-4 \
paragraphs, each 3-5 sentences. Lead with what makes it worth reading. Give \
specific, verifiable details. End with how it feels to be there.

"product1": "1"
"product2": "2"
"status": "Draft"

Return only valid JSON. No markdown fences. No commentary outside the JSON."""


def generate_state_article(
    state: str,
    research: ResearchOutput,
    config: StateActivityConfig,
    force: bool = False,
) -> StateArticle:
    """Generate a state activity article using Claude.

    Idempotent: skips work if article.json exists (unless force=True).
    Validates LLM output via Pydantic StateArticle model.
    Retries once on validation failure; raises ContentGeneratorError on second failure.
    """
    artifact_path = state_runs_path(state, config.activity_id, "article.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached article.json: %s", artifact_path)
        return StateArticle.model_validate_json(artifact_path.read_text())

    prompt = _build_state_article_prompt(state, research, config)

    # First attempt
    logger.info(
        "Generating state article via Claude (%s): %s in %s",
        MODEL_ID, config.activity_id, state,
    )
    response_text = _call_anthropic_with_system(
        system=_STATE_ARTICLE_SYSTEM,
        user=prompt,
    )

    try:
        data = _parse_response(response_text)
        article = StateArticle(**data)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(
            "First state article attempt invalid: %s. Retrying with correction.", e,
        )

        correction = (
            f"Your previous response was invalid. Error: {e}\n\n"
            "IMPORTANT CORRECTIONS:\n"
            "- If the error mentions em dashes, remove ALL em dashes (the unicode "
            "character U+2014). Use commas or periods instead.\n"
            "- If the error mentions character limits, shorten the field.\n"
            "- If the error mentions slug format, use exactly: "
            f"{config.activity_id}-in-{slugify(state)}\n"
            "- If the error mentions h2 body without heading, either add the "
            "heading or remove the body.\n\n"
            "Return ONLY valid JSON. No markdown fences."
        )
        response_text = _call_anthropic_with_system(
            system=_STATE_ARTICLE_SYSTEM,
            user=prompt + "\n\n" + correction,
        )
        try:
            data = _parse_response(response_text)
            article = StateArticle(**data)
        except (json.JSONDecodeError, Exception) as e2:
            raise ContentGeneratorError(
                f"State article LLM output invalid after retry: {e2}"
            ) from e2

    # Save artifact
    artifact_path.write_text(article.model_dump_json(indent=2))
    logger.info(
        "State article generated: %s (%d h2 sections used)",
        article.slug,
        sum(1 for i in range(1, 9) if getattr(article, f"h2_{i}")),
    )

    return article


def _call_anthropic_with_system(system: str, user: str) -> str:
    """Call the Anthropic API with a system prompt and return the response text."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ContentGeneratorError("ANTHROPIC_API_KEY must be set")

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        raise ContentGeneratorError(f"Anthropic API error (with system): {e}") from e


# ---------------------------------------------------------------------------
# State activity product copy generation
# ---------------------------------------------------------------------------

_PRODUCT_COPY_SYSTEM = """\
You are a product copywriter for SportsAndNatureGear.com. You write short, \
honest, beginner-friendly product descriptions for women shopping for outdoor \
gear. You never invent specifications or make claims not supported by the \
provided data. You write like a helpful friend, not a salesperson.

HARD RULES:
- NEVER use em dashes (unicode character U+2014). Use commas or periods instead.
- title must be 65 characters or fewer.
- description must be 165 characters or fewer.
- Do not invent specs, weights, materials, or features not in the input data.
- Do not make health or biomechanical claims."""


def _build_product_copy_prompt(
    state: str,
    activity: str,
    products: list[RankedProduct],
) -> str:
    """Build the user prompt for state activity product copy."""
    product_data = []
    for p in products:
        product_data.append({
            "rank": p.rank,
            "asin": p.asin,
            "title": p.title,
            "brand": p.brand,
            "model": p.model,
            "full_name": p.full_name,
            "price_usd": p.price_usd,
            "rating": p.rating,
            "review_count": p.review_count,
            "bsr": p.bsr,
            "image_url": p.image_url,
        })

    return f"""\
Write product copy for these {activity} products. Audience: women aged 25-50, \
beginners to casual participants. State context: {state}.

PRODUCT DATA:
{json.dumps(product_data, indent=2)}

For each product, generate a JSON object with these keys:

"title": Product name, max 65 characters. Clear and descriptive. No hype words.

"description": Max 165 characters. Lead with the primary benefit for a beginner. \
End with a specific feature (weight, material, size) if available in the data. \
No exclamation points.

"link_text": 3-4 word CTA. Options: "See Current Price", "Shop on Amazon", \
"Check Today's Price", "View on Amazon."

"image_alt_text": Descriptive alt text. Format: \
"{{Brand}} {{product type}} {{relevant descriptor}} for {{activity}}" \
e.g. "Coleman 2-person tent set up at lakeside campsite"

Return a JSON array of {len(products)} objects, one per product, in the same \
order as input. Each object must have exactly these 4 keys: title, description, \
link_text, image_alt_text.

Return only valid JSON. No markdown fences. No commentary."""


def generate_state_product_copy(
    state: str,
    activity: str,
    products: list[RankedProduct],
    config: StateActivityConfig,
    force: bool = False,
) -> list[StateActivityProduct]:
    """Generate product copy for state activity products using Claude.

    Idempotent: skips work if products_copy.json exists (unless force=True).
    Returns a list of StateActivityProduct with copy fields populated.
    Affiliate links are set from ranked product data initially;
    the GeniusLink enrichment step updates affiliate_link later.
    """
    artifact_path = state_runs_path(state, config.activity_id, "products_copy.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached products_copy.json: %s", artifact_path)
        data = json.loads(artifact_path.read_text())
        return [StateActivityProduct(**p) for p in data]

    prompt = _build_product_copy_prompt(state, activity, products)

    logger.info(
        "Generating product copy via Claude (%s): %d products for %s in %s",
        MODEL_ID, len(products), activity, state,
    )
    response_text = _call_anthropic_with_system(
        system=_PRODUCT_COPY_SYSTEM,
        user=prompt,
    )

    try:
        copy_list = _parse_response(response_text)
        if not isinstance(copy_list, list) or len(copy_list) != len(products):
            raise ValueError(
                f"Expected {len(products)} product objects, got "
                f"{len(copy_list) if isinstance(copy_list, list) else 'non-list'}"
            )
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            "First product copy attempt invalid: %s. Retrying with correction.", e,
        )
        correction = (
            f"Your previous response was invalid. Error: {e}\n\n"
            f"Return ONLY a JSON array of exactly {len(products)} objects. "
            "No markdown fences. No text outside the array."
        )
        response_text = _call_anthropic_with_system(
            system=_PRODUCT_COPY_SYSTEM,
            user=prompt + "\n\n" + correction,
        )
        try:
            copy_list = _parse_response(response_text)
            if not isinstance(copy_list, list) or len(copy_list) != len(products):
                raise ValueError(
                    f"Expected {len(products)} product objects, got "
                    f"{len(copy_list) if isinstance(copy_list, list) else 'non-list'}"
                )
        except (json.JSONDecodeError, ValueError) as e2:
            raise ContentGeneratorError(
                f"Product copy LLM output invalid after retry: {e2}"
            ) from e2

    # Build StateActivityProduct records by merging copy with ranked data
    state_slug = slugify(state)
    result: list[StateActivityProduct] = []

    for i, (product, copy) in enumerate(zip(products, copy_list), start=1):
        slug = f"{activity}-in-{state_slug}-{i}"
        product_group = "1" if i <= 5 else "2"

        record = StateActivityProduct(
            slug=slug,
            state=state,
            activity=activity,
            image_url=product.image_url or "",
            image_alt_text=copy.get("image_alt_text", ""),
            title=copy.get("title", product.full_name),
            description=copy.get("description", ""),
            link_text=copy.get("link_text", "See Current Price"),
            affiliate_link=product.detail_page_url or "",
            asin=product.asin,
            bsr=product.bsr,
            product_group=product_group,
            status="Draft",
        )
        result.append(record)

    # Save artifact
    artifact_path.write_text(
        json.dumps([r.model_dump() for r in result], indent=2)
    )
    logger.info(
        "Product copy generated: %d products for %s in %s",
        len(result), activity, state,
    )

    return result
