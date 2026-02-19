# State Activity Content Pipeline — Developer Specification

**Target site:** https://www.sportsandnaturegear.com/
**Last updated:** 2026-02-19
**Status:** Active
**GitHub Repo:** `sportsandnaturegear` (same repo as weekly trending pipeline)

---

# Overview

This pipeline generates SEO-optimized state activity guide articles and affiliate product sets for SportsAndNatureGear.com. It runs daily via GitHub Actions, processing one U.S. state per day across all configured activities. Day one is Alabama with all 4 activities; day fifty is Wyoming. Each daily run writes article content and affiliate product records to Airtable as Draft records for human review before publishing.

---

# Key Decisions (Pre-Build)

| Decision | Choice | Rationale |
| --- | --- | --- |
| Schedule | **Daily, one state per day** | 50-day run covers all states; all activities for that state run in one day |
| Activity processing | **All activities per state in one run** | Camping, hiking, cycling, kayaking run sequentially for the same state |
| State order | **Alphabetical** | Alabama → Wyoming, predictable queue, easy to monitor |
| Content LLM | **Claude Sonnet 4 via Anthropic API** (`claude-sonnet-4-6`) | Matches existing pipeline; best instruction-following for structured JSON output |
| Research sources | **Web search, priority order** | State park sites → official tourism boards → AllTrails → Recreation.gov → local chambers of commerce |
| Missing facts | **Omit silently** | If a verifiable detail cannot be found, leave it out. No placeholders, no fabricated data |
| H2 sections | **Dynamic per state/activity** | LLM decides how many of the 8 slots to use based on research richness; unused fields left blank |
| Blank H2 fields | **Empty string** | No placeholder text. Frontend hides empty sections automatically |
| Amazon products | **10 per state/activity** | Products 1-5 = group 1, products 6-10 = group 2 |
| Amazon API | **Amazon Creators API** (not PA-API) | PA-API deprecated April 30, 2026 — same API used in trending pipeline |
| Affiliate links | **GeniusLink** | Same account and API pattern as trending pipeline |
| Status on write | **Always Draft** | William reviews in Airtable before publishing. Pipeline never sets Published |
| Data store | **Airtable (`sang-trending-pages` base)** | Two new tables: `state_activities` and `state_activity_products` |
| Runtime | **Python + GitHub Actions** | Zero infrastructure cost; secrets via GitHub Secrets |
| Config | **YAML per activity** | Add a new activity (e.g. fishing) with one new YAML file, zero code changes |
| Manual intervention | **Zero after deploy** | All research, writing, product selection, link creation, and Airtable writes are automated |

---

# 0) Credentials Checklist

All credentials are stored exclusively as **GitHub Actions Secrets** — never in code, config files, or documentation. The variable names below are the secret keys already configured in the repository from the trending pipeline.

### Reused from existing pipeline (no new secrets required)

1. **Amazon Creators API** — `AMZ_CREATORS_ACCESS_KEY`, `AMZ_CREATORS_SECRET_KEY`, `AMZ_ASSOC_TAG`
2. **GeniusLink API** — `GENIUSLINK_API_KEY`, `GENIUSLINK_API_SECRET`
3. **Anthropic API** — `ANTHROPIC_API_KEY`
4. **Airtable** — `AIRTABLE_ACCESS_TOKEN`, `AIRTABLE_BASE_ID`

### New secrets required

5. **Web search API** — `SEARCH_API_KEY`
   - Used for content research phase (state park sites, tourism boards, AllTrails, etc.)
   - Recommended: Serper API (`api.serper.dev`) or SerpAPI — both have Python SDKs and predictable pricing
   - One API key, used across all activities

---

# 1) Architecture

## 1.1 Activity Config Files

Each activity is defined in `config/state-activities/` as a YAML file. Adding a new activity (fishing, skiing, birdwatching) requires only a new YAML file — no code changes.

```yaml
# config/state-activities/camping.yaml
activity_id: camping
display_name: "Camping"
site_name: "Sports & Nature Gear"
site_url: "https://www.sportsandnaturegear.com"
audience: "women and families, beginner-friendly"
table_activities: "state_activities"
table_products: "state_activity_products"
geniuslink_group_id: "state-camping"  # must exist in GeniusLink before first run

# Amazon product search parameters
search_index: "SportingGoods"
keywords: "women's camping gear"
min_reviews: 50
min_rating: 3.5
price_min_usd: 15
price_max_usd: 300

# Research source priority (used to guide search queries)
research_sources:
  - state_parks
  - tourism_boards
  - alltrails
  - recreation_gov
  - chambers_of_commerce

# Article H2 section guidance — LLM decides which apply based on research
h2_section_pool:
  - Hidden Gems and Best Spots
  - When to Go
  - Wildlife and Nature
  - Water Access and Lakeside Sites
  - Cultural and Historic Connections
  - Local Food and Traditions
  - Events and Festivals
  - Gear Tips for [State]
```

