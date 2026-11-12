# Incremento Sync

An Anki add-on that syncs [Incremento](https://github.com/BaskovicP/Incremento)'s own data between devices:
priorities, the knowledge tree, reader bookmarks and positions, highlights, links between documents, local files, video
files with their subtitles, and the PDF and EPUB documents themselves. (Audio topics are Anki media, which AnkiWeb syncs.) Anki's own sync (AnkiWeb) carries the cards; this carries what Incremento keeps beside them.

It runs from a shared folder (Dropbox, Syncthing, a network drive…) or through a small hub on your own network, and it is
what the Android app [Ankrimento](https://github.com/lukasheber/Ankrimento) uses to stay in step with desktop.

It is a separate add-on on purpose: Incremento is not changed, and anyone who does not need sync is not affected.

## Install

1. Install Incremento.
2. Download `incremento_sync.ankiaddon` from the [releases](https://github.com/lukasheber/incremento-sync/releases) and open it
   in Anki (*Tools → Add-ons → Install from file…*). Restart Anki.
3. Choose *Incremento → Sync Incremento Data…* and pick the shared folder (or pair with a hub, see
   [docs/sync-hub.md](docs/sync-hub.md)). Do the same on every device.

## Switching it off puts everything back

While it is on, the add-on extends Incremento's `backend` package with the sync engine and changes Incremento's behaviour in
one place (backups leave out the sync folder). It also adds a menu entry, a timer and Anki hooks. All of that is registered
through one switch (`incremento_sync/loader.py`) and undone by it:

- set `"enabled": false` in the add-on's config (*Tools → Add-ons → Incremento Sync → Config*), or remove the add-on, and
  Incremento is exactly as if the add-on had never been installed: the menu entries, timer, hooks, patched functions and
  loaded modules are gone;
- the sync data it wrote (its tables in Incremento's database, and `user_files/sync`) is **kept**, so switching it on again
  resumes where it stopped;
- *Incremento → Remove Incremento Sync Data…* deletes that data (cards, documents and settings are never touched).

`tools/anki_smoke.py` checks this in a real Anki window, and `tests/test_addon_loader.py` checks it on the modules.

## What it needs from Incremento

It uses Incremento's `backend` modules (`paths`, `db`, the reader and highlight stores…) and finds the add-on by its files, so
it works whatever folder Anki installed Incremento in. If a future Incremento changes something it depends on, enabling fails
with a message and leaves Incremento untouched. It was written against Incremento v2026.11.11.

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
git clone https://github.com/BaskovicP/Incremento vendor/incremento      # or set INCREMENTO_DIR
INCREMENTO_DIR=vendor/incremento python -m pytest
python tools/anki_smoke.py vendor/incremento                           # real Anki, offscreen
python tools/build.py                                                  # dist/incremento_sync.ankiaddon
```

The engine is in `incremento_sync/engine/` (plain Python, no Qt),
so the Android app runs the same files. `docs/shared-folder-sync.md` is the design; `docs/sync-hub.md` the hub.

## Licence and credit

GPL-3.0-or-later (see `LICENSE`). Incremento is by Paulo (BaskovicP) and is loaded, not copied: this repository contains none
of its code.
