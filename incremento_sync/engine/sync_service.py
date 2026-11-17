"""What the desktop calls: set up, run, inspect and settle shared-folder sync for a profile.

See docs/architecture/shared-folder-sync.md §7 (order of operations) and §8.

The chosen folder is stored per profile in ``sync_meta``, beside the
library and device identity, rather than in the add-on-wide config. One sync
runs at a time per profile; a sync checks between steps that its profile is
still the current one and stops otherwise. ``query`` has the shape of Anki's
``col.db.all`` and is only read from.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from . import paths as _paths, sync_paths
    from .db import get_connection as _host_connection
    from .sync_schema import ensure_schema
    from .sync_anki import Query, card_refs, collection_scm, collection_usn
    from .sync_apply import _update_receipt, apply, resolve_conflict
    from .sync_capture import adopt_new_device_identity, capture, enable_sync, sync_identity
    from .sync_documents import DocumentFamily
    from .sync_files import LocalFileFamily
    from .sync_videos import VideoFileFamily
    from .sync_links import LINK_FAMILIES
    from .sync_blobs import Budget, blob_path, fetch_exchange_blob
    from .sync_exchange import (
        EXCHANGE_DIR, announce, library_rel, missing_blobs, publish, receive, release_waiting, sample_card_refs,
        unsent_blobs,
    )
    from .sync_snapshot import catch_up, history_complete, retire_packs, sweep_blobs, write_snapshot_if_due
    from .sync_http import HttpStore, Pairing
    from .sync_store import LocalStore, Store, StoreError, as_store
    from .sync_records import FAMILIES, Family
except ImportError:
    import paths as _paths  # type: ignore[no-redef]
    import sync_paths  # type: ignore[no-redef]
    from db import get_connection as _host_connection  # type: ignore[no-redef]
    from sync_schema import ensure_schema  # type: ignore[no-redef]
    from sync_anki import Query, card_refs, collection_scm, collection_usn  # type: ignore[no-redef]
    from sync_apply import _update_receipt, apply, resolve_conflict  # type: ignore[no-redef]
    from sync_capture import (  # type: ignore[no-redef]
        adopt_new_device_identity, capture, enable_sync, sync_identity,
    )
    from sync_documents import DocumentFamily  # type: ignore[no-redef]
    from sync_files import LocalFileFamily  # type: ignore[no-redef]
    from sync_videos import VideoFileFamily  # type: ignore[no-redef]
    from sync_links import LINK_FAMILIES  # type: ignore[no-redef]
    from sync_blobs import Budget, blob_path, fetch_exchange_blob  # type: ignore[no-redef]
    from sync_exchange import (  # type: ignore[no-redef]
        EXCHANGE_DIR, announce, library_rel, missing_blobs, publish, receive, release_waiting, sample_card_refs,
        unsent_blobs,
    )
    from sync_snapshot import (  # type: ignore[no-redef]
        catch_up, history_complete, retire_packs, sweep_blobs, write_snapshot_if_due,
    )
    from sync_http import HttpStore, Pairing  # type: ignore[no-redef]
    from sync_store import LocalStore, Store, StoreError, as_store  # type: ignore[no-redef]
    from sync_records import FAMILIES, Family  # type: ignore[no-redef]


#: Write a snapshot after this many revisions published or integrated since the last one ...
SNAPSHOT_EVERY_REVISIONS = 1000
#: ... or after this long, if anything changed at all.
SNAPSHOT_MAX_AGE_SECONDS = 7 * 86400
#: Unreferenced exchange blobs are swept once older than this, at most once per interval.
BLOB_GRACE_SECONDS = 30 * 86400
SWEEP_INTERVAL_SECONDS = 86400
#: Changes waiting this long for a card, file or parent may be discarded by the user.
STALE_WAITING_SECONDS = 30 * 86400
_WAITING = ("pending_anki", "pending_blob", "pending_parent")


class SyncSetupError(RuntimeError):
    """The chosen folder cannot be used; the message says why."""


class SyncLocationError(SyncSetupError):
    """The folder or hub could not be read at all (unreachable, or it refused this device)."""


@dataclass(frozen=True)
class SetupResult:
    library_id: str
    device_id: str
    created: bool


@dataclass
class SyncReport:
    skipped: str | None = None
    captured: int = 0
    published: int = 0
    received: int = 0
    applied: int = 0
    conflicts: int = 0
    problems: list[str] = field(default_factory=list)
    #: File transfer was cut short by the time budget: call again to continue.
    more: bool = False
    #: Files still to place in the sync location, their size, and files still to fetch from it, after this call.
    files_to_send: int = 0
    bytes_to_send: int = 0
    files_to_receive: int = 0


@dataclass(frozen=True)
class SyncProgress:
    """How far a sync is with files: ``stage`` is "preparing" (copying this device's files for sync), "sending" or
    "receiving"; ``done`` of ``total`` files in this call."""
    stage: str
    done: int
    total: int


@dataclass(frozen=True)
class SyncStatus:
    configured: bool
    folder: str | None
    last_sync: int | None
    conflicts: int
    waiting_for_anki: int
    waiting_for_files: int
    waiting_for_parents: int
    #: Changes that have waited longer than STALE_WAITING_SECONDS.
    stale_waiting: int = 0
    #: When this computer last put changes of its own in the sync location (None: never).
    last_published: int | None = None
    #: Files waiting to be placed in the sync location, their size, and files waiting to be fetched from it.
    files_to_send: int = 0
    bytes_to_send: int = 0
    files_to_receive: int = 0


@dataclass(frozen=True)
class Conflict:
    id: int
    family: str
    key: str
    heads: list[str]
    values: list[dict[str, Any] | None]
    #: Which of ``values`` this computer has now.
    local_index: int


_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()
_document_families: dict[tuple[str, str], DocumentFamily] = {}
_file_families: dict[tuple[str, str], LocalFileFamily] = {}
_video_families: dict[tuple[str, str], VideoFileFamily] = {}


def families_for(addon_dir: str, profile: str) -> dict[str, Family]:
    with _locks_guard:
        documents = _document_families.setdefault((addon_dir, profile), DocumentFamily(addon_dir, profile))
        files = _file_families.setdefault((addon_dir, profile), LocalFileFamily(addon_dir, profile))
        videos = _video_families.setdefault((addon_dir, profile), VideoFileFamily(addon_dir, profile))
    return {**FAMILIES, **LINK_FAMILIES, "document": documents, "localfile": files, "video": videos}


def _meta(conn: Any, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _set_meta(conn: Any, key: str, value: str | None) -> None:
    with conn:
        if value is None:
            conn.execute("DELETE FROM sync_meta WHERE key=?", (key,))
        else:
            conn.execute("INSERT OR REPLACE INTO sync_meta(key, value) VALUES (?, ?)", (key, value))


def get_connection(addon_dir: str, profile: str) -> Any:
    """The profile's database with the sync tables in place, whether or not the host's migrations made them."""
    conn = _host_connection(addon_dir, profile)
    ensure_schema(conn)
    return conn


def libraries_in(root: Path | str | Store) -> list[str]:
    """Library ids found in an exchange folder (each with a matching library.json)."""
    store = as_store(root)
    found: list[str] = []
    entries = sorted(store.listdir("/".join(EXCHANGE_DIR)), key=lambda entry: entry.name)  # StoreError: not empty, unreadable
    for entry in entries:
        if entry.is_symlink or not entry.is_dir:
            continue
        rel = "/".join((*EXCHANGE_DIR, entry.name, "library.json"))
        descriptor = store.stat(rel)
        if descriptor is None or descriptor.is_dir or descriptor.size > 4096:
            continue
        try:
            data = store.read(rel, 4096)
            if data is not None and json.loads(data.decode("utf-8")).get("library_id") == entry.name:
                found.append(entry.name)
        except (ValueError, OSError):
            continue
    return found


def _check_same_collection(folder: Path | Store, library_id: str, query: Query) -> None:
    """Refuse to join a library whose cards belong to another Anki collection."""
    sample = sample_card_refs(folder, library_id)
    if not sample:
        return
    present = card_refs(query, sample)
    if any(ref.guid != sample[card_id] for card_id, ref in present.items()):
        raise SyncSetupError("This folder's Incremento library belongs to another Anki collection.")
    if not present:
        raise SyncSetupError("None of this library's cards are in this Anki collection yet. "
                             "Sync with AnkiWeb first, then choose the folder again.")


def configure(addon_dir: str, profile: str, folder: str, *, query: Query | None = None,
              store: Store | None = None) -> SetupResult:
    """Use ``folder`` for this profile: join the library in it, or start one.

    With ``query`` (the collection's ``col.db.all``), joining an existing
    library first checks that its cards are this collection's. ``folder`` is a
    directory, or, when a ``store`` is given (Android's document provider, a
    LAN hub), only the label under which that store is remembered.
    """
    if store is None:
        path = Path(folder)
        if not path.is_dir():
            raise SyncSetupError(f"The folder {folder} does not exist.")
        store = LocalStore(path)
    conn = get_connection(addon_dir, profile)
    try:
        libraries = libraries_in(store)
    except (StoreError, OSError) as error:  # unreachable is not empty: no library is invented for it
        raise SyncLocationError(f"The sync location cannot be read: {error}") from error
    identity = sync_identity(conn)
    created = False
    if identity is not None:
        if ensure_device_binding(addon_dir, profile):
            identity = sync_identity(conn)
            assert identity is not None
        if libraries and identity.library_id not in libraries:
            raise SyncSetupError("This folder holds another Incremento library than this profile's.")
    elif len(libraries) > 1:
        raise SyncSetupError("This folder holds several Incremento libraries; choose a folder with one.")
    else:
        if libraries and query is not None:
            _check_same_collection(store, libraries[0], query)
        identity = enable_sync(conn, library_id=libraries[0] if libraries else None)
        created = not libraries
    _bind(addon_dir, profile, identity.device_id)
    _set_meta(conn, "folder", str(folder))
    publish(conn, store)  # announces the library and this device
    return SetupResult(identity.library_id, identity.device_id, created)


def _binding_path(addon_dir: str, profile: str) -> Path:
    """Which device identity this installation owns. Lives beside the blob store, outside backups."""
    return sync_paths.get_sync_dir(addon_dir, profile) / "device-binding.json"


def _bind(addon_dir: str, profile: str, device_id: str) -> None:
    path = _binding_path(addon_dir, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"device_id": device_id}), encoding="utf-8")
    temporary.replace(path)


def ensure_device_binding(addon_dir: str, profile: str) -> bool:
    """Take a new device identity if the database came from a backup or another computer.

    The binding file is not part of backups, so a restored or copied database
    no longer matches it. Returns whether a new identity was adopted.
    """
    conn = get_connection(addon_dir, profile)
    identity = sync_identity(conn)
    if identity is None:
        return False
    try:
        bound = json.loads(_binding_path(addon_dir, profile).read_text("utf-8")).get("device_id")
    except (OSError, ValueError, AttributeError):
        bound = None
    if bound == identity.device_id:
        return False
    _bind(addon_dir, profile, adopt_new_device_identity(conn).device_id)
    return True


def disable(addon_dir: str, profile: str) -> None:
    """Stop syncing this profile. Its identity stays, so it can resume where it left off."""
    _set_meta(get_connection(addon_dir, profile), "folder", None)


def _lock(addon_dir: str, profile: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((addon_dir, profile), threading.Lock())


def _run(addon_dir: str, profile: str, folder: Store, query: Query, is_current: Callable[[], bool],
         now: Callable[[], float], *, publish_only: bool = False, budget: Budget | None = None,
         progress: Callable[[str, int, int], None] | None = None) -> SyncReport:
    conn = get_connection(addon_dir, profile)
    families = families_for(addon_dir, profile)
    # Local file cards exist only as Anki notes: this family reads them through the run's collection.
    families["localfile"].query = query  # type: ignore[attr-defined]
    families["video"].query = query  # type: ignore[attr-defined]
    store = sync_paths.get_sync_blob_dir(addon_dir, profile)
    usn, scm = collection_usn(query), collection_scm(query)
    state = f"{scm}:{query('SELECT mod FROM col')[0][0]}"

    def resolver(card_ids: Any) -> Any:
        return card_refs(query, card_ids)

    report = SyncReport()
    steps: list[Callable[[], None]] = [
        lambda: setattr(report, "captured", report.captured + capture(conn, families, resolver, now=now, collection_state=state,
                                                                    progress=progress).captured),
        lambda: setattr(report, "published", report.published + publish(
            conn, folder, anki_usn=usn, anki_scm=scm, blob_store=store, now=now, budget=budget, progress=progress).revisions_published),
        lambda: _catch_up(conn, folder, families, now),
        lambda: _receive(report, receive(conn, folder, families, blob_store=store, now=now, budget=budget,
                                                progress=progress)),
        lambda: _apply(report, apply(conn, families, resolver, collection_usn=usn, collection_scm=scm, blob_store=store,
                                    now=now)),
        # Parents that lived only in retired packs can no longer arrive: judge their children now.
        lambda: _release_retired(report, conn, folder, store, lambda: apply(
            conn, families, resolver, collection_usn=usn, collection_scm=scm, blob_store=store, now=now), now),
        # Local edits protected during apply are published in the same run.
        lambda: setattr(report, "captured", report.captured + capture(conn, families, resolver, now=now, collection_state=state,
                                                                    progress=progress).captured),
        lambda: setattr(report, "published", report.published + publish(
            conn, folder, anki_usn=usn, anki_scm=scm, blob_store=store, now=now, budget=budget, progress=progress).revisions_published),
    ]
    steps.append(lambda: _maintain(conn, folder, usn, scm, now, store, budget))
    if publish_only:
        steps = steps[:2]
    for step in steps:
        if not is_current():
            report.skipped = "profile changed"
            return report
        step()
    if budget is not None and budget.more:
        report.more = True
    report.files_to_send, report.bytes_to_send, report.files_to_receive = _backlog(conn, store)
    if not publish_only and not report.more:
        _set_meta(conn, "last_sync", str(int(now())))
    return report


def _backlog(conn: Any, blob_store: Path) -> tuple[int, int, int]:
    """Files still to send, their size in bytes, and files still to receive."""
    identity = sync_identity(conn)
    if identity is None:
        return 0, 0, 0
    unsent = unsent_blobs(conn, identity.device_id)
    size = 0
    for sha in unsent:
        try:
            size += blob_path(blob_store, sha).stat().st_size
        except (OSError, ValueError):
            pass
    return len(unsent), size, len(missing_blobs(conn, blob_store))


def _catch_up(conn: Any, folder: Store, families: dict[str, Family], now: Callable[[], float]) -> None:
    catch_up(conn, folder, families, now=now())


def _release_retired(report: SyncReport, conn: Any, folder: Store, store: Path, apply_again: Callable[[], Any],
                     now: Callable[[], float]) -> None:
    if history_complete(conn, folder) and release_waiting(conn, int(now()), blob_store=store,
                                                          parents_may_be_retired=True):
        _apply(report, apply_again())


def _maintain(conn: Any, folder: Store, usn: int, scm: int, now: Callable[[], float],
              blob_store: Path | None = None, budget: Budget | None = None) -> None:
    """Snapshot when due, retire covered packs, announce progress, and now and then sweep blobs."""
    identity = sync_identity(conn)
    assert identity is not None
    timestamp = now()
    write_snapshot_if_due(conn, folder, every=SNAPSHOT_EVERY_REVISIONS, max_age=SNAPSHOT_MAX_AGE_SECONDS,
                          now=timestamp, blob_store=blob_store, budget=budget)
    retire_packs(conn, folder)
    announce(conn, folder, anki_usn=usn, anki_scm=scm, blob_store=blob_store)
    swept_at = float(_meta(conn, "swept_at") or 0)
    if timestamp - swept_at >= SWEEP_INTERVAL_SECONDS:
        sweep_blobs(folder, identity.library_id, now=timestamp, grace_seconds=BLOB_GRACE_SECONDS)
        _set_meta(conn, "swept_at", str(timestamp))


HUB_FILE = "hub.json"


def hub_label(pairing: Pairing) -> str:
    """How a hub is named in the profile: its device and a hash of the key, never the key."""
    return f"hub:{pairing.identity}:{hashlib.sha256(pairing.key).hexdigest()[:8]}"


def _hub_path(addon_dir: str, profile: str) -> Path:
    return sync_paths.get_sync_dir(addon_dir, profile) / HUB_FILE


def store_for(addon_dir: str, profile: str, label: str) -> Store:
    """The store a profile was configured with: a hub paired earlier, this computer's own phone folder, or a folder."""
    if label.startswith("phone:"):
        return LocalStore(Path(label[len("phone:"):]))
    if not label.startswith("hub:"):
        return LocalStore(Path(label))
    try:
        pairing = Pairing.from_code(json.loads(_hub_path(addon_dir, profile).read_text(encoding="utf-8"))["code"])
        if hub_label(pairing) != label:
            raise ValueError("another hub")
    except (OSError, ValueError, KeyError, TypeError):
        raise SyncSetupError("This profile syncs through a hub, but its pairing is missing. "
                             "Pair with the hub again.") from None
    return HttpStore.from_pairing(pairing)


def configure_hub(addon_dir: str, profile: str, code: str, *, query: Query | None = None) -> SetupResult:
    """Sync this profile through the hub a pairing code names (printed by scripts/sync_hub.py)."""
    try:
        pairing = Pairing.from_code(code)
    except ValueError as error:
        raise SyncSetupError(str(error)) from None
    store = HttpStore.from_pairing(pairing)
    try:
        result = configure(addon_dir, profile, hub_label(pairing), query=query, store=store)
    except SyncLocationError as error:
        raise SyncSetupError(f"The hub cannot be used: {error.__cause__}") from error
    except (StoreError, OSError) as error:
        raise SyncSetupError(f"The hub cannot be used: {error}") from error
    finally:
        store.close()
    path = _hub_path(addon_dir, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".hub-", suffix=".tmp")  # private to the owner
    try:
        with open(handle, "w", encoding="utf-8") as out:
            json.dump({"code": code.strip()}, out)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)
    return result


def sweep_blobs_now(folder: Path | Store, library_id: str, *, now: float) -> int:
    return sweep_blobs(folder, library_id, now=now, grace_seconds=BLOB_GRACE_SECONDS)


def _receive(report: SyncReport, result: Any) -> None:
    report.received += result.received
    report.problems += result.problems


def _apply(report: SyncReport, result: Any) -> None:
    report.applied += result.applied + result.merged
    report.conflicts += result.conflicts


def sync_now(addon_dir: str, profile: str, query: Query, *, is_current: Callable[[], bool] = lambda: True,
             now: Callable[[], float] = time.time, publish_only: bool = False,
             store: Store | None = None, budget_seconds: float | None = None,
             on_progress: Callable[[SyncProgress], None] | None = None) -> SyncReport:
    """Capture, publish, receive and apply. Safe to call from a background worker.

    ``on_progress`` hears a ``SyncProgress`` as files are prepared, sent and received, on the worker's thread; a
    listener that fails is ignored, so a closed window cannot stop a sync.
    """
    conn = get_connection(addon_dir, profile)
    folder = _meta(conn, "folder") if sync_identity(conn) is not None else None
    if folder is None:
        return SyncReport(skipped="not configured")
    lock = _lock(addon_dir, profile)
    if not lock.acquire(blocking=False):
        return SyncReport(skipped="already running")
    try:
        ensure_device_binding(addon_dir, profile)
        owned = store is None
        target = store if store is not None else store_for(addon_dir, profile, folder)
        try:
            return _run(addon_dir, profile, target, query, is_current, now, publish_only=publish_only,
                        budget=Budget(budget_seconds), progress=_listener(on_progress))
        finally:
            if owned and hasattr(target, "close"):
                target.close()
    finally:
        lock.release()


def _listener(on_progress: Callable[[SyncProgress], None] | None) -> Callable[[str, int, int], None] | None:
    if on_progress is None:
        return None

    def hear(stage: str, done: int, total: int) -> None:
        try:
            on_progress(SyncProgress(stage, done, total))
        except Exception:
            pass

    return hear


@dataclass(frozen=True)
class PeerProgress:
    #: Revisions of this device the other one has not placed yet, and files it still waits for.
    behind: int
    files_waiting: int


def peer_progress(addon_dir: str, profile: str, *, store: Store | None = None) -> dict[str, PeerProgress]:
    """How far each other device is with this one's changes, from what it announced (its device.json).

    A device has all of this one's changes when it has placed every revision published so far (``behind`` 0) and no
    file is still on its way to it (``files_waiting`` 0). Devices that do not announce it (older versions) are left
    out, so the caller can fall back to when they last synced.
    """
    conn = get_connection(addon_dir, profile)
    identity = sync_identity(conn)
    label = _meta(conn, "folder")
    if identity is None or label is None:
        return {}
    published = int(_meta(conn, "published_through") or 0)
    owned = store is None
    target = store if store is not None else store_for(addon_dir, profile, label)
    devices = f"{library_rel(identity.library_id)}/devices"
    progress: dict[str, PeerProgress] = {}
    try:
        for entry in target.listdir(devices):
            if not entry.is_dir or entry.is_symlink or entry.name == identity.device_id:
                continue
            data = target.read(f"{devices}/{entry.name}/device.json", 64 * 1024)
            try:
                info = json.loads(data.decode("utf-8")) if data else None
            except ValueError:
                continue
            if not isinstance(info, dict) or not isinstance(info.get("received"), dict):
                continue
            mine = info["received"].get(identity.device_id)
            through = mine.get("through", 0) if isinstance(mine, dict) and mine.get("epoch") == identity.epoch else 0
            waiting = info.get("files_waiting", 0)
            progress[entry.name] = PeerProgress(max(0, published - int(through)),
                                                int(waiting) if isinstance(waiting, int) else 0)
    except (StoreError, OSError):
        return {}
    finally:
        if owned and hasattr(target, "close"):
            target.close()
    return progress


@dataclass(frozen=True)
class FetchReport:
    #: Files copied into this device's blob store by this call, and files still waiting after it.
    fetched: int
    left: int


def fetch_files(addon_dir: str, profile: str, *, store: Store | None = None,
                budget_seconds: float | None = None) -> FetchReport:
    """Download files that received changes wait for into the blob store, without touching the Anki collection.

    Only verified blobs are placed (named by their hash), and the profile's database is only read, through a
    connection of its own: this can run while the app uses the collection, and the next ``sync_now`` places what
    arrived. With ``budget_seconds`` it stops after at least one file once the time is up. Nothing to do (sync off,
    nothing waiting, or a sync running) is ``FetchReport(0, 0)`` or the files left.
    """
    db_path = Path(_paths.get_db_path(addon_dir, profile))
    if not db_path.is_file():
        return FetchReport(0, 0)
    reader = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        identity = sync_identity(reader)
        label = _meta(reader, "folder")
        blob_dir = sync_paths.get_sync_blob_dir(addon_dir, profile)
        missing = sorted(missing_blobs(reader, blob_dir)) if identity is not None and label is not None else []
    except sqlite3.Error:
        return FetchReport(0, 0)  # sync tables not made yet: sync was never set up
    finally:
        reader.close()
    if not missing:
        return FetchReport(0, 0)
    lock = _lock(addon_dir, profile)
    if not lock.acquire(blocking=False):
        return FetchReport(0, len(missing))  # a sync is moving files now
    try:
        owned = store is None
        target = store if store is not None else store_for(addon_dir, profile, label)
        budget, fetched = Budget(budget_seconds), 0
        try:
            library = library_rel(identity.library_id)
            for sha in missing:
                if fetched and budget.exceeded():
                    break
                if fetch_exchange_blob(target, library, sha, blob_dir):
                    fetched += 1
        finally:
            if owned and hasattr(target, "close"):
                target.close()
        return FetchReport(fetched, len(missing) - fetched)
    finally:
        lock.release()


def publish_now(addon_dir: str, profile: str, query: Query, *,
                now: Callable[[], float] = time.time, store: Store | None = None) -> SyncReport:
    """Capture and publish only: quick, for a closing profile. Nothing received is applied."""
    return sync_now(addon_dir, profile, query, now=now, publish_only=True, store=store)


def status(addon_dir: str, profile: str, *, now: float | None = None) -> SyncStatus:
    conn = get_connection(addon_dir, profile)
    cutoff = (time.time() if now is None else now) - STALE_WAITING_SECONDS
    stale = conn.execute(f"SELECT count(*) FROM sync_inbox WHERE status IN ({','.join('?' * len(_WAITING))}) "
                         "AND received_at < ?", (*_WAITING, cutoff)).fetchone()[0]
    configured = sync_identity(conn) is not None and _meta(conn, "folder") is not None
    counts = dict(conn.execute("SELECT status, count(*) FROM sync_inbox GROUP BY status").fetchall())
    last = _meta(conn, "last_sync")
    open_conflicts = conn.execute("SELECT count(*) FROM sync_conflicts WHERE resolved_revision IS NULL").fetchone()[0]
    # Published revisions leave the outbox: the time of the last publication is kept apart.
    published = _meta(conn, "published_at") or conn.execute("SELECT max(published_at) FROM sync_outbox").fetchone()[0]
    to_send, bytes_to_send, to_receive = _backlog(conn, sync_paths.get_sync_blob_dir(addon_dir, profile))
    return SyncStatus(configured=configured, folder=_meta(conn, "folder"),
                      last_sync=int(last) if last is not None else None, conflicts=open_conflicts,
                      waiting_for_anki=counts.get("pending_anki", 0), waiting_for_files=counts.get("pending_blob", 0),
                      waiting_for_parents=counts.get("pending_parent", 0), stale_waiting=stale,
                      last_published=int(published) if published is not None else None,
                      files_to_send=to_send, bytes_to_send=bytes_to_send, files_to_receive=to_receive)


def discard_stale(addon_dir: str, profile: str, *, now: float | None = None) -> int:
    """Give up on changes that waited too long; they were never applied, so nothing here changes.

    They become obsolete, so they no longer hold back receipts and retention.
    """
    conn = get_connection(addon_dir, profile)
    cutoff = (time.time() if now is None else now) - STALE_WAITING_SECONDS
    timestamp = int(time.time() if now is None else now)
    with conn:
        rows = conn.execute(f"SELECT i.revision_id, r.device, r.epoch FROM sync_inbox i "
                            f"JOIN sync_revisions r ON r.id = i.revision_id "
                            f"WHERE i.status IN ({','.join('?' * len(_WAITING))}) AND i.received_at < ?",
                            (*_WAITING, cutoff)).fetchall()
        for revision, _, _ in rows:
            conn.execute("UPDATE sync_inbox SET status='obsolete', reason='discarded after waiting', updated_at=? "
                         "WHERE revision_id=?", (timestamp, revision))
        for device, epoch in {(device, epoch) for _, device, epoch in rows}:
            _update_receipt(conn, device, epoch)
    return len(rows)


def open_conflicts(addon_dir: str, profile: str) -> list[Conflict]:
    conn = get_connection(addon_dir, profile)
    conflicts = []
    for conflict_id, family, key, heads in conn.execute(
            "SELECT id, family, key, heads FROM sync_conflicts WHERE resolved_revision IS NULL ORDER BY id"):
        head_ids = json.loads(heads)
        values = []
        for head in head_ids:
            row = conn.execute("SELECT value FROM sync_revisions WHERE id=?", (head,)).fetchone()
            values.append(None if row is None or row[0] is None else json.loads(row[0]))
        base = conn.execute("SELECT revision_id FROM sync_baseline WHERE family=? AND key=?", (family, key)).fetchone()
        local = head_ids.index(base[0]) if base is not None and base[0] in head_ids else 0
        conflicts.append(Conflict(conflict_id, family, key, head_ids, values, local))
    return conflicts


def resolve(addon_dir: str, profile: str, conflict_id: int, value: dict[str, Any] | None, query: Query, *,
            now: Callable[[], float] = time.time) -> str:
    """Settle a conflict with the chosen value; published by the next sync."""
    conn = get_connection(addon_dir, profile)
    return resolve_conflict(conn, conflict_id, value, families_for(addon_dir, profile),
                            lambda card_ids: card_refs(query, card_ids), now=now)
