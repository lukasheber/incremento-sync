"""The sync dialog in real Qt (offscreen), driven by a fake controller."""
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]

SCRIPT_HEAD = dedent(r"""
    import os, sys, time
    from types import SimpleNamespace as NS
    sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), "incremento_sync", "engine"),
                    os.path.join(os.environ["INCREMENTO_DIR"], "backend")]
    from aqt.qt import QApplication, Qt
    app = QApplication([])
    from incremento_sync import sync_dialog
    from sync_service import Conflict, SyncStatus

    NOW = time.time()
    PHONE = "phone:C:/Profile/sync/phone-hub"

    def phone(name="pixel", seen=None, changed=None):
        return NS(name=name, seen=seen, changed=changed)

    def status(folder=None, last_sync=None, published=None, conflicts=0, waiting=0, files=0, stale=0):
        return SyncStatus(folder is not None, folder, last_sync, conflicts, waiting, files, 0, stale,
                          last_published=published)

    class Controller:
        def __init__(self, folder=None, conflicts=(), last_sync=None, published=None, waiting_for_anki=0, stale=0,
                     phones=(), code=None, running=True, unavailable=None, syncing=False, error=None):
            self.folder, self._conflicts = folder, list(conflicts)
            self.last_sync, self.published, self.waiting_for_anki, self.stale = last_sync, published, waiting_for_anki, stale
            self.phones, self.code, self.running, self.unavailable = list(phones), code, running, unavailable
            self.syncing, self.error = syncing, error
            self.calls, self.done = [], None
        def status(self):
            return status(self.folder, self.last_sync, self.published, len(self._conflicts), self.waiting_for_anki,
                          0, self.stale)
        def conflicts(self):
            return list(self._conflicts)
        def activity(self):
            return NS(syncing=self.syncing, error=self.error)
        def phone_unavailable(self):
            return self.unavailable
        def phone(self):
            return NS(running=self.running, code=self.code, seconds_left=272, address="192.168.1.20",
                      phones=list(self.phones))
        def start_phone(self):
            self.calls.append(("start_phone",))
            self.folder, self.code = PHONE, "123 456"
        def new_code(self):
            self.calls.append(("new_code",))
            self.code = "999 000"
        def cancel_code(self):
            self.calls.append(("cancel_code",))
            self.code = None
        def remove_phone(self, name):
            self.calls.append(("remove_phone", name))
            self.phones = [p for p in self.phones if p.name != name]
        def configure(self, folder):
            self.calls.append(("configure", folder))
            if folder == "bad":
                raise RuntimeError("holds several libraries")
            self.folder = folder
        def configure_hub(self, code):
            self.calls.append(("configure_hub", code))
            if code == "bad":
                raise RuntimeError("The hub cannot be used: refused")
            self.folder = "hub:desktop:0a1b2c3d"
        def disable(self):
            self.calls.append(("disable",))
            self.folder = None
        def sync(self, done):
            self.calls.append(("sync",))
            self.done = done
        def discard_stale(self):
            self.calls.append(("discard_stale",))
            self.waiting_for_anki, self.stale = 0, 0
        def resolve(self, conflict_id, value):
            self.calls.append(("resolve", conflict_id, value))
            self._conflicts = [c for c in self._conflicts if c.id != conflict_id]

    def shown(widget):
        return not widget.isHidden()
""")


