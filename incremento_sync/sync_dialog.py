"""Sync for one profile: with a phone (this computer is the server), through a cloud folder, or through
another computer's server.

Once set up, the window answers three questions and nothing more: is the other side in step (when it last synced,
and who is behind), is anything on its way, and is there anything to decide or worry about.

A thin view over a controller supplied by the composition root:
``status()``, ``conflicts()``, ``configure(folder)``, ``configure_hub(code)``, ``disable()``,
``sync(done)`` (runs in the background, calls ``done`` on the main thread), ``resolve(conflict_id, value)``,
``discard_stale()``, ``activity()`` (``syncing``, ``error``, ``progress``) and, for phones, ``phone_unavailable()``,
``start_phone()``, ``phone()``, ``new_code()``, ``cancel_code()`` and ``remove_phone(name)``. Optional:
``inventory(done)`` (counts what the collection names, in the background, and calls ``done`` with a
``sync_service.Inventory`` or None) and ``save_log(path, done)`` (writes the report for help, then calls ``done``
with None or why it failed). The rules live in backend/sync_service.py and backend/sync_phone.py.
"""

import html
import json
import os
import time

from .i18n import t

from aqt.qt import (
    QColor, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QPainter, QProgressBar, QPushButton, QSizePolicy, Qt, QTimer, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showInfo, showWarning

MAX_VALUE_TEXT = 60
CODE_TICK_MS = 1000
STATUS_TICK_MS = 5000
#: While files move, the window follows them this closely.
PROGRESS_TICK_MS = 1000
#: What the collection names is counted again at most this often while the window is open (and after each sync).
COUNT_EVERY_SECONDS = 60
#: Left-out files named in the status; the saved report names more.
LEFT_OUT_SHOWN = 3

#: The mark shown before a status line, by kind, and its colour (None: the text colour).
MARKS = {
    "ok": ("✓", "#2e7d32"),
    "info": ("•", None),
    "busy": ("↻", "#1565c0"),
    "wait": ("…", "#8d6e00"),
    "warn": ("!", "#c77700"),
    "error": ("✕", "#c62828"),
}


def tn(message_id: str, count: int, **values) -> str:
    """A counted message: its ``_one`` form for one, where the language has one."""
    if count == 1:
        one = t(message_id + "_one", count=count, **values)
        if one != message_id + "_one":
            return one
    return t(message_id, count=count, **values)


def size_text(size: int) -> str:
    """A file size the way a person reads it: "512 B", "1.5 MB", "3.0 GB"."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def progress_of(activity):
    """How far the running sync is with files (a ``SyncProgress``), or None."""
    progress = getattr(activity, "progress", None) if activity and activity.syncing else None
    return progress if progress is not None and progress.total else None


def mode_of(folder: str | None) -> str:
    """How a profile syncs: "none", "phone" (this computer serves), "hub" (another computer serves) or "folder"."""
    if not folder:
        return "none"
    if folder.startswith("phone:"):
        return "phone"
    return "hub" if folder.startswith("hub:") else "folder"


def ago(stamp: float, now: float) -> str:
    """When something happened, the way a person says it."""
    seconds = max(0, now - stamp)
    if seconds < 60:
        return t("admin_sync_ago_now")
    if seconds < 3600:
        return t("admin_sync_ago_minutes", count=int(seconds // 60))
    if time.localtime(stamp)[:3] == time.localtime(now)[:3]:
        return t("admin_sync_ago_today", time=time.strftime("%H:%M", time.localtime(stamp)))
    return t("admin_sync_ago_date", time=time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)))


#: The stages a document or file goes through, in order, and the colour each has in the bar and its box.
STAGE_COLOURS = {
    "have": "#43a047",       # on the other device: synced
    "fetching": "#1e88e5",   # the other device is downloading it now
    "waiting": "#90a4ae",    # ready here, the other device has not asked for it yet
    "ready": "#7cb342",      # ready here (the other device does not say what it has)
    "preparing": "#fb8c00",  # new or changed here, still to be copied for syncing
    "cannot": "#e53935",     # missing here, or too big
}


class Tile:
    """One stage: its key, how many are in it, what it is called and a short line on what it means."""

    def __init__(self, key: str, count: int, label: str, hint: str):
        self.key, self.count, self.label, self.hint = key, count, label, hint


class Overview:
    def __init__(self, headline: str, *, percent: int | None = None, complete: bool = False, tiles=(),
                 segments=(), left_out=(), note: str = ""):
        self.headline, self.percent, self.complete, self.note = headline, percent, complete, note
        self.tiles, self.segments, self.left_out = list(tiles), list(segments), list(left_out)


def _names(inventory, reason: str) -> str:
    names = [name for why, name in inventory.left_out if why == reason]
    count = inventory.missing if reason == "missing" else inventory.too_big
    return ", ".join(names[:LEFT_OUT_SHOWN]) + (" …" if count > len(names[:LEFT_OUT_SHOWN]) else "")


def overview(inventory, *, target: str | None = None, counting: bool = False, syncing: bool = False) -> Overview:
    """Where the collection's documents and files are, for ``target`` (the device they go to, as
    ``inventory.deliveries`` names it; "" or None for "the other device"): on it, downloading to it, waiting for it,
    still being prepared here, or unable to sync."""
    if inventory is None:
        return Overview(t("admin_sync_counting") if counting else "")
    if not inventory.found:
        return Overview(t("admin_sync_found_none"))
    name = target or t("admin_sync_other_device")
    delivery = dict(inventory.deliveries).get(target or "") if inventory.deliveries else None
    cannot = inventory.missing + inventory.too_big
    syncable = inventory.found - cannot
    preparing_hint = t("admin_sync_tile_preparing_now" if syncing else "admin_sync_tile_preparing_hint")
    tail = [Tile("preparing", inventory.preparing, t("admin_sync_tile_preparing"), preparing_hint),
            Tile("cannot", cannot, t("admin_sync_tile_cannot"), t("admin_sync_tile_cannot_hint"))]
    left_out = [t(key, names=_names(inventory, reason)) for reason, key in
                (("missing", "admin_sync_cannot_missing"), ("too_big", "admin_sync_cannot_too_big"))
                if (inventory.missing if reason == "missing" else inventory.too_big)]
    if delivery is None:
        tiles = [Tile("ready", inventory.ready, t("admin_sync_tile_ready"), t("admin_sync_tile_ready_hint")), *tail]
        note = t("admin_sync_not_said", name=name) if target else ""
        return Overview(t("admin_sync_overview_ready", ready=inventory.ready, total=inventory.found),
                        percent=round(100 * inventory.ready / syncable) if syncable else None, tiles=tiles,
                        segments=[(tile.key, tile.count) for tile in tiles], left_out=left_out, note=note)
    tiles = [Tile("have", delivery.have, t("admin_sync_tile_have", name=name), t("admin_sync_tile_have_hint")),
             Tile("fetching", delivery.fetching, t("admin_sync_tile_fetching", name=name),
                  t("admin_sync_tile_fetching_hint")),
             Tile("waiting", delivery.waiting, t("admin_sync_tile_waiting", name=name),
                  t("admin_sync_tile_waiting_hint", name=name)), *tail]
    left = syncable - delivery.have
    complete = left <= 0 and syncable > 0
    if complete:
        headline = t("admin_sync_overview_all", count=delivery.have, name=name)
    else:
        headline = (t("admin_sync_overview_of", have=delivery.have, total=syncable, name=name) + " · "
                    + tn("admin_sync_overview_left", left))
    return Overview(headline, percent=int(100 * delivery.have / syncable) if syncable else None, complete=complete,
                    tiles=tiles, segments=[(tile.key, tile.count) for tile in tiles], left_out=left_out)


def status_lines(mode: str, status, *, phones=(), activity=None, now: float | None = None) -> list[tuple[str, str]]:
    """What the window says about the sync, as (kind, text) lines; kinds are the keys of ``MARKS``.

    For a phone: whether each phone has this computer's newest changes (it synced after they were sent), whether
    the phone's newest changes are still on their way here, and anything waiting, to decide or that failed.
    """
    now = time.time() if now is None else now
    syncing = bool(activity and activity.syncing)
    error = activity.error if activity else None
    progress = progress_of(activity)
    running = (("busy", t(f"admin_sync_progress_{progress.stage}", done=progress.done, total=progress.total))
               if progress else ("busy", t("admin_sync_exchanging")))
    lines: list[tuple[str, str]] = []
    if mode == "phone":
        for phone in phones:
            # What the phone says it has placed, when it says (an older app does not): a phone that asked after the
            # newest changes may still be fetching their documents.
            waiting, behind = getattr(phone, "files_waiting", None), getattr(phone, "behind", None)
            if phone.seen is None:
                lines.append(("info", t("admin_sync_phone_never", name=phone.name)))
            elif waiting:
                lines.append(("busy", tn("admin_sync_phone_fetching", waiting, name=phone.name,
                                         when=ago(phone.seen, now))))
            elif behind:
                lines.append(("info", t("admin_sync_phone_placing", name=phone.name, when=ago(phone.seen, now))))
            elif status.last_published and phone.seen < status.last_published:
                lines.append(("info", t("admin_sync_phone_behind", name=phone.name, when=ago(phone.seen, now))))
            else:
                lines.append(("ok", t("admin_sync_phone_current", name=phone.name, when=ago(phone.seen, now))))
        newest = max((phone.changed for phone in phones if phone.changed), default=None)
        if syncing:
            lines.append(running)
        elif newest and (not status.last_sync or newest > status.last_sync):
            lines.append(("busy", t("admin_sync_phone_incoming")))
    else:
        if syncing:
            lines.append(running)
        elif status.last_sync:
            lines.append(("ok", t("admin_sync_last_synced", when=ago(status.last_sync, now))))
        else:
            lines.append(("info", t("admin_sync_never_synced")))
    to_send, to_receive = getattr(status, "files_to_send", 0), getattr(status, "files_to_receive", 0)
    if to_send and not progress:
        lines.append(("wait", tn("admin_sync_files_to_send", to_send,
                                 size=size_text(getattr(status, "bytes_to_send", 0)))))
    if to_receive and not progress:
        lines.append(("wait", tn("admin_sync_files_to_receive", to_receive)))
    if status.waiting_for_anki:
        lines.append(("wait", tn("admin_sync_waiting_for_anki", status.waiting_for_anki)))
    if status.waiting_for_files:
        lines.append(("wait", tn("admin_sync_waiting_for_files", status.waiting_for_files)))
    if getattr(status, "stale_waiting", 0):
        lines.append(("warn", tn("admin_sync_stale", status.stale_waiting)))
    if status.conflicts:
        lines.append(("warn", tn("admin_sync_conflicts", status.conflicts)))
    if error:
        lines.append(("error", t("admin_sync_failed", error=error)))
    if mode == "phone" and phones and all(kind == "ok" for kind, _ in lines):
        lines.insert(0, ("ok", t("admin_sync_up_to_date")))
    return lines


def lines_html(lines: list[tuple[str, str]]) -> str:
    rows = []
    for kind, text in lines:
        mark, colour = MARKS[kind]
        style = f' style="color:{colour}"' if colour else ""
        rows.append(f"<tr><td width=22{style}><b>{mark}</b></td><td>{html.escape(text)}</td></tr>")
    return "<table cellspacing=0 cellpadding=3>" + "".join(rows) + "</table>"


def _family_label(family: str, card: str) -> str:
    labels = {
        "priority": lambda: t("admin_sync_family_priority", card=card),
        "reader_bookmark": lambda: t("admin_sync_family_reader_bookmark", card=card),
        "reading_position": lambda: t("admin_sync_family_reading_position", card=card),
        "pdf_highlight": lambda: t("admin_sync_family_pdf_highlight", card=card),
        "epub_highlight": lambda: t("admin_sync_family_epub_highlight", card=card),
        "document": lambda: t("admin_sync_family_document", card=card),
    }
    return labels[family]() if family in labels else f"{family} {card}"


def describe_value(family: str, value) -> str:
    """A short, readable form of one side of a conflict."""
    if value is None:
        return t("admin_sync_deleted")
    if family == "priority":
        return f"{value['priority']:g}"
    if family == "reading_position":
        for field in ("page", "section_index", "position"):
            if field in value:
                return f"{field.split('_')[0]} {value[field]:g}"
    if family in ("reader_bookmark", "pdf_highlight", "epub_highlight"):
        text = value.get("comment_text") or value.get("note") or value.get("label") or value.get("text") or ""
        if text:
            return text if len(text) <= MAX_VALUE_TEXT else text[:MAX_VALUE_TEXT - 1] + "…"
    compact = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return compact if len(compact) <= MAX_VALUE_TEXT else compact[:MAX_VALUE_TEXT - 1] + "…"


def conflict_text(conflict) -> str:
    card = conflict.key.split("/")[0].partition(":")[2]
    mine = conflict.values[conflict.local_index]
    theirs = next(value for index, value in enumerate(conflict.values) if index != conflict.local_index)
    return t("admin_sync_conflict_row", what=_family_label(conflict.family, card),
             mine=describe_value(conflict.family, mine), theirs=describe_value(conflict.family, theirs))


def _text(message: str = "", *, small: bool = False) -> QLabel:
    label = QLabel(message)
    label.setWordWrap(True)
    if small:
        label.setStyleSheet("color: palette(placeholder-text);")
    return label


def _heading(message: str = "", scale: float = 1.15) -> QLabel:
    label = QLabel(message)
    font = label.font()
    font.setBold(True)
    font.setPointSizeF(font.pointSizeF() * scale)
    label.setFont(font)
    return label


def _link(message: str) -> QPushButton:
    """A quiet action: a flat button that reads like a link."""
    button = QPushButton(message)
    button.setFlat(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class StageBar(QWidget):
    """One bar for the whole collection, split by stage in the stages' colours."""

    def __init__(self):
        super().__init__()
        self._segments: list[tuple[str, int]] = []
        self.setFixedHeight(16)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_segments(self, segments) -> None:
        self._segments = [(key, count) for key, count in segments if count > 0]
        self.setToolTip(" · ".join(f"{count}" for _, count in self._segments))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = self.rect()
        painter.setBrush(self.palette().mid())
        painter.drawRoundedRect(rect, 8, 8)
        total = sum(count for _, count in self._segments)
        if not total:
            return
        painter.setClipRect(rect)
        x = 0.0
        for index, (key, count) in enumerate(self._segments):
            width = rect.width() * count / total
            right = rect.width() if index == len(self._segments) - 1 else round(x + width)
            painter.setBrush(QColor(STAGE_COLOURS[key]))
            painter.drawRect(round(x), 0, max(1, right - round(x)), rect.height())
            x += width
        painter.end()


