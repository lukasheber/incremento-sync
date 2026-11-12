"""Video files for sync: the video of an "Incremento Video" card, and its subtitles, travel as blobs.

A video topic made from a local file (or a downloaded one) keeps its file and subtitle files in the profile's
``videos/`` folder. The note travels through Anki; this family carries the files and places them where the note says.
"""

import itertools
import sqlite3

import pytest

import paths
import sync_apply
import sync_capture
import sync_exchange
import sync_paths
import sync_records
import sync_videos
from sync_records import CardRef

NOTE_TYPE = "Incremento Video"
FIELDS = ("Title", "Local_Video_File", "Target_Subtitle_File", "Target_Subtitle_Label", "Reference_Subtitle_File",
          "Reference_Subtitle_Label")
CARDS = {
    7: CardRef(card_id=7, note_id=70, guid="guid-7", uploaded_usn=12),
    8: CardRef(card_id=8, note_id=80, guid="guid-8", uploaded_usn=12),
}
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"video frames " * 200
SUBTITLES = b"WEBVTT\n\n00:00.000 --> 00:02.000\nHello\n"


def _anki(card_ids):
    return {card_id: CARDS[card_id] for card_id in card_ids if card_id in CARDS}


class Device:
    """One computer: its Incremento database, its Anki collection (the note rows sync reads) and its files."""

    def __init__(self, tmp_path, name, folder, *, library_id=None):
        import db
        from db_schema import initialize_schema

        self.addon = tmp_path / name
        self.profile = "Profile"
        self.conn = sqlite3.connect(tmp_path / f"{name}.db", isolation_level="")
        initialize_schema(self.conn, bootstrap=db._create_tables, migrations=db._SCHEMA_MIGRATIONS)
        counter = itertools.count(1)
        sync_capture.enable_sync(self.conn, make_id=lambda: f"{name}-{next(counter)}", library_id=library_id)
        self.col = sqlite3.connect(":memory:")
        self.col.execute("CREATE TABLE notetypes (id INTEGER PRIMARY KEY, name TEXT)")
        self.col.execute("CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT)")
        self.col.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, mid INTEGER, flds TEXT)")
        self.col.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, ord INTEGER)")
        self.col.execute("INSERT INTO notetypes VALUES (1, ?)", (NOTE_TYPE,))
        for ordinal, field in enumerate(FIELDS):
            self.col.execute("INSERT INTO fields VALUES (1, ?, ?)", (ordinal, field))
        self.folder = folder
        self.family = sync_videos.VideoFileFamily(str(self.addon), self.profile)
        self.family.query = lambda sql, *args: self.col.execute(sql, args).fetchall()
        self.families = {**sync_records.FAMILIES, "video": self.family}
        self.store = sync_paths.get_sync_blob_dir(str(self.addon), self.profile)

    @property
    def videos(self):
        return paths.get_videos_dir(str(self.addon), self.profile)

    def note(self, card_id=7, video="videos/lecture-1.mp4", target="", reference=""):
        flds = "\x1f".join(["Lecture", video, target, "English" if target else "", reference,
                            "Português" if reference else ""])
        self.col.execute("INSERT OR REPLACE INTO notes VALUES (?, 1, ?)", (card_id * 10, flds))
        self.col.execute("INSERT OR REPLACE INTO cards VALUES (?, ?, 0)", (card_id, card_id * 10))

    def add(self, name="lecture-1.mp4", data=VIDEO, card_id=7, *, subtitles=None, reference=None):
        self.videos.mkdir(parents=True, exist_ok=True)
        (self.videos / name).write_bytes(data)
        target = ""
        if subtitles:
            (self.videos / subtitles[0]).write_bytes(subtitles[1])
            target = f"videos/{subtitles[0]}"
        ref = ""
        if reference:
            (self.videos / reference[0]).write_bytes(reference[1])
            ref = f"videos/{reference[0]}"
        self.note(card_id, f"videos/{name}", target, ref)

    def push(self):
        sync_capture.capture(self.conn, self.families, _anki, now=lambda: 1)
        return sync_exchange.publish(self.conn, self.folder, blob_store=self.store, now=lambda: 2)

    def pull(self):
        received = sync_exchange.receive(self.conn, self.folder, self.families, blob_store=self.store, now=lambda: 3)
        applied = sync_apply.apply(self.conn, self.families, _anki, blob_store=self.store, now=lambda: 4)
        return received, applied


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "Cloud"
    path.mkdir()
    return path


