This is the fully revised, automation-first build spec. It is multi-category from the ground up, uses Airtable as the permanent data store, and requires zero manual intervention after initial deployment.

**Target site:** https://www.sportsandnaturegear.com/

> **Last updated:** 2026-02-18
> 

> **Status:** Active — supersedes all previous versions
> 

> **GitHub Repo:** `sportsandnaturegear`
> 

---

# Key Decisions (Pre-Build)

These are locked decisions that shape the entire architecture below.

| Decision | Choice | Rationale |
| --- | --- | --- |
| Top N per week | **5** | Clean editorial unit; manageable content volume |
| Trending signal | **Amazon Creators API only** (no Google Trends) | BSR + review velocity measure actual purchase intent; Google Trends measures awareness and adds a fragile unofficial dependency |
| Amazon API | **Amazon Creators API** (not PA-API)
https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction | PA-API deprecated April 30, 2026 — build on the successor from day one |
| ASIN resolution | **Fully automated** via Creators API `searchProducts` | No manual CSV mapping tables |
| Content LLM | **Claude Sonnet 4 via Anthropic API** | Best instruction-following for structured JSON/HTML output |
| Data store | **Airtable (permanent)** | Single source of truth for all pipeline output; Duda reads from here or pipeline writes to Duda separately if needed later |
| Runtime | **Python + GitHub Actions** | Zero infrastructure cost for weekly scheduled jobs; secrets via GitHub Secrets |
| Multi-category | **Config-driven** | One YAML file per category — add men's shoes with 12 lines of config, zero code changes |
| Manual intervention | **Zero after deploy** | All resolution, generation, and writes are automated |

---

# 0) Credentials Checklist

All credentials are stored exclusively as **GitHub Actions Secrets** — never in code, config files, or documentation. The variable names below are the secret keys to create in the repository settings.

### Required before any code runs

1. **Amazon Creators API**
    - Secret names: `AMZ_CREATORS_ACCESS_KEY`, `AMZ_CREATORS_SECRET_KEY`, `AMZ_ASSOC_TAG`
    - Where to generate: Amazon Associates Central → Tools → Creators API (not the old PA-API tab)
    - Use: Product search, BSR data, images, pricing, review counts
    - ⚠️ PA-API keys do not work here — generate new credentials in the Creators API section
