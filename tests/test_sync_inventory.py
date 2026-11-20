"""What the add-on can say about itself: which documents it found and how far each is, a log of what each sync did,
and a report to save for help ("Sync Incremento Data doesn't do anything").
"""
import sqlite3

import pytest

import db
import paths
import sync_blobs
import sync_paths
import sync_service
from sync_store import MemoryStore

PDF_TYPE = 1


class Collection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE col (usn INTEGER, scm INTEGER, mod INTEGER DEFAULT 1)")
        self.db.execute("INSERT INTO col(usn, scm) VALUES (20, 1000)")
        self.db.execute("CREATE TABLE notetypes (id INTEGER PRIMARY KEY, name TEXT)")
        self.db.execute("CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT)")
        self.db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, flds TEXT)")
        self.db.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, ord INTEGER, usn INTEGER)")
        self.db.execute("INSERT INTO notetypes VALUES (?, 'Incremento PDF')", (PDF_TYPE,))
        for ordinal, name in enumerate(("Title", "PDF_Filename")):
            self.db.execute("INSERT INTO fields VALUES (?, ?, ?)", (PDF_TYPE, ordinal, name))

    def query(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def add(self, card_id, filename):
        self.db.execute("INSERT INTO notes VALUES (?, ?, ?, ?)",
                        (card_id * 10, f"guid-{card_id}", PDF_TYPE, f"Book {card_id}\x1f{filename}"))
        self.db.execute("INSERT INTO cards VALUES (?, ?, 0, 5)", (card_id, card_id * 10))


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


@pytest.fixture
def computer(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_blobs, "MAX_BLOB_BYTES", 50_000)
    col, addon = Collection(), str(tmp_path / "computer")
    store = MemoryStore()
    sync_service.configure(addon, "P", "store:computer", query=col.query, store=store)
    folder = paths.get_pdf_dir(addon, "P")
    folder.mkdir(parents=True, exist_ok=True)
    for card in (1, 2, 3):
        (folder / f"book{card}.pdf").write_bytes(b"%PDF-1.7\n" + f"book {card} ".encode() * 1000)
        col.add(card, f"book{card}.pdf")
    col.add(4, "gone.pdf")  # the note names a file that is not on this computer
    (folder / "huge.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 60_000)
    col.add(5, "huge.pdf")
    return addon, col, store


def test_the_inventory_says_what_was_found_and_how_far_it_is(computer):
    addon, col, store = computer

    before = sync_service.inventory(addon, "P", col.query)
    sync_service.sync_now(addon, "P", col.query, store=store)
    after = sync_service.inventory(addon, "P", col.query)

    assert (before.found, before.ready, before.preparing, before.missing, before.too_big) == (5, 0, 3, 1, 1)
    assert (after.found, after.ready, after.preparing, after.missing, after.too_big) == (5, 3, 0, 1, 1)
    assert sorted(after.left_out) == [("missing", "gone.pdf"), ("too_big", "huge.pdf")]


def test_each_sync_is_written_to_a_log(computer):
    addon, col, store = computer

    sync_service.sync_now(addon, "P", col.query, store=store)

    log = (sync_paths.get_sync_dir(addon, "P") / "sync.log").read_text(encoding="utf-8")
    assert "sync started" in log and "sync finished" in log and "published" in log


def test_a_sync_that_fails_is_logged_with_why_and_still_fails(computer, monkeypatch):
    addon, col, store = computer

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sync_service, "capture", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        sync_service.sync_now(addon, "P", col.query, store=store)

    log = (sync_paths.get_sync_dir(addon, "P") / "sync.log").read_text(encoding="utf-8")
    assert "sync failed" in log and "disk full" in log and "Traceback" in log


def test_the_log_stays_small(computer, monkeypatch):
    addon, col, store = computer
    monkeypatch.setattr(sync_service, "LOG_MAX_BYTES", 2_000)
    for _ in range(30):
        sync_service.sync_now(addon, "P", col.query, store=store)

    assert (sync_paths.get_sync_dir(addon, "P") / "sync.log").stat().st_size <= 2_000


def test_the_report_to_save_has_what_helps_and_no_keys(computer, tmp_path):
    from sync_http import KeyBook

    addon, col, store = computer
    sync_service.sync_now(addon, "P", col.query, store=store)
    key = KeyBook(sync_paths.get_sync_dir(addon, "P") / "phone-keys.json").add("pixel")

    report = sync_service.diagnostics(addon, "P", col.query, extra={"Anki": "26.9.4"})

    for expected in ("Incremento Sync", "Anki: 26.9.4", "Documents and files found: 5", "Ready to sync: 3",
                     "gone.pdf", "huge.pdf", "sync finished"):
        assert expected in report, expected
    assert key.hex() not in report
