"""The exchange over the local network: a hub that serves one folder, and the engine's Store over it.

A hub is a file server over the folder that would otherwise be a cloud folder, so devices without a shared folder (a
phone and a computer on one network) can still sync. It understands no Incremento data: the engine on every device
validates everything it reads, exactly as for a cloud folder, so a hub cannot make a device accept bad data.

The channel is TLS with a pre-shared key, the pairing secret:

* encrypted and forward secret (``ECDHE-PSK-CHACHA20-POLY1305``), and each side proves it holds the key, so a device
  knows it talks to its own hub and the hub knows the device is paired;
* no certificate to create, trust, pin or renew, and no dependency beyond Python's ``ssl`` (3.13 or later);
* one identity and key per paired device, so a lost device is removed by deleting its key.

Wire protocol (all under ``/v1``, the path in the ``path`` query parameter, ``/``-separated and relative)::

    GET    /v1/list?path=<folder>   JSON [{name, dir, size, mtime}]; a missing folder is []
    GET    /v1/stat?path=<rel>      JSON {name, dir, size, mtime}, or 404
    GET    /v1/file?path=<rel>      the bytes, or 404
    PUT    /v1/file?path=<rel>      replace the file atomically; Content-Length required, 204
    DELETE /v1/file?path=<rel>      204, also when there is nothing to delete

The hub refuses unsafe paths, bodies over ``MAX_BODY_BYTES`` and uploads that are cut short, and it never shows a
symbolic link. A paired device can write anywhere in the folder, as it can in a cloud folder; clients check what
they read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import http.server
import json
import re
import secrets
import shutil
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

try:
    from .sync_store import Entry, LocalStore, Store, StoreError, as_store, clean
except ImportError:
    from sync_store import Entry, LocalStore, Store, StoreError, as_store, clean  # type: ignore[no-redef]

#: Forward secret, authenticated encryption; the second is for a Python whose OpenSSL lacks ChaCha20.
CIPHERS = "ECDHE-PSK-CHACHA20-POLY1305:PSK-AES256-GCM-SHA384"
MAX_BODY_BYTES = 512 * 1024 * 1024
MAX_LISTING_BYTES = 16 * 1024 * 1024
HANDSHAKE_SECONDS = 10.0
#: How long to wait for a hub to answer at all; a hub that is on answers at once, and one that is off should say so soon.
CONNECT_SECONDS = 8.0
IDLE_SECONDS = 60.0
_IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Below a device folder only the device that first wrote there may write or delete.
_DEVICE_FOLDER = re.compile(r"^[^/]+/v[0-9]+/[^/]+/devices/([^/]+)/")
DEFAULT_MAX_CONNECTIONS = 64
DISCOVERY_PORT = 8766
_DISCOVERY_REQUEST = b"IHUB1 "
_DISCOVERY_REPLY = b"IHUB1R "
_HEX = re.compile(rb"^[0-9a-f]{16,64}$")
DEFAULT_PORT = 8765
PAIRING_SECONDS = 300.0
PAIRING_ATTEMPTS = 3
#: Not a secret: it only gives the pairing exchange an encrypted channel; the code decides who may pair.
PAIRING_IDENTITY = "ihub-pairing"
PAIRING_KEY = hashlib.sha256(b"incremento-hub-pairing-v1").digest()
_PAIR_QUESTION = b"IHUB1P "
_PAIR_ANSWER = b"IHUB1PR "
_PIN = re.compile(r"^[0-9]{6}$")
_handshake_identity = threading.local()
_CHUNK = 256 * 1024


def require_psk() -> None:
    """TLS pre-shared keys need Python 3.13's ssl module (Anki 25 and the phone have it)."""
    if not getattr(ssl, "HAS_PSK", False):
        raise StoreError("Syncing over the network needs Python 3.13 or later (TLS pre-shared keys).")


def new_key() -> bytes:
    """A pairing key: 256 random bits."""
    return secrets.token_bytes(32)


@dataclass(frozen=True)
class Pairing:
    """What a device needs to reach a hub, as one string to copy or scan."""

    host: str
    port: int
    identity: str
    key: bytes

    def code(self) -> str:
        payload = json.dumps([1, self.host, self.port, self.identity, self.key.hex()], separators=(",", ":"))
        body = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return f"ihub1-{body}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:6]}"

    @classmethod
    def from_code(cls, code: str) -> Pairing:
        try:
            prefix, body, check = code.strip().split("-")
            if prefix != "ihub1":
                raise ValueError
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
            if hashlib.sha256(payload.encode("utf-8")).hexdigest()[:6] != check:
                raise ValueError
            version, host, port, identity, key = json.loads(payload)
            if (version != 1 or not isinstance(host, str) or not host or not isinstance(port, int)
                    or isinstance(port, bool) or not 0 < port < 65536 or not isinstance(identity, str)
                    or not _IDENTITY.match(identity) or not isinstance(key, str)):
                raise ValueError
            raw = bytes.fromhex(key)
            if len(raw) != 32:
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("This is not a valid hub pairing code.") from None
        return cls(host, port, identity, raw)


