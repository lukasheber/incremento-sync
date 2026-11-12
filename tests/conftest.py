"""Test setup: the engine runs against a real Incremento checkout (``INCREMENTO_DIR``, else ``vendor/incremento``).

Incremento's own tests import its ``backend`` modules flat, with Anki and Qt mocked; these tests do the same, with
this add-on's ``engine/`` folder first on the path, so ``import sync_service`` finds the engine and ``import db``
finds Incremento's.
"""

import os
import sys
import types
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ADDON = os.path.join(REPO, "incremento_sync")


def _incremento_dir() -> str:
    for candidate in (os.environ.get("INCREMENTO_DIR"), os.path.join(REPO, "vendor", "incremento")):
        if candidate and os.path.isfile(os.path.join(candidate, "backend", "db_schema.py")):
            return os.path.abspath(candidate)
    raise RuntimeError("Set INCREMENTO_DIR to a checkout of Incremento (or add it as vendor/incremento).")


INCREMENTO = _incremento_dir()
os.environ["INCREMENTO_DIR"] = INCREMENTO  # for the subprocess tests


class ModuleMock(MagicMock):
    """Keep lazy stand-ins concrete so fixture teardown can restore them."""

    def __getattr__(self, name):
        value = super().__getattr__(name)
        if not name.startswith("_"):
            self.__dict__[name] = value
        return value


for mod in ("anki", "anki.cards", "aqt", "aqt.utils", "aqt.qt", "aqt.operations", "aqt.gui_hooks"):
    sys.modules.setdefault(mod, ModuleMock())
for mod_name in (
    "incremento.backend", "incremento.backend.cards", "incremento.backend.statistics",
    "incremento.backend.session", "incremento.frontend", "incremento.frontend.stats_dialog",
):
    sys.modules.setdefault(mod_name, ModuleMock())
sys.modules.setdefault("incremento", types.ModuleType("incremento"))

for path in (os.path.join(INCREMENTO, "frontend"), os.path.join(INCREMENTO, "backend"), os.path.join(ADDON, "engine")):
    if path not in sys.path:
        sys.path.insert(0, path)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import paths as _paths  # noqa: E402

_paths.set_active_profile("TestProfile")

# Incremento's own releases do not have the sync tables (the stack's migration 27 was never merged): the engine makes them
# itself when it is switched on, and the tests that use the database directly get them the same way.
import db as _db  # noqa: E402
import sync_schema as _sync_schema  # noqa: E402

_original_get_connection = _db.get_connection


def _get_connection_with_sync_tables(*args, **kwargs):
    conn = _original_get_connection(*args, **kwargs)
    _sync_schema.ensure_schema(conn)
    return conn


_db.get_connection = _get_connection_with_sync_tables

import db_schema as _db_schema  # noqa: E402

_original_initialize_schema = _db_schema.initialize_schema


def _initialize_schema_with_sync_tables(conn, *args, **kwargs):
    result = _original_initialize_schema(conn, *args, **kwargs)
    _sync_schema.ensure_schema(conn)
    return result


_db_schema.initialize_schema = _initialize_schema_with_sync_tables

import ssl  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _real_ssl_context():
    """Some developer environments (pip-system-certs) replace ssl.SSLContext with a wrapper that drops TLS
    pre-shared-key callbacks, which the sync hub relies on."""
    if ssl.SSLContext.__module__ != "ssl":
        try:
            from pip._vendor import truststore

            truststore.extract_from_ssl()
        except Exception:
            pass
