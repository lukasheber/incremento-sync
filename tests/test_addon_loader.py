"""Switching the engine on and off in a running Incremento, and that off puts everything back."""

import importlib
import os
import sys
import types

import pytest

from incremento_sync import loader, patches

INCREMENTO = os.environ["INCREMENTO_DIR"]


@pytest.fixture
def incremento():
    """The real Incremento backend, imported as a package the way Anki loads an add-on."""
    name = "host_incremento"
    root = types.ModuleType(name)
    root.__path__ = [INCREMENTO]
    root.__file__ = os.path.join(INCREMENTO, "__init__.py")
    sys.modules[name] = root
    importlib.import_module(f"{name}.backend")
    yield root
    loader.disable()
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


def _skip(root):
    return importlib.import_module(f"{root.__name__}.backend.export_bundle").should_skip_user_file


def _backup_ops(root):
    return importlib.import_module(f"{root.__name__}.backend.mcp_backup_operations")


def test_the_engine_loads_into_incrementos_backend_when_enabled(incremento):
    assert not loader.is_enabled()
    loader.enable(incremento)

    assert loader.is_enabled()
    assert loader.ENGINE_DIR in incremento.backend.__path__
    assert loader.module("sync_service").__name__ == "host_incremento.backend.sync_service"


def test_enabled_backups_leave_out_the_sync_folder_everywhere_incremento_uses_it(incremento):
    original = _skip(incremento)
    assert original("sync/blobs/ab/cd") is False
    loader.enable(incremento)

    assert _skip(incremento)("sync/blobs/ab/cd") is True
    assert _backup_ops(incremento).should_skip_user_file("sync/x") is True
    assert _skip(incremento)("pdfs/book.pdf") is False  # Incremento's own rules still apply
    assert _skip(incremento)("bookshelf_cover_cache/x") is True


def test_disabled_incremento_is_exactly_as_it_was(incremento):
    skip_before = _skip(incremento)
    ops_before = _backup_ops(incremento).should_skip_user_file
    path_before = list(incremento.backend.__path__)
    loader.enable(incremento)
    loader.disable()

    assert not loader.is_enabled()
    assert _skip(incremento) is skip_before
    assert _backup_ops(incremento).should_skip_user_file is ops_before
    assert list(incremento.backend.__path__) == path_before
    assert not [name for name in sys.modules if name.startswith("host_incremento.backend.sync_")]
    assert not hasattr(incremento.backend, "sync_service")
    with pytest.raises(ImportError):
        importlib.import_module("host_incremento.backend.sync_service")


def test_it_can_be_switched_on_again_after_off(incremento):
    loader.enable(incremento)
    loader.disable()
    loader.enable(incremento)

    assert _skip(incremento)("sync/x") is True
    assert loader.module("sync_store")


def test_enabling_twice_changes_nothing_more(incremento):
    loader.enable(incremento)
    patched = _skip(incremento)
    loader.enable(incremento)

    assert _skip(incremento) is patched
    assert incremento.backend.__path__.count(loader.ENGINE_DIR) == 1


def test_what_the_host_registered_is_undone_last_in_first_out(incremento):
    order = []
    loader.enable(incremento)
    loader.on_disable(lambda: order.append("first"))
    loader.on_disable(lambda: order.append("second"))
    loader.disable()

    assert order == ["second", "first"]


def test_one_failing_undo_does_not_stop_the_rest(incremento):
    done = []
    loader.enable(incremento)
    loader.on_disable(lambda: done.append("after the failure"))
    loader.on_disable(lambda: 1 / 0)
    loader.disable()

    assert done == ["after the failure"] and not loader.is_enabled()


def test_disabling_when_not_enabled_is_harmless():
    loader.disable()
    assert not loader.is_enabled()


def test_an_incremento_that_cannot_carry_the_patches_stays_untouched(incremento, monkeypatch):
    def broken(pkg):
        raise AttributeError("should_skip_user_file is gone")

    monkeypatch.setattr(patches, "apply_all", broken)
    path_before = list(incremento.backend.__path__)

    with pytest.raises(loader.Unsupported):
        loader.enable(incremento)

    assert not loader.is_enabled()
    assert list(incremento.backend.__path__) == path_before
    assert not hasattr(incremento.backend, "sync_service")


def test_a_package_without_a_backend_is_unsupported():
    stranger = types.ModuleType("not_incremento")
    stranger.__path__ = []
    with pytest.raises(loader.Unsupported):
        loader.enable(stranger)
