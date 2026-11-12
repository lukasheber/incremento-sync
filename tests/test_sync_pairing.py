"""Pairing a device with a short code the hub shows, instead of a long key to copy.

The hub opens a pairing window and shows a six digit code. The device finds the hub (a broadcast the hub answers only
while the window is open), connects with a public pairing key, and proves it knows the code with a value bound to that
very connection; the hub proves it knows it too, then hands over the device's real key inside the same connection.
A man in the middle sees two different connections, so neither proof it relays can verify, and the code is good for a
few attempts only: a wrong guess costs one of three.
"""

import json
import socket
import ssl
import time

import pytest

import sync_http
from sync_http import HttpStore, Hub, KeyBook, Pairing, PairingRefused, pair_with_pin
from sync_store import StoreError


@pytest.fixture
def book(tmp_path):
    return KeyBook(tmp_path / "keys.json")


@pytest.fixture
def hub(tmp_path, book):
    root = tmp_path / "Hub"
    root.mkdir()
    server = Hub(root, book.as_mapping(), host="127.0.0.1", port=0, discovery_port=0, key_book=book)
    server.start()
    yield server
    server.stop()


def _pair(hub, pin, name="tablet", **options):
    return pair_with_pin(pin, name, host="127.0.0.1", port=hub.port, **options)


def test_the_code_is_six_digits_shown_in_two_groups(hub):
    pin = hub.open_pairing()

    assert len(pin) == 6 and pin.isdigit()
    assert sync_http.format_pin(pin) == f"{pin[:3]} {pin[3:]}"


def test_a_device_pairs_with_the_code_and_then_syncs_with_the_key_it_was_given(hub, book):
    pin = hub.open_pairing()

    pairing = _pair(hub, pin)

    assert isinstance(pairing, Pairing) and pairing.identity == "tablet" and pairing.host == "127.0.0.1"
    assert book.devices()["tablet"] == pairing.key  # the hub kept the same key
    store = HttpStore.from_pairing(pairing, discovery_port=None)
    store.write("x/hello.txt", b"hi")
    assert store.read("x/hello.txt", 100) == b"hi"
    store.close()


def test_the_code_can_be_typed_with_spaces_or_dashes(hub):
    pin = hub.open_pairing()

    assert _pair(hub, f"{pin[:3]}-{pin[3:]}").identity == "tablet"


def test_a_code_is_good_for_one_pairing_only(hub):
    pin = hub.open_pairing()
    _pair(hub, pin)

    with pytest.raises(PairingRefused):
        _pair(hub, pin, name="intruder")


def test_a_wrong_code_is_refused_and_pairs_nothing(hub, book):
    hub.open_pairing()

    with pytest.raises(PairingRefused, match="code"):
        _pair(hub, "000000" if hub._pairing.pin != "000000" else "111111")

    assert book.devices() == {}


def test_three_wrong_codes_close_the_window_even_for_the_right_code(hub, book):
    pin = hub.open_pairing()
    wrong = "000000" if pin != "000000" else "111111"
    for _ in range(3):
        with pytest.raises(PairingRefused):
            _pair(hub, wrong)

    with pytest.raises(PairingRefused):
        _pair(hub, pin)

    assert book.devices() == {}


def test_a_window_that_is_not_open_refuses_everyone(hub, book):
    with pytest.raises(PairingRefused):
        _pair(hub, "123456")

    assert book.devices() == {}


def test_the_window_closes_after_its_time(hub):
    pin = hub.open_pairing(seconds=0.2)
    time.sleep(0.5)

    with pytest.raises(PairingRefused):
        _pair(hub, pin)


def test_opening_another_window_replaces_the_code(hub):
    first = hub.open_pairing()
    second = hub.open_pairing()
    if first == second:
        pytest.skip("the two random codes happened to be equal")

    with pytest.raises(PairingRefused):
        _pair(hub, first)
    assert _pair(hub, second).identity == "tablet"


def test_a_name_that_is_taken_gets_a_number(hub, book):
    book.add("tablet")
    pin = hub.open_pairing()

    assert _pair(hub, pin).identity == "tablet-2"
    assert set(book.devices()) == {"tablet", "tablet-2"}


def test_an_unusable_device_name_is_cleaned(hub):
    pin = hub.open_pairing()

    assert _pair(hub, pin, name="Galaxy Tab S9 (Wi-Fi)!").identity == "Galaxy-Tab-S9-Wi-Fi"


def test_the_pairing_key_alone_gets_nothing_but_pairing(hub):
    """The public pairing key cannot read or change the folder, only take part in pairing."""
    hub.open_pairing()
    store = HttpStore("127.0.0.1", hub.port, sync_http.PAIRING_IDENTITY, sync_http.PAIRING_KEY, discovery_port=None, timeout=3)

    with pytest.raises(StoreError):
        store.listdir("")


