"""Sessions outside tmux, in folders that are not git repos -- the laptop case.

Before these changes a session with no tmux pane could not register (the hook returned early),
and had it registered, liveness would have judged it dead on the first sweep. So on a MacBook
the whole package silently did nothing. These tests pin the replacement: a pane-less session is
identified by its claude pid and that pid's start time, and a plain folder is a workspace.
"""

from __future__ import annotations

import os

import pytest

from sessioncoord import cli, detect, gitio, liveness, procio, store
from sessioncoord.model import Board, Presence, RepoFacts


def rec(sid, *, pane=None, pid=None, started="", workdir="/w", repo="/w", title=""):
    return Presence(session_id=sid, repo_root=repo, workdir=workdir, pane=pane,
                    claude_pid=pid, pid_started=started, title=title)


class TestPidLiveness:
    def test_a_paneless_session_is_live_while_its_pid_keeps_its_start_time(self):
        p = rec("a", pid=10, started="t0")
        assert liveness.is_live(p, None, None, {10: "t0"})

    def test_a_recycled_pid_is_not_the_same_session(self):
        p = rec("a", pid=10, started="t0")
        assert not liveness.is_live(p, None, None, {10: "t1"})

    def test_a_gone_pid_is_dead(self):
        assert not liveness.is_live(rec("a", pid=10, started="t0"), None, None, {10: None})

    def test_an_unreadable_process_table_keeps_everyone(self):
        # The same fail-open rule as the tmux path: not being able to look is not absence.
        assert liveness.is_live(rec("a", pid=10, started="t0"), None, None, None)

    def test_tmux_being_unavailable_no_longer_shields_paneless_records(self):
        # The old early return on a None pane map meant a box with no tmux never swept.
        live, dead = liveness.partition([rec("a", pid=10, started="t0")], None, None, {})
        assert not live and len(dead) == 1

    def test_a_record_with_neither_proof_cannot_be_placed(self):
        assert not liveness.is_live(rec("a", pid=10), None, None, {10: "t0"})

    def test_pane_records_are_still_judged_by_tmux(self):
        p = rec("a", pane="%1", pid=10, started="t0")
        assert liveness.is_live(p, {"%1": 10}, None, {})
        assert not liveness.is_live(p, {"%1": 99}, None, {10: "t0"})


class TestSweep:
    def test_sweep_drops_a_dead_paneless_session_and_keeps_a_live_one(self, tmp_path,
                                                                       monkeypatch):
        monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path))
        store.save(rec("alive", pid=10, started="t0"))
        store.save(rec("gone", pid=11, started="t0"))
        dropped = store.sweep("/w", None, None, {10: "t0", 11: None})
        assert [p.session_id for p in dropped] == ["gone"]
        assert [p.session_id for p in store.load_all("/w")] == ["alive"]


class TestProcio:
    def test_this_process_has_a_start_fingerprint(self):
        assert procio.start_of(os.getpid())

    def test_ps_fallback_agrees_that_this_process_exists(self, monkeypatch):
        monkeypatch.setattr(procio, "_PROC", False)
        assert procio.start_of(os.getpid()).startswith("ps:")
        assert procio.parent_of(os.getpid()) == os.getppid()

    @pytest.mark.parametrize("argv,want", [
        (["claude", "--resume"], True),
        (["/usr/local/bin/claude"], True),
        (["node", "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js"], True),
        (["node", "/usr/local/bin/claude"], True),
        (["node", "server.js"], False),
        # Hostile fix pass on PR #6: a substring match took these for Claude, so send() typed
        # into a dashboard's pane and a wrapper above a real Claude hid it from the walk.
        (["node", "/work/claude-code-dashboard/server.js"], False),
        (["bun", "/work/my-claude-code-tools/index.ts"], False),
        (["node", "/Users/x/.npm-global/lib/node_modules/@anthropic-ai/claude-code/cli.mjs"], True),
        (["bash"], False),
        ([], False),
    ])
    def test_recognises_native_and_npm_installs(self, argv, want):
        assert procio.is_claude(argv) is want


