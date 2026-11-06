"""Codex's shared app-server daemon: its environment is not the caller's.

Observed on kelun, 2026-11-06, Codex CLI 0.163 with daemon 0.167. A TUI started without
`--no-daemon` (and without any `-c` override) runs its tools and hooks inside a per-user
`codex app-server` daemon. The daemon is a child of the TUI that started it and kept that TUI's
environment; later TUIs attach to its socket. A tool command from a second TUI in scratch pane
%2 saw ancestry ending at the first TUI, `TMUX_PANE=%183` and that TUI's TMUX socket. Only
`CODEX_SESSION_ID` / `CODEX_THREAD_ID` were its own.
"""

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from sessioncoord import cli, procio, store
from sessioncoord.model import Presence, RepoFacts

ROOT = Path(__file__).resolve().parents[1]
# Captured at import, before the suite's autouse fixture replaces it with a neutral stub.
_real_nearest = procio.nearest_engine
DAEMON = ["/home/u/.codex/packages/app-server-daemon/releases/0.167.0/bin/codex",
          "app-server", "--listen", "unix://", "--managed-daemon"]


@pytest.mark.parametrize("argv,want", [
    (DAEMON, True),
    (["/opt/bin/codex", "app-server", "daemon", "pid-update-loop"], True),
    (["node", "/opt/node_modules/@openai/codex/bin/codex.js", "app-server"], True),
    (["/opt/bin/codex", "exec-server"], True),
    (["/opt/bin/codex", "exec", "one-shot"], False),
    (["/opt/bin/codex", "--dangerously-bypass-approvals-and-sandbox", "reply OK"], False),
    (["/opt/bin/codex", "-C", "app-server"], False),
    (["claude"], False),
])
def test_codex_server_identity(argv, want):
    assert procio.is_codex_server(argv) is want


def _tree(monkeypatch, chain):
    """chain: argv lists from the caller's parent upward."""
    pids = list(range(100, 100 + len(chain)))
    parents = {pid: nxt for pid, nxt in zip([99] + pids, pids)}
    monkeypatch.setattr(procio, "parent_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(procio, "argv_of", lambda pid: dict(zip(pids, chain)).get(pid))
    return pids


def test_nearest_engine_names_the_daemon(monkeypatch):
    # Undo the suite's neutral default: this test is about the real walk.
    monkeypatch.setattr(procio, "nearest_engine", _real_nearest)
    pids = _tree(monkeypatch, [["bash", "-lc", "x"], DAEMON, ["/opt/bin/codex", "go"]])
    assert procio.nearest_engine(99) == ("codex-server", pids[1])


def test_nearest_engine_prefers_a_claude_started_by_a_codex_tool(monkeypatch):
    monkeypatch.setattr(procio, "nearest_engine", _real_nearest)
    pids = _tree(monkeypatch, [["bash"], ["claude"], ["bash"], DAEMON])
    assert procio.nearest_engine(99) == ("claude", pids[1])


def test_session_start_hook_under_daemon_explains_and_does_not_register(monkeypatch, tmp_path,
                                                                        capsys):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1352327)
    monkeypatch.setattr(procio, "argv_of", lambda pid: DAEMON)
    monkeypatch.setenv("TMUX_PANE", "%183")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-2", "cwd": str(tmp_path), "source": "startup"})))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("registered under the shared daemon"))
    assert module["main"]() == 0
    out = capsys.readouterr().out
    assert "NOT on the board" in out and "--no-daemon" in out


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/w")
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: "/w")
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: None)
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: RepoFacts(repo_root=r, is_git=False))
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: None)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: None)
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda pane, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "panes", lambda run=None: None)
    monkeypatch.setattr(cli.procio, "claude_ancestor", lambda pid=None: None)
    monkeypatch.setattr(cli.procio, "start_of", lambda pid: f"t{pid}")
    monkeypatch.setattr(cli.procio, "starts", lambda pids: {p: f"t{p}" for p in pids})
    return tmp_path


def _under_daemon(monkeypatch):
    monkeypatch.setattr(procio, "nearest_engine", lambda pid=None: ("codex-server", 1352327))
    monkeypatch.setattr(cli.procio, "codex_ancestor", lambda pid=None: 1352327)
    monkeypatch.setenv("TMUX_PANE", "%183")


def test_register_under_daemon_without_pid_refuses(world, monkeypatch, capsys):
    _under_daemon(monkeypatch)
    monkeypatch.setenv("CODEX_SESSION_ID", "thread-2")
    assert cli.main(["register", "--name", "codex-p2v1"]) == 2
    assert store.load_all("/w") == []
    err = capsys.readouterr().err
    assert "shared" in err and "--no-daemon" in err


def test_me_under_daemon_ignores_the_daemons_pane(world, monkeypatch):
    # The first TUI is registered in %183. A tool from the SECOND TUI inherits TMUX_PANE=%183
    # from the daemon; it must resolve to its own record by thread id, not to the first one.
    store.save(Presence(session_id="thread-1", repo_root="/w", workdir="/w", pane="%183",
                        claude_pid=1341719, engine="codex", title="first"))
    store.save(Presence(session_id="thread-2", repo_root="/w", workdir="/w", pane="%185",
                        claude_pid=1444495, engine="codex", title="second"))
    _under_daemon(monkeypatch)
    monkeypatch.setenv("CODEX_SESSION_ID", "thread-2")
    assert cli._me("/w").session_id == "thread-2"
    monkeypatch.delenv("CODEX_SESSION_ID")
    assert cli._me("/w") is None


def test_register_refuses_a_pid_the_pane_scan_contradicts(world, monkeypatch, capsys):
    # 16:34:53: `--pid 1341681` named the npm launcher; the pane scan finds the native TUI.
    monkeypatch.setattr(cli.tmuxio, "panes", lambda run=None: {
        "%183": {"pane": "%183", "window": "tessera:7", "path": "/w", "title": "t",
                 "claude_pid": 1341719, "engine": "codex"}})
    assert cli.main(["register", "--session", "thread-1", "--engine", "codex",
                     "--pid", "1341681", "--pane", "%183"]) == 2
    assert store.load_all("/w") == []
    assert "--pid 1341719" in capsys.readouterr().err
    assert cli.main(["register", "--session", "thread-1", "--engine", "codex",
                     "--pid", "1341719", "--pane", "%183"]) == 0
    (p,) = store.load_all("/w")
    assert p.claude_pid == 1341719 and p.pane == "%183"

