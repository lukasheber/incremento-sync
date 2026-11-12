"""Video files for sync: the video of an "Incremento Video" card, and its subtitles.

A video topic made from a local file, or from a link whose video was downloaded, keeps that file and its subtitle files
in the profile's ``videos/`` folder. Neither Anki's media sync nor the other families carry them, so the topic would
reach another device without its video. The note travels through Anki; this family carries the files and places them
where the note says (``Local_Video_File``, ``Target_Subtitle_File``, ``Reference_Subtitle_File``).

A record ``card:<id>/video`` names the note and, for each file the card has on this device, its slot, its
profile-relative path and its sha256 as the *version*; the files are copied into the profile's blob store when captured
and the receiving device places each one where the note says. A note whose video is missing here is unavailable:
neither changed nor deleted. A tombstone forgets the record, never the user's files. A file larger than
``MAX_VIDEO_BYTES`` is not carried (the engine's blob limit), and a card shares its subtitles only together with a video
it carries.

Like the local files, a video card exists only as an Anki note, so this family reads the collection through ``query``
(the shape of ``col.db.all``, set by the service for each run). Without it the scan fails rather than report that every
video is gone.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    from . import paths, sync_paths
    from .sync_anki import Query
    from .sync_blobs import MAX_BLOB_BYTES, has_blob, materialize, store_file
    from .sync_records import SyncScanError, card_key, split_card_key, value_hash
except ImportError:
    import paths  # type: ignore[no-redef]
    import sync_paths  # type: ignore[no-redef]
    from sync_anki import Query  # type: ignore[no-redef]
    from sync_blobs import MAX_BLOB_BYTES, has_blob, materialize, store_file  # type: ignore[no-redef]
    from sync_records import SyncScanError, card_key, split_card_key, value_hash  # type: ignore[no-redef]

NOTE_TYPE = "Incremento Video"
#: slot -> the note field naming that file
SLOT_FIELDS = {
    "video": "Local_Video_File",
    "target_subtitle": "Target_Subtitle_File",
    "reference_subtitle": "Reference_Subtitle_File",
}
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".webm", ".mov", ".m4v")
SUBTITLE_EXTENSIONS = (".srt", ".vtt")
MAX_VIDEO_BYTES = MAX_BLOB_BYTES
IDENTITY_FIELDS = ("slot", "path", "version")
FILE_FIELDS = {*IDENTITY_FIELDS, "blob"}
RECORD_FIELDS = {"note_id", "files"}
_PATH = re.compile(r"^videos/([A-Za-z0-9._-]{1,255})$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^sha256:[0-9a-f]{64}$")


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _name(slot: Any, relpath: Any) -> str | None:
    """The file name inside ``videos/`` that a path names for this slot, or None when this device may not use it."""
    match = _PATH.match(relpath) if isinstance(relpath, str) else None
    if match is None or match.group(1).startswith(".") or slot not in SLOT_FIELDS:
        return None
    extensions = VIDEO_EXTENSIONS if slot == "video" else SUBTITLE_EXTENSIONS
    return match.group(1) if match.group(1).lower().endswith(extensions) else None


class VideoFileFamily:
    name = "video"

    def __init__(self, addon_dir: str, profile: str):
        self.addon_dir = addon_dir
        self.profile = profile
        self.blob_store = sync_paths.get_sync_blob_dir(addon_dir, profile)
        #: The collection's ``col.db.all``; the service sets it before each run.
        self.query: Query | None = None
        self._hashes: dict[str, tuple[int, int, str]] = {}

    # --- keys, paths and versions -------------------------------------------------

    def _parse_key(self, key: str) -> int:
        card_id, name = split_card_key(key, "video")
        if name or key != f"{card_key(card_id)}/video":
            raise ValueError(f"not a video key: {key!r}")
        return card_id

    def _canonical(self, stored: str) -> str:
        """``videos/<name>`` from what a note stores (Incremento also writes it with ``user_files/`` or the profile in front)."""
        rel = stored.strip().replace("\\", "/")
        if rel.startswith("user_files/"):
            rel = rel[len("user_files/"):]
        prefix = f"{paths.sanitize_profile_name(self.profile)}/"
        return rel[len(prefix):] if rel.startswith(prefix) else rel

    def _file(self, slot: str, relpath: str) -> Path | None:
        name = _name(slot, relpath)
        return None if name is None else paths.get_videos_dir(self.addon_dir, self.profile) / name

    def _sha(self, path: Path) -> str:
        """sha256 of a file, remembered while its size and modification time are unchanged."""
        stat = path.stat()
        cached = self._hashes.get(str(path))
        if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]
        sha = _file_sha(path)
        self._hashes[str(path)] = (stat.st_size, stat.st_mtime_ns, sha)
        return sha

    def _notes(self, card_id: int | None = None) -> list[tuple[int, int, dict[str, str]]]:
        """(card id, note id, {slot: stored path}) of every video card in the collection that names a local video."""
        if self.query is None:
            raise SyncScanError("videos cannot be read without the Anki collection")
        try:
            types = self.query("SELECT id FROM notetypes WHERE name = ?", NOTE_TYPE)
            if not types:
                return []
            ntid = types[0][0]
            ords = {name: ordinal for name, ordinal in self.query("SELECT name, ord FROM fields WHERE ntid = ?", ntid)}
            if SLOT_FIELDS["video"] not in ords:
                return []
            where, args = (" AND c.id = ?", (card_id,)) if card_id is not None else ("", ())
            rows = self.query("SELECT c.id, n.id, n.flds FROM cards c JOIN notes n ON n.id = c.nid "
                              "WHERE n.mid = ? AND c.ord = 0" + where + " ORDER BY c.id", ntid, *args)
        except sqlite3.OperationalError as exc:
            # A collection with no note types has no video cards; any other failure must not read as "no videos".
            if "no such table" in str(exc):
                return []
            raise SyncScanError(f"the Anki collection could not be read: {exc}") from exc
        found = []
        for cid, nid, flds in rows:
            fields = str(flds).split("\x1f")
            named: dict[str, str] = {}
            for slot, field in SLOT_FIELDS.items():
                position = ords.get(field)
                if position is not None and position < len(fields) and fields[position].strip():
                    named[slot] = self._canonical(fields[position])
            if "video" in named:
                found.append((int(cid), int(nid), named))
        return found

    def _identity(self, note: tuple[int, int, dict[str, str]]) -> tuple[dict[str, Any], dict[str, Path]] | None:
        """The record's identity and the files behind it, or None when the card's video is not (usably) here."""
        _, note_id, named = note
        files, located = [], {}
        for slot in SLOT_FIELDS:  # a fixed order, so equal cards give equal records
            if slot not in named:
                continue
            path = self._file(slot, named[slot])
            if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_VIDEO_BYTES:
                if slot == "video":
                    return None  # subtitles travel only with the video they belong to
                continue
            files.append({"slot": slot, "path": named[slot], "version": f"sha256:{self._sha(path)}"})
            located[slot] = path
        if "video" not in located:
            return None
        return {"note_id": note_id, "files": files}, located

    # --- the Family interface ------------------------------------------------------

    def scan(self, conn: sqlite3.Connection) -> Iterable[tuple[str, dict[str, Any]]]:
        records = []
        for note in self._notes():
            found = self._identity(note)
            if found is not None:
                records.append((f"{card_key(note[0])}/video", found[0]))
        return records

    def read(self, conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
        notes = self._notes(self._parse_key(key))
        found = self._identity(notes[0]) if notes else None
        if found is None:
            return None
        value, located = found
        for entry in value["files"]:
            entry["blob"] = store_file(self.blob_store, located[entry["slot"]])
        return value

    def unavailable(self, conn: sqlite3.Connection, key: str) -> bool:
        """The note names a local video but its file is not here: not a change, not a deletion."""
        notes = self._notes(self._parse_key(key))
        return bool(notes) and self._identity(notes[0]) is None

    def card_id(self, key: str) -> int | None:
        return self._parse_key(key)

    def fingerprint(self, value: dict[str, Any]) -> str:
        return value_hash({"note_id": value["note_id"],
                           "files": [{field: entry[field] for field in IDENTITY_FIELDS} for entry in value["files"]]})

    def blobs(self, value: dict[str, Any]) -> list[str]:
        return [entry["blob"] for entry in value.get("files", []) if entry.get("blob")]

    def validate(self, key: str, value: Any) -> None:
        self._parse_key(key)
        if value is None:
            return
        if not isinstance(value, dict) or set(value) != RECORD_FIELDS:
            raise ValueError("video fields")
        if not isinstance(value["note_id"], int) or isinstance(value["note_id"], bool) or value["note_id"] <= 0:
            raise ValueError("video note id")
        files = value["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= len(SLOT_FIELDS):
            raise ValueError("video files")
        slots = []
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != FILE_FIELDS:
                raise ValueError("video file fields")
            if _name(entry["slot"], entry["path"]) is None:
                raise ValueError("video file path")
            if not isinstance(entry["version"], str) or not _VERSION.match(entry["version"]):
                raise ValueError("video file version")
            if not isinstance(entry["blob"], str) or not _HEX64.match(entry["blob"]):
                raise ValueError("video file blob")
            slots.append(entry["slot"])
        if len(set(slots)) != len(slots) or "video" not in slots:
            raise ValueError("video file slots")

    def write(self, conn: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
        """Place the files where the note says. Runs inside the caller's transaction; nothing in the database changes."""
        self._parse_key(key)
        for entry in value["files"]:
            target = self._file(entry["slot"], entry["path"])
            if target is None:
                raise ValueError("video file path")
            if not has_blob(self.blob_store, entry["blob"]):
                raise FileNotFoundError(f"blob {entry['blob']} is not in the local blob store")
            if target.is_symlink():
                raise ValueError("video file target is a symlink")
            if not (target.is_file() and self._sha(target) == entry["blob"]):
                materialize(self.blob_store, entry["blob"], target)

    def delete(self, conn: sqlite3.Connection, key: str) -> None:
        """Forget the record. The files stay: sync never deletes a user's file."""
        self._parse_key(key)


__all__ = ["VideoFileFamily"]
