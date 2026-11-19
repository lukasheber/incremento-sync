"""Whether a newer Incremento Sync is out on GitHub (no Qt here; ``desktop.py`` asks and shows it).

GitHub is asked at most once a day, for its public "latest release" of this add-on: nothing about the user is sent
beyond what any web request carries. A version the user declined is not offered again; a later one is. Turned off with
``check_for_updates`` in the add-on's config.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LATEST_URL = "https://api.github.com/repos/lukasheber/incremento-sync/releases/latest"
CHECK_EVERY_SECONDS = 86400
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Release:
    version: str
    url: str


def parse(tag: str) -> tuple[int, int, int] | None:
    found = _VERSION.match(str(tag).strip())
    return None if found is None else (int(found[1]), int(found[2]), int(found[3]))


def is_newer(tag: str, current: str) -> bool:
    latest, mine = parse(tag), parse(current)
    return latest is not None and mine is not None and latest > mine


def fetch_latest(timeout: float = 10.0) -> Any:
    request = urllib.request.Request(LATEST_URL, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "incremento-sync"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (a fixed https URL)
        return json.loads(response.read(256 * 1024).decode("utf-8"))


def _state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(path).with_name(Path(path).name + ".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass  # only means asking GitHub again sooner


def check(state_path: Path, current: str, *, now: float,
          fetch: Callable[[], Any] | None = None) -> Release | None:
    """The newer release to offer, or None: not due yet, none newer, declined before, or no answer."""
    state = _state(state_path)
    checked = state.get("checked_at")
    if isinstance(checked, (int, float)) and now - checked < CHECK_EVERY_SECONDS:
        return None
    try:
        latest = (fetch or fetch_latest)()
    except (OSError, ValueError):
        return None  # offline, or GitHub did not answer: try again next time
    state["checked_at"] = now
    _save(state_path, state)
    if not isinstance(latest, dict) or latest.get("draft") or latest.get("prerelease"):
        return None
    tag, url = str(latest.get("tag_name") or ""), str(latest.get("html_url") or "")
    if not is_newer(tag, current) or not url.startswith("https://"):
        return None
    version = ".".join(str(part) for part in parse(tag) or ())
    if state.get("declined") == version:
        return None
    return Release(version, url)


def dismiss(state_path: Path, version: str) -> None:
    """The user declined ``version``: it is not offered again."""
    state = _state(state_path)
    state["declined"] = version
    _save(state_path, state)
