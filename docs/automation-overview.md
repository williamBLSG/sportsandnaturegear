# Sports & Nature Gear — Automation Overview

*For writers and content collaborators. Technical specifications are at the end.*

---

## What the Automations Do

Sports & Nature Gear runs two automated pipelines that create and publish content to the website without anyone pressing a button. Both pipelines pull real product data, generate written content using AI, and save everything to Airtable (our content database). The website then reads from Airtable to display the pages.

---

## Pipeline 1: Weekly Trending Products

**When it runs:** Every Monday at 6:00 AM Pacific Time

**What it does, step by step:**

1. **Finds what's selling on Amazon.** The system searches Amazon for the current best-selling products in a given category (right now: women's running shoes and men's running shoes). It filters out anything with fewer than 50 reviews, ratings below 3.5 stars, or prices outside the $50–$250 range.

2. **Checks Google Trends.** It looks at what people are actually searching for on Google related to the category — which brands and models are trending up in search interest right now.

3. **Ranks the top 5.** It combines the Amazon sales data with the Google search trends to produce a "Heat Score" for each product. The top 5 products become that week's trending picks. It also compares against last week's rankings to show movement (new entries, products moving up or down).

4. **Creates affiliate links.** Each product gets a trackable affiliate link (through GeniusLink) so we earn commission when readers click through and buy. The system remembers links it has already created, so the same shoe appearing two weeks in a row reuses its existing link.

5. **Writes the content.** AI (Claude) generates all the page copy: the intro paragraph, individual product write-ups, FAQs, meta title/description for SEO, and social media snippets for Pinterest and BlueSky/X. All content is written for our target reader, Active Amy (more on her below).

6. **Publishes to Airtable.** The finished content is saved to three Airtable tables:
   - **Weekly Roundups** — the full page content (intro, methodology blurb, etc.)
   - **Weekly Rankings** — individual product entries with rank, write-up, and affiliate link
   - **Catalog** — a master record for each product that persists across weeks

**The result:** A fresh "Top 5 Trending" page for each category, updated every Monday, with real sales data, real search trends, and affiliate links — all without manual work.

### Current Categories
- Women's Running Shoes
- Men's Running Shoes

New categories can be added without changing any code — just a new configuration file.

---

## Pipeline 2: State Activity Guides

**When it runs:** Every day at 5:00 AM Pacific Time

**What it does, step by step:**

1. **Picks today's state.** The system works through all 50 U.S. states on a rotating daily schedule. One state per day.

2. **Researches each activity.** For the chosen state, it runs through four outdoor activities — camping, hiking, cycling, and kayaking. For each one, it searches the web (state parks sites, tourism boards, AllTrails, recreation.gov, local guides) and collects 15–25 verified facts about doing that activity in that state.

3. **Writes a full article.** AI takes those researched facts and writes a complete guide article — with multiple sections, local tips, and beginner-friendly advice — for each activity in that state.

4. **Saves the article to Airtable.** The guide content goes into the state activities table.

5. **Finds relevant products.** For each activity, it searches Amazon for the top gear (e.g., camping equipment for the camping guide) and ranks the best 10 products.

6. **Writes product copy and creates affiliate links.** Each product gets a short write-up and a trackable affiliate link, same as the weekly pipeline.

7. **Saves products to Airtable.** Product entries are linked to their parent article.

**The result:** Over the course of ~50 days, the system builds out 200 state activity guides (50 states x 4 activities), each with researched local content and relevant product recommendations.

---

## Who the Content Is Written For: Active Amy

All generated content targets a reader persona called **Active Amy**. The AI has been trained to write specifically for her. Here's who she is:

- **Age/demographic:** Women 25–50, middle income, suburban or urban U.S.
- **Lifestyle:** About 70% are mothers with daughters aged 6–16 who are starting a sport. The other 30% are women pursuing personal fitness or outdoor goals.
- **Shopping behavior:** Shops on Amazon, browses Pinterest and Instagram, watches YouTube "best of" and "how-to" videos before buying.
- **What she needs:** Beginner-friendly, jargon-free guidance. She's not a gear expert. She wants to know "what should I buy?" and "will it work for me?" — not technical specs.
- **Values:** Affordability, sustainability, and gear that's stylish enough to feel good wearing.

