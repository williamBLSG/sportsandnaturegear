"""Config loader — reads category YAML and injects runtime secrets."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from pipeline.models import CategoryConfig

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "categories"
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
        else:
            resolved[key] = value
    return resolved


def load(category_id: str) -> CategoryConfig:
    """Load and validate a category config from YAML.

    Raises ConfigLoaderError on missing file, invalid YAML, missing env vars,
    or validation failure.
    """
    config_path = _CONFIG_DIR / f"{category_id}.yaml"

    if not config_path.exists():
        raise ConfigLoaderError(
            f"Config file not found: {config_path}"
        )

    logger.info("Loading config: %s", config_path)

    try:
        raw_text = config_path.read_text()
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise ConfigLoaderError(f"Invalid YAML in {config_path}: {e}") from e

    if not isinstance(raw_data, dict):
        raise ConfigLoaderError(f"Expected a YAML mapping in {config_path}")

    # Validate category_id matches filename
    file_category_id = raw_data.get("category_id")
    if file_category_id != category_id:
        raise ConfigLoaderError(
            f"category_id in YAML ('{file_category_id}') does not match "
            f"filename ('{category_id}')"
        )

    # Resolve env var references
    try:
        resolved_data = _walk_and_resolve(raw_data)
    except ConfigLoaderError:
        raise

    # Validate with Pydantic
    try:
        config = CategoryConfig(**resolved_data)
    except ValidationError as e:
        raise ConfigLoaderError(
            f"Config validation failed for '{category_id}': {e}"
        ) from e

    logger.info("Config loaded: %s (%s)", config.display_name, config.category_id)
    return config