```yaml
# config/state-activities/hiking.yaml
activity_id: hiking
display_name: "Hiking"
site_name: "Sports & Nature Gear"
site_url: "https://www.sportsandnaturegear.com"
audience: "women and families, beginner-friendly"
table_activities: "state_activities"
table_products: "state_activity_products"
geniuslink_group_id: "state-hiking"

search_index: "SportingGoods"
keywords: "women's hiking gear"
min_reviews: 50
min_rating: 3.5
price_min_usd: 15
price_max_usd: 250

research_sources:
  - state_parks
  - tourism_boards
  - alltrails
  - recreation_gov
  - chambers_of_commerce

h2_section_pool:
  - Best Trails for Beginners
  - When to Go
  - Wildlife and Nature
  - Terrain and Difficulty What to Expect
  - Cultural and Historic Connections
  - Local Food and Traditions
  - Events and Festivals
  - Gear Tips for [State]
```

```yaml
# config/state-activities/cycling.yaml
activity_id: cycling
display_name: "Cycling"
site_name: "Sports & Nature Gear"
site_url: "https://www.sportsandnaturegear.com"
audience: "women and families, beginner-friendly"
table_activities: "state_activities"
table_products: "state_activity_products"
geniuslink_group_id: "state-cycling"

search_index: "SportingGoods"
keywords: "women's cycling gear"
min_reviews: 50
min_rating: 3.5
price_min_usd: 15
price_max_usd: 300

research_sources:
  - state_parks
  - tourism_boards
  - alltrails
  - recreation_gov
  - chambers_of_commerce

h2_section_pool:
  - Best Rides and Rail Trails
  - When to Go
  - Terrain What to Expect
  - Scenic Routes and Hidden Gems
  - Cultural and Historic Connections
  - Local Food and Traditions
  - Events and Festivals
  - Gear Tips for [State]
```

```yaml
# config/state-activities/kayaking.yaml
activity_id: kayaking
display_name: "Kayaking"
site_name: "Sports & Nature Gear"
site_url: "https://www.sportsandnaturegear.com"
audience: "women and families, beginner-friendly"
table_activities: "state_activities"
table_products: "state_activity_products"
geniuslink_group_id: "state-kayaking"

search_index: "SportingGoods"
keywords: "women's kayaking gear"
min_reviews: 50
min_rating: 3.5
price_min_usd: 15
price_max_usd: 300

research_sources:
  - state_parks
  - tourism_boards
  - alltrails
  - recreation_gov
  - chambers_of_commerce

h2_section_pool:
  - Best Paddling Spots
  - When to Go
  - Wildlife and Nature on the Water
  - Flatwater vs Moving Water What to Expect
  - Cultural and Historic Connections
  - Local Food and Traditions
  - Events and Festivals
  - Gear Tips for [State]
```

## 1.2 State Queue

The pipeline reads from a static queue file that tracks progress across the 50-day run.

```yaml
# config/state-queue.yaml
states:
  - Alabama
  - Alaska
  - Arizona
  - Arkansas
  - California
  - Colorado
  - Connecticut
  - Delaware
  - Florida
  - Georgia
  - Hawaii
  - Idaho
  - Illinois
  - Indiana
  - Iowa
  - Kansas
  - Kentucky
  - Louisiana
  - Maine
  - Maryland
  - Massachusetts
  - Michigan
  - Minnesota
  - Mississippi
  - Missouri
  - Montana
  - Nebraska
  - Nevada
  - New Hampshire
  - New Jersey
  - New Mexico
  - New York
  - North Carolina
  - North Dakota
  - Ohio
  - Oklahoma
  - Oregon
  - Pennsylvania
  - Rhode Island
  - South Carolina
  - South Dakota
  - Tennessee
  - Texas
  - Utah
  - Vermont
  - Virginia
  - Washington
  - West Virginia
  - Wisconsin
  - Wyoming
```

The GitHub Actions workflow passes the current state by computing the day offset from a fixed start date, using the queue index. If a state's run fails, the next day still advances to the next state — no state is skipped permanently. Failed states can be re-run manually via `workflow_dispatch`.

## 1.3 GitHub Actions Workflow

