"""Config loader — reads hiking article YAML and injects runtime secrets."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from hiking_pipeline.models import HikingArticleConfig

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "hiking"
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


class ConfigLoaderError(Exception):
    pass


def _resolve_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} patterns with values from os.environ."""
    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ConfigLoaderError(
                f"Environment variable '{var_name}' is required but not set"
            )
        return env_value

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _walk_and_resolve(data: dict) -> dict:
    """Recursively resolve env var references in all string values."""
    resolved = {}
    for key, value in data.items():
        if isinstance(value, str) and _ENV_VAR_PATTERN.search(value):
            resolved[key] = _resolve_env_vars(value)
        elif isinstance(value, dict):
            resolved[key] = _walk_and_resolve(value)
        elif isinstance(value, list):
            resolved[key] = [
                _walk_and_resolve(item) if isinstance(item, dict)
                else _resolve_env_vars(item) if isinstance(item, str) and _ENV_VAR_PATTERN.search(item)
                else item
                for item in value
            ]
        else:
            resolved[key] = value
    return resolved


def load(article_id: str) -> HikingArticleConfig:
    """Load and validate a hiking article config from YAML.

    Raises ConfigLoaderError on missing file, invalid YAML, missing env vars,
    or validation failure.
    """
    config_path = _CONFIG_DIR / f"{article_id}.yaml"

    if not config_path.exists():
        raise ConfigLoaderError(
            f"Config file not found: {config_path}"
        )

    logger.info("Loading hiking config: %s", config_path)

    try:
        raw_text = config_path.read_text()
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise ConfigLoaderError(f"Invalid YAML in {config_path}: {e}") from e

    if not isinstance(raw_data, dict):
        raise ConfigLoaderError(f"Expected a YAML mapping in {config_path}")

    # Validate article_id matches filename
    file_article_id = raw_data.get("article_id")
    if file_article_id != article_id:
        raise ConfigLoaderError(
            f"article_id in YAML ('{file_article_id}') does not match "
            f"filename ('{article_id}')"
        )

    # Handle airtable_base_id — strip table ID suffix if present
    # .env has AIRTABLE_SOFTBALL_BASE_ID=appjTpD9Z41EHx64D/tbl7Zg3RtPrzEvWkp
    # We only want the base ID (appjTpD9Z41EHx64D)
    # Resolve env vars first, then strip
    try:
        resolved_data = _walk_and_resolve(raw_data)
    except ConfigLoaderError:
        raise

    airtable_base_id = resolved_data.get("airtable_base_id", "")
    if "/" in airtable_base_id:
        resolved_data["airtable_base_id"] = airtable_base_id.split("/")[0]

    # Validate with Pydantic
    try:
        config = HikingArticleConfig(**resolved_data)
    except ValidationError as e:
        raise ConfigLoaderError(
            f"Config validation failed for '{article_id}': {e}"
        ) from e

    logger.info("Hiking config loaded: %s (%s)", config.display_name, config.article_id)
    return config


def list_article_ids() -> list[str]:
    """Return all article IDs from YAML files in the hiking config directory."""
    if not _CONFIG_DIR.exists():
        return []
    return sorted([
        p.stem for p in _CONFIG_DIR.glob("*.yaml")
    ])
