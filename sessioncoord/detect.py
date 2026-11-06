"""What the board notices about a set of live sessions in one repo.

Every detector traces to a cost paid on 2026-09-17 in `acme/infra`. Each one carries its
incident in a comment, so a future reader can tell an observed hazard from an imagined one --
and can retire a detector honestly if the hazard stops being real.

The hardest constraint is not what these fire on but what they stay silent about. One session
alone in a repo is almost every session, and it must produce exactly nothing: a board that
speaks in the common case gets muted, and a muted board protects nobody.

Pure. Everything comes in on the `Board`; nothing here shells out.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from collections import defaultdict
from typing import Iterable, Sequence

from .who import user

from .model import Board, CONDUCTOR, Finding, Presence, order_findings


def _norm_claim(claim: str) -> str:
    """A claim reduced to the path prefix it governs.

    `docs/cyber/**` and `docs/cyber/` both govern `docs/cyber`. Trailing glob segments are
    dropped rather than interpreted: full glob-intersection is a much harder problem than this
    needs, and the useful cases are a directory against a file inside it.
    """
    c = claim.strip().strip("/")
    while c.endswith("*"):
        c = c.rstrip("*").rstrip("/")
    # A claim that reduces to nothing was a repo-wide glob -- `**`, `*`, `.` -- and returning
    # "" made it collide with NOTHING, which is the exact opposite of what the author meant.
    # ROOT is a sentinel that overlaps every non-empty claim.
    return posixpath.normpath(c) if c else ROOT


# Everything is inside the repo root, so a claim that normalises to it overlaps all others.
ROOT = "."


def _claims_overlap(a: str, b: str) -> bool:
    """True when two claims can touch the same file.

    The separator check is load-bearing: a raw `startswith` makes `docs/cyberpunk` read as
    inside `docs/cyber`, which would produce collisions between sessions that never meet.
    """
    x, y = _norm_claim(a), _norm_claim(b)
    if not x or not y:
        return False
    if x == ROOT or y == ROOT:
        return True
    if x == y:
        return True
    return y.startswith(x + "/") or x.startswith(y + "/")


def _group(sessions: Iterable[Presence], key) -> dict:
    out = defaultdict(list)
    for s in sessions:
        k = key(s)
        if k is not None:
            out[k].append(s)
    return out


def _labels(sessions: Sequence[Presence]) -> tuple[str, ...]:
    return tuple(sorted(s.label for s in sessions))


def detect_shared_tree(board: Board) -> list[Finding]:
    """Two or more sessions editing the same files.

    2026-09-17: four sessions sat in `acme/infra`'s single checkout. `infra:6`'s working
    tree held `infra:11`'s uncommitted audit -- 44 lines of `inventory.md` plus two untracked
    files -- and `infra:6` commits with `git add -A`. One more commit and it would have
    landed a stranger's work under its own message, silently. Nothing prevented it but timing.
    """
    out = []
    for workdir, group in _group(board.sessions, lambda s: s.workdir.rstrip("/")).items():
        if len(group) < 2:
            continue
        if not _is_git(board):
            out.append(_shared_folder(workdir, group))
            continue
        if _shared_on_purpose(board):
            out.append(_shared_checkout(workdir, group))
            continue
        out.append(Finding(
            code="shared-tree",
            severity="block",
            summary=f"{len(group)} sessions are editing the same working tree",
            detail=(
                f"{workdir} is open in {', '.join(_labels(group))}. A `git add -A` in any of "
                "them reaches the others' uncommitted files, and `git status` shows every "
                "session changes it did not make."
            ),
            sessions=_labels(group),
            workdirs=(workdir,),
            remedy=(
                "Each session takes its own worktree: `git worktree add "
                "<repo>/.worktrees/<topic> -b <topic> origin/main`, then EnterWorktree into "
                "it -- a running session can relocate mid-conversation without restarting."
            ),
        ))
    return out


def _is_git(board: Board) -> bool:
    return board.facts is None or board.facts.is_git


def _shared_on_purpose(board: Board) -> bool:
    return board.facts is not None and board.facts.shared_checkout


# What the gate will still let through in a shared tree. Kept in one place because the board's
# remedy and the gate's refusal both have to say it, and they must not drift apart.
EXPLICIT_REMEDY = (
    "Name the files explicitly: `git add <file> <file>` and `git commit -- <file> <file>`. "
    "A bare `git commit` after `git add` in the same command is fine when nothing else is "
    "staged. Broad forms (`add -A`, `add .`, `add -u`, `commit -a`, directories, globs) stay "
    "refused, and so is any file another session in this tree has claimed."
)


def _shared_checkout(workdir: str, group: Sequence[Presence]) -> Finding:
    """A repo whose owner wants its sessions in one checkout.

    2026-11-02: a docs repo whose rule is to commit straight to main from the main checkout,
    no worktree and no branch. The block told its sessions to take a worktree -- contradicting
    the repo -- and refused commits that named two files nobody else had touched. The owner
    can now say so in a checked-in file, and the finding becomes a warning that says what is
    still safe. The gate's refusal of broad pathspecs does not change: the hazard is the same.
    """
    from .gitio import SHARED_CHECKOUT_MARKER
    return Finding(
        code="shared-tree",
        severity="warn",
        summary=f"{len(group)} sessions share this checkout (by design)",
        detail=(
            f"{workdir} is open in {', '.join(_labels(group))}. This repo's "
            f"{SHARED_CHECKOUT_MARKER} says sessions share its main checkout on purpose, so "
            "stage only your own files and claim them first."
        ),
        sessions=_labels(group),
        workdirs=(workdir,),
        remedy=("Claim each file before you commit it: `session-coord claim -- <file> "
                "<file>`. Here the gate refuses a commit of any file this session has not "
                "claimed. " + EXPLICIT_REMEDY),
    )


def _claim_relative(claim: str, workdir: str, strip: bool = True) -> str | None:
    """A claim as a path relative to `workdir`, or None when it governs somewhere else.

    Claims are usually repo-relative (`docs/**`), but sessions also claim absolute paths, and
    an absolute claim outside this tree says nothing about it.
    """
    # Normalised lexically first: `/repo/../repo/docs/**` and `./docs/*.md` are ordinary
    # spellings of claims, and the gate normalises the paths it asks about (Copilot on PR #5).
    # `strip` is for the peer side, where erring toward a match is safe; ownership passes
    # False, because `notes.md ` is a different file from `notes.md`.
    c = posixpath.normpath(claim.strip() if strip else claim)
    if not posixpath.isabs(c):
        return c
    root = posixpath.normpath(workdir)
    # The ownership side (strip=False) reads `/repo/*` as `*`, one root segment, through the
    # prefix branch below. Collapsing it to `**` is safe only where a match refuses (Copilot
    # on #10).
    if c.rstrip("/") == root or (strip and c.rstrip("/*") == root):
        return "**"
    if c.startswith(root + "/"):
        return c[len(root) + 1:]
    return None


def _claim_covers(claim: str, path: str) -> bool:
    """True when `claim` could govern `path`. Errs toward True; a false hit only refuses.

    Prefix overlap alone ignored any glob before the last segment, so `*.md` and
    `docs/*/secret.md` protected nothing (hostile pass on PR #5, finding 4). `fnmatch` lets
    `*` cross `/`, which over-matches -- the safe direction for a check that authorises.
    Compared casefolded, because on a case-insensitive filesystem `readme.md` IS `README.md`.
    """
    c, p = claim.strip().casefold(), path.strip().strip("/").casefold()
    if _claims_overlap(c, p):
        return True
    if any(ch in c for ch in "*?["):
        pat = c.strip("/")
        return fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p, pat + "/*")
    return False


def claimed_paths(board: Board, workdir: str, session_id: str,
                  paths: Iterable[str]) -> list[Finding]:
    """A finding for each named path that another session in `workdir` has claimed.

    What lets the gate pass an explicit `git add` in a shared tree: the files must be ones no
    peer has said it is editing. Only peers in the SAME tree count -- one in its own worktree
    stages into its own index, and claim-overlap already reports the conflict in intent.

    An empty `session_id` means the caller could not say who it is. Then every claim counts,
    its own included, because excusing a claim needs knowing whose it is.
    """
    here = workdir.rstrip("/")
    peers = [s for s in board.sessions
             if s.workdir.rstrip("/") == here and (not session_id or s.session_id != session_id)]
    out = []
    for path in paths:
        for s in peers:
            hit = next((c for c in s.claims
                        if (rel := _claim_relative(c, here)) is not None
                        and _claim_covers(rel, path)), None)
            if hit is None:
                continue
            out.append(Finding(
                code="claimed-path",
                severity="block",
                summary=f"{path} is claimed by {s.label}",
                detail=f"{path} (claim: {hit})",
                sessions=(s.label,),
                workdirs=(here,),
                remedy=f"Leave {path} to {s.label}, or agree a hand-off with it first.",
            ))
            break
    return out


def _glob_regex(pattern: str) -> str:
    """A claim glob as a regex where `*` and `?` stay inside one path segment and `**` spans
    segments -- what a session means by `docs/*.md` or `drafts/**`."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1:j].replace("\\", "\\\\")
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _claim_owns(claim: str, path: str) -> bool:
    """True only when `claim` names `path` itself, a directory above it, or a glob matching it.

    Deliberately NOT `_claim_covers`. That one errs toward True, which is the safe direction
    when a peer's claim REFUSES a commit. Here a claim AUTHORISES one, so the same leniency
    is a fail-open: it accepted `README.md` for `readme.md`, `notes.md ` for `notes.md`, and
    `docs/owned.md` for `docs` (hostile pass on #10, finding 1). Exact, case-sensitive, no
    trimming; a refusal on a case-insensitive filesystem is the safe error.
    """
    if claim in (".", "**"):
        # The repo root: `claim -- .` or an absolute claim of the tree itself (Copilot on #10).
        return True
    if any(ch in claim for ch in "*?["):
        try:
            return re.fullmatch(_glob_regex(claim.rstrip("/")), path) is not None
        except re.error:
            # A malformed glob such as `[]` owns nothing. Raising would turn one bad claim
            # into a board the gate cannot ask, refusing every commit (Copilot on #10).
            return False
    c = claim.rstrip("/")
    return path == c or path.startswith(c + "/")


def unclaimed_paths(board: Board, workdir: str, session_id: str,
                    paths: Iterable[str]) -> list[Finding]:
    """Findings for the named paths the asking session has NOT claimed, in a repo that shares
    its checkout on purpose.

    2026-11-05, Daniel's call (R-AS3C): where sessions share a checkout by design, an explicit
    path is not enough. Two sessions editing the same file each see it dirty, and an explicit
    `git commit -- that.md` carries both sets of edits under one session's message. Nothing
    records who dirtied a file, so a claim is the only evidence the gate can ask for. Requiring
    it turns a silent overlap into a declaration both sessions can see.

    One finding, listing every unclaimed path, so the refusal can hand back a single claim
    line. A caller that cannot say who it is, or has no record in this tree, gets the same
    finding for every path: excusing a path needs knowing whose claim it is.
    """
    here = workdir.rstrip("/")
    me = next((s for s in board.sessions
               if session_id and s.session_id == session_id and s.workdir.rstrip("/") == here),
              None)
    # A blank claim is no claim. normpath turns "" into ".", which owns the whole tree, so an
    # unset shell variable in `claim -- "$F"` would have authorised every file (hostile pass
    # at ff11154).
    own = tuple(c for c in (me.claims if me else ()) if c.strip())
    missing = [p for p in paths
               if not any((rel := _claim_relative(c, here, strip=False)) is not None
                          and _claim_owns(rel, p)
                          for c in own)]
    if not missing:
        return []
    # The remedy names the tree. A session committing into another repo with `git -C` would
    # otherwise claim the file in the repo it happens to sit in, be told it succeeded, and be
    # refused again (hostile pass on #10, finding 2). With no record in that tree, it has to
    # register there first, and the gate knows its session id.
    files = " ".join(shlex.quote(p) for p in missing)
    tree = shlex.quote(here)
    if me is not None:
        remedy = f"session-coord claim --repo {tree} -- {files}"
    else:
        sid = f" --session {shlex.quote(session_id)}" if session_id else ""
        remedy = (f"session-coord register --repo {tree} --cwd {tree}{sid} "
                  + " ".join(f"--claim {shlex.quote(p)}" for p in missing))
    out = [Finding(
        code="unclaimed-path",
        severity="block",
        summary=f"{len(missing)} path(s) this session has not claimed",
        detail=" ".join(missing),
        workdirs=(here,),
        remedy=remedy,
    )]
    if me is None or (not me.charter and not me.claims):
        out.append(Finding(
            code="unoriented",
            severity="note",
            summary="this session has registered no charter and no claims",
            detail=("" if me else "no record for this session in this tree"),
            workdirs=(here,),
            remedy=("Load the session-coord skill and follow 'Say who you are, what you are "
                    "for, and what you will write'."),
        ))
    return out


def _shared_folder(workdir: str, group: Sequence[Presence]) -> Finding:
    """The same hazard in a plain folder, where git cannot be the remedy.

    Nothing stages anything here, so there is nothing for the gate to refuse and the finding is
    a warning rather than a block. The collision that remains is two sessions writing one
    document -- a synthesis, a memo, a deck -- each overwriting the other's last edit, or
    reaching opposite conclusions in adjacent sections without either seeing the other.
    """
    return Finding(
        code="shared-tree",
        severity="warn",
        summary=f"{len(group)} sessions are working in the same folder",
        detail=(
            f"{workdir} is open in {', '.join(_labels(group))}. Each can overwrite a file the "
            "other is editing, and neither sees the other's conclusions unless told."
        ),
        sessions=_labels(group),
        workdirs=(workdir,),
        remedy=(
            "Each session claims the files it will write (`session-coord register --claim "
            "'<path>'`), so an overlap is flagged before it happens, and one session owns each "
            "output document."
        ),
    )


def detect_shared_branch(board: Board) -> list[Finding]:
    """Separate worktrees committing to one branch.

    2026-09-17: `infra:6` and `infra:8` had both been committing to
    `drill-rename-and-trial-tenant` without knowing about each other. Seventeen commits, split
    10/4/3 -- and the last three belonged to neither of them. Afterwards nobody could say who
    wrote them.

    Sessions already caught sharing a tree are excluded: a working tree holds exactly one
    branch, so reporting both would split one problem into two and dilute the block.
    """
    shared_trees = {
        s.workdir.rstrip("/")
        for workdir, g in _group(board.sessions, lambda s: s.workdir.rstrip("/")).items()
        if len(g) > 1
        for s in g
    }
    candidates = [s for s in board.sessions if s.workdir.rstrip("/") not in shared_trees]
    out = []
    for branch, group in _group(candidates, lambda s: s.branch).items():
        if len(group) < 2:
            continue
        out.append(Finding(
            code="shared-branch",
            severity="warn",
            summary=f"{len(group)} sessions are committing to `{branch}`",
            detail=(
                f"{', '.join(_labels(group))} are on `{branch}` from different worktrees. "
                "Commit authorship will not be recoverable afterwards, and a rebase or a "
                "merge by one of them rewrites history the others are building on."
            ),
            sessions=_labels(group),
            workdirs=tuple(sorted(s.workdir.rstrip("/") for s in group)),
            remedy="Give each session its own branch, or name one owner for this one.",
        ))
    return out


def detect_unworktreed(board: Board) -> list[Finding]:
    """A session in the repo's main checkout while others are live here.

    Alone, this is harmless and says nothing -- the rule exists to prevent collisions, not for
    its own sake. With company it is the shared-tree hazard waiting for the next arrival.
    """
    if len(board.sessions) < 2 or not _is_git(board) or _shared_on_purpose(board):
        return []
    exposed = [s for s in board.sessions if s.in_main_checkout]
    if not exposed:
        return []
    shared = {
        s.workdir.rstrip("/")
        for _, g in _group(board.sessions, lambda s: s.workdir.rstrip("/")).items()
        if len(g) > 1
        for s in g
    }
    exposed = [s for s in exposed if s.workdir.rstrip("/") not in shared]
    if not exposed:
        return []
    return [Finding(
        code="unworktreed",
        severity="warn",
        summary=f"{', '.join(_labels(exposed))} is working in the main checkout",
        detail=(
            f"{len(board.sessions)} sessions are live in {board.repo_root}. A session in the "
            "main checkout shares its files with the next one to arrive, and a branch switch "
            "there yanks the floor out from under any session parked in it."
        ),
        sessions=_labels(exposed),
        workdirs=tuple(sorted(s.workdir.rstrip("/") for s in exposed)),
        remedy=(
            "`git worktree add <repo>/.worktrees/<topic> -b <topic> origin/main`, then "
            "EnterWorktree into it. No restart and no lost context."
        ),
    )]


def detect_claim_overlap(board: Board) -> list[Finding]:
    """Two sessions that have declared an interest in the same file.

    2026-09-17: `.github/workflows/ci.yml`'s gate list is a single hand-edited line every
    workstream appends to, and `AGENTS.md` states a count that any addition invalidates.
    Three of four sessions touched them and they broke twice in a day. Worse, `AGENTS.md:107`
    read identically on both sides of the merge, so git produced no conflict and the file was
    silently wrong -- a conflict you must resolve is the safe case.
    """
    out = []
    seen: set[tuple[str, str, str]] = set()
    sessions = list(board.sessions)
    for i, a in enumerate(sessions):
        for b in sessions[i + 1:]:
            hits = sorted({
                _norm_claim(ca)
                for ca in a.claims for cb in b.claims if _claims_overlap(ca, cb)
            })
            if not hits:
                continue
            key = (a.session_id, b.session_id, ",".join(hits))
            if key in seen:
                continue
            seen.add(key)
            out.append(Finding(
                code="claim-overlap",
                severity="warn",
                summary=f"{a.label} and {b.label} both claim {', '.join(hits)}",
                detail=(
                    f"Declared claims intersect at: {', '.join(hits)}. Nothing will warn "
                    "you if both edits merge cleanly while contradicting each other -- a stated "
                    "count, a figure, a hand-maintained list."
                ),
                sessions=_labels([a, b]),
                workdirs=tuple(sorted({a.workdir.rstrip("/"), b.workdir.rstrip("/")})),
                remedy=(
                    "Agree an owner for the file, or make the contested thing data rather "
                    "than a hand-edited sequence so the collision stops existing."
                ),
            ))
    return out


def detect_no_conductor(board: Board) -> list[Finding]:
    """Several sessions live and nobody sequencing them.

    2026-09-17: three sessions each hit the same wall, each correctly refused to guess, and
    each escalated to Daniel -- who was the only channel between them. One of them asked him
    for permission to deviate from a rule he had set an hour earlier: the right instinct and
    the wrong recipient. The missing piece was a role, not better behaviour, and no amount of
    session discipline substitutes for it.
    """
    if len(board.sessions) < 2 or board.conductor is not None:
        return []
    return [Finding(
        code="no-conductor",
        severity="note",
        summary=f"{len(board.sessions)} sessions here and none is sequencing",
        detail=(
            f"With nobody holding the role, every ordering question routes to {user()}, who "
            "becomes the only channel between sessions that cannot see each other."
        ),
        sessions=_labels(board.sessions),
        remedy="`session-coord conductor claim` in whichever session has the widest view.",
    )]


def detect_stale_default(board: Board) -> list[Finding]:
    """The local default branch is behind its remote.

    A worktree cut from a stale local `main` starts from another session's in-flight work
    rather than from what is released. Silent when git could not tell us -- unknown is not a
    finding, and crying wolf here trains him to skim the whole board.
    """
    facts = board.facts
    if facts is None or facts.default_branch_in_sync:
        return []
    return [Finding(
        code="stale-default",
        severity="note",
        summary=f"local `{facts.default_branch}` is not what the remote has",
        detail=(
            f"local {facts.local_default_sha[:7]} vs remote {facts.remote_default_sha[:7]}. A "
            "worktree cut from here starts from the wrong base."
        ),
        remedy=f"`git fetch origin` and branch from `origin/{facts.default_branch}`.",
    )]


DETECTORS = (
    detect_shared_tree,
    detect_shared_branch,
    detect_unworktreed,
    detect_claim_overlap,
    detect_no_conductor,
    detect_stale_default,
)


def check(board: Board) -> list[Finding]:
    """Every finding for this board, worst first. Empty when there is nothing to say."""
    found: list[Finding] = []
    for d in DETECTORS:
        found.extend(d(board))
    return order_findings(found)