```yaml
# .github/workflows/state-activity-pipeline.yml
on:
  schedule:
    - cron: "0 15 * * *"   # Daily 7am PT (15:00 UTC)
  workflow_dispatch:
    inputs:
      state:
        description: "Override state name (e.g. Alabama). Leave blank for automatic queue."
        required: false
      activity:
        description: "Override single activity (e.g. camping). Leave blank to run all."
        required: false

jobs:
  run-pipeline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python pipeline/state_activity_run.py
        env:
          STATE_OVERRIDE: ${{ github.event.inputs.state }}
          ACTIVITY_OVERRIDE: ${{ github.event.inputs.activity }}
          AMZ_CREATORS_ACCESS_KEY: ${{ secrets.AMZ_CREATORS_ACCESS_KEY }}
          AMZ_CREATORS_SECRET_KEY: ${{ secrets.AMZ_CREATORS_SECRET_KEY }}
          AMZ_ASSOC_TAG: ${{ secrets.AMZ_ASSOC_TAG }}
          GENIUSLINK_API_KEY: ${{ secrets.GENIUSLINK_API_KEY }}
          GENIUSLINK_API_SECRET: ${{ secrets.GENIUSLINK_API_SECRET }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          AIRTABLE_ACCESS_TOKEN: ${{ secrets.AIRTABLE_ACCESS_TOKEN }}
          AIRTABLE_BASE_ID: ${{ secrets.AIRTABLE_BASE_ID }}
          SEARCH_API_KEY: ${{ secrets.SEARCH_API_KEY }}
```

Unlike the trending pipeline which uses a matrix strategy for parallel category runs, the state activity pipeline runs activities sequentially within a single job. This avoids rate limit collisions across the Anthropic API, Creators API, and GeniusLink when processing 4 activities in the same run.

## 1.4 Repository Structure

New files added to the existing `sportsandnaturegear/` repo:

```
sportsandnaturegear/
├── .github/
│   └── workflows/
│       ├── weekly-pipeline.yml          # existing — unchanged
│       └── state-activity-pipeline.yml  # new
├── config/
│   ├── categories/                      # existing — unchanged
│   └── state-activities/               # new
│       ├── camping.yaml
│       ├── hiking.yaml
│       ├── cycling.yaml
│       └── kayaking.yaml
│   └── state-queue.yaml                # new
├── pipeline/
│   ├── run.py                          # existing — unchanged
│   ├── state_activity_run.py           # new — entry point for this pipeline
│   ├── models.py                       # existing — extend with new Pydantic schemas
│   └── modules/
│       ├── config_loader.py            # existing — extend to handle state-activity configs
│       ├── signals_collector.py        # existing — reuse for product search
│       ├── ranker.py                   # existing — reuse for product ranking
│       ├── geniuslink_client.py        # existing — reuse unchanged
│       ├── content_generator.py        # existing — extend with state activity prompt
│       ├── airtable_client.py          # existing — extend for new tables
│       ├── state_researcher.py         # new — web search and fact extraction
│       └── state_queue_manager.py      # new — tracks which state runs today
├── runs/
│   └── state-activities/              # new — gitignored, local artifacts only
│       └── {state}/
│           └── {activity}/
│               ├── research.json
│               ├── article.json
│               ├── products.json
│               └── run_log.json
├── requirements.txt                    # extend with search API client
└── README.md
```

---

# 2) Pipeline Flow

Each daily run processes one state across all configured activities. For a state with 4 activities, the sequence runs 4 times — once per activity — before the job completes.

```
For each activity in [camping, hiking, cycling, kayaking]:
  1. state_researcher     → Research state + activity facts from web
  2. content_generator    → Write article using SANG persona prompt
  3. airtable_client      → Write 1 record to state_activities (status: Draft)
  4. signals_collector    → Search Amazon Creators API for 10 products
  5. ranker               → Score and select top 10 by BSR + rating + reviews
  6. content_generator    → Write product titles, descriptions, CTA text
  7. geniuslink_client    → Create GeniusLink for each product
  8. airtable_client      → Write 10 records to state_activity_products (status: Draft)
```

---

# 3) Data Model

## 3.1 Canonical JSON — Article

Validated with Pydantic before any Airtable write.

