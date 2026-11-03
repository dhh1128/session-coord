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
import os
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
    def test_a_semicolon_or_newline_passes_from_an_unshared_session(
            self, trees, monkeypatch, capsys, sep):
        shared, other = trees
        lone = str(Path(other).parent)  # a directory nobody shares
        denied, asked = run(monkeypatch, capsys, f"cd {other}{sep}git add -A", lone, shared)
        assert denied is None, denied
        assert other in asked and lone in asked

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


class TestCopilotOnPR7:
    def test_a_semicolon_or_newline_from_the_shared_tree_is_refused(
            self, trees, monkeypatch, capsys):
        # The `cd` is checked before the shell runs it; if the target goes away in between,
        # `;` and a newline still run the write here.
        shared, other = trees
        for sep in ("; ", "\n"):
            denied, asked = run(monkeypatch, capsys, f"cd {other}{sep}git add -A", shared, shared)
            assert denied, sep
            assert "may not hold" in denied

    @pytest.mark.parametrize("rest", ["a || git add -A", "a; git add -A", "a & git add -A",
                                      "a\ngit add -A"])
    def test_a_rest_that_runs_whatever_the_cd_did(self, trees, monkeypatch, capsys, rest):
        shared, other = trees
        denied, _ = run(monkeypatch, capsys, f"cd {other} && {rest}", shared, shared)
        assert denied, rest

    def test_popd_through_a_pushd_stack(self, trees, monkeypatch, capsys, tmp_path):
        shared, other = trees
        safe = tmp_path / "safe"
        safe.mkdir()
        # The write lands in `safe`; a peer there must be seen.
        denied, asked = run(monkeypatch, capsys,
                            f"pushd {safe} && pushd {other} && popd && git add -A",
                            str(tmp_path), str(safe))
        assert denied
        assert str(safe) in asked

    def test_a_missing_component_hidden_by_dotdot(self, trees):
        shared, other = trees
        parent = str(Path(other).parent)
        assert gate._literal_cd("cd missing/../remote-oracle && git add -A", parent) is None
        assert gate._literal_cd("cd ../remote-oracle && git add -A", other)[0] == other

    @pytest.mark.parametrize("sep", ["\n", "; "])
    @pytest.mark.parametrize("op", ["&&", "||", "|", "&", ";"])
    def test_a_rest_opening_on_an_operator_is_a_syntax_error(self, trees, sep, op):
        _, other = trees
        assert gate._literal_cd(f"cd {other}{sep}{op} git add -A", "/") is None


