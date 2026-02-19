"""State activity pipeline runner — orchestrates daily state processing.

Entry point: python pipeline/state_activity_run.py [--state STATE] [--activity ACTIVITY] [--force]

Processes one state per day across all configured activities. Each activity
runs sequentially: research -> article -> airtable article -> products ->
rank -> product copy -> geniuslink -> airtable products.

A failure in one activity does not abort the remaining activities.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path so `pipeline.*` imports work
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline.models import (
    StateActivityConfig,
    StateActivityRunLog,
    state_activity_as_category_config,
    state_runs_path,
)
from pipeline.modules.config_loader import (
    ConfigLoaderError,
    load_state_activity,
    load_state_queue,
)
from pipeline.modules.state_queue_manager import (
    StateQueueManagerError,
    get_todays_state,
)
from pipeline.modules.state_researcher import (
    StateResearcherError,
    research,
)
from pipeline.modules.content_generator import (
    ContentGeneratorError,
    generate_state_article,
    generate_state_product_copy,
)
from pipeline.modules.signals_collector import (
    SignalsCollectorError,
    collect as collect_signals,
)
from pipeline.modules.ranker import (
    RankerError,
    rank as rank_products,
)
from pipeline.modules.geniuslink_client import (
    GeniusLinkError,
    enrich_state_products,
)
from pipeline.modules.airtable_client import (
    AirtableClientError,
    write_state_activity,
    write_state_activity_products,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("state_activity_pipeline")

# Activities to process (filenames in config/state-activities/ without .yaml)
ACTIVITY_IDS = ["camping", "hiking", "cycling", "kayaking"]


def _discover_activities(override: str | None = None) -> list[str]:
    """Return list of activity IDs to process.

    If override is set, returns only that activity (validated against ACTIVITY_IDS).
    Otherwise returns all configured activities that have YAML files.
    """
    if override:
        return [override]

    config_dir = Path(__file__).resolve().parent.parent / "config" / "state-activities"
    found = []
    for aid in ACTIVITY_IDS:
        if (config_dir / f"{aid}.yaml").exists():
            found.append(aid)
    return found


def _save_run_log(run_log: StateActivityRunLog, state: str) -> None:
    """Save run log to the runs directory."""
    from pipeline.models import slugify
    state_slug = slugify(state)
    base = Path(__file__).resolve().parent.parent / "runs" / "state-activities" / state_slug
    base.mkdir(parents=True, exist_ok=True)
    path = base / "run_log.json"
    path.write_text(run_log.model_dump_json(indent=2))


def _run_activity(
    state: str,
    activity_id: str,
    force: bool = False,
) -> dict:
    """Run the full pipeline for one state+activity. Returns a status dict."""
    result = {
        "status": "success",
        "research_facts": 0,
        "article_slug": "",
        "article_sections": 0,
        "products_found": 0,
        "products_ranked": 0,
        "products_written": 0,
        "geniuslink_cached": 0,
        "geniuslink_created": 0,
        "geniuslink_failed": 0,
        "warnings": [],
        "error": None,
    }

    # --- Load activity config ---
    try:
        config = load_state_activity(activity_id)
    except ConfigLoaderError as e:
        logger.error("[%s/%s] Config load failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"config_loader: {e}"
        return result

    # --- Step 1: Research ---
    try:
        research_output = research(state, config, force=force)
        result["research_facts"] = len(research_output.facts)
        logger.info(
            "[%s/%s] Research: %d facts from %s",
            state, activity_id, len(research_output.facts),
            research_output.sources_consulted,
        )
    except StateResearcherError as e:
        logger.error("[%s/%s] Research failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"state_researcher: {e}"
        return result

    # --- Step 2: Generate article ---
    try:
        article = generate_state_article(state, research_output, config, force=force)
        result["article_slug"] = article.slug
        result["article_sections"] = sum(
            1 for i in range(1, 9) if getattr(article, f"h2_{i}")
        )
        logger.info(
            "[%s/%s] Article: %s (%d sections)",
            state, activity_id, article.slug, result["article_sections"],
        )
    except ContentGeneratorError as e:
        logger.error("[%s/%s] Article generation failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"content_generator (article): {e}"
        return result

    # --- Step 3: Write article to Airtable ---
    try:
        write_state_activity(article, config)
        logger.info("[%s/%s] Article written to Airtable", state, activity_id)
    except AirtableClientError as e:
        logger.error("[%s/%s] Article Airtable write failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"airtable_client (article): {e}"
        return result

    # --- Step 4: Collect Amazon product signals ---
    cat_config = state_activity_as_category_config(state, config)
    today = date.today().isoformat()

    try:
        signals = collect_signals(cat_config, today, force=force)
        result["products_found"] = signals.products_after_filter
        logger.info(
            "[%s/%s] Products: %d found, %d after filter",
            state, activity_id, signals.products_before_filter,
            signals.products_after_filter,
        )
    except SignalsCollectorError as e:
        logger.error("[%s/%s] Signals collection failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"signals_collector: {e}"
        return result

    # --- Step 5: Rank products (top 10) ---
    try:
        ranked = rank_products(signals, cat_config, today, top_n=10, force=force)
        result["products_ranked"] = ranked.product_count
        logger.info(
            "[%s/%s] Ranked: %d products",
            state, activity_id, ranked.product_count,
        )
    except RankerError as e:
        logger.error("[%s/%s] Ranking failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"ranker: {e}"
        return result

    # --- Step 6: Generate product copy ---
    try:
        products = generate_state_product_copy(
            state, activity_id, ranked.products, config, force=force,
        )
        logger.info(
            "[%s/%s] Product copy: %d products",
            state, activity_id, len(products),
        )
    except ContentGeneratorError as e:
        logger.error("[%s/%s] Product copy failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"content_generator (products): {e}"
        return result

    # --- Step 7: Enrich with GeniusLink ---
    try:
        products, gl_cached, gl_created, gl_failed = enrich_state_products(
            products, state, config, force=force,
        )
        result["geniuslink_cached"] = gl_cached
        result["geniuslink_created"] = gl_created
        result["geniuslink_failed"] = gl_failed
        if gl_failed > 0:
            result["warnings"].append(
                f"GeniusLink failed for {gl_failed} product(s)"
            )
        logger.info(
            "[%s/%s] GeniusLink: %d cached, %d created, %d failed",
            state, activity_id, gl_cached, gl_created, gl_failed,
        )
    except GeniusLinkError as e:
        logger.error("[%s/%s] GeniusLink failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"geniuslink_client: {e}"
        return result

    # --- Step 8: Write products to Airtable ---
    try:
        write_state_activity_products(products, config)
        result["products_written"] = len(products)
        logger.info(
            "[%s/%s] Products written to Airtable: %d",
            state, activity_id, len(products),
        )
    except AirtableClientError as e:
        logger.error("[%s/%s] Products Airtable write failed: %s", state, activity_id, e)
        result["status"] = "failed"
        result["error"] = f"airtable_client (products): {e}"
        return result

    return result


def main(
    force_state: str | None = None,
    force_activity: str | None = None,
    force: bool = False,
) -> None:
    """Run the state activity pipeline for today's state."""

    # --- Determine today's state ---
    try:
        start_date, states = load_state_queue()
    except ConfigLoaderError as e:
        logger.error("Failed to load state queue: %s", e)
        sys.exit(1)

    # Check env var overrides (from GitHub Actions workflow_dispatch)
    state_override = force_state or os.environ.get("STATE_OVERRIDE") or None
    activity_override = force_activity or os.environ.get("ACTIVITY_OVERRIDE") or None

    try:
        state = get_todays_state(states, start_date, force_state=state_override)
    except StateQueueManagerError as e:
        logger.error("Queue manager failed: %s", e)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("State Activity Pipeline: %s", state)
    logger.info("=" * 60)

    # --- Discover activities ---
    activity_ids = _discover_activities(override=activity_override)
    if not activity_ids:
        logger.error("No activity configs found")
        sys.exit(1)

    logger.info("Activities to process: %s", activity_ids)

    # --- Initialize run log ---
    run_log = StateActivityRunLog(
        state=state,
        run_date=date.today().isoformat(),
        run_started_at=datetime.now(timezone.utc),
    )

    # --- Process each activity ---
    any_failed = False

    for activity_id in activity_ids:
        logger.info("-" * 40)
        logger.info("Starting: %s in %s", activity_id, state)
        logger.info("-" * 40)

        result = _run_activity(state, activity_id, force=force)
        run_log.activities[activity_id] = result

        if result["status"] == "failed":
            any_failed = True
            logger.error(
                "[%s/%s] FAILED: %s", state, activity_id, result["error"],
            )
        else:
            logger.info(
                "[%s/%s] SUCCESS: %d facts, %d sections, %d products",
                state, activity_id,
                result["research_facts"],
                result["article_sections"],
                result["products_written"],
            )

        if result.get("warnings"):
            run_log.warnings.extend(
                f"[{activity_id}] {w}" for w in result["warnings"]
            )

    # --- Finalize ---
    run_log.status = "failed" if any_failed else "success"
    run_log.run_completed_at = datetime.now(timezone.utc)
    _save_run_log(run_log, state)

    # --- Print summary ---
    print(f"\n{'=' * 60}")
    print(f"State Activity Pipeline: {state}")
    print(f"Date: {run_log.run_date}")
    print(f"Status: {run_log.status.upper()}")
    print(f"{'=' * 60}")
    for aid, res in run_log.activities.items():
        status_icon = "OK" if res["status"] == "success" else "FAIL"
        print(f"  [{status_icon}] {aid}")
        if res["status"] == "success":
            print(f"       Facts: {res['research_facts']}, "
                  f"Sections: {res['article_sections']}, "
                  f"Products: {res['products_written']}")
        else:
            print(f"       Error: {res['error']}")
    if run_log.warnings:
        print(f"\nWarnings:")
        for w in run_log.warnings:
            print(f"  - {w}")
    print(f"{'=' * 60}\n")

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the state activity content pipeline",
    )
    parser.add_argument(
        "--state", default=None,
        help="Override state (e.g. Alabama). Default: auto from queue.",
    )
    parser.add_argument(
        "--activity", default=None,
        help="Override single activity (e.g. camping). Default: all.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-run, ignoring cached artifacts.",
    )
    args = parser.parse_args()

    main(
        force_state=args.state,
        force_activity=args.activity,
        force=args.force,
    )
