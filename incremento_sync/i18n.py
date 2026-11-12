"""The add-on's own texts (its window and two messages), in Incremento's language when it has them."""

from __future__ import annotations

import json
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
_cache: dict[str, dict[str, str]] = {}


def _catalog(locale: str) -> dict[str, str]:
    if locale not in _cache:
        try:
            with open(os.path.join(_DIR, f"{locale}.json"), encoding="utf8") as handle:
                _cache[locale] = json.load(handle)
        except (OSError, ValueError):
            _cache[locale] = {}
    return _cache[locale]


def current_locale() -> str:
    """Incremento's chosen language when it can be asked, else English."""
    from . import loader

    try:
        return str(loader.module("i18n").get_locale())
    except Exception:
        return "en"


def t(message_id: str, **values) -> str:
    text = _catalog(current_locale()).get(message_id) or _catalog("en").get(message_id) or message_id
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text
