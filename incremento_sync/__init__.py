"""Incremento Sync: shared-folder and hub sync for Incremento's own data, as a separate Anki add-on.

It extends the Incremento add-on while it is switched on and puts everything back when it is switched off
(``enabled`` in this add-on's config, or by removing it). See ``loader.py``.
"""

from __future__ import annotations

try:
    from aqt import gui_hooks, mw
except ImportError:  # the phone's runtime loads ``loader`` itself, without Qt
    gui_hooks = mw = None

if gui_hooks is not None:
    from . import desktop

    # Every add-on is loaded by then, so Incremento can be found.
    gui_hooks.main_window_did_init.append(desktop.start)