class TestFixReviewOnPR7:
    """glm's read of the fix diff: a `cd` the resolver declines must still have its target
    asked, which `candidate_trees` does for any literal `cd` argument."""

    @pytest.mark.parametrize("command", [
        "cd {e}/../devenv && git add -A",
        "cd {lone} && cd e/../devenv && git add -A",
    ])
    def test_a_declined_dotdot_cd_still_has_its_target_asked(
            self, trees, monkeypatch, capsys, tmp_path, command):
        shared, other = trees
        (tmp_path / "e").mkdir()
        lone = str(tmp_path)
        real_ask = []

        def fake_ask(tree, cwd_, paths=(), session=""):
            resolved = os.path.normpath(os.path.join(cwd_, tree))
            real_ask.append(resolved)
            return [dict(SHARED, workdirs=[shared])] if resolved == shared else []

        monkeypatch.setattr(gate, "ask", fake_ask)
        payload = {"tool_input": {"command": command.format(e=tmp_path / "e", lone=lone)},
                   "cwd": str(Path(other).parent), "session_id": "me-id"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        gate.main()
        assert capsys.readouterr().out, real_ask
        assert shared in real_ask


class TestHostileFixPassOnPR7:
    def test_a_dotdot_through_real_directories_is_followed(self, trees, monkeypatch, capsys):
        shared, other = trees
        wt = Path(other).parent
        command = f"cd {wt}/../.worktrees/remote-oracle && git add -A"
        denied, asked = run(monkeypatch, capsys, command, shared, shared)
        assert denied is None, denied
        assert asked == [other]

    def test_a_symlink_then_dotdot_asks_the_logical_parent(
            self, trees, monkeypatch, capsys, tmp_path):
        shared, other = trees
        # `devenv/link` points elsewhere; bash's `cd devenv/link/..` lands in devenv.
        (Path(shared) / "link").symlink_to(other)
        assert gate._literal_cd(f"cd {shared}/link/.. && git add -A", "/") is None
        lone = str(tmp_path / "claude-zamu")
        denied, asked = run(monkeypatch, capsys, f"cd {shared}/link/.. && git add -A",
                            lone, shared)
        assert denied
        assert shared in asked

    def test_a_quoted_semicolon_is_a_word(self, trees, monkeypatch, capsys):
        shared, other = trees
        for tail in ("printf ';'", 'echo "a || b"', "echo '&'"):
            denied, _ = run(monkeypatch, capsys,
                            f"cd {other} && git add -A && {tail}", shared, shared)
            assert denied is None, (tail, denied)


class TestHostilePassOnPR8:
    @pytest.mark.parametrize("rest", ["echo \\#; git add -A", "echo a # x; git add -A"])
    def test_a_hash_does_not_hide_a_separator(self, rest):
        assert gate._runs_only_after(" " + rest) is False

    def test_many_named_paths_stay_linear(self):
        import time
        command = "git " + " ".join(f"-C /p{i}" for i in range(20000)) + " add -A"
        t = time.monotonic()
        gate.candidate_trees(command, "/")
        assert time.monotonic() - t < 2

    @pytest.mark.parametrize("rest,sure", [
        (r"printf a\;b && git add -A", True),
        (r'echo "a\"; b" && git add -A', True),
        (r"echo 'a\'; git add -A", False),
        (r"echo a\\; git add -A", False),
    ])
    def test_backslash_escapes(self, rest, sure):
        assert gate._runs_only_after(" " + rest) is sure

    def test_a_symlinks_physical_parent_is_asked_as_well(self, tmp_path):
        # Copilot on PR #8: `set -o physical` makes bash's cd physical, so both are asked.
        real = tmp_path / "real" / "bin"
        real.mkdir(parents=True)
        (tmp_path / "bin").symlink_to(real)
        got = gate.candidate_trees(f"cd {tmp_path}/bin/.. && git add -A", "/")
        assert got == [str(tmp_path), str(tmp_path / "real")]

    def test_too_many_candidates_refuses(self, trees, monkeypatch, capsys):
        shared, _ = trees
        command = "cd $X && git " + " ".join(f"-C /p{i}" for i in range(9)) + " add -A"
        denied, _ = run(monkeypatch, capsys, command, shared, "/nobody")
        assert denied and "more than the 8" in denied

    def test_non_git_dash_C_arguments_are_not_candidates(self):
        command = "bash -c 'git add -A'; printf '%s' " + " ".join(f"-C /p{i}" for i in range(9))
        assert gate.candidate_trees(command, "/") == []
        assert gate.candidate_trees("xargs git -C /x add -A", "/") == ["/x"]

    @pytest.mark.parametrize("command", [
        "xargs git -C /shared add -A",
        "xargs -0 git add -A",
        "find . -name x -exec git add {} +",
    ])
    def test_a_runner_carrying_a_git_write_is_a_write(self, command):
        assert gate.analyse(command, "/")[0] is True

    def test_a_runner_reaches_the_shared_tree(self, monkeypatch, capsys):
        denied, asked = run(monkeypatch, capsys, "xargs git -C /shared add -A", "/lone",
                            "/shared")
        assert denied and "/shared" in asked

    def test_a_printed_git_word_starts_no_scan(self):
        command = "bash -c 'git add -A'; printf '%s' git " + " ".join(
            f"-C /p{i}" for i in range(9))
        assert gate.candidate_trees(command, "/") == []