```json
{
  "slug": "camping-in-alabama",
  "activity": "camping",
  "state_filter": "Alabama",
  "parent_page_description": "Discover Alabama's best camping spots, from mountain lakes to Gulf Coast shores.",
  "parent_page_cta": "Explore Camping in Alabama",
  "meta_title": "Camping in Alabama: Best Spots for Women & Families",
  "meta_description": "Find Alabama's top campgrounds for women and families. From mountain lakes to Gulf shores, here's where to go and what to bring.",
  "h1": "Camping in Alabama: Where to Go, What to Know",
  "intro": "Alabama doesn't always top the camping bucket list, but it probably should...",
  "h2_1": "Hidden Gems and Best Spots",
  "h2_1_body": "...",
  "h2_2": "When to Go",
  "h2_2_body": "...",
  "h2_3": "Wildlife and Nature",
  "h2_3_body": "...",
  "h2_4": "Water Access and Lakeside Sites",
  "h2_4_body": "...",
  "h2_5": "Cultural and Historic Connections",
  "h2_5_body": "...",
  "h2_6": "Local Food and Traditions",
  "h2_6_body": "...",
  "h2_7": "Events and Festivals",
  "h2_7_body": "...",
  "h2_8": "Gear Tips for Alabama",
  "h2_8_body": "...",
  "product1": "1",
  "product2": "2",
  "status": "Draft"
}
```

Fields `h2_1` through `h2_8` and their body pairs are all optional. If the LLM determines fewer than 8 sections are warranted by the research, unused fields are omitted from the JSON entirely — they will not be written to Airtable, leaving those Airtable fields blank.

## 3.2 Canonical JSON — Products

One object per product, 10 total per state/activity run.

```json
{
  "slug": "camping-in-alabama-1",
  "state": "Alabama",
  "activity": "camping",
  "image_url": "https://m.media-amazon.com/images/I/XXXXX.jpg",
  "image_alt_text": "Coleman 2-person camping tent in green set up at lakeside campsite",
  "title": "Coleman Sundome 2-Person Tent",
  "description": "A reliable, easy-setup tent for beginner campers. Weatherproof and roomy enough for two adults plus gear.",
  "link_text": "See Current Price",
  "affiliate_link": "https://geni.us/XXXXX",
  "asin": "B0XXXXXXX",
  "bsr": 42,
  "product_group": "1",
  "status": "Draft"
}
```

`slug` format: `{activity}-in-{state-slug}-{product-number}` — e.g. `camping-in-alabama-1` through `camping-in-alabama-10`. Product numbers 1-5 write `product_group: "1"`, numbers 6-10 write `product_group: "2"`.

---

# 4) Airtable Schema

## Base: `sang-trending-pages` (`appVT9YEy3ETbKRul`)

Both new tables already exist in Airtable — created prior to this spec. Field IDs are listed for direct API use.

## Table: `state_activities` (`tblhN3nQAsP2vLXXX`)

| Field | Type | Field ID | Notes |
| --- | --- | --- | --- |
| slug | Single line text | `fldFZzQdQ9OnqLPwt` | **Primary key.** Format: `camping-in-alabama` |
| activity | Single line text | `fldgmoUc5WPDcnScS` | e.g. `camping` |
| state_filter | Single line text | `fldybwRGhzbsEO9yc` | Full state name, e.g. `Alabama` |
| parent_page_description | Single line text | `fldRr3Sz6x2O78CAt` | 1 sentence for state hub page |
| parent_page_cta | Single line text | `fldvww4pMJSXXonXO` | Button CTA text |
| meta_title | Single line text | `fldDjPjxlzx9O9e8j` | Max 65 characters |
| meta_description | Single line text | `fldTTrZAk8Dx7fWjz` | Max 165 characters with CTA |
| h1 | Single line text | `fldt54tyKZFWzafLL` | H1 headline |
| intro | Long text | `fldFjqitRaGRexbjq` | Opening paragraphs |
| h2_1 | Single line text | `fldE5f2VSnauZr6xs` | |
| h2_1_body | Long text | `fldSl6nifYlmsoCMs` | |
| h2_2 | Single line text | `fldVBScmYLfWuQvvF` | |
| h2_2_body | Long text | `fldM2CWE4JiNQc1P4` | |
| h2_3 | Single line text | `fldy2639gbUFJwBpg` | |
| h2_3_body | Long text | `fldiaCAS8uRfXbuco` | |
| h2_4 | Single line text | `fld4K0bLsI7S6oK0J` | |
| h2_4_body | Long text | `fldDJv2nlKd7XDeAl` | |
| h2_5 | Single line text | `flduW3DoSedf07TJc` | |
| h2_5_body | Long text | `fldBdphcRIh83bWFf` | |
| h2_6 | Single line text | `fldNDyMAIkGB9GBP1` | |
| h2_6_body | Long text | `fldSTq3TyDqXV3JOu` | |
| h2_7 | Single line text | `fldaPEh0IcOKEOeaS` | |
| h2_7_body | Long text | `fldw0qsFn52wJ6yyA` | |
| h2_8 | Single line text | `fldQAcf3drTaKKY3W` | |
| h2_8_body | Long text | `fldT6St1ixWuJWzFE` | |
| product1 | Single line text | `fldmFBu3Wb16YAWct` | Value: `"1"` — signals group 1 products exist |
| product2 | Single line text | `fldDe5rBh3dqa8ynj` | Value: `"2"` — signals group 2 products exist |
| status | Single select | `fldo9WXcgpEm4reeV` | Always written as `Draft` |

