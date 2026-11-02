"""The board half of the 2026-11-02 fix: claimed paths, and a repo that shares its checkout.

The gate can only let an explicit `git add` through if something can say whether a PEER claims
one of the named files. That is the board's job, so `check` answers it: `--path` names the
files, `--session` names who is asking, and a peer in the same tree that claims one of them
produces a blocking `claimed-path` finding.

The opt-out is for a repo whose owner wants sessions to share the main checkout -- the docs repo
in the incident commits straight to main with no worktree and no branch. A checked-in
`.session-coord-shared-checkout` on the remote's default branch downgrades shared-tree from
block to warn and stops telling sessions to take a worktree. It does NOT loosen the gate's
refusal of broad pathspecs; it only changes what the board says.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from sessioncoord import cli, detect, gitio, store
from sessioncoord.model import Board, Presence, RepoFacts

REPO = "/repo"


def board(shared_checkout=False, **claims):
    sessions = [
        Presence(session_id=sid, repo_root=REPO, workdir=REPO, claims=tuple(c))
        for sid, c in claims.items()
    ]
    return Board.of(REPO, sessions, facts=RepoFacts(repo_root=REPO,
                                                    shared_checkout=shared_checkout))


class TestTheOptOutDowngradesTheBlock:
    def test_by_default_a_shared_tree_blocks(self):
        (f,) = [f for f in detect.check(board(a=(), b=())) if f.code == "shared-tree"]
        assert f.severity == "block" and "worktree" in f.remedy

    def test_an_opted_out_repo_warns_and_says_what_to_do_instead(self):
        (f,) = [f for f in detect.check(board(True, a=(), b=())) if f.code == "shared-tree"]
        assert f.severity == "warn"
        assert "git worktree add" not in f.remedy
        assert "name" in f.remedy.lower() and ".session-coord-shared-checkout" in f.detail

    def test_an_opted_out_repo_does_not_nag_a_lone_main_checkout_session(self):
        sessions = [Presence(session_id="a", repo_root=REPO, workdir=REPO),
                    Presence(session_id="b", repo_root=REPO, workdir=REPO + "/.worktrees/x")]
        b = Board.of(REPO, sessions, facts=RepoFacts(repo_root=REPO, shared_checkout=True))
        assert "unworktreed" not in [f.code for f in detect.check(b)]


class TestClaimedPaths:
    def test_a_peer_claim_on_a_named_path_is_found(self):
        b = board(me=("a.md",), peer=("docs/**", "ai-chain-of-command.md"))
        got = detect.claimed_paths(b, REPO, "me", ["a.md", "ai-chain-of-command.md", "docs/x.md"])
        assert [(f.code, f.severity) for f in got] == [("claimed-path", "block")] * 2
        text = " ".join(f.summary + f.detail for f in got)
        assert "ai-chain-of-command.md" in text and "docs/x.md" in text and "peer" in text

    def test_its_own_claims_never_block_it(self):
        assert detect.claimed_paths(board(me=("a.md",), peer=()), REPO, "me", ["a.md"]) == []

    def test_unknown_caller_treats_every_claim_as_a_peers(self):
        # Fail closed: if the gate cannot say who is asking, nobody's claim can be excused.
        assert detect.claimed_paths(board(me=("a.md",)), REPO, "", ["a.md"])

    def test_an_absolute_claim_inside_the_tree_counts(self):
        b = board(me=(), peer=(REPO + "/notes/**",))
        assert detect.claimed_paths(b, REPO, "me", ["notes/n.md"])

    def test_an_absolute_claim_in_another_repo_does_not(self):
        b = board(me=(), peer=("/elsewhere/**",))
        assert detect.claimed_paths(b, REPO, "me", ["notes/n.md"]) == []

    def test_a_peer_in_another_worktree_does_not_block_this_tree(self):
        sessions = [Presence(session_id="me", repo_root=REPO, workdir=REPO),
                    Presence(session_id="peer", repo_root=REPO,
                             workdir=REPO + "/.worktrees/x", claims=("a.md",))]
        assert detect.claimed_paths(Board.of(REPO, sessions), REPO, "me", ["a.md"]) == []

    @pytest.mark.parametrize("claim,path", [
        ("*.md", "a.md"),
        ("docs/*/secret.md", "docs/x/secret.md"),
        ("docs/?.md", "docs/a.md"),
        ("docs/**/deep.md", "docs/a/b/deep.md"),
    ])
    def test_a_glob_claim_protects_what_it_matches(self, claim, path):
        # Hostile pass on PR #5, finding 4: prefix overlap ignored inner globs entirely.
        b = board(me=(), peer=(claim,))
        assert detect.claimed_paths(b, REPO, "me", [path]), (claim, path)

    def test_case_differences_do_not_slip_past_a_claim(self):
        # Finding 3's case half: on a case-insensitive filesystem `readme.md` is `README.md`.
        b = board(me=(), peer=("README.md",))
        assert detect.claimed_paths(b, REPO, "me", ["readme.md"])

    def test_an_absolute_claim_is_normalised_before_it_is_placed(self):
        # Copilot on PR #5: `/repo/../repo/docs/**` read as outside the tree.
        b = board(me=(), peer=(REPO + "/../repo/docs/**",))
        assert detect.claimed_paths(b, REPO, "me", ["docs/x.md"])

    def test_a_dot_slash_glob_claim_matches(self):
        b = board(me=(), peer=("./docs/*.md",))
        assert detect.claimed_paths(b, REPO, "me", ["docs/x.md"])

    def test_a_sibling_name_is_not_inside_a_claimed_directory(self):
        b = board(me=(), peer=("docs/cyber/**",))
        assert detect.claimed_paths(b, REPO, "me", ["docs/cyberpunk.md"]) == []


@pytest.fixture
def two_in_main(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: p.rstrip("/"))
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 1, "%2": 2})
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    store.save(Presence(session_id="me", repo_root=REPO, workdir=REPO, pane="%1",
                        claude_pid=1, window="w:me"))
    store.save(Presence(session_id="peer", repo_root=REPO, workdir=REPO, pane="%2",
                        claude_pid=2, window="w:peer", claims=("ai-chain-of-command.md",)))


class TestCheckAnswersForTheGate:
    def codes(self, capsys, *extra):
        cli.main(["check", "--repo", REPO, "--workdir", REPO, "--json", *extra])
        return [f["code"] for f in json.loads(capsys.readouterr().out)]

    def test_no_paths_means_no_claim_question(self, two_in_main, capsys):
        assert "claimed-path" not in self.codes(capsys)

    def test_unclaimed_paths_add_nothing(self, two_in_main, capsys):
        assert "claimed-path" not in self.codes(
            capsys, "--session", "me", "--path", "products.md", "--path", "components.md")

    def test_a_peers_path_is_reported(self, two_in_main, capsys):
        assert "claimed-path" in self.codes(
            capsys, "--session", "me", "--path", "a.md", "--path", "ai-chain-of-command.md")


class TestGitioReadsTheOptOutFromTheRemote:
    def _repo(self, tmp_path):
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                       capture_output=True)
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
        (work / "README.md").write_text("x\n")
        subprocess.run(git + ["add", "README.md"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        subprocess.run(git + ["push", "-q", "origin", "main"], check=True, capture_output=True)
        return work, git

    def test_absent_by_default(self, tmp_path):
        work, _ = self._repo(tmp_path)
        assert gitio.facts(str(work)).shared_checkout is False

    def test_an_uncommitted_marker_does_not_count(self, tmp_path):
        # A session must not be able to opt itself out by creating a file.
        work, _ = self._repo(tmp_path)
        (work / ".session-coord-shared-checkout").write_text("because\n")
        assert gitio.facts(str(work)).shared_checkout is False

    def test_a_local_commit_does_not_count_until_it_is_on_the_remote(self, tmp_path):
        work, git = self._repo(tmp_path)
        (work / ".session-coord-shared-checkout").write_text("because\n")
        subprocess.run(git + ["add", ".session-coord-shared-checkout"], check=True)
        subprocess.run(git + ["commit", "-qm", "opt out"], check=True)
        assert gitio.facts(str(work)).shared_checkout is False
        subprocess.run(git + ["push", "-q", "origin", "main"], check=True, capture_output=True)
        assert gitio.facts(str(work)).shared_checkout is True
