# Sports & Nature Gear — Trending Pipeline Architecture Directive

**Last Updated:** 2026-02-21
**Applies to:** `sportsandnaturegear` repository — automated weekly trending pages pipeline
**Target site:** https://www.sportsandnaturegear.com/

---

## 1) Purpose

Keep the pipeline maintainable as categories scale by enforcing a strict module boundary model, preventing credentials from leaking into config or code, and defaulting to idempotency over cleverness. Every rule here exists because the pipeline runs unattended every Monday — there is no one watching to catch a bad pattern at runtime.

---

## 1.5) System Overview

### What This Pipeline Does

An automated weekly job that discovers the top 5 trending products in a given category, generates affiliate-optimized HTML content via LLM, and writes the results to Airtable as permanent structured records.

One pipeline codebase serves every category. Adding a new category requires only a new YAML config file — zero code changes.

**Weekly execution flow:**

1. `config_loader` — reads and validates category YAML, injects runtime secrets
2. `signals_collector` — queries Amazon Creators API, applies config filters, saves raw response
3. `trends_collector` — queries Google Trends for rising/top search queries related to the category
4. `ranker` — normalizes brand/model names, deduplicates, computes Heat Scores (combining Amazon sales data and Google Trends search interest), resolves rank changes. Trend metadata (source, match type, query, search interest, selection tier) is attached to each ranked product.
5. `geniuslink_client` — creates or retrieves cached `geni.us` affiliate links per ASIN using a global per-category cache (persists across weeks). Carries trend fields from `RankedProduct` through to `LinkedProduct`.
6. `content_generator` — calls Anthropic API with structured product data including Google Trends metadata, validates JSON output
7. `airtable_client` — upserts to three shared Airtable tables, partitioned by `category_id`

### Key Technologies

- **Runtime:** Python 3.12, GitHub Actions (scheduled + manual dispatch)
- **Data store:** Airtable (permanent — three shared tables across all categories)
- **Product data:** Amazon Creators API (`searchProducts` endpoint)
- **Trend data:** Google Trends (rising and top queries, matched to products by brand/model)
- **Affiliate links:** GeniusLink API (`POST /v3/shorturls`), with global per-category ASIN cache
- **Content generation:** Anthropic API (`claude-sonnet-4-6`)
- **Config:** YAML per category in `config/categories/`
- **Validation:** Pydantic schemas in `pipeline/models.py`
- **Secrets:** GitHub Actions Secrets only — never in code or config

### Current Categories

- `womens-running-shoes` — Women's Running (Amazon browse node 679255011)
- `mens-running-shoes` — Men's Running (Amazon browse node 679341011)

---

## 2) Core Principles

1. **Config drives behavior, not code.** Category differences live in YAML. Python never branches on `category_id`.
2. **Idempotency is non-negotiable.** Running the pipeline twice for the same week must produce identical Airtable state. No duplicates, no orphaned rows.
3. **Secrets never touch the filesystem.** Credentials exist only as GitHub Actions Secrets, injected as environment variables at runtime. Not in YAML. Not in logs.
4. **Each module does one thing.** No module calls another module directly — `run.py` orchestrates. Modules are independently testable.
5. **Fail loudly, fail early.** A module that can't do its job exits with code 1. The pipeline never silently continues with corrupt state.
6. **LLM output is always validated.** Claude's JSON response is parsed and validated via Pydantic before any Airtable write. Hallucinated product names are caught at this boundary.
7. **Raw data is always saved first.** Before any transformation or write, raw API responses are saved to `runs/`. A failed pipeline can resume from local artifacts without re-calling paid APIs.

---

## 3) Repository Structure

```
sportsandnaturegear/
├── .github/
│   └── workflows/
│       └── weekly-pipeline.yml        # Scheduled + manual dispatch
├── config/
│   └── categories/
│       ├── womens-running-shoes.yaml
│       └── mens-running-shoes.yaml    # Add new categories here only
├── pipeline/
│   ├── run.py                         # Orchestrator — the only file that calls modules
│   ├── models.py                      # Pydantic schemas — canonical JSON contract
│   └── modules/
│       ├── config_loader.py
│       ├── signals_collector.py
│       ├── ranker.py
│       ├── geniuslink_client.py
│       ├── content_generator.py
│       └── airtable_client.py
├── runs/                              # gitignored — local artifacts only
│   ├── {category_id}/
│   │   ├── geniuslink_cache.json      # Global ASIN→geni.us cache (persists across weeks)
│   │   └── {week_of}/
│   │       ├── raw_signals.json
│   │       ├── ranked.json
│   │       ├── canonical.json
│   │       └── run_log.json
│   └── state-activities/
│       ├── geniuslink_cache.json      # Global ASIN→geni.us cache (shared across all states/activities)
│       └── {state_slug}/{activity}/
│           └── ...
├── tests/
│   ├── fixtures/                      # Static JSON for unit tests (no API calls)
│   └── test_*.py
├── requirements.txt
├── .gitignore                         # Must include /runs/
└── README.md
```