**Upsert key:** `slug`. If a record with the same slug exists, update all fields. If not, create.

## Table: `state_activity_products` (`tblxGtdaPrFEe1BRq`)

| Field | Type | Field ID | Notes |
| --- | --- | --- | --- |
| slug | Single line text | `fld6Znva59kSdE4QK` | **Primary key.** Format: `camping-in-alabama-1` |
| state | Single line text | `fldlqa5g8TzRgEVdF` | Full state name |
| activity | Single line text | `fld3bhliGNHClv7pa` | e.g. `camping` |
| image_url | URL | `fldqdoOp6N6IzbOtx` | Amazon CDN image URL — do not rehost |
| image_alt_text | Single line text | `fldVgqOOMrfbTb6Ve` | Descriptive, SEO-friendly |
| title | Single line text | `fldGAhXckKSqMhz2g` | Max 65 characters |
| description | Single line text | `fld6Xk7NaVb9D0ROj` | Max 165 characters |
| link_text | Single line text | `fldM8FgbUMlu8rT2H` | CTA button text |
| affiliate_link | URL | `fldZrr6zy6QzxKeqm` | GeniusLink URL |
| asin | Single line text | `fldJUsHYkiB79lN8N` | |
| bsr | Number | `fldJNcIHNhSDd1GSU` | BSR at time of capture |
| product_group | Single select | `fldVo3FSX27djrpkr` | `"1"` or `"2"` |
| status | Single select | `fldbkYQBQW6VIRsWj` | Always written as `Draft` |

**Upsert key:** `slug`. Same upsert logic as article table.

---

# 5) Module Specifications

## `state_queue_manager`

- Reads `config/state-queue.yaml`
- Computes today's state by calculating days elapsed since a fixed pipeline start date (e.g. `2026-03-01`) and indexing into the states list
- If `STATE_OVERRIDE` env var is set (from `workflow_dispatch` input), uses that state instead
- Returns the current state name as a string
- Logs which state is being processed in the run log

## `state_researcher`

- Accepts: state name, activity config
- For each research source in priority order (`state_parks → tourism_boards → alltrails → recreation_gov → chambers_of_commerce`):
  - Constructs targeted search queries, e.g. `"Alabama state parks camping site:alapark.com"`, `"camping Alabama tourism"`, `"camping Alabama alltrails"`
  - Calls web search API (Serper or SerpAPI)
  - Fetches top 3 result pages per source
  - Extracts: location names, acreage, trail mileage, fees, permit requirements, seasonal notes, wildlife, cultural/historic details, local events
- Stops adding sources once sufficient facts are gathered (target: 15-25 verifiable details)
- If a detail cannot be verified from any source, it is omitted — no invented data
- Saves raw research output to `runs/state-activities/{state}/{activity}/research.json`
- Returns structured research object for use by `content_generator`

**Research object schema:**
```json
{
  "state": "Alabama",
  "activity": "camping",
  "sources_consulted": ["alapark.com", "alabama.travel", "alltrails.com"],
  "facts": [
    {
      "type": "location",
      "name": "Cheaha State Park",
      "detail": "Alabama's highest point at 2,413 feet; 73 campsites, cabins available",
      "source": "alapark.com"
    }
  ],
  "seasonal_notes": "...",
  "permit_info": "...",
  "cultural_notes": "...",
  "wildlife_notes": "..."
}
```

## `content_generator` (state activity mode)

- Accepts: research object, activity config, state name
- Calls Anthropic API (`claude-sonnet-4-6`) with the master article prompt (see Section 6)
- Parses and validates JSON response with Pydantic
- Validates:
  - All H2 headings have a corresponding non-empty body
  - No body field contains an H2 heading without a corresponding heading field
  - meta_title ≤ 65 characters
  - meta_description ≤ 165 characters
  - No em dashes (—) appear anywhere in the output
  - slug matches format `{activity}-in-{state-slug}`
- On validation failure: retry once with explicit correction instruction; if still failing, log error, skip this activity, continue to next
- Saves article JSON to `runs/state-activities/{state}/{activity}/article.json`

