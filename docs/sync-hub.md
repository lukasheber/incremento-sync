# Sync with your phone (a hub on your network)

A hub lets devices that share no cloud folder (a phone and a computer on one network) exchange Incremento data.
It is the same exchange as a shared folder, reached over the network. The engine on each device validates every
file it reads, so a hub is only a place to put files: it cannot make a device accept bad data.

## Anki as the hub (the usual way)

In Anki choose *Incremento → Sync Incremento Data…* and press **Set up phone sync**. This computer becomes the hub:

- the window shows a six digit code, good for one phone, five minutes and three wrong tries (**New code** shows another);
- on the phone open Ankrimento → Incremento settings → This device → Sync hub and type the code. The phone finds the
  computer on the network by itself; if it cannot (a guest network that keeps devices apart, for instance), type this
  computer's address after a comma, as the window shows it: `483920,192.168.1.20`;
- once the phone is paired the code goes away and the window shows the status instead: when each phone last synced
  and whether it has this computer's newest changes, whether the phone's newest changes are still on their way here,
  changes waiting for cards from AnkiWeb, conflicts to decide, and a sync that failed. **Pair another phone**,
  **Remove a phone…** and **Stop phone sync** are below it.

Nobody has to press anything to keep in step. The phone sends and receives when it syncs (after its AnkiWeb sync, or
with Sync now in its Incremento menu). The computer takes in what a phone sent within half a minute, and sends its own
changes every five minutes while phones are served, after AnkiWeb syncs, and when the profile closes. The hub notes
when each phone last made a request and last changed a file (`user_files/sync/phone-activity.json`); "has this
computer's newest changes" means the phone synced after this computer last sent changes.

Anki serves the phones only while it is open, and the profile that is open. The first time, the firewall may ask
whether to let Anki use the network: allow it on private networks. The server uses port 8765 (and UDP 8766 for being
found), or another free port if 8765 is taken. The computer syncs against the served folder directly (it is the profile's
`user_files/sync/phone-hub`), so nothing goes over the network for itself. The keys are kept beside that folder, not in
it, and are skipped by exports and backups with the rest of the `sync` folder.

A profile syncs one way at a time: phone sync, a cloud folder, or another computer's hub. *Stop syncing* frees it; the
paired phones stay paired and the data is kept.

## A hub on another computer (advanced)

For a machine that is on while you study and is not the one you run Anki on, run the standalone hub (any computer with
Python 3.13 or later; nothing to install):

```
python scripts/sync_hub.py --keys hub-keys.json serve ~/IncrementoHub
```

It shows a six digit code, like Anki does:

```
  Pairing code:  483 920
```

Type it on the device:

- Anki desktop: Incremento sync dialog, **Advanced: use another computer as the server…**.
- Ankrimento: Incremento settings, **This device**, **Sync hub**.

Press Enter in the hub's window for a new code to pair another device. A code is good for one device, five minutes and
three wrong tries; the device's real key is made by the hub and handed over inside that exchange, so no key is ever typed
or shown.

`devices` lists the paired devices and `revoke` removes one, at once (the device is then told to pair again). Keep the
keys file out of the served folder and out of any cloud folder. The hub needs its port reachable from the devices
(default 8765, and UDP 8766 for finding it); the firewall may ask the first time.

For networks where a device cannot find the hub at all there is the long way: `pair NAME --host ADDRESS` prints a
pairing code for the device to copy, which a device accepts in place of the six digits.

A hub paired with a device replaces that device's sync folder. Pairing a device that already has data with an empty hub
makes it publish everything it has, with the documents.

## What protects the data

- **The channel**: TLS 1.2 with a pre-shared key, the pairing secret (`ECDHE-PSK-CHACHA20-POLY1305`). It is
  encrypted, forward secret, and both sides prove they hold the key, so a device knows it is talking to its own hub
  and the hub knows the device is paired. There is no certificate to create, trust or renew. It needs Python 3.13's
  `ssl`, which Anki 25 and Ankrimento have; an older Python says so.
- **One key per device**: 256 random bits each. A lost device is revoked by deleting its key.
- **Each device changes only its own folder**: the first device to write below `devices/<id>/` owns it, and another
  paired device cannot write or delete there (everyone can read everything, and blobs and `library.json` are shared).
  A device that is revoked and paired again should keep its name, or it cannot write to the folder it owned. The owners
  are kept in a file beside the keys file; if it is lost, the next writers claim the folders again.
- **The hub's own checks**: it refuses unsafe paths (`..`, absolute, backslash, empty parts), uploads over 512 MB or
  without a length, and uploads that stop part-way (nothing is written). It never shows or follows a symbolic link.
  A client that stalls or sends garbage cannot hold up the others.
- **The devices' checks**: unchanged from a cloud folder. Packs, snapshots and blobs are validated and hashed before
  anything is applied.

- **Discovery**: a device that cannot reach its hub broadcasts `IHUB1 <device> <nonce>`; a hub answers only a device it
  has a key for, with its port and an HMAC of the nonce and port under that key. The question reveals nothing, and an
  answer without a valid proof is ignored. `serve --no-discovery` turns the answers off.

## Protocol (version 1)

All under `/v1`, the path in the `path` query parameter (`/`-separated, relative):

| Request | Answer |
|---|---|
| `GET /v1/list?path=<folder>` | JSON `[{name, dir, size, mtime}]`; a missing folder is `[]` |
| `GET /v1/stat?path=<rel>` | JSON `{name, dir, size, mtime}`, or 404 |
| `GET /v1/file?path=<rel>` | the bytes, or 404 |
| `PUT /v1/file?path=<rel>` | replaces the file atomically; `Content-Length` required; 204 |
| `DELETE /v1/file?path=<rel>` | 204, also when there is nothing to delete |

The six digit code works like this: the device connects with a public pairing key (an encrypted channel and nothing
more) and sends an HMAC of the code bound to that very TLS connection (its `tls-unique` channel binding), so a proof
cannot be replayed on another connection and someone in the middle, holding two different connections, cannot make
either side's proof verify. The hub answers with its own proof, then the device's key. The code is random, valid for
five minutes, used once, and the window closes after three wrong proofs, so a guess has about a three in a million
chance. Only a hub that is showing a code answers the device's broadcast.

The long pairing code is `ihub1-` + the URL-safe base64 of `[1, host, port, identity, key-hex]` + `-` + a 6-character
check. It contains the key, so treat it like a password.

## Not covered

- **A paired device is still trusted not to fill the disk or delete shared blobs** (a blob nobody names is cleared
  by any device's cleanup, by design). Revoke a device that misbehaves.
- **Finding the hub again is best effort**: the pairing code names the hub's address; if it cannot be reached there, a
  device asks the local network (UDP broadcast, port 8766) and a hub holding its key answers with a proof only that key
  can make, so a device never follows an impostor. It works on one network segment; across routers or Wi-Fi client
  isolation, pair again with the new address.
- **At most 64 simultaneous connections** (`max_connections`); more are turned away and retry on the next sync.
- The key is stored in the profile's `sync/hub.json` (desktop) or the app's private preferences (Ankrimento), not in
  an OS keystore. Exports and support bundles skip the `sync` folder.