**Rules:**
- New categories → new YAML in `config/categories/` only
- New data fields → update `models.py` first, then the module that produces them
- New modules → add to `pipeline/modules/`, wire in `run.py` only
- Never import one module from another — all orchestration lives in `run.py`

---

## 4) Module Rules

### The Orchestrator (`run.py`)

`run.py` is the only file allowed to call modules and the only file that handles cross-module error recovery.

✅ **Correct pattern:**
```python
def main(category_id: str):
    config = config_loader.load(category_id)
    signals = signals_collector.collect(config)
    ranked = ranker.rank(signals, config)
    linked = geniuslink_client.enrich(ranked, config)
    content = content_generator.generate(linked, config)
    airtable_client.write(content, config)
```

❌ **Never do this — modules calling modules:**
```python
# Inside ranker.py
from pipeline.modules import geniuslink_client  # BAD
linked = geniuslink_client.enrich(self.ranked)  # BAD — ranker's job ends at ranking
```

### Module Contracts

Every module must:

- Accept a typed input (Pydantic model or typed dict) and return a typed output
- Raise a named exception on failure (not `Exception` — use `SignalsCollectorError`, `RankerError`, etc.)
- Save its output artifact to `runs/{category_id}/{week_of}/` before returning
- Be callable independently with a fixture for testing

### Idempotency Pattern

Every module checks for an existing artifact before doing work:

```python
def collect(config: CategoryConfig) -> RawSignals:
    artifact_path = runs_path(config, "raw_signals.json")
    if artifact_path.exists():
        logger.info("Resuming from cached raw_signals.json")
        return RawSignals.parse_file(artifact_path)

    signals = _call_amazon_api(config)
    artifact_path.write_text(signals.json())
    return signals
```

This means a partial pipeline failure can be resumed without re-calling paid APIs.

### GeniusLink Cache Strategy

GeniusLink has no server-side dedup — posting the same Amazon URL twice creates two separate short URLs. The pipeline owns dedup responsibility entirely on the client side via global ASIN caches:

- **Weekly pipeline:** `runs/{category_id}/geniuslink_cache.json` — one cache per category, shared across all weeks. The same shoe appearing in consecutive weeks reuses its existing `geni.us` link.
- **State activity pipeline:** `runs/state-activities/geniuslink_cache.json` — one cache shared across all states and activities. The same product across multiple states reuses its existing link.

Both caches are persisted across GitHub Actions runs via `actions/cache` steps in the workflow files. On a cold start (cache miss), links are created normally and cached for future runs.

---

## 5) Config Rules

### What Belongs in YAML

Category-specific values that change per category and never at runtime:

```yaml
category_id: womens-running-shoes
display_name: "Women's Running Shoes"
site_name: "Sports & Nature Gear"
site_url: "https://www.sportsandnaturegear.com"
gender: women
product_type: running shoes
search_index: FashionWomen
browse_node_id: "679255011"
keywords: "women's running shoes"
min_reviews: 50
min_rating: 3.5
price_min_usd: 50
price_max_usd: 250
slug_prefix: "womens-running-shoes-trending"
table_roundups: "weekly_roundups"
table_rankings: "weekly_rankings"
table_catalog: "catalog"
assoc_tag: "${AMZ_ASSOC_TAG}"       # env var reference — injected at runtime
geniuslink_group_id: "womens-running-shoes"
schedule: "every_monday_6am_pt"
```

### What Never Belongs in YAML

❌ API keys, tokens, or secrets of any kind — use `${ENV_VAR_NAME}` references only  
❌ URLs that change at runtime  
❌ Logic, conditionals, or computed values  

### Adding a New Category

1. Copy an existing YAML file
2. Update all category-specific fields
3. Create the corresponding GeniusLink group (must exist before first run)
4. Add the `category_id` to the matrix in `.github/workflows/weekly-pipeline.yml`
5. Done — no Python changes required

