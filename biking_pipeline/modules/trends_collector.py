"""Trends collector — fetches Google Trends rising + top queries for biking articles.

Reuses the same pytrends + Claude classification pattern as the weekly trending
pipeline but adapted for biking-specific config (BikingArticleConfig,
biking brand lists, per-article trends_keyword).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import anthropic

from biking_pipeline.models import (
    BikingArticleConfig,
    BikingTrendsData,
    BikingTrendsQuery,
    biking_runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"


class TrendsCollectorError(Exception):
    pass


def _fetch_trends(config: BikingArticleConfig) -> tuple[dict, str]:
    """Fetch related queries from Google Trends via pytrends.

    Returns raw pytrends related_queries dict and the keyword used.
    Rate limiting: 2s sleep before call, retry once on 429 with 60s backoff.
    """
    from pytrends.request import TrendReq

    keyword = config.trends_keyword or config.keywords

    pytrends = TrendReq(hl="en-US", tz=480)

    time.sleep(2)  # Rate limit: pause before call

    try:
        pytrends.build_payload(
            [keyword],
            timeframe="today 3-m",  # Last 3 months — captures seasonal biking interest
            geo="US",
        )
        related = pytrends.related_queries()
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "Too Many Requests" in error_str:
            logger.warning("Google Trends 429 — retrying in 60s")
            time.sleep(60)
            try:
                pytrends.build_payload(
                    [keyword],
                    timeframe="today 3-m",
                    geo="US",
                )
                related = pytrends.related_queries()
            except Exception as retry_e:
                raise TrendsCollectorError(
                    f"Google Trends retry failed: {retry_e}"
                ) from retry_e
        else:
            raise TrendsCollectorError(
                f"Google Trends fetch failed: {e}"
            ) from e

    return related, keyword


def _parse_raw_queries(related: dict, keyword: str) -> tuple[list[dict], list[dict]]:
    """Extract rising and top queries from pytrends response."""
    rising_raw = []
    top_raw = []

    kw_data = related.get(keyword, {})

    rising_df = kw_data.get("rising")
    if rising_df is not None and not rising_df.empty:
        for _, row in rising_df.iterrows():
            rising_raw.append({
                "query": row["query"],
                "value": int(row["value"]) if str(row["value"]).isdigit() else 0,
                "increase_percent": str(row["value"]) if not str(row["value"]).isdigit() else f"{row['value']}%",
            })

    top_df = kw_data.get("top")
    if top_df is not None and not top_df.empty:
        for _, row in top_df.iterrows():
            top_raw.append({
                "query": row["query"],
                "value": int(row["value"]),
            })

    return rising_raw, top_raw


def _classify_queries(
    rising_raw: list[dict],
    top_raw: list[dict],
    config: BikingArticleConfig,
) -> tuple[list[BikingTrendsQuery], list[BikingTrendsQuery]]:
    """Use Claude to classify each query as brand_model / brand_only / generic.

    Sends all queries in one call for efficiency.
    Uses the article's top_brands list for brand recognition.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise TrendsCollectorError("ANTHROPIC_API_KEY must be set for trends classification")

    all_queries = []
    for q in rising_raw:
        all_queries.append({"query": q["query"], "source": "rising"})
    for q in top_raw:
        all_queries.append({"query": q["query"], "source": "top"})

    if not all_queries:
        return [], []

    # Use biking-specific brands from config, supplemented with common biking brands
    known_brands = list(set(config.top_brands + [
        "Rawlings", "Wilson", "Mizuno", "Nokona", "Easton",
        "Louisville Slugger", "DeMarini", "Dudley", "Worth",
        "Champro", "Diamond", "Franklin", "Miken", "Anderson",
        "Marucci", "New Balance", "Under Armour", "Nike", "Adidas",
        "Ringor", "Boombah",
    ]))

    prompt = f"""Classify each Google Trends query for the "{config.display_name}" category (biking gear).

Known biking brands: {', '.join(sorted(known_brands))}

For each query, determine:
- query_type: "brand_model" if it contains a specific brand AND model/product name (e.g., "rawlings liberty advanced", "demarini prism", "louisville slugger lxt")
- query_type: "brand_only" if it mentions a brand but no specific model (e.g., "rawlings biking gloves", "easton fastpitch bats")
- query_type: "generic" if no brand is mentioned (e.g., "best biking gloves", "fastpitch bat reviews")

For brand_model and brand_only queries, also provide:
- normalized_brand: Title-case brand name (e.g., "Rawlings", "DeMarini", "Louisville Slugger")
- normalized_model: Title-case model name without color/size/gender (e.g., "Liberty Advanced", "Prism", "LXT") — null for brand_only

Return a JSON array:
[{{"query": "...", "source": "...", "query_type": "...", "normalized_brand": "..." or null, "normalized_model": "..." or null}}]

Queries:
{json.dumps(all_queries, indent=2)}

Return ONLY the JSON array, no other text."""

    client = anthropic.Anthropic(api_key=api_key)

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
            lines = [line for line in lines if not line.strip().startswith("```")]
            response_text = "\n".join(lines)

        classified = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise TrendsCollectorError(
            f"Claude returned invalid JSON for trends classification: {e}"
        ) from e
    except anthropic.APIError as e:
        raise TrendsCollectorError(
            f"Anthropic API error during trends classification: {e}"
        ) from e

    # Build lookup for raw values
    rising_by_query = {q["query"].lower(): q for q in rising_raw}
    top_by_query = {q["query"].lower(): q for q in top_raw}

    rising_results = []
    top_results = []

    for item in classified:
        query_lower = item["query"].lower()
        source = item["source"]

        if source == "rising":
            raw = rising_by_query.get(query_lower, {})
            search_interest = raw.get("value", 0)
            increase_percent = raw.get("increase_percent")
        else:
            raw = top_by_query.get(query_lower, {})
            search_interest = raw.get("value", 0)
            increase_percent = None

        tq = BikingTrendsQuery(
            query=item["query"],
            search_interest=search_interest,
            increase_percent=increase_percent,
            source=source,
            query_type=item.get("query_type", "generic"),
            normalized_brand=item.get("normalized_brand"),
            normalized_model=item.get("normalized_model"),
        )

        if source == "rising":
            rising_results.append(tq)
        else:
            top_results.append(tq)

    return rising_results, top_results


