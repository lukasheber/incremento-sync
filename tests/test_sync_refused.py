"""One value another device cannot accept must not stop everything else ("still doesn't sync the files").

A pack holds up to 2000 changes. Receiving used to refuse a whole pack over one value it found wrong, remember the
refusal for good, and say so only once: a computer that published one such value (a document Incremento recorded
without its note) left the phone with none of its documents, silently, at every sync.
"""
import sqlite3

import pytest

import db
import paths
import sync_documents
import sync_service
from sync_store import MemoryStore

DOCUMENTS = 5


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


@pytest.fixture(autouse=True)
def _fresh():
    db.close_connection()
    yield
    db.close_connection()


class Device:
    def __init__(self, tmp_path, name, store):
        self.addon, self.store = str(tmp_path / name), store
        sync_service.configure(self.addon, "P", f"store:{name}", query=CARDS, store=store)

    def add_documents(self, count):
        for index in range(1, count + 1):
            pdf = paths.get_pdf_dir(self.addon, "P") / f"book{index}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.7\n" + f"book {index} ".encode() * 500)
            with db.get_connection(self.addon, "P") as conn:
                conn.execute("INSERT INTO content_items(content_id, kind, card_id, note_id, storage_key, created_at, "
                             "updated_at) VALUES (?, 'pdf', ?, ?, ?, 1, 1)",
                             (f"{index:032x}", index, index * 10, f"pdfs/book{index}.pdf"))

    def sync(self):
        report = sync_service.sync_now(self.addon, "P", CARDS, store=self.store)
        while report.more:
            report = sync_service.sync_now(self.addon, "P", CARDS, store=self.store)
        return report

    def placed(self):
        return sorted(p.name for p in paths.get_pdf_dir(self.addon, "P").glob("*.pdf"))

    def device_id(self):
        return sync_service.sync_identity(db.get_connection(self.addon, "P")).device_id


def _refuse_card(monkeypatch, card_id):
    """This device finds the document of ``card_id`` wrong, as a stricter or different version might."""
    original = sync_documents.DocumentFamily.validate

    def validate(self, key, value):
        if key.startswith(f"card:{card_id}/"):
            raise ValueError("document refused here")
        return original(self, key, value)

    monkeypatch.setattr(sync_documents.DocumentFamily, "validate", validate)


def test_a_change_the_receiver_refuses_is_left_out_and_the_rest_arrive(tmp_path, monkeypatch):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)
    a.sync()

    _refuse_card(monkeypatch, 3)
    report = b.sync()
    b.sync()

    assert b.placed() == ["book1.pdf", "book2.pdf", "book4.pdf", "book5.pdf"]
    assert any("document refused here" in problem for problem in report.problems), report.problems
    rejected = db.get_connection(b.addon, "P").execute(
        "SELECT r.key, i.reason FROM sync_inbox i JOIN sync_revisions r ON r.id = i.revision_id "
        "WHERE i.status = 'rejected'").fetchall()
    assert len(rejected) == 1 and rejected[0][0].startswith("card:3/") and "refused here" in rejected[0][1]
    # The refused change is settled: b is not "behind" over it for ever.
    assert sync_service.peer_progress(a.addon, "P", store=store)[b.device_id()].behind == 0


def test_packs_an_earlier_version_refused_whole_are_read_again(tmp_path):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)
    a.sync()
    library = sync_service.sync_identity(db.get_connection(a.addon, "P")).library_id
    a_id = a.device_id()
    packs = [entry.name for entry in store.listdir(f"IncrementoSync/v1/{library}/devices/{a_id}/packs")]
    with db.get_connection(b.addon, "P") as conn:  # as left by 0.3.3 or older: refused for good
        for name in packs:
            conn.execute("INSERT INTO sync_seen_packs(device, name, outcome, format, seen_at) "
                         "VALUES (?, ?, 'rejected', 1, 1)", (a_id, name))

    b.sync()
    b.sync()

    assert b.placed() == [f"book{index}.pdf" for index in range(1, DOCUMENTS + 1)]