2. **GeniusLink API**
    - Secret names: `GENIUSLINK_API_KEY`, `GENIUSLINK_API_SECRET`
    - Confirm account is on Business plan (required for API link creation)
    - Use: Generate trackable [geni.us](http://geni.us) affiliate links per ASIN
    - Note on `GENIUSLINK_GROUP_ID`: GeniusLink groups are user-defined labels for organizing links. The default convention for this project is to use the `category_id` value (e.g. `womens-running-shoes`) as the group name — one group per category. Groups must exist in GeniusLink before the pipeline runs. `GENIUSLINK_GROUP_ID` is not a secret — it is defined per category in the YAML config file (see Section 1.1).
3. **Anthropic API**
    - Secret name: `ANTHROPIC_API_KEY`
    - Model in code: `claude-sonnet-4-6`
    - Use: Content generation — all HTML blocks from structured product data
4. **Airtable**
    - Secret names: `AIRTABLE_ACCESS_TOKEN`, `AIRTABLE_BASE_ID`
    - Use: Permanent data store for all pipeline output

---

# 1) Multi-Category Architecture

The entire system is category-agnostic. One Python codebase serves every category by reading a config file.

## 1.1 Category Config File

Each category is defined in `config/categories/` as a YAML file. The `collection_` fields reference Airtable table names.

```yaml
# config/categories/womens-running-shoes.yaml
category_id: womens-running-shoes
display_name: "Women's Running Shoes"
site_name: "Sports & Nature Gear"        # used in LLM prompt and meta titles
site_url: "https://www.sportsandnaturegear.com"
gender: women
product_type: running shoes
search_index: FashionWomen
browse_node_id: "679255011"              # Women's Running on Amazon US
keywords: "women's running shoes"
min_reviews: 50
min_rating: 3.5
price_min_usd: 50
price_max_usd: 250
slug_prefix: "womens-running-shoes-trending"
table_roundups: "weekly_roundups"
table_rankings: "weekly_rankings"
table_catalog: "catalog"
assoc_tag: "${AMZ_ASSOC_TAG}"            # injected from GitHub Secret at runtime
geniuslink_group_id: "womens-running-shoes"  # must exist as a group in GeniusLink before first run
schedule: "every_monday_6am_pt"
```

```yaml
# config/categories/mens-running-shoes.yaml  ← add this file, get a new pipeline
category_id: mens-running-shoes
display_name: "Men's Running Shoes"
site_name: "Sports & Nature Gear"
site_url: "https://www.sportsandnaturegear.com"
gender: men
product_type: running shoes
search_index: FashionMen
browse_node_id: "679341011"              # Men's Running on Amazon US
keywords: "men's running shoes"
min_reviews: 50
min_rating: 3.5
price_min_usd: 50
price_max_usd: 250
slug_prefix: "mens-running-shoes-trending"
table_roundups: "weekly_roundups"
table_rankings: "weekly_rankings"
table_catalog: "catalog"
assoc_tag: "${AMZ_ASSOC_TAG}"
geniuslink_group_id: "mens-running-shoes"    # must exist as a group in GeniusLink before first run
schedule: "every_monday_6am_pt"
```

Note: Both categories write to the same three Airtable tables — rows are partitioned by the `category_id` field. Credentials (`AMZ_ASSOC_TAG`, `GENIUSLINK_API_KEY`, etc.) are injected at runtime from GitHub Secrets and never appear in config files.

## 1.2 GitHub Actions Workflow

```yaml
# .github/workflows/weekly-pipeline.yml
on:
  schedule:
    - cron: "0 14 * * 1"   # Monday 6am PT (14:00 UTC)
  workflow_dispatch:        # Manual trigger for testing

jobs:
  run-pipeline:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        category: [womens-running-shoes, mens-running-shoes]  # add categories here
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python pipeline/run.py --category ${{ matrix.category }}
        env:
          AMZ_CREATORS_ACCESS_KEY: ${{ secrets.AMZ_CREATORS_ACCESS_KEY }}
          AMZ_CREATORS_SECRET_KEY: ${{ secrets.AMZ_CREATORS_SECRET_KEY }}
          AMZ_ASSOC_TAG: ${{ secrets.AMZ_ASSOC_TAG }}
          GENIUSLINK_API_KEY: ${{ secrets.GENIUSLINK_API_KEY }}
          GENIUSLINK_API_SECRET: ${{ secrets.GENIUSLINK_API_SECRET }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          AIRTABLE_ACCESS_TOKEN: ${{ secrets.AIRTABLE_ACCESS_TOKEN }}
          AIRTABLE_BASE_ID: ${{ secrets.AIRTABLE_BASE_ID }}
```

Categories run in parallel via the matrix strategy — women's and men's execute simultaneously, each writing their own rows to Airtable.

## 1.3 Repository Structure

```
sportsandnaturegear/
├── .github/
│   └── workflows/
│       └── weekly-pipeline.yml
├── config/
│   └── categories/
│       ├── womens-running-shoes.yaml
│       └── mens-running-shoes.yaml
├── pipeline/
│   ├── run.py                  # entry point: python pipeline/run.py --category <id>
│   ├── models.py               # Pydantic schemas (canonical JSON contract)
│   └── modules/
│       ├── config_loader.py
│       ├── signals_collector.py
│       ├── ranker.py
│       ├── geniuslink_client.py
│       ├── content_generator.py
│       └── airtable_client.py
├── runs/                       # gitignored — local run artifacts only
│   └── {category_id}/
│       └── {week_of}/
│           ├── raw_signals.json
│           ├── ranked.json
│           ├── canonical.json
│           └── run_log.json
├── requirements.txt
├── .gitignore                  # must include /runs/
└── README.md
```

---

# 2) Trending Signal Strategy

## Why Amazon Creators API Only (No Google Trends)

Google Trends measures what people are searching for — awareness. Amazon BSR measures what people are **buying right now**, updated hourly. For affiliate revenue, purchase intent is the right signal. The unofficial Google Trends library (pytrends) also breaks regularly when Google changes its response format, making it a maintenance liability. BSR + review velocity + rating produces a defensible, data-grounded Heat Score without the fragility.

## The Heat Score Formula

For each product returned by the Creators API, compute a **Heat Score** (0–100):

```
Heat Score = (BSR_component × 0.50) + (review_velocity_component × 0.30) + (rating_component × 0.20)
```

**BSR component:** Inverse of BSR within the browse node. BSR #1 = 100, BSR #500 ≈ 0. Formula: `max(0, 100 - (bsr / 5))`

**Review velocity component:** Total review count as a momentum proxy. High count + high rating = sustained demand signal. Formula: `min(100, review_count / 20)`

**Rating component:** Linear scale of star rating. Formula: `(rating / 5) × 100`

Products rank by Heat Score descending. Top 5 go into the weekly roundup. All component values are stored in Airtable for week-over-week comparison (enabling "up 2 spots" copy).

## Automated ASIN Resolution

1. Call `searchProducts` with `browse_node_id`, `sort_by: HighSales`, `item_count: 20`
2. Apply config filters: price range, `min_reviews`, `min_rating`
3. Normalize product titles via a Claude call to extract canonical brand + model (e.g., "Brooks Ghost 16 Women's Running Shoe (B, Black/White, 9)" → `{brand: "Brooks", model: "Ghost 16"}`)
4. Deduplicate by brand+model — if the same shoe appears as multiple ASINs (colors/sizes), keep the one with the best Heat Score
5. Rank by Heat Score, take top 5
6. Cache ASIN → normalized model in the `catalog` table

Fully automated. No human touches ASIN data.

---

# 3) Data Model

## 3.1 Canonical JSON Contract

Every pipeline module reads from and writes to this schema. Validated with Pydantic before any Airtable write.

```json
{
  "category_id": "womens-running-shoes",
  "week_of": "2026-03-02",
  "slug": "womens-running-shoes-trending-2026-03-02",
  "h1_title": "Top 5 Trending Women's Running Shoes: Week of March 2, 2026",
  "meta_title": "Top 5 Women's Running Shoes This Week (March 2026) | Site Name",
  "meta_description": "The 5 hottest women's running shoes on Amazon this week, ranked by sales momentum and buyer ratings. Updated every Monday.",
  "intro_html": "<p>...</p>",
  "methodology_html": "<p>...</p>",
  "trend_insight_html": "<p>...</p>",
  "faqs_html": "<div>...</div>",
  "affiliate_disclosure_html": "<p>...</p>",
  "models": [
    {
      "rank": 1,
      "brand": "Brooks",
      "model": "Ghost 16",
      "model_slug": "brooks-ghost-16",
      "full_name": "Brooks Ghost 16 Women's Running Shoe",
      "category_tags": ["Daily Trainer", "Neutral"],
      "best_for": "High-mileage daily training",
      "why_hot_html": "<ul><li>...</li><li>...</li><li>...</li></ul>",
      "heat_score": 84.3,
      "bsr": 12,
      "review_count": 4820,
      "rating": 4.7,
      "price_usd": 139.95,
      "rank_change": "+2",
      "asin": "B0XXXXXXX",
      "amazon_url": "https://www.amazon.com/dp/B0XXXXXXX?tag=yourtag-20",
      "geniuslink_url": "https://geni.us/XXXXX",
      "image_url": "https://m.media-amazon.com/images/I/XXXXX.jpg",
      "image_alt": "Brooks Ghost 16 women's running shoe in black",
      "short_specs_html": "<ul><li>Price: $139.95</li><li>Rating: 4.7/5 (4,820 reviews)</li><li>Type: Daily Trainer / Neutral</li><li>Best for: High-mileage daily training</li></ul>"
    }
  ]
}
```

---

# 4) Airtable Schema

One Airtable base (`sportsandnaturegear`) contains three tables shared across all categories. Every row carries a `category_id` field to keep data partitioned.

## Table: `weekly_roundups` (1 row per week per category)

| Field | Type | Notes |
| --- | --- | --- |
| slug | Single line text | **Primary key.** e.g. `womens-running-shoes-trending-2026-03-02` |
| category_id | Single line text | e.g. `womens-running-shoes` |
| week_of | Date | ISO date of the Monday |
| h1_title | Single line text |  |
| meta_title | Single line text |  |
| meta_description | Single line text |  |
| intro | Long text | HTML |
| methodology | Long text | HTML |
| trend_insight | Long text | HTML |
| faqs | Long text | HTML |
| affiliate_disclosure | Long text | HTML |
| status | Single select | Draft / Published / Archived |
| created_at | Created time | Auto |

## Table: `weekly_rankings` (5 rows per week per category)

| Field | Type | Notes |
| --- | --- | --- |
| slug | Single line text | **Primary key.** e.g. `2026-03-02-womens-brooks-ghost-16` |
| category_id | Single line text |  |
| week_of | Date |  |
| roundup_slug | Single line text | FK → weekly_roundups.slug |
| rank | Number | 1–5 |
| rank_change | Single line text | `+2`, `-1`, `NEW`, `=` |
| brand | Single line text |  |
| model | Single line text |  |
| model_slug | Single line text |  |
| full_name | Single line text |  |
| category_tags | Multiple select | Daily Trainer, Stability, Carbon Plate, etc. |
| best_for | Single line text |  |
| why_hot | Long text | HTML `<ul>` |
| heat_score | Number |  |
| bsr | Number | Amazon BSR at time of capture |
| review_count | Number |  |
| rating | Number |  |
| price_usd | Currency |  |
| asin | Single line text |  |
| amazon_url | URL |  |
| geniuslink_url | URL |  |
| primary_image_url | URL | Creators API CDN URL — do not rehost |
| image_alt | Single line text |  |
| short_specs | Long text | HTML `<ul>` |

## Table: `catalog` (deduplicated canonical shoe records)

> **Note on primary key:** The PK is `{category_id}-{model_slug}` (e.g. `womens-running-shoes-brooks-ghost-16`), not `model_slug` alone. This is necessary because the same shoe model can appear in both women's and men's categories with different canonical ASINs. A future edge case to watch: if the same model+gender produces multiple top-ranked ASINs (different colorways that weren't deduplicated cleanly), this PK scheme may still produce collisions. If that occurs in practice, the PK may need to incorporate the ASIN directly. The current approach is the right starting point — revisit if deduplication issues emerge in production.
> 

| Field | Type | Notes |
| --- | --- | --- |
| catalog_slug | Single line text | **Primary key.** Compound: `{category_id}-{model_slug}`, e.g. `womens-running-shoes-brooks-ghost-16`. Note: this PK may need to change if variant deduplication proves insufficient — see note above. |
| category_id | Single line text |  |
| brand | Single line text |  |
| model | Single line text |  |
| asin | Single line text | Canonical ASIN |
| first_seen | Date | First week this shoe appeared in rankings |
| last_seen | Date | Most recent week ranked |
| appearances | Number | Total weeks ranked |
| default_geniuslink_url | URL | Cached [geni.us](http://geni.us) link |
| default_image_url | URL |  |
| evergreen_blurb | Long text | Optional — for future hub/archive pages |

---

# 5) LLM Prompt Template

## Model: `claude-sonnet-4-6` via Anthropic API

The LLM receives only factual data extracted from Amazon — it is never asked to invent specifications, claim health benefits, or fabricate sources. Its sole role is **language expansion and HTML formatting of provided facts**.

## Master Prompt

```
SYSTEM:
You are a technical content writer specializing in athletic footwear. You write concise,
factual, SEO-optimized HTML content for affiliate product pages. You never invent product
specifications, claims, or features. You only expand upon facts explicitly provided to you.
You never make medical or biomechanical health claims. You always write in a helpful,
neutral, third-person voice. Output must be valid HTML fragments (no <html>, <head>,
or <body> tags).

USER:
Generate a weekly trending shoe roundup using the structured data below.
Follow the output format exactly. Do not add fields not listed.

CATEGORY: {display_name}
WEEK OF: {week_of}
SITE NAME: {site_name}

PRODUCT DATA (ranked 1–5, do not change the order):
{models_json}

OUTPUT FORMAT — return a single JSON object with these exact keys:

"h1_title": One H1 string. Format: "Top 5 Trending {display_name}: Week of {formatted_date}"

"meta_title": Under 60 characters. Include week/month and category.

"meta_description": 140–155 characters. Mention the top brand from rank 1, week context,
and that rankings update weekly.

"intro_html": 2–3 sentence <p> paragraph. Reference the week, mention the category,
briefly note the ranking methodology (sales momentum + buyer ratings).
Do not name specific shoes here.

"methodology_html": 1 <p> paragraph. Explain that rankings are derived from Amazon Best
Sellers Rank within the {display_name} category, weighted with buyer rating and review
volume. Keep it factual and brief (2–3 sentences).

"trend_insight_html": 2–3 sentence <p> paragraph. Highlight any meaningful pattern in
this week's top 5: is one brand dominating? Is a particular shoe type (stability, neutral,
carbon plate) prevalent? Base observations only on the provided data.

"faqs_html": An HTML <div> containing exactly 3 <details>/<summary> FAQ pairs relevant
to buying {display_name}. Questions should address: (1) how to choose the right type,
(2) what sales rank means for shoppers, (3) a size/fit consideration. Keep answers
factual and generic — no product-specific claims.

"affiliate_disclosure_html": A single <p> with class "affiliate-disclosure". Standard
FTC-compliant text: disclose that links are affiliate links earning a commission at no
extra cost to the reader.

For each model in the models array, generate:
  "why_hot_html": An HTML <ul> with exactly 3 <li> items. Each bullet must be grounded
  in the provided data (BSR rank, review count, rating, price, category tags, best_for).
  No invented specs.
  "short_specs_html": An HTML <ul> with 3–4 <li> items using only provided data fields:
  price, rating, review_count, category_tags, best_for. Format as "Price: $X.XX",
  "Rating: X.X/5 (N reviews)", etc.
  "image_alt": A descriptive alt string: "{brand} {model} {gender}'s running shoe".

Return only valid JSON. No markdown fences. No commentary outside the JSON.
```

## Prompt Variables

| Variable | Source |
| --- | --- |
| `display_name` | Category config YAML |
| `week_of` | Runtime date (YYYY-MM-DD) |
| `formatted_date` | e.g. "March 2, 2026" |
| `site_name` | Category config YAML (`site_name` field) — e.g. "Sports & Nature Gear" |
| `models_json` | Ranker module output — 5 products with all API-sourced fields |

## What the LLM Cannot Do

Enforced by the system prompt and validated post-generation (pipeline checks all brand/model names in output against input data):

- Invent shoe weight, drop height, foam type, or any spec not in the provided JSON
- Make claims about injury prevention, arch support benefits, or pain relief
- Add sources or URLs not in the input
- Change the rank order
- Add products not in the provided list

---

# 6) Module Specifications

## `config_loader`

- Reads `config/categories/{category_id}.yaml`
- Validates all required fields present
- Injects env var references at runtime
- Returns typed config object

## `signals_collector`

- Calls Amazon Creators API `searchProducts`
- Parameters: `browse_node_id`, `search_index`, `keywords`, `sort_by: HighSales`, `item_count: 20`
- Resources requested: `ItemInfo.Title`, `Images.Primary.Large`, `Offers.Listings.Price`, `CustomerReviews.Count`, `CustomerReviews.StarRating`, `BrowseNodeInfo.BrowseNodes.SalesRank`
- Applies config filters (price range, min_reviews, min_rating)
- **Idempotency:** Saves raw API response to `runs/{category_id}/{week_of}/raw_signals.json`

## `ranker`

- Normalizes brand + model names via a Claude call (entity extraction only — cheap, fast)
- Deduplicates by brand+model, keeping best Heat Score ASIN
- Computes Heat Score per product
- Computes `rank_change` by comparing against prior week's `weekly_rankings` rows in Airtable
- Returns top 5 as ranked list
- **Idempotency:** Saves ranked output to `runs/{category_id}/{week_of}/ranked.json`

## `geniuslink_client`

- Checks `catalog` table for a cached [geni.us](http://geni.us) link for this ASIN less than 90 days old
- If fresh cache exists: reuse
- If not: `POST https://api.geni.us/v3/links` with `{url, group_id, label}`; headers `X-Api-Key`, `X-Api-Secret`
- Stores result back in `catalog`
- Retry: exponential backoff, 3 attempts
- Failure mode: pipeline continues with raw `amazon_url`, flags in run log

## `content_generator`

- Calls Anthropic API with master prompt, injects `models_json` from ranker output
- Parses and validates JSON response with Pydantic
- Validates: brand/model names in output match input, all required HTML fields present
- On validation failure: retry once with explicit correction instruction; if still failing, abort run and alert

## `airtable_client`

- Uses Airtable REST API with Personal Access Token
- Upsert logic: search for existing row by `slug`; if found, update fields; if not, create
- Writes: 1 row to `weekly_roundups`, 5 rows to `weekly_rankings`, up to 5 rows upserted to `catalog` (upsert key: `catalog_slug` = `{category_id}-{model_slug}`)
- Confirms expected row count after write; logs any discrepancy
- All writes use the `category_id` field to keep categories partitioned within shared tables

## `pipeline_runner`

- Orchestrates all modules in sequence
- Catches exceptions at each module boundary with full stack trace logging
- Writes structured run log to `runs/{category_id}/{week_of}/run_log.json`
- Creates GitHub Actions job summary with key stats on completion
- On failure: exits with code 1, which causes GitHub Actions to mark the job red and send a notification to repository watchers via the standard GitHub Actions email notification system. No additional alerting infrastructure required.
- ⚠️ Ensure the repository owner has GitHub Actions failure notifications enabled in their GitHub account notification settings.

---

# 7) Implementation Phases

## Phase 1 — Foundation (1–2 days)

**Goal:** Pipeline skeleton produces valid ranked JSON for one category.

- Repo structure, config loader, Pydantic schemas
- `signals_collector` against Amazon Creators API
- `ranker` with Heat Score formula
- Output saved to `runs/` directory
- ✅ Done when: `python pipeline/run.py --category womens-running-shoes` produces a valid `ranked.json`

## Phase 2 — Content + Links (1–2 days)

**Goal:** Full canonical JSON with generated content and affiliate links.

- `content_generator` with master prompt
- `geniuslink_client`
- Full pipeline wired end-to-end
- ✅ Done when: canonical JSON has all fields, HTML is valid, [geni.us](http://geni.us) links are live

## Phase 3 — Airtable Write (1 day)

**Goal:** Pipeline writes to Airtable with correct idempotency.

- `airtable_client` with upsert logic
- Run twice — confirm zero duplicate rows
- Verify all three tables populated correctly with `category_id` field set
- ✅ Done when: Airtable shows correct rows after 2 runs, no duplicates

## Phase 4 — Second Category (half day)

**Goal:** Men's running shoes added by config file alone, no code changes.

- Create `config/categories/mens-running-shoes.yaml`
- Run `--category mens-running-shoes`
- ✅ Done when: men's rows appear in Airtable alongside women's, correctly partitioned by `category_id`

## Phase 5 — Scheduling + Monitoring (1 day)

**Goal:** Fully automated weekly runs with alerting.

- GitHub Actions workflow with matrix strategy
- All secrets configured in repository settings
- Confirm GitHub Actions failure notifications are enabled in account settings
- Manual `workflow_dispatch` trigger verified
- ✅ Done when: workflow runs green on both categories simultaneously; deliberately failing a run confirms GitHub sends a failure notification email

---

# 8) Edge Cases and Failure Handling

**Amazon API returns fewer than 5 qualifying products:** Pipeline accepts minimum 3, logs a warning, continues. Content generator prompt is adjusted for N products.

**Same shoe ranks #1 multiple weeks:** Correct behavior. `rank_change` shows `=`. Copy references sustained momentum. No artificial rotation.

**GeniusLink API fails:** Run continues with raw `amazon_url`. Next week's run retries link creation for that ASIN and updates the `catalog` entry.

**Claude returns malformed JSON:** Retry once with a correction prompt. If second attempt fails, run aborts with alert. Prior week's Airtable data is untouched.

**Amazon image URL breaks:** Do not rehost images (ToS violation). Pipeline refetches from Creators API on next run. Airtable row updated automatically.

**Browse node ID changes:** Amazon occasionally restructures category trees. Update the `browse_node_id` in the relevant YAML config. No code changes needed.

---

# 9) Done Definition (MVP)

MVP is complete when:

- GitHub Actions runs automatically every Monday without any manual intervention
- Women's and men's categories run in parallel, each producing 5 ranked products
- All three Airtable tables populated correctly with no duplicate rows
- `category_id` correctly partitions all data within shared tables
- Canonical JSON saved to `runs/` for audit and reproducibility
- GeniusLink affiliate links live and cached in `catalog`
- LLM-generated HTML is factual, validates via Pydantic, and passes manual spot-check
- Failure alerting tested and confirmed working
- Adding a third category requires only a new YAML config file