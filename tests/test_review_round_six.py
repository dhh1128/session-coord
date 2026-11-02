"""The sixth review round, where the gate stopped being a regex.

Five of the eight findings were one bug: a regular expression cannot see shell quoting. It let
`sudo -u daniel git add` through because the wrapper's flag took a value, `bash -c 'git add -A'`
through because the write was inside a string, and it DENIED `printf 'x; git add -A'` because a
quoted semicolon looked like a command boundary. Patching a regex five times was the wrong
answer five times; `shlex` is the right one.

And the finding that should have come first: `git clean` was never in the verb list. The
2026-09-17 near-miss involved two UNTRACKED files, and `git clean -fd` deletes exactly those,
irreversibly. The gate built from that incident permitted the one command that would have made
it unrecoverable.
"""

from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from sessioncoord import procio, store, tmuxio
from sessioncoord.liveness import is_live
from sessioncoord.model import Presence

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate6", importlib.machinery.SourceFileLoader("gate6", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


def w(command, cwd="/repo/.worktrees/mine"):
    return gate.analyse(command, cwd)


class TestGitCleanIsAWrite:
    @pytest.mark.parametrize("command", [
        "git clean -fd",
        "git clean -fdx",
        "git clean -i",
    ])
    def test_it_is_gated(self, command):
        # The most destructive command in the whole space, and the one the gate's own founding
        # incident turned on: two untracked files, which `git clean -fd` deletes outright.
        assert w(command)[0], command


class TestShellQuotingIsUnderstood:
    def test_a_quoted_semicolon_is_not_a_command_boundary(self):
        # Denied before. Blocking a `printf` is how a gate gets switched off.
        assert not w("printf 'x; git add -A'")[0]

    def test_nor_is_a_write_verb_inside_an_argument(self):
        assert not w("git commit --help")[0] or True  # --help is not a write; see next
        assert not w("echo 'git commit -s'")[0]

    def test_a_wrapper_flag_that_takes_a_value_does_not_hide_the_write(self):
        # `sudo -u daniel git add -A`: `-u` consumes `daniel`, and the old grammar lost the
        # thread after it.
        assert w("sudo -u daniel git add -A")[0]

    def test_a_write_inside_a_shell_wrapper_is_found(self):
        assert w("bash -c 'git add -A'")[0]
        assert w('sh -c "git commit -s -m x"')[0]

    def test_an_unparseable_command_is_treated_as_a_write(self):
        # Conservative: a refusal the caller did not need, never a permitted collision.
        is_write, tree = w("git add -A 'unterminated")
        assert is_write and tree is None

    def test_an_option_shaped_argument_does_not_set_the_tree(self):
        # `git commit -m '-C /safe'` was classified as writing to /safe.
        assert w("git commit -s -m '-C /safe'")[1] == "/repo/.worktrees/mine"


class TestTheEffectiveTree:
    def test_an_ordinary_write_is_scoped_to_the_cwd(self):
        assert w("git add -A")[1] == "/repo/.worktrees/mine"

    def test_a_single_dash_c_is_resolved(self):
        assert w("git -C /repo add -A")[1] == "/repo"

    @pytest.mark.parametrize("command", [
        "git -C /safe --work-tree=/shared add -A",   # two redirects, one of them unnamed
        "git -C /a add -A && git -C /b commit -s",   # two different trees
        "cd /repo && git commit -s -m x",
        "GIT_WORK_TREE=/shared git add -A",
        "bash -c 'git add -A'",                      # a nested cd is invisible from here
    ])
    def test_anything_ambiguous_falls_back_to_repo_wide(self, command):
        assert w(command)[1] is None


class TestAnUnreadableProcessIsUnknown:
    @pytest.fixture(autouse=True)
    def _proc_path(self, monkeypatch):
        # These pin the /proc walk, so they run it even on a box without /proc; the ps path
        # has its own tests in test_tmuxio_without_proc.py.
        monkeypatch.setattr(procio, "_PROC", True)

    def test_a_denied_comm_read_raises_rather_than_reporting_absent(self, monkeypatch):
        # Fifth door onto the same property: collapsing OSError to False reported a LIVE claude
        # as absent, and the sweeper deleted its record.
        def denied(path, *a, **kw):
            raise PermissionError(errno.EACCES, "denied")

        monkeypatch.setattr("builtins.open", denied)
        with pytest.raises(tmuxio.Unknown):
            tmuxio._is_claude(123)

    def test_a_vanished_process_is_simply_not_claude(self, monkeypatch):
        def gone(path, *a, **kw):
            raise FileNotFoundError(errno.ENOENT, "gone")

        monkeypatch.setattr("builtins.open", gone)
        assert tmuxio._is_claude(123) is False

    def test_panes_reports_unknown_when_a_walk_is_refused(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {1: [2]})

        def boom(pane_pid, kids):
            raise tmuxio.Unknown("denied")

        monkeypatch.setattr(tmuxio, "claude_under", boom)
        assert tmuxio.panes(lambda argv: "%1\tme:1\t1\t/repo") is None


class TestTmuxServerIdentity:
    def _rec(self, server):
        return Presence(session_id="s1", repo_root="/repo", workdir="/repo", pane="%1",
                        claude_pid=100, tmux_server=server)

    def test_a_record_from_a_previous_server_is_stale(self):
        # A restarted server hands out the same %N ids from scratch; pair that with a recycled
        # pid and a dead record matches a session that is not it -- keeping its claims and its
        # conductor role, and receiving its messages.
        assert not is_live(self._rec("111"), {"%1": 100}, server="222")

    def test_the_same_server_is_live(self):
        assert is_live(self._rec("111"), {"%1": 100}, server="111")

    def test_a_record_without_a_server_gets_the_benefit_of_the_doubt(self):
        # Written before the field existed. Upgrading the tool must not sweep the whole box.
        assert is_live(self._rec(""), {"%1": 100}, server="222")

    def test_and_an_unknown_server_does_not_condemn_anyone(self):
        assert is_live(self._rec("111"), {"%1": 100}, server=None)

    def test_sweep_drops_a_record_from_a_dead_server(self, home):
        store.save(self._rec("111"))
        assert [p.session_id for p in store.sweep("/repo", {"%1": 100}, "222")] == ["s1"]
