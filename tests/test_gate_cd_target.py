"""The gate judges the tree a command writes to, not the session's launch directory.

2026-11-03. A session launched in a devenv checkout that another live session also had open did
its work in a worktree of a DIFFERENT repo, claude-zamu, and committed there with

    cd ~/code/me/claude-zamu/.worktrees/remote-oracle && git add -A && git commit -qs -F - <<'EOF'
    ...
    EOF

The gate refused it with devenv's shared-tree finding. Nothing in devenv was at risk. A literal
`cd X &&` was already followed, but only on a one-line command, and the heredoc commit message
made this one multi-line. EnterWorktree could not help: it only enters worktrees of the
session's own repo.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_cdt", importlib.machinery.SourceFileLoader("gate_cdt", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

SHARED = {"code": "shared-tree", "severity": "block", "summary": "2 sessions",
          "detail": "shared", "remedy": "Each session takes its own worktree: ...",
          "sessions": ["peer", "me"]}


@pytest.fixture
def trees(tmp_path, monkeypatch):
    monkeypatch.delenv("CDPATH", raising=False)
    shared = tmp_path / "devenv"
    other = tmp_path / "claude-zamu" / ".worktrees" / "remote-oracle"
    shared.mkdir()
    other.mkdir(parents=True)
    return str(shared), str(other)


def run(monkeypatch, capsys, command, cwd, shared):
    """Run the gate with a board on which only `shared` has a peer. Returns (denied, asked)."""
    asked = []

    def fake_ask(tree, cwd_, paths=(), session=""):
        asked.append(tree)
        return [dict(SHARED, workdirs=[shared])] if tree == shared else []

    monkeypatch.setattr(gate, "ask", fake_ask)
    payload = {"tool_input": {"command": command}, "cwd": cwd, "session_id": "me-id"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert gate.main() == 0
    out = capsys.readouterr().out
    return (json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else None,
            asked)


INCIDENT = ("cd {other} && git status --short && git add -A && git commit -qs -F - <<'EOF'\n"
            "Add the remote oracle\n\nBody that mentions git add -A in passing.\nEOF")


class TestACommitInAnotherRepoIsJudgedThere:
    def test_the_incident_command_passes(self, trees, monkeypatch, capsys):
        shared, other = trees
        denied, asked = run(monkeypatch, capsys, INCIDENT.format(other=other), shared, shared)
        assert denied is None, denied
        assert asked == [other]

    def test_git_dash_C_into_the_other_repo_passes(self, trees, monkeypatch, capsys):
        shared, other = trees
        denied, asked = run(monkeypatch, capsys, f"git -C {other} add -A", shared, shared)
        assert denied is None, denied
        assert asked == [other]

    @pytest.mark.parametrize("sep", ["; ", "\n"])
    def test_a_semicolon_or_newline_after_the_cd(self, trees, monkeypatch, capsys, sep):
        shared, other = trees
        denied, asked = run(monkeypatch, capsys, f"cd {other}{sep}git add -A", shared, shared)
        assert denied is None, denied
        assert asked == [other]

    def test_pushd(self, trees, monkeypatch, capsys):
        shared, other = trees
        denied, _ = run(monkeypatch, capsys, f"pushd {other} && git add -A", shared, shared)
        assert denied is None, denied


class TestStillRefused:
    def test_a_cd_into_the_shared_tree_itself(self, trees, monkeypatch, capsys):
        shared, other = trees
        denied, asked = run(monkeypatch, capsys, f"cd {shared} && git add -A", other, shared)
        assert denied and "shared" in denied
        assert asked == [shared]

    def test_the_incident_shape_aimed_at_the_shared_tree(self, trees, monkeypatch, capsys):
        shared, other = trees
        denied, _ = run(monkeypatch, capsys, INCIDENT.format(other=shared), other, shared)
        assert denied

    def test_a_second_cd_back_into_the_shared_tree(self, trees, monkeypatch, capsys):
        shared, other = trees
        for command in (f"cd {other} && cd {shared} && git add -A",
                        f"cd {other}\ncd {shared}\ngit add -A"):
            denied, _ = run(monkeypatch, capsys, command, shared, shared)
            assert denied, command

    @pytest.mark.parametrize("command", [
        'cd "$W" && git add -A',
        "cd $(git rev-parse --show-toplevel) && git add -A",
        "cd `pwd` && git add -A",
        "(cd {other} && git add -A)",
        "cd {other} || git add -A",
        "true && cd {other} && git add -A",
    ])
    def test_a_target_it_cannot_name_falls_back_to_the_session_dir(
            self, trees, monkeypatch, capsys, command):
        shared, other = trees
        denied, asked = run(monkeypatch, capsys, command.format(other=other), shared, shared)
        assert denied, command
        assert shared in asked

    def test_a_relative_cd_while_CDPATH_is_set(self, trees, monkeypatch, capsys):
        shared, other = trees
        rel = Path(other).relative_to(Path(shared).parent)
        monkeypatch.setenv("CDPATH", "/somewhere")
        assert gate._literal_cd(f"cd ../{rel} && git add -A", shared) is not None  # bash skips CDPATH for ../
        assert gate._literal_cd(f"cd {rel} && git add -A", str(Path(shared).parent)) is None

    def test_an_unsearchable_directory(self, trees, monkeypatch):
        _, other = trees
        monkeypatch.setattr(gate.os, "access", lambda p, m: False)
        assert gate._literal_cd(f"cd {other} && git add -A", "/") is None


class TestHostilePassOnPR7:
    """codex's full pass on PR #7. Each was reproduced in bash and through the hook."""

    def test_popd_back_into_the_shared_tree(self, trees, monkeypatch, capsys):
        shared, other = trees
        for command in (f"pushd {other}; popd; git add -A",
                        f"pushd {other} && popd && git add -A"):
            denied, asked = run(monkeypatch, capsys, command, shared, shared)
            assert denied, command
            assert shared in asked

    def test_an_unreadable_rest_after_a_cd_also_asks_the_session_tree(
            self, trees, monkeypatch, capsys):
        shared, other = trees
        denied, _ = run(monkeypatch, capsys, f'cd {other} && cd "$W" && git add -A',
                        shared, shared)
        assert denied

    @pytest.mark.parametrize("quoted", ["'~'", '"~"', "\\~", "'~/x'", '~"/x"'])
    def test_a_quoted_tilde_is_not_expanded(self, quoted):
        assert gate._literal_cd(f"cd {quoted}; git add -A", "/") is None

    def test_an_unquoted_tilde_is(self):
        import os
        assert gate._literal_cd("cd ~; git add -A", "/")[0] == os.path.expanduser("~")

    @pytest.mark.parametrize("ch", ["\r", "\v", "\f", "\x00"])
    def test_a_control_character_in_the_cd_line(self, trees, ch):
        _, other = trees
        assert gate._literal_cd(f"cd {other}{ch}; git add -A", "/") is None
