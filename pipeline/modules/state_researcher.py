"""State researcher — web search + Claude fact extraction for state activity guides."""

from __future__ import annotations

import json
import logging
import os

import anthropic
import requests

from pipeline.models import (
    ResearchFact,
    ResearchOutput,
    StateActivityConfig,
    state_runs_path,
)

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"
SERPAPI_URL = "https://serpapi.com/search"
MAX_RESULTS_PER_QUERY = 5
MAX_SNIPPETS_PER_QUERY = 3
TARGET_FACTS_MIN = 15
TARGET_FACTS_MAX = 25


class StateResearcherError(Exception):
    pass


# ---------------------------------------------------------------------------
# Search query construction
# ---------------------------------------------------------------------------

_QUERY_TEMPLATES: dict[str, str] = {
    "state_parks": '"{state} state parks {activity}"',
    "tourism_boards": '"{activity} in {state} tourism guide"',
    "alltrails": '"{state} {activity} site:alltrails.com"',
    "recreation_gov": '"{state} {activity} site:recreation.gov"',
    "chambers_of_commerce": '"{activity} {state} local guide"',
}


def _build_query(source: str, state: str, activity: str) -> str:
    """Build a search query for a given source type."""
    template = _QUERY_TEMPLATES.get(source)
    if not template:
        return f"{activity} in {state} {source}"
    return template.format(state=state, activity=activity)


# ---------------------------------------------------------------------------
# SerpAPI
# ---------------------------------------------------------------------------

def _search_serpapi(query: str, api_key: str) -> list[dict]:
    """Call SerpAPI and return organic results.

    Returns a list of dicts with keys: title, snippet, link.
    """
    params = {
        "q": query,
        "api_key": api_key,
        "engine": "google",
        "num": MAX_RESULTS_PER_QUERY,
    }

    resp = requests.get(SERPAPI_URL, params=params, timeout=15)
    resp.raise_for_status()

    data = resp.json()
    organic = data.get("organic_results", [])

    results = []
    for item in organic[:MAX_SNIPPETS_PER_QUERY]:
        results.append({
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "link": item.get("link", ""),
        })
    return results


# ---------------------------------------------------------------------------
# Claude fact extraction
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM = (
    "You are a research assistant extracting verifiable facts about outdoor "
    "activities in U.S. states from web search results. "
    "Only include facts that are directly supported by the provided snippets. "
    "Attribute every fact to its source URL. "
    "Do not invent, estimate, or fabricate any detail. "
    "If a snippet is too vague to extract a concrete fact, skip it."
)

_EXTRACTION_USER = """\
Extract structured facts about {activity} in {state} from these search results.

SEARCH RESULTS:
{snippets_json}

Return a single JSON object with these exact keys:

"state": "{state}"
"activity": "{activity}"
"sources_consulted": [list of unique source domains consulted]
"facts": [array of fact objects, target {target_min}-{target_max} facts]
  Each fact object:
    "type": one of "location", "season", "wildlife", "culture", "event", "permit", "general"
    "name": location/event name if applicable, otherwise null
    "detail": the specific fact (include numbers, dates, fees when available)
    "source": the source URL this fact came from
"seasonal_notes": summary of best seasons/timing (empty string if insufficient data)
"permit_info": summary of permit requirements (empty string if insufficient data)
"cultural_notes": cultural or historical context (empty string if insufficient data)
"wildlife_notes": notable wildlife encounters (empty string if insufficient data)

Return only valid JSON. No markdown fences. No commentary outside the JSON."""


def _extract_facts(
    state: str,
    activity: str,
    all_snippets: list[dict],
    api_key: str,
) -> ResearchOutput:
    """Send collected snippets to Claude for structured fact extraction."""
    client = anthropic.Anthropic(api_key=api_key)

    user_prompt = _EXTRACTION_USER.format(
        state=state,
        activity=activity,
        snippets_json=json.dumps(all_snippets, indent=2),
        target_min=TARGET_FACTS_MIN,
        target_max=TARGET_FACTS_MAX,
    )

    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=4096,
        system=_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = response.content[0].text

    try:
        data = json.loads(response_text)
        output = ResearchOutput(**data)
    except (json.JSONDecodeError, Exception) as e:
        raise StateResearcherError(
            f"Claude fact extraction returned invalid output: {e}"
        ) from e

    return output


