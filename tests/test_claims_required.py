"""A session commits only what it has claimed, in a repo that shares its checkout on purpose.

2026-11-05, Daniel's call (R-AS3C). Explicit paths stop `git add -A` from sweeping a stranger's
files, but not two sessions editing the same file: an explicit commit of it carries both sets of
edits. So in a marked repo the gate also asks whether the committing session claimed each file,
and the `claim` command lets a session add one without restating its whole list.
"""

from __future__ import annotations

import pytest

from sessioncoord import cli, detect, store
from sessioncoord.model import Board, Presence

TREE = "/repo"


def rec(sid="me", claims=(), charter="", workdir=TREE, pane="%1"):
    return Presence(session_id=sid, repo_root=TREE, workdir=workdir, pane=pane,
                    claims=tuple(claims), charter=charter, claude_pid=101,
                    tmux_server="999")


def codes(findings):
    return [f.code for f in findings]


class TestUnclaimedPaths:
    def test_claimed_paths_produce_nothing(self):
        board = Board.of(TREE, [rec(claims=("a.md", "docs/**"))])
        assert detect.unclaimed_paths(board, TREE, "me", ["a.md", "docs/x/y.md"]) == []

    def test_one_finding_lists_every_unclaimed_path_with_a_claim_line(self):
        board = Board.of(TREE, [rec(claims=("a.md",), charter="drafting")])
        found = detect.unclaimed_paths(board, TREE, "me", ["a.md", "b.md", "my notes.md"])
        assert codes(found) == ["unclaimed-path"]
        assert found[0].detail == "b.md my notes.md"
        assert found[0].remedy == "session-coord claim --repo /repo -- b.md 'my notes.md'"

    def test_a_peers_claim_does_not_count_as_mine(self):
        board = Board.of(TREE, [rec(charter="x"), rec("peer", claims=("a.md",), pane="%2")])
        assert codes(detect.unclaimed_paths(board, TREE, "me", ["a.md"])) == ["unclaimed-path"]

    def test_an_absolute_claim_inside_the_tree_counts(self):
        board = Board.of(TREE, [rec(claims=(f"{TREE}/a.md",))])
        assert detect.unclaimed_paths(board, TREE, "me", ["a.md"]) == []

    @pytest.mark.parametrize("claim,path", [
        ("README.md", "readme.md"),
        ("notes.md ", "notes.md"),
        ("docs/owned.md", "docs"),
        ("docs/*.md", "docs/deep/x.md"),
        ("*.md", "sub/x.md"),
        ("a[!b].md", "ab.md"),
    ])
    def test_a_claim_owns_only_what_it_names(self, claim, path):
        # Hostile pass on #10, finding 1: the peer matcher's leniency is a fail-open here.
        board = Board.of(TREE, [rec(claims=(claim,), charter="x")])
        assert codes(detect.unclaimed_paths(board, TREE, "me", [path])) == ["unclaimed-path"]

    @pytest.mark.parametrize("claim,path", [
        ("docs", "docs/a.md"), ("docs/", "docs/a.md"), ("docs/**", "docs/x/y.md"),
        ("docs/*.md", "docs/y.md"), ("**/*.md", "sub/x.md"), ("a[bc].md", "ab.md"),
    ])
    def test_and_does_own_what_it_names(self, claim, path):
        board = Board.of(TREE, [rec(claims=(claim,), charter="x")])
        assert detect.unclaimed_paths(board, TREE, "me", [path]) == []

    def test_with_no_record_in_this_tree_the_remedy_registers_here(self):
        # Hostile pass on #10, finding 2: `git -C /other ...` must not be told to claim in
        # the repo the session happens to sit in.
        board = Board.of(TREE, [])
        found = detect.unclaimed_paths(board, TREE, "sid-1", ["a.md"])
        assert found[0].remedy == ("session-coord register --repo /repo --cwd /repo "
                                   "--session sid-1 --claim a.md")

    def test_a_session_with_nothing_registered_is_marked_unoriented(self):
        board = Board.of(TREE, [rec()])
        assert codes(detect.unclaimed_paths(board, TREE, "me", ["a.md"])) == [
            "unclaimed-path", "unoriented"]

    @pytest.mark.parametrize("sid", ["", "stranger"])
    def test_an_unknown_session_has_claimed_nothing(self, sid):
        board = Board.of(TREE, [rec(claims=("a.md",))])
        assert "unclaimed-path" in codes(detect.unclaimed_paths(board, TREE, sid, ["a.md"]))

    def test_a_record_in_another_tree_does_not_count(self):
        board = Board.of(TREE, [rec(claims=("a.md",), workdir=f"{TREE}/.worktrees/t")])
        assert "unclaimed-path" in codes(detect.unclaimed_paths(board, TREE, "me", ["a.md"]))


@pytest.fixture
def estate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(cli.gitio, "workspace_root", lambda path: path)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    monkeypatch.setattr(cli, "_live", lambda records, table=None: list(records))
    return monkeypatch


class TestClaimCommand:
    def test_it_adds_without_dropping_what_was_claimed(self, estate, capsys):
        store.save(rec(claims=("a.md",)))
        assert cli.main(["claim", "--repo", TREE, "--", "b.md", "a.md", "b.md"]) == 0
        assert store.load_all(TREE)[0].claims == ("a.md", "b.md")
        assert "claimed: b.md" in capsys.readouterr().out

    def test_a_dash_prefixed_path_after_double_dash_is_a_path(self, estate):
        store.save(rec())
        assert cli.main(["claim", "--repo", TREE, "--", "-odd.md"]) == 0
        assert store.load_all(TREE)[0].claims == ("-odd.md",)

    def test_an_unregistered_session_is_told_how_to_register(self, estate, capsys):
        assert cli.main(["claim", "--repo", TREE, "a.md"]) == 2
        assert "session-coord register" in capsys.readouterr().err

    def test_by_session_id(self, estate):
        store.save(rec("other", pane="%9"))
        assert cli.main(["claim", "--repo", TREE, "--session", "other", "a.md"]) == 0
        assert store.load_all(TREE)[0].claims == ("a.md",)


class TestTheQuestionReachesTheBoard:
    def test_the_gate_passes_require_claim_only_when_asked(self, monkeypatch):
        from test_gate_explicit_paths import gate
        seen = []

        class R:
            stdout, returncode = "[]", 0

        monkeypatch.setattr(gate.subprocess, "run", lambda argv, **kw: seen.append(argv) or R())
        gate.ask("/t", "/t", ["a.md"], "me", require_claim=True)
        gate.ask("/t", "/t", ["a.md"], "me")
        assert "--require-claim" in seen[0] and "--require-claim" not in seen[1]

    def test_check_reports_unclaimed_paths_as_json(self, estate, capsys):
        import json
        board = Board.of(TREE, [rec(claims=("a.md",), charter="x")])
        estate.setattr(cli, "_live_board", lambda repo: board)
        estate.setattr(cli.gitio, "workdir", lambda path: path)
        estate.setattr(cli.detect, "check", lambda b: [])
        cli.main(["check", "--repo", TREE, "--json", "--workdir", TREE, "--path=a.md",
                  "--path=b.md", "--session=me", "--require-claim"])
        found = json.loads(capsys.readouterr().out)
        assert [(f["code"], f["detail"]) for f in found] == [("unclaimed-path", "b.md")]
