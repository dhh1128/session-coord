"""State on disk: one directory per repo, one file per session, nothing in the repo itself.

Chosen over `<repo>/.ignored/` deliberately (Daniel, 2026-09-17). A session touches several
repos, `.ignored/` multiplies once per worktree and dies when the worktree is removed, and one
state root answers "who is where across the whole estate" in a single scan.
"""

from __future__ import annotations

import os

import json

import pytest
from sessioncoord import store
from sessioncoord.model import Ask, Presence


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


def rec(session_id="s1", repo="/home/user/code/acme/infra", **kw):
    return Presence(session_id=session_id, repo_root=repo,
                    workdir=kw.pop("workdir", repo), pane=kw.pop("pane", "%1"),
                    claude_pid=kw.pop("claude_pid", 100), **kw)


class TestRepoKey:
    def test_the_key_is_a_pure_function_of_the_canonical_path(self):
        # Renamed after the PR #3 review, which was right that the old name -- "two worktrees
        # of one repo share a key" -- claimed a property this cannot have. `repo_key` never
        # consults git, so `repo_key(main)` and `repo_key(main/.worktrees/x)` DIFFER. Collapsing
        # a worktree onto its main checkout is `gitio.repo_root`'s job, and the test that a
        # worktree resolves correctly lives beside it in test_adapters.py. This asserts only
        # what is true here: determinism, and insensitivity to a trailing slash.
        assert store.repo_key("/home/user/code/acme/infra") == store.repo_key(
            "/home/user/code/acme/infra/")

    def test_a_worktree_path_is_deliberately_a_different_key(self):
        # Pinned so nobody "fixes" the callers by passing a raw workdir and silently splits one
        # repo's board in two -- which would defeat every detector without failing anything.
        assert store.repo_key("/home/user/code/acme/infra") != store.repo_key(
            "/home/user/code/acme/infra/.worktrees/cyber")

    def test_different_repos_do_not_collide(self):
        assert store.repo_key("/home/user/code/acme/infra") != store.repo_key(
            "/home/user/code/acme/witness")

    def test_two_repos_with_the_same_basename_do_not_collide(self):
        # acme/keripy and wot/keripy both exist on this box.
        assert store.repo_key("/home/user/code/acme/keripy") != store.repo_key(
            "/home/user/code/wot/keripy")

    def test_the_key_is_readable_enough_to_find_by_hand(self):
        # Daniel debugs by looking; an opaque hash would make the state directory unusable.
        assert "infra" in store.repo_key("/home/user/code/acme/infra")


class TestRoundTrip:
    def test_a_saved_record_comes_back_equal(self, home):
        p = rec(charter="Cyber hardening.", claims=("docs/cyber/**",), branch="cyber")
        store.save(p)
        assert store.load_all(p.repo_root) == [p]

    def test_saving_the_same_session_twice_updates_rather_than_duplicates(self, home):
        store.save(rec(branch="one"))
        store.save(rec(branch="two"))
        loaded = store.load_all("/home/user/code/acme/infra")
        assert len(loaded) == 1 and loaded[0].branch == "two"

    def test_records_are_returned_in_a_stable_order(self, home):
        for sid in ("s3", "s1", "s2"):
            store.save(rec(sid))
        assert [p.session_id for p in store.load_all(
            "/home/user/code/acme/infra")] == ["s1", "s2", "s3"]

    def test_an_unknown_repo_is_empty_not_an_error(self, home):
        assert store.load_all("/home/user/code/nothing/here") == []

    def test_a_record_written_by_a_newer_version_still_loads(self, home):
        # Two sessions can be running different checkouts of this tool mid-upgrade. An
        # unrecognised field must not take the board down for everybody.
        store.save(rec())
        path = next((store.state_dir("/home/user/code/acme/infra") / "sessions").glob("*.json"))
        d = json.loads(path.read_text())
        d["some_future_field"] = ["anything"]
        path.write_text(json.dumps(d))
        assert store.load_all("/home/user/code/acme/infra")[0].session_id == "s1"

    def test_a_corrupt_record_is_skipped_rather_than_fatal(self, home):
        store.save(rec("good"))
        (store.state_dir("/home/user/code/acme/infra") / "sessions" / "bad.json").write_text(
            "{not json")
        assert [p.session_id for p in store.load_all(
            "/home/user/code/acme/infra")] == ["good"]


class TestSweep:
    def test_drops_records_whose_session_is_gone_and_keeps_the_rest(self, home):
        store.save(rec("live", pane="%1", claude_pid=100))
        store.save(rec("dead", pane="%2", claude_pid=200))
        dropped = store.sweep("/home/user/code/acme/infra", {"%1": 100, "%2": None})
        assert [p.session_id for p in dropped] == ["dead"]
        assert [p.session_id for p in store.load_all(
            "/home/user/code/acme/infra")] == ["live"]

    def test_sweeping_an_empty_repo_is_harmless(self, home):
        assert store.sweep("/home/user/code/nothing", {}) == []


class TestAsksPersistence:
    def test_asks_round_trip(self, home):
        a = Ask(tag="Q-ZNYN", kind="Q", text="Merge the PR carrying three sessions' work?",
                session_id="s1", window="infra:11")
        store.save_asks("/home/user/code/acme/infra", [a])
        assert store.load_asks("/home/user/code/acme/infra") == [a]

    def test_no_queue_yet_is_an_empty_list(self, home):
        assert store.load_asks("/home/user/code/acme/infra") == []


class TestKnownRepos:
    def test_lists_every_repo_with_a_registrant(self, home):
        store.save(rec("a", repo="/home/user/code/acme/infra"))
        store.save(rec("b", repo="/home/user/code/me/tools"))
        # The marker stores the realpath, and on macOS /home resolves to
        # /System/Volumes/Data/home, so the expected values go through realpath too.
        assert sorted(store.known_repos()) == [os.path.realpath("/home/user/code/acme/infra"),
                                               os.path.realpath("/home/user/code/me/tools")]

    def test_nothing_registered_anywhere_is_an_empty_list(self, home):
        assert store.known_repos() == []
