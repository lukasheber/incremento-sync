"""Content-addressed file storage for sync: ``<store>/<aa>/<sha256>``.

Used for the profile's local blob store and for the exchange folder's
``blobs/``. A blob is written to a temporary name while it is hashed and is
renamed into place only when its bytes match its name, so a blob that exists
under its name is complete and verified.

On a phone (``KEEP_ONE_COPY``) space is short and an app may not hard-link
files: a blob is removed once its document is in place with the same content
(see ``sync_service``), and made again from that document when it is needed
(``restore_from_placed``), so each document takes its space once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

try:
    from .sync_store import Store, StoreError
except ImportError:
    from sync_store import Store, StoreError  # type: ignore[no-redef]

MAX_BLOB_BYTES = 512 * 1024 * 1024
#: Keep one copy of each placed document, not a blob beside it (Android, where space is short).
KEEP_ONE_COPY = (sys.platform == "android" or hasattr(sys, "getandroidapilevel")
                 or ("ANDROID_DATA" in os.environ and "ANDROID_ROOT" in os.environ))
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def left_out(path: Path | None, limit: int | None = None) -> str | None:
    """Why the file at ``path`` cannot sync: "missing" (not here, or not a plain file), "too_big", or None."""
    if path is None or path.is_symlink() or not path.is_file():
        return "missing"
    if path.stat().st_size > (MAX_BLOB_BYTES if limit is None else limit):
        return "too_big"
    return None


class Budget:
    """A time allowance for the file transfer in one sync call; ``seconds`` of None means no limit.

    Whoever transfers files asks ``exceeded()`` before each further unit of work, after finishing at least one, so
    every call makes progress. ``more`` says that work was left for a later call.
    """

    def __init__(self, seconds: float | None, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._deadline = None if seconds is None else clock() + seconds
        self.more = False

    def exceeded(self) -> bool:
        if self._deadline is not None and self._clock() >= self._deadline:
            self.more = True
            return True
        return False


class FileHashes:
    """sha256 of files, remembered while each file's size and modification time stay the same, kept on disk.

    Scanning a library of many gigabytes would otherwise read every file again at the first sync after every start.
    The record is only a shortcut: a missing or damaged one costs reading the files again, nothing else.
    """

    def __init__(self, path: Path, compute: Callable[[Path], str]):
        self._path = Path(path)
        self._compute = compute
        self._known: dict[str, tuple[int, int, str]] | None = None
        self._dirty = False

    def _load(self) -> dict[str, tuple[int, int, str]]:
        if self._known is None:
            known: dict[str, tuple[int, int, str]] = {}
            try:
                for name, (size, mtime, sha) in json.loads(self._path.read_text(encoding="utf-8")).items():
                    if isinstance(size, int) and isinstance(mtime, int) and isinstance(sha, str) and _HEX64.match(sha):
                        known[name] = (size, mtime, sha)
            except (OSError, ValueError, TypeError, AttributeError):
                known = {}
            self._known = known
        return self._known

    def sha(self, path: Path) -> str:
        known = self._load()
        stat = path.stat()
        cached = known.get(str(path))
        if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]
        sha = self._compute(path)
        known[str(path)] = (stat.st_size, stat.st_mtime_ns, sha)
        self._dirty = True
        return sha

    def save(self) -> None:
        """Write what was learned (atomically); a failure only means reading the files again next time."""
        if not self._dirty or self._known is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(self._path.name + ".tmp")
            temporary.write_text(json.dumps({name: list(entry) for name, entry in self._known.items()}),
                                 encoding="utf-8")
            os.replace(temporary, self._path)
            self._dirty = False
        except OSError:
            pass


def blob_path(store: Path, sha: str) -> Path:
    if not _HEX64.match(sha):
        raise ValueError(f"not a blob name: {sha!r}")
    return Path(store) / sha[:2] / sha


def has_blob(store: Path | None, sha: str) -> bool:
    if store is None:
        return False
    path = blob_path(store, sha)
    return path.is_file() and not path.is_symlink()


def _copy_hashing(source: Path, directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    handle, name = tempfile.mkstemp(dir=directory, prefix=".incoming-")
    try:
        with os.fdopen(handle, "wb") as target, source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BLOB_BYTES:
                    raise ValueError(f"{source.name} exceeds {MAX_BLOB_BYTES} bytes")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name), digest.hexdigest()


def _place(temporary: Path, store: Path, sha: str) -> None:
    final = blob_path(store, sha)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.is_file():
        temporary.unlink(missing_ok=True)
        return
    os.replace(temporary, final)


def store_file(store: Path, source: Path) -> str:
    """Copy ``source`` into the store; returns its sha256."""
    if Path(source).is_symlink():
        raise ValueError(f"{source} is a symlink")
    temporary, sha = _copy_hashing(Path(source), Path(store) / ".tmp")
    _place(temporary, Path(store), sha)
    return sha


def exchange_blob_rel(library_rel: str, sha: str) -> str:
    if not _HEX64.match(sha):
        raise ValueError(f"not a blob name: {sha!r}")
    return f"{library_rel}/blobs/{sha[:2]}/{sha}"


def exchange_blob_present(store: Store, library_rel: str, local_store: Path, sha: str) -> bool:
    """Whether the exchange already holds this blob (as complete as its local copy: named by its hash, written whole)."""
    if not has_blob(local_store, sha):
        return False
    existing = store.stat(exchange_blob_rel(library_rel, sha))
    return existing is not None and not existing.is_dir and existing.size == blob_path(local_store, sha).stat().st_size


def share_exchange_blobs(store: Store, library_rel: str, local_store: Path) -> int:
    """Make every blob also in the exchange one file with its local copy, where ``store`` shares files; bytes freed."""
    share = getattr(store, "share", None)
    if share is None or not getattr(store, "share_files", False) or not Path(local_store).is_dir():
        return 0
    freed = 0
    for blob in Path(local_store).glob("??/*"):
        if _HEX64.match(blob.name) and blob.is_file() and not blob.is_symlink():
            freed += share(blob, exchange_blob_rel(library_rel, blob.name))
    return freed


def put_exchange_blob(store: Store, library_rel: str, local_store: Path, sha: str) -> bool:
    """Place one of this device's verified local blobs in the exchange. False if it has no local copy."""
    if not has_blob(local_store, sha):
        return False
    source = blob_path(local_store, sha)
    rel = exchange_blob_rel(library_rel, sha)
    existing = store.stat(rel)
    if existing is not None and not existing.is_dir and existing.size == source.stat().st_size:
        return True  # named by its hash and written atomically: complete
    store.upload(source, rel)
    return True