Also handles product copy generation (second call per activity):
- Accepts: list of 10 ranked products with Amazon data
- Calls Anthropic API with the product copy prompt (see Section 6)
- Validates: title ≤ 65 chars, description ≤ 165 chars, link_text is 3-5 words
- Returns 10 product copy objects

## `signals_collector` (reused, state activity mode)

- Same module as trending pipeline
- Called once per activity with activity-specific config (`keywords`, `search_index`, `browse_node_id` if applicable)
- Parameters: `keywords`, `sort_by: HighSales`, `item_count: 20`
- Applies config filters: `price_min_usd`, `price_max_usd`, `min_reviews`, `min_rating`
- Returns top 10 qualifying products (vs top 5 for trending pipeline)
- Saves to `runs/state-activities/{state}/{activity}/products_raw.json`

## `ranker` (reused, state activity mode)

- Same Heat Score formula as trending pipeline
- Normalizes product titles via Claude (entity extraction)
- Deduplicates by brand+model
- Returns top 10 ranked products
- No `rank_change` calculation needed — products are not tracked week-over-week

## `geniuslink_client` (reused, unchanged)

- Same module as trending pipeline
- Called once per product (10 calls per activity)
- UTM parameters appended to the destination Amazon URL before link creation:
  - `utm_source=sportsandnaturegear.com`
  - `utm_medium=states`
  - `utm_campaign={state}` (e.g. `Alabama`)
  - `utm_term={activity}` (e.g. `camping`)
  - `utm_content={short-product-name-slug}` (e.g. `coleman-sundome-tent`)
- GeniusLink group: `geniuslink_group_id` from activity config (e.g. `state-activities-camping`)
- No caching against `catalog` table — state activity products are not tracked for reuse
- Retry: exponential backoff, 3 attempts
- Failure mode: continue with raw `amazon_url`, flag in run log

## `airtable_client` (extended)

- Extends existing module with two new write methods
- `write_state_activity(record)`: upserts to `state_activities` table; upsert key = `slug`
- `write_state_activity_products(records)`: upserts 10 records to `state_activity_products` table; upsert key = `slug`
- Confirms expected row count after write; logs any discrepancy
- All writes set `status` = `Draft`

## `state_activity_run` (new entry point)

- Reads today's state from `state_queue_manager`
- Loads all activity configs from `config/state-activities/`
- If `ACTIVITY_OVERRIDE` env var is set, processes only that activity
- Otherwise, iterates through all activities sequentially
- For each activity:
  1. Calls `state_researcher`
  2. Calls `content_generator` (article)
  3. Calls `airtable_client.write_state_activity`
  4. Calls `signals_collector`
  5. Calls `ranker`
  6. Calls `content_generator` (products)
  7. Calls `geniuslink_client` × 10
  8. Calls `airtable_client.write_state_activity_products`
- Catches exceptions at each step with full stack trace logging
- A failure in one activity does not abort the remaining activities — logs error and continues
- Writes structured run log to `runs/state-activities/{state}/run_log.json`
- Creates GitHub Actions job summary on completion
- Exits with code 1 if any activity failed, triggering GitHub failure notification

---

# 6) LLM Prompt Templates

## 6.1 Article Generation Prompt

