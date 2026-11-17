"""On a phone, a synced document takes its space once, not twice.

The engine keeps every file it moves in its blob store (named by its hash) and places a copy where the document lives,
so a phone held each document twice: 2.7 GB of documents took 5.4 GB. Android does not let an app hard-link files, so
on a phone (``KEEP_ONE_COPY``) a blob is removed once its document is in place with the same content, and made again
from that document if it is ever needed. Blobs something still needs stay: changes waiting to be placed, this
device's changes not yet sent, and the versions of an open conflict. A computer keeps every blob, as before.
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

    def blob(self, data: bytes):
        return sync_blobs.blob_path(sync_service.sync_paths.get_sync_blob_dir(self.addon, "P"),
                                    hashlib.sha256(data).hexdigest())

    def blobs(self):
        store = sync_service.sync_paths.get_sync_blob_dir(self.addon, "P")
        return sorted(path.name for path in store.glob("??/*")) if store.is_dir() else []

    def save(self, index, data):
        """As Incremento saves a changed document: a new file renamed over the old one."""
        replacement = self.pdf(index).with_name(f"book{index}.pdf.tmp")
        replacement.write_bytes(data)
        os.replace(replacement, self.pdf(index))

    def sync(self, budget=None):
        report = sync_service.sync_now(self.addon, "P", CARDS, store=self.store, budget_seconds=budget)
        assert report.problems == [] and report.skipped is None, (report.problems, report.skipped)
        return report


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


def _phone(monkeypatch, on=True):
    monkeypatch.setattr(sync_blobs, "KEEP_ONE_COPY", on)


def _two(tmp_path, count=3):
    store = MemoryStore()
    computer, phone = Device(tmp_path, "computer", store), Device(tmp_path, "phone", store)
    computer.add_documents(count)
    computer.sync()
    return computer, phone


def test_a_phone_keeps_one_copy_of_each_received_document(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path)
    _phone(monkeypatch)

    phone.sync()

    assert [phone.pdf(i).read_bytes() for i in (1, 2, 3)] == [computer.pdf(i).read_bytes() for i in (1, 2, 3)]
    assert phone.blobs() == []
    assert len(computer.blobs()) == 3  # a computer keeps them


def test_files_still_waiting_to_be_placed_keep_their_blobs(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path, 4)
    _phone(monkeypatch)

    report = phone.sync(budget=0.0)  # some files arrive, not all are placed yet
    placed = [i for i in (1, 2, 3, 4) if phone.pdf(i).is_file()]

    assert report.more
    for blob in phone.blobs():  # what is kept is what waits
        assert not any(hashlib.sha256(phone.pdf(i).read_bytes()).hexdigest() == blob for i in placed)
    while phone.sync(budget=0.0).more:
        pass
    assert phone.blobs() == [] and all(phone.pdf(i).is_file() for i in (1, 2, 3, 4))


def test_a_document_changed_on_the_phone_is_sent_and_then_kept_once(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path, 1)
    _phone(monkeypatch)
    phone.sync()
    changed = phone.pdf(1).read_bytes() + b"\n% highlighted on the phone"
    phone.save(1, changed)

    report = phone.sync()
    computer.sync()

    assert report.published >= 1
    assert computer.pdf(1).read_bytes() == changed
    assert phone.blobs() == []  # sent, so its blob went too


def test_a_change_not_yet_sent_keeps_its_blob(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path, 1)
    _phone(monkeypatch)
    phone.sync()
    changed = phone.pdf(1).read_bytes() + b"\n% offline"
    phone.save(1, changed)

    monkeypatch.setattr(sync_service, "publish", lambda *args, **kwargs: sync_service.PublishResult())  # hub away
    sync_service.sync_now(phone.addon, "P", CARDS, store=phone.store)

    assert phone.blob(changed).name in phone.blobs()  # captured, waiting to be sent


def test_a_new_version_from_the_computer_replaces_the_one_on_the_phone(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path, 1)
    _phone(monkeypatch)
    phone.sync()
    newer = computer.pdf(1).read_bytes() + b"\n% annotated on the computer"
    computer.save(1, newer)
    computer.sync()

    phone.sync()

    assert phone.pdf(1).read_bytes() == newer and phone.blobs() == []


def test_a_blob_needed_again_is_made_from_the_document_on_the_phone_not_downloaded(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path, 1)
    _phone(monkeypatch)
    phone.sync()
    data = phone.pdf(1).read_bytes()
    library = sync_service.sync_identity(db.get_connection(phone.addon, "P")).library_id
    downloads = []
    real = sync_blobs.fetch_exchange_blob
    monkeypatch.setattr(sync_service, "fetch_exchange_blob",
                        lambda *args, **kwargs: downloads.append(args[2]) or real(*args, **kwargs))
    import sync_exchange
    monkeypatch.setattr(sync_exchange, "fetch_exchange_blob",
                        lambda *args, **kwargs: downloads.append(args[2]) or real(*args, **kwargs))

    assert sync_blobs.restore_from_placed(sync_service.sync_paths.get_sync_blob_dir(phone.addon, "P"),
                                          hashlib.sha256(data).hexdigest())
    assert phone.blob(data).read_bytes() == data and downloads == []
    assert library  # the library is untouched


def test_on_a_computer_every_blob_is_kept(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path)
    _phone(monkeypatch, on=False)

    phone.sync()

    assert len(phone.blobs()) == 3


def test_copies_made_before_are_removed_at_the_next_sync_on_a_phone(tmp_path, monkeypatch):
    computer, phone = _two(tmp_path, 2)
    _phone(monkeypatch, on=False)
    phone.sync()  # an earlier version kept both copies
    assert len(phone.blobs()) == 2
    _phone(monkeypatch)

    report = phone.sync()

    assert report.captured == 0 and report.published == 0 and phone.blobs() == []
    assert all(phone.pdf(i).read_bytes() == computer.pdf(i).read_bytes() for i in (1, 2))