@pytest.fixture
def pair(tmp_path, folder):
    one = Device(tmp_path, "one", folder)
    two = Device(tmp_path, "two", folder, library_id=sync_capture.sync_identity(one.conn).library_id)
    yield one, two
    one.conn.close()
    two.conn.close()


def test_a_video_reaches_the_other_device_where_its_note_says(pair):
    one, two = pair
    one.add()
    two.note()  # the note arrived through Anki; its video has not

    one.push()
    two.pull()

    assert (two.videos / "lecture-1.mp4").read_bytes() == VIDEO


def test_its_subtitles_come_with_it(pair):
    one, two = pair
    reference = ("lecture-1-pt.srt", "1\n00:00:00,000 --> 00:00:02,000\nOlá\n".encode("utf-8"))
    one.add(subtitles=("lecture-1-en.vtt", SUBTITLES), reference=reference)
    two.note(target="videos/lecture-1-en.vtt", reference="videos/lecture-1-pt.srt")

    one.push()
    two.pull()

    assert (two.videos / "lecture-1.mp4").read_bytes() == VIDEO
    assert (two.videos / "lecture-1-en.vtt").read_bytes() == SUBTITLES
    assert (two.videos / "lecture-1-pt.srt").read_bytes().startswith(b"1\n")


def test_a_subtitle_added_later_ships_and_the_video_does_not_ship_again(pair):
    one, two = pair
    one.add()
    first = one.push()
    one.add(subtitles=("lecture-1-en.vtt", SUBTITLES))

    second = one.push()
    two.note(target="videos/lecture-1-en.vtt")
    two.pull()

    assert first.revisions_published == 1 and second.revisions_published == 1
    assert (two.videos / "lecture-1-en.vtt").read_bytes() == SUBTITLES
    assert (two.videos / "lecture-1.mp4").read_bytes() == VIDEO


def test_a_note_whose_video_is_missing_is_unavailable_not_deleted(pair):
    one, _ = pair
    one.note()  # the note names a video, but the file is not on this device yet

    assert list(one.family.scan(one.conn)) == []
    assert one.family.unavailable(one.conn, "card:7/video") is True


def test_a_note_that_names_no_local_video_has_no_record(pair):
    one, _ = pair
    one.note(video="")  # a video known only by its link

    assert list(one.family.scan(one.conn)) == []
    assert one.family.unavailable(one.conn, "card:7/video") is False


def test_an_unchanged_video_ships_once_and_a_replaced_one_again(pair):
    one, two = pair
    one.add()

    first = one.push()
    again = one.push()
    one.add(data=VIDEO + b"re-encoded")
    replaced = one.push()
    two.note()
    two.pull()

    assert (first.revisions_published, again.revisions_published, replaced.revisions_published) == (1, 0, 1)
    assert (two.videos / "lecture-1.mp4").read_bytes() == VIDEO + b"re-encoded"


def test_two_cards_keep_their_own_videos(pair):
    one, two = pair
    one.add("a-1.mp4", b"first video" * 50, card_id=7)
    one.add("b-1.mp4", b"second video" * 50, card_id=8)
    two.note(7, "videos/a-1.mp4")
    two.note(8, "videos/b-1.mp4")

    one.push()
    two.pull()

    assert (two.videos / "a-1.mp4").read_bytes() == b"first video" * 50
    assert (two.videos / "b-1.mp4").read_bytes() == b"second video" * 50


