"""Sync with a phone: this computer is the server (design: docs/sync-hub.md).

The profile's sync folder is a plain folder inside the profile's ``sync`` directory. The desktop syncs against it
directly; a ``Hub`` serves the same folder to the paired phones. The hub lives only while Anki is open.

The profile's location label is ``phone:<folder>`` so the window can tell this mode from a cloud folder.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from . import sync_paths, sync_service
    from .sync_http import DEFAULT_PORT, DISCOVERY_PORT, PAIRING_SECONDS, Hub, KeyBook, StoreError, format_pin, require_psk
    from .sync_store import LocalStore
except ImportError:
    import sync_paths  # type: ignore[no-redef]
    import sync_service  # type: ignore[no-redef]
    from sync_http import (  # type: ignore[no-redef]
        DEFAULT_PORT, DISCOVERY_PORT, PAIRING_SECONDS, Hub, KeyBook, StoreError, format_pin, require_psk,
    )
    from sync_store import LocalStore  # type: ignore[no-redef]

LABEL_PREFIX = "phone:"
FOLDER_NAME = "phone-hub"
KEYS_NAME = "phone-keys.json"
OWNERS_NAME = "phone-owners.json"
ACTIVITY_NAME = "phone-activity.json"


def is_phone_label(label: str | None) -> bool:
    return bool(label) and label.startswith(LABEL_PREFIX)


def folder_of(label: str) -> Path:
    return Path(label[len(LABEL_PREFIX):])


def hub_folder(addon_dir: str, profile: str) -> Path:
    return sync_paths.get_sync_dir(addon_dir, profile) / FOLDER_NAME


def own_address() -> str:
    """This computer's address on the network, as a phone would reach it ("" if it cannot tell)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; the system only picks the interface it would use
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def supported() -> str | None:
    """None when this Python can serve phones, else the reason it cannot."""
    try:
        require_psk()
    except StoreError as error:
        return str(error)
    return None


def configure(addon_dir: str, profile: str, *, query=None) -> sync_service.SetupResult:
    """Make this profile sync through its own phone folder (the server is started by the caller)."""
    folder = hub_folder(addon_dir, profile)
    folder.mkdir(parents=True, exist_ok=True)
    return sync_service.configure(addon_dir, profile, LABEL_PREFIX + str(folder), query=query,
                                  store=LocalStore(folder, share_files=True))