---

## 6) Data Model Rules

### `models.py` Is the Source of Truth

All Pydantic schemas live in `models.py`. No module defines its own data shapes.

```
models.py defines → RankedProduct, LinkedProduct, WeeklyRoundup, ProductContent, CatalogEntry, RunLog, CategoryConfig
modules import from models.py → never define inline TypedDicts or ad-hoc dicts
```

`LinkedProduct` carries all `RankedProduct` fields plus `affiliate_url` and `model_slug`. Google Trends fields (`trend_source`, `trend_match_type`, `trend_query`, `trend_search_interest`, `selection_tier`) flow through from `RankedProduct` → `LinkedProduct` → `content_generator` so the LLM can reference real trend data.

### Schema Change Protocol

1. Update `models.py` first — add/change/deprecate the field
2. Update the module that *produces* the field
3. Update the module that *consumes* the field
4. Update the Airtable schema if the field is persisted (document the migration — Airtable has no Alembic)
5. Update fixtures in `tests/fixtures/` to include the new field

**Safe additive change** (no migration needed):
```python
class RankedProduct(BaseModel):
    heat_score: float
    new_optional_field: Optional[str] = None  # New — safe, existing records unaffected
```

**Breaking change** (requires Airtable schema update before deploying):
```python
class RankedProduct(BaseModel):
    heat_score: float        # was: int — type change, breaking
    model_slug: str          # renamed from: slug — breaking
```

### Primary Key Conventions

| Table | Primary Key | Format |
|---|---|---|
| `weekly_roundups` | `slug` | `{category_id}-trending-{week_of}` |
| `weekly_rankings` | `slug` | `{week_of}-{category_id}-{model_slug}` |
| `catalog` | `catalog_slug` | `{category_id}-{model_slug}` |

All upserts use these keys. A write that doesn't use the PK for lookup is a bug.

---

## 7) Airtable Rules

### Content Purpose Context

The HTML written to Airtable will be rendered on pages read by **Active Amy** — the site's primary persona. She is a 25–50 year old woman, middle-income ($50K–$100K household), often a mother with a daughter aged 6–16 starting a sport. She works as a teacher, nurse, office worker, or stay-at-home parent. She is comfortable with online shopping and Pinterest/Instagram but may be new to the gear category she's researching.

She is not a gear expert and will not tolerate unexplained jargon. She shops on Amazon and REI, compares for value, and responds to clear CTAs and beginner-friendly language. About 30% of Amy's audience is women without children who are pursuing personal growth through sports or outdoor activity — content should speak to both groups without assuming one or the other.

Every `intro`, `short_specs`, and FAQ block written to Airtable as HTML must be fit for this reader. `why_hot` is plain text (no HTML tags) since it renders in a non-HTML context in weekly_rankings. This is an editorial quality standard, not just a formatting requirement — if the content reads like a spec sheet, it has failed.

### One Client, One Module

All Airtable interaction happens in `airtable_client.py`. No other module imports the Airtable SDK or constructs API requests.

### Upsert, Never Blind Insert

```python
# ✅ Correct — always upsert by PK
def _upsert_roundup(roundup: WeeklyRoundup, at: AirtableBase):
    existing = at.search("weekly_roundups", "slug", roundup.slug)
    if existing:
        at.update("weekly_roundups", existing[0]["id"], roundup.dict())
    else:
        at.create("weekly_roundups", roundup.dict())

# ❌ Wrong — creates duplicates on re-run
def _write_roundup(roundup: WeeklyRoundup, at: AirtableBase):
    at.create("weekly_roundups", roundup.dict())
```

### Row Count Validation

After every write batch, verify expected row counts:

```python
def write(content: WeeklyRoundup, config: CategoryConfig):
    _upsert_roundup(content, at)
    for product in content.models:
        _upsert_ranking(product, content.week_of, config, at)
        _upsert_catalog(product, config, at)
    
    # Validate
    roundup_count = at.count("weekly_roundups", filter=f"category_id='{config.category_id}'")
    assert roundup_count >= 1, "Roundup write failed"
    ranking_count_this_week = at.count("weekly_rankings", 
        filter=f"week_of='{content.week_of}' AND category_id='{config.category_id}'")
    assert ranking_count_this_week == len(content.models), \
        f"Expected {len(content.models)} ranking rows, found {ranking_count_this_week}"
```