def test_the_pairing_key_does_not_connect_when_no_window_is_open(hub):
    store = HttpStore("127.0.0.1", hub.port, sync_http.PAIRING_IDENTITY, sync_http.PAIRING_KEY, discovery_port=None, timeout=3)

    with pytest.raises(StoreError):
        store.listdir("")


def test_a_hub_that_does_not_know_the_code_is_not_trusted_by_the_device(tmp_path, book):
    """An impostor that accepts the device's proof cannot answer with the hub's proof."""
    root = tmp_path / "Hub"
    root.mkdir()
    real = Hub(root, book.as_mapping(), host="127.0.0.1", port=0, key_book=book)
    real.start()
    try:
        real.open_pairing()
        real._pairing.skip_proof_check = True  # test hook: accept any device proof, as an impostor would
        real._pairing.pin = "999999"  # ... but the proofs it answers with are made from another code
        with pytest.raises(PairingRefused, match="hub"):
            _pair(real, "123456")
    finally:
        real.stop()


def test_pairing_finds_a_hub_that_is_pairing_by_broadcast_and_ignores_one_that_is_not(tmp_path, book):
    root = tmp_path / "Hub"
    root.mkdir()
    pairing_hub = Hub(root, book.as_mapping(), host="127.0.0.1", port=0, discovery_port=0, key_book=book)
    pairing_hub.start()
    try:
        assert sync_http.find_pairing_hubs(port=pairing_hub.discovery_port, targets=("127.0.0.1",), timeout=0.8) == []
        pin = pairing_hub.open_pairing()
        found = sync_http.find_pairing_hubs(port=pairing_hub.discovery_port, targets=("127.0.0.1",), timeout=1.5)
        assert found == [("127.0.0.1", pairing_hub.port)]

        pairing = pair_with_pin(pin, "tablet", discovery_port=pairing_hub.discovery_port, discovery_targets=("127.0.0.1",))
        assert pairing.port == pairing_hub.port
    finally:
        pairing_hub.stop()


def test_without_a_hub_in_reach_the_device_says_so(tmp_path):
    with pytest.raises(PairingRefused, match="find"):
        pair_with_pin("123456", "tablet", discovery_port=9,
                      discovery_targets=("127.0.0.1",), discovery_timeout=0.5)


def test_a_code_that_is_not_six_digits_is_refused_before_anything_is_sent():
    with pytest.raises(PairingRefused, match="six"):
        pair_with_pin("12345", "tablet", host="127.0.0.1", port=9)


def test_the_text_a_person_types_is_either_a_code_with_an_optional_address_or_a_pairing_code():
    assert sync_http.parse_pairing_input("483 920") == ("483920", None, None)
    assert sync_http.parse_pairing_input("483-920 192.168.1.5") == ("483920", "192.168.1.5", None)
    assert sync_http.parse_pairing_input("483920 192.168.1.5:8800") == ("483920", "192.168.1.5", 8800)
    assert sync_http.parse_pairing_input("192.168.1.5:8800 483920") == ("483920", "192.168.1.5", 8800)
    assert sync_http.parse_pairing_input("ihub1-abc-def") is None
    assert sync_http.parse_pairing_input("hello") is None


def test_a_long_pairing_code_still_works_for_those_who_have_one(hub, book):
    key = book.add("laptop")
    code = Pairing("127.0.0.1", hub.port, "laptop", key).code()

    assert sync_http.resolve_pairing(code, "laptop").identity == "laptop"


def test_the_state_of_the_window_is_reported(hub):
    assert hub.pairing_open is False
    hub.open_pairing()
    assert hub.pairing_open is True
    hub.close_pairing()
    assert hub.pairing_open is False


def test_the_device_proof_is_bound_to_the_connection(hub):
    """A proof made for one connection does not verify on another: relaying it is useless."""
    pin = hub.open_pairing()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(sync_http.CIPHERS)
    context.set_psk_client_callback(lambda hint: (sync_http.PAIRING_IDENTITY, sync_http.PAIRING_KEY))
    with socket.create_connection(("127.0.0.1", hub.port), timeout=5) as raw:
        with context.wrap_socket(raw) as first:
            stale = sync_http._client_proof(pin, first.get_channel_binding("tls-unique"), "tablet")
    with socket.create_connection(("127.0.0.1", hub.port), timeout=5) as raw:
        with context.wrap_socket(raw) as second:
            body = json.dumps({"name": "tablet", "proof": stale}).encode()
            second.sendall(b"POST /v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                           b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            reply = b""
            while chunk := second.recv(4096):
                reply += chunk

    assert b" 403 " in reply.split(b"\r\n")[0]
