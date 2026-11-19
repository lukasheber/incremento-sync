"""Telling the user when a newer Incremento Sync is out on GitHub: once a day at most, and not again once declined."""
import json

from incremento_sync import updates
from incremento_sync.version import VERSION

DAY = 86400


def _release(tag, url="https://github.com/lukasheber/incremento-sync/releases/tag/x"):
    return lambda: {"tag_name": tag, "html_url": url, "draft": False, "prerelease": False}


def test_versions_compare_by_their_numbers():
    assert updates.parse("v0.3.10") == (0, 3, 10)
    assert updates.parse("0.4.0") == (0, 4, 0)
    assert updates.parse("not a version") is None
    assert updates.is_newer("v0.3.10", "0.3.9")
    assert not updates.is_newer("v0.3.2", "0.3.2")
    assert not updates.is_newer("v0.2.0", "0.3.2")
    assert not updates.is_newer("garbage", "0.3.2")


def test_a_newer_release_is_offered_with_its_page(tmp_path):
    state = tmp_path / "update-check.json"

    found = updates.check(state, "0.3.2", now=1_000_000, fetch=_release("v0.3.3", "https://example.org/r"))

    assert found == updates.Release("0.3.3", "https://example.org/r")


def test_the_same_or_an_older_release_is_not_offered(tmp_path):
    assert updates.check(tmp_path / "a.json", "0.3.2", now=1, fetch=_release("v0.3.2")) is None
    assert updates.check(tmp_path / "b.json", "0.3.2", now=1, fetch=_release("v0.3.1")) is None


def test_github_is_asked_at_most_once_a_day(tmp_path):
    state = tmp_path / "update-check.json"
    calls = []

    def fetch():
        calls.append(1)
        return _release("v0.3.3")()

    assert updates.check(state, "0.3.2", now=1_000_000, fetch=fetch) is not None
    assert updates.check(state, "0.3.2", now=1_000_000 + DAY - 1, fetch=fetch) is None
    assert updates.check(state, "0.3.2", now=1_000_000 + DAY, fetch=fetch) is not None
    assert len(calls) == 2


def test_a_declined_version_is_not_offered_again_but_a_later_one_is(tmp_path):
    state = tmp_path / "update-check.json"
    updates.dismiss(state, "0.3.3")

    assert updates.check(state, "0.3.2", now=1_000_000, fetch=_release("v0.3.3")) is None
    assert updates.check(state, "0.3.2", now=1_000_000 + DAY, fetch=_release("v0.3.4")) == \
        updates.Release("0.3.4", "https://github.com/lukasheber/incremento-sync/releases/tag/x")


def test_no_network_or_a_strange_answer_is_quietly_nothing(tmp_path):
    def offline():
        raise OSError("no network")

    assert updates.check(tmp_path / "a.json", "0.3.2", now=1, fetch=offline) is None
    assert updates.check(tmp_path / "b.json", "0.3.2", now=1, fetch=lambda: ["not", "a", "release"]) is None
    (tmp_path / "c.json").write_text("{damaged", encoding="utf-8")
    assert updates.check(tmp_path / "c.json", "0.3.2", now=1, fetch=_release("v0.3.3")) is not None


def test_the_add_on_knows_its_own_version():
    assert updates.parse(VERSION) is not None
    assert json.loads(open(updates.__file__.replace("updates.py", "config.json"), encoding="utf-8").read())[
        "check_for_updates"] is True