```
SYSTEM:
You are a content writer for SportsAndNatureGear.com writing state activity guides
for women and families who are beginners or casual participants.

Your voice: warm, specific, practical. Like a well-traveled friend who genuinely
wants you to have a good trip. You write the way Southern Living editors write —
enthusiastic without being breathless, useful without being dry.

VOICE RULES (follow without exception):
- Write in prose paragraphs. No bullet points inside article sections.
- Use "folks" and "you" naturally. Never "individuals" or "users."
- Be specific: say "365 acres" not "a large lake." Say "7-mile trail" not "miles of trails."
- Be confident: "Jordan Lake is ideal for..." not "Jordan Lake might be a good option for..."
- Keep paragraphs to 3-5 sentences maximum.
- Vary sentence length: short sentences. Then longer ones that earn their length. Back to short.
- Include at least one historical or cultural detail in the article.
- Use contractions naturally: it's, you'll, don't, that's.
- Gear section: shift to practical advisor mode. Lead with what matters most for this
  activity in this specific state (climate, terrain, season).

HARD RULES — violations will cause the output to be rejected:
- Never use em dashes (—). Use commas, periods, or rewrite the sentence.
- Never use: nestled, vibrant, tapestry, boasts, showcasing, seamlessly, breathtaking,
  stunning (max once per article), amazing, incredible, perfect (max once per article),
  leverage, cutting-edge, groundbreaking, game-changer, Furthermore, Moreover,
  Additionally, In conclusion, Whether you're a beginner or an expert,
  There's something for everyone, No matter your skill level.
- Never invent facts. If the research data does not include a specific detail
  (acreage, mileage, fee), omit it. Do not estimate or fabricate.
- Never use placeholder text. If a section has insufficient research to write well, omit it.
- meta_title must be 65 characters or fewer.
- meta_description must be 165 characters or fewer and must include a CTA.
- slug must be exactly: {activity}-in-{state_slug}

USER:
Write a state activity guide about {display_name} in {state}.
Target audience: women aged 25-50, beginners to casual participants, often planning
trips with daughters or friends.

RESEARCH DATA (use only these facts — do not invent additional details):
{research_json}

OUTPUT FORMAT — return a single JSON object with these exact keys.
Omit any h2/body pair where the research does not support a full, specific section.
Do not include empty strings. Do not include placeholders.

"slug": "{activity}-in-{state_slug}" — lowercase, hyphens, no spaces

"activity": "{activity_id}"

"state_filter": "{state}"

"parent_page_description": One sentence (max 120 characters) for the state hub page.
Specific and inviting. No generic phrases.

"parent_page_cta": 3-5 word CTA for the hub page button. e.g. "Explore Camping in Alabama"

"meta_title": Max 65 characters. Include activity and state. Beginner/family angle where natural.

"meta_description": Max 165 characters. Include activity, state, and a clear CTA.
e.g. "Discover Alabama's best camping spots for women and families. Find top sites, gear tips, and when to go. Start planning your trip."

"h1": The article headline. Specific and inviting. Not a restatement of the meta title.

"intro": 2-3 short paragraphs. Open with a human truth or feeling — why people love
this activity, what memory or emotion it connects to. Do not open with a fact or definition.
Second paragraph orients the reader to what the article covers.

"h2_1" through "h2_8": Section headings. Use only the sections supported by the research.
Each heading should be specific to this state and activity — not generic.
For example, not "Best Spots" but "Where Alabama Campers Actually Go."

"h2_1_body" through "h2_8_body": Section body content. Each section: 2-4 paragraphs,
each 3-5 sentences. Lead with what makes it worth reading. Give specific, verifiable
details. End with how it feels to be there.

"product1": "1"
"product2": "2"
"status": "Draft"

Return only valid JSON. No markdown fences. No commentary outside the JSON.
```

## 6.2 Product Copy Prompt

```
SYSTEM:
You are a product copywriter for SportsAndNatureGear.com. You write short, honest,
beginner-friendly product descriptions for women shopping for outdoor gear.
You never invent specifications or make claims not supported by the provided data.
You write like a helpful friend, not a salesperson.

USER:
Write product copy for these {activity} products. Audience: women aged 25-50,
beginners to casual participants.

PRODUCT DATA:
{products_json}

For each product, generate:

"title": Product name, max 65 characters. Clear and descriptive. No hype words.

"description": Max 165 characters. Lead with the primary benefit for a beginner.
End with a specific feature (weight, material, size). No exclamation points.

"link_text": 3-4 word CTA. Options: "See Current Price", "Shop on Amazon",
"Check Today's Price", "View on Amazon."

"image_alt_text": Descriptive alt text. Format:
"{Brand} {product type} {relevant descriptor} for {activity}"
e.g. "Coleman 2-person tent set up at lakeside campsite"

Return a JSON array of 10 objects, one per product, in the same order as input.
Return only valid JSON. No markdown fences. No commentary.
```

---

# 7) GeniusLink UTM Structure

| Parameter | Value | Example |
| --- | --- | --- |
| utm_source | `sportsandnaturegear.com` | `sportsandnaturegear.com` |
| utm_medium | `states` | `states` |
| utm_campaign | State name, lowercase, hyphens | `alabama` |
| utm_term | Activity ID | `camping` |
| utm_content | Short product name slug | `coleman-sundome-tent` |

Full example destination URL before GeniusLink wrapping:
`https://www.amazon.com/dp/B0XXXXXXX?tag=yourtag-20&utm_source=sportsandnaturegear.com&utm_medium=states&utm_campaign=alabama&utm_term=camping&utm_content=coleman-sundome-tent`

GeniusLink group per activity must be created manually in the GeniusLink dashboard before the first pipeline run:
- `state-activities-camping`
- `state-activities-hiking`
- `state-activities-cycling`
- `state-activities-kayaking`

---

# 8) What the LLM Cannot Do

Enforced by system prompt and validated post-generation:

- Invent location names, acreage, trail mileage, permit fees, or any fact not in the research JSON
- Use em dashes anywhere in the output
- Exceed character limits on meta_title or meta_description
- Write a body section without a corresponding heading (or vice versa)
- Use placeholder text or empty strings in included fields
- Use banned vocabulary (full list in system prompt)
- Set status to anything other than `Draft`

