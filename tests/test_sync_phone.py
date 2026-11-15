"""Sync with a phone: this computer serves its own profile folder and syncs against it directly."""

import sqlite3

import pytest

import db
import sync_http
import sync_paths
import sync_phone
import sync_service
from sync_http import HttpStore, pair_with_pin


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


@pytest.fixture
def server(tmp_path):
    addon = str(tmp_path / "desktop")
    phone = sync_phone.PhoneServer(addon, "P", host="127.0.0.1", port=0, discovery_port=None)
    phone.addon = addon
    yield phone
    phone.stop()


def _set(addon, card, value):
    with db.get_connection(addon, "P") as conn:
        conn.execute("INSERT OR REPLACE INTO priorities(card_id, priority) VALUES (?, ?)", (card, value))


def _get(addon, card):
    row = db.get_connection(addon, "P").execute("SELECT priority FROM priorities WHERE card_id=?", (card,)).fetchone()
    return None if row is None else row[0]


def test_the_profile_syncs_through_its_own_phone_folder_and_is_labelled_as_a_phone(server):
    sync_phone.configure(server.addon, "P", query=CARDS)

    status = sync_service.status(server.addon, "P")
    assert status.configured and sync_phone.is_phone_label(status.folder)
    assert sync_phone.folder_of(status.folder) == sync_phone.hub_folder(server.addon, "P")
    assert sync_phone.hub_folder(server.addon, "P").parent == sync_paths.get_sync_dir(server.addon, "P")
    assert not sync_phone.is_phone_label("C:/Cloud") and not sync_phone.is_phone_label("hub:desktop:0a1b2c3d")


def test_a_phone_pairs_with_the_code_and_its_changes_reach_the_desktop(server, tmp_path):
    sync_phone.configure(server.addon, "P", query=CARDS)
    server.start()
    code = server.new_code()
    assert len(code.replace(" ", "")) == 6 and server.code()[0] == code and 0 < server.code()[1] <= 300

    pairing = pair_with_pin(code, "pixel", host="127.0.0.1", port=server.port)
    assert [phone.name for phone in server.phones()] == ["pixel"]
    assert server.phones()[0].seen is None  # paired, not synced yet
    assert server.code() is None  # one code, one phone

    # The phone is another device on the same library, reaching the folder through the server.
    phone_addon = str(tmp_path / "phone")
    store = HttpStore.from_pairing(pairing)
    try:
        sync_service.configure(phone_addon, "P", "hub:pixel:test", query=CARDS, store=store)
        _set(phone_addon, 7, 44.0)
        assert sync_service.sync_now(phone_addon, "P", CARDS, store=store).problems == []
    finally:
        store.close()
    report = sync_service.sync_now(server.addon, "P", CARDS)  # the desktop reads its own folder directly

    assert report.problems == [] and report.skipped is None
    assert _get(server.addon, 7) == 44.0
    pixel = server.phones()[0]
    assert pixel.seen is not None and pixel.changed is not None  # it synced, and it sent a change
    assert server.last_change() == pixel.changed


def test_a_phone_that_only_reads_is_seen_but_sent_no_change(server):
    server.start()
    store = HttpStore.from_pairing(pair_with_pin(server.new_code(), "pixel", host="127.0.0.1", port=server.port))
    try:
        store.listdir("")
    finally:
        store.close()

    pixel = server.phones()[0]
    assert pixel.seen is not None and pixel.changed is None and server.last_change() is None


def test_when_each_phone_last_synced_is_remembered_across_restarts(server):
    server.start()
    store = HttpStore.from_pairing(pair_with_pin(server.new_code(), "pixel", host="127.0.0.1", port=server.port))
    try:
        store.write("hello.txt", b"hi")
    finally:
        store.close()
    before = server.phones()[0]
    server.stop()

    again = sync_phone.PhoneServer(server.addon, "P", host="127.0.0.1", port=0, discovery_port=None)
    assert again.phones() == [before] and again.last_change() == before.changed
    again.remove("pixel")
    assert again.phones() == []


def test_a_removed_phone_is_turned_away_at_once(server):
    server.start()
    pairing = pair_with_pin(server.new_code(), "pixel", host="127.0.0.1", port=server.port)
    assert server.remove("pixel") and server.phones() == []

    store = HttpStore.from_pairing(pairing)
    try:
        with pytest.raises(Exception):
            store.listdir("")
    finally:
        store.close()
    assert not server.remove("pixel")


def test_the_server_is_only_up_while_started_and_keeps_its_phones_across_restarts(server):
    assert not server.running
    with pytest.raises(RuntimeError):
        server.new_code()
    server.start()
    assert server.running and server.code() is None
    pair_with_pin(server.new_code(), "pixel", host="127.0.0.1", port=server.port)

    server.stop()
    assert not server.running
    again = sync_phone.PhoneServer(server.addon, "P", host="127.0.0.1", port=0, discovery_port=None)
    try:
        assert [phone.name for phone in again.phones()] == ["pixel"]
    finally:
        again.stop()


def test_the_keys_are_kept_out_of_the_folder_that_is_served(server):
    server.start()
    pair_with_pin(server.new_code(), "pixel", host="127.0.0.1", port=server.port)

    assert server.book.path.parent == server.folder.parent and server.folder not in server.book.path.parents


def test_a_taken_port_falls_back_to_another_one(tmp_path):
    first = sync_phone.PhoneServer(str(tmp_path / "a"), "P", host="127.0.0.1", port=0, discovery_port=None)
    first.start()
    second = sync_phone.PhoneServer(str(tmp_path / "b"), "P", host="127.0.0.1", port=first.port, discovery_port=None)
    try:
        second.start()
        assert second.running and second.port != first.port
    finally:
        second.stop()
        first.stop()


def test_the_address_is_shown_with_a_port_only_when_it_is_not_the_usual_one(server):
    server.start()
    assert server.address().endswith(f":{server.port}") or server.port == sync_http.DEFAULT_PORT
