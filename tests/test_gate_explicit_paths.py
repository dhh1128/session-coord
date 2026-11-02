"""A shared tree must still let a session commit files it names and nobody else claims.

2026-11-02, a docs repo whose owner's rule is to commit straight to main from the main checkout.
Two sessions were registered there. One ran

    git add products.md components.md && git commit -s -m '...'

and later the same shape for `roadmap.md roadmap-inputs.md`. The gate refused both with
`[block] shared-tree`. The other session's only uncommitted change was a third file neither
command named, and the user had to run the commits by hand through a script.

The hazard behind the gate is a BROAD pathspec -- `git add -A` sweeping a stranger's audit into
your commit, 2026-09-17. A list of explicit files the caller owns cannot sweep anything, so
refusing it bought no safety and cost a round trip. What follows pins both halves: the explicit
forms pass, and every broad form the handoff named still fails.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_ep", importlib.machinery.SourceFileLoader("gate_ep", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

INCIDENT_1 = ("git add products.md components.md && git commit -s -m "
              "'Products and components: name what we sell'")
INCIDENT_2 = ("git add roadmap.md roadmap-inputs.md && git commit -s -m "
              "'Roadmap: fold in the inputs'")


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    for name in ("products.md", "components.md", "roadmap.md", "roadmap-inputs.md",
                 "a.md", "b.md", "ai-chain-of-command.md", "sub/c.md"):
        (root / name).write_text("x\n")
    return str(root)


def form(command, cwd, root=None):
    return gate.staging_form(command, cwd, root or cwd)


class TestTheIncidentsCommandsAreExplicit:
    @pytest.mark.parametrize("command,paths", [
        (INCIDENT_1, {"products.md", "components.md"}),
        (INCIDENT_2, {"roadmap.md", "roadmap-inputs.md"}),
    ])
    def test_the_two_commands_that_were_refused(self, tree, command, paths):
        f = form(command, tree)
        assert f.ok, f.reason
        assert set(f.paths) == paths
        # A bare commit after the adds: safe only if nothing ELSE is staged, which the gate
        # checks against the index rather than assuming.
        assert f.bare_commit


class TestOtherExplicitFormsPass:
    @pytest.mark.parametrize("command,paths,bare", [
        ("git add a.md", {"a.md"}, False),
        ("git add -- a.md b.md", {"a.md", "b.md"}, False),
        ("git commit -s -m x -- a.md b.md", {"a.md", "b.md"}, False),
        ("git commit -m x a.md", {"a.md"}, False),
        ("git commit -sm 'a message' a.md", {"a.md"}, False),
        ("git commit --message=x --signoff -- a.md", {"a.md"}, False),
        ("git commit -F /tmp/msg.txt -- a.md", {"a.md"}, False),
        ("git add a.md && git commit -s -F - -- a.md", {"a.md"}, False),
        ("/usr/bin/git add a.md", {"a.md"}, False),
        ("git add sub/c.md", {"sub/c.md"}, False),
        ("git status && git add a.md && git log -1", {"a.md"}, False),
        ("git add deleted-file.md", {"deleted-file.md"}, False),
    ])
    def test_explicit_paths(self, tree, command, paths, bare):
        f = form(command, tree)
        assert f.ok, (command, f.reason)
        assert set(f.paths) == paths
        assert f.bare_commit is bare

    def test_paths_are_relative_to_the_tree_root_from_a_subdirectory(self, tree):
        f = form("git add c.md", tree + "/sub", tree)
        assert f.ok and f.paths == ("sub/c.md",)

    def test_a_dash_C_into_the_same_tree_is_resolved(self, tree):
        f = form(f"git -C {tree}/sub add c.md", tree, tree)
        assert f.ok and f.paths == ("sub/c.md",)


class TestBroadFormsAreStillRefused:
    @pytest.mark.parametrize("command", [
        "git add -A",
        "git add --all",
        "git add .",
        "git add -u",
        "git add --update",
        "git add",
        "git add -p a.md",
        "git add -i",
        "git add sub",
        "git add sub/",
        "git add '*.md'",
        "git add a?.md",
        "git add '[ab].md'",
        "git add ':(glob)*.md'",
        "git add :/",
        "git add ':/a.md'",
        "git add --pathspec-from-file=list.txt",
        "git add --pathspec-from-file list.txt",
        "git add ../outside.md",
        "git add $FILE",
        "git add ~/x.md",
        "git commit -a -m x",
        "git commit -am x",
        "git commit --all -m x",
        "git commit -s -m x",
        "git commit",
        "git commit --amend -m x -- a.md",
        "git commit -i -m x a.md",
        "git commit --include -m x a.md",
        "git commit -p",
        "git commit --pathspec-from-file=list.txt -m x",
        "git update-index --add a.md",
        "git add a.md && git stash",
        "git add a.md && git checkout b.md",
        "git rm a.md",
        "git add a.md -A",
    ])
    def test_broad(self, tree, command):
        f = form(command, tree)
        assert not f.ok, command
        assert f.reason, command

    @pytest.mark.parametrize("command", [
        "cd sub && git add c.md",
        "bash -c 'git add a.md'",
        "GIT_WORK_TREE=/elsewhere git add a.md",
        "git --work-tree=/elsewhere add a.md",
    ])
    def test_a_tree_the_gate_cannot_name_is_refused(self, tree, command):
        assert not form(command, tree).ok, command

    def test_a_bare_commit_without_an_add_says_to_name_the_files(self, tree):
        f = form("git commit -s -m x", tree)
        assert not f.ok and "git commit" in f.reason and "--" in f.reason


# ----- the gate end to end, with the board and the index replaced -------------------------

SHARED = {"code": "shared-tree", "severity": "block", "summary": "2 sessions",
          "detail": "shared", "remedy": "Each session takes its own worktree: ...",
          "sessions": ["peer", "me"], "workdirs": []}


def run_gate(monkeypatch, capsys, command, cwd, findings, staged=()):
    asked = []

    def fake_ask(tree, cwd_, paths=(), session=""):
        asked.append((tree, tuple(paths), session))
        out = []
        for f in findings:
            f = dict(f)
            if f["code"] == "shared-tree":
                f["workdirs"] = [cwd]
            if f["code"] == "claimed-path" and not paths:
                continue
            out.append(f)
        return out

    monkeypatch.setattr(gate, "ask", fake_ask)
    monkeypatch.setattr(gate, "staged_paths", lambda root: None if staged is None else tuple(staged))
    payload = {"tool_input": {"command": command}, "cwd": cwd, "session_id": "me-id"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert gate.main() == 0
    out = capsys.readouterr().out
    return (json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else None,
            asked)


class TestTheGateEndToEnd:
    def test_the_incident_passes_in_a_shared_tree(self, tree, monkeypatch, capsys):
        for cmd, staged in ((INCIDENT_1, ()), (INCIDENT_2, ("roadmap.md",))):
            denied, asked = run_gate(monkeypatch, capsys, cmd, tree, [SHARED], staged)
            assert denied is None, denied
        # The board was asked about exactly the named paths, on behalf of this session.
        assert asked[-1][1] == ("roadmap.md", "roadmap-inputs.md")
        assert asked[-1][2] == "me-id"

    def test_add_all_is_still_refused_in_the_same_tree(self, tree, monkeypatch, capsys):
        denied, _ = run_gate(monkeypatch, capsys, "git add -A", tree, [SHARED])
        assert denied and "-A" in denied
        assert "name the files" in denied.lower()

    def test_a_peers_claim_refuses_and_names_the_peer(self, tree, monkeypatch, capsys):
        claimed = {"code": "claimed-path", "severity": "block",
                   "summary": "ai-chain-of-command.md is claimed by peer",
                   "detail": "ai-chain-of-command.md (claim: ai-chain-of-command.md)",
                   "remedy": "", "sessions": ["peer"], "workdirs": [tree]}
        denied, _ = run_gate(monkeypatch, capsys,
                             "git add a.md ai-chain-of-command.md", tree, [SHARED, claimed])
        assert denied and "peer" in denied and "ai-chain-of-command.md" in denied

    def test_a_bare_commit_refuses_when_something_else_is_staged(
            self, tree, monkeypatch, capsys):
        # The peer staged its file; the bare commit would carry it under our message.
        denied, _ = run_gate(monkeypatch, capsys, INCIDENT_1, tree, [SHARED],
                             staged=("products.md", "ai-chain-of-command.md"))
        assert denied and "ai-chain-of-command.md" in denied
        assert "git commit" in denied and "--" in denied

    def test_an_unreadable_index_refuses_a_bare_commit(self, tree, monkeypatch, capsys):
        denied, _ = run_gate(monkeypatch, capsys, INCIDENT_1, tree, [SHARED], staged=None)
        assert denied

    def test_but_an_explicit_commit_does_not_need_the_index(self, tree, monkeypatch, capsys):
        denied, _ = run_gate(monkeypatch, capsys, "git commit -s -m x -- a.md", tree,
                             [SHARED], staged=None)
        assert denied is None

    def test_a_non_staging_write_still_gets_the_old_refusal(self, tree, monkeypatch, capsys):
        denied, _ = run_gate(monkeypatch, capsys, "git stash", tree, [SHARED])
        assert denied and "worktree" in denied

    def test_alone_nothing_changes(self, tree, monkeypatch, capsys):
        denied, _ = run_gate(monkeypatch, capsys, "git add -A", tree, [])
        assert denied is None

    def test_an_opted_out_repo_does_not_tell_a_session_to_take_a_worktree(
            self, tree, monkeypatch, capsys):
        opted = dict(SHARED, severity="warn",
                     remedy="This repo shares its main checkout on purpose: name the files.")
        denied, _ = run_gate(monkeypatch, capsys, "git add -A", tree, [opted])
        assert denied and "git worktree add" not in denied
        denied, _ = run_gate(monkeypatch, capsys, INCIDENT_1, tree, [opted])
        assert denied is None


class TestStagedPathsReadsTheRealIndex:
    def test_it_lists_what_is_staged(self, tree):
        subprocess.run(["git", "init", "-q", tree], check=True)
        subprocess.run(["git", "-C", tree, "add", "a.md", "sub/c.md"], check=True)
        assert set(gate.staged_paths(tree)) == {"a.md", "sub/c.md"}

    def test_it_says_none_when_it_cannot_look(self, tmp_path):
        assert gate.staged_paths(str(tmp_path / "not-a-repo")) is None