### `category_id` Is Always Set

Every row written to every table must include `category_id`. This is the partition key across all shared tables. A row without it is invisible to category-scoped queries.

---

## 8) LLM Rules

### Primary Audience: Active Amy

All generated content targets a primary persona called **Active Amy**. The LLM must internalize these facts before generating any copy:

**Who she is:**
- Women 25–50, middle-income ($50K–$100K household), suburban or urban U.S. — especially outdoor-friendly states (Colorado, California, Oregon)
- Occupations: teachers, nurses, office workers, stay-at-home parents
- 70% are mothers with daughters aged 6–16 starting sports; 30% are women without children pursuing personal growth
- Comfortable with Amazon, Pinterest, Instagram, and YouTube — not with gear jargon

**Her motivations:**
- Build confidence by trying new activities and completing personal milestones (first hike, first tennis lesson)
- Stay active to improve physical *and* mental health — nature and movement are intertwined for her
- Bond with daughters through shared outdoor experiences (family camping trips, learning tennis together)
- Find gear that is beginner-friendly, durable, stylish, and won't break the budget
- Connect with communities of women in sports and outdoors, both online (Instagram, Pinterest) and local (hiking groups, tennis clubs)
- Make purchases that align with her values — eco-friendly options and recycled-material gear (Patagonia, Adidas) resonate strongly

**Her pain points:**
- Overwhelmed by gear options and unsure where to start ("What's the difference between spiked and spikeless golf shoes?")
- Worried about fit — women-specific sizing concerns like narrower heels and wider toe boxes
- Price-conscious, especially buying for growing kids
- Limited time to research due to work and family; feels self-conscious as a beginner
- Needs guidance on kids' gear sizing ("What size tennis racket for a 10-year-old?")

