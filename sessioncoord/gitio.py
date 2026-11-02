"""Everything this package needs from git, behind one seam.

Kept separate from `detect` so the detectors stay pure and the suite can hold the 2026-09-17
tangle as a literal instead of standing up four worktrees. The `run` parameter is the seam:
tests pass a fake, production passes the default.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from .model import RepoFacts

Runner = Callable[[list[str], str], str]


def _run(argv: list[str], cwd: str) -> str:
    """Return stdout, or "" for any failure. Never raises; a missing repo is not an error here."""
    try:
        out = subprocess.run(argv, cwd=cwd or None, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def repo_root(path: str, run: Runner = _run) -> str | None:
    """The MAIN checkout of the repo containing `path`, not the worktree.

    This is the whole reason `git rev-parse --show-toplevel` is wrong here: in a worktree it
    returns the worktree, so four sessions in four worktrees would register as four unrelated
    repos and no detector would ever fire. `--git-common-dir` points at the shared `.git`, and
    its parent is the main checkout every worktree hangs off.
    """
    path = os.path.abspath(os.path.expanduser(path))
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    if not common:
        return None
    root = os.path.dirname(common.rstrip("/")) if os.path.basename(
        common.rstrip("/")) == ".git" else common
    return os.path.realpath(root) or None


# Files that mark a folder as a project when it is not a git repo. CLAUDE.md and .claude/ are
# what Claude Code itself treats as a project root; `.session-coord` is an explicit opt-in for
# a folder that has neither.
WORKSPACE_MARKERS = (".session-coord", "CLAUDE.md", ".claude")


def workspace_root(path: str, run: Runner = _run) -> str:
    """The key sessions are grouped under: the git main checkout, else a marked folder.

    Written for someone whose projects are folders of research and drafts rather than repos.
    Sessions that should see each other have to agree on one key, and keying on the exact cwd
    would split two sessions working in `Acme/` and `Acme/legal/` onto boards of one. So walk
    up to the nearest folder carrying a marker, stopping short of $HOME (whose `~/.claude` would
    otherwise make every folder on the machine one workspace), and fall back to the folder
    itself.
    """
    found = repo_root(path, run)
    if found:
        return found
    start = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    home = os.path.realpath(os.path.expanduser("~"))
    cur = start
    while True:
        if cur == home or cur == os.path.dirname(cur):
            break
        if any(os.path.exists(os.path.join(cur, m)) for m in WORKSPACE_MARKERS):
            return cur
        cur = os.path.dirname(cur)
    return start


def workdir(path: str, run: Runner = _run) -> str:
    """The working tree `path` sits in -- the worktree, when there is one."""
    top = run(["git", "rev-parse", "--show-toplevel"], os.path.abspath(path))
    return os.path.realpath(top) if top else os.path.abspath(path)


def branch(path: str, run: Runner = _run) -> str | None:
    b = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    return None if not b or b == "HEAD" else b


def default_branch(path: str, run: Runner = _run) -> str:
    ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    if ref.startswith("origin/"):
        return ref.split("/", 1)[1]
    for guess in ("main", "master"):
        if run(["git", "rev-parse", "--verify", "--quiet", guess], path):
            return guess
    return "main"


def dirty_paths(path: str, run: Runner = _run) -> tuple[str, ...]:
    out = run(["git", "status", "--porcelain"], path)
    return tuple(line[3:] for line in out.splitlines() if len(line) > 3)


#: A repo whose owner wants sessions to share the main checkout -- a docs repo that commits
#: straight to main, say -- declares it with this file at its root. It counts only when it is
#: on the REMOTE's default branch: creating a file, or even committing one locally, is
#: something a session can do to itself without anyone noticing, and pushing to the default
#: branch is not. 2026-11-02: the gate refused explicit commits in such a repo and told the
#: session to take a worktree, which contradicted the repo's own rule.
SHARED_CHECKOUT_MARKER = ".session-coord-shared-checkout"


def shared_checkout(repo: str, default: str, run: Runner = _run) -> bool:
    """True only when the marker is in `origin/<default>`. Any failure to look is False."""
    ref = f"origin/{default}:{SHARED_CHECKOUT_MARKER}"
    return bool(run(["git", "cat-file", "-t", ref], repo) == "blob")


def facts(repo: str, run: Runner = _run) -> RepoFacts:
    """One gather, passed to the detectors. Nothing re-queries git downstream.

    No `git fetch`: this runs on every session start and a network round trip there would be
    felt. `remote_default_sha` is therefore as fresh as the last fetch, which understates
    staleness rather than inventing it -- the same tradeoff `repo-status` already makes.
    """
    if not run(["git", "rev-parse", "--git-dir"], repo):
        return RepoFacts(repo_root=repo, is_git=False)
    dflt = default_branch(repo, run)
    return RepoFacts(
        repo_root=repo,
        default_branch=dflt,
        local_default_sha=run(["git", "rev-parse", "--verify", "--quiet", dflt], repo),
        remote_default_sha=run(
            ["git", "rev-parse", "--verify", "--quiet", f"origin/{dflt}"], repo),
        dirty_paths=dirty_paths(repo, run),
        shared_checkout=shared_checkout(repo, dflt, run),
    )
