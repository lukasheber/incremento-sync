"""Switching the sync engine on and off in a running Incremento add-on, reversibly.

``enable(incremento)`` does three things, each undone by ``disable()``:

1. the add-on's ``engine/`` folder joins the search path of Incremento's ``backend`` package, so the sync
   modules import there like Incremento's own (they use relative imports and share its ``paths``, ``db``…);
2. the few places where Incremento must behave differently for sync are patched (see ``patches.py``);
3. the host-specific parts (the desktop's menu and timers) are registered by the caller and handed back to
   ``disable()`` as callbacks with ``on_disable``.

Nothing is written into Incremento's own files. The engine's tables and files in the user's profile stay when
the engine is switched off, so switching it on again resumes; ``remove_data`` deletes them.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from types import ModuleType

from . import patches

ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")


class Unsupported(RuntimeError):
    """The Incremento that is loaded cannot carry the sync engine (missing or changed internals)."""


class _State:
    backend: ModuleType | None = None
    prefix: str = ""
    undo: list[Callable[[], None]]

    def __init__(self) -> None:
        self.undo = []


_state = _State()


def is_enabled() -> bool:
    return _state.backend is not None


def backend() -> ModuleType:
    if _state.backend is None:
        raise RuntimeError("the sync engine is not enabled")
    return _state.backend


def module(name: str) -> ModuleType:
    """An engine module (``sync_service``…) as loaded inside Incremento's backend."""
    return importlib.import_module(f"{backend().__name__}.{name}")


def on_disable(callback: Callable[[], None]) -> None:
    """Registers what to undo when the engine is switched off (run last-in, first-out)."""
    _state.undo.append(callback)


def enable(incremento: ModuleType) -> None:
    """Loads the engine into [incremento]'s backend and applies the patches; idempotent."""
    if is_enabled():
        return
    try:
        pkg = importlib.import_module(f"{incremento.__name__}.backend")
    except ImportError as error:
        raise Unsupported(f"{incremento.__name__} has no backend package") from error
    if ENGINE_DIR not in pkg.__path__:
        pkg.__path__.append(ENGINE_DIR)
    _state.backend, _state.prefix = pkg, f"{pkg.__name__}.sync_"
    try:
        importlib.import_module(f"{pkg.__name__}.sync_service")
        patches.apply_all(pkg)
    except Exception as error:
        disable()
        raise Unsupported(f"the sync engine does not fit this Incremento: {error}") from error


def disable() -> None:
    """Undoes everything [enable] and the host did; safe to call when not enabled."""
    while _state.undo:
        try:
            _state.undo.pop()()
        except Exception:  # undoing must go on
            pass
    patches.revert_all()
    pkg, prefix = _state.backend, _state.prefix
    if pkg is None:
        return
    for name in [n for n in sys.modules if n.startswith(prefix)]:
        del sys.modules[name]
        leaf = name.rsplit(".", 1)[1]
        if hasattr(pkg, leaf):
            try:
                delattr(pkg, leaf)
            except AttributeError:
                pass
    if ENGINE_DIR in pkg.__path__:
        pkg.__path__.remove(ENGINE_DIR)
    importlib.invalidate_caches()
    _state.backend, _state.prefix = None, ""


def remove_data(addon_dir: str, profile: str) -> None:
    """Deletes what sync keeps in [profile]: its tables and its folder of blobs (only when the user asks)."""
    import shutil

    sync_schema, sync_paths = module("sync_schema"), module("sync_paths")
    db = importlib.import_module(f"{backend().__name__}.db")
    conn = db.get_connection(os.fspath(addon_dir), profile)
    with conn:
        for table in sync_schema.SYNC_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    shutil.rmtree(sync_paths.get_sync_dir(os.fspath(addon_dir), profile), ignore_errors=True)
