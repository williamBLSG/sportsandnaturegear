"""Hiking pipeline orchestrator.

Three run types:
  1. daily_build   — Full article build (signals → trends → rank → link → generate → write)
  2. price_check   — Sunday lightweight check: re-query prices, update if changed >10%
  3. manual_refresh — Full product discovery + editorial refresh (same as daily_build + force)

Usage:
  python -m hiking_pipeline.run --article hiking-boots --type daily_build
  python -m hiking_pipeline.run --article hiking-boots --type price_check
  python -m hiking_pipeline.run --article hiking-boots --type manual_refresh
  python -m hiking_pipeline.run --type daily_build  # Auto-picks next queued article
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from hiking_pipeline.models import HikingRunLog, hiking_runs_path

from hiking_pipeline.modules import (
    airtable_client,
    config_loader,
    content_generator,
    geniuslink_client,
    ranker,
    signals_collector,
    trends_collector,
)
from hiking_pipeline.modules.airtable_client import AirtableClientError
from hiking_pipeline.modules.config_loader import ConfigLoaderError
from hiking_pipeline.modules.content_generator import ContentGeneratorError
from hiking_pipeline.modules.geniuslink_client import GeniusLinkError
from hiking_pipeline.modules.ranker import RankerError
from hiking_pipeline.modules.signals_collector import SignalsCollectorError
from hiking_pipeline.modules.trends_collector import TrendsCollectorError

logger = logging.getLogger(__name__)


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _save_run_log(run_log: HikingRunLog, run_date: str) -> None:
    path = hiking_runs_path(run_log.article_id, run_date, "run_log.json")
    path.write_text(run_log.model_dump_json(indent=2))
    logger.info("Run log saved: %s", path)


# ---------------------------------------------------------------------------
# Auto-pick: select the article with the oldest (or missing) last_refresh
# ---------------------------------------------------------------------------

def auto_pick_article() -> str:
    """Pick the next article to build based on staleness.

    Queries Airtable for each article's last_refresh date and picks the one
    that is oldest or has never been built. This ensures round-robin coverage
    across all articles on scheduled runs.

    Falls back to alphabetical first article if Airtable is unreachable.
    """
    import os
    from pyairtable import Api

    article_ids = config_loader.list_article_ids()
    if not article_ids:
        raise SystemExit("No hiking config files found in config/hiking/")

    token = os.environ.get("AIRTABLE_ACCESS_TOKEN")
    base_id = os.environ.get("AIRTABLE_SOFTBALL_BASE_ID")

    if not token or not base_id:
        logger.warning("Airtable credentials not set — falling back to first article: %s", article_ids[0])
        return article_ids[0]

    try:
        api = Api(token)
        table = api.table(base_id, "hiking-articles")
        rows = table.all(fields=["article_id", "last_refresh"])

        # Build map: article_id → last_refresh date string (or None)
        refresh_dates: dict[str, str | None] = {aid: None for aid in article_ids}
        for row in rows:
            fields = row.get("fields", {})
            aid = fields.get("article_id", "")
            if aid in refresh_dates:
                refresh_dates[aid] = fields.get("last_refresh")

        # Sort: None (never built) first, then oldest date
        def sort_key(item: tuple[str, str | None]) -> tuple[int, str]:
            aid, dt = item
            if dt is None:
                return (0, aid)  # Never built — highest priority
            return (1, dt)  # Oldest date first

        sorted_articles = sorted(refresh_dates.items(), key=sort_key)
        picked = sorted_articles[0][0]
        last = sorted_articles[0][1] or "never"
        logger.info("Auto-picked article: %s (last refresh: %s)", picked, last)
        return picked

    except Exception as e:
        logger.warning("Auto-pick Airtable query failed (%s) — falling back to first article: %s", e, article_ids[0])
        return article_ids[0]


# ---------------------------------------------------------------------------
# Daily Build / Manual Refresh
# ---------------------------------------------------------------------------

def run_daily_build(article_id: str, force: bool = False) -> None:
    """Full article build pipeline.

    1. Load config
    2. Collect signals (Amazon Creators API)
    3. Collect trends (Google Trends) — soft failure
    4. Rank products (composite scoring + price terciles)
    5. Enrich with GeniusLink affiliate URLs
    6. Generate content (10 HTML widgets + FAQs)
    7. Write to Airtable (3 tables)
    """
    run_date = _today()
    run_type = "manual_refresh" if force else "daily_build"

    run_log = HikingRunLog(
        article_id=article_id,
        category_id="hiking",
        run_date=run_date,
        run_type=run_type,
        run_started_at=datetime.now(timezone.utc),
    )

    try:
        # Step 1: Load config
        logger.info("=" * 60)
        logger.info("HIKING PIPELINE: %s (%s)", article_id, run_type)
        logger.info("=" * 60)

        config = config_loader.load(article_id)

        # Step 2: Collect signals
        logger.info("--- Step 2: Signals Collection ---")
        signals = signals_collector.collect(config, run_date, force=force)
        run_log.products_found = signals.total_api_results
        run_log.products_after_filter = signals.products_after_filter

        # Step 3: Collect trends (soft failure — trends are a scoring signal, not required)
        logger.info("--- Step 3: Trends Collection ---")
        trends_data = None
        try:
            trends_data = trends_collector.collect(config, run_date, force=force)
            run_log.trends_rising_count = len(trends_data.rising_queries)
            run_log.trends_top_count = len(trends_data.top_queries)
        except TrendsCollectorError as e:
            logger.warning("Trends collection failed (non-fatal): %s", e)
            run_log.trends_failed = True
            run_log.warnings.append(f"Trends failed: {e}")

        # Step 4: Rank
        logger.info("--- Step 4: Ranking ---")
        ranked = ranker.rank(
            signals, config, run_date,
            trends=trends_data,
            force=force,
        )
        run_log.products_ranked = ranked.product_count

        # Step 5: GeniusLink enrichment
        logger.info("--- Step 5: GeniusLink ---")
        linked = geniuslink_client.enrich(ranked, config, run_date, force=force)

        # Count cache stats from run log perspective
        # (actual counts are logged by the module itself)

        # Step 6: Content generation
        logger.info("--- Step 6: Content Generation ---")
        content = content_generator.generate(linked, config, run_date, force=force)
        run_log.widgets_generated = 10
        run_log.faqs_generated = len(content.faqs)

        # Step 7: Airtable write
        logger.info("--- Step 7: Airtable Write ---")
        write_result = airtable_client.write(content, config, run_date)
        run_log.airtable_article_written = write_result["airtable_article_written"]
        run_log.airtable_products_written = write_result["airtable_products_written"]
        run_log.airtable_faqs_written = write_result["airtable_faqs_written"]

        # Success
        run_log.status = "success"
        run_log.run_completed_at = datetime.now(timezone.utc)
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETE: %s", article_id)
        logger.info("=" * 60)

    except (ConfigLoaderError, SignalsCollectorError, RankerError,
            GeniusLinkError, ContentGeneratorError, AirtableClientError) as e:
        run_log.status = "failed"
        run_log.error = str(e)
        run_log.run_completed_at = datetime.now(timezone.utc)
        logger.error("Pipeline failed for %s: %s", article_id, e)
        _save_run_log(run_log, run_date)
        sys.exit(1)
    except Exception as e:
        run_log.status = "failed"
        run_log.error = f"Unexpected error: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        logger.exception("Unexpected pipeline failure for %s", article_id)
        _save_run_log(run_log, run_date)
        sys.exit(1)

    _save_run_log(run_log, run_date)


# ---------------------------------------------------------------------------
# Sunday Price Check
# ---------------------------------------------------------------------------

def run_price_check(article_id: str) -> None:
    """Lightweight Sunday price check.

    1. Load config
    2. Get current products from Airtable
    3. Re-query Amazon for current prices
    4. If price changed >10%, update product + regenerate affected widget
    """
    run_date = _today()

    run_log = HikingRunLog(
        article_id=article_id,
        category_id="hiking",
        run_date=run_date,
        run_type="price_check",
        run_started_at=datetime.now(timezone.utc),
    )

    try:
        logger.info("=" * 60)
        logger.info("PRICE CHECK: %s", article_id)
        logger.info("=" * 60)

        config = config_loader.load(article_id)

        # Get current products from Airtable
        current_products = airtable_client.get_current_products(config)
        run_log.prices_checked = len(current_products)

        if not current_products:
            logger.warning("No products found in Airtable for %s — skipping price check", article_id)
            run_log.status = "success"
            run_log.warnings.append("No products in Airtable — nothing to check")
            run_log.run_completed_at = datetime.now(timezone.utc)
            _save_run_log(run_log, run_date)
            return

        # Re-query Amazon for current prices
        signals = signals_collector.collect(config, run_date, force=True)

        # Build ASIN → new price map
        new_prices = {}
        for p in signals.products:
            if p.price_usd is not None:
                new_prices[p.asin] = p.price_usd

        # Role → widget mapping:
        #   top_pick → widget 2 (Our Top Pick card)
        #   budget_pick, midrange_pick, premium_pick → widget 4 (Three Tiers — one widget with all 3)
        # Note: Airtable stores display names; map them back to pipeline values
        AIRTABLE_ROLE_TO_PIPELINE = {
            "Main Pick": "top_pick",
            "Budget Pick": "budget_pick",
            "Runner Up": "midrange_pick",
            "Upgrade Pick": "premium_pick",
            "Honorable Mention": "comparison",
            "Other": "comparison",
        }
        tier_widget_needs_regen = False  # Track if widget 4 needs regeneration

        # Check each product for price changes >10%
        for product in current_products:
            asin = product.get("asin")
            old_price = product.get("price_usd")
            # Map Airtable display role back to pipeline role
            raw_role = product.get("role", "")
            role = AIRTABLE_ROLE_TO_PIPELINE.get(raw_role, raw_role)

            if not asin or old_price is None:
                continue

            new_price = new_prices.get(asin)
            if new_price is None:
                continue

            # Calculate price change
            pct_change = abs(new_price - old_price) / old_price * 100

            if pct_change > 10:
                logger.info(
                    "Price change for %s: $%.2f → $%.2f (%.1f%%)",
                    asin, old_price, new_price, pct_change,
                )
                run_log.prices_changed += 1

                # Update price in products table
                airtable_client.update_product_price(config, asin, new_price)

                # Regenerate widget 2 if top_pick price changed
                if role == "top_pick":
                    try:
                        from hiking_pipeline.models import HikingLinkedProduct
                        linked = HikingLinkedProduct(
                            rank=0, asin=asin,
                            title=product.get("brand", "") + " " + product.get("model", ""),
                            brand=product.get("brand", ""),
                            model=product.get("model", ""),
                            full_name=product.get("brand", "") + " " + product.get("model", ""),
                            price_usd=new_price,
                            rating=product.get("rating"),
                            review_count=product.get("review_count"),
                            composite_score=0.0, role=role,
                            affiliate_url=product.get("affiliate_url", ""),
                        )
                        new_html = content_generator.regenerate_widget_for_price_change(
                            linked, 2, config, old_price, new_price,
                        )
                        airtable_client.update_article_widget(config, 2, new_html)
                        run_log.widgets_regenerated += 1
                    except ContentGeneratorError as e:
                        logger.warning("Widget 2 regeneration failed for %s: %s", asin, e)
                        run_log.warnings.append(f"Widget 2 regen failed for {asin}: {e}")

                # Flag widget 6 for regeneration if any tier product price changed
                if role in ("budget_pick", "midrange_pick", "premium_pick"):
                    tier_widget_needs_regen = True

        # Regenerate widget 4 (Three Tiers) once if any tier price changed
        if tier_widget_needs_regen:
            try:
                from hiking_pipeline.models import HikingLinkedProduct
                tier_roles = ("budget_pick", "midrange_pick", "premium_pick")
                all_tier_products = []
                trigger_product = None
                trigger_old_price = 0.0
                trigger_new_price = 0.0
                for prod in current_products:
                    r = AIRTABLE_ROLE_TO_PIPELINE.get(prod.get("role", ""), prod.get("role", ""))
                    if r in tier_roles:
                        # Use updated price if available, else current
                        p_price = new_prices.get(prod["asin"], prod.get("price_usd", 0))
                        linked = HikingLinkedProduct(
                            rank=0, asin=prod["asin"],
                            title=prod.get("brand", "") + " " + prod.get("model", ""),
                            brand=prod.get("brand", ""),
                            model=prod.get("model", ""),
                            full_name=prod.get("brand", "") + " " + prod.get("model", ""),
                            price_usd=p_price,
                            rating=prod.get("rating"),
                            review_count=prod.get("review_count"),
                            composite_score=0.0, role=r,
                            affiliate_url=prod.get("affiliate_url", ""),
                        )
                        all_tier_products.append(linked)
                        if trigger_product is None:
                            trigger_product = linked
                            trigger_old_price = prod.get("price_usd", 0)
                            trigger_new_price = p_price

                if trigger_product and all_tier_products:
                    new_html = content_generator.regenerate_widget_for_price_change(
                        trigger_product, 4, config,
                        trigger_old_price, trigger_new_price,
                        all_tier_products=all_tier_products,
                    )
                    airtable_client.update_article_widget(config, 4, new_html)
                    run_log.widgets_regenerated += 1
            except ContentGeneratorError as e:
                logger.warning("Widget 4 (Three Tiers) regeneration failed: %s", e)
                run_log.warnings.append(f"Widget 4 regen failed: {e}")

        run_log.status = "success"
        run_log.run_completed_at = datetime.now(timezone.utc)
        logger.info(
            "Price check complete: %d checked, %d changed, %d widgets regenerated",
            run_log.prices_checked, run_log.prices_changed, run_log.widgets_regenerated,
        )

    except (ConfigLoaderError, SignalsCollectorError, AirtableClientError) as e:
        run_log.status = "failed"
        run_log.error = str(e)
        run_log.run_completed_at = datetime.now(timezone.utc)
        logger.error("Price check failed for %s: %s", article_id, e)
        _save_run_log(run_log, run_date)
        sys.exit(1)
    except Exception as e:
        run_log.status = "failed"
        run_log.error = f"Unexpected error: {e}"
        run_log.run_completed_at = datetime.now(timezone.utc)
        logger.exception("Unexpected price check failure for %s", article_id)
        _save_run_log(run_log, run_date)
        sys.exit(1)

    _save_run_log(run_log, run_date)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _setup_logging()

    parser = argparse.ArgumentParser(description="Hiking pipeline orchestrator")
    parser.add_argument(
        "--article",
        type=str,
        default=None,
        help="Article ID (e.g., hiking-boots). If omitted with daily_build, "
             "lists available articles.",
    )
    parser.add_argument(
        "--type",
        type=str,
        choices=["daily_build", "price_check", "manual_refresh"],
        default="daily_build",
        help="Run type (default: daily_build)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run even if artifacts exist",
    )

    args = parser.parse_args()

    # If no article specified, auto-pick the stalest one
    if args.article is None:
        logger.info("No --article specified, auto-picking...")
        article_id = auto_pick_article()
    else:
        article_id = args.article

    if args.type == "price_check":
        run_price_check(article_id)
    elif args.type == "manual_refresh":
        run_daily_build(article_id, force=True)
    else:
        run_daily_build(article_id, force=args.force)


if __name__ == "__main__":
    main()