class StageTile(QFrame):
    """A stage's box: its colour, how many are in it, what it is and what that means."""

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        top = QHBoxLayout()
        swatch = QLabel()
        swatch.setFixedSize(10, 10)
        swatch.setStyleSheet(f"background: {STAGE_COLOURS[key]}; border-radius: 5px;")
        top.addWidget(swatch)
        self._number = QLabel("0")
        font = self._number.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.6)
        self._number.setFont(font)
        top.addWidget(self._number)
        top.addStretch(1)
        layout.addLayout(top)
        self._label = QLabel()
        self._label.setWordWrap(True)
        bold = self._label.font()
        bold.setBold(True)
        self._label.setFont(bold)
        layout.addWidget(self._label)
        self._hint = _text(small=True)
        layout.addWidget(self._hint)
        layout.addStretch(1)

    def show_tile(self, tile) -> None:
        self._number.setText(f"{tile.count:,}".replace(",", "\u202f"))
        self._label.setText(tile.label)
        self._hint.setText(tile.hint)
        self.setEnabled(tile.count > 0)  # an empty stage is greyed, still there to say it is empty

    def count(self) -> str:
        return self._number.text()


def _row(*widgets, stretch_first: bool = False) -> QHBoxLayout:
    row = QHBoxLayout()
    if stretch_first:
        row.addStretch(1)
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch(1)
    return row