def _extract_facts_with_retry(
    state: str,
    activity: str,
    all_snippets: list[dict],
    api_key: str,
) -> ResearchOutput:
    """Extract facts with one retry on failure."""
    try:
        return _extract_facts(state, activity, all_snippets, api_key)
    except StateResearcherError as first_error:
        logger.warning(
            "First fact extraction attempt failed: %s. Retrying with correction.",
            first_error,
        )

    # Retry with correction prompt
    client = anthropic.Anthropic(api_key=api_key)

    correction_prompt = (
        f"Your previous response was invalid: {first_error}\n\n"
        "Please try again. Return ONLY valid JSON matching the schema exactly. "
        "No markdown fences. No text outside the JSON object.\n\n"
        + _EXTRACTION_USER.format(
            state=state,
            activity=activity,
            snippets_json=json.dumps(all_snippets, indent=2),
            target_min=TARGET_FACTS_MIN,
            target_max=TARGET_FACTS_MAX,
        )
    )

    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=4096,
        system=_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": correction_prompt}],
    )

    response_text = response.content[0].text

    try:
        data = json.loads(response_text)
        output = ResearchOutput(**data)
    except (json.JSONDecodeError, Exception) as e:
        raise StateResearcherError(
            f"Claude fact extraction failed on retry: {e}"
        ) from e

    return output


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def research(
    state: str,
    config: StateActivityConfig,
    force: bool = False,
) -> ResearchOutput:
    """Research a state+activity combination via web search and Claude extraction.

    Checks for cached research.json artifact before doing work (idempotency).
    Set force=True to skip the cache check.

    Raises StateResearcherError on unrecoverable failures.
    """
    artifact_path = state_runs_path(state, config.activity_id, "research.json")

    # Idempotency: return cached result if available
    if not force and artifact_path.exists():
        logger.info("Resuming from cached research.json: %s", artifact_path)
        try:
            data = json.loads(artifact_path.read_text())
            return ResearchOutput(**data)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("Cached research.json is invalid, re-running: %s", e)

    # Validate required env vars
    search_api_key = os.environ.get("SEARCH_API_KEY")
    if not search_api_key:
        raise StateResearcherError(
            "SEARCH_API_KEY environment variable is not set"
        )

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise StateResearcherError(
            "ANTHROPIC_API_KEY environment variable is not set"
        )

    activity = config.activity_id

    # Collect snippets from each source in priority order
    all_snippets: list[dict] = []
    sources_consulted: list[str] = []

    for source in config.research_sources:
        query = _build_query(source, state, activity)
        logger.info("Searching [%s]: %s", source, query)

        try:
            results = _search_serpapi(query, search_api_key)
        except requests.RequestException as e:
            logger.warning("SerpAPI failed for source '%s': %s. Skipping.", source, e)
            continue

        if results:
            sources_consulted.append(source)
            for r in results:
                all_snippets.append({
                    "source": source,
                    "query": query,
                    "title": r["title"],
                    "snippet": r["snippet"],
                    "link": r["link"],
                })

        logger.info(
            "Collected %d snippets so far from %d sources",
            len(all_snippets), len(sources_consulted),
        )

    if not all_snippets:
        raise StateResearcherError(
            f"No search results collected for {activity} in {state}. "
            "All sources failed or returned empty results."
        )

    logger.info(
        "Total snippets collected: %d from sources: %s",
        len(all_snippets), ", ".join(sources_consulted),
    )

    # Extract structured facts via Claude
    output = _extract_facts_with_retry(state, activity, all_snippets, anthropic_api_key)

    if len(output.facts) < 5:
        logger.warning(
            "Only %d facts extracted for %s in %s (target: %d-%d). Proceeding anyway.",
            len(output.facts), activity, state, TARGET_FACTS_MIN, TARGET_FACTS_MAX,
        )

    # Save artifact
    artifact_path.write_text(output.model_dump_json(indent=2))
    logger.info(
        "Research complete: %d facts saved to %s",
        len(output.facts), artifact_path,
    )

    return output