**How she finds content:**
- Google searches: "best women's hiking boots for beginners" (1,300/mo), "women's ski jackets 2025" (2,900/mo), "tennis gear for girls" (600/mo), "women's running shoes wide toe box," "most comfortable running shoes for women"
- Platforms: Pinterest (pins gear to inspiration boards), Instagram (follows women's outdoor influencers), YouTube (watches "how-to" and "best of" videos before buying)
- Content she engages with: "Top 5 Tips for Women New to Hiking," "Best Women's Bike Shorts for Comfort," FAQ posts addressing common worries ("Are women's hiking boots true to size?")
- Trusts beginner-friendly guides with friendly, encouraging tone over technical reviews or salesy copy
- Clicks affiliate links with clear, action-oriented CTAs like "Shop Women's Hiking Gear Now" or "Explore Beginner Trails in Your State"
- Responds to content featuring diverse women and girls using gear in real settings — trails, tennis courts, slopes

This context must shape all generated copy — intro, product blurbs, FAQ answers, meta titles, and CTAs. It is not background context for the intro paragraph only.

### Content Voice Rules

Amy trusts content that is **casual, encouraging, and beginner-friendly**. The canonical tone signal is: *"Don't worry about the jargon — we'll break it down."* The LLM must apply this throughout all generated HTML, not just introductions.

✅ **Write like this:**
- "These are the 5 women's running shoes Amazon shoppers are buying most right now — ranked by sales momentum and buyer ratings."
- "Great for high-mileage days — nearly 5,000 shoppers give it 4.7 stars."
- "If you're new to running, this is a reliable, well-reviewed place to start."
- "New to hiking? We've got you covered with comfy, stylish picks."
- "Finding gear that actually fits women's feet can be tricky — here's what shoppers are loving right now."

❌ **Never write like this:**
- "Featuring a segmented crash pad and nitrogen-infused midsole foam..." (invented specs, unexplained jargon)
- "Clinically proven to reduce overpronation..." (health/biomechanical claim)
- "The ultimate performance footwear solution for serious athletes..." (salesy, alienating to beginners)
- Dense bullet lists of technical attributes with no plain-English translation
- Copy that assumes the reader is experienced or implicitly talks down to a beginner

**Jargon rule:** If a technical term must appear (e.g., "heel-to-toe drop," "BOA Fit System," "waterproofing rating"), a plain-English explanation must follow in the same sentence or the next. No standalone jargon.

### SEO Awareness

The LLM must write with Active Amy's actual search behavior in mind. She uses Google with beginner-intent, conversational queries — not brand or expert queries. Meta titles and descriptions must reflect her natural search language, not brand marketing language.

**Search patterns to honor:**
- Beginner-intent: "best women's running shoes for beginners," "women's hiking boots for beginners" (~1,300/mo)
- Fit/comfort-focused: "women's running shoes wide toe box," "comfortable women's running shoes"
- Trend/seasonal: "women's ski jackets 2025" (~2,900/mo), "women's running shoes this week"
- Kids/family: "tennis gear for girls" (~600/mo), "girls' running shoes"
- Eco/values: "eco-friendly women's outdoor gear," "sustainable women's running shoes"
- State/location: "women's hiking gear Colorado," "beginner trails Oregon" (relevant for state activity guide content)

**Meta title rules:**
- Include the week or month, category name, and a benefit phrase
- Example: "Top 5 Women's Running Shoes This Week — Ranked by Buyers | Sports & Nature Gear"
- Never: "Best Performance Running Footwear for Elite Athletes 2026"

**Meta description rules:**
- 140–155 characters
- Mention the top-ranked brand, note that rankings update weekly, include a light CTA
- Example: "Brooks leads this week's top 5 women's running shoes, ranked by Amazon sales and buyer ratings. Updated weekly. Find your fit →"

**Intro copy rules:**
- Reference the specific week
- Briefly explain the ranking method in plain language (sales momentum + ratings + Google search trends — no formula details)
- Acknowledge that choosing can feel overwhelming, then immediately reassure
- Example hook: "Finding the right running shoe can feel like a lot — we've done the work so you don't have to."

**CTA rules:**
- Action-oriented, specific, non-salesy: "Shop Women's Running Shoes Now," "See All 5 Picks," "Find Your Fit on Amazon"
- Never: "Purchase Now," "Buy This Product," "Click Here"

### Internal Linking

Generated roundup pages are one node in a broader content graph. Where the category config includes `related_guides` or `related_categories`, the LLM should include natural internal link suggestions in the `internal_links_html` field using the following patterns:

- Category links: "Explore all women's running gear →" linking to the category page
- Related guide links: "New to running? Read our beginner's guide →" linking to a blog post
- Cross-category links: "Also trending: women's trail running shoes →"

Internal link anchor text must be descriptive and match Amy's search language — not "click here" or "learn more." If no related content is specified in config, this field may be omitted.

### What the LLM Is Allowed to Do

- Expand factual product data (BSR, rating, review count, price, category tags) and Google Trends metadata (trend_source, trend_query, trend_search_interest) into readable content in Active Amy's voice
- Reference real Google Trends data in `trend_insight` and `why_hot` fields — e.g., "Searches for 'brooks ghost 16' are rising on Google this week." Only use trend_query values actually present in the product data; never fabricate trend queries.
- Generate meta titles, descriptions, and intro copy using her beginner-intent search language
- Mention general fit considerations relevant to women (wider toe boxes, narrower heels, women-specific last shapes) if `gender: women` is in config — as category-level context only, never as a spec claim about a specific product
- Format data into `<ul>`, `<details>/<summary>`, and `<p>` HTML structures for HTML fields (`intro`, `methodology`, `trend_insight`, `affiliate_disclosure`, `short_specs`). `why_hot` must be plain text with no HTML tags.
- Write FAQ answers that directly address Amy's pain points: fit and sizing, how rankings work, beginner guidance, kids' sizing, and value vs. price tradeoffs
- Reference eco-friendly attributes (recycled materials, sustainable certifications) if present in product data — this aligns with Amy's values around sustainable living and brands like Patagonia and Adidas that she already trusts
- Note when a product suits kids or families if that context fits the category
- Write FAQ entries in a voice that matches the YouTube "how-to" style Amy watches: short, direct answers to specific worries ("Will these run narrow?" / "Are they good for beginners?"), not marketing copy
- Generate social-ready `social_snippet_html` in the Amy-appropriate style: Pinterest pins lead with a benefit headline + emoji + beginner-friendly hook + CTA; BlueSky/X posts are shorter with 1–2 hashtags (e.g., `#WomensRunning #OutdoorGear`)

### What the LLM Must Never Do

- Invent product specifications (weight, drop height, foam type, stack height, cushioning compound)
- Make health or biomechanical claims ("reduces knee pain," "improves pronation," "corrects overpronation," "supports your arch")
- Change the rank order of products
- Add products not in the input data
- Fabricate sources, URLs, review quotes, endorsements, or Google Trends queries not present in the input data
- Write in a salesy or intimidating tone — no "elite performance," "serious athletes," or "ultimate" language directed at a beginner audience
- Use jargon (heel-to-toe drop, BOA Fit System, stack height, carbon fiber plate) without a plain-English explanation in the same sentence or immediately following
- Assume the reader is a mother — write for both the 70% who are and the 30% who aren't

### Social Content Patterns

When the output schema includes social content fields, the LLM must follow Amy's platform conventions precisely.

**Pinterest pin format:**
```
Title: [Benefit headline — e.g., "Best Women's Running Shoes for Beginners"]
Description: [Beginner-friendly hook + 1 practical detail + CTA + 2-3 hashtags]

Example:
"New to running? 👟 Our weekly picks rank the most popular women's running shoes
by real Amazon buyer ratings. Comfy, stylish, and beginner-approved. Shop now! 🏃‍♀️
#WomensRunning #BeginnerRunner #OutdoorGear"
```

**BlueSky / X format:**
```
[Short hook] + [1 data point] + [CTA] + [1-2 hashtags, max]

Example:
"This week's top women's running shoe? Brooks Ghost 16 — 4.7 ⭐ from 12,000+ buyers.
Great for beginners. Shop now → [link] #WomensRunning #OutdoorGear"
```

**Rules for both:**
- Emojis are appropriate and expected — they match the tone Amy engages with
- Never use hashtags like `#BestProduct`, `#MustBuy`, or `#Ad` (unless legally required for affiliate disclosure)
- Pinterest descriptions can be 2–3 sentences; BlueSky/X must fit in ~280 characters
- Always end with a CTA — "Shop now," "See all 5 picks," or "Find your fit"

### Validation Is Mandatory

Every LLM response is validated before use. The pipeline does not trust Claude's output without checking it:

```python
def generate(ranked: list[RankedProduct], config: CategoryConfig) -> WeeklyRoundup:
    response_text = _call_anthropic(ranked, config)
    
    try:
        data = json.loads(response_text)
        roundup = WeeklyRoundup(**data)  # Pydantic validates structure
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"LLM output invalid: {e}. Retrying with correction prompt.")
        response_text = _call_anthropic_with_correction(response_text, str(e), config)
        data = json.loads(response_text)
        roundup = WeeklyRoundup(**data)
    
    _validate_brand_model_integrity(roundup, ranked)  # Names match input
    return roundup

def _validate_brand_model_integrity(roundup: WeeklyRoundup, ranked: list[RankedProduct]):
    input_names = {(p.brand, p.model) for p in ranked}
    for model in roundup.models:
        assert (model.brand, model.model) in input_names, \
            f"LLM invented product: {model.brand} {model.model}"
```

### Model Pin

The model string in code is `claude-sonnet-4-6`. Do not use `claude-sonnet-latest` or any unpinned alias. Unpinned model strings can silently change behavior between runs.

---

## 9) Secrets and Credentials

### The Rule

**Secrets exist in exactly one place: GitHub Actions Secrets.**

| Where | Allowed? |
|---|---|
| GitHub Actions Secrets | ✅ Yes |
| Environment variables (injected from Secrets at runtime) | ✅ Yes |
| YAML config files | ❌ Never |
| Python source code | ❌ Never |
| `runs/` artifact files | ❌ Never (scrub before saving) |
| Log output | ❌ Never |
| Git history | ❌ Never — rotate immediately if committed |

### Required Secrets

| Secret Name | Used By |
|---|---|
| `AMZ_CREATORS_ACCESS_KEY` | `signals_collector` |
| `AMZ_CREATORS_SECRET_KEY` | `signals_collector` |
| `AMZ_ASSOC_TAG` | `signals_collector`, `config_loader` |
| `GENIUSLINK_API_KEY` | `geniuslink_client` |
| `GENIUSLINK_API_SECRET` | `geniuslink_client` |
| `ANTHROPIC_API_KEY` | `content_generator` |
| `AIRTABLE_ACCESS_TOKEN` | `airtable_client` |
| `AIRTABLE_BASE_ID` | `airtable_client` |

### If a Secret Is Exposed

1. Rotate immediately — treat the old value as compromised
2. Check `git log` and `runs/` for any accidental persistence
3. Update the GitHub Secret with the new value
4. Re-run the pipeline to confirm

---

## 10) Error Handling

### Module-Level Exceptions

Each module defines a named exception class. `run.py` catches them by type and logs structured context:

```python
# In signals_collector.py
class SignalsCollectorError(Exception):
    pass

# In run.py
try:
    signals = signals_collector.collect(config)
except SignalsCollectorError as e:
    log_run_failure(config, "signals_collector", str(e))
    sys.exit(1)
```

### Failure Modes and Expected Behavior

| Failure | Behavior |
|---|---|
| Amazon API returns < 3 products after filtering | Log warning, continue with N products, adjust LLM prompt |
| Amazon API returns < 1 product | Abort, exit 1 |
| GeniusLink API fails | Continue with raw `amazon_url`, flag in run log, retry next week |
| LLM returns invalid JSON | Retry once with correction prompt; abort if second attempt fails |
| LLM invents a product name | Abort immediately — do not write corrupt data to Airtable |
| Airtable write fails | Abort, exit 1 — do not write partial state |
| Airtable row count mismatch after write | Abort, exit 1, log discrepancy |

### `run_log.json` Structure

Every run writes a structured log regardless of outcome:

```json
{
  "category_id": "womens-running-shoes",
  "week_of": "2026-03-02",
  "run_started_at": "2026-03-02T14:01:32Z",
  "run_completed_at": "2026-03-02T14:03:11Z",
  "status": "success",
  "products_found": 20,
  "products_after_filter": 12,
  "products_ranked": 5,
  "geniuslink_cached": 3,
  "geniuslink_created": 2,
  "geniuslink_failed": 0,
  "airtable_roundup_written": true,
  "airtable_rankings_written": 5,
  "airtable_catalog_upserted": 5,
  "warnings": [],
  "error": null
}
```

---

## 11) GitHub Actions Rules

### Workflow Structure

Categories run in parallel via matrix strategy. Each category is an independent job — a failure in one does not cancel the other.

```yaml
strategy:
  matrix:
    category: [womens-running-shoes, mens-running-shoes]
  fail-fast: false  # REQUIRED — don't cancel sibling jobs on failure
```

### Adding a Category to the Schedule

1. Add the YAML config to `config/categories/`
2. Add the `category_id` to the matrix array in `weekly-pipeline.yml`
3. That's it

### GeniusLink Cache Persistence

Both workflow files include an `actions/cache@v4` step to persist GeniusLink ASIN caches across runs:

- `weekly-pipeline.yml`: caches `runs/{category}/geniuslink_cache.json` with key `geniuslink-{category}`
- `state-activity-pipeline.yml`: caches `runs/state-activities/geniuslink_cache.json` with key `geniuslink-state-activities`

This prevents duplicate affiliate link creation for the same products across weeks/states. On a cold start, links are created and cached normally.

### Failure Notification

GitHub Actions sends a failure notification email to repository watchers when any job exits with code 1. Ensure the repository owner has "Actions" notifications enabled in GitHub account settings (Settings → Notifications → GitHub Actions).

No additional alerting infrastructure is needed for MVP.

### Manual Testing

Use `workflow_dispatch` to trigger a run manually before the first scheduled Monday:

```bash
# Via GitHub CLI
gh workflow run weekly-pipeline.yml -f category=womens-running-shoes
```

---

## 12) Testing Strategy

### Philosophy

Every module must be testable in isolation against fixture data, with no live API calls. The `runs/` directory is designed for this — save a real API response once, use it as a fixture forever.

### Test Structure

```
tests/
├── fixtures/
│   ├── womens-running-shoes/
│   │   ├── raw_signals.json       # Real Amazon API response (sanitized)
│   │   └── ranked.json            # Expected ranker output
│   └── canonical_example.json    # Full canonical JSON for content_generator tests
└── test_ranker.py
    test_content_generator.py
    test_airtable_client.py
    test_config_loader.py
```

### Required Tests

✅ Write a test when:
- Adding a new module
- Changing the Heat Score formula
- Changing a Pydantic schema
- Adding a new category filter
- Fixing a bug (add regression test before fixing)

### Key Test Cases

**Ranker:**
- Deduplication correctly collapses multiple ASINs for the same brand+model
- Heat Score formula produces expected output for known inputs
- `rank_change` correctly identifies NEW, =, +N, -N from prior week fixture

**Content generator:**
- LLM brand/model integrity check catches an invented product name
- Validation rejects missing required HTML fields
- Retry logic fires on first failure, aborts on second

**Airtable client:**
- Upsert does not create duplicate rows when run twice
- `category_id` is present on every written row
- Row count assertion fires when fewer rows than expected are written

**Config loader:**
- Raises on missing required fields
- Correctly injects env var references

---

## 13) Common Workflows

### Running a Category Manually (Local)

```bash
# Set secrets as env vars locally (use a .env file, never commit it)
export AMZ_CREATORS_ACCESS_KEY=...
export AMZ_CREATORS_SECRET_KEY=...
export AMZ_ASSOC_TAG=...
export GENIUSLINK_API_KEY=...
export GENIUSLINK_API_SECRET=...
export ANTHROPIC_API_KEY=...
export AIRTABLE_ACCESS_TOKEN=...
export AIRTABLE_BASE_ID=...

python pipeline/run.py --category womens-running-shoes
```

### Resuming a Failed Run

If a run fails partway through, the saved artifacts in `runs/{category_id}/{week_of}/` let each module skip re-work:

```bash
# Re-run — modules with saved artifacts resume from them automatically
python pipeline/run.py --category womens-running-shoes
```

To force a full re-run from scratch (re-calls all APIs):

```bash
rm -rf runs/womens-running-shoes/2026-03-02/
python pipeline/run.py --category womens-running-shoes
```

### Adding a New Data Field to the Pipeline

1. Add the field to the appropriate Pydantic model in `models.py`
2. Update the module that produces the field (return it in its output)
3. Update the module that consumes the field (use it)
4. If the field is persisted: add the column to the Airtable table manually (no migrations — document in a comment at the top of `airtable_client.py`)
5. Update `tests/fixtures/` to include the field
6. Run tests

### Changing the Heat Score Formula

The Heat Score formula is in `ranker.py`. It combines Amazon sales signals (BSR, ratings, review counts) with Google Trends search interest data. It is explicitly **not** in config because changes to the formula change the meaning of historical data — they should be deliberate code changes, not config tweaks.

When changing the formula:
1. Document the old formula in a comment before removing it
2. Note the date of the change
3. Understand that `rank_change` comparisons across the formula-change week will be unreliable

---

## 14) Exceptions and Pragmatism

### When Rules Can Bend

✅ **Local development:** Skip idempotency artifact-check logic with a `--force` flag for faster iteration  
✅ **One-off backfills:** A standalone script to backfill historical data can live in `scripts/` outside the main pipeline  
✅ **API shape changes:** If Amazon changes a response field name, a quick fix in `signals_collector.py` may precede a full `models.py` update — mark with `# TODO: update models.py`

### When Rules Cannot Bend

❌ **Secrets in config or code** — rotate and fix immediately  
❌ **Blind inserts to Airtable** — always upsert  
❌ **Unvalidated LLM output written to Airtable** — validate first, always  
❌ **Modules calling other modules** — all orchestration in `run.py`  
❌ **`category_id` missing from any Airtable row** — shared tables depend on this partition key  

---

## 15) Known Edge Cases

**Same shoe in both categories:** The `catalog` PK is `{category_id}-{model_slug}`, not `model_slug` alone. Brooks Ghost 16 in women's and men's are distinct catalog entries with different canonical ASINs. This is by design.

**Same model, multiple colorways, not deduplicated:** If two ASINs for the same brand+model survive deduplication (e.g., a new colorway with more reviews than the flagged canonical), the catalog PK will cause a collision. If this happens in production, the PK will need to incorporate the ASIN directly. Monitor for unexpected `catalog` row counts.

**Amazon browse node ID changes:** Amazon occasionally restructures its category tree. Update `browse_node_id` in the relevant YAML. No code changes needed.

**PA-API credentials:** PA-API was deprecated April 30, 2026. All Amazon credentials must be from the Creators API section of Amazon Associates Central. PA-API keys do not work here.

**GeniusLink group must pre-exist:** The pipeline does not create GeniusLink groups. Groups must be created manually in the GeniusLink dashboard before the first run for any category. The group name convention is the `category_id` value (e.g. `womens-running-shoes`).

**GeniusLink has no server-side dedup:** The GeniusLink API does not deduplicate by destination URL — posting the same Amazon URL twice creates two separate short URLs with different codes. All dedup is handled client-side via global ASIN caches. If the cache is lost (GitHub Actions cache eviction), a few duplicate links will be created on the next run — this is not data corruption, just minor waste in the GeniusLink dashboard.
