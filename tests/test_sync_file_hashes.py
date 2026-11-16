"""Files are fingerprinted once, not again at every start.

Every sync scans the documents, local files and videos, and fingerprints each file (sha256) unless its size and
modification time are unchanged since it was last read. Kept only in memory, that meant reading the whole library again
at the first sync after every start: minutes for a library of many gigabytes, with the Anki collection held all along.
"""
import os

import pytest

import db
import paths
import sync_documents
import sqlite3

import sync_service
from sync_store import MemoryStore


def _collection(cards):
    col = sqlite3.connect(":memory:", check_same_thread=False)
    col.execute("CREATE TABLE col (usn INTEGER, scm INTEGER, mod INTEGER DEFAULT 1)")
    col.execute("INSERT INTO col(usn, scm) VALUES (20, 1000)")
    col.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT)")
    col.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, usn INTEGER)")
    for card_id in cards:
        col.execute("INSERT INTO notes VALUES (?, ?)", (card_id * 10, f"g{card_id}"))
        col.execute("INSERT INTO cards VALUES (?, ?, 5)", (card_id, card_id * 10))
    return lambda sql, *args: col.execute(sql, args).fetchall()


CARDS = _collection(range(1, 30))


class Device:
    def __init__(self, tmp_path, name, store):
        self.addon, self.store = str(tmp_path / name), store
        sync_service.configure(self.addon, "P", f"store:{name}", query=CARDS, store=store)

    def add_documents(self, count):
        for index in range(1, count + 1):
            pdf = paths.get_pdf_dir(self.addon, "P") / f"book{index}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.7\n" + f"book {index} ".encode() * 3000)
            with db.get_connection(self.addon, "P") as conn:
                conn.execute("INSERT INTO content_items(content_id, kind, card_id, note_id, storage_key, created_at, "
                             "updated_at) VALUES (?, 'pdf', ?, ?, ?, 1, 1)",
                             (f"{index:032x}", index, index * 10, f"pdfs/book{index}.pdf"))

    def sync(self):
        report = sync_service.sync_now(self.addon, "P", CARDS, store=self.store)
        assert report.problems == [] and report.skipped is None, (report.problems, report.skipped)
        return report, None


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


def _restart():
    """What a new process (the app or Anki started again) has: no family objects, so no memory of read files."""
    for families in (sync_service._document_families, sync_service._file_families, sync_service._video_families):
        families.clear()


@pytest.fixture
def reads(monkeypatch):
    seen = []
    real = sync_documents._file_sha
    monkeypatch.setattr(sync_documents, "_file_sha", lambda path: seen.append(path.name) or real(path))
    return seen


def test_after_a_restart_unchanged_documents_are_not_read_again(tmp_path, reads):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(5)
    a.sync()
    _restart()
    reads.clear()

    report, _ = a.sync()

    assert reads == []
    assert report.captured == 0


def test_after_a_restart_a_changed_document_is_read_again_and_its_new_version_sent(tmp_path, reads):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(3)
    a.sync()
    _restart()
    changed = paths.get_pdf_dir(a.addon, "P") / "book2.pdf"
    changed.write_bytes(changed.read_bytes() + b"\n% annotated")
    stamp = changed.stat().st_mtime_ns + 5_000_000_000
    os.utime(changed, ns=(stamp, stamp))
    reads.clear()

    report, _ = a.sync()

    assert set(reads) == {"book2.pdf"}
    assert report.captured == 1 and report.published >= 1


def test_a_damaged_fingerprint_file_only_costs_reading_the_files_again(tmp_path, reads):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(2)
    a.sync()
    _restart()
    for remembered in (tmp_path / "a").rglob("file-hashes*.json"):
        remembered.write_text("{not json", encoding="utf-8")
    reads.clear()

    report, _ = a.sync()

    assert sorted(reads) == ["book1.pdf", "book2.pdf"] and report.captured == 0
    assert sync_service.sync_now(a.addon, "P", CARDS, store=a.store).problems == []
