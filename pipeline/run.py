"""Pipeline runner — orchestrates modules in sequence.

Entry point: python pipeline/run.py --category <category_id> [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path so `pipeline.*` imports work
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline.models import CategoryConfig, RunLog, TrendsData, compute_weekly_id, runs_path
from pipeline.modules.config_loader import ConfigLoaderError, load as load_config
from pipeline.modules.trends_collector import TrendsCollectorError, collect as collect_trends
from pipeline.modules.signals_collector import SignalsCollectorError, collect as collect_signals
from pipeline.modules.ranker import RankerError, rank as rank_products
from pipeline.modules.geniuslink_client import GeniusLinkError, enrich as enrich_links
from pipeline.modules.content_generator import ContentGeneratorError, generate as generate_content
from pipeline.modules.airtable_client import AirtableClientError, write as write_airtable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def _compute_week_of() -> str:
    """Return the Monday of the current week as YYYY-MM-DD."""
    today = date.today()
    monday = today - __import__("datetime").timedelta(days=today.weekday())
    return monday.isoformat()


def _build_supplemental_keywords(
    trends: TrendsData, config: CategoryConfig,
) -> list[str]:
    """Build Amazon search keywords from trends data.

    For brand+model queries: "{brand} {model} {gender}'s"
    For brand-only queries: "{brand} {gender}'s {product_type}"
    Capped at trends_max_supplemental_searches.
    """
    keywords: list[str] = []
    seen: set[str] = set()

    all_queries = trends.rising_queries + trends.top_queries

    for tq in all_queries:
        if tq.query_type == "brand_model" and tq.normalized_brand and tq.normalized_model:
            kw = f"{tq.normalized_brand} {tq.normalized_model} {config.gender}'s"
        elif tq.query_type == "brand_only" and tq.normalized_brand:
            kw = f"{tq.normalized_brand} {config.gender}'s {config.product_type}"
        else:
            continue

        kw_lower = kw.lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            keywords.append(kw)

        if len(keywords) >= config.trends_max_supplemental_searches:
            break

    return keywords


def _save_run_log(run_log: RunLog, week_of: str) -> None:
    """Save run log to the runs directory."""
    path = runs_path(run_log.category_id, week_of, "run_log.json")
    path.write_text(run_log.model_dump_json(indent=2))


def main(category_id: str, force: bool = False) -> None:
    week_of = _compute_week_of()
    logger.info("Starting pipeline: category=%s, week_of=%s, force=%s", category_id, week_of, force)

    run_log = RunLog(
        category_id=category_id,
        week_of=week_of,
        run_started_at=datetime.now(timezone.utc),
    )

    # --- Step 1: Load config ---
    try:
        config = load_config(category_id)
    except ConfigLoaderError as e:
        logger.error("Config loader failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"config_loader: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    # --- Step 2: Collect Google Trends (graceful degradation) ---
    trends: TrendsData | None = None
    try:
        trends = collect_trends(config, week_of, force=force)
        run_log.trends_rising_count = len(trends.rising_queries)
        run_log.trends_top_count = len(trends.top_queries)
    except TrendsCollectorError as e:
        logger.warning("Trends failed: %s — BSR-only fallback", e)
        run_log.warnings.append(f"trends_collector: {e}")
        run_log.trends_failed = True

    # --- Step 3: Collect Amazon signals (with supplemental searches from trends) ---
    supplemental = _build_supplemental_keywords(trends, config) if trends else None
    if supplemental:
        run_log.trends_supplemental_searches = len(supplemental)
        logger.info("Built %d supplemental keywords from trends", len(supplemental))

    try:
        signals = collect_signals(config, week_of, supplemental_keywords=supplemental, force=force)
    except SignalsCollectorError as e:
        logger.error("Signals collector failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"signals_collector: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    run_log.products_found = signals.products_before_filter
    run_log.products_after_filter = signals.products_after_filter

    # --- Step 4: Rank products (trends-aware) ---
    try:
        ranked = rank_products(signals, config, week_of, trends=trends, force=force)
    except RankerError as e:
        logger.error("Ranker failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"ranker: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    run_log.products_ranked = ranked.product_count

    # --- Step 5: Enrich with GeniusLink affiliate URLs ---
    try:
        linked, gl_cached, gl_created, gl_failed = enrich_links(
            ranked, config, week_of, force=force,
        )
    except GeniusLinkError as e:
        logger.error("GeniusLink client failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"geniuslink_client: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    run_log.geniuslink_cached = gl_cached
    run_log.geniuslink_created = gl_created
    run_log.geniuslink_failed = gl_failed
    if gl_failed > 0:
        run_log.warnings.append(
            f"GeniusLink failed for {gl_failed} product(s) — using raw Amazon URLs"
        )

    # --- Step 6: Generate content via Claude ---
    try:
        roundup = generate_content(linked, config, week_of, force=force)
    except ContentGeneratorError as e:
        logger.error("Content generator failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"content_generator: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    # Set weekly_id on the roundup
    roundup.weekly_id = compute_weekly_id(week_of)

    # --- Step 7: Write to Airtable ---
    try:
        write_airtable(roundup, config, linked_products=linked)
    except AirtableClientError as e:
        logger.error("Airtable client failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"airtable_client: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    run_log.airtable_roundup_written = True
    run_log.airtable_rankings_written = len(roundup.products)
    run_log.airtable_catalog_upserted = len(roundup.products)

    # --- Pipeline complete ---
    run_log.status = "success"
    run_log.run_completed_at = datetime.now(timezone.utc)
    _save_run_log(run_log, week_of)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Pipeline complete: {config.display_name}")
    print(f"Week of: {week_of}")
    if trends:
        print(f"Trends: {len(trends.rising_queries)} rising, {len(trends.top_queries)} top queries")
        print(f"Supplemental searches: {len(supplemental) if supplemental else 0}")
    else:
        print("Trends: unavailable (BSR-only fallback)")
    print(f"Products found: {signals.products_before_filter}")
    print(f"After filtering: {signals.products_after_filter}")
    print(f"Ranked: {ranked.product_count}")
    print(f"GeniusLink: {gl_cached} cached, {gl_created} created, {gl_failed} failed")
    print(f"Airtable: 1 roundup, {len(roundup.products)} rankings, {len(roundup.products)} catalog")
    print(f"{'='*60}")
    tier_labels = {
        1: "Rising trend (model match)",
        2: "Top trend (model match)",
        3: "Rising trend (brand match)",
        4: "Top trend (brand match)",
        5: "Heat Score fallback",
    }
    for p in ranked.products:
        reason = tier_labels.get(p.selection_tier, f"Tier {p.selection_tier}")
        print(f"  #{p.rank}  {p.full_name}")
        print(f"       {reason}  |  Heat: {p.heat_score}  |  ${p.price_usd}  |  {p.rank_change}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the trending products pipeline")
    parser.add_argument("--category", required=True, help="Category ID (e.g. womens-running-shoes)")
    parser.add_argument("--force", action="store_true", help="Force re-run, ignoring cached artifacts")
    args = parser.parse_args()

    main(args.category, force=args.force)
