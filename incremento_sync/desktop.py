"""The desktop side: a menu entry, the sync window, the timer and the Anki hooks.

All of it is registered when the engine is switched on and removed again when it is switched off, so a
user who turns the add-on off (or removes it) has Incremento's menu and hooks exactly as before.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from aqt import gui_hooks, mw
from aqt.qt import QAction, QTimer, qconnect
from aqt.utils import askUser, tooltip

from . import host, loader
from .i18n import t

_SYNC_INTERVAL_MS = 15 * 60 * 1000
_SYNC_AFTER_OPEN_MS = 20 * 1000
_PHONE_WATCH_MS = 30 * 1000
#: While phones are served, this computer also sends its own changes this often, so a phone that syncs finds them.
_PHONE_SEND_SECONDS = 5 * 60
_MENU_OBJECT = "incremento_menu"

_incremento = None
_addon_dir = ""
_timer: QTimer | None = None
_generation = 0
_phone = None  # the PhoneServer of the open profile, while phone sync is on
_phone_profile = ""
_phone_timer: QTimer | None = None
_phone_seen: float | None = None
_phone_sent_at = 0.0  # time.monotonic() of the last sync started for the phones
_syncing = 0  # background syncs running
_last_error: str | None = None  # why the last background sync failed, until one succeeds
_progress = None  # sync_service.SyncProgress: how far the running sync is with files


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
    loader.on_disable(_stop_phone)
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
        _stop_phone()
        _service().disable(_addon_dir, self.profile)

    # -- Sync with a phone: this computer is the server --

    def phone_unavailable(self) -> str | None:
        """Why this Anki cannot serve phones, or None."""
        return loader.module("sync_phone").supported()

    def start_phone(self) -> None:
        """Sync this profile through its own phone folder and start serving it."""
        phone = loader.module("sync_phone")
        phone.configure(_addon_dir, self.profile, query=mw.col.db.all)
        server = _phone_server(self.profile)
        server.start()
        server.new_code()
        _start_phone_watch()

    def phone(self) -> PhoneInfo:
        server = _phone if _phone_profile == self.profile else None
        if server is None or not server.running:
            return PhoneInfo(False, None, 0, "", [])
        code = server.code()
        return PhoneInfo(True, code[0] if code else None, code[1] if code else 0, server.address(), server.phones(),
                         server.tailscale())

    def activity(self) -> Activity:
        """Whether a sync is running now and how far it is with files, and why the last one failed (None when it did not)."""
        return Activity(_syncing > 0, _last_error, _progress if _syncing > 0 else None)

    def new_code(self) -> None:
        if _phone is not None and _phone_profile == self.profile:
            _phone.new_code()

    def cancel_code(self) -> None:
        if _phone is not None and _phone_profile == self.profile:
            _phone.cancel_code()

    def remove_phone(self, name: str) -> None:
        if _phone is not None and _phone_profile == self.profile:
            _phone.remove(name)

    def sync(self, done) -> None:
        start_background_sync(done=done)

    def discard_stale(self) -> None:
        _service().discard_stale(_addon_dir, self.profile)

    def resolve(self, conflict_id: int, value) -> None:
        _service().resolve(_addon_dir, self.profile, conflict_id, value, mw.col.db.all)


@dataclass(frozen=True)
class PhoneInfo:
    running: bool
    code: str | None  # "483 920" while one is showing
    seconds_left: int
    address: str
    phones: list  # sync_phone.Phone: name, seen, changed
    tailscale: str = ""  # this computer's Tailscale address while Tailscale runs


@dataclass(frozen=True)
class Activity:
    syncing: bool
    error: str | None
    progress: object = None  # sync_service.SyncProgress while files are prepared, sent or received


def _phone_server(profile: str):
    """The phone server of [profile], made on first use."""
    global _phone, _phone_profile
    if _phone is None or _phone_profile != profile:
        _stop_phone()
        _phone = loader.module("sync_phone").PhoneServer(_addon_dir, profile)
        _phone_profile = profile
    return _phone


def _stop_phone() -> None:
    global _phone, _phone_profile, _phone_timer, _phone_seen
    if _phone_timer is not None:
        _phone_timer.stop()
        _phone_timer = None
    server, _phone, _phone_profile, _phone_seen = _phone, None, "", None
    if server is not None:
        server.stop()


def _resume_phone() -> None:
    """A profile that syncs with a phone serves it again whenever it is open."""
    try:
        profile = _profile()
        label = _service().status(_addon_dir, profile).folder
        if not loader.module("sync_phone").is_phone_label(label) or loader.module("sync_phone").supported():
            return
        _phone_server(profile).start()
        _start_phone_watch()
    except Exception as error:
        tooltip(t("phone_server_failed", error=error))


def _start_phone_watch() -> None:
    """While phones are served, take in what one just sent (a phone's own sync is not heard by the timer)."""
    global _phone_timer, _phone_seen
    if _phone_timer is not None:
        _phone_timer.stop()
    _phone_seen = _phone.last_change() if _phone else None
    _phone_timer = QTimer(mw)
    _phone_timer.setInterval(_PHONE_WATCH_MS)
    _phone_timer.timeout.connect(_phone_changed)
    _phone_timer.start()


def _phone_changed() -> None:
    """Take in what a phone just sent at once, and send this computer's changes every few minutes."""
    global _phone_seen, _phone_sent_at
    if _phone is None:
        return
    changed = _phone.last_change()
    news = changed is not None and (_phone_seen is None or changed > _phone_seen)
    if news:
        _phone_seen = changed
    if news or time.monotonic() - _phone_sent_at >= _PHONE_SEND_SECONDS:
        _phone_sent_at = time.monotonic()
        start_background_sync()


def open_dialog() -> None:
    from .sync_dialog import SyncDialog

    SyncDialog(_profile_name(), _Controller(_profile()), mw).exec()


def remove_data() -> None:
    if not askUser(t("sync_remove_data_confirm")):
        return
    try:
        _cancel_running()
        _stop_phone()
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
        global _syncing, _progress
        _syncing = max(0, _syncing - 1)
        if not _syncing:
            _progress = None
        if done is not None:
            done()

    global _syncing
    _syncing += 1
    try:
        configured = mw.col is not None and _service().status(_addon_dir, profile).configured
    except Exception:
        configured = False
    if not configured:
        finish()
        return

    def run(col):
        return _service().sync_now(_addon_dir, profile, col.db.all, is_current=is_current, on_progress=_heard)

    def succeeded(report) -> None:
        global _last_error
        if report.skipped is None:
            _last_error = None
        if is_current() and report.conflicts:
            count = len(_service().open_conflicts(_addon_dir, profile))
            tooltip(t("root_sync_conflicts", count=count))
        finish()

    def failed(error: Exception) -> None:
        global _last_error
        _last_error = str(error) or type(error).__name__
        if is_current():
            tooltip(t("root_sync_failed", error=error))
        finish()

    QueryOp(parent=mw, op=run, success=succeeded).failure(failed).run_in_background()


def _heard(progress) -> None:
    """From the sync's worker thread: the window reads it on its next tick."""
    global _progress
    _progress = progress


def _start_schedule() -> None:
    global _timer, _generation
    _generation += 1
    _stop_timer()
    _timer = QTimer(mw)
    _timer.setInterval(_SYNC_INTERVAL_MS)
    _timer.timeout.connect(lambda: start_background_sync())
    _timer.start()
    _resume_phone()
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
    _stop_phone()
    try:
        profile = _profile()
        if mw.col is not None and _service().status(_addon_dir, profile).configured:
            _service().publish_now(_addon_dir, profile, mw.col.db.all)
    except Exception:
        pass