### What This Means for the Content

- The tone is casual, encouraging, and supportive — like a knowledgeable friend, not a salesperson
- Technical terms always get a plain-English explanation
- The content never assumes she's an expert or talks down to a beginner
- CTAs are action-oriented but not pushy ("Find your fit on Amazon" not "BUY NOW")
- The writing acknowledges that choosing gear can feel overwhelming, then immediately helps

---

## What the AI Is (and Isn't) Allowed to Do

**It can:**
- Turn real product data (price, rating, review count, sales rank) into readable descriptions
- Reference real Google Trends data ("Searches for 'Brooks Ghost 16' are rising this week")
- Write intros, FAQs, meta titles, social snippets
- Mention general fit considerations for women (wider toe boxes, narrower heels)

**It cannot:**
- Invent product specs (weight, foam type, drop height) — if it's not in the data, it doesn't get written
- Make health claims ("reduces knee pain," "corrects pronation")
- Add products that aren't in the data
- Change the rank order
- Use unexplained jargon
- Use a salesy or intimidating tone

Every piece of AI-generated content is automatically validated before publishing. If the AI invents a product name or returns badly formatted content, the system catches it and either retries or stops the run entirely.

---

## Where the Content Lives

All content is stored in **Airtable**, organized into these tables:

| Table | What's in it |
|---|---|
| `weekly_roundups` | Full weekly trending page content (intro, methodology, disclosure, etc.) |
| `weekly_rankings` | Individual product entries per week (rank, write-up, affiliate link, price, rating) |
| `catalog` | Master product records that persist across weeks |
| `faq` | FAQ entries |
| `state_activities` | State activity guide articles |
| `state_activity_products` | Products linked to state activity guides |

The website reads from these tables to render pages. The automation writes to them. This separation means content changes show up on the site without any code deployment.

---

## How Rankings Work (The "Heat Score")

Each product's ranking is based on a **Heat Score** that combines:

1. **Amazon sales signals** — Best Seller Rank (BSR), number of reviews, average rating
2. **Google Trends search interest** — how much search volume the brand/model is getting right now

This is recalculated fresh every week, so rankings reflect real-time buying and searching behavior. The Heat Score formula is deliberately not in the content — readers just see "ranked by sales momentum and buyer ratings."

---

## What Happens When Something Goes Wrong

The automations are designed to fail safely:

- If Amazon returns too few products, the system adjusts and continues with what it has (but stops entirely if fewer than 1 product passes filters)
- If the AI produces bad content, it retries once with a correction prompt, then stops if the second attempt also fails
- If one category or activity fails, the others still run independently
- Every run produces a log file recording exactly what happened, how many products were found, and whether anything went wrong
- Failed runs send email notifications to the repository owner

Nothing broken ever makes it to Airtable. The system would rather stop and report an error than publish bad data.

---
---

# Technical Specifications

*Reference section for developers and technical stakeholders.*

## Architecture

- **Runtime:** Python 3.12, GitHub Actions (scheduled cron + manual `workflow_dispatch`)
- **Data store:** Airtable (shared tables partitioned by `category_id`)
- **Product data:** Amazon Creators API (`searchProducts` endpoint)
- **Trend data:** Google Trends (rising and top queries, matched to products by brand/model)
- **Affiliate links:** GeniusLink API (`POST /v3/shorturls`) with client-side ASIN dedup cache
- **Content generation:** Anthropic API, model `claude-sonnet-4-6` (pinned, no aliases)
- **Web research (state pipeline):** SerpAPI for Google search results
- **Config format:** YAML per category in `config/categories/`, per activity in `config/state-activities/`
- **Validation:** Pydantic schemas in `pipeline/models.py`
- **Secrets:** GitHub Actions Secrets only — never in code, config, logs, or artifacts

## Repository Layout

