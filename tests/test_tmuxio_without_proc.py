"""The tmux path on a box with no `/proc`, which is every Mac.

Reported 2026-11-02 by a session on sumago: `session-coord msg` filed mail and never rang the
doorbell, and a window label like `main:3` failed with "tmux could not be consulted". The
process-tree half of `tmuxio` read `/proc` directly, so on macOS `_children()` returned None
(the "could not look" answer, correctly) on every call, and every pane lookup gave up.
`procio` already had a `ps` fallback for the same facts; this module had not been routed
through it.

The property these tests hold is the one the review rounds kept reopening: "could not look"
stays None or Unknown on the `ps` path too, and never collapses into "looked and found nothing".
"""

from __future__ import annotations

import subprocess

import pytest

from sessioncoord import procio, tmuxio


@pytest.fixture
def no_proc(monkeypatch):
    monkeypatch.setattr(procio, "_PROC", False)

    def no_open(path, *a, **kw):  # any /proc read on this path is the bug
        raise AssertionError(f"read {path} on a box without /proc")

    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **kw: no_open(p) if str(p).startswith("/proc")
                        else real_open(p, *a, **kw))


def fake_ps(monkeypatch, table: dict[tuple, tuple[int, str]]):
    """Replace subprocess.run for `ps` calls: key is the argv tail after 'ps'."""
    real = subprocess.run

    def run(argv, *a, **kw):
        if argv and argv[0] == "ps":
            key = tuple(argv[1:])
            if key not in table:
                raise AssertionError(f"unexpected ps call {argv}")
            rc, out = table[key]
            return subprocess.CompletedProcess(argv, rc, out, "")
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


class TestTheTreeComesFromPs:
    def test_children_are_read_from_one_ps_listing(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-A", "-o", "pid=,ppid="): (0, "  1     0\n 10     1\n 11    10\n 12    10\n")})
        assert tmuxio._children() == {0: [1], 1: [10], 10: [11, 12]}

    def test_a_failed_ps_is_could_not_look_not_an_empty_tree(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-A", "-o", "pid=,ppid="): (1, "")})
        assert tmuxio._children() is None

    def test_an_unparseable_listing_is_could_not_look(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-A", "-o", "pid=,ppid="): (0, "garbage here\n")})
        assert tmuxio._children() is None

    def test_no_ps_at_all_is_could_not_look(self, no_proc, monkeypatch):
        def missing(argv, *a, **kw):
            raise FileNotFoundError("ps")

        monkeypatch.setattr(subprocess, "run", missing)
        assert tmuxio._children() is None


class TestIsClaudeWithoutProc:
    def test_a_native_claude_is_recognised_by_its_comm(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (0, "claude\n")})
        assert tmuxio._is_claude(40) is True

    def test_a_full_path_comm_is_recognised(self, no_proc, monkeypatch):
        # BSD ps prints the executable path in comm for some processes.
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (0, "/Users/x/.local/bin/claude\n")})
        assert tmuxio._is_claude(40) is True

    def test_a_shell_is_not_claude(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (0, "-zsh\n"),
                              ("-o", "args=", "-p", "40"): (0, "-zsh\n")})
        assert tmuxio._is_claude(40) is False

    def test_a_vanished_process_is_simply_not_claude(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (1, "")})
        monkeypatch.setattr(tmuxio.os, "kill", _raises(ProcessLookupError()))
        assert tmuxio._is_claude(40) is False

    def test_a_live_process_ps_will_not_describe_is_unknown(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (1, "")})
        monkeypatch.setattr(tmuxio.os, "kill", lambda pid, sig: None)
        with pytest.raises(tmuxio.Unknown):
            tmuxio._is_claude(40)

    def test_another_users_live_process_ps_will_not_describe_is_unknown(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (1, "")})
        monkeypatch.setattr(tmuxio.os, "kill", _raises(PermissionError()))
        with pytest.raises(tmuxio.Unknown):
            tmuxio._is_claude(40)


    @pytest.mark.parametrize("args", [
        "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        "node /Users/x/.npm-global/bin/claude --resume",
        "bun /Users/x/.bun/bin/claude",
    ])
    def test_an_npm_or_bun_claude_is_recognised(self, no_proc, monkeypatch, args):
        # Hostile pass on PR #6: these ran as "node"/"bun", so a comm-and-argv[0] check read a
        # live session as absent, where before this branch the whole tree was unknown and the
        # record was kept. procio.is_claude already knew these shapes.
        fake_ps(monkeypatch, {("-o", "comm=", "-p", "40"): (0, args.split()[0] + "\n"),
                              ("-o", "args=", "-p", "40"): (0, args + "\n")})
        assert tmuxio._is_claude(40) is True


class TestPidExitedWithoutProc:
    def test_a_missing_pid_has_exited(self, no_proc, monkeypatch):
        monkeypatch.setattr(tmuxio.os, "kill", _raises(ProcessLookupError()))
        assert tmuxio._pid_exited(40) is True

    def test_a_pid_we_may_not_signal_is_still_there(self, no_proc, monkeypatch):
        monkeypatch.setattr(tmuxio.os, "kill", _raises(PermissionError()))
        assert tmuxio._pid_exited(40) is False

    def test_a_live_pid_is_still_there(self, no_proc, monkeypatch):
        monkeypatch.setattr(tmuxio.os, "kill", lambda pid, sig: None)
        assert tmuxio._pid_exited(40) is False


class TestAPaneResolvesEndToEnd:
    def test_the_claude_under_a_pane_is_found_without_proc(self, no_proc, monkeypatch):
        fake_ps(monkeypatch, {
            ("-A", "-o", "pid=,ppid="): (0, "100 1\n101 100\n"),
            ("-o", "comm=", "-p", "101"): (0, "claude\n"),
        })
        out = "%3\tmain:3\t100\t/repo\ttitle"
        result = tmuxio.panes(lambda argv: out)
        assert result is not None
        assert result["%3"]["claude_pid"] == 101


@pytest.mark.skipif(procio._PROC, reason="the real-ps check only means something without /proc")
def test_this_process_is_in_the_real_tree():
    import os
    kids = tmuxio._children()
    assert kids is not None
    assert os.getpid() in kids.get(os.getppid(), [])


def _raises(exc):
    def f(*a, **kw):
        raise exc
    return f