# --- Finding the hub on the network -----------------------------------------------------------------------------
# The pairing code names the hub's address, but a home network may give the hub another one. A device broadcasts a
# question naming itself; a hub that holds that device's key answers with its port and a proof computed from the key
# and the device's one-time nonce. The question reveals nothing, a hub does not answer devices it does not know, and
# an answer that is not provably from a holder of the key is ignored.

def _proof(key: bytes, nonce: bytes, port: bytes) -> bytes:
    return hmac.new(key, b"ihub-discovery|" + nonce + b"|" + port, hashlib.sha256).hexdigest().encode("ascii")


def discover(identity: str, key: bytes, *, port: int = DISCOVERY_PORT,
             targets: tuple[str, ...] = ("255.255.255.255",), timeout: float = 1.5) -> tuple[str, int] | None:
    """The (address, port) of the hub that holds ``key`` for ``identity``, or None if none answers in time."""
    nonce = secrets.token_hex(16).encode("ascii")
    question = _DISCOVERY_REQUEST + identity.encode("ascii", "replace") + b" " + nonce
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.1)
        deadline = time.monotonic() + timeout
        next_question = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_question:
                for target in targets:
                    try:
                        sock.sendto(question, (target, port))
                    except OSError:
                        pass
                next_question = time.monotonic() + 0.5
            try:
                data, address = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                continue
            if not data.startswith(_DISCOVERY_REPLY):
                continue
            fields = data[len(_DISCOVERY_REPLY):].split(b" ")
            if len(fields) != 2 or not fields[0].isdigit() or not 0 < int(fields[0]) < 65536:
                continue
            if hmac.compare_digest(fields[1], _proof(key, nonce, fields[0])):
                return str(address[0]), int(fields[0])
    finally:
        sock.close()
    return None


# --- Pairing with a short code ----------------------------------------------------------------------------------
# The hub shows six digits. The device broadcasts a question that a hub answers only while it is pairing, connects
# with the public pairing key (an encrypted channel, nothing more), and sends a proof of the code made from that very
# connection (TLS channel binding), so it cannot be replayed on another one. The hub checks it, answers with its own
# proof, and hands over the device's real key through the same connection. Someone in the middle holds two different
# connections, so neither proof they relay verifies, and the code is good for three wrong tries and a few minutes.

class PairingRefused(StoreError):
    """Pairing did not happen; the message says why in words a person can act on."""


def format_pin(pin: str) -> str:
    return f"{pin[:3]} {pin[3:]}"


