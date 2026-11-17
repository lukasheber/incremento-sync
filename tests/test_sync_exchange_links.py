"""On the computer that serves its phones, the blob store and the phone folder share each file.

Each document was on the computer three times: the document, its blob (the sync store's checked copy) and the copy in
the folder the phone downloads from. Blobs are named by their hash and never written in place, in the store or in the
phone folder, so for the computer's own phone folder the two are one file (a hard link). The document itself stays a
copy, since other programs may rewrite it in place. A cloud folder keeps copies: its sync client might rewrite a
file in place.
"""
import os
import shutil
import sqlite3

import pytest

import db
import paths
import sync_paths
import sync_phone
import sync_service
from sync_blobs import blob_path, exchange_blob_rel
from sync_exchange import library_rel
from sync_store import LocalStore


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


def _add_documents(addon, start, count):
    for index in range(start, start + count):
        pdf = paths.get_pdf_dir(addon, "P") / f"book{index}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.7\n" + f"book {index} ".encode() * 3000)
        with db.get_connection(addon, "P") as conn:
            conn.execute("INSERT INTO content_items(content_id, kind, card_id, note_id, storage_key, created_at, "
                         "updated_at) VALUES (?, 'pdf', ?, ?, ?, 1, 1)",
                         (f"{index:032x}", index, index * 10, f"pdfs/book{index}.pdf"))


def _pairs(addon, folder):
    """(local blob, exchange copy) for every local blob."""
    library = sync_service.sync_identity(db.get_connection(addon, "P")).library_id
    store = sync_paths.get_sync_blob_dir(addon, "P")
    pairs = []
    for blob in sorted(store.glob("??/*")):
        pairs.append((blob, folder / exchange_blob_rel(library_rel(library), blob.name)))
    return pairs


def test_the_computer_and_its_phone_folder_share_each_file(tmp_path):
    computer = str(tmp_path / "computer")
    sync_phone.configure(computer, "P", query=CARDS)
    _add_documents(computer, 1, 3)

    sync_service.sync_now(computer, "P", CARDS)

    pairs = _pairs(computer, sync_phone.hub_folder(computer, "P"))
    assert len(pairs) == 3
    for blob, copy in pairs:
        assert os.path.samefile(blob, copy)
    for index in range(1, 4):  # the documents themselves stay apart
        pdf = paths.get_pdf_dir(computer, "P") / f"book{index}.pdf"
        assert not any(os.path.samefile(pdf, blob) for blob, _ in pairs)


def test_a_phone_still_receives_whole_documents_from_the_shared_files(tmp_path):
    computer = str(tmp_path / "computer")
    sync_phone.configure(computer, "P", query=CARDS)
    _add_documents(computer, 1, 2)
    sync_service.sync_now(computer, "P", CARDS)
    phone = str(tmp_path / "phone")
    store = LocalStore(sync_phone.hub_folder(computer, "P"))  # what the hub serves
    sync_service.configure(phone, "P", "store:phone", query=CARDS, store=store)

    sync_service.sync_now(phone, "P", CARDS, store=store)

    for index in (1, 2):
        name = f"book{index}.pdf"
        assert (paths.get_pdf_dir(phone, "P") / name).read_bytes() == (paths.get_pdf_dir(computer, "P") / name).read_bytes()


def test_a_document_from_the_phone_is_one_file_in_the_phone_folder_and_the_computers_store(tmp_path):
    computer = str(tmp_path / "computer")
    sync_phone.configure(computer, "P", query=CARDS)
    sync_service.sync_now(computer, "P", CARDS)
    phone = str(tmp_path / "phone")
    store = LocalStore(sync_phone.hub_folder(computer, "P"))
    sync_service.configure(phone, "P", "store:phone", query=CARDS, store=store)
    _add_documents(phone, 5, 1)
    sync_service.sync_now(phone, "P", CARDS, store=store)

    sync_service.sync_now(computer, "P", CARDS)

    assert (paths.get_pdf_dir(computer, "P") / "book5.pdf").is_file()
    pairs = _pairs(computer, sync_phone.hub_folder(computer, "P"))
    assert pairs and all(os.path.samefile(blob, copy) for blob, copy in pairs)


def test_copies_made_before_become_one_file_at_the_next_sync(tmp_path):
    computer = str(tmp_path / "computer")
    sync_phone.configure(computer, "P", query=CARDS)
    _add_documents(computer, 1, 2)
    sync_service.sync_now(computer, "P", CARDS)
    pairs = _pairs(computer, sync_phone.hub_folder(computer, "P"))
    for blob, copy in pairs:  # as an earlier version left them: two copies
        copy.unlink()
        shutil.copyfile(blob, copy)
    with db.get_connection(computer, "P") as conn:
        conn.execute("DELETE FROM sync_meta WHERE key='exchange_shared'")

    sync_service.sync_now(computer, "P", CARDS)

    for blob, copy in pairs:
        assert os.path.samefile(blob, copy) and copy.read_bytes() == blob.read_bytes()


def test_a_cloud_folder_keeps_separate_copies(tmp_path):
    computer, cloud = str(tmp_path / "computer"), tmp_path / "Dropbox"
    cloud.mkdir()
    sync_service.configure(computer, "P", str(cloud), query=CARDS)
    _add_documents(computer, 1, 2)

    sync_service.sync_now(computer, "P", CARDS)

    pairs = _pairs(computer, cloud)
    assert pairs and not any(os.path.samefile(blob, copy) for blob, copy in pairs)
