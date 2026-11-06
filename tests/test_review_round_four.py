"""The fourth review round.

The one worth reading twice is the gate scoping. `WRITE_RE` deliberately accepts
`git -C /elsewhere add -A` and `cd /elsewhere; git commit`, and the checker then asked its
question about the caller's OWN cwd — so a session in a safe worktree writing into the shared
checkout looked safe and was permitted. I introduced that hole in round one, along with the
scoping that fixed a different one. A narrowing fix can open a gap of its own, and this suite
is where that gets caught rather than argued about.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest
from sessioncoord import cli, locks, store
from sessioncoord.model import Presence

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_check4", importlib.machinery.SourceFileLoader("gate_check4", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def ANALYSE_TREE(command, cwd):
    """The tree a command writes to; None when it cannot be named."""
    return gate.analyse(command, cwd)[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def fake_world(monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 111, "%2": 222})
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda p, run=None: "me:1")


def rec(sid, pane="%1", pid=111, **kw):
    return Presence(session_id=sid, repo_root="/repo", workdir=kw.pop("workdir", "/repo"),
                    pane=pane, claude_pid=pid, **kw)


@pytest.mark.parametrize("command", [
    "git rm -r .",
    "git rm --cached secrets.env",
    "git mv a.py b.py",
    "git reset --hard HEAD~1",
    "git restore --staged .",
    "git cherry-pick abc123",
    "git revert abc123",
    "git apply --index /tmp/x.patch",
    "git am /tmp/x.patch",
])
def test_these_index_mutating_verbs_are_gated(command):
    # `git rm -r .` in a shared checkout destroys a peer's uncommitted work outright, which is
    # worse than the `git add -A` case the gate was built for -- and it was not covered.
    #
    # Renamed from "every index-mutating verb": review round eight pointed out that the old
    # name claimed a completeness the list did not have, and a test name that overclaims is
    # worse than a short list, because it stops anyone going back to check.
    assert gate.analyse(command, "/repo")[0], command


class TestTheEffectiveTree:
    def test_an_ordinary_command_is_scoped_to_the_callers_cwd(self):
        assert ANALYSE_TREE("git add -A", "/repo/.worktrees/mine") == \
            "/repo/.worktrees/mine"

    def test_an_absolute_dash_c_is_resolved_to_its_target(self):
        # Writing into the shared checkout from a safe worktree. Scoping this to the caller's
        # own tree is what made it look safe.
        assert ANALYSE_TREE("git -C /repo add -A", "/repo/.worktrees/mine") == "/repo"

    def test_a_relative_dash_c_resolves_against_the_cwd(self):
        assert ANALYSE_TREE("git -C ../.. add -A", "/repo/a/b") == "/repo/a/b/../.."

    @pytest.mark.parametrize("command", [
        "cd /repo && git commit -s -m x",
        "cd /repo; git add -A",
        "git -C /repo -C /other add -A",        # ambiguous; do not guess
        "git --work-tree=/repo add -A",
        "git -C $TARGET add -A",                # unexpandable
        "pushd /repo && git add -A",
    ])
    def test_anything_it_cannot_resolve_falls_back_to_repo_wide(self, command):
        # None means "ask the repo-wide question", which can cost a false refusal and cannot
        # cost a missed collision. That is the right way round for the one thing this blocks.
        assert ANALYSE_TREE(command, "/repo/.worktrees/mine") is None


class TestPresenceLifecycleIsSerialised:
    def test_registration_takes_the_same_lock_as_a_conductor_claim(self, home, fake_world,
                                                                    capsys):
        # A SessionStart that loaded role=None before a claim could save afterwards and erase
        # the new conductor -- making `conductor claim`'s success message false, which is worse
        # than the claim failing outright.
        with locks.hold("/repo", "presence", owner="a peer"):
            rc = cli.main(["register", "--repo", "/repo", "--session", "s1", "--pane", "%1",
                           "--pid", "111", "--wait", "0"])
        assert rc == 75

    def test_so_does_deregistration(self, home, fake_world):
        store.save(rec("going"))
        with locks.hold("/repo", "presence", owner="a peer"):
            assert cli.main(["deregister", "--repo", "/repo", "--session", "going",
                             "--wait", "0"]) == 75

    def test_and_so_does_sweeping(self, home, fake_world):
        with locks.hold("/repo", "presence", owner="a peer"):
            assert cli.main(["sweep", "--repo", "/repo", "--wait", "0"]) == 75


class TestOneBadStateFileCannotTakeDownTheBoard:
    @pytest.mark.parametrize("body", ["[]", '"a string"', "42", "null", "[{}]"])
    def test_valid_json_that_is_not_a_record_is_skipped(self, home, body):
        # `[]` parses fine and then raised AttributeError inside from_dict, which nothing
        # caught -- so one malformed file tracebacked every board and check command in the repo.
        store.save(rec("good"))
        (store.state_dir("/repo") / "sessions" / "bad.json").write_text(body)
        assert [p.session_id for p in store.load_all("/repo")] == ["good"]


class TestMsgAcceptsASessionIdTarget:
    def test_a_session_id_is_translated_to_its_pane(self, home, fake_world, monkeypatch):
        # The parser advertises session ids as targets, and tmux has never heard of one -- so
        # every such target was refused by `send` before this.
        store.save(rec("7df6c2fe-b110", pane="%74", pid=171425, window="me:5"))
        seen = {}
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda t, x, run=None, expect_pid=None, **kw: seen.update(
                                target=t, pid=expect_pid) or "%74")
        assert cli.main(["msg", "--repo", "/repo", "7df6c2fe-b110", "a message"]) == 0
        assert seen["target"] == "%74" and seen["pid"] == 171425

    def test_a_session_id_beats_a_local_title_equal_to_it(self, home, fake_world, monkeypatch):
        # The cross-box sender line gives a session id as the reply address. A session HERE
        # titled with that id used to capture the reply, because titles were matched in the
        # same pass and this repo is searched first (hostile pass on cross-host).
        store.save(Presence(session_id="victim-id", repo_root="/other", workdir="/other",
                            pane=None, claude_pid=333, title="the real one"))
        store.save(rec("attacker", pane=None, pid=111, title="victim-id", named=True))
        assert cli.main(["msg", "--repo", "/repo", "victim-id", "private"]) == 0
        assert [m.recipient for m in store.load_inbox("/other")] == ["victim-id"]
        assert store.load_inbox("/repo") == []

    def test_a_session_id_in_several_repos_goes_to_the_one_with_a_live_pane(
            self, home, fake_world, monkeypatch):
        store.save(Presence(session_id="sid", repo_root="/current", workdir="/current",
                            pane="%1", claude_pid=111, updated="2026-11-06T10:00:00+00:00"))
        store.save(Presence(session_id="sid", repo_root="/abandoned", workdir="/abandoned",
                            pane=None, claude_pid=111, updated="2026-11-06T11:00:00+00:00"))
        monkeypatch.setattr(cli.tmuxio, "send", lambda *a, **k: "%1")
        assert cli.main(["msg", "--repo", "/repo", "sid", "hi"]) == 0
        assert [m.recipient for m in store.load_inbox("/current")] == ["sid"]
        assert store.load_inbox("/abandoned") == []

    def test_a_session_id_with_no_way_to_choose_is_refused_not_guessed(
            self, home, fake_world, capsys):
        # Neither record has a pane, and one has no timestamp: refused, without comparing.
        store.save(Presence(session_id="sid", repo_root="/a", workdir="/a", pane=None,
                            claude_pid=1, updated=None))
        store.save(Presence(session_id="sid", repo_root="/b", workdir="/b", pane=None,
                            claude_pid=1, updated="2026-11-06T11:00:00+00:00"))
        assert cli.main(["msg", "--repo", "/repo", "sid", "hi"]) == 2
        assert "does not name one" in capsys.readouterr().err
        assert store.load_inbox("/a") == [] and store.load_inbox("/b") == []

    def test_a_pane_target_is_left_alone(self, home, fake_world, monkeypatch):
        store.save(rec("s1", pane="%74", pid=171425))
        seen = {}
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda t, x, run=None, expect_pid=None, **kw: seen.update(target=t) or t)
        cli.main(["msg", "--repo", "/repo", "%74", "a message"])
        assert seen["target"] == "%74"
