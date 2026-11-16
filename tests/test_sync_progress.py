"""A big sync says how far it is: files prepared, sent and received so far, and how many are left.

A first sync of a large library copies every document before any of it can reach another device, and can take long
enough that, without word of its progress, it looks as if nothing is happening.
"""
import sqlite3

import pytest

import db
import paths
import sync_service
from sync_store import MemoryStore

DOCUMENTS = 5


def _collection(cards, usn=20):
    col = sqlite3.connect(":memory:", check_same_thread=False)
    col.execute("CREATE TABLE col (usn INTEGER, scm INTEGER, mod INTEGER DEFAULT 1)")
    col.execute("INSERT INTO col(usn, scm) VALUES (?, 1000)", (usn,))
    col.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT)")
    col.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, usn INTEGER)")
    for card_id in cards:
        col.execute("INSERT INTO notes VALUES (?, ?)", (card_id * 10, f"g{card_id}"))
        col.execute("INSERT INTO cards VALUES (?, ?, 5)", (card_id, card_id * 10))
    return lambda sql, *args: col.execute(sql, args).fetchall()


CARDS = _collection(range(1, 30))


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


class Device:
    def __init__(self, tmp_path, name, store):
        self.addon, self.store = str(tmp_path / name), store
        sync_service.configure(self.addon, "P", f"store:{name}", query=CARDS, store=store)

    def add_documents(self, count, size=3000):
        for index in range(1, count + 1):
            pdf = paths.get_pdf_dir(self.addon, "P") / f"book{index}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.7\n" + f"book {index} ".encode() * size)
            with db.get_connection(self.addon, "P") as conn:
                conn.execute("INSERT INTO content_items(content_id, kind, card_id, note_id, storage_key, created_at, "
                             "updated_at) VALUES (?, 'pdf', ?, ?, ?, 1, 1)",
                             (f"{index:032x}", index, index * 10, f"pdfs/book{index}.pdf"))
        with db.get_connection(self.addon, "P") as conn:
            conn.execute("INSERT INTO priorities(card_id, priority) VALUES (1, 7.0)")

    def sync(self, budget=None):
        heard = []
        report = sync_service.sync_now(self.addon, "P", CARDS, store=self.store, budget_seconds=budget,
                                       on_progress=heard.append)
        assert report.problems == [] and report.skipped is None, (report.problems, report.skipped)
        return report, heard


def _stage(heard, stage):
    return [(p.done, p.total) for p in heard if p.stage == stage]


def test_a_first_sync_counts_the_files_it_prepares_and_sends(tmp_path):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(DOCUMENTS)

    report, heard = a.sync()

    assert _stage(heard, "preparing") == [(n, DOCUMENTS) for n in range(DOCUMENTS + 1)]
    assert _stage(heard, "sending") == [(n, DOCUMENTS) for n in range(DOCUMENTS + 1)]
    assert report.files_to_send == 0 and report.bytes_to_send == 0


def test_receiving_counts_the_files_still_to_fetch(tmp_path):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)
    a.sync()

    report, heard = b.sync()

    assert _stage(heard, "receiving") == [(n, DOCUMENTS) for n in range(DOCUMENTS + 1)]
    assert report.files_to_receive == 0


def test_a_sync_cut_short_says_how_many_files_are_left(tmp_path):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)

    left = []
    while True:
        report, _ = a.sync(budget=0.0)  # a file or two, then the time is up
        left.append(report.files_to_send)
        assert (report.bytes_to_send > 0) == (report.files_to_send > 0)
        if not report.more:
            break
    assert 0 < left[0] < DOCUMENTS and left == sorted(left, reverse=True) and left[-1] == 0

    first, heard = b.sync(budget=0.0)
    assert first.more and 0 < first.files_to_receive < DOCUMENTS
    assert _stage(heard, "receiving")[0] == (0, DOCUMENTS)


def test_a_sync_with_no_files_reports_no_file_stages(tmp_path):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(DOCUMENTS)
    a.sync()
    with db.get_connection(a.addon, "P") as conn:
        conn.execute("UPDATE priorities SET priority=3.0 WHERE card_id=1")

    report, heard = a.sync()

    assert report.published == 1
    assert heard == []


def test_the_status_says_how_many_files_wait_to_be_sent(tmp_path):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(DOCUMENTS)
    report, _ = a.sync(budget=0.0)

    status = sync_service.status(a.addon, "P")

    assert 0 < status.files_to_send == report.files_to_send < DOCUMENTS
    assert status.bytes_to_send == report.bytes_to_send > 0 and status.files_to_receive == 0


def test_a_failing_progress_listener_does_not_stop_the_sync(tmp_path):
    a = Device(tmp_path, "a", MemoryStore())
    a.add_documents(DOCUMENTS)

    def broken(_progress):
        raise RuntimeError("window closed")

    report = sync_service.sync_now(a.addon, "P", CARDS, store=a.store, on_progress=broken)

    assert report.published >= DOCUMENTS
