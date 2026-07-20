"""Centralized Russian text catalog for bot and payment-facing messages."""

from functools import lru_cache
from importlib.resources import files
import json
from typing import Any


@lru_cache
def _catalog() -> dict[str, str]:
    resource = files("app.resources").joinpath("messages.json")
    loaded = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in loaded.items()):
        raise RuntimeError("messages catalog must contain string keys and values")
    return loaded


def message(key: str, /, **values: Any) -> str:
    """Return a catalog message, formatting named placeholders when supplied."""
    try:
        template = _catalog()[key]
    except KeyError as exc:
        raise KeyError(f"Unknown message key: {key}") from exc
    try:
        return template.format(**values)
    except KeyError as exc:
        raise KeyError(f"Missing value {exc.args[0]!r} for message key: {key}") from exc