def test_a_value_others_would_refuse_is_not_published_and_is_logged(tmp_path, monkeypatch):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)

    _refuse_card(monkeypatch, 3)
    report = a.sync()
    monkeypatch.undo()
    b.sync()
    b.sync()

    assert b.placed() == ["book1.pdf", "book2.pdf", "book4.pdf", "book5.pdf"]
    assert any("card:3/" in problem and "document refused here" in problem for problem in report.problems), \
        report.problems
    log = (sync_service.log_path(a.addon, "P")).read_text(encoding="utf-8")
    assert "card:3/" in log

    a.sync()  # fixed (here: this version accepts it again): it follows
    b.sync()
    b.sync()
    assert b.placed() == [f"book{index}.pdf" for index in range(1, DOCUMENTS + 1)]


def test_a_document_recorded_without_its_note_takes_the_note_of_its_card(tmp_path):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)
    with db.get_connection(a.addon, "P") as conn:
        conn.execute("UPDATE content_items SET note_id = NULL WHERE card_id = 3")

    report = a.sync()
    b.sync()
    b.sync()

    assert report.problems == []
    assert b.placed() == [f"book{index}.pdf" for index in range(1, DOCUMENTS + 1)]
    note = db.get_connection(b.addon, "P").execute("SELECT note_id FROM content_items WHERE card_id = 3").fetchone()
    assert note == (30,)


def test_files_a_device_still_downloads_are_not_counted_as_on_it(tmp_path, monkeypatch):
    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)
    a.sync()
    # A device that started from a snapshot knows every change (its receipt says so) while files still come.
    monkeypatch.setattr(sync_service, "_announced",
                        lambda *args, **kwargs: {b.device_id(): ({a.device_id(): {"epoch": 1, "through": 10 ** 6}}, 2)})

    delivery = dict(sync_service.inventory(a.addon, "P", CARDS, store=store).deliveries)[b.device_id()]

    assert delivery == sync_service.Delivery(have=DOCUMENTS - 2, fetching=2, waiting=0)


def test_a_document_published_without_its_note_by_an_earlier_version_reaches_the_phone_after_updating(tmp_path,
                                                                                                    monkeypatch):
    import sync_exchange

    store = MemoryStore()
    a, b = Device(tmp_path, "a", store), Device(tmp_path, "b", store)
    a.add_documents(DOCUMENTS)
    with db.get_connection(a.addon, "P") as conn:
        conn.execute("UPDATE content_items SET note_id = NULL WHERE card_id = 3")
    # As 0.3.3 did: published with note id 0, which every receiver refuses.
    monkeypatch.setattr(sync_documents.DocumentFamily, "_card_note", lambda self, card_id: None)
    monkeypatch.setattr(sync_exchange, "check_value", lambda *args: None)
    a.sync()
    monkeypatch.undo()

    b.sync()
    b.sync()
    assert b.placed() == ["book1.pdf", "book2.pdf", "book4.pdf", "book5.pdf"]  # the rest is no longer held up

    a.sync()  # the updated computer finds the card's note and sends the document again
    b.sync()
    b.sync()
    assert b.placed() == [f"book{index}.pdf" for index in range(1, DOCUMENTS + 1)]


def test_the_report_says_which_published_changes_other_devices_refuse(tmp_path, monkeypatch):
    import sync_exchange

    store = MemoryStore()
    a = Device(tmp_path, "a", store)
    a.add_documents(DOCUMENTS)
    with db.get_connection(a.addon, "P") as conn:
        conn.execute("UPDATE content_items SET note_id = NULL WHERE card_id = 3")
    monkeypatch.setattr(sync_documents.DocumentFamily, "_card_note", lambda self, card_id: None)
    monkeypatch.setattr(sync_exchange, "check_value", lambda *args: None)
    a.sync()
    monkeypatch.undo()

    report = sync_service.diagnostics(a.addon, "P", CARDS)

    assert "Published changes other devices refuse: 1" in report, report
    assert "card:3/document:pdf: value: document note id" in report