def collect(
    config: BikingArticleConfig,
    run_date: str,
    force: bool = False,
) -> BikingTrendsData:
    """Collect Google Trends data for a biking article.

    Idempotent: skips work if trends.json exists (unless force=True).
    Raises TrendsCollectorError on failure — caller should handle gracefully
    (trends are a scoring signal, not a hard dependency).
    """
    artifact_path = biking_runs_path(config.article_id, run_date, "trends.json")

    if artifact_path.exists() and not force:
        logger.info("Resuming from cached trends.json")
        return BikingTrendsData.model_validate_json(artifact_path.read_text())

    # Fetch from Google Trends
    related, keyword = _fetch_trends(config)

    # Parse raw queries
    rising_raw, top_raw = _parse_raw_queries(related, keyword)
    logger.info(
        "Google Trends: %d rising, %d top queries for '%s'",
        len(rising_raw), len(top_raw), keyword,
    )

    if not rising_raw and not top_raw:
        raise TrendsCollectorError(
            f"Google Trends returned no queries for '{keyword}'"
        )

    # Classify via Claude
    rising, top = _classify_queries(rising_raw, top_raw, config)

    brand_model_count = sum(
        1 for q in rising + top if q.query_type == "brand_model"
    )
    brand_only_count = sum(
        1 for q in rising + top if q.query_type == "brand_only"
    )
    logger.info(
        "Classified: %d brand+model, %d brand-only, %d generic",
        brand_model_count,
        brand_only_count,
        len(rising) + len(top) - brand_model_count - brand_only_count,
    )

    trends = BikingTrendsData(
        article_id=config.article_id,
        collected_at=datetime.now(timezone.utc),
        trends_keyword=keyword,
        rising_queries=rising,
        top_queries=top,
    )

    # Save artifact
    artifact_path.write_text(trends.model_dump_json(indent=2))
    logger.info("Saved trends.json for %s", config.article_id)

    return trends