class TestWorkspaceRoot:
    def test_a_marked_ancestor_groups_subfolders(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitio, "repo_root", lambda p, run=None: None)
        (tmp_path / "Acme").mkdir()
        (tmp_path / "Acme" / "CLAUDE.md").write_text("x")
        (tmp_path / "Acme" / "legal").mkdir()
        got = gitio.workspace_root(str(tmp_path / "Acme" / "legal"))
        assert got == os.path.realpath(tmp_path / "Acme")

    def test_an_unmarked_folder_is_its_own_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitio, "repo_root", lambda p, run=None: None)
        (tmp_path / "loose").mkdir()
        assert gitio.workspace_root(str(tmp_path / "loose")) == os.path.realpath(
            tmp_path / "loose")

    def test_home_is_never_the_workspace(self, tmp_path, monkeypatch):
        # ~/.claude exists on every machine that runs Claude Code. Treating it as a marker
        # would make every folder under $HOME one workspace.
        monkeypatch.setattr(gitio, "repo_root", lambda p, run=None: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        (tmp_path / "notes").mkdir()
        assert gitio.workspace_root(str(tmp_path / "notes")) == os.path.realpath(
            tmp_path / "notes")

    def test_git_wins_when_there_is_a_repo(self, monkeypatch):
        monkeypatch.setattr(gitio, "repo_root", lambda p, run=None: "/repo")
        assert gitio.workspace_root("/repo/sub") == "/repo"


class TestPlainFolderFindings:
    def board(self, n=2):
        sessions = [rec(f"s{i}", pid=i, started="t", workdir="/w") for i in range(n)]
        return Board.of("/w", sessions, facts=RepoFacts(repo_root="/w", is_git=False))

    def test_two_sessions_in_one_folder_warn_but_do_not_block(self):
        f = [x for x in detect.check(self.board()) if x.code == "shared-tree"]
        assert f and f[0].severity == "warn"
        assert "worktree" not in f[0].remedy and "--claim" in f[0].remedy

    def test_no_git_remedies_in_a_plain_folder(self):
        codes = {x.code for x in detect.check(self.board())}
        assert "unworktreed" not in codes and "stale-default" not in codes


class TestIdentityWithoutTmux:
    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/w")
        monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: "/w")
        monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: None)
        monkeypatch.setattr(cli.gitio, "facts",
                            lambda r, run=None: RepoFacts(repo_root=r, is_git=False))
        monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: None)
        monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: None)
        monkeypatch.setattr(cli.tmuxio, "window_of", lambda pane, run=None: None)
        monkeypatch.setattr(cli.procio, "claude_ancestor", lambda pid=None: 4242)
        monkeypatch.setattr(cli.procio, "start_of", lambda pid: "t0" if pid == 4242 else None)
        monkeypatch.setattr(cli.procio, "starts",
                            lambda pids: {p: ("t0" if p == 4242 else None) for p in pids})
        return tmp_path

    def test_register_without_a_pane_records_pid_and_start(self, world):
        assert cli.main(["register", "--session", "abc", "--pid", "4242",
                         "--name", "market-sizing"]) == 0
        (p,) = store.load_all("/w")
        assert p.pane is None and p.claude_pid == 4242 and p.pid_started == "t0"
        assert p.name == "market-sizing"

    def test_session_defaults_to_this_process_once_registered(self, world):
        cli.main(["register", "--session", "abc", "--pid", "4242"])
        assert cli.main(["register", "--charter", "sizing the TAM"]) == 0
        (p,) = store.load_all("/w")
        assert p.session_id == "abc" and p.charter == "sizing the TAM"

    def test_codex_manual_register_without_engine_uses_codex_process(self, world, monkeypatch):
        monkeypatch.setattr(cli.procio, "claude_ancestor", lambda pid=None: None)
        monkeypatch.setattr(cli.procio, "codex_ancestor", lambda pid=None: 4242)
        monkeypatch.setenv("CODEX_SESSION_ID", "codex-thread")
        assert cli.main(["register", "--name", "worker"]) == 0
        (p,) = store.load_all("/w")
        assert p.session_id == "codex-thread"
        assert p.engine == "codex" and p.claude_pid == 4242 and p.name == "worker"

    def test_a_message_to_a_paneless_peer_is_filed_without_a_doorbell(self, world, capsys,
                                                                      monkeypatch):
        cli.main(["register", "--session", "abc", "--pid", "4242", "--name", "me"])
        store.save(rec("peer", pid=777, started="t9", title="legal-memo"))
        monkeypatch.setattr(cli.procio, "starts", lambda pids: {p: (
            "t0" if p == 4242 else "t9" if p == 777 else None) for p in pids})

        def no_tmux(*a, **k):
            raise AssertionError("tmux must not be touched for a paneless recipient")
        monkeypatch.setattr(cli.tmuxio, "send", no_tmux)
        assert cli.main(["msg", "legal-memo", "the TAM figure moved to 4.1B"]) == 0
        assert "next turn" in capsys.readouterr().out
        assert [m.recipient for m in store.load_inbox("/w")] == ["peer"]
