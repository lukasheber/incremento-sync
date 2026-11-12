"""The desktop side: a menu entry, the sync window, the timer and the Anki hooks.

All of it is registered when the engine is switched on and removed again when it is switched off, so a
user who turns the add-on off (or removes it) has Incremento's menu and hooks exactly as before.
"""

from __future__ import annotations

import os

from aqt import gui_hooks, mw
from aqt.qt import QAction, QTimer, qconnect
from aqt.utils import askUser, tooltip

from . import host, loader
from .i18n import t

_SYNC_INTERVAL_MS = 15 * 60 * 1000
_SYNC_AFTER_OPEN_MS = 20 * 1000
_MENU_OBJECT = "incremento_menu"

_incremento = None
_addon_dir = ""
_timer: QTimer | None = None
_generation = 0


def _own_name() -> str:
    return __name__.split(".")[0]


def _config() -> dict:
    return mw.addonManager.getConfig(_own_name()) or {}


def start() -> None:
    """Runs once the main window is up (every add-on is loaded): switch on when the config says so."""
    mw.addonManager.setConfigUpdatedAction(_own_name(), lambda _config: apply_config())
    apply_config()


def apply_config() -> None:
    wanted = bool(_config().get("enabled", True))
    if wanted and not loader.is_enabled():
        enable()
    elif not wanted and loader.is_enabled():
        loader.disable()


def enable() -> None:
    global _incremento, _addon_dir
    found = host.find_incremento()
    if found is None:
        tooltip("Incremento Sync: the Incremento add-on is not installed.")
        return
    try:
        loader.enable(found)
    except loader.Unsupported as error:
        tooltip(f"Incremento Sync is off: {error}")
        return
    _incremento, _addon_dir = found, os.path.dirname(os.path.abspath(found.__file__))
    _add_menu()
    _hook(gui_hooks.profile_did_open, _start_schedule)
    _hook(gui_hooks.sync_did_finish, _after_ankiweb)
    _hook(gui_hooks.profile_will_close, _publish_on_close)
    _hook(gui_hooks.collection_will_temporarily_close, _cancel_running)
    loader.on_disable(_stop_timer)
    if mw.col is not None:  # switched on while a profile is open
        _start_schedule()


def _hook(hook, callback) -> None:
    hook.append(callback)
    loader.on_disable(lambda: hook.remove(callback) if callback in hook else None)


def _service():
    return loader.module("sync_service")


def _profile() -> str:
    return loader.module("paths").get_active_profile()


def _device_name() -> str:
    """This computer's name as a hub device name (letters, digits, dashes)."""
    import re
    import socket

    return re.sub(r"[^A-Za-z0-9_-]+", "-", socket.gethostname()).strip("-")[:60] or "desktop"


def _profile_name() -> str:
    getter = getattr(_incremento, "_current_profile_name", None)
    try:
        return str(getter()) if getter else _profile()
    except Exception:
        return _profile()


# -- Menu ---------------------------------------------------------------------


def _add_menu() -> None:
    menu = None
    for action in mw.menuBar().actions():
        if action.objectName() == _MENU_OBJECT:
            menu = action.menu()
    if menu is None:
        menu = mw.form.menuTools
    for label, handler in ((t("root_menu_sync"), open_dialog), (t("sync_remove_data"), remove_data)):
        action = QAction(label, mw)
        qconnect(action.triggered, handler)
        menu.addAction(action)
        loader.on_disable(lambda a=action, m=menu: m.removeAction(a))


class _Controller:
    """What the sync window may do, bound to the profile it was opened for."""

    def __init__(self, profile: str):
        self.profile = profile

    def status(self):
        return _service().status(_addon_dir, self.profile)

    def conflicts(self):
        return _service().open_conflicts(_addon_dir, self.profile)

    def configure(self, folder: str) -> None:
        _service().configure(_addon_dir, self.profile, folder, query=mw.col.db.all)

    def configure_hub(self, code: str) -> None:
        # A six digit code the hub shows (found on the network), or a long pairing code.
        pairing = loader.module("sync_http").resolve_pairing(code, _device_name())
        _service().configure_hub(_addon_dir, self.profile, pairing.code(), query=mw.col.db.all)

    def disable(self) -> None:
        _service().disable(_addon_dir, self.profile)

    def sync(self, done) -> None:
        start_background_sync(done=done)

    def discard_stale(self) -> None:
        _service().discard_stale(_addon_dir, self.profile)

    def resolve(self, conflict_id: int, value) -> None:
        _service().resolve(_addon_dir, self.profile, conflict_id, value, mw.col.db.all)


def open_dialog() -> None:
    from .sync_dialog import SyncDialog

    SyncDialog(_profile_name(), _Controller(_profile()), mw).exec()


def remove_data() -> None:
    if not askUser(t("sync_remove_data_confirm")):
        return
    try:
        _cancel_running()
        loader.remove_data(_addon_dir, _profile())
    except Exception as error:
        tooltip(t("root_sync_failed", error=error))
        return
    tooltip(t("sync_remove_data_done"))


# -- Running it ---------------------------------------------------------------


def start_background_sync(done=None) -> None:
    """Sync the active profile on the collection executor; results of a closed profile are dropped."""
    from aqt.operations import QueryOp

    profile = _profile()
    generation = _generation

    def is_current() -> bool:
        return generation == _generation and _profile() == profile

    def finish() -> None:
        if done is not None:
            done()

    try:
        configured = mw.col is not None and _service().status(_addon_dir, profile).configured
    except Exception:
        configured = False
    if not configured:
        finish()
        return

    def run(col):
        return _service().sync_now(_addon_dir, profile, col.db.all, is_current=is_current)

    def succeeded(report) -> None:
        if is_current() and report.conflicts:
            count = len(_service().open_conflicts(_addon_dir, profile))
            tooltip(t("root_sync_conflicts", count=count))
        finish()

    def failed(error: Exception) -> None:
        if is_current():
            tooltip(t("root_sync_failed", error=error))
        finish()

    QueryOp(parent=mw, op=run, success=succeeded).failure(failed).run_in_background()


def _start_schedule() -> None:
    global _timer, _generation
    _generation += 1
    _stop_timer()
    _timer = QTimer(mw)
    _timer.setInterval(_SYNC_INTERVAL_MS)
    _timer.timeout.connect(lambda: start_background_sync())
    _timer.start()
    # Give Anki's own sync on open a head start: cards it delivers let more Incremento changes apply.
    QTimer.singleShot(_SYNC_AFTER_OPEN_MS, lambda: start_background_sync() if loader.is_enabled() else None)


def _stop_timer() -> None:
    global _timer
    if _timer is not None:
        _timer.stop()
        _timer = None


def _after_ankiweb() -> None:
    start_background_sync()


def _cancel_running(*_args) -> None:
    """A full sync replaces the collection: drop the results of a sync started before it."""
    global _generation
    _generation += 1


def _publish_on_close() -> None:
    """Capture and publish what changed before the profile closes; never block closing."""
    _cancel_running()
    _stop_timer()
    try:
        profile = _profile()
        if mw.col is not None and _service().status(_addon_dir, profile).configured:
            _service().publish_now(_addon_dir, profile, mw.col.db.all)
    except Exception:
        pass
