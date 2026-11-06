"""The two modules that touch the world, driven through their injected runners.

`send` gets the most attention here. It is one session typing into another's prompt without
Daniel watching, which is the largest saving of his attention in this package and also the
thing most able to do damage -- so every refusal it makes is pinned.
"""

from __future__ import annotations

import os

import pytest
from sessioncoord import gitio, tmuxio
from sessioncoord.tmuxio import SendRefused


class FakeGit:
    """Answers git by prefix match on argv, so a test states only what it cares about."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, cwd=""):
        self.calls.append((argv, cwd))
        for prefix, value in self.answers.items():
            if " ".join(argv).startswith(prefix):
                return value
        return ""


class TestRepoRoot:
    def test_a_worktree_resolves_to_the_repo_it_hangs_off(self):
        # The load-bearing case. `--show-toplevel` would return the worktree, so four sessions
        # in four worktrees would register as four unrelated repos and nothing would ever fire.
        run = FakeGit({"git rev-parse --path-format=absolute --git-common-dir":
                       "/home/user/code/acme/infra/.git"})
        # Compared through realpath because repo_root resolves symlinks, and on macOS /home
        # is one (to /System/Volumes/Data/home), so the literal fails there while the code is
        # right.
        assert gitio.repo_root("/home/user/code/acme/infra/.worktrees/cyber",
                               run) == os.path.realpath("/home/user/code/acme/infra")

    def test_outside_a_repo_is_none_not_a_crash(self):
        assert gitio.repo_root("/tmp", FakeGit({})) is None


class TestFacts:
    def test_reports_a_local_default_behind_its_remote(self):
        run = FakeGit({
            "git rev-parse --git-dir": ".git",
            "git symbolic-ref": "origin/main",
            "git rev-parse --verify --quiet main": "aaa",
            "git rev-parse --verify --quiet origin/main": "bbb",
        })
        assert not gitio.facts("/repo", run).default_branch_in_sync

    def test_a_repo_with_no_remote_is_treated_as_in_sync(self):
        # Unknown is not a finding. Saying otherwise on every local-only repo would train him
        # to skim the board.
        run = FakeGit({"git rev-parse --verify --quiet main": "aaa"})
        assert gitio.facts("/repo", run).default_branch_in_sync

    def test_does_not_fetch(self):
        # This runs on every session start; a network round trip there would be felt.
        run = FakeGit({})
        gitio.facts("/repo", run)
        assert not any("fetch" in " ".join(argv) for argv, _ in run.calls)


class FakeTmux:
    def __init__(self, listing="", claude_panes=()):
        self.listing = listing
        self.claude = set(claude_panes)
        self.sent = []

    def __call__(self, argv):
        if argv[:2] == ["tmux", "list-panes"]:
            return self.listing
        if argv[:2] == ["tmux", "send-keys"]:
            self.sent.append(argv)
        return ""


LISTING = "\n".join([
    "%67\tinfra:11\t4140543\t/home/user/code/acme/infra",
    "%60\tinfra:8\t3399003\t/home/user/code/acme/infra",
])


@pytest.fixture
def tmux(monkeypatch):
    """A tmux with a live Claude in %67 and only a shell in %60 -- window 8 after Daniel
    closed the session but kept the window so the others would not renumber."""
    def fake_claude_under(pane_pid, kids):
        return 4141161 if int(pane_pid) == 4140543 else None

    monkeypatch.setattr(tmuxio, "claude_under", fake_claude_under)
    monkeypatch.setattr(tmuxio, "_children", lambda: {})
    return FakeTmux(LISTING)


class TestPanes:
    def test_maps_panes_to_the_claude_running_in_them(self, tmux):
        assert tmuxio.claude_pids(tmux) == {"%67": 4141161, "%60": None}

    def test_resolves_a_window_label_to_a_pane(self, tmux):
        assert tmuxio.pane_of_window("infra:11", tmux) == "%67"

    def test_no_tmux_server_is_an_empty_map_not_a_crash(self):
        assert tmuxio.claude_pids(FakeTmux("")) == {}


class TestSend:
    def test_types_the_message_then_submits_it(self, tmux):
        tmuxio.send("infra:11", "Read /tmp/window-10-reply.md", tmux)
        # `-l` is literal mode and is load-bearing: without it tmux parses key NAMES out of the
        # message. The submit is a separate, deliberately non-literal keystroke.
        assert tmux.sent[0][:6] == ["tmux", "send-keys", "-t", "%67", "-l", "--"]
        assert tmux.sent[0][-1].endswith("Read /tmp/window-10-reply.md")
        assert tmux.sent[1][-1] == "Enter"

    def test_accepts_a_pane_id_as_well_as_a_window_label(self, tmux):
        assert tmuxio.send("%67", "hello there, this is a message", tmux) == "%67"

    def test_refuses_a_pane_holding_only_a_shell(self, tmux):
        # Sending here would execute the message as a shell command.
        with pytest.raises(SendRefused, match="no Claude session"):
            tmuxio.send("infra:8", "rm -rf something", tmux)
        assert tmux.sent == []

    def test_refuses_an_unknown_target(self, tmux):
        with pytest.raises(SendRefused, match="no pane matches"):
            tmuxio.send("infra:99", "hello", tmux)
        assert tmux.sent == []

    def test_refuses_an_empty_message(self, tmux):
        # A bare Enter would submit whatever the recipient had half-typed.
        with pytest.raises(SendRefused, match="empty"):
            tmuxio.send("infra:11", "   ", tmux)
        assert tmux.sent == []
