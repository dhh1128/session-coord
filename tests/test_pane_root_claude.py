"""A pane whose ROOT process is claude holds a Claude session.

`tmux new-window -n <name> claude "<prompt>"` starts claude as the pane's own process, with
no shell above it. zamu launches workers that way, and so does any hand-written launcher.
`claude_under` began its walk at the pane pid's children, so it never looked at the pane pid
itself and reported such a pane as empty. Everything downstream then believed it:

- the sweep run by every registration in the repo deleted the session's presence record,
  so the session vanished from the board seconds after registering successfully;
- held mail is delivered only to a LIVE record whose name matches, so it was never delivered;
- `msg %N` refused the pane as "a plain shell" while tmux reported claude running in it.

Observed 2026-11-05 on two keripy sessions (panes %162 and %180, both root=claude).
"""

from __future__ import annotations

from sessioncoord import liveness, store, tmuxio
from sessioncoord.model import Presence

from test_tmuxio_without_proc import fake_ps, no_proc  # noqa: F401  (fixture)


def test_claude_as_the_pane_root_is_found(monkeypatch):
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 100)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: ["claude", "do the brief"])
    assert tmuxio.claude_under(100, {}) == 100


def test_a_shell_root_still_finds_its_claude_child(monkeypatch):
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 101)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {100: ["bash"]}.get(pid))
    assert tmuxio.claude_under(100, {100: [101]}) == 101


def test_a_root_claude_does_not_report_its_subagent(monkeypatch):
    # Do not descend past the session: a claude it started is not the pane's session.
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid in (100, 101))
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: ["claude"])
    assert tmuxio.claude_under(100, {100: [101]}) == 100


def test_a_codex_root_with_a_claude_tool_child_is_still_not_claude(monkeypatch):
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 30)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        10: ["/opt/bin/codex"], 30: ["claude", "-p", "one-shot"]}.get(pid))
    assert tmuxio.claude_under(10, {10: [30]}) is None


def test_panes_reports_a_root_claude_without_proc(no_proc, monkeypatch):  # noqa: F811
    fake_ps(monkeypatch, {
        ("-A", "-o", "pid=,ppid="): (0, "100 1\n"),
        ("-o", "comm=", "-p", "100"): (0, "claude\n"),
        ("-o", "args=", "-p", "100"): (0, "claude Read /tmp/brief.md\n"),
    })
    out = "%180\tbakobo:11\t100\t/repo/.worktrees/tls-pin\tKeripy TLS pin"
    table = tmuxio.panes(lambda argv: out)
    assert table is not None
    assert table["%180"]["claude_pid"] == 100
    assert table["%180"]["engine"] == "claude"


def test_sweep_keeps_a_root_claude_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    assert str(store.home()).startswith(str(tmp_path))  # never the real state directory
    repo = str(tmp_path / "repo")
    rec = Presence(session_id="d0e290a3-2b13-4cb6-9a0b-b8b04c6ecf58", repo_root=repo,
                   workdir=repo, branch="main", pane="%162", window="bakobo:9",
                   claude_pid=1880241, title="pr1685-attribs", tmux_server="3203")
    store.save(rec)
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 1880241)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: ["claude", "Read /tmp/brief"])
    pane_map = {"%162": tmuxio.claude_under(1880241, {})}
    assert liveness.is_live(rec, pane_map, "3203")
    assert store.sweep(repo, pane_map, "3203") == []
    assert [p.session_id for p in store.load_all(repo)] == [rec.session_id]


def test_a_one_shot_claude_pane_is_not_a_session(monkeypatch):
    # Hostile pass p1.1: `tmux new-window claude -p "..."` runs a one-shot with no prompt to
    # receive a message; reading it as a session would let `send` type into it and succeed.
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 100)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: ["claude", "-p", "one-shot"])
    monkeypatch.setattr(tmuxio.procio, "argv_of", lambda pid: ["claude", "-p", "one-shot"])
    assert tmuxio.claude_under(100, {}) is None


def test_a_one_shot_claude_under_a_shell_is_not_a_session(monkeypatch):
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 101)
    argv = {100: ["bash"], 101: ["claude", "--print", "x"]}
    monkeypatch.setattr(tmuxio, "_argv_of", argv.get)
    monkeypatch.setattr(tmuxio.procio, "argv_of", argv.get)
    assert tmuxio.claude_under(100, {100: [101]}) is None


def test_a_prompt_mentioning_dash_p_is_still_a_session(monkeypatch):
    # Without /proc, ps splits argv on spaces; a prompt word "-p" must not read as one-shot.
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 100)
    argv = ["claude", "retry", "with", "-p", "please"]
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: argv)
    monkeypatch.setattr(tmuxio.procio, "argv_of", lambda pid: argv)
    assert tmuxio.claude_under(100, {}) == 100


import pytest


@pytest.mark.parametrize("argv,one_shot", [
    (["claude", "-p", "x"], True),
    (["claude", "--print", "x"], True),
    (["claude", "--print=json"], True),
    (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-p", "x"], True),
    # Last-look p3.2 (~5zve): an interactive session whose prompt or flag value is "-p".
    (["claude", "--", "-p", "review", "the", "diff"], False),
    (["claude", "--append-system-prompt", "-p", "be", "careful"], False),
    (["claude", "Read /tmp/brief.md"], False),
    (["claude"], False),
])
def test_one_shot_is_judged_by_the_first_argument_after_the_program(monkeypatch, argv, one_shot):
    monkeypatch.setattr(tmuxio.procio, "argv_of", lambda pid: argv)
    assert tmuxio._one_shot(1) is one_shot
