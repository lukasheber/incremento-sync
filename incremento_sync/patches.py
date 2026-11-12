"""The places where Incremento must behave differently for sync, as reversible patches.

Each patch replaces a function wherever Incremento's backend holds a reference to it (modules that did
``from .x import f`` keep their own name for it) and remembers the original, so ``revert_all`` restores every
reference exactly. A patch that cannot be applied (the function is gone) raises, and the engine stays off.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from types import ModuleType

_applied: list[tuple[ModuleType, str, Callable]] = []


def _replace_everywhere(pkg: ModuleType, owner: str, name: str, make: Callable[[Callable], Callable]) -> None:
    owner_module = importlib.import_module(f"{pkg.__name__}.{owner}")
    original = getattr(owner_module, name)
    replacement = make(original)
    for module_name, module in list(sys.modules.items()):
        if module is None or not module_name.startswith(pkg.__name__ + "."):
            continue
        if getattr(module, name, None) is original:
            setattr(module, name, replacement)
            _applied.append((module, name, original))


def _skip_sync_folder(original: Callable) -> Callable:
    """Backups leave out the sync folder: it only holds copies of files the backup already contains."""

    def should_skip_user_file(rel_path: str) -> bool:
        if original(rel_path):
            return True
        parts = [part for part in str(rel_path).replace("\\", "/").split("/") if part not in ("", ".")]
        return bool(parts) and parts[0] == "sync"

    should_skip_user_file.__wrapped__ = original  # type: ignore[attr-defined]
    return should_skip_user_file


def apply_all(pkg: ModuleType) -> None:
    _replace_everywhere(pkg, "export_bundle", "should_skip_user_file", _skip_sync_folder)


def revert_all() -> None:
    while _applied:
        module, name, original = _applied.pop()
        setattr(module, name, original)