def fetch_exchange_blob(store: Store, library_rel: str, sha: str, local_store: Path) -> bool:
    """Copy one blob from the exchange into the local store after verifying its bytes against its name."""
    if has_blob(local_store, sha):
        return True
    rel = exchange_blob_rel(library_rel, sha)
    entry = store.stat(rel)
    if entry is None or entry.is_dir or entry.size > MAX_BLOB_BYTES:
        return False
    directory = Path(local_store) / ".tmp"
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory, prefix=".incoming-")
    os.close(handle)
    downloaded = Path(name)
    try:
        try:
            store.download(rel, downloaded, MAX_BLOB_BYTES)
        except StoreError:
            return False
        if _file_sha256(downloaded) != sha:
            return False
        _place(downloaded, Path(local_store), sha)
        return True
    finally:
        downloaded.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def placed_index(store: Path) -> dict[str, list[tuple[Path, int, int]]]:
    """The fingerprinted files beside ``store`` by content: sha256 -> [(path, size, modification time)]."""
    index: dict[str, list[tuple[Path, int, int]]] = {}
    for record in sorted(Path(store).parent.glob("file-hashes-*.json")):
        try:
            entries = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entries, dict):
            for name, entry in entries.items():
                if isinstance(entry, list) and len(entry) == 3 and isinstance(entry[2], str):
                    index.setdefault(entry[2], []).append((Path(name), entry[0], entry[1]))
    return index


def restore_from_placed(store: Path, sha: str, index: dict[str, list[tuple[Path, int, int]]] | None = None) -> bool:
    """Make blob ``sha`` again from a placed file with that content, if this device has one; True when it is there.

    The files a scan fingerprinted are listed beside the store (``file-hashes-*.json``); one whose size and
    modification time are still those it was fingerprinted with is copied in and verified like any blob. Saves
    downloading a file this device already holds, after its blob was removed to keep one copy.
    """
    if has_blob(store, sha):
        return True
    for path, size, mtime in (index if index is not None else placed_index(store)).get(sha, []):
        try:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (size, mtime) or path.is_symlink():
                continue
            if store_file(Path(store), path) == sha:
                return True
        except (OSError, ValueError):
            continue
    return False


def materialize(store: Path, sha: str, destination: Path) -> None:
    """Atomically place a stored blob at ``destination``."""
    source = blob_path(store, sha)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
    os.close(handle)
    try:
        shutil.copyfile(source, name)
        with open(name, "r+b") as stream:  # Windows cannot fsync a read-only handle
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