class PhoneServer:
    """The hub of one profile: start it, show a code, list and remove the phones."""

    def __init__(self, addon_dir: str, profile: str, *, host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 discovery_port: int | None = DISCOVERY_PORT):
        sync_dir = sync_paths.get_sync_dir(addon_dir, profile)
        self._addon_dir, self._profile = addon_dir, profile
        self.folder = hub_folder(addon_dir, profile)
        self.book = KeyBook(sync_dir / KEYS_NAME)
        self._owners = sync_dir / OWNERS_NAME
        self._host, self._port, self._discovery = host, port, discovery_port
        self._hub: Hub | None = None
        self._expires = 0.0
        self._pin = ""
        self._lock = threading.Lock()
        self._activity_path = sync_dir / ACTIVITY_NAME
        self._known = self._load_activity()

    @property
    def running(self) -> bool:
        return self._hub is not None

    @property
    def port(self) -> int:
        return self._hub.port if self._hub is not None else self._port

    def start(self) -> None:
        with self._lock:
            if self._hub is not None:
                return
            self.folder.mkdir(parents=True, exist_ok=True)
            hub = Hub(self.folder, self.book.as_mapping(), host=self._host, port=self._port,
                      state_path=self._owners, discovery_port=self._discovery, key_book=self.book)
            try:
                hub.start()
            except OSError:
                if self._port == 0:
                    raise
                hub = Hub(self.folder, self.book.as_mapping(), host=self._host, port=0, state_path=self._owners,
                          discovery_port=self._discovery, key_book=self.book)  # the usual port is taken
                hub.start()
            self._hub = hub

    def stop(self) -> None:
        if self._hub is not None:
            self._save_activity()
        with self._lock:
            hub, self._hub = self._hub, None
            self._pin, self._expires = "", 0.0
        if hub is not None:
            hub.stop()

    def new_code(self) -> str:
        """A fresh six digit code (formatted ``483 920``) for one phone; replaces one already showing."""
        hub = self._hub
        if hub is None:
            raise RuntimeError("the phone server is not running")
        pin = hub.open_pairing()
        self._pin, self._expires = pin, time.monotonic() + PAIRING_SECONDS
        return format_pin(pin)

    def cancel_code(self) -> None:
        if self._hub is not None:
            self._hub.close_pairing()

    def code(self) -> tuple[str, int] | None:
        """The code showing now and the seconds it stays good for, or None when none is open."""
        hub = self._hub
        if hub is None or not hub.pairing_open:
            return None
        return format_pin(self._pin), max(0, int(self._expires - time.monotonic()))

    def address(self) -> str:
        """``192.168.1.20`` or ``192.168.1.20:8800``: what a phone types after the comma if it cannot find us."""
        host = own_address() or "<this computer>"
        return host if self.port == DEFAULT_PORT else f"{host}:{self.port}"

    def phones(self) -> list[Phone]:
        """The paired phones, with when each last synced and last sent a change (also from before a restart), and how
        far each is with this computer's changes (None when the phone does not say)."""
        seen = self._activity()
        progress = self._progress()
        phones = []
        for name in sorted(self.book.devices()):
            mine = progress.get(name)
            phones.append(Phone(name, *seen.get(name, (None, None)), behind=mine.behind if mine else None,
                                files_waiting=mine.files_waiting if mine else None))
        return phones

    def _progress(self) -> dict[str, sync_service.PeerProgress]:
        """Each phone's announced progress, by the name it was paired as (the hub knows which device folder is whose)."""
        try:
            owners = json.loads(self._owners.read_text(encoding="utf-8"))["owners"]
            by_device = sync_service.peer_progress(self._addon_dir, self._profile, store=LocalStore(self.folder))
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        progress: dict[str, sync_service.PeerProgress] = {}
        for device, mine in by_device.items():
            name = owners.get(device) if isinstance(owners, dict) else None
            if isinstance(name, str):
                progress[name] = mine  # a phone paired again has one folder per pairing: the last one listed wins
        return progress

    def remove(self, name: str) -> bool:
        removed = self.book.revoke(name)
        with self._lock:
            self._known.pop(name, None)
        return removed

    def last_change(self) -> float | None:
        """When a phone last sent a change."""
        return max((phone.changed for phone in self.phones() if phone.changed), default=None)

    def _activity(self) -> dict[str, tuple[float | None, float | None]]:
        with self._lock:
            known = dict(self._known)
            hub = self._hub
        if hub is not None:
            for name, (seen, changed) in hub.activity().items():
                before = known.get(name, (None, None))
                known[name] = (seen, changed if changed is not None else before[1])
        return known

    def _load_activity(self) -> dict[str, tuple[float | None, float | None]]:
        try:
            raw = json.loads(self._activity_path.read_text(encoding="utf-8"))
            return {str(name): (_stamp(times.get("seen")), _stamp(times.get("changed")))
                    for name, times in raw["phones"].items()}
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            return {}

    def _save_activity(self) -> None:
        known = self._activity()
        try:
            self._activity_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._activity_path.with_name(self._activity_path.name + ".tmp")
            temporary.write_text(json.dumps({"version": 1, "phones": {
                name: {"seen": seen, "changed": changed} for name, (seen, changed) in known.items()}}), encoding="utf-8")
            temporary.replace(self._activity_path)
        except OSError:
            pass  # only the "last synced" times are lost
        with self._lock:
            self._known = known


def _stamp(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class Phone:
    name: str
    #: When it last synced (any request), and when it last sent a change; None if never heard from.
    seen: float | None
    changed: float | None
    #: This computer's revisions it has not placed yet, and files it still waits for; None when it does not say.
    behind: int | None = None
    files_waiting: int | None = None