def _client_proof(pin: str, binding: bytes, name: str) -> str:
    return hmac.new(pin.encode("ascii"), b"ihub-pair-device|" + binding + b"|" + name.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _hub_proof(pin: str, binding: bytes, name: str, key: bytes) -> str:
    return hmac.new(pin.encode("ascii"), b"ihub-pair-hub|" + binding + b"|" + name.encode("utf-8") + b"|" + key,
                    hashlib.sha256).hexdigest()


def parse_pairing_input(text: str) -> tuple[str, str | None, int | None] | None:
    """What a person typed: a six digit code, optionally with the hub's address (``483 920 192.168.1.5[:8765]``,
    in either order), as ``(code, host, port)``; None when it is anything else (a long pairing code, for instance)."""
    tokens = text.replace(",", " ").split()
    pin_parts: list[str] = []
    address: str | None = None
    for token in tokens:
        if re.fullmatch(r"[0-9]{1,6}(-[0-9]{1,6})?", token) and "." not in token and ":" not in token:
            pin_parts.append(token.replace("-", ""))
        elif re.fullmatch(r"[A-Za-z0-9.\-]+(:[0-9]{1,5})?", token) and address is None and not token.startswith("ihub1-"):
            address = token
        else:
            return None
    pin = "".join(pin_parts)
    if not _PIN.match(pin):
        return None
    if address is None:
        return pin, None, None
    host, _, port = address.partition(":")
    if not host or (port and not 0 < int(port) < 65536):
        return None
    return pin, host, int(port) if port else None


def find_pairing_hubs(*, port: int = DISCOVERY_PORT, targets: tuple[str, ...] = ("255.255.255.255",),
                      timeout: float = 2.0) -> list[tuple[str, int]]:
    """The hubs on the network that are showing a pairing code, as (address, port)."""
    question = _PAIR_QUESTION + secrets.token_hex(8).encode("ascii")
    found: list[tuple[str, int]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.1)
        deadline = time.monotonic() + timeout
        next_question = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_question:
                for target in targets:
                    try:
                        sock.sendto(question, (target, port))
                    except OSError:
                        pass
                next_question = time.monotonic() + 0.5
            try:
                data, address = sock.recvfrom(256)
            except (socket.timeout, OSError):
                continue
            if data.startswith(_PAIR_ANSWER) and data[len(_PAIR_ANSWER):].isdigit():
                where = (str(address[0]), int(data[len(_PAIR_ANSWER):]))
                if 0 < where[1] < 65536 and where not in found:
                    found.append(where)
    finally:
        sock.close()
    return found


def _pair_once(pin: str, name: str, host: str, port: int, timeout: float) -> Pairing:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(CIPHERS)
    context.set_psk_client_callback(lambda hint: (PAIRING_IDENTITY, PAIRING_KEY))  # type: ignore[attr-defined]
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=timeout)
    try:
        try:
            connection.connect()
            binding = connection.sock.get_channel_binding("tls-unique")  # type: ignore[union-attr]
            body = json.dumps({"name": name, "proof": _client_proof(pin, binding, name)}).encode("utf-8")
            connection.request("POST", "/v1/pair", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read(65536)
        except ssl.SSLError as error:
            raise PairingRefused("The hub is not showing a code. Press Enter in the hub's window to show one.") from error
        except (OSError, http.client.HTTPException) as error:
            raise PairingRefused(f"Could not reach the hub at {host}:{port} ({error}).") from error
        try:
            reply = json.loads(data.decode("utf-8")) if data else {}
        except ValueError:
            reply = {}
        if response.status == 403:
            if reply.get("error") == "wrong code":
                raise PairingRefused("That code is not right.")
            raise PairingRefused("The hub is not showing a code. Press Enter in the hub's window to show one.")
        if response.status != 200:
            raise PairingRefused(f"The hub refused to pair (HTTP {response.status}).")
        try:
            given, key, answered = str(reply["name"]), bytes.fromhex(reply["key"]), str(reply["proof"])
        except (KeyError, ValueError, TypeError):
            raise PairingRefused("The hub's answer was not understood.") from None
        if not _IDENTITY.match(given) or len(key) != 32 or not hmac.compare_digest(
                answered, _hub_proof(pin, binding, given, key)):
            raise PairingRefused("The hub did not prove that it knows the code, so nothing was paired.")
        return Pairing(host, port, given, key)
    finally:
        connection.close()


def pair_with_pin(pin: str, name: str, *, host: str | None = None, port: int | None = None,
                  discovery_port: int = DISCOVERY_PORT, discovery_targets: tuple[str, ...] = ("255.255.255.255",),
                  discovery_timeout: float = 2.5, timeout: float = 10.0) -> Pairing:
    """Pair this device, to be called ``name``, with the hub that shows ``pin``; returns the pairing to keep.

    Without ``host`` the hubs that are showing a code are found by broadcast. Raises ``PairingRefused``."""
    require_psk()
    digits = re.sub(r"[\s-]", "", pin)
    if not _PIN.match(digits):
        raise PairingRefused("The code is six digits.")
    if host:
        candidates = [(host, port or DEFAULT_PORT)]
    else:
        candidates = find_pairing_hubs(port=discovery_port, targets=discovery_targets, timeout=discovery_timeout)
        if not candidates:
            raise PairingRefused("Could not find a hub that is showing a code. Is this device on the same network as "
                                 "the hub? You can also type the hub's address after the code.")
    problems: list[PairingRefused] = []
    for where in candidates:
        try:
            return _pair_once(digits, name, where[0], where[1], timeout)
        except PairingRefused as refused:
            problems.append(refused)
    raise problems[0]


def resolve_pairing(text: str, name: str, **options: Any) -> Pairing:
    """A pairing from what a person typed: a six digit code (with an optional address), or a long pairing code."""
    parsed = parse_pairing_input(text)
    if parsed is None:
        return Pairing.from_code(text)
    pin, host, port = parsed
    return pair_with_pin(pin, name, host=host, port=port, **options)


class _PairingWindow:
    def __init__(self, pin: str, expires: float):
        self.pin = pin
        self.expires = expires
        self.attempts_left = PAIRING_ATTEMPTS
        self.skip_proof_check = False  # tests only


# --- The paired devices ----------------------------------------------------------------------------------------

class KeyBook:
    """The hub's list of paired devices and their keys, in a file that is not in the folder the hub serves."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def devices(self) -> dict[str, bytes]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise ValueError(f"The hub's keys file {self.path} cannot be read: {error}") from error
        try:
            devices = {str(name): bytes.fromhex(key) for name, key in raw["devices"].items()}
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"The hub's keys file {self.path} is damaged.") from error
        return devices

    def _save(self, devices: Mapping[str, bytes]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": 1, "devices": {name: key.hex() for name, key in sorted(devices.items())}},
                          indent=2)
        handle, name = tempfile.mkstemp(dir=self.path.parent, prefix=".keys-", suffix=".tmp")
        try:
            with open(handle, "w", encoding="utf-8") as out:  # mkstemp's file is already private to the owner
                out.write(text)
            Path(name).replace(self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def add(self, name: str) -> bytes:
        """Pair a new device: returns its key, which is shown once and never needs to be shown again."""
        if not _IDENTITY.match(name):
            raise ValueError("A device name is 1 to 64 letters, digits, dashes or underscores.")
        devices = self.devices()
        if name in devices:
            raise ValueError(f"A device called {name!r} is already paired.")
        devices[name] = new_key()
        self._save(devices)
        return devices[name]

    def revoke(self, name: str) -> bool:
        devices = self.devices()
        if name not in devices:
            return False
        del devices[name]
        self._save(devices)
        return True

    def as_mapping(self) -> Mapping[str, bytes]:
        """A live view for a running hub: devices paired or revoked later take effect at once."""
        return _LiveKeys(self)


class _LiveKeys(Mapping[str, bytes]):
    def __init__(self, book: KeyBook):
        self._book = book
        self._seen: tuple[float, int] | None = None
        self._devices: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def _current(self) -> dict[str, bytes]:
        with self._lock:
            try:
                info = self._book.path.stat()
                stamp: tuple[float, int] | None = (info.st_mtime, info.st_size)
            except OSError:
                stamp = None
            if stamp != self._seen:
                try:
                    self._devices = self._book.devices()
                except ValueError:
                    self._devices = {}  # a damaged file admits nobody until it is repaired
                self._seen = stamp
            return self._devices

    def __getitem__(self, name: str) -> bytes:
        return self._current()[name]

    def __iter__(self) -> Any:
        return iter(self._current())

    def __len__(self) -> int:
        return len(self._current())


# --- The hub ---------------------------------------------------------------------------------------------------

class _HandshakeFailed(Exception):
    pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        pass

    def setup(self) -> None:
        # The handshake happens here, in the connection's own thread, so one stalled client cannot hold up the rest.
        sock: ssl.SSLSocket = self.request
        sock.settimeout(HANDSHAKE_SECONDS)
        try:
            sock.do_handshake()
        except (ssl.SSLError, OSError) as error:
            raise _HandshakeFailed from error
        sock.settimeout(IDLE_SECONDS)
        # The key lookup ran in this thread during the handshake and noted who is connecting.
        self.identity: str | None = getattr(_handshake_identity, "value", None)
        _handshake_identity.value = None
        super().setup()

    # -- helpers --

    def _reply(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(self, status: int, message: str) -> None:
        # A body we did not read would be taken for the next request.
        self.close_connection = True
        self._reply(status, json.dumps({"error": message}).encode("utf-8"))

    def _target(self) -> tuple[str, str] | None:
        parts = urlsplit(self.path)
        rel = parse_qs(parts.query, keep_blank_values=True).get("path", [""])[0]
        try:
            clean(rel)
        except StoreError:
            self._refuse(400, "unsafe path")
            return None
        return parts.path, rel

    def _may_change(self, rel: str) -> bool:
        if self.server.may_write(self.identity, rel):
            return True
        self._refuse(403, "this device may not change another device's folder")
        return False

    @staticmethod
    def _entry(entry: Entry) -> dict[str, Any]:
        return {"name": entry.name, "dir": entry.is_dir, "size": entry.size, "mtime": entry.mtime}

    # -- methods --

    def _pairing_only(self) -> bool:
        """The public pairing key may take part in pairing and nothing else."""
        if self.identity != PAIRING_IDENTITY:
            return False
        self._refuse(403, "this key is for pairing only")
        return True

    def do_POST(self) -> None:  # noqa: N802
        if self.identity != PAIRING_IDENTITY or urlsplit(self.path).path != "/v1/pair":
            self._refuse(404, "unknown route")
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= 4096:
                raise ValueError
            asked = json.loads(self.rfile.read(length).decode("utf-8"))
            name, proof = str(asked["name"]), str(asked["proof"])
        except (ValueError, KeyError, TypeError, UnicodeError):
            self._refuse(400, "bad pairing request")
            return
        binding = self.request.get_channel_binding("tls-unique")
        status, answer = self.server.hub.handle_pairing(name, proof, binding)  # type: ignore[attr-defined]
        if status != 200:
            self.close_connection = True
        self._reply(status, json.dumps(answer).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - the base class's naming
        if self._pairing_only():
            return
        target = self._target()
        if target is None:
            return
        route, rel = target
        store = self.server.store
        try:
            if route == "/v1/list":
                entries = [self._entry(e) for e in store.listdir(rel) if not e.is_symlink]
                self._reply(200, json.dumps(entries).encode("utf-8"))
            elif route == "/v1/stat":
                found = store.stat(rel)
                if found is None:
                    self._reply(404, b"{}")
                else:
                    self._reply(200, json.dumps(self._entry(found)).encode("utf-8"))
            elif route == "/v1/file":
                self._send_file(rel)
            else:
                self._refuse(404, "unknown route")
        except StoreError as error:
            self._refuse(400, str(error))
        except OSError as error:
            self._refuse(500, str(error))

    def _send_file(self, rel: str) -> None:
        store = self.server.store
        found = store.stat(rel)
        if found is None or found.is_dir:
            self._reply(404, b"{}")
            return
        if found.size > MAX_BODY_BYTES:
            self._refuse(413, "file too large")
            return
        with tempfile.TemporaryDirectory(prefix="incremento-hub-") as spool:
            copy = Path(spool) / "file"
            store.download(rel, copy, MAX_BODY_BYTES)
            size = copy.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with copy.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, _CHUNK)

    def do_PUT(self) -> None:  # noqa: N802
        if self._pairing_only():
            return
        target = self._target()
        if target is None:
            return
        route, rel = target
        if route != "/v1/file" or not rel:
            self._refuse(400 if route == "/v1/file" else 404, "a file path is required")
            return
        if not self._may_change(rel):
            return
        length = self.headers.get("Content-Length")
        if length is None:
            self._refuse(411, "Content-Length required")
            return
        try:
            expected = int(length)
            if expected < 0:
                raise ValueError
        except ValueError:
            self._refuse(400, "bad Content-Length")
            return
        if expected > MAX_BODY_BYTES:
            self._refuse(413, "body too large")
            return
        with tempfile.TemporaryDirectory(prefix="incremento-hub-") as spool:
            received = Path(spool) / "upload"
            remaining = expected
            with received.open("wb") as out:
                while remaining:
                    chunk = self.rfile.read(min(_CHUNK, remaining))
                    if not chunk:  # the client went away part-way: nothing is written
                        self.close_connection = True
                        return
                    out.write(chunk)
                    remaining -= len(chunk)
            try:
                self.server.store.upload(received, rel)
            except StoreError as error:
                self._refuse(400, str(error))
                return
            except OSError as error:
                self._refuse(500, str(error))
                return
        self._reply(204)

    def do_DELETE(self) -> None:  # noqa: N802
        if self._pairing_only():
            return
        target = self._target()
        if target is None:
            return
        route, rel = target
        if route != "/v1/file" or not rel:
            self._refuse(400 if route == "/v1/file" else 404, "a file path is required")
            return
        if not self._may_change(rel):
            return
        try:
            self.server.store.delete(rel)
        except StoreError as error:
            self._refuse(400, str(error))
            return
        except OSError as error:
            self._refuse(500, str(error))
            return
        self._reply(204)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], store: Store, context: ssl.SSLContext, *,
                 state_path: Path | None = None, max_connections: int = DEFAULT_MAX_CONNECTIONS):
        self.store = store
        self.context = context
        self._state_path = state_path
        self._owners_lock = threading.Lock()
        self._owners: dict[str, str] = self._load_owners()
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, _Handler)

    def _load_owners(self) -> dict[str, str]:
        if self._state_path is None:
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return {str(device): str(identity) for device, identity in data["owners"].items()}
        except (OSError, ValueError, KeyError, AttributeError):
            return {}

    def may_write(self, identity: str | None, rel: str) -> bool:
        """Device folders belong to the device that first wrote in them; everything else is shared."""
        found = _DEVICE_FOLDER.match(rel)
        if found is None:
            return True
        if identity is None:
            return False
        with self._owners_lock:
            owner = self._owners.setdefault(found[1], identity)
            if owner == identity and self._state_path is not None:
                self._save_owners()
            return owner == identity

    def _save_owners(self) -> None:
        assert self._state_path is not None
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self._state_path.parent, prefix=".owners-", suffix=".tmp")
        try:
            with open(handle, "w", encoding="utf-8") as out:
                json.dump({"version": 1, "owners": self._owners}, out, indent=2)
            Path(name).replace(self._state_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, address = super().get_request()
        if not self._slots.acquire(blocking=False):  # too many devices at once: turn this one away
            sock.close()
            raise OSError("too many connections")
        try:
            return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address
        except BaseException:
            self._slots.release()
            raise

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a failed handshake or a dropped connection is not the hub's problem


class Hub:
    """Serve ``root`` to the paired devices in ``keys`` (identity to key)."""

    def __init__(self, root: Path | str, keys: Mapping[str, bytes], *, host: str = "0.0.0.0", port: int = 0,
                 state_path: Path | str | None = None, max_connections: int = DEFAULT_MAX_CONNECTIONS,
                 discovery_port: int | None = None, key_book: KeyBook | None = None):
        if isinstance(keys, dict):
            for identity, key in keys.items():
                if not _IDENTITY.match(identity) or len(key) < 16:
                    raise ValueError(f"unusable pairing for {identity!r}")
        self.root = Path(root)
        self._store = LocalStore(self.root)
        self._keys = keys  # not copied: a live view of the key book sees devices paired later
        self._book = key_book
        self._pairing: _PairingWindow | None = None
        self._pairing_lock = threading.Lock()
        self._host, self._port = host, port
        self._state_path = None if state_path is None else Path(state_path)
        self._max_connections = max_connections
        self._discovery_wanted = discovery_port
        self._discovery: socket.socket | None = None
        self._discovery_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    # -- Pairing with a short code --

    @property
    def pairing_open(self) -> bool:
        with self._pairing_lock:
            window = self._pairing
            return window is not None and time.monotonic() < window.expires and window.attempts_left > 0

    def open_pairing(self, seconds: float = PAIRING_SECONDS) -> str:
        """Start accepting one device with a new six digit code, which this returns; a window already open is replaced."""
        if self._book is None:
            raise ValueError("this hub has no key book to add devices to")
        pin = f"{secrets.randbelow(1_000_000):06d}"
        with self._pairing_lock:
            self._pairing = _PairingWindow(pin, time.monotonic() + seconds)
        return pin

    def close_pairing(self) -> None:
        with self._pairing_lock:
            self._pairing = None

    def handle_pairing(self, name: str, proof: str, binding: bytes) -> tuple[int, dict[str, Any]]:
        """A device's pairing request (see ``_Handler.do_POST``): (HTTP status, JSON answer)."""
        with self._pairing_lock:
            window = self._pairing
            if (window is None or time.monotonic() >= window.expires or window.attempts_left <= 0
                    or self._book is None or self._server is None):
                return 403, {"error": "pairing is not open"}
            if not window.skip_proof_check and not hmac.compare_digest(proof, _client_proof(window.pin, binding, name)):
                window.attempts_left -= 1
                if window.attempts_left <= 0:
                    self._pairing = None
                return 403, {"error": "wrong code"}
            base = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:56] or "device"
            taken = set(self._book.devices())
            chosen, number = base, 1
            while chosen in taken:
                number += 1
                chosen = f"{base}-{number}"
            key = self._book.add(chosen)
            pin = window.pin
            self._pairing = None  # one code, one device
            return 200, {"name": chosen, "key": key.hex(), "port": int(self._server.server_address[1]),
                         "proof": _hub_proof(pin, binding, chosen, key)}

    @property
    def discovery_port(self) -> int | None:
        return None if self._discovery is None else int(self._discovery.getsockname()[1])

    def _answer_questions(self, sock: socket.socket) -> None:
        keys = self._keys
        while not self._stopping.is_set():
            try:
                data, address = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                # An oversized packet or an unreachable sender (Windows reports those as errors): skip it. Only
                # stopping the hub ends this thread.
                if self._stopping.is_set():
                    return
                continue
            if data.startswith(_PAIR_QUESTION) and len(data) <= 100:
                if self.pairing_open and self._server is not None:  # only while a code is showing
                    try:
                        sock.sendto(_PAIR_ANSWER + str(self._server.server_address[1]).encode("ascii"), address)
                    except OSError:
                        pass
                continue
            if len(data) > 200 or not data.startswith(_DISCOVERY_REQUEST):
                continue
            fields = data[len(_DISCOVERY_REQUEST):].split(b" ")
            if len(fields) != 2 or not _HEX.match(fields[1]):
                continue
            try:
                identity = fields[0].decode("ascii")
            except UnicodeDecodeError:
                continue
            key = keys.get(identity, b"") if _IDENTITY.match(identity) else b""
            if len(key) < 16 or self._server is None:
                continue  # an unknown device gets no answer at all
            port = str(self._server.server_address[1]).encode("ascii")
            try:
                sock.sendto(_DISCOVERY_REPLY + port + b" " + _proof(key, fields[1], port), address)
            except OSError:
                pass

    def _context(self) -> ssl.SSLContext:
        require_psk()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers(CIPHERS)
        keys = self._keys

        def lookup(identity: str | None) -> bytes:
            _handshake_identity.value = identity
            if identity == PAIRING_IDENTITY:  # the public key, and only while a code is showing
                return PAIRING_KEY if self.pairing_open else b""
            key = keys.get(identity or "", b"")
            return key if len(key) >= 16 else b""  # empty: unknown identity, the handshake fails

        context.set_psk_server_callback(lookup)  # type: ignore[attr-defined]
        return context

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _Server((self._host, self._port), self._store, self._context(),
                               state_path=self._state_path, max_connections=self._max_connections)
        self._server.hub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, name="incremento-hub", daemon=True)
        self._thread.start()
        if self._discovery_wanted is not None:
            self._stopping.clear()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._discovery_wanted))
            sock.settimeout(0.3)
            self._discovery = sock
            self._discovery_thread = threading.Thread(target=self._answer_questions, args=(sock,),
                                                      name="incremento-hub-discovery", daemon=True)
            self._discovery_thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._discovery is not None:
            self._discovery_wanted = self._discovery.getsockname()[1]
            self._discovery.close()
            self._discovery = None
        if self._discovery_thread is not None:
            self._discovery_thread.join(timeout=2)
            self._discovery_thread = None
        server, self._server = self._server, None
        if server is not None:
            self._port = int(server.server_address[1])
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