def _run(body: str) -> None:
    result = subprocess.run([sys.executable, "-c", SCRIPT_HEAD + dedent(body)], cwd=ROOT,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


# --- What the status says ---------------------------------------------------------------------------------------

def test_a_phone_that_synced_after_this_computer_sent_its_changes_is_up_to_date():
    _run(r"""
        lines = sync_dialog.status_lines("phone", status(PHONE, last_sync=NOW - 60, published=NOW - 600),
                                         phones=[phone(seen=NOW - 120, changed=NOW - 300)], now=NOW)
        assert lines == [("ok", "Everything is up to date."),
                         ("ok", "pixel synced 2 min ago and has all of this computer's changes.")]
    """)


def test_a_phone_that_synced_before_the_newest_changes_gets_them_at_its_next_sync():
    _run(r"""
        lines = sync_dialog.status_lines("phone", status(PHONE, last_sync=NOW - 10, published=NOW - 10),
                                         phones=[phone(seen=NOW - 3000)], now=NOW)
        assert lines == [("info", "pixel synced 50 min ago. It gets this computer's newest changes the next time it syncs.")]
    """)


def test_changes_a_phone_sent_after_the_last_sync_here_are_on_their_way():
    _run(r"""
        lines = sync_dialog.status_lines("phone", status(PHONE, last_sync=NOW - 600, published=NOW - 900),
                                         phones=[phone(seen=NOW - 30, changed=NOW - 30)], now=NOW)
        assert ("busy", "Taking in your phone's newest changes…") in lines
        assert ("ok", "Everything is up to date.") not in lines
        running = sync_dialog.status_lines("phone", status(PHONE, last_sync=NOW - 600), phones=[phone(seen=NOW)],
                                           activity=NS(syncing=True, error=None), now=NOW)
        assert ("busy", "Sending and receiving changes…") in running
    """)


def test_a_new_phone_is_paired_and_waits_for_its_first_sync():
    _run(r"""
        lines = sync_dialog.status_lines("phone", status(PHONE, last_sync=NOW, published=NOW), phones=[phone()], now=NOW)
        assert lines == [("info", "pixel is paired. It sends and receives at its first sync.")]
    """)


def test_waiting_for_anki_web_conflicts_and_failures_are_said_plainly():
    _run(r"""
        lines = sync_dialog.status_lines(
            "phone", status(PHONE, last_sync=NOW, published=NOW - 60, conflicts=1, waiting=3),
            phones=[phone(seen=NOW)], activity=NS(syncing=False, error="disk full"), now=NOW)
        kinds = [kind for kind, _ in lines]
        assert kinds == ["ok", "wait", "warn", "error"]
        assert lines[1][1].startswith("3 changes from another device are waiting for their cards")
        assert "AnkiWeb" in lines[1][1]
        assert lines[2][1] == "1 change needs your decision (below)."
        assert lines[3][1] == "The last sync did not finish: disk full"
        one = sync_dialog.status_lines("folder", status("C:/Cloud", last_sync=NOW, waiting=1), now=NOW)
        assert one[1][1].startswith("1 change from another device is waiting for its card")
    """)


def test_a_folder_says_when_it_last_synced():
    _run(r"""
        assert sync_dialog.status_lines("folder", status("C:/Cloud", last_sync=NOW - 900), now=NOW) == [
            ("ok", "Synced 15 min ago.")]
        assert sync_dialog.status_lines("folder", status("C:/Cloud"), now=NOW) == [("info", "Not synced yet.")]
        assert sync_dialog.ago(NOW - 5, NOW) == "just now"
    """)


# --- Setting up -------------------------------------------------------------------------------------------------

def test_an_unconfigured_profile_offers_the_phone_first_then_a_cloud_folder_then_another_computer():
    _run(r"""
        dialog = sync_dialog.SyncDialog("Main", Controller())
        assert shown(dialog._setup) and not shown(dialog._pairing_page) and not shown(dialog._status_page)
        assert dialog._phone_setup.text() == "Set up phone sync" and dialog._phone_setup.isEnabled()
        assert dialog._phone_setup.isDefault()
        assert dialog._choose.text() == "Choose folder…" and dialog._choose.isEnabled()
        assert dialog._hub.text().startswith("Advanced") and dialog._hub.isFlat()
        assert not shown(dialog._sync) and not shown(dialog._phone_unavailable)
    """)


def test_phone_sync_says_why_it_cannot_be_set_up():
    _run(r"""
        dialog = sync_dialog.SyncDialog("Main", Controller(unavailable="needs Python 3.13"))
        assert not dialog._phone_setup.isEnabled()
        assert "needs Python 3.13" in dialog._phone_unavailable.text() and shown(dialog._phone_unavailable)
        assert dialog._choose.isEnabled()
    """)


def test_a_phone_that_cannot_be_set_up_is_explained_and_nothing_changes():
    _run(r"""
        messages = []
        sync_dialog.showInfo = lambda text, **kwargs: messages.append(text)
        controller = Controller()
        def refuse():
            raise RuntimeError("port blocked")
        controller.start_phone = refuse
        dialog = sync_dialog.SyncDialog("Main", controller)

        dialog.use_phone()

        assert messages == ["Phone sync could not be set up: port blocked"] and shown(dialog._setup)
    """)


# --- Pairing ----------------------------------------------------------------------------------------------------

def test_setting_up_the_phone_shows_only_the_pairing_steps_and_the_code():
    _run(r"""
        controller = Controller()
        dialog = sync_dialog.SyncDialog("Main", controller)

        dialog._phone_setup.click()

        assert controller.calls == [("start_phone",)]
        assert shown(dialog._pairing_page) and not shown(dialog._setup) and not shown(dialog._status_page)
        assert dialog._code.text() == "123 456" and "4:32 left" in dialog._code_note.text()
        assert "123456,192.168.1.20" in dialog._address.text()
        assert not shown(dialog._new_code) and not shown(dialog._cancel_code)  # nothing to cancel to yet
        assert not shown(dialog._sync)
        assert dialog._ticker.isActive() and dialog._ticker.interval() == sync_dialog.CODE_TICK_MS
    """)


def test_once_the_phone_is_paired_the_code_goes_away_and_the_status_shows():
    _run(r"""
        controller = Controller(folder=PHONE, code="123 456")
        dialog = sync_dialog.SyncDialog("Main", controller)
        assert shown(dialog._pairing_page)

        controller.code, controller.phones = None, [phone()]  # the hub closes the code when a phone pairs
        dialog.refresh()

        assert not shown(dialog._pairing_page) and shown(dialog._status_page)
        assert dialog._mode_heading.text() == "Syncing with your phone"
        assert "pixel is paired" in dialog._status.text()
        assert shown(dialog._pair_another) and shown(dialog._remove) and not shown(dialog._change)
        assert not shown(dialog._sync)  # a phone syncs by itself
        assert "closing this window does not stop it" in dialog._how.text()
        assert dialog._ticker.interval() == sync_dialog.STATUS_TICK_MS
    """)


def test_an_expired_code_offers_a_new_one():
    _run(r"""
        controller = Controller(folder=PHONE)
        dialog = sync_dialog.SyncDialog("Main", controller)
        assert shown(dialog._pairing_page) and "expired" in dialog._code_note.text() and shown(dialog._new_code)

        dialog._new_code.click()

        assert controller.calls == [("new_code",)] and dialog._code.text() == "999 000"
    """)


def test_pairing_another_phone_can_be_cancelled_back_to_the_status():
    _run(r"""
        controller = Controller(folder=PHONE, phones=[phone(seen=NOW)], last_sync=NOW)
        dialog = sync_dialog.SyncDialog("Main", controller)
        assert shown(dialog._status_page)

        dialog._pair_another.click()
        assert shown(dialog._pairing_page) and dialog._code.text() == "999 000" and shown(dialog._cancel_code)
        dialog._cancel_code.click()

        assert controller.calls == [("new_code",), ("cancel_code",)] and shown(dialog._status_page)
    """)


def test_a_phone_server_that_is_not_running_says_so():
    _run(r"""
        dialog = sync_dialog.SyncDialog("Main", Controller(folder=PHONE, running=False))
        assert shown(dialog._status_page) and "not running" in dialog._status.text()
    """)


# --- Phones and stopping ----------------------------------------------------------------------------------------

def test_removing_the_only_phone_asks_first():
    _run(r"""
        answers = [False, True]
        sync_dialog.askUser = lambda text, **kwargs: answers.pop(0) if "pixel" in text else None
        controller = Controller(folder=PHONE, phones=[phone(seen=NOW)])
        dialog = sync_dialog.SyncDialog("Main", controller)

        dialog._remove.click()
        assert controller.calls == []
        dialog._remove.click()

        assert controller.calls == [("remove_phone", "pixel")]
        assert shown(dialog._pairing_page)  # no phone left: back to pairing
    """)


def test_with_several_phones_removing_asks_which():
    _run(r"""
        sync_dialog.askUser = lambda text, **kwargs: True
        sync_dialog.QInputDialog.getItem = staticmethod(lambda *args: ("tablet", True))
        controller = Controller(folder=PHONE, phones=[phone(seen=NOW), phone("tablet", seen=NOW)])
        dialog = sync_dialog.SyncDialog("Main", controller)

        dialog._remove.click()

        assert controller.calls == [("remove_phone", "tablet")]
    """)


def test_stopping_phone_sync_asks_first_and_goes_back_to_the_choices():
    _run(r"""
        answers = [False, True]
        sync_dialog.askUser = lambda text, **kwargs: answers.pop(0)
        controller = Controller(folder=PHONE, phones=[phone(seen=NOW)])
        dialog = sync_dialog.SyncDialog("Main", controller)
        assert dialog._stop.text() == "Stop phone sync"

        dialog._stop.click()
        assert controller.calls == [] and shown(dialog._status_page)
        dialog._stop.click()

        assert controller.calls == [("disable",)]
        assert shown(dialog._setup) and not shown(dialog._status_page) and not dialog._ticker.isActive()
    """)


# --- A cloud folder or another computer ----------------------------------------------------------------------------

def test_a_cloud_folder_profile_shows_its_folder_and_sync_now():
    _run(r"""
        dialog = sync_dialog.SyncDialog("Main", Controller(folder="C:/Cloud", last_sync=NOW - 60))
        assert dialog._mode_heading.text() == "Syncing through a cloud folder"
        assert dialog._where.text() == "C:/Cloud" and shown(dialog._change) and shown(dialog._sync)
        assert not shown(dialog._pair_another) and not shown(dialog._remove) and not shown(dialog._conflicts_box)
        assert dialog._stop.text() == "Stop syncing" and "15 minutes" in dialog._how.text()
    """)


def test_sync_now_shows_progress_and_refreshes_when_done():
    _run(r"""
        controller = Controller(folder="C:/Cloud")
        dialog = sync_dialog.SyncDialog("Main", controller)
        dialog._sync.click()
        assert controller.calls == [("sync",)]
        assert dialog._sync.text() == "Syncing…" and not dialog._sync.isEnabled()
        assert not dialog._stop.isEnabled() and not dialog._change.isEnabled()

        controller.done()

        assert dialog._sync.text() == "Sync now" and dialog._sync.isEnabled()
    """)


def test_a_folder_the_service_refuses_is_explained_and_changes_nothing():
    _run(r"""
        messages = []
        sync_dialog.showInfo = lambda text, **kwargs: messages.append(text)
        controller = Controller()
        dialog = sync_dialog.SyncDialog("Main", controller)

        dialog.use_folder("bad")

        assert messages == ["This folder can't be used: holds several libraries"] and shown(dialog._setup)
        dialog.use_folder("C:/Cloud")
        assert dialog._where.text() == "C:/Cloud" and dialog._sync.isEnabled()
    """)


def test_a_hub_is_paired_with_a_code_and_shown_by_the_device_it_was_paired_as():
    _run(r"""
        messages = []
        sync_dialog.showInfo = lambda text, **kwargs: messages.append(text)
        controller = Controller()
        dialog = sync_dialog.SyncDialog("Main", controller)

        dialog.use_hub("bad")

        assert messages == ["This folder can't be used: The hub cannot be used: refused"] and shown(dialog._setup)
        dialog.use_hub("  ihub1-code  ")
        assert controller.calls[-1] == ("configure_hub", "ihub1-code")
        assert dialog._mode_heading.text() == "Syncing through another computer"
        assert "desktop" in dialog._where.text() and "0a1b2c3d" not in dialog._where.text()
    """)


# --- Conflicts and waiting changes -------------------------------------------------------------------------------

def test_a_conflict_reads_as_this_computer_against_the_other_device_and_can_be_settled():
    _run(r"""
        conflict = Conflict(4, "priority", "card:7", ["h1", "h2"], [{"priority": 30.0}, {"priority": 90.0}], 1)
        controller = Controller(folder=PHONE, phones=[phone(seen=NOW)], conflicts=[conflict])
        dialog = sync_dialog.SyncDialog("Main", controller)
        assert shown(dialog._conflicts_box) and "1 change needs your decision" in dialog._status.text()
        assert dialog._list.item(0).text() == "Priority of card 7: this computer 90 · other device 30"
        assert not dialog._use_theirs.isEnabled()

        dialog._list.setCurrentRow(0)
        assert dialog._use_theirs.isEnabled()
        dialog._use_theirs.click()

        assert controller.calls == [("resolve", 4, {"priority": 30.0})]
        assert dialog._list.count() == 0 and not shown(dialog._conflicts_box)
    """)


def test_values_are_described_briefly_and_deletions_are_named():
    _run(r"""
        assert sync_dialog.describe_value("priority", {"priority": 12.5}) == "12.5"
        assert sync_dialog.describe_value("reading_position", {"page": 42, "scroll_ratio": 0.1,
                                                                "read_page": 41, "read_anchor_json": ""}) == "page 42"
        assert sync_dialog.describe_value("pdf_highlight", None) == "deleted"
        long = sync_dialog.describe_value("reader_bookmark", {"comment_text": "x" * 200})
        assert len(long) == sync_dialog.MAX_VALUE_TEXT and long.endswith("…")
    """)


def test_changes_waiting_too_long_can_be_discarded_after_confirming():
    _run(r"""
        answers = [False, True]
        sync_dialog.askUser = lambda text, **kwargs: answers.pop(0) if "Discard 3" in text else None
        controller = Controller(folder="C:/Cloud", waiting_for_anki=3, stale=3)
        dialog = sync_dialog.SyncDialog("Main", controller)
        assert "3 changes have waited over 30 days" in dialog._status.text() and shown(dialog._discard)

        dialog._discard.click()
        assert controller.calls == []
        dialog._discard.click()

        assert controller.calls == [("discard_stale",)]
        assert "30 days" not in dialog._status.text() and not shown(dialog._discard)
    """)


def test_the_discard_button_is_hidden_when_nothing_waited_too_long():
    _run(r"""
        dialog = sync_dialog.SyncDialog("Main", Controller(folder="C:/Cloud", waiting_for_anki=2))
        assert not shown(dialog._discard) and "waiting for their cards" in dialog._status.text()
    """)
