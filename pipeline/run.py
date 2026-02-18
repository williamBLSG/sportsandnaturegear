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

from pipeline.models import RunLog, runs_path
from pipeline.modules.config_loader import ConfigLoaderError, load as load_config
from pipeline.modules.signals_collector import SignalsCollectorError, collect as collect_signals
from pipeline.modules.ranker import RankerError, rank as rank_products

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

    # --- Step 2: Collect signals ---
    try:
        signals = collect_signals(config, week_of, force=force)
    except SignalsCollectorError as e:
        logger.error("Signals collector failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"signals_collector: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    run_log.products_found = signals.products_before_filter
    run_log.products_after_filter = signals.products_after_filter

    # --- Step 3: Rank products ---
    try:
        ranked = rank_products(signals, config, week_of, force=force)
    except RankerError as e:
        logger.error("Ranker failed: %s", e)
        run_log.status = "failed"
        run_log.error = f"ranker: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        _save_run_log(run_log, week_of)
        sys.exit(1)

    run_log.products_ranked = ranked.product_count

    # --- Pipeline complete (Phase 1 ends here) ---
    run_log.status = "success"
    run_log.run_completed_at = datetime.now(timezone.utc)
    _save_run_log(run_log, week_of)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Pipeline complete: {config.display_name}")
    print(f"Week of: {week_of}")
    print(f"Products found: {signals.products_before_filter}")
    print(f"After filtering: {signals.products_after_filter}")
    print(f"Ranked: {ranked.product_count}")
    print(f"{'='*60}")
    for p in ranked.products:
        print(f"  #{p.rank}  {p.full_name}")
        print(f"       Heat Score: {p.heat_score}  |  BSR: {p.bsr}  |  Rating: {p.rating}  |  Reviews: {p.review_count}  |  ${p.price_usd}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the trending products pipeline")
    parser.add_argument("--category", required=True, help="Category ID (e.g. womens-running-shoes)")
    parser.add_argument("--force", action="store_true", help="Force re-run, ignoring cached artifacts")
    args = parser.parse_args()

    main(args.category, force=args.force)
