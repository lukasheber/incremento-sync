"""The desktop's phone controller (start, status, new code, remove, stop) with Anki's main window faked."""
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = dedent(r"""
    import os, sys, sqlite3, tempfile, types, importlib
    sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), "incremento_sync", "engine"),
                    os.path.join(os.environ["INCREMENTO_DIR"], "backend")]
    from aqt.qt import QApplication
    app = QApplication([])
    from incremento_sync import desktop

    def collection(cards):
        col = sqlite3.connect(":memory:", check_same_thread=False)
        col.execute("CREATE TABLE col (usn INTEGER, scm INTEGER, mod INTEGER DEFAULT 1)")
        col.execute("INSERT INTO col(usn, scm) VALUES (20, 1000)")
        col.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT)")
        col.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, usn INTEGER)")
        for card_id in cards:
            col.execute("INSERT INTO notes VALUES (?, ?)", (card_id * 10, f"g{card_id}"))
            col.execute("INSERT INTO cards VALUES (?, ?, 5)", (card_id, card_id * 10))
        return lambda sql, *args: col.execute(sql, args).fetchall()

    class Timer:
        def __init__(self, *args): self.running = False
        def setInterval(self, ms): pass
        def start(self): self.running = True
        def stop(self): self.running = False
        class _Signal:
            def connect(self, callback): pass
        timeout = _Signal()

    class Loader:
        module = staticmethod(lambda name: importlib.import_module(name))

    shown = []
    desktop.mw = types.SimpleNamespace(col=types.SimpleNamespace(db=types.SimpleNamespace(all=collection(range(1, 30)))))
    desktop.QTimer = Timer
    desktop.loader = Loader
    desktop.tooltip = shown.append
    desktop._addon_dir = tempfile.mkdtemp()
    import sync_phone
    original = sync_phone.PhoneServer
    # the server of a test takes free ports, not the usual ones
    def _make(profile):
        if desktop._phone is None or desktop._phone_profile != profile:
            desktop._stop_phone()
            desktop._phone = original(desktop._addon_dir, profile, host="127.0.0.1", port=0, discovery_port=None)
            desktop._phone_profile = profile
        return desktop._phone

    desktop._phone_server = _make
    controller = desktop._Controller("P")
""")


def _run(body: str) -> None:
    result = subprocess.run([sys.executable, "-c", SCRIPT + dedent(body)], cwd=ROOT,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_starting_phone_sync_serves_the_profile_and_shows_a_code():
    _run(r"""
        assert controller.phone_unavailable() is None
        assert not controller.phone().running

        controller.start_phone()

        info = controller.phone()
        assert info.running and len(info.code.replace(" ", "")) == 6 and info.phones == []
        assert desktop._phone_timer is not None and desktop._phone_timer.running
        status = controller.status()
        assert status.configured and status.folder.startswith("phone:")

        controller.new_code()
        assert controller.phone().code and controller.phone().seconds_left > 0

        controller.disable()
        assert not controller.phone().running and desktop._phone is None and desktop._phone_timer is None
        assert not controller.status().configured
    """)


def test_a_profile_that_syncs_with_a_phone_serves_again_when_it_is_opened():
    _run(r"""
        controller.start_phone()
        desktop._stop_phone()
        assert not controller.phone().running and controller.status().folder.startswith("phone:")
        import sync_service
        desktop._profile = lambda: "P"
        desktop._service = lambda: sync_service

        desktop._resume_phone()

        assert controller.phone().running and shown == []
        desktop._stop_phone()
    """)


def test_a_phone_that_changed_something_makes_the_desktop_sync_soon():
    _run(r"""
        calls = []
        desktop.start_background_sync = lambda done=None: calls.append("sync")
        controller.start_phone()
        stamps = iter([None, 100.0, 100.0, 250.0])
        desktop._phone.last_change = lambda: next(stamps)
        desktop._phone_seen = None
        desktop._phone_sent_at = desktop.time.monotonic()  # this computer just sent its own changes

        desktop._phone_changed()   # nothing from a phone yet
        desktop._phone_changed()   # the phone wrote
        desktop._phone_changed()   # nothing new
        desktop._phone_changed()   # the phone wrote again

        assert calls == ["sync", "sync"]
        desktop._stop_phone()
    """)


def test_while_phones_are_served_this_computer_sends_its_changes_every_few_minutes():
    _run(r"""
        calls = []
        desktop.start_background_sync = lambda done=None: calls.append("sync")
        controller.start_phone()
        desktop._phone.last_change = lambda: None
        desktop._phone_sent_at = desktop.time.monotonic()

        desktop._phone_changed()
        assert calls == []
        desktop._phone_sent_at -= desktop._PHONE_SEND_SECONDS
        desktop._phone_changed()

        assert calls == ["sync"]
        desktop._stop_phone()
    """)


def test_the_window_hears_whether_a_sync_runs_and_why_the_last_one_failed():
    _run(r"""
        assert controller.activity() == desktop.Activity(False, None)
        desktop._syncing, desktop._last_error = 1, "disk full"
        assert controller.activity() == desktop.Activity(True, "disk full")
    """)


def test_the_window_hears_how_far_a_running_sync_is():
    _run(r"""
        import sync_service
        assert controller.activity().progress is None
        desktop._syncing = 1
        desktop._heard(sync_service.SyncProgress("preparing", 3, 10))
        assert controller.activity() == desktop.Activity(True, None, sync_service.SyncProgress("preparing", 3, 10))
    """)