@pytest.mark.parametrize("relpath", ["videos/../escape.mp4", "videos/sub/dir.mp4", "/etc/passwd", "files/x.mp4", "x.mp4",
                                     "videos/.hidden.mp4", "videos/", "videos/tool.exe", "videos/a\x00b.mp4"])
def test_a_path_that_is_not_a_video_in_the_videos_folder_is_refused(pair, relpath):
    one, _ = pair
    value = {"note_id": 70, "files": [{"slot": "video", "path": relpath, "version": "sha256:" + "a" * 64,
                                       "blob": "a" * 64}]}

    with pytest.raises(ValueError):
        one.family.validate("card:7/video", value)


def test_a_subtitle_must_be_srt_or_vtt_and_a_video_must_not_be(pair):
    one, _ = pair
    video = {"slot": "video", "path": "videos/x.mp4", "version": "sha256:" + "a" * 64, "blob": "a" * 64}
    good = {"slot": "target_subtitle", "path": "videos/x.vtt", "version": "sha256:" + "b" * 64, "blob": "b" * 64}
    one.family.validate("card:7/video", {"note_id": 70, "files": [video, good]})

    with pytest.raises(ValueError):
        one.family.validate("card:7/video", {"note_id": 70, "files": [video, {**good, "path": "videos/x.mp4"}]})
    with pytest.raises(ValueError):
        one.family.validate("card:7/video", {"note_id": 70, "files": [{**video, "path": "videos/x.vtt"}]})
    with pytest.raises(ValueError):  # subtitles travel only with their video
        one.family.validate("card:7/video", {"note_id": 70, "files": [good]})


def test_a_record_with_unexpected_or_repeated_parts_is_refused(pair):
    one, _ = pair
    file = {"slot": "video", "path": "videos/x.mp4", "version": "sha256:" + "a" * 64, "blob": "a" * 64}

    for bad in ({"note_id": 70, "files": [file], "extra": 1}, {"note_id": 70, "files": [file, file]},
                {"note_id": 70, "files": []}, {"note_id": 70, "files": [{**file, "extra": 1}]},
                {"note_id": 70, "files": [{**file, "slot": "audio"}]}, {"note_id": 0, "files": [file]}):
        with pytest.raises(ValueError):
            one.family.validate("card:7/video", bad)


def test_a_key_of_another_family_is_refused(pair):
    one, _ = pair

    with pytest.raises(ValueError):
        one.family.validate("card:7/localfile", None)


def test_deleting_the_record_never_deletes_the_users_file(pair):
    one, _ = pair
    one.add()

    one.family.delete(one.conn, "card:7/video")

    assert (one.videos / "lecture-1.mp4").read_bytes() == VIDEO


def test_a_symlink_is_never_followed(pair, tmp_path):
    one, _ = pair
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"not for sync")
    one.videos.mkdir(parents=True, exist_ok=True)
    try:
        (one.videos / "lecture-1.mp4").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    one.note()

    assert list(one.family.scan(one.conn)) == []


def test_a_video_over_the_size_limit_is_left_out_not_an_error(pair, monkeypatch):
    one, _ = pair
    one.add()
    monkeypatch.setattr(sync_videos, "MAX_VIDEO_BYTES", 100)

    assert list(one.family.scan(one.conn)) == []
    assert one.family.unavailable(one.conn, "card:7/video") is True


def test_a_note_may_name_the_video_with_the_profile_folder_in_front(pair):
    one, two = pair
    one.add()
    one.note(video="user_files/videos/lecture-1.mp4")
    two.note(video="user_files/videos/lecture-1.mp4")

    one.push()
    two.pull()

    assert (two.videos / "lecture-1.mp4").read_bytes() == VIDEO


def test_without_a_collection_the_scan_fails_instead_of_reporting_no_videos(pair):
    one, _ = pair
    one.family.query = None

    with pytest.raises(sync_records.SyncScanError):
        list(one.family.scan(one.conn))


def test_a_collection_without_the_video_note_type_has_no_videos(pair):
    one, _ = pair
    one.col.execute("DELETE FROM notetypes")

    assert list(one.family.scan(one.conn)) == []
