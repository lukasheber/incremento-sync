"""PDFs and EPUBs are found through their Anki notes, as Incremento's reader finds them.

Incremento only records a document in ``content_items`` since its August 2026 update; it finds a card's file through
the note (``PDF_Filename`` / ``EPUB_Filename`` in the profile's pdfs/epubs folder), and never filled the table in for
documents imported before. Sync looked only at ``content_items``, so a library of 1097 documents offered 9 to the
phone. A document with no row is now found through its note, with the note's content id or one made from the note's
GUID (the same on every device). A row whose file is gone while the note names one that is there (a repaired file
name) follows the note.
"""
import hashlib
import sqlite3

import pytest

import db
import paths
import sync_service
from sync_store import MemoryStore

PDF_TYPE, EPUB_TYPE = 1, 2
PDF_FIELDS = ("Title", "PDF_Filename", "Incremento_Content_ID")
EPUB_FIELDS = ("Title", "EPUB_Filename")


class Collection:
    """The Anki collection both devices share through AnkiWeb: Incremento's note types, notes and cards."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE col (usn INTEGER, scm INTEGER, mod INTEGER DEFAULT 1)")
        self.db.execute("INSERT INTO col(usn, scm) VALUES (20, 1000)")
        self.db.execute("CREATE TABLE notetypes (id INTEGER PRIMARY KEY, name TEXT)")
        self.db.execute("CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT)")
        self.db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, flds TEXT)")
        self.db.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, ord INTEGER, usn INTEGER)")
        self.db.execute("INSERT INTO notetypes VALUES (?, 'Incremento PDF')", (PDF_TYPE,))
        self.db.execute("INSERT INTO notetypes VALUES (?, 'Incremento EPUB')", (EPUB_TYPE,))
        for ntid, fields in ((PDF_TYPE, PDF_FIELDS), (EPUB_TYPE, EPUB_FIELDS)):
            for ordinal, name in enumerate(fields):
                self.db.execute("INSERT INTO fields VALUES (?, ?, ?)", (ntid, ordinal, name))

    def query(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def add(self, card_id, ntid, fields):
        self.db.execute("INSERT OR REPLACE INTO notes VALUES (?, ?, ?, ?)",
                        (card_id * 10, f"guid-{card_id}", ntid, "\x1f".join(fields)))
        self.db.execute("INSERT OR REPLACE INTO cards VALUES (?, ?, 0, 5)", (card_id, card_id * 10))


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


class Device:
    def __init__(self, tmp_path, name, store, col):
        self.addon, self.store, self.col = str(tmp_path / name), store, col
        sync_service.configure(self.addon, "P", f"store:{name}", query=col.query, store=store)

    def file(self, folder, name, data):
        path = (paths.get_pdf_dir if folder == "pdfs" else paths.get_epub_dir)(self.addon, "P") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def rows(self):
        return db.get_connection(self.addon, "P").execute(
            "SELECT content_id, kind, card_id, storage_key FROM content_items ORDER BY card_id").fetchall()

    def sync(self):
        report = sync_service.sync_now(self.addon, "P", self.col.query, store=self.store)
        assert report.problems == [] and report.skipped is None, (report.problems, report.skipped)
        return report


def _pair(tmp_path):
    store, col = MemoryStore(), Collection()
    return Device(tmp_path, "computer", store, col), Device(tmp_path, "phone", store, col), col


def test_a_pdf_imported_before_content_items_reaches_the_phone(tmp_path):
    computer, phone, col = _pair(tmp_path)
    data = b"%PDF-1.7\nold book " * 2000
    computer.file("pdfs", "Old_book.pdf", data)
    col.add(5, PDF_TYPE, ("Old book", "Old_book.pdf", ""))  # no content_items row, as before August 2026
    assert computer.rows() == []

    computer.sync()
    phone.sync()

    assert (paths.get_pdf_dir(phone.addon, "P") / "Old_book.pdf").read_bytes() == data
    content_id = hashlib.sha256(b"pdf:guid-5").hexdigest()[:32]
    assert phone.rows() == [(content_id, "pdf", 5, "pdfs/Old_book.pdf")]
    assert computer.rows() == []  # the computer's records are left as they are


def test_the_notes_own_content_id_is_used_when_it_has_one(tmp_path):
    computer, phone, col = _pair(tmp_path)
    computer.file("pdfs", "Tagged.pdf", b"%PDF-1.7\ntagged " * 2000)
    col.add(6, PDF_TYPE, ("Tagged", "Tagged.pdf", "abc123def456"))

    computer.sync()
    phone.sync()

    assert phone.rows() == [("abc123def456", "pdf", 6, "pdfs/Tagged.pdf")]


def test_an_old_epub_reaches_the_phone_too(tmp_path):
    computer, phone, col = _pair(tmp_path)
    data = b"PK\x03\x04 an old epub " * 2000
    computer.file("epubs", "Old_novel.epub", data)
    col.add(7, EPUB_TYPE, ("Old novel", "Old_novel.epub"))

    computer.sync()
    phone.sync()

    assert (paths.get_epub_dir(phone.addon, "P") / "Old_novel.epub").read_bytes() == data


def test_a_row_whose_file_was_renamed_follows_the_note(tmp_path):
    computer, phone, col = _pair(tmp_path)
    data = b"%PDF-1.7\nrepaired " * 2000
    computer.file("pdfs", "Book_new.pdf", data)  # repaired: the note names the file that is there
    col.add(8, PDF_TYPE, ("Book", "Book_new.pdf", "row8"))
    with db.get_connection(computer.addon, "P") as conn:
        conn.execute("INSERT INTO content_items(content_id, kind, card_id, note_id, storage_key, created_at, "
                     "updated_at) VALUES ('row8', 'pdf', 8, 80, 'pdfs/Book_old.pdf', 1, 1)")

    computer.sync()
    phone.sync()

    assert (paths.get_pdf_dir(phone.addon, "P") / "Book_new.pdf").read_bytes() == data
    assert phone.rows() == [("row8", "pdf", 8, "pdfs/Book_new.pdf")]


def test_a_document_found_through_its_note_does_not_travel_back_and_forth(tmp_path):
    computer, phone, col = _pair(tmp_path)
    computer.file("pdfs", "Steady.pdf", b"%PDF-1.7\nsteady " * 2000)
    col.add(9, PDF_TYPE, ("Steady", "Steady.pdf", ""))
    computer.sync()
    phone.sync()

    again_phone, again_computer = phone.sync(), computer.sync()

    assert again_phone.captured == 0 and again_phone.published == 0
    assert again_computer.captured == 0 and again_computer.published == 0


def test_a_note_without_its_file_here_is_neither_sent_nor_deleted(tmp_path):
    computer, phone, col = _pair(tmp_path)
    col.add(10, PDF_TYPE, ("Elsewhere", "Not_here.pdf", ""))

    report = computer.sync()

    assert report.captured == 0 and report.files_to_send == 0


def test_a_document_too_big_to_sync_is_left_out_and_the_others_still_travel(tmp_path, monkeypatch):
    import sync_blobs
    monkeypatch.setattr(sync_blobs, "MAX_BLOB_BYTES", 50_000)
    computer, phone, col = _pair(tmp_path)
    computer.file("pdfs", "Huge.pdf", b"%PDF-1.7\n" + b"x" * 60_000)
    col.add(11, PDF_TYPE, ("Huge", "Huge.pdf", ""))
    small = b"%PDF-1.7\nsmall " * 2000
    computer.file("pdfs", "Small.pdf", small)
    col.add(12, PDF_TYPE, ("Small", "Small.pdf", ""))

    computer.sync()
    phone.sync()

    assert (paths.get_pdf_dir(phone.addon, "P") / "Small.pdf").read_bytes() == small
    assert not (paths.get_pdf_dir(phone.addon, "P") / "Huge.pdf").exists()
