"""The fifth review round, and the fourth distinct door onto one property.

Round three's commit message said, of "a partial observation is not an observation": *there
will be a fourth*. There was. `_children()` returned `None` when `/proc` could not be listed —
and then silently skipped every individual `stat` that failed, so a `hidepid=2` boundary
produced a partial process tree, `claude_under` reported no Claude for panes it was refused
sight of, and `sweep` deleted those live records.

The discrimination that fixes it is worth the precision: `ENOENT` means the process exited
between the listdir and the open, which happens constantly on a busy box and is genuinely
absent; `EACCES`/`EPERM` means we were refused, which is unknown. Treating them alike in either
direction is wrong — one way makes the tree permanently unreadable, the other deletes live
sessions.
"""

from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from sessioncoord import detect, procio, store, tmuxio
from sessioncoord.model import Board, Presence

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_check5", importlib.machinery.SourceFileLoader("gate_check5", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def ANALYSE_TREE(command, cwd):
    """The tree a command writes to; None when it cannot be named."""
    return gate.analyse(command, cwd)[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.mark.parametrize("command", [
    "GIT_WORK_TREE=/shared git add -A",
    "GIT_DIR=/shared/.git git commit -s -m x",
    "GIT_INDEX_FILE=/tmp/i git add .",
    "FOO=1 BAR=2 git commit -s",
    "GIT_WORK_TREE=/shared sudo git add -A",
])
def test_a_leading_environment_assignment_does_not_hide_the_write(command):
    # A shell assignment before the executable is not a wrapper keyword, so the prefix grammar
    # did not admit it and the prefilter exited without ever invoking the checker.
    assert gate.analyse(command, "/repo")[0], command


@pytest.mark.parametrize("command", [
    "GIT_WORK_TREE=/shared git add -A",
    "GIT_DIR=/shared/.git git commit -s",
])
def test_and_an_environment_redirect_falls_back_to_the_repo_wide_question(command):
    # The environment points git somewhere the cwd does not, and there is no `-C` to read it
    # from, so the effective tree is not knowable. None means ask repo-wide.
    assert ANALYSE_TREE(command, "/repo/.worktrees/mine") is None


class TestARootGlobClaimsEverything:
    def test_a_repo_wide_glob_normalises_to_the_root_sentinel(self):
        assert detect._norm_claim("**") == detect.ROOT
        assert detect._norm_claim("*") == detect.ROOT
        assert detect._norm_claim("./") == detect.ROOT

    def test_and_therefore_collides_with_every_other_claim(self):
        # It reduced to "" and then collided with NOTHING -- the exact opposite of what a
        # session declaring `--claim '**'` meant by it.
        assert detect._claims_overlap("**", "docs/cyber/census.md")
        assert detect._claims_overlap("claude/install.sh", "**")

    def test_the_detector_reports_it(self):
        def p(sid, pane, claims):
            return Presence(session_id=sid, repo_root="/repo",
                            workdir=f"/repo/.worktrees/{sid}", branch=sid, pane=pane,
                            window=f"w:{sid}", claude_pid=1, claims=claims)

        board = Board.of("/repo", [p("a", "%1", ("**",)), p("b", "%2", ("prmerge/**",))])
        assert "claim-overlap" in [f.code for f in detect.check(board)]


class TestAnUnreadableProcessIsUnknownNotAbsent:
    @pytest.fixture(autouse=True)
    def _proc_path(self, monkeypatch):
        # These pin the /proc walk, so they run it even on a box without /proc; the ps path
        # has its own tests in test_tmuxio_without_proc.py.
        monkeypatch.setattr(procio, "_PROC", True)

    def test_permission_denied_makes_the_whole_tree_unknown(self, monkeypatch):
        # hidepid=2, a container boundary, another user's process. We were refused sight of it,
        # which is not the same as it not being there.
        monkeypatch.setattr(tmuxio.os, "listdir", lambda p: ["1", "2"])

        def denied(path, *a, **kw):
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr("builtins.open", denied)
        assert tmuxio._children() is None

    def test_a_vanished_process_is_skipped_rather_than_poisoning_the_tree(self, monkeypatch):
        # ENOENT between the listdir and the open happens constantly on a busy box. Treating it
        # as unknown would make the process tree permanently unreadable.
        monkeypatch.setattr(tmuxio.os, "listdir", lambda p: ["1", "2"])
        real_open = open

        def sometimes(path, *a, **kw):
            if str(path).endswith("/1/stat"):
                raise FileNotFoundError(errno.ENOENT, "No such process")
            if str(path).endswith("/2/stat"):
                import io
                return io.StringIO("2 (bash) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", sometimes)
        assert tmuxio._children() is not None

    def test_and_a_refused_read_therefore_leaves_the_board_alone(self, home, monkeypatch):
        store.save(Presence(session_id="a", repo_root="/repo", workdir="/repo",
                            pane="%1", claude_pid=1))
        monkeypatch.setattr(tmuxio, "_children", lambda: None)
        store.sweep("/repo", tmuxio.claude_pids(lambda argv: "%1\tme:1\t1\t/repo"))
        assert len(store.load_all("/repo")) == 1
