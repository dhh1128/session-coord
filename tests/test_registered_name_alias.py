"""A name given with `register --name` keeps reaching the session after its pane title moves.

2026-11-06: pr1685-attribs and keripy-tls-pin registered by name, and held mail was filed under
those names. Under tmux the board reads each session's name from its LIVE pane title, which
Claude Code rewrites as the conversation goes ("PR1685-attribs brief", "Keripy TLS pin"), so
neither the held mail nor `msg pr1685-attribs` could ever find them again.
"""

from __future__ import annotations

import pytest

from sessioncoord import cli, store
from sessioncoord.model import Presence

REPO = "/repo"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "workspace_root", lambda p: p)
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    store.save(Presence(session_id="d0e290a3-0000", repo_root=REPO, workdir=REPO, pane="%2",
                        claude_pid=2, title="pr1685-attribs", tmux_server="999", named=True))
    table = {"%2": {"pane": "%2", "window": "bakobo:9", "claude_pid": 2,
                    "title": "PR1685-attribs brief"}}
    monkeypatch.setattr(cli, "_live_table", lambda: table)
    monkeypatch.setattr(cli._Doorbell, "reserve", lambda self: "budget exhausted")
    return monkeypatch


def test_held_mail_for_the_registered_name_is_delivered(world, capsys):
    held = store.load_held()
    from sessioncoord.inbox import Message
    store.save_held(list(held) + [Message(id="held-1", sender="keripy-tls-pin",
                                          recipient="pr1685-attribs", body="DeclRecord note",
                                          created="2026-11-05T22:34:57+00:00")])
    assert cli.main(["inbox", "--repo", REPO, "--session", "d0e290a3-0000"]) == 0
    assert "DeclRecord note" in capsys.readouterr().out
    assert store.load_held() == []


def test_msg_by_registered_name_reaches_the_live_session(world):
    rang = {}
    world.setattr(cli.tmuxio, "send", lambda t, x, **kw: rang.update(t=t) or t)
    assert cli.main(["msg", "--repo", REPO, "pr1685-attribs", "hello"]) == 0
    assert [m.body for m in store.load_inbox(REPO)] == ["hello"]
    assert store.load_held() == []


def test_the_live_title_still_works(world):
    world.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    assert cli.main(["msg", "--repo", REPO, "PR1685-attribs brief", "hi"]) == 0
    assert [m.body for m in store.load_inbox(REPO)] == ["hi"]


def test_a_dead_session_does_not_keep_its_registered_name(world):
    # The pane now holds another claude: neither the old title nor its name reaches it.
    cli._live_table  # noqa: B018
    world.setattr(cli, "_live_table", lambda: {"%2": {"pane": "%2", "window": "bakobo:9",
                                                      "claude_pid": 77, "title": "someone"}})
    world.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "pr1685-attribs", "hello"])
    assert store.load_inbox(REPO) == []


def test_a_live_title_elsewhere_outranks_a_local_registered_name(world):
    # Hostile p1.1: local session registered "alpha", now titled "beta"; a peer in another repo
    # is titled "alpha" right now. `msg alpha` must reach the peer.
    store.save(Presence(session_id="local-1", repo_root=REPO, workdir=REPO, pane="%3",
                        claude_pid=3, title="alpha", tmux_server="999", named=True))
    store.save(Presence(session_id="remote-1", repo_root="/other", workdir="/other", pane="%4",
                        claude_pid=4, title="alpha", tmux_server="999"))
    table = {"%2": {"pane": "%2", "window": "bakobo:9", "claude_pid": 2, "title": "x"},
             "%3": {"pane": "%3", "window": "w:3", "claude_pid": 3, "title": "beta"},
             "%4": {"pane": "%4", "window": "w:4", "claude_pid": 4, "title": "alpha"}}
    world.setattr(cli, "_live_table", lambda: table)
    world.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    assert cli.main(["msg", "--repo", REPO, "alpha", "for remote"]) == 0
    assert store.load_inbox(REPO) == []
    assert [m.recipient for m in store.load_inbox("/other")] == ["remote-1"]


def test_a_registered_name_is_never_read_from_a_file():
    # Hostile p1.2: a stored alias would survive the stale-record scrub.
    p = Presence.from_dict({"session_id": "s", "repo_root": "/r", "workdir": "/r",
                            "registered_name": "victim"})
    assert p.registered_name == ""
    assert "registered_name" not in p.to_json()


def test_re_registering_without_a_name_keeps_the_registered_one(world, monkeypatch):
    # Hostile p1.3: a resume or charter update re-registers without --name.
    monkeypatch.setenv("TMUX_PANE", "%2")
    monkeypatch.setattr(cli, "_title_of", lambda pane: "PR1685-attribs brief")
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%2": 2})
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda pane, run=None: "bakobo:9")
    assert cli.main(["register", "--repo", REPO, "--session", "d0e290a3-0000", "--pid", "2",
                     "--charter", "new charter"]) == 0
    rec = [p for p in store.load_all(REPO) if p.session_id == "d0e290a3-0000"][0]
    assert rec.title == "pr1685-attribs"


def test_an_automatic_title_is_not_kept_or_aliased(world, monkeypatch):
    # Fix pass finding 1: a title read from the pane, never given with --name.
    store.save(Presence(session_id="auto-1", repo_root=REPO, workdir=REPO, pane="%5",
                        claude_pid=5, title="claude", tmux_server="999"))
    table = {"%2": {"pane": "%2", "window": "bakobo:9", "claude_pid": 2, "title": "x"},
             "%5": {"pane": "%5", "window": "w:5", "claude_pid": 5, "title": "new task"}}
    monkeypatch.setattr(cli, "_live_table", lambda: table)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%2": 2, "%5": 5})
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda pane, run=None: "w:5")
    monkeypatch.setattr(cli, "_title_of", lambda pane: "new task")
    monkeypatch.setenv("TMUX_PANE", "%5")
    assert cli.main(["register", "--repo", REPO, "--session", "auto-1", "--pid", "5"]) == 0
    rec = [p for p in store.load_all(REPO) if p.session_id == "auto-1"][0]
    assert rec.title == "new task" and not rec.named
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "claude", "hello"])
    assert [m.recipient for m in store.load_inbox(REPO)] == []


def test_tmux_reads_do_not_grow_with_the_estate(world, monkeypatch):
    # Fix pass finding 2: a miss scanned every repo twice, reading tmux for each.
    table = {"%2": {"pane": "%2", "window": "bakobo:9", "claude_pid": 2, "title": "x"}}
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)

    def reads():
        calls = []
        monkeypatch.setattr(cli, "_live_table", lambda: calls.append(1) or table)
        cli.main(["msg", "--repo", REPO, "nobody-at-all", "hello"])
        return len(calls)

    few = reads()
    for i in range(20):
        store.save(Presence(session_id=f"x{i}", repo_root=f"/r{i}", workdir=f"/r{i}"))
    assert reads() == few


def test_a_failed_tmux_read_is_not_repeated_per_repo(world, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_live_table", lambda: calls.append(1) or None)
    monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: t)
    cli.main(["msg", "--repo", REPO, "nobody-at-all", "hello"])
    few = len(calls)
    for i in range(20):
        store.save(Presence(session_id=f"y{i}", repo_root=f"/q{i}", workdir=f"/q{i}"))
    calls.clear()
    cli.main(["msg", "--repo", REPO, "nobody-at-all", "hello"])
    assert len(calls) == few