---

# 9) Implementation Phases

## Phase 1 — Research Module (1-2 days)

**Goal:** `state_researcher` produces a validated research JSON for one state/activity.

- Web search API integration (Serper or SerpAPI)
- Source priority logic
- Fact extraction and structured output
- ✅ Done when: `research.json` for `camping` in `Alabama` contains 15+ verifiable facts with sources

## Phase 2 — Article Generation (1-2 days)

**Goal:** Full article JSON generated and validated for one state/activity.

- Article prompt integrated into `content_generator`
- Pydantic validation (character limits, no em dashes, slug format)
- Retry logic on validation failure
- ✅ Done when: `article.json` for `camping` in `Alabama` passes all validation checks and reads naturally

## Phase 3 — Product Pipeline (1 day)

**Goal:** 10 products selected, copy written, GeniusLinks created.

- `signals_collector` called with activity config
- `ranker` returns top 10
- Product copy prompt in `content_generator`
- `geniuslink_client` with UTM parameters
- ✅ Done when: 10 product records with live GeniusLinks and validated copy

## Phase 4 — Airtable Write (1 day)

**Goal:** All records written correctly to both new tables.

- `airtable_client` extended with two new write methods
- Upsert logic tested: run twice, confirm zero duplicates
- Blank H2 fields confirmed empty (not null, not placeholder)
- ✅ Done when: Airtable shows 1 article record + 10 product records for `camping-in-alabama`; running again produces no duplicates

## Phase 5 — Full State Run (1 day)

**Goal:** All 4 activities run sequentially for one state.

- `state_activity_run.py` orchestrates full flow
- Activity failure isolation confirmed (one failure does not abort others)
- Run log captures all activity results
- ✅ Done when: Alabama run produces 4 article records + 40 product records in Airtable

## Phase 6 — Queue + Scheduling (1 day)

**Goal:** Daily GitHub Actions run advances through state queue automatically.

- `state_queue_manager` computes correct state from start date
- `workflow_dispatch` manual override tested for both state and activity
- GitHub Actions failure notifications confirmed
- ✅ Done when: Two consecutive scheduled runs produce correct consecutive states with no manual intervention

---

# 10) Edge Cases and Failure Handling

**State has minimal activity content** (e.g. ocean kayaking in a landlocked state): Research module returns fewer facts. LLM uses fewer H2 sections. Article may be shorter. This is correct behavior — do not pad with invented content.

**Web search returns no usable results for a source**: Skip that source, continue to next in priority order. Log which sources were consulted vs skipped.

**Amazon returns fewer than 10 qualifying products**: Accept minimum 5. Log a warning. Product copy prompt adjusted for N products. If fewer than 5, skip product write for this activity and log error.

**Claude returns malformed JSON**: Retry once with a correction prompt specifying the validation failure. If second attempt fails, skip this activity, log error with full response, continue to next activity. Do not write partial data to Airtable.

**Claude output contains em dash**: Caught by Pydantic validation. Retry with explicit correction instruction: "Your previous output contained em dashes (—). Remove all em dashes. Use commas or periods instead."

**GeniusLink API fails for one product**: Continue with raw `amazon_url` in the `affiliate_link` field. Flag in run log. Do not block the remaining 9 products.

**Duplicate slug in Airtable**: Upsert logic handles this — existing record is updated, not duplicated. Confirmed by row count check after write.

**State queue exhausted (day 51+)**: `state_queue_manager` logs that all 50 states are complete. Pipeline exits cleanly. Re-running the full queue requires resetting the start date in config.

---

# 11) Done Definition (MVP)

MVP is complete when:

- GitHub Actions runs automatically every day at 7am PT without manual intervention
- Correct state is selected each day via queue, Alabama through Wyoming over 50 days
- All 4 activities run per state in a single daily job
- 1 article record per activity written to `state_activities` as Draft
- 10 product records per activity written to `state_activity_products` as Draft
- All records have valid slugs, no em dashes, no placeholder text, no invented facts
- Unused H2 fields are blank — not null, not placeholder
- GeniusLink affiliate links are live with correct UTM parameters
- Running the pipeline twice for the same state produces no duplicate Airtable rows
- Manual `workflow_dispatch` override works for both state and activity
- A single activity failure does not abort the remaining activities in that day's run
- Failure alerting tested and confirmed working via GitHub Actions notification
- Adding a 5th activity (e.g. fishing) requires only a new YAML config file in `config/state-activities/`