class SyncDialog(QDialog):
    def __init__(self, profile: str, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._conflicts = []
        self._phone_names: list[str] = []
        self._configured = False
        self._mode = "none"
        self._pairing = False
        self._running = False
        self._stale = 0
        self._activity = None
        self._inventory = None
        self._counting = False
        self._counted_at = 0.0
        self._was_syncing = False
        self._base_height = 0  # the height before the window grew to fit new lines
        self._phones_seen = None  # when each phone last synced, at the last look
        self.setWindowTitle(t("admin_sync_title", profile=profile))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_setup())
        layout.addWidget(self._build_pairing())
        layout.addWidget(self._build_status())

        close_row = QHBoxLayout()
        # Quiet, for when something seems wrong: a report to send with a question.
        self._save_log = _link(t("admin_sync_save_log"))
        self._save_log.setToolTip(t("admin_sync_save_log_tip"))
        self._save_log.clicked.connect(self._save_report)
        close_row.addWidget(self._save_log)
        close_row.addStretch(1)
        self._sync = QPushButton(t("admin_sync_now"))
        self._sync.clicked.connect(self._sync_now)
        close_row.addWidget(self._sync)
        close = QPushButton(t("common_close"))
        close.clicked.connect(self.reject)
        close_row.addWidget(close)
        layout.addLayout(close_row)

        self._ticker = QTimer(self)
        self._ticker.timeout.connect(self.refresh)
        self.refresh()

    # --- building ----------------------------------------------------------------

    def _build_setup(self) -> QWidget:
        """What to choose while nothing is set up: the phone first, a cloud folder as the smaller second choice."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_text(t("admin_sync_scope")))

        layout.addWidget(_heading(t("admin_sync_phone_heading")))
        layout.addWidget(_text(t("admin_sync_phone_explanation")))
        self._phone_setup = QPushButton(t("admin_sync_phone_setup"))
        self._phone_setup.setDefault(True)
        self._phone_setup.clicked.connect(self.use_phone)
        layout.addLayout(_row(self._phone_setup))
        self._phone_unavailable = _text(small=True)
        layout.addWidget(self._phone_unavailable)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(rule)
        layout.addWidget(_text(t("admin_sync_cloud_heading")))
        layout.addWidget(_text(t("admin_sync_cloud_explanation"), small=True))
        self._choose = QPushButton(t("admin_sync_choose"))
        self._choose.clicked.connect(self._choose_folder)
        layout.addLayout(_row(self._choose))
        self._hub = _link(t("admin_sync_advanced"))
        self._hub.clicked.connect(self._ask_for_hub)
        layout.addLayout(_row(self._hub))
        layout.addWidget(_text(t("admin_sync_local_only"), small=True))
        self._setup = page
        return page

    def _build_pairing(self) -> QWidget:
        """Pairing a phone: two steps, the code, and what to do if the phone cannot find this computer."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_heading(t("admin_sync_pair_heading")))
        layout.addWidget(_text(t("admin_sync_pair_steps")))
        self._code = QLabel()
        self._code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self._code.font()
        font.setPointSizeF(font.pointSizeF() * 2.6)
        font.setBold(True)
        self._code.setFont(font)
        layout.addWidget(self._code)
        self._code_note = _text(small=True)
        self._code_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._code_note)
        self._new_code = QPushButton(t("admin_sync_phone_new_code"))
        self._new_code.clicked.connect(self._make_code)
        self._cancel_code = _link(t("admin_sync_pair_cancel"))
        self._cancel_code.clicked.connect(self._stop_pairing)
        layout.addLayout(_row(self._new_code, self._cancel_code, stretch_first=True))
        self._address = _text(small=True)
        self._address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._address)
        layout.addWidget(_text(t("admin_sync_phone_firewall"), small=True))
        self._pairing_page = page
        return page

    def _build_status(self) -> QWidget:
        """Once set up: one heading, the status lines, conflicts when there are any, and a row of quiet actions."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._mode_heading = _heading()
        layout.addWidget(self._mode_heading)
        self._where = _text(small=True)
        self._where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._where)

        # What went wrong comes first, where it cannot be missed.
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._error.setStyleSheet(f"background: {MARKS['error'][1]}; color: white; padding: 8px; border-radius: 4px;")
        self._error.setHidden(True)
        layout.addWidget(self._error)

        # Where the documents and files are: one sentence, one bar, one box per stage.
        self._headline = _heading(scale=1.3)
        self._headline.setWordWrap(True)
        self._percent = _heading(scale=1.3)
        headline_row = QHBoxLayout()
        headline_row.addWidget(self._headline, 1)
        headline_row.addWidget(self._percent, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        layout.addLayout(headline_row)
        self._bar = StageBar()
        self._bar.setAccessibleName(t("admin_sync_progress_name"))
        layout.addWidget(self._bar)
        self._tiles = {key: StageTile(key) for key in ("have", "fetching", "waiting", "ready", "preparing", "cannot")}
        self._tile_grid = QGridLayout()
        self._tile_grid.setSpacing(6)
        layout.addLayout(self._tile_grid)
        self._left_out = _text(small=True)
        self._left_out.setStyleSheet(f"color: {MARKS['warn'][1]};")
        self._left_out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._left_out)
        self._note = _text(small=True)
        layout.addWidget(self._note)

        # The sync running now, then the rest in short lines.
        self._progress = QProgressBar()
        self._progress.setAccessibleName(t("admin_sync_progress_name"))
        self._progress.setHidden(True)
        layout.addWidget(self._progress)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.RichText)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(self._status)
        self._discard = _link(t("admin_sync_discard"))
        self._discard.clicked.connect(self._discard_stale)
        card_layout.addLayout(_row(self._discard))
        layout.addWidget(card)
        self._how = _text(small=True)
        layout.addWidget(self._how)

        layout.addWidget(self._build_conflicts())

        self._pair_another = _link(t("admin_sync_pair_another"))
        self._pair_another.clicked.connect(self._make_code)
        self._remove = _link(t("admin_sync_phone_remove"))
        self._remove.clicked.connect(self._remove_phone)
        self._change = _link(t("admin_sync_change"))
        self._change.clicked.connect(self._change_location)
        self._stop = _link(t("admin_sync_stop"))
        self._stop.clicked.connect(self._stop_syncing)
        layout.addLayout(_row(self._pair_another, self._remove, self._change, self._stop))
        self._status_page = page
        return page

    def _build_conflicts(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_text(t("admin_sync_conflicts_heading")))
        self._list = QListWidget()
        self._list.setAccessibleName(t("admin_sync_conflicts_heading"))
        self._list.currentRowChanged.connect(lambda _row: self._update_buttons())
        layout.addWidget(self._list)
        self._keep_mine = QPushButton(t("admin_sync_keep_mine"))
        self._keep_mine.clicked.connect(lambda: self._resolve(mine=True))
        self._use_theirs = QPushButton(t("admin_sync_use_theirs"))
        self._use_theirs.clicked.connect(lambda: self._resolve(mine=False))
        layout.addLayout(_row(self._keep_mine, self._use_theirs))
        self._conflicts_box = box
        return box

    # --- state -------------------------------------------------------------------

    def _count(self, *, again: bool = False) -> None:
        """Count what the collection names, in the background; at most one count at a time."""
        start = getattr(self._controller, "inventory", None)
        if start is None or self._counting:
            return
        if not again and self._inventory is not None and time.time() - self._counted_at < COUNT_EVERY_SECONDS:
            return
        self._counting = True
        self._counted_at = time.time()
        start(self._counted)

    def _counted(self, inventory) -> None:
        self._counting = False
        if inventory is not None:
            self._inventory = inventory
        try:
            if self.isVisible() or self._ticker.isActive():
                self.refresh()
        except RuntimeError:  # the window closed while counting
            pass

    def refresh(self) -> None:
        self._activity = self._controller.activity()
        syncing = bool(self._activity and self._activity.syncing)
        finished, self._was_syncing = self._was_syncing and not syncing, syncing
        status = self._controller.status()
        self._configured = bool(status.configured)
        self._mode = mode_of(status.folder if status.configured else None)
        info = self._controller.phone() if self._mode == "phone" else None
        # Pairing shows while a code is open, and until the first phone is paired; then the status takes over.
        self._pairing = bool(info and info.running and (info.code or not info.phones))
        self._setup.setVisible(self._mode == "none")
        self._pairing_page.setVisible(self._pairing)
        self._status_page.setVisible(self._mode != "none" and not self._pairing)
        if self._mode == "none":
            reason = self._controller.phone_unavailable()
            self._phone_unavailable.setText(t("admin_sync_phone_unavailable", reason=reason) if reason else "")
            self._phone_unavailable.setVisible(bool(reason))
        elif self._pairing:
            self._show_pairing(info)
        else:
            self._count(again=finished)
            self._show_status(status, info)
        moving = self._mode != "none" and not self._pairing and progress_of(self._activity) is not None
        self._ticker.setInterval(CODE_TICK_MS if self._pairing else PROGRESS_TICK_MS if moving else STATUS_TICK_MS)
        if self._mode == "none":
            self._ticker.stop()
        elif not self._ticker.isActive():
            self._ticker.start()
        self._update_buttons()
        QTimer.singleShot(0, self._fit)  # once Qt has measured the new text

    def _fit(self) -> None:
        """Grow the window when its wrapped text needs more room (Qt does not, for lines added once it is open)."""
        try:
            if not self.isVisible():
                return
        except RuntimeError:  # closed meanwhile
            return
        layout = self.layout()
        layout.activate()
        needed = (layout.totalHeightForWidth(self.width()) if layout.hasHeightForWidth()
                  else layout.totalSizeHint().height())
        # Grown by the window itself, it gives the room back as lines go, down to the height it had before.
        if needed > self.height():
            self._base_height = self._base_height or self.height()
            self.resize(self.width(), needed)
        elif self._base_height and self.height() > max(needed, self._base_height):
            self.resize(self.width(), max(needed, self._base_height))

    def _show_pairing(self, info) -> None:
        self._phone_names = [phone.name for phone in info.phones]
        if info.code:
            self._code.setText(info.code)
            self._code_note.setText(t("admin_sync_phone_code_left", minutes=info.seconds_left // 60,
                                      seconds=info.seconds_left % 60))
            address = t("admin_sync_phone_address_code", code=info.code.replace(" ", ""), address=info.address)
            if getattr(info, "tailscale", ""):
                address += "\n" + t("admin_sync_phone_tailscale_code", code=info.code.replace(" ", ""),
                                     address=info.tailscale)
            self._address.setText(address)
        else:
            self._code.setText("— — —")
            self._code_note.setText(t("admin_sync_phone_code_expired"))
            address = t("admin_sync_phone_address", address=info.address)
            if getattr(info, "tailscale", ""):
                address += "\n" + t("admin_sync_phone_tailscale", address=info.tailscale)
            self._address.setText(address)
        self._new_code.setVisible(not info.code)
        self._cancel_code.setVisible(bool(info.phones))

    def _show_status(self, status, info) -> None:
        heading = {"phone": "admin_sync_mode_phone", "folder": "admin_sync_mode_folder", "hub": "admin_sync_mode_hub"}
        self._mode_heading.setText(t(heading[self._mode]))
        self._where.setText(self._where_text(status))
        self._where.setVisible(self._mode in ("folder", "hub"))
        activity = self._activity
        progress = progress_of(activity)
        self._progress.setVisible(progress is not None)
        if progress is not None:
            self._progress.setRange(0, progress.total)
            self._progress.setValue(min(progress.done, progress.total))
        if self._mode == "phone" and not info.running:
            lines = [("error", t("admin_sync_phone_not_running"))]
            phones = []
        else:
            phones = list(info.phones) if info else []
            lines = status_lines(self._mode, status, phones=phones, activity=activity)
        self._phone_names = [phone.name for phone in phones]
        errors = [text for kind, text in lines if kind == "error"]
        self._error.setText("\n".join(errors))
        self._error.setVisible(bool(errors))
        # A phone that synced may have taken files: count again at once.
        seen = tuple((phone.name, phone.seen) for phone in phones)
        if self._phones_seen is not None and seen != self._phones_seen:
            self._count(again=True)
        self._phones_seen = seen
        complete = self._show_overview(phones, activity)
        details = [(kind, text) for kind, text in lines if kind != "error"]
        if not complete:  # files still left: not "up to date", whatever the timestamps say
            details = [line for line in details if line[1] != t("admin_sync_up_to_date")]
        self._status.setText(lines_html(details))
        self._how.setText(t("admin_sync_phone_how") if self._mode == "phone" else t("admin_sync_folder_how"))
        self._stale = getattr(status, "stale_waiting", 0)
        self._discard.setVisible(bool(self._stale))
        conflicts = list(self._controller.conflicts())
        if [c.id for c in conflicts] != [c.id for c in self._conflicts]:
            self._conflicts = conflicts
            self._list.clear()
            for conflict in conflicts:
                item = QListWidgetItem(conflict_text(conflict))
                item.setData(Qt.ItemDataRole.UserRole, conflict.id)
                self._list.addItem(item)
            rows = max(1, min(len(conflicts), 6))
            self._list.setFixedHeight(self._list.sizeHintForRow(0) * rows + 2 * self._list.frameWidth() + 4
                                      if conflicts else 0)
        self._conflicts_box.setVisible(bool(self._conflicts))

    def _show_overview(self, phones, activity) -> bool:
        """Show where the documents and files are; True unless some are known to be still left."""
        inventory = self._inventory
        names = dict(inventory.deliveries) if inventory is not None else {}
        # The phone the counts are for: the first paired one that says what it has, else the first paired one.
        if self._mode == "phone":
            target = next((phone.name for phone in phones if phone.name in names),
                          phones[0].name if phones else None)
        else:
            target = ""
        view = overview(inventory, target=target, counting=self._counting,
                        syncing=bool(activity and activity.syncing))
        self._headline.setText(("✓ " if view.complete else "") + view.headline)
        self._headline.setStyleSheet(f"color: {STAGE_COLOURS['have']};" if view.complete else "")
        self._percent.setText(f"{view.percent}%" if view.percent is not None and not view.complete else "")
        self._bar.set_segments(view.segments)
        self._bar.setVisible(bool(view.tiles))
        while self._tile_grid.count():
            self._tile_grid.takeAt(0)
        for tile in self._tiles.values():
            tile.setHidden(True)
        columns = 3
        for index, tile in enumerate(view.tiles):
            box = self._tiles[tile.key]
            box.show_tile(tile)
            box.setHidden(False)
            self._tile_grid.addWidget(box, index // columns, index % columns)
        self._left_out.setText("\n".join(view.left_out))
        self._left_out.setVisible(bool(view.left_out))
        others = [t("admin_sync_other_phone", name=name, have=delivery.have,
                    total=inventory.found - inventory.missing - inventory.too_big)
                  for name, delivery in names.items() if name and name != target]
        note = "\n".join(filter(None, [view.note, *others]))
        self._note.setText(note)
        self._note.setVisible(bool(note))
        return view.complete or inventory is None or not view.tiles

    def _selected(self):
        row = self._list.currentRow()
        return self._conflicts[row] if self._conflicts and 0 <= row < len(self._conflicts) else None

    def _update_buttons(self) -> None:
        idle = not self._running
        chosen = self._selected() is not None and idle
        self._keep_mine.setEnabled(chosen)
        self._use_theirs.setEnabled(chosen)
        status_shown = self._mode != "none" and not self._pairing
        # A phone syncs by itself; Sync now prepares this computer's changes at once (for a phone too).
        self._sync.setVisible(status_shown)
        self._sync.setEnabled(idle)
        self._sync.setText(t("admin_sync_running") if self._running else t("admin_sync_now"))
        self._stop.setText(t("admin_sync_phone_stop") if self._mode == "phone" else t("admin_sync_stop"))
        self._stop.setEnabled(idle)
        self._pair_another.setVisible(self._mode == "phone")
        self._remove.setVisible(self._mode == "phone" and bool(self._phone_names))
        self._change.setVisible(self._mode in ("folder", "hub"))
        self._change.setEnabled(idle)
        self._choose.setEnabled(idle)
        self._hub.setEnabled(idle)
        self._phone_setup.setEnabled(idle and not self._controller.phone_unavailable())

    # --- actions -----------------------------------------------------------------

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, t("admin_sync_choose_folder"), "")
        if folder:
            self.use_folder(folder)

    def _where_text(self, status) -> str:
        """A folder as chosen; a hub by the device it was paired as, never by anything that could be a key."""
        if self._mode == "hub":
            return t("admin_sync_hub_label", device=status.folder.split(":")[1])
        return status.folder if self._mode == "folder" else ""

    def _change_location(self) -> None:
        if self._mode == "hub":
            self._ask_for_hub()
        else:
            self._choose_folder()

    def _ask_for_hub(self) -> None:
        code, accepted = QInputDialog.getText(self, t("admin_sync_advanced_title"), t("admin_sync_hub_prompt"))
        if accepted and code.strip():
            self.use_hub(code)

    def use_hub(self, code: str) -> None:
        try:
            self._controller.configure_hub(code.strip())
        except Exception as exc:  # the service explains why a hub cannot be used
            showInfo(t("admin_sync_setup_failed", error=exc), parent=self)
        self.refresh()

    def use_folder(self, folder: str) -> None:
        try:
            self._controller.configure(folder)
        except Exception as exc:  # the service explains why a folder cannot be used
            showInfo(t("admin_sync_setup_failed", error=exc), parent=self)
        self.refresh()

    def use_phone(self) -> None:
        try:
            self._controller.start_phone()
        except Exception as exc:
            showInfo(t("admin_sync_phone_failed", error=exc), parent=self)
        self.refresh()

    def _make_code(self) -> None:
        self._controller.new_code()
        self.refresh()

    def _stop_pairing(self) -> None:
        self._controller.cancel_code()
        self.refresh()

    def _remove_phone(self) -> None:
        names = list(self._phone_names)
        if len(names) > 1:
            name, accepted = QInputDialog.getItem(self, t("admin_sync_phone_remove"), t("admin_sync_phone_which"),
                                                  names, 0, False)
            if not accepted:
                return
        elif names:
            name = names[0]
        else:
            return
        self.remove_phone(name)

    def remove_phone(self, name: str) -> None:
        if askUser(t("admin_sync_phone_remove_confirm", name=name), parent=self):
            self._controller.remove_phone(name)
            self.refresh()

    def _sync_now(self) -> None:
        self._running = True
        self._update_buttons()
        self._controller.sync(self._synced)

    def _synced(self) -> None:
        self._running = False
        self._count(again=True)
        self.refresh()

    def _save_report(self) -> None:
        save = getattr(self._controller, "save_log", None)
        if save is None:
            return
        name = time.strftime("incremento-sync-report-%Y%m%d-%H%M.txt")
        start = os.path.join(os.path.expanduser("~"), "Desktop")
        start = os.path.join(start if os.path.isdir(start) else os.path.expanduser("~"), name)
        path, _ = QFileDialog.getSaveFileName(self, t("admin_sync_save_log"), start, t("admin_sync_save_log_filter"))
        if not path:
            return

        def saved(error) -> None:
            if error:
                showWarning(t("admin_sync_save_log_failed", error=error), parent=self)
            else:
                showInfo(t("admin_sync_save_log_done", path=path), parent=self)

        save(path, saved)

    def _discard_stale(self) -> None:
        if askUser(t("admin_sync_discard_confirm", count=self._stale), parent=self):
            self._controller.discard_stale()
            self.refresh()

    def _stop_syncing(self) -> None:
        question = "admin_sync_phone_stop_confirm" if self._mode == "phone" else "admin_sync_stop_confirm"
        if askUser(t(question), parent=self):
            self._controller.disable()
            self.refresh()

    def _resolve(self, *, mine: bool) -> None:
        conflict = self._selected()
        if conflict is None:
            return
        if mine:
            value = conflict.values[conflict.local_index]
        else:
            value = next(v for index, v in enumerate(conflict.values) if index != conflict.local_index)
        self._controller.resolve(conflict.id, value)
        self.refresh()

    def done(self, result: int) -> None:
        self._ticker.stop()
        super().done(result)