```
sportsandnaturegear/
├── .github/workflows/
│   ├── weekly-pipeline.yml             # Monday 6am PT — trending products
│   └── state-activity-pipeline.yml     # Daily 5am PT — state guides
├── config/
│   ├── categories/                     # One YAML per product category
│   │   ├── womens-running-shoes.yaml
│   │   └── mens-running-shoes.yaml
│   └── state-activities/               # One YAML per outdoor activity
│       ├── camping.yaml
│       ├── hiking.yaml
│       ├── cycling.yaml
│       └── kayaking.yaml
├── pipeline/
│   ├── run.py                          # Weekly pipeline orchestrator
│   ├── state_activity_run.py           # State pipeline orchestrator
│   ├── models.py                       # All Pydantic schemas (source of truth)
│   └── modules/
│       ├── config_loader.py
│       ├── signals_collector.py        # Amazon Creators API
│       ├── trends_collector.py         # Google Trends
│       ├── ranker.py                   # Heat Score + dedup + rank change
│       ├── geniuslink_client.py        # Affiliate link creation/caching
│       ├── content_generator.py        # Anthropic API (Claude)
│       ├── airtable_client.py          # All Airtable reads/writes
│       ├── state_researcher.py         # SerpAPI + Claude fact extraction
│       └── state_queue_manager.py      # Daily state rotation logic
├── runs/                               # gitignored — local artifacts only
└── tests/
```

## Weekly Pipeline Flow

```
config_loader → signals_collector → trends_collector → ranker → geniuslink_client → content_generator → airtable_client
```

Categories run in parallel via GitHub Actions matrix strategy with `fail-fast: false`.

## State Activity Pipeline Flow

```
state_queue_manager → [for each activity]:
  config_loader → state_researcher → content_generator (article) → airtable_client (article)
  → signals_collector → ranker → content_generator (products) → geniuslink_client → airtable_client (products)
```

Activities run sequentially within a state. A failure in one activity does not abort the others.

## Schedules

| Pipeline | Cron (UTC) | Local Time |
|---|---|---|
| Weekly Trending | `0 14 * * 1` | Monday 6:00 AM PT |
| State Activities | `0 13 * * *` | Daily 5:00 AM PT |

## Primary Keys (Airtable)

| Table | PK Field | Format |
|---|---|---|
| `weekly_roundups` | `slug` | `{category_id}-trending-{week_of}` |
| `weekly_rankings` | `slug` | `{week_of}-{category_id}-{model_slug}` |
| `catalog` | `catalog_slug` | `{category_id}-{model_slug}` |

All writes use upsert (search by PK, update if exists, create if not). Blind inserts are never used.

## Product Filters (Weekly Pipeline)

| Filter | Value |
|---|---|
| Minimum reviews | 50 |
| Minimum rating | 3.5 stars |
| Price range | $50–$250 |

## Required Secrets (GitHub Actions)

| Secret | Used By |
|---|---|
| `AMZ_CREATORS_ACCESS_KEY` | signals_collector |
| `AMZ_CREATORS_SECRET_KEY` | signals_collector |
| `AMZ_ASSOC_TAG` | signals_collector, config_loader |
| `GENIUSLINK_API_KEY` | geniuslink_client |
| `GENIUSLINK_API_SECRET` | geniuslink_client |
| `ANTHROPIC_API_KEY` | content_generator |
| `AIRTABLE_ACCESS_TOKEN` | airtable_client |
| `AIRTABLE_BASE_ID` | airtable_client |
| `SEARCH_API_KEY` | state_researcher (SerpAPI) |

## Idempotency

Every module checks for existing artifacts in `runs/` before doing work. Re-running a pipeline after a partial failure resumes from cached artifacts without re-calling paid APIs. The `--force` flag bypasses this for full re-runs.

## GeniusLink Cache

- Weekly: `runs/{category_id}/geniuslink_cache.json` (one per category, persists across weeks)
- State: `runs/state-activities/geniuslink_cache.json` (shared across all states/activities)
- Persisted across GitHub Actions runs via `actions/cache@v4`
- Client-side dedup only — GeniusLink API has no server-side dedup

## Error Handling

Each module defines a named exception class. The orchestrator (`run.py` / `state_activity_run.py`) catches by type and logs structured context. Failures exit with code 1, triggering GitHub Actions email notifications. Every run writes a structured `run_log.json` regardless of outcome.
