"""On a phone, a document received through sync is one file on disk, not two.

The engine keeps every file it moves in its blob store (named by its hash) and places a copy where the document lives,
so a phone held each document twice: 2.7 GB of documents took 5.4 GB. Where space is short (Android), the placed file
is a hard link to the blob instead. That is safe because nothing writes a document in place: Incremento saves a
changed PDF or EPUB to a new file and renames it over the old one, which leaves the blob as it was.
"""
import hashlib
import os
import sqlite3

import pytest

import db
import paths
import sync_blobs
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
            pdf = self.pdf(index)
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.7\n" + f"book {index} ".encode() * 3000)
            with db.get_connection(self.addon, "P") as conn:
                conn.execute("INSERT INTO content_items(content_id, kind, card_id, note_id, storage_key, created_at, "
                             "updated_at) VALUES (?, 'pdf', ?, ?, ?, 1, 1)",
                             (f"{index:032x}", index, index * 10, f"pdfs/book{index}.pdf"))

    def pdf(self, index):
        return paths.get_pdf_dir(self.addon, "P") / f"book{index}.pdf"

    def blob(self, path):
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return sync_blobs.blob_path(sync_service.sync_paths.get_sync_blob_dir(self.addon, "P"), sha)

    def sync(self):
        report = sync_service.sync_now(self.addon, "P", CARDS, store=self.store)
        assert report.problems == [] and report.skipped is None, (report.problems, report.skipped)
        return report


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(sync_blobs, "LINK_PLACED_FILES", True)


def _received(tmp_path, count=3):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(count)
    a.sync()
    b.sync()
    return a, b


def test_a_received_document_and_its_blob_are_one_file_on_a_phone(tmp_path, phone):
    _, b = _received(tmp_path)

    for index in (1, 2, 3):
        assert os.path.samefile(b.pdf(index), b.blob(b.pdf(index)))


def test_on_a_computer_a_received_document_is_a_copy_of_its_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_blobs, "LINK_PLACED_FILES", False)
    _, b = _received(tmp_path, 1)

    assert not os.path.samefile(b.pdf(1), b.blob(b.pdf(1)))


def test_a_document_changed_on_the_phone_leaves_its_blob_whole_and_its_new_version_travels(tmp_path, phone):
    a, b = _received(tmp_path, 1)
    blob = b.blob(b.pdf(1))
    original = blob.read_bytes()
    replacement = b.pdf(1).with_name("book1.pdf.tmp")  # as Incremento saves an annotated PDF
    replacement.write_bytes(original + b"\n% highlighted")
    os.replace(replacement, b.pdf(1))

    assert blob.read_bytes() == original and hashlib.sha256(original).hexdigest() == blob.name
    assert b.sync().published >= 1
    a.sync()
    assert a.pdf(1).read_bytes().endswith(b"% highlighted")


def test_documents_received_before_are_made_one_file_with_their_blobs_at_the_next_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_blobs, "LINK_PLACED_FILES", False)
    a, b = _received(tmp_path, 2)  # an earlier version placed copies
    before = {index: b.pdf(index).stat().st_mtime_ns for index in (1, 2)}
    monkeypatch.setattr(sync_blobs, "LINK_PLACED_FILES", True)

    report = b.sync()

    assert report.captured == 0 and report.published == 0  # the documents did not change
    for index in (1, 2):
        assert os.path.samefile(b.pdf(index), b.blob(b.pdf(index)))
        assert b.pdf(index).stat().st_mtime_ns == before[index]


def test_a_document_that_differs_from_every_blob_stays_as_it_is(tmp_path, phone):
    store = MemoryStore()
    a = Device(tmp_path, "a", store)
    a.add_documents(1)
    a.sync()  # captured: the blob is a copy of the document
    a.pdf(1).write_bytes(a.pdf(1).read_bytes() + b"\n% edited")

    a.sync()

    assert os.path.samefile(a.pdf(1), a.blob(a.pdf(1)))  # its new version was captured, then shared
    assert a.pdf(1).read_bytes().endswith(b"% edited")
