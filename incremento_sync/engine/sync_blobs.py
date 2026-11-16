"""Content-addressed file storage for sync: ``<store>/<aa>/<sha256>``.

Used for the profile's local blob store and for the exchange folder's
``blobs/``. A blob is written to a temporary name while it is hashed and is
renamed into place only when its bytes match its name, so a blob that exists
under its name is complete and verified.

On a phone (``LINK_PLACED_FILES``) a blob and the document it is placed as are
one file (a hard link), not two copies: space is short there, and nothing
writes a document in place (Incremento saves a changed PDF or EPUB to a new
file and renames it over the old one, which leaves the blob as it was). The
document path itself is never replaced to share it: the blob is made a link to
the document, and only while the document is still the very file that was
hashed (same inode, size and modification time).
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
#: Place received files as hard links to their blobs (Android): one file on disk instead of two.
LINK_PLACED_FILES = hasattr(sys, "getandroidapilevel")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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
    """Copy ``source`` into the store (on a phone: link it, when it can be); returns its sha256."""
    if Path(source).is_symlink():
        raise ValueError(f"{source} is a symlink")
    if LINK_PLACED_FILES:
        before = Path(source).stat()
        if before.st_size > MAX_BLOB_BYTES:
            raise ValueError(f"{Path(source).name} exceeds {MAX_BLOB_BYTES} bytes")
        sha = _file_sha256(Path(source))
        if share_with_blob(Path(store), sha, Path(source), before):
            return sha
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


def share_with_blob(store: Path, sha: str, path: Path, hashed: os.stat_result) -> bool:
    """Make ``path`` (whose content hashed to ``sha`` when it had the stat ``hashed``) the blob ``sha``: one file.

    Only on a phone, and only while ``path`` is still that file; the document path is left alone. Returns whether the
    blob and ``path`` are now one file; on any doubt or failure nothing changes.
    """
    if not LINK_PLACED_FILES or not _HEX64.match(sha):
        return False
    final = blob_path(store, sha)
    linked = final.with_name(f".{sha}.link")
    try:
        if final.is_file() and os.path.samefile(final, path):
            return True
        final.parent.mkdir(parents=True, exist_ok=True)
        linked.unlink(missing_ok=True)
        os.link(path, linked)
        now = linked.stat()
        if (now.st_ino, now.st_size, now.st_mtime_ns) != (hashed.st_ino, hashed.st_size, hashed.st_mtime_ns):
            return False  # the document changed after it was hashed: not this blob's content
        os.replace(linked, final)
        return True
    except OSError:
        return False  # e.g. another file system: the blob stays a copy
    finally:
        linked.unlink(missing_ok=True)


def materialize(store: Path, sha: str, destination: Path) -> None:
    """Atomically place a stored blob at ``destination`` (on a phone: the same file as the blob, when it can be)."""
    source = blob_path(store, sha)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
    os.close(handle)
    if LINK_PLACED_FILES:
        try:
            Path(name).unlink()
            os.link(source, name)
            os.replace(name, destination)
            return
        except OSError:
            Path(name).unlink(missing_ok=True)
            Path(name).touch()  # another file system: a copy, as on a computer
    try:
        shutil.copyfile(source, name)
        with open(name, "r+b") as stream:  # Windows cannot fsync a read-only handle
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
