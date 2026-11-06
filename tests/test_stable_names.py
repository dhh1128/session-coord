"""Sessions are named by title, and their windows are read live. 2026-10-10.

Daniel keeps `renumber-windows on`, so closing one window renumbers every window after it. A
window stored at registration then names whoever holds that number now. That day a session in
infra:6 introduced itself as infra:7, and another asked after a reply "from infra:7" while
it was itself running in infra:7.
"""

from __future__ import annotations

import pytest
from sessioncoord import cli, store, tmuxio
from sessioncoord.model import Presence

REPO = "/repo"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    # Registered when they were infra:6 and infra:7; infra:5 has since closed.
    for sid, pane, pid, win in [("aaaa1111", "%1", 1, "infra:6"), ("bbbb2222", "%2", 2, "infra:7")]:
        store.save(Presence(session_id=sid, repo_root=REPO, workdir=REPO, pane=pane,
                            claude_pid=pid, window=win, tmux_server="999"))
    table = {
        "%1": {"pane": "%1", "window": "infra:5", "claude_pid": 1, "title": "conf-suite-open"},
        "%2": {"pane": "%2", "window": "infra:6", "claude_pid": 2, "title": "more-adapters"},
    }
    monkeypatch.setattr(cli, "_live_table", lambda: table)
    monkeypatch.setenv("TMUX_PANE", "%1")
    return table


def test_a_label_carries_the_name_and_the_window_it_is_in_now(board):
    live = {p.session_id: p for p in cli._live(store.load_all(REPO))}
    assert live["aaaa1111"].label == "conf-suite-open (now infra:5)"
    assert live["bbbb2222"].name == "more-adapters"


def test_a_pane_holding_a_different_claude_is_not_relabelled(board):
    board["%2"]["claude_pid"] = 77
    live = {p.session_id: p for p in cli._live(store.load_all(REPO))}
    assert live["bbbb2222"].window is None, "that pane is somebody else now"


def test_a_stale_record_is_not_reached_through_its_old_window(board, monkeypatch, capsys):
    # PR #35, finding 2: a record whose pane holds another claude kept its stored window, so
    # `msg infra:7` filed into the dead session's inbox. Nobody live is infra:7 now.
    board["%2"]["claude_pid"] = 77
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "infra:7", "hello"])
    assert store.load_inbox(REPO) == []


def test_msg_by_name_reaches_the_titled_session(board, monkeypatch, capsys):
    rang = {}
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: rang.update(t=t) or t)
    cli.main(["msg", "--repo", REPO, "more-adapters", "hello"])
    assert rang["t"] == "%2"
    assert "more-adapters (now infra:6)" in capsys.readouterr().out


def test_a_stale_window_label_reaches_whoever_is_there_now_and_says_so(board, monkeypatch,
                                                                      capsys):
    # "infra:6" was conf-suite-open's number when it registered and is more-adapters' now. The
    # result line names who was reached, so a sender holding the old number sees the mismatch.
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "infra:6", "hello"])
    assert "more-adapters" in capsys.readouterr().out


def test_the_inbox_says_who_you_are_and_how_to_reply(board, monkeypatch, capsys):
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "more-adapters", "the body"])
    monkeypatch.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    cli.main(["inbox", "--repo", REPO])
    out = capsys.readouterr().out
    assert "You are more-adapters, now in window infra:6" in out
    # Shell-quoted, so a name needing no quotes gets none; double quotes would let a name
    # carrying `$(...)` run when the hint is pasted (hostile pass on PR #12).
    assert "session-coord msg conf-suite-open " in out


def test_titles_drop_claude_codes_status_glyph():
    assert tmuxio.clean_title("\u2733 fiki-release") == "fiki-release"
    assert tmuxio.clean_title("\u2810 Merge-pr skill") == "Merge-pr skill"


def test_a_stale_record_is_not_reached_through_its_old_title(board, monkeypatch):
    # PR #35 fix-diff pass, finding 2.
    board["%2"]["claude_pid"] = 77
    store.save(store.load_all(REPO)[1].with_(title="more-adapters"))
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "more-adapters", "hello"])
    assert store.load_inbox(REPO) == []


def test_an_empty_pane_table_makes_every_record_stale(board, monkeypatch):
    # Finding 3: tmux answered and there are no panes, which is not "could not ask".
    monkeypatch.setattr(cli, "_live_table", lambda: {})
    assert all(p.window is None for p in cli._live(store.load_all(REPO)))


def test_a_stale_record_is_not_reached_through_its_old_pane(board, monkeypatch):
    # PR #35, third pass, finding 3.
    board["%2"]["claude_pid"] = 77
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "%2", "hello"])
    assert store.load_inbox(REPO) == []