# --- The client ------------------------------------------------------------------------------------------------

class HttpStore:
    """The engine's Store over a hub. Keeps one connection open and reconnects when the hub restarted."""

    def __init__(self, host: str, port: int, identity: str, key: bytes, *, timeout: float = 30.0,
                 discovery_port: int | None = DISCOVERY_PORT, discovery_targets: tuple[str, ...] = ("255.255.255.255",)):
        self._host, self._port, self._timeout = host, port, timeout
        self._discovery_port, self._discovery_targets = discovery_port, discovery_targets
        self._identity, self._key = identity, key
        self._connection: http.client.HTTPSConnection | None = None
        self._lock = threading.RLock()

    @classmethod
    def from_pairing(cls, pairing: Pairing, **options: Any) -> HttpStore:
        return cls(pairing.host, pairing.port, pairing.identity, pairing.key, **options)

    def _context(self) -> ssl.SSLContext:
        require_psk()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False  # there is no certificate: the key proves who the hub is
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers(CIPHERS)
        identity, key = self._identity, self._key
        context.set_psk_client_callback(lambda hint: (identity, key))  # type: ignore[attr-defined]
        return context

    def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _connect(self) -> http.client.HTTPSConnection:
        if self._connection is None:
            connection = http.client.HTTPSConnection(self._host, self._port, context=self._context(),
                                                     timeout=min(self._timeout, CONNECT_SECONDS))
            connection.connect()
            connection.sock.settimeout(self._timeout)  # type: ignore[union-attr]
            self._connection = connection
        return self._connection

    @property
    def address(self) -> tuple[str, int]:
        """Where the hub is now: the paired address, or where the network says it moved to."""
        return self._host, self._port

    def _exchange(self, method: str, target: str, body: Any = None, length: int | None = None) -> http.client.HTTPResponse:
        """Send one request and return its response.

        A connection that went stale is retried once, and a hub that cannot be reached at the paired address is looked
        for on the network once (not when it refused this device: a wrong key is not a moved hub).
        """
        headers = {} if length is None else {"Content-Length": str(length)}
        retried = searched = False
        while True:
            reused = self._connection is not None
            connecting = not reused
            try:
                connection = self._connect()
                if hasattr(body, "seek"):
                    body.seek(0)
                connection.request(method, target, body=body, headers=headers)
                return connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                self._drop()
                failure = StoreError(f"{method} {target.split('?')[0]}: {error}")
                if connecting and isinstance(error, (ssl.SSLError, ConnectionResetError, ConnectionAbortedError)):
                    # The hub is there and hung up during the handshake: it does not know this device's key.
                    failure = StoreError(f"The hub at {self._host}:{self._port} did not accept this device. It may have "
                                         "been removed from the hub: pair it again with a new code.")
                elif connecting and isinstance(error, OSError):
                    failure = StoreError(f"Can't reach the sync hub at {self._host}:{self._port} ({error}). "
                                         "Is the computer on, on the same network, and the hub running?")
                if isinstance(error, ssl.SSLError):
                    raise failure from error
                if reused and not retried:
                    retried = True
                    continue
                if not searched and self._discovery_port is not None and not isinstance(error, http.client.HTTPException):
                    searched = True
                    found = discover(self._identity, self._key, port=self._discovery_port,
                                     targets=self._discovery_targets)
                    if found is not None and found != (self._host, self._port):
                        self._host, self._port = found
                        continue
                raise failure from error

    @staticmethod
    def _target(route: str, rel: str) -> str:
        return f"/v1/{route}?path={quote(rel, safe='')}"

    def _finish(self, response: http.client.HTTPResponse, what: str) -> None:
        if response.status in (400, 411, 413, 500):
            detail = response.read(2000).decode("utf-8", "replace")
            self._drop()
            raise StoreError(f"{what}: the hub answered {response.status} {detail}")
        if response.status not in (200, 204, 404):
            self._drop()
            raise StoreError(f"{what}: the hub answered {response.status}")

    def _json(self, route: str, rel: str) -> Any | None:
        with self._lock:
            response = self._exchange("GET", self._target(route, rel))
            self._finish(response, f"{route} {rel!r}")
            declared = int(response.getheader("Content-Length") or 0)
            if declared > MAX_LISTING_BYTES:
                self._drop()
                raise StoreError(f"{route} {rel!r}: the answer is too large")
            data = response.read()
            if response.status == 404:
                return None
            try:
                return json.loads(data)
            except ValueError as error:
                self._drop()
                raise StoreError(f"{route} {rel!r}: the hub sent something that is not JSON") from error

    @staticmethod
    def _entry(info: Mapping[str, Any]) -> Entry:
        try:
            return Entry(str(info["name"]), bool(info["dir"]), int(info["size"]), float(info["mtime"]), False)
        except (KeyError, TypeError, ValueError) as error:
            raise StoreError("the hub sent a damaged listing") from error

    # -- Store --

    def listdir(self, rel: str) -> list[Entry]:
        clean(rel)
        data = self._json("list", rel)
        if not isinstance(data, list):
            raise StoreError("the hub sent a damaged listing")
        return [self._entry(info) for info in data]

    def stat(self, rel: str) -> Entry | None:
        clean(rel)
        data = self._json("stat", rel)
        return None if data is None else self._entry(data)

    def read(self, rel: str, limit: int) -> bytes | None:
        clean(rel)
        with self._lock:
            response = self._exchange("GET", self._target("file", rel))
            self._finish(response, f"read {rel!r}")
            if response.status == 404:
                response.read()
                return None
            if int(response.getheader("Content-Length") or 0) > limit:
                self._drop()
                raise StoreError(f"{rel} is larger than {limit} bytes")
            data = response.read(limit + 1)
            if len(data) > limit:
                self._drop()
                raise StoreError(f"{rel} is larger than {limit} bytes")
            return data

    def _put(self, rel: str, body: Any, length: int) -> None:
        if not clean(rel):
            raise StoreError(f"unsafe path {rel!r}")
        with self._lock:
            response = self._exchange("PUT", self._target("file", rel), body=body, length=length)
            self._finish(response, f"write {rel!r}")
            response.read()

    def write(self, rel: str, data: bytes) -> None:
        self._put(rel, bytes(data), len(data))

    def upload(self, local: Path, rel: str) -> None:
        with Path(local).open("rb") as stream:
            self._put(rel, stream, Path(local).stat().st_size)

    def download(self, rel: str, local: Path, limit: int) -> None:
        if not clean(rel):
            raise StoreError(f"unsafe path {rel!r}")
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            response = self._exchange("GET", self._target("file", rel))
            self._finish(response, f"download {rel!r}")
            if response.status == 404:
                response.read()
                raise StoreError(f"{rel} is missing")
            if int(response.getheader("Content-Length") or 0) > limit:
                self._drop()
                raise StoreError(f"{rel} is larger than {limit} bytes")
            total = 0
            try:
                with target.open("wb") as out:
                    while chunk := response.read(_CHUNK):
                        total += len(chunk)
                        if total > limit:
                            raise StoreError(f"{rel} is larger than {limit} bytes")
                        out.write(chunk)
            except (OSError, http.client.HTTPException, StoreError) as error:
                self._drop()
                target.unlink(missing_ok=True)
                if isinstance(error, StoreError):
                    raise
                raise StoreError(f"download {rel!r}: {error}") from error

    def delete(self, rel: str) -> None:
        if not clean(rel):
            raise StoreError(f"unsafe path {rel!r}")
        with self._lock:
            response = self._exchange("DELETE", self._target("file", rel))
            self._finish(response, f"delete {rel!r}")
            response.read()

    # -- for tests and diagnostics --

    def _socket(self) -> ssl.SSLSocket:
        with self._lock:
            connection = self._connect()
            if connection.sock is None:
                connection.connect()
            return connection.sock  # type: ignore[return-value]

    def cipher(self) -> str:
        negotiated = self._socket().cipher()
        return "" if negotiated is None else str(negotiated[0])

    def tls_version(self) -> str:
        return str(self._socket().version())

    def close(self) -> None:
        with self._lock:
            self._drop()


__all__ = ["CIPHERS", "DISCOVERY_PORT", "HttpStore", "Hub", "KeyBook", "Pairing", "as_store", "discover", "new_key",
           "require_psk"]
