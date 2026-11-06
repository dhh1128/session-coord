"""argparse wiring over the rest of the package. The surface hooks and the skill call.

Two contracts worth stating because callers depend on them.

Exit codes: 0 clean, 1 warnings, 2 a blocking finding, and the PreToolUse gate turns a 2 into
a refused tool call. Anything else is the CLI itself failing.

Silence: `--quiet` must print nothing at all when there is nothing to say. One session alone in
a repo is almost every session, and a tool that speaks in the common case gets muted -- after
which it protects nobody. `repo-status -q` already works this way, for the same reason.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone

from . import asks as asks_mod
from . import comms, detect, gitio, harvest, locks, procio, remote, store, tmuxio
from . import inbox as inbox_mod
from .model import Ask, Board, CONDUCTOR, Presence


# First line of `inbox --ids`, so a caller takes bodies and ids from ONE snapshot and strips
# the machine-readable part before showing the rest to a reader.
IDS_PREFIX = "#ids "


from .who import user as _user


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_repo(args) -> str | None:
    if getattr(args, "repo", None):
        # Through gitio.repo_root, NOT taken literally. `--repo "$PWD"` from inside a worktree
        # otherwise keys state on the worktree, so the session registers on a private board of
        # one, sees no peers, and is invisible to them -- silent isolation reached by using the
        # tool as documented. Found by the tagged session, in its own code first.
        explicit = os.path.realpath(os.path.expanduser(args.repo))
        return gitio.workspace_root(explicit)
    # --repo-of names a PATH whose repo we want, which is not always the one containing cwd:
    # `git -C /other/repo add -A` issued from here writes to a different repository's board,
    # and asking this one answered about the wrong tree -- or exited clean, which reads as safe.
    target = getattr(args, "repo_of", None)
    if target:
        return gitio.workspace_root(os.path.expanduser(target))
    # `register --cwd` names where the SESSION is, which is not always where this process runs.
    # A hook's working directory is the harness's choice, so the session's own directory is the
    # one to key on when it was given.
    base = getattr(args, "cwd", None)
    return gitio.workspace_root(os.path.expanduser(base) if base else os.getcwd())


def _live_board(repo: str) -> Board:
    """Sweep, then build the board from what is left. The sweep is why nothing needs tidying.

    Under the presence lock, because the sweep MUTATES: a `board` or `check` running
    concurrently with `conductor claim` could delete a record in the middle of that claim's
    read-modify-save and have it written straight back. Read-only on contention rather than
    failing -- answering from a board that is one sweep stale is much better than not
    answering, since every caller here is trying to find out who else is present.
    """
    try:
        with locks.hold(repo, "presence", wait=0.5, owner="board"):
            # Snapshot INSIDE the lock. Taken outside, a session registering between the
            # snapshot and the lock is absent from the map and gets swept -- deleting a live
            # session at the moment it starts, which is the worst possible time.
            store.sweep(repo, tmuxio.claude_pids(), tmuxio.server_pid())
    except locks.Busy:
        pass
    return Board.of(repo, _live(store.load_all(repo)), facts=gitio.facts(repo))


def _live_table():
    """The tmux pane table `_live` reads. Its own function so the test suite can replace it."""
    return tmuxio.panes()


_READ = object()  # "read tmux now"; None is an answer meaning tmux could not be read


def _live(records, table=_READ):
    """Records with `window` and `title` read from tmux NOW, not from when they registered.

    Windows renumber when Daniel closes one (`renumber-windows on`, which he keeps because
    double-digit windows are awkward), so a stored window is a guess about the past. On
    2026-10-10 a session in infra:6 introduced itself as infra:7, and another asked after a
    response "from infra:7" while running in it. Anything shown to a session or to Daniel goes
    through here. A pane holding a different claude than the record's is left as recorded:
    that pane is somebody else now, and the sweep is what retires the record.
    """
    # An explicit None is a failed read already made: reading again per repo is what made one
    # `msg` wait out a stalled tmux once for every repo in the estate (hostile pass).
    table = _live_table() if table is _READ else table
    # None is "could not ask tmux", and the stored values are the best there is. An EMPTY table
    # is an answer -- no panes at all -- and every record in it is stale (the fix-diff pass on
    # PR #35, finding 3).
    if table is None:
        return list(records)
    out = []
    for p in records:
        if not p.pane:
            # No pane, so tmux has nothing to say about it: a session outside tmux keeps the
            # name it registered with, and has no window to go stale.
            out.append(p)
            continue
        info = table.get(p.pane or "")
        if info and info["claude_pid"] is tmuxio.UNKNOWN_PID:
            out.append(p)
            continue
        if info and (p.claude_pid is None or info["claude_pid"] == p.claude_pid):
            live_title = info.get("title") or p.title
            p = p.with_(window=info["window"], title=live_title,
                        registered_name=p.title if p.named and p.title != live_title else "")
        else:
            # Its pane is gone or holds someone else, so its stored window is a number another
            # session may hold now. Kept, `msg <window>` matched it and filed into the dead
            # session's inbox (the hostile pass on PR #35, finding 2).
            # The title goes too: kept, `msg <old title>` still filed into the dead inbox
            # (finding 2). The session-id prefix is what names it now.
            # And the pane: kept, `msg %2` still matched the dead record and filed into its inbox
            # (the third pass on PR #35). A pane id addressed directly now reaches whoever
            # is in that pane, by the unregistered path.
            p = p.with_(window=None, title="", pane=None, registered_name="")
        out.append(p)
    return out


def _render_board(board: Board) -> str:
    lines = [f"{len(board.sessions)} sessions in {board.repo_root}"]
    for s in board.sessions:
        role = "  [conductor]" if s.role == CONDUCTOR else ""
        where = s.workdir
        if where.startswith(board.repo_root):
            where = "." + where[len(board.repo_root):] or "."
        git = board.facts is None or board.facts.is_git
        branch = f"  ({s.branch or 'detached'})" if git else ""
        lines.append(f"  {s.label:<14} {where}{branch}{role}")
        lines.append(f"      address as: {s.name}")
        if s.charter:
            lines.append(f"      {s.charter}")
        if s.claims:
            lines.append(f"      claims: {', '.join(s.claims)}")
    return "\n".join(lines) + "\n"


def _render_findings(findings) -> str:
    out = []
    for f in findings:
        out.append(f"[{f.severity}] {f.code}: {f.summary}")
        if f.detail:
            out.append(f"    {f.detail}")
        if f.remedy:
            out.append(f"    -> {f.remedy}")
    return "\n".join(out) + "\n" if out else ""


def _rc(findings) -> int:
    if any(f.severity == "block" for f in findings):
        return 2
    return 1 if findings else 0


# ---- commands ------------------------------------------------------------------------

def cmd_register(args) -> int:
    if not args.session:
        print("session-coord: no session id. Hooks pass --session; by hand, this session must "
              "already be registered (the SessionStart hook does it).", file=sys.stderr)
        return 2
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo; nothing registered.", file=sys.stderr)
        return 0
    # Validate BEFORE anything destructive. `store.save` validates too, but by then the
    # superseded record has already been unlinked, so a malformed id deleted a live session's
    # registration and then reported an error -- leaving the pane unregistered and invisible,
    # which is the one state this tool must never produce.
    try:
        store.check_name(args.session, "session id")
    except store.UnsafeName as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2

    # The whole presence lifecycle runs under the same lock `conductor claim` takes. Without
    # it, a SessionStart that loaded role=None before a claim could save afterwards and erase
    # the new conductor -- leaving `conductor claim`'s success message false, which is worse
    # than the claim simply failing. A /clear races the same way against its own supersede.
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner=f"register {args.session[:8]}"):
            return _register_locked(args, repo)
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75


def _title_of(pane: str | None) -> str:
    table = tmuxio.panes() if pane else None
    return ((table or {}).get(pane or "") or {}).get("title", "")


def _pid_started(pid: int | None, inherited: Presence | None) -> str:
    """The start fingerprint for the pid being recorded, kept only while it is the same pid."""
    if pid is None:
        return inherited.pid_started if inherited else ""
    if inherited and inherited.claude_pid == pid and inherited.pid_started:
        return inherited.pid_started
    return procio.start_of(pid) or ""


def _register_locked(args, repo: str) -> int:
    server = tmuxio.server_pid()
    store.sweep(repo, tmuxio.claude_pids(), server)
    existing = {p.session_id: p for p in store.load_all(repo)}
    prior = existing.get(args.session)
    # Explicit argument, then what this session was already recorded in, then the environment.
    # Prior beats the environment because a re-register from an ordinary CLI call (the
    # documented charter/claim update) inherits whatever pane that shell happens to sit in,
    # which would silently move the record to a pane the session is not in and make the next
    # sweep delete it.
    pane = args.pane or (prior.pane if prior else None) or os.environ.get("TMUX_PANE")

    # A /clear mints a NEW session id while the same claude process stays in the same pane.
    # Without this, the old record is still genuinely live by the liveness predicate, the new
    # one is added beside it, and one conversation shows up as two sessions in one working
    # tree -- which the shared-tree detector then reports as a blocking collision with itself.
    # Inherit the departing record rather than merely deleting it, so a charter and claims
    # survive the clear that Daniel did not intend to reset.
    superseded = [
        p for p in existing.values()
        if p.session_id != args.session and p.pane == pane and pane
        and p.claude_pid is not None and p.claude_pid == args.pid
    ]
    for old in superseded:
        # NO MAIL REBIND HERE, and its absence is deliberate rather than an oversight.
        #
        # A /clear does strand unread mail: it is filed against the departed session id, so
        # `inbox` never returns it. That is a real gap, recorded on PR #11 as `P-R1Z5`, and it
        # predates the delivery hook.
        #
        # A rebind was written here and removed. It could not fail safely. Registration must
        # never fail over mail -- an unregistered pane is invisible to every peer, which is
        # strictly worse -- so a failed rebind fell through to this `remove` and orphaned the
        # mail it existed to save. And resolving the recipient before taking the inbox lock let
        # a concurrent /clear file a message under an id this loop had just deleted. Two
        # defects, both inside the fix, both found in the round after it landed.
        #
        # Carrying mail across an id change needs a decision, not a patch: either the old
        # record survives until the mail has moved, or registration retries, or `inbox`
        # resolves superseded ids at read time. Leaving the gap is worse than nothing only if
        # you believe a rebind that silently loses mail is better than one that visibly never
        # ran. See A-W552.
        store.remove(repo, old.session_id)
    inherited = prior or (superseded[0] if superseded else None)

    detected_engine, detected_pid = None, None
    if not inherited and (args.pid is None or not getattr(args, "engine", None)):
        detected_pid = procio.claude_ancestor()
        if detected_pid is not None:
            detected_engine = "claude"
        else:
            detected_pid = procio.codex_ancestor()
            if detected_pid is not None:
                detected_engine = "codex"
    engine = (getattr(args, "engine", None) or
              (inherited.engine if inherited else detected_engine) or "claude")
    session_pid = (args.pid if args.pid is not None else
                   (inherited.claude_pid if inherited else
                    (detected_pid if detected_engine == engine else
                     (procio.codex_ancestor() if engine == "codex"
                      else procio.claude_ancestor()))))

    cwd = os.path.realpath(os.path.expanduser(args.cwd or os.getcwd()))
    p = Presence(
        session_id=args.session,
        repo_root=repo,
        workdir=gitio.workdir(cwd),
        branch=gitio.branch(cwd),
        pane=pane,
        window=args.window or tmuxio.window_of(pane or "") or (
            inherited.window if inherited else None),
        # A name already on record outranks the pane's title: Claude Code retitles a pane as the
        # conversation goes, and a re-register without --name (a /clear, a resume, a charter
        # update) otherwise replaced the --name peers address it by (hostile pass, finding 3).
        title=(getattr(args, "name", None)
               or (inherited.title if inherited and inherited.named else "")
               or _title_of(pane) or (inherited.title if inherited else "")),
        named=bool(getattr(args, "name", None)) or bool(inherited and inherited.named),
        # A re-register without --pid must not blank the pid. The documented charter/claim
        # update in SKILL.md omits it, so this path is the common one, and a null pid makes
        # the next sweep treat a running session as dead.
        claude_pid=session_pid,
        engine=engine,
        charter=args.charter or (inherited.charter if inherited else ""),
        claims=tuple(args.claim) if args.claim else (inherited.claims if inherited else ()),
        role=inherited.role if inherited else None,
        started=inherited.started if inherited else _now(),
        updated=_now(),
        transcript=args.transcript or (inherited.transcript if inherited else ""),
        tmux_server=(server or "") if pane else "",
        pid_started=_pid_started(session_pid, inherited),
    )
    try:
        store.save(p)
    except store.UnsafeName as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    # Registration is a snapshot. If the recorded directory is not the tree this command is
    # actually running in, the board is confidently wrong -- and the gate acts on the board, so
    # a session that moved with `cd` gets refused while believing it complied. Say so here, at
    # the only moment we can see both values at once.
    live_tree = gitio.workdir(os.getcwd())
    if live_tree and live_tree.rstrip("/") != p.workdir.rstrip("/"):
        print(
            f"session-coord: recorded as working in {p.workdir}, but this command is running in "
            f"{live_tree}.\n"
            "  The board -- and the PreToolUse gate that reads it -- will treat you as being in "
            "the recorded one.\n"
            "  If you moved with `cd`, that is why: use the EnterWorktree tool instead, which "
            "moves the session itself, then re-register.",
            file=sys.stderr)

    board = Board.of(repo, _live(store.load_all(repo)), facts=gitio.facts(repo))
    findings = detect.check(board)
    if len(board.sessions) > 1 or findings:
        sys.stdout.write(_render_board(board))
        sys.stdout.write(_render_findings(findings))
    return 0


def _estate_view() -> dict:
    """Every live session on this box, in the shape a peer box reads over ssh.

    Read-only: no sweep, since a peer asking must not mutate this box's records, and a
    stale record costs one extra line on someone else's board.
    """
    table = _live_table()
    out = []
    for repo in store.known_repos():
        origin = remote.origin_of(repo)
        for p in _live(store.load_all(repo), table):
            out.append({"name": p.name, "label": p.label, "repo_root": p.repo_root,
                        "workdir": p.workdir, "branch": p.branch, "charter": p.charter,
                        "claims": list(p.claims), "role": p.role, "origin": origin,
                        "engine": p.engine})
    # Worktrees too, because a session's registration and its work can be in different
    # repos. On 2026-11-06 a kelun session registered in devenv was working in
    # session-coord/.worktrees/codex-appserver, so a board matched on origin missed it; what
    # found it was listing that repo's worktrees by hand.
    trees = []
    # Not only repos with a registrant: the case this exists for is a session registered in
    # one repo working in another, which may have no registrant at all (Copilot on PR #12).
    roots = set(store.known_repos())
    for s in out:
        root = gitio.repo_root(s["workdir"]) if s["workdir"] else None
        if root:
            roots.add(root)
    for repo in sorted(roots):
        origin = remote.origin_of(repo)
        for path, branch in gitio.worktrees(repo):
            if os.path.realpath(path) != os.path.realpath(repo):
                trees.append({"repo_root": repo, "origin": origin, "path": path,
                              "branch": branch})
    return {"host": remote.local_host(), "sessions": out, "worktrees": trees}


def cmd_board(args) -> int:
    if getattr(args, "estate", False):
        # What a peer box fetches. The only output is the JSON, so a remote parse never has
        # to step around a warning.
        print(json.dumps(_estate_view()))
        return 0
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    board = _live_board(repo)
    if args.json:
        print(json.dumps([json.loads(s.to_json()) for s in board.sessions], indent=2))
        return 0
    hosts = remote.load_hosts() if getattr(args, "hosts", False) else []
    if args.quiet and len(board.sessions) < 2 and not hosts:
        return 0
    sys.stdout.write(_render_board(board))
    if getattr(args, "hosts", False):
        if not hosts:
            print(f"on other boxes: none configured (list them, one per line, in "
                  f"{remote.hosts_path()})")
            return 0
        views, note = remote.fetch(hosts)
        sys.stdout.write(remote.render(views, remote.origin_of(repo), note,
                                       show_all=getattr(args, "all", False)))
    return 0


def cmd_claim(args) -> int:
    """Add to this session's claims without restating them.

    `register --claim` REPLACES the list, which is right for a charter and wrong for "I am
    about to write one more file": a session following a refusal's advice would silently drop
    every claim it already held. This appends, de-duplicated, under the presence lock.
    """
    blank = [c for c in args.paths if not c.strip()]
    if blank:
        print("session-coord: refusing an empty path. A blank claim usually means an unset "
              "shell variable; name the files literally. Nothing was claimed.", file=sys.stderr)
        return 2
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo; nothing claimed.", file=sys.stderr)
        return 2
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner="claim"):
            records = _live(store.load_all(repo))
            me = (next((p for p in records if p.session_id == args.session), None)
                  if args.session else _me(repo))
            if me is None:
                print("session-coord: this session has no record here, so there is nothing to "
                      "add claims to. It registers at session start; if it did not, run "
                      "`session-coord register --name <name> --charter '...' --claim <path>`.",
                      file=sys.stderr)
                return 2
            claims = list(me.claims)
            added = [c for c in args.paths if c not in claims]
            claims += [c for i, c in enumerate(added) if c not in added[:i]]
            store.save(replace(me, claims=tuple(claims), updated=_now()))
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    print(f"claimed: {' '.join(added) if added else '(nothing new)'}; "
          f"this session now claims {len(claims)} path(s)")
    return 0


def cmd_check(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    board = _live_board(repo)
    findings = detect.check(board)
    if args.workdir:
        # Scope to the caller's own tree. The PreToolUse gate asks this question on behalf of
        # ONE session, and repo-wide findings would refuse a session sitting safely in its own
        # worktree because two OTHER sessions share the main checkout -- denying exactly the
        # sessions that followed the rule. A finding with no `workdirs` is repo-wide and still
        # applies to everyone.
        # Through gitio.workdir, because registration stores the worktree ROOT while a hook's
        # cwd can be any subdirectory of it. Comparing the raw path found no match, dropped the
        # shared-tree blocker, and allowed the staging command -- fail-open, again.
        here = gitio.workdir(os.path.expanduser(args.workdir)).rstrip("/")
        findings = [f for f in findings if not f.workdirs or here in f.workdirs]
        if args.path:
            # The gate's question for an explicit `git add`/`git commit` in a shared tree:
            # does any PEER in this tree claim one of these files? Paths arrive relative to
            # the tree root. 2026-11-02, the docs-repo incident.
            findings = findings + detect.claimed_paths(
                board, here, args.session or "", args.path)
            if args.require_claim:
                # In a repo that shares its checkout on purpose, the asking session must
                # claim what it commits (2026-11-05, R-AS3C).
                findings = findings + detect.unclaimed_paths(
                    board, here, args.session or "", args.path)
    if args.json:
        print(json.dumps([{"code": f.code, "severity": f.severity, "summary": f.summary,
                           "detail": f.detail, "remedy": f.remedy,
                           "sessions": list(f.sessions),
                           "workdirs": list(f.workdirs)} for f in findings], indent=2))
        return _rc(findings)
    rc = _rc(findings)
    # `--quiet` says "only blocking findings", so filter what is RENDERED while keeping the
    # full list for the exit code. Printing a block's accompanying warnings under --quiet broke
    # the option's own contract, and the gate is the caller that relies on it being terse.
    shown = [f for f in findings if f.severity == "block"] if args.quiet else findings
    if shown:
        sys.stdout.write(_render_findings(shown))
    # Rides on `check` rather than being its own command, because this is what a session
    # already runs when orienting. Never under --quiet: that is the PreToolUse gate's contract
    # for "blocking findings only", and anything else there is noise on every tool call.
    if not args.quiet:
        who = _me(repo)
        if who:
            # `check` is the orientation command and a PreToolUse dependency. It must survive a
            # damaged budget log: raising here turned an unreadable file into a traceback from
            # the one command a session runs to find out what is wrong.
            try:
                log = store.load_sends(repo)
            except store.UnreadableSends as e:
                print(f"comms budget: unavailable -- {e}")
                log = None
            if log is not None:
                line = comms.budget_line(log, who.session_id, time.time())
                if line:
                    print(line)
    return rc


def cmd_deregister(args) -> int:
    """Remove one session's record, by id. What SessionEnd calls; a sweep cannot do it."""
    repo = _resolve_repo(args)
    if repo is None:
        return 0
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner="deregister"):
            if store.remove(repo, args.session):
                print(f"deregistered {args.session}")
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    except store.UnsafeName as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_sweep(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        return 0
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner="sweep"):
            dropped = store.sweep(repo, tmuxio.claude_pids(), tmuxio.server_pid())
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    for p in dropped:
        print(f"dropped {p.session_id} ({p.label}) -- session is gone")
    return 0


def cmd_conductor(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    if args.action == "show":
        holder = next((p for p in _live(store.load_all(repo)) if p.role == CONDUCTOR), None)
        print(f"conductor: {holder.label}" if holder
              else "conductor: nobody holds it. Claim it if you have the widest view here.")
        return 0
    if args.action == "claim" and not args.session:
        print("session-coord: `conductor claim` needs --session.", file=sys.stderr)
        return 2
    # The role is one snapshot followed by several writes, so it needs the same treatment as
    # the ask queue: two sessions could each read "no conductor", each set itself, and both
    # records end up marked -- after which `Board.conductor` silently picks one and peers
    # route the same question to different arbiters. Two conductors is worse than none.
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner=f"conductor {args.action}"):
            records = store.load_all(repo)
            if args.action == "release":
                for p in records:
                    if p.role == CONDUCTOR:
                        store.save(p.with_(role=None, updated=_now()))
                print("conductor: released.")
                return 0
            if not any(p.session_id == args.session for p in records):
                print(f"session-coord: {args.session} is not registered here.", file=sys.stderr)
                return 2
            for p in records:
                want = CONDUCTOR if p.session_id == args.session else None
                if p.role != want:
                    store.save(p.with_(role=want, updated=_now()))
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    print(f"conductor: {args.session}. Peers route sequencing questions here, not to {_user()}.")
    return 0


def cmd_ask(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    # argparse cannot express "required for these actions only", so validate here rather than
    # letting a missing value reach .strip() on None and surface as a traceback.
    missing = {
        "add": [n for n, v in (("--tag", args.tag), ("--kind", args.kind),
                               ("--text", args.text)) if not v],
        "answer": [n for n, v in (("--tag", args.tag),) if not v],
        "dissolve": [n for n, v in (("--tag", args.tag),) if not v],
        "list": [],
    }[args.action]
    if missing:
        print(f"session-coord: `ask {args.action}` needs {', '.join(missing)}.", file=sys.stderr)
        return 2

    if args.action == "list":  # read-only; no lock, so a list never waits on a writer
        try:
            sys.stdout.write(asks_mod.render(store.load_asks(repo)))
        except store.UnreadableQueue as e:
            print(f"session-coord: {e}", file=sys.stderr)
            return 2
        return 0
    try:
        # The whole load/transform/save under one lock. An atomic rename stops a torn read; it
        # does nothing about two sessions both loading the same list, each appending, and the
        # second save dropping the first one's ask. Both PR #3 reviewers found that.
        with locks.hold(repo, "asks", wait=args.wait, owner=f"ask {args.action}"):
            # Refuse rather than overwrite. The whole point of UnreadableQueue is that this
            # transaction would otherwise save a fresh list over a file it failed to parse.
            queue = store.load_asks(repo)
            if args.action == "add":
                ask = Ask(tag=args.tag, kind=args.kind, text=args.text,
                          session_id=args.session or "", window=args.window or "",
                          created=_now(), depends_on=tuple(args.depends_on or ()))
                store.save_asks(repo, asks_mod.add(queue, ask))
                print(f"filed {args.tag}")
            elif args.action == "answer":
                store.save_asks(repo, asks_mod.answer(queue, args.tag, args.text or ""))
                print(f"answered {args.tag}")
            else:
                store.save_asks(repo, asks_mod.dissolve_resolved(queue, [args.tag]))
                print(f"dissolved {args.tag}")
    except store.UnreadableQueue as e:
        print(f"session-coord: {e}\nRefusing to mutate it -- a write here would replace the "
              "asks it could not read. Repair or delete the file, then retry.", file=sys.stderr)
        return 2
    except asks_mod.AskError as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    return 0



class _Doorbell:
    """Reserve a doorbell, ring it, and give it back if the ring does not happen.

    ONE GATE FOR BOTH SEND PATHS, and the reservation, the decision and the ring all sit inside
    a single estate-wide lock. Three separate defects made this a class rather than two inline
    blocks:

    * The check and the ring were not atomic, so two `msg` calls from one pane could each see
      fewer than three sends and both ring. `O_APPEND` stops log lines being lost; it does not
      make read-then-decide-then-act atomic.
    * The budget lived only in the registered-recipient branch, so a registered sender
      addressing an unregistered target or a bare pane typed directly with no accounting at
      all -- a complete bypass of the limit by choosing a different recipient.
    * The lock has to be estate-wide, because the budget is. A per-repo lock would fail to
      serialise the one pair that needs it: two sends from the same session in different repos.
    """

    def __init__(self, repo, sender_id, target, reply, override):
        self.repo = repo
        self.sender_id = sender_id
        self.target = target
        self.reply = reply
        self.override = override
        self.charge = None
        self.left = None

    def reserve(self):
        """Take the budget, or return a refusal string. None means go ahead."""
        if not self.sender_id:
            return None  # unattributable: not budgeted, per the documented exception
        now = time.time()
        try:
            with locks.hold(locks.ESTATE, "doorbell", wait=15.0,
                            owner=f"msg {self.sender_id[:8]}"):
                try:
                    log = store.load_sends(self.repo)
                    # A TORN TAIL MUST BE REPAIRED BEFORE APPENDING. `load_sends` tolerates an
                    # unterminated final record as a half-written append -- but appending after
                    # those bytes concatenates the two, so the ring recorded here is
                    # unparseable on the next read and the charge silently disappears. The
                    # terminating newline costs nothing and makes the tail a complete, skipped
                    # record rather than a splice point.
                    store.terminate_sends(self.repo)
                except store.UnreadableSends as e:
                    # Fail closed -- but an explicit --override still passes, because the whole
                    # point of the flag is to proceed when the tool cannot decide for you, and
                    # a storage fault is exactly that case.
                    if self.override:
                        log = None
                    else:
                        return (f"{e}\nThe doorbell budget cannot be checked, so it is assumed "
                                "spent. Repair or remove that file, or pass --override if this "
                                "must interrupt now.")
                if log is not None:
                    recent = comms.spent(log, self.sender_id, now)
                    if len(recent) >= comms.BUDGET_PER_HOUR and not self.override:
                        return comms.refusal(recent, now)
                self.charge = comms.Sent(session_id=self.sender_id, target=self.target, at=now,
                                         reply=self.reply, override=self.override,
                                         id=f"{now:.6f}-{os.getpid()}")
                store.record_send(self.repo, self.charge)
                try:
                    self.left = comms.remaining(store.load_sends(self.repo), self.sender_id, now)
                except store.UnreadableSends:
                    self.left = None
        except locks.Busy as e:
            return f"another send is holding the doorbell budget: {e}"
        except OSError as e:
            return (f"the doorbell budget could not be recorded ({e}), and ringing without "
                    "recording would remove the limit silently.")
        return None

    # NO give_back, DELIBERATELY, and this is a reduction rather than an omission.
    #
    # The budget used to be refunded when the ring was refused, so that an unreachable peer
    # cost nothing. That nicety produced three review findings in a row and each fix made it
    # more elaborate: charging after the ring left a window where an interruption was free;
    # charging before it and refunding needed an append-only reversal; and the refund itself
    # was wrong, because `SendRefused` covers BOTH "nothing was typed" and "the text was typed
    # but the submit keystroke failed" -- refunding the second leaves a doorbell sitting in the
    # peer's prompt, uncharged, which interrupts them the moment they submit.
    #
    # Charging on ATTEMPT removes all three at once. There is no window, no reversal, and no
    # outcome to disambiguate, and it is also the more honest price: the message is filed and
    # delivered either way, so what was actually spent is an attempt to interrupt.
    #
    # It costs a sender one doorbell an hour when a peer is unreachable. That is visible in the
    # log and self-correcting within the hour, which is the direction this module has learned
    # to fail in.

    def tail(self):
        if self.charge is None:
            return ""
        if self.left is None:
            return "  [budget unreadable; it was charged]"
        return f"  [{self.left} of {comms.BUDGET_PER_HOUR} doorbells left this hour]"


# A message is prose for a peer's context, not a payload. Bounded so a runaway pipe cannot
# make `msg peer -` hold an arbitrary amount in memory before any check runs.
MSG_STDIN_LIMIT = 256 * 1024


def _msg_remote(args, name: str, host) -> int:
    """`msg name@host`: hand the message to session-coord on that box, body on stdin.

    The remote side files and rings exactly as a local send would, so held mail, the inbox
    and the doorbell all behave the same; only the sender label says where it came from.
    """
    # The recipient answers by pasting what it was given, so the sender line has to carry an
    # address that still works when it does. Not the label, which carries "(now me:2)", and
    # not only the name: a title changes when the session is renamed and a window number when
    # another window closes. The session id lasts as long as the session, and `msg` resolves
    # it exactly on the far side. The name stays first because it is what a reader recognises.
    me = _me_anywhere(_resolve_repo(args))
    here = remote.local_host()
    if args.sender:
        sender = f"{args.sender}@{here}"
    elif me:
        sender = f"{me.name}@{here} (reply to {me.session_id}@{here})"
    else:
        sender = f"a peer@{here}"
    # The reply address goes across separately: over ssh the far box has no record of this
    # session, so without it the recipient is told to reply to nobody (Copilot on PR #12).
    reply_to = f"{me.session_id}@{here}" if me else ""
    argv = ["msg", name, "-", "--sender", sender, *(["--sender-name", reply_to] if reply_to else []),
            "--reply", getattr(args, "reply", comms.REPLY_NONE)]
    if args.subject:
        argv += ["--subject", args.subject]
    if getattr(args, "override", ""):
        argv += ["--override", args.override]
    # The pane-reuse guard and --force mean the same thing on the far box, where the pid lives.
    if args.pid is not None:
        argv += ["--pid", str(args.pid)]
    if args.force:
        argv += ["--force"]
    blocked = remote.sandboxed()
    if blocked:
        print(f"session-coord: nothing sent to {name}@{host.name}: {blocked}.", file=sys.stderr)
        return 2
    # The doorbell budget is charged HERE, where the sender is known. Over ssh the far box sees
    # only a display string and cannot attribute the send, so it would exempt every remote
    # message from the limit (hostile pass on PR #12). Over budget, the message is still filed
    # there, just not rung, which is the same rule as a local send.
    sender_id = me.session_id if me else ""
    bell = _Doorbell(_resolve_repo(args), sender_id, f"{name}@{host.name}",
                     getattr(args, "reply", comms.REPLY_NONE),
                     (getattr(args, "override", "") or "").strip())
    refused = bell.reserve()
    if refused:
        argv += ["--no-ring"]
        print(f"session-coord: filing for {name}@{host.name} without ringing: {refused}",
              file=sys.stderr)
    body = args.text.encode("utf-8")
    try:
        proc = remote._spawn(remote.remote_argv(host, *argv), stdin=body)
        out, err = proc.communicate(body, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        print(f"session-coord: {host.name} did not answer within 20s; the message may or may "
              "not have been filed there.", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"session-coord: could not start ssh to {host.name} ({e}); nothing sent.",
              file=sys.stderr)
        return 2
    sys.stdout.write(out.decode("utf-8", "replace"))
    text = err.decode("utf-8", "replace")
    if proc.returncode == 255:
        text = (f"session-coord: ssh to {host.name} failed, so nothing was sent. "
                f"{text.strip()}\n")
    sys.stderr.write(text)
    return proc.returncode


def cmd_msg(args) -> int:
    # `-` reads the body from stdin, as `zamu tell` does, so prose a command guard would
    # inspect need not appear in the command line. Before this, `msg <peer> - < file` sent a
    # body of exactly "-" and reported success (2026-11-05).
    if args.text == "-":
        try:
            raw = sys.stdin.buffer.read(MSG_STDIN_LIMIT + 1)
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError, AttributeError, ValueError) as e:
            print(f"session-coord: could not read the body from stdin ({e}); nothing sent.",
                  file=sys.stderr)
            return 2
        if len(raw) > MSG_STDIN_LIMIT:
            print(f"session-coord: the body on stdin is over {MSG_STDIN_LIMIT} bytes; nothing "
                  "sent. Put the detail in a file and send its path.", file=sys.stderr)
            return 2
        # Text-mode stdin used to turn CRLF into LF; the byte read must do it explicitly, or
        # validate refuses the CR as a control character.
        args.text = text.replace("\r\n", "\n").strip("\n")
        if not args.text.strip():
            print("session-coord: the body was `-` but stdin was empty; nothing sent.",
                  file=sys.stderr)
            return 2
    # `name@host` goes to that box, but only when `host` is in the hosts file: an `@` in an
    # ordinary pane title must keep meaning a local session.
    routed = remote.split_address(args.target, remote.load_hosts())
    if routed:
        return _msg_remote(args, *routed)
    # Look the target up on the board and pass its recorded pid. `send` documents expect_pid as
    # the guard against pane-id reuse, and the CLI was not using its own guard -- so a window
    # reassigned between the two resolutions would have been typed into as though it were the
    # intended peer. Falls back to no pid only when the target is not a registered session
    # here, which is the case where there is nothing better to check against.
    expect, target = args.pid, args.target
    recipient = None
    repo = _resolve_repo(args)
    recipient_repo = repo
    # The same key held mail uses, so a name means one thing whichever path it takes. With
    # `clean_title` here, "%9 " and "会議A" matched registered sessions titled "9" and "A" --
    # now that the search spans every repo, a wrong match files private mail across the estate.
    want = _held_key(args.target)
    # Two tiers. A live address (pane, window, id, current title) anywhere in the estate wins
    # over a registered name, so a local session's old --name cannot capture mail meant for a
    # peer that carries that title now (hostile pass on the alias change, finding 1).
    by_name = bool(want) and not want.startswith("%")
    primary = lambda p: (args.target in (p.pane, p.window, p.session_id)
                         or (by_name and _held_key(p.title) == want))
    alias = lambda p: by_name and bool(p.registered_name) and _held_key(p.registered_name) == want

    # One tmux read and one load per repo, shared by both tiers: a miss used to scan the estate
    # twice, querying tmux for every repo each time (hostile pass, finding 2).
    table = _live_table()
    cache: dict[str, list] = {}

    def live_in(r):
        if r not in cache:
            cache[r] = _live(store.load_all(r), table)
        return cache[r]

    def search(matches):
        local = [p for p in live_in(repo) if matches(p)]
        if local:
            return local, repo
        found = [(other, p) for other in store.known_repos() if other != repo
                 for p in live_in(other) if matches(p)]
        return [p for _, p in found], (found[0][0] if len(found) == 1 else repo)
    if repo:
        # EVERY match, not the first one. A window label can name two registered sessions -- a
        # split window is the ordinary way -- and taking the first meant the body was filed into
        # one of them arbitrarily before `send` performed its own ambiguity check. The send then
        # refused, correctly, and the message sat in a stranger's inbox: refused and delivered
        # to the wrong session at once. Resolve to exactly one before anything is written.
        # LIVE windows and titles (`_live`): a stored window names whoever held that number
        # when the record was written, which after a close is a different session.
        # The foreman-imbu incident lasted an hour because a peer in another repo was
        # mistaken for an unregistered target, then lost when the direct-send budget ran
        # out. Keep local resolution first, but search the estate before deciding no inbox
        # exists. The recipient's repo owns the queue, regardless of the sender's cwd.
        # Tier zero: an exact session id, across the whole estate, before any title is looked
        # at. A session id is what the cross-box sender line gives as the reply address, so it
        # has to mean that session even when some other session -- in this repo, which is
        # searched first -- carries a title equal to it (hostile pass on cross-host). A session
        # with records in several repos reads the inbox of the one it is in now, and the only
        # evidence of which that is, is a pane tmux shows right now. So: the one record whose
        # pane is in the live table, else the only record there is, else refuse below as
        # ambiguous. Not the sender's repo first: a dead record there took the mail (last
        # look on cross-host). Not a stored pane when tmux could not be read: `_live` leaves
        # it as recorded, so it proves nothing. Never a timestamp: a stale record can be newest.
        by_id = [(r, p) for r in [repo] + [o for o in store.known_repos() if o != repo]
                 for p in live_in(r) if p.session_id == args.target]
        if by_id:
            paned = [x for x in by_id
                     if table is not None and isinstance(x[1].pane, str) and x[1].pane in table]
            pick = (paned if len(paned) == 1 else []) or (by_id if len(by_id) == 1 else [])
            if pick:
                recipient_repo, hits = pick[0][0], [pick[0][1]]
            else:
                hits = [p for _, p in by_id]
        else:
            hits, recipient_repo = search(primary)
        if not hits:
            hits, recipient_repo = search(alias)
        if len(hits) > 1:
            where = ", ".join(sorted(f"{p.repo_root}: {p.session_id} ({p.pane})"
                                     for p in hits))
            print(f"session-coord: {args.target} names {len(hits)} registered sessions "
                  f"({where}), so it does not name one. Nothing was filed and nothing was "
                  "sent; address a pane id or a session id.", file=sys.stderr)
            return 2
        for p in hits:
            recipient = p
            # A record from a previous tmux server is not this peer, whatever its pid
            # says: a restart reuses %N ids and pids get recycled, so expect_pid alone
            # would pass and the message would go to whoever inherited the pane.
            here = tmuxio.server_pid()
            if here and p.tmux_server and p.tmux_server != here:
                print(f"session-coord: {args.target} is registered against tmux server "
                      f"{p.tmux_server}, but this box is running {here}. That record is "
                      "stale -- the pane id means nothing across a restart. Nothing sent.",
                      file=sys.stderr)
                return 2
            if expect is None:
                if p.claude_pid is None:
                    # A registered record with no pid cannot guard against pane reuse, and
                    # sending anyway contradicted the comment below. Refuse: a known peer
                    # deserves MORE care than an unknown one, not less.
                    print(f"session-coord: {args.target} is registered here but has no "
                          "recorded claude pid, so a pane-reuse check is impossible. "
                          "Pass --pid explicitly if you are sure.", file=sys.stderr)
                    return 2
                expect = p.claude_pid
            # A session id is advertised as a target but means nothing to tmux, so it has
            # to be translated here or `send` refuses every one of them.
            if args.target not in (p.window,) and p.pane:
                target = p.pane
    me = _me(repo)
    sender = args.sender or (me.label if me else "")

    # Resolved BEFORE filing, for two reasons Copilot found on PR #12.
    #
    # The marker has to be on the body that is STORED, not only on the one that is typed. It was
    # not: `compose` was written, tested and imported, and then never called on the registered-
    # peer path, which is the normal path -- so the recipient's inbox never carried
    # `[no reply needed]` and the opt-in contract was not enforced anywhere that mattered. A
    # unit test of a pure function cannot notice that nothing calls it.
    #
    # And identity is taken from the PANE, never from the argument. Trusting `--session` meant
    # the three-doorbell limit was bypassable by inventing an id: `--session fresh` got a clean
    # budget, and naming a peer spent theirs. An explicit value is now only accepted when it
    # agrees with the pane's own record.
    try:
        reply = comms.check_reply(getattr(args, "reply", comms.REPLY_NONE))
    except comms.CommsError as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    # The SENDER is resolved estate-wide, unlike `me` above, which is the target board's view
    # and is what names this session to the recipient. Two different questions: who is writing
    # (budget) and how they appear on the board the message lands on (attribution).
    sender_rec = _me_anywhere(repo)
    claimed = (getattr(args, "session", "") or "").strip()
    if claimed and sender_rec and claimed != sender_rec.session_id:
        print(f"session-coord: --session {claimed} is not this pane's session "
              f"({sender_rec.session_id}). The doorbell budget is per session, so it is taken "
              "from the pane rather than from an argument. Drop --session.", file=sys.stderr)
        return 2
    sender_id = sender_rec.session_id if sender_rec else ""
    override = (getattr(args, "override", "") or "").strip()

    # File the body in the recipient's inbox FIRST, then ring. The message is DELIVERED the
    # moment it is filed; the doorbell only shortens how long it sits unread. That ordering is
    # what turns a busy or unreachable peer into a delay rather than a failure -- before it,
    # three consecutive sends to one peer were refused and the thing that needed saying went
    # nowhere at all.
    if recipient is not None and recipient_repo:
        # Filed mail is read by a terminal exactly as typed mail is, so it is filtered exactly
        # as `tmuxio._clean` filters a direct send -- newlines and tabs allowed, because prose
        # is the point of the queue, and nothing else. Before this the queue was the way AROUND
        # the guard: the one path that never types is the one that never checked.
        try:
            inbox_mod.validate(args.text, args.subject or "")
        except inbox_mod.Unsafe as e:
            print(f"session-coord: {e}", file=sys.stderr)
            return 2
        try:
            with locks.hold(recipient_repo, "inbox", wait=15.0,
                            owner=f"msg from {sender or '?'}"):
                try:
                    queue = store.load_inbox(recipient_repo)
                except store.UnreadableQueue as e:
                    # Preserve, then proceed. Overwriting would destroy mail nobody has read;
                    # refusing would leave a peer unreachable behind a corrupt file nobody is
                    # looking at. Renaming is the only move that keeps both.
                    kept = store.quarantine_inbox(recipient_repo)
                    print(f"session-coord: {e}\nMoved it to {kept} and started a fresh inbox; "
                          "nothing in it was lost, but nobody has read it either.",
                          file=sys.stderr)
                    queue = []
                queue = list(queue) + [inbox_mod.Message(
                    id=f"{_now()}-{os.getpid()}-{len(queue)}",
                    sender=sender or "a peer", recipient=recipient.session_id,
                    sender_name=me.name if me else (args.sender_name or ""),
                    body=args.text, created=_now(), subject=args.subject or "",
                    reply=reply)]
                store.save_inbox(recipient_repo, queue)
                ring = inbox_mod.needs_doorbell(queue, recipient.session_id, _now())
        except locks.Busy as e:
            print(f"session-coord: {e}", file=sys.stderr)
            return 75
        if not ring:
            print(f"filed for {recipient.label}; it already has unread mail, so no doorbell "
                  "rung")
            return 0
        if getattr(args, "no_ring", False):
            print(f"filed for {recipient.label}, and not rung: the sender's doorbell budget "
                  "is spent")
            return 0
        if not recipient.pane:
            # Outside tmux there is no prompt to type a doorbell into. The message is delivered
            # all the same: the UserPromptSubmit hook reads the inbox at the start of the
            # recipient's next turn. What is lost is only the WAKING of a session that would
            # otherwise sit idle, and that has to be the human switching to it.
            print(f"filed for {recipient.label}; it is not in tmux, so it will read this at the "
                  "start of its next turn")
            return 0

        # THE BUDGET GATES THE DOORBELL, NEVER THE FILING. Filing costs the recipient nothing
        # until it looks; ringing makes it spend a turn reading and usually another replying.
        # So what is rationed is interrupting a peer -- a far narrower claim than telling one
        # something -- and an over-budget message is still delivered, just quietly.
        bell = _Doorbell(repo, sender_id, args.target, reply, override)
        refused = bell.reserve()
        if refused:
            print(f"filed for {recipient.label}, and NOT rung: {refused}", file=sys.stderr)
            return 0
        try:
            pane = tmuxio.send(target, inbox_mod.DOORBELL, expect_pid=expect, sender="",
                               force=args.force, spill_over=0)
        except tmuxio.SendUnknown as e:
            print(f"filed for {recipient.label}; doorbell outcome unknown: {e}",
                  file=sys.stderr)
            return 0
        except tmuxio.SendRefused as e:
            print(f"filed for {recipient.label}, but the doorbell could not be rung: {e}",
                  file=sys.stderr)
            return 0
        print(f"filed for {recipient.label} and rang ({pane}){bell.tail()}")
        return 0

    # An unregistered Claude may lack the hook, so successful direct typing remains its best
    # delivery. A failed send to a name can wait for a later registration. Pane and window
    # addresses can be recycled, so holding mail under either could give it to another process.
    # A successful direct send must not also be filed: that would put the same message in two turns.
    bell = _Doorbell(repo, sender_id, args.target, reply, override)
    refused = ("the sender's doorbell budget is spent" if getattr(args, "no_ring", False)
               else bell.reserve())
    if not refused:
        try:
            pane = tmuxio.send(target, comms.compose(args.text, reply), expect_pid=expect,
                               sender=sender, force=args.force)
        except tmuxio.SendUnknown as e:
            # Typing may have delivered this turn. Holding a second copy would risk duplication.
            print(f"session-coord: send outcome unknown; nothing was filed: {e}",
                  file=sys.stderr)
            return 2
        except tmuxio.SendRefused as e:
            refused = str(e)
        else:
            print(f"sent to {args.target} ({pane}) -- unregistered, so typed rather than "
                  f"filed{bell.tail()}")
            return 0
    # A refused send to a tmux address cannot safely become held mail. An empty name also
    # cannot be matched on a later turn, so report that nothing was filed in either case.
    key = _held_key(args.target)
    # Judged on the key, which keeps a `%`: "%9 ", "%%9" and "✳ %9" all slipped past checks
    # on the raw text and were held under "9", a name any session can take.
    if key.startswith("%") or re.fullmatch(r"\S+:\d+", key) or not key:
        print(f"session-coord: NOT sent, and nothing was filed -- {args.target!r} has no "
              f"safe name for held mail: {refused}", file=sys.stderr)
        return 2
    try:
        inbox_mod.validate(args.text, args.subject or "")
    except inbox_mod.Unsafe as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    try:
        with locks.hold(locks.ESTATE, "held", wait=15.0,
                        owner=f"msg from {sender or '?'}"):
            try:
                queue = store.load_held()
            except store.UnreadableQueue as e:
                kept = store.quarantine_held()
                if kept is None:
                    print(f"session-coord: {e}\nHeld mail could not be preserved; nothing "
                          "was filed.", file=sys.stderr)
                    return 2
                print(f"session-coord: {e}\nMoved it to {kept} and started fresh held mail; "
                      "nothing in it was lost, but nobody has read it either.",
                      file=sys.stderr)
                queue = []
            queue, _ = store.unexpired_held(queue)
            queue.append(inbox_mod.Message(
                id=f"held-{uuid.uuid4().hex}", sender=sender or "a peer", recipient=key,
                sender_name=me.name if me else (args.sender_name or ""), body=args.text,
                created=_now(),
                subject=args.subject or "", reply=reply))
            store.save_held(queue)
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    hours = int(store.HELD_TTL.total_seconds() // 3600)
    print(f"held for {args.target}; it will be delivered at the start of the next turn of "
          f"a session with that name, if one takes a turn within {hours} hours, and dropped "
          f"after that ({refused})")
    return 0


def addendum_path() -> Path:
    env = os.environ.get("SESSION_COORD_ADDENDUM")
    if env:
        return Path(os.path.expanduser(env))
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "session-coord" / "addendum.md"


def cmd_addendum(args) -> int:
    """Print the local addendum to the skill: house rules this machine adds to the generic ones.

    The seam that lets one published skill serve people with different surrounding tooling. The
    generic SKILL.md knows nothing about any particular task tracker, PR pipeline or naming
    convention; whoever installs it writes theirs into `~/.config/session-coord/addendum.md`
    (or names a file with `SESSION_COORD_ADDENDUM`), and the skill reads it on load. Silent when
    there is none, so the common case costs nothing.
    """
    path = addendum_path()
    try:
        text = path.read_text()
    except OSError:
        return 0
    if text.strip():
        sys.stdout.write(f"# Local rules from {path} -- these refine the generic skill\n\n")
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_lock(args) -> int:
    """Serialize an operation that mutates the repo's shared state.

    Exists for `pr-merge`: its merge-and-cleanup tail touches `.git` in the main checkout, and
    under `strict_required_status_checks` the first merge to land makes every other open PR
    stale. Wrapping only that tail -- never the CI wait -- serializes what races and leaves the
    parallelism that is worth having.
    """
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    if args.status:
        who = locks.holder(repo, args.name)
        print(f"{args.name}: held by {who}" if who else f"{args.name}: free")
        return 0
    if not args.command:
        print("session-coord: nothing to run; pass a command after `--`.", file=sys.stderr)
        return 2
    try:
        return locks.run(repo, args.name, args.command, wait=args.wait, owner=args.owner or "")
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75  # EX_TEMPFAIL: retryable, and distinguishable from the command's own codes


def _me(repo: str | None) -> Presence | None:
    """This session's own presence record, found by pane."""
    if not repo:
        return None
    pane = os.environ.get("TMUX_PANE")
    records = _live(store.load_all(repo))
    if pane:
        return next((p for p in records if p.pane == pane), None)
    return _mine_by_pid(records)


def _mine_by_pid(records) -> Presence | None:
    """Outside tmux, a session is the claude process this command runs under.

    Matched on pid AND start time, so a record left behind by a process whose pid has since
    been recycled cannot be mistaken for this session.
    """
    for pid in (procio.claude_ancestor(), procio.codex_ancestor()):
        if pid is None:
            continue
        started = procio.start_of(pid)
        found = next((p for p in records if not p.pane and p.claude_pid == pid
                      and started and p.pid_started == started), None)
        if found:
            return found
    return None


def _me_anywhere(repo: str | None) -> Presence | None:
    """This session's record, searched across the ESTATE rather than one repo's board.

    The doorbell budget is per session and spans repositories, so the identity behind it has to
    as well. `_me(repo)` looks only at the TARGET's board, so a session registered in repo A
    sending into repo B found no record, passed an empty id, and was treated as unattributable
    -- which skips accounting entirely. Repeating that across repos bypassed the whole limit,
    and it needed no flag: just a `--repo` pointing somewhere this pane is not registered.

    The local board is checked first because it is the common case and one read; the estate
    scan is the fallback, and it is bounded by the number of repos that have ever registered a
    session, not by anything a caller controls.
    """
    server = tmuxio.server_pid()

    def mine(p) -> bool:
        # A PANE ID MEANS NOTHING ACROSS A TMUX RESTART: %N ids are reused, so a record from a
        # previous incarnation can match this pane and absorb the charge -- billing a stale
        # session while the current one keeps its full budget. `cmd_msg` already refuses a
        # target on those grounds; the sender needed the same check and did not have it.
        #
        # An empty `tmux_server` is a record written before that field existed, and is accepted
        # for compatibility rather than treated as a mismatch.
        # An UNKNOWN server is not a wildcard. `not server` made every record with this pane
        # look like the sender when tmux could not be observed, so a charge landed on whoever
        # held the pane in a previous incarnation -- attributing somebody else's budget, which
        # is the opposite of the fail-closed rule this predicate exists to enforce. No server
        # means no identity: the send becomes unattributable and therefore unbudgeted, which is
        # the documented exception rather than a new one.
        #
        # An empty `tmux_server` on the RECORD is different and still accepted: those were
        # written before the field existed, and rejecting them would unbudget every
        # pre-upgrade session.
        if not server:
            return False
        return p.pane == pane and (not p.tmux_server or p.tmux_server == server)

    pane = os.environ.get("TMUX_PANE")
    here = _me(repo)
    if not pane:
        # Outside tmux, identity is the pid-and-start-time match `_me` already made; there is
        # no pane to be confused across a server restart, so `mine` has nothing to add.
        if here is not None:
            return here
        for other in store.known_repos():
            if repo and other == repo:
                continue
            found = _mine_by_pid(store.load_all(other))
            if found is not None:
                return found
        return None
    if here is not None and mine(here):
        return here
    for other in store.known_repos():
        if repo and other == repo:
            continue
        found = next((p for p in store.load_all(other) if mine(p)), None)
        if found is not None:
            return found
    return None


def _you(repo: str, session_id: str) -> str:
    """"you are X, now in window Y", read live, for the top of a delivered inbox."""
    rec = next((p for p in _live(store.load_all(repo)) if p.session_id == session_id), None)
    if rec is None:
        return ""
    if not rec.pane:
        return f"You are {rec.name}. Name sessions, yourself included, by name."
    where = f", now in window {rec.window}" if rec.window else ""
    return (f"You are {rec.name}{where} (pane {rec.pane}). Window numbers shift when "
            f"{_user()} closes a window, so name sessions, yourself included, by name.")


def _held_key(value: str) -> str:
    """Use the same deliverable name when holding mail and looking for its recipient."""
    # Not `tmuxio.clean_title`: its ASCII-only rule strips a whole non-Latin title, and strips
    # the non-Latin part of a mixed one, so "会議A" and "A" shared a key and one session could
    # read mail held for the other. Here only a leading run of characters that are not letters
    # or digits in any script is removed -- Claude's status glyph and its space -- so the name
    # itself survives intact. A `%` is kept, so a pane address stays recognisable as one.
    # `lower`, not `casefold`: casefold maps "Straße" to "strasse", merging two distinct names.
    i = 0
    while i < len(value) and not (value[i].isalnum() or value[i] == "%"):
        i += 1
    return value[i:].strip().lower()


def _held_names(p: Presence) -> set[str]:
    """Names by which held mail can find a session on a later turn."""
    return {name for name in (_held_key(p.title), _held_key(p.registered_name),
                              _held_key(p.session_id)) if name}


def cmd_inbox(args) -> int:
    """Drain this session's unread messages.

    What the doorbell tells a peer to run. Prints full bodies rather than a summary -- this IS
    the delivery, and a session that has to go and open a file has not received anything yet.
    """
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    me = _me(repo)
    who = args.session or (me.session_id if me else "")
    if not who:
        print("session-coord: cannot tell which session this is; pass --session.",
              file=sys.stderr)
        return 2
    try:
        held = store.load_held()
    except store.UnreadableQueue as e:
        # A damaged shared queue must not prevent this session from reading its own mail.
        # Only a writer can preserve that queue before replacing it.
        print(f"session-coord: {e}; held mail was left unchanged.", file=sys.stderr)
        held = []
    try:
        # Most turns have no held mail, so they need only this repo's inbox lock. When mail
        # exists, re-read it under the estate lock before deciding who receives it.
        held_lock = (locks.hold(locks.ESTATE, "held", wait=args.wait, owner="inbox")
                     if held else nullcontext())
        with held_lock:
            if held:
                try:
                    held = store.load_held()
                except store.UnreadableQueue as e:
                    print(f"session-coord: {e}; held mail was left unchanged.", file=sys.stderr)
                    held = []
            # Expire under the lock, and write the pruned queue back even when nothing below
            # is delivered: otherwise mail for a name nobody takes is re-read on every turn of
            # every session in the estate, forever.
            # Held mail is moved only if it can be removed afterwards. A message copied into
            # this inbox and left in held mail would be delivered again to the next session to
            # take the name, in any repo (hostile pass on #9). So the pruned queue is written
            # back here, under the lock, and that write doubles as the check: if it fails,
            # held mail stays held this turn and only this session's own inbox is read. A
            # failure here must never stand between a session and its own mail (hostile pass
            # at edcad05).
            held_writable = True
            if held:
                held, _ = store.unexpired_held(held)
                try:
                    store.save_held(held)
                except OSError as e:
                    held_writable = False
                    print(f"session-coord: held mail could not be updated, so it stays held "
                          f"until it can be: {e}", file=sys.stderr)
            # Saving the inbox first means a crash between the two writes can repeat a
            # message, but cannot lose it; the next read recognizes its id and finishes
            # removing it from held mail. Moving on peek leaves it unread for the hook's
            # peek-then-ack delivery.
            live = ([p for other in store.known_repos()
                     for p in _live(store.load_all(other))] if held else [])
            current = next((p for p in live if p.repo_root == repo and p.session_id == who),
                           None)
            mine_names = _held_names(current) if current else set()
            # One process can have records in several repos, but a resumed conversation can
            # have two live processes with the same session id. Count both parts of identity.
            counts = {name: len({(p.session_id, p.claude_pid) for p in live
                                 if name in _held_names(p)})
                      for name in mine_names}
            selected = [m for m in held if held_writable and m.recipient in mine_names
                        and counts[m.recipient] == 1]
            ambiguous = sorted({m.recipient for m in held if m.recipient in mine_names
                                and counts[m.recipient] > 1})
            with locks.hold(repo, "inbox", wait=args.wait, owner="inbox"):
                try:
                    queue = store.load_inbox(repo)
                except store.UnreadableQueue as e:
                    # A reader never repairs and never writes. Say what is wrong and where, so
                    # the recipient knows mail may be waiting rather than concluding it is empty.
                    print(f"session-coord: {e}\nYour mail may be in there; nothing was changed.",
                          file=sys.stderr)
                    return 2
                if selected:
                    already = {m.id for m in queue}
                    moved = [replace(m, recipient=who) for m in selected
                             if m.id not in already]
                    if moved:
                        queue = list(queue) + moved
                        store.save_inbox(repo, queue)
                    moved_ids = {m.id for m in selected}
                    # A held message must end up in exactly one place. If it cannot be removed
                    # from held mail, take back what this read just added to the inbox, so it
                    # stays held and is not delivered to this session now and to the next
                    # namesake later (hostile passes on #9). Raising here would withhold this
                    # session's own mail, so the failure is reported instead (hostile pass at
                    # 80c2779).
                    try:
                        store.save_held([m for m in held if m.id not in moved_ids])
                    except OSError as e:
                        # Every selected id, not only those this read added: a copy left by
                        # an earlier read whose restore failed is just as much in two places.
                        present = {m.id for m in queue} & moved_ids
                        queue = [m for m in queue if m.id not in moved_ids]
                        try:
                            if present:
                                store.save_inbox(repo, queue)
                            print(f"session-coord: held mail could not be updated, so it stays "
                                  f"held until it can be: {e}", file=sys.stderr)
                        except OSError as e2:
                            print(f"session-coord: held mail could not be updated ({e}), and "
                                  f"the inbox could not be restored ({e2}); a held message may "
                                  "reach a later session with the same name as well",
                                  file=sys.stderr)
                mine = inbox_mod.unread(queue, who)
                # `--ack` acknowledges exactly the messages that were delivered, by id, rather
                # than "whatever is unread now". The delivery hook reads without clearing, writes
                # the mail into the turn, and only then acknowledges -- and between those two
                # calls a peer can file something new. Clearing by recipient would mark that new
                # message read although it reached nobody: mail lost in the gap between two
                # processes, which is the same delivery-versus-bookkeeping failure one level out.
                if args.ack:
                    wanted = {i for spec in args.ack for i in spec.split(",") if i}
                    mine = [m for m in mine if m.id in wanted]
                    if mine:
                        store.save_inbox(repo, inbox_mod.mark_read(queue, [m.id for m in mine]))
                    if args.ids:
                        print("\n".join(m.id for m in mine))
                    return 0
                if args.ids:
                    # ONE SNAPSHOT, under the lock already held. An earlier version had the hook
                    # call this a second time to learn the ids of what it had just read, and those
                    # two peeks were separate processes: a message filed between them appeared in
                    # the id list but not in the bodies, so the acknowledgement marked it read
                    # although it had been delivered to nobody. That is the same defect the
                    # acknowledgement-by-id was introduced to fix, reintroduced by the fix.
                    #
                    # So the ids ride with the bodies, on a sentinel first line the caller strips.
                    # Two peeks cannot be made consistent by trying harder; there has to be one.
                    sys.stdout.write(IDS_PREFIX + " ".join(m.id for m in mine) + "\n")
                    sys.stdout.write(inbox_mod.render(mine, marked_read=False, you=_you(repo, who)))
                    if ambiguous:
                        sys.stdout.write("Held mail for " + ", ".join(ambiguous) +
                                         " is ambiguous across live sessions; nothing was "
                                         "delivered for that name.\n")
                    sys.stdout.flush()
                    return 0
                # Render and WRITE before the queue is marked read, while still holding the lock.
                # Committing first meant a broken pipe, a closed stdout or an interrupt between the
                # save and the write left the messages unreadable from the queue although nothing
                # had been delivered -- the delivery guarantee inverted by its own bookkeeping.
                if not (args.quiet and not mine):
                    sys.stdout.write(inbox_mod.render(mine, marked_read=bool(mine) and
                                                      not args.peek, you=_you(repo, who)))
                if ambiguous:
                    sys.stdout.write("Held mail for " + ", ".join(ambiguous) +
                                     " is ambiguous across live sessions; nothing was "
                                     "delivered for that name.\n")
                if mine or ambiguous:
                    sys.stdout.flush()
                if mine and not args.peek:
                    store.save_inbox(repo, inbox_mod.mark_read(queue, [m.id for m in mine]))
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    return 0


def cmd_harvest(args) -> int:
    repo = _resolve_repo(args)
    path = args.transcript
    # SKILL.md documents `session-coord harvest --quiet` with no --session, so falling back to
    # the environment is what makes the DOCUMENTED command work rather than report "no
    # transcript". Same class of bug as the --pid one: the broken path was the one the
    # documentation told people to take.
    session = args.session or os.environ.get("CLAUDE_SESSION_ID") or ""
    if not path and repo:
        rec = next((p for p in store.load_all(repo) if p.session_id == session), None)
        if rec is None and not session:
            pane = os.environ.get("TMUX_PANE")
            rec = next((p for p in store.load_all(repo) if pane and p.pane == pane), None)
        path = rec.transcript if rec else ""
    if not path:
        print("session-coord: no transcript to harvest.", file=sys.stderr)
        return 0
    cands = harvest.candidates(harvest.load_transcript(path))
    if args.json:
        print(json.dumps([c.__dict__ for c in cands], indent=2))
        return 0
    if args.quiet and not cands:
        return 0
    sys.stdout.write(harvest.render(cands))
    return 0


# ---- wiring --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session-coord",
        description="Keep several Claude sessions in one repo from tangling.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def repo_arg(sp):
        sp.add_argument("--repo", help="repo root (default: the one containing cwd)")

    reg = sub.add_parser("register", help="record this session's presence in a repo")
    repo_arg(reg)
    reg.add_argument("--session", help="default: this session, found by pane or by claude pid")
    reg.add_argument("--name", help="a short stable name peers address you by (outside tmux)")
    reg.add_argument("--pane")
    reg.add_argument("--window")
    reg.add_argument("--pid", type=int)
    reg.add_argument("--engine", choices=("claude", "codex"))
    reg.add_argument("--cwd")
    reg.add_argument("--charter", help="one paragraph: what this session is for")
    reg.add_argument("--claim", action="append", help="a path or glob this session will edit")
    reg.add_argument("--transcript")
    reg.add_argument("--wait", type=float, default=5.0,
                     help="seconds to wait for the presence lock (default 5). Deliberately "
                          "short: this runs in a SessionStart hook, and a long stall there is "
                          "the first thing a session does.")
    reg.set_defaults(func=cmd_register)

    b = sub.add_parser("board", help="who else is working in this repo")
    repo_arg(b)
    b.add_argument("--quiet", action="store_true", help="say nothing unless there are peers")
    b.add_argument("--json", action="store_true")
    b.add_argument("--hosts", action="store_true",
                   help="also ask the boxes in ~/.config/session-coord/hosts, over ssh "
                        "(at most 5s; never from a hook)")
    b.add_argument("--all", action="store_true",
                   help="with --hosts: list every remote session, not just this repo's")
    b.add_argument("--estate", action="store_true",
                   help="every live session on this box as JSON; what --hosts reads remotely")
    b.set_defaults(func=cmd_board)

    c = sub.add_parser("check", help="findings for this repo; exit 2 blocks, 1 warns")
    repo_arg(c)
    c.add_argument("--workdir", help="report only findings that involve this working tree")
    c.add_argument("--repo-of", dest="repo_of",
                   help="resolve the repo from this path rather than from cwd")
    c.add_argument("--quiet", action="store_true", help="print only blocking findings")
    c.add_argument("--json", action="store_true")
    c.add_argument("--path", action="append", default=[],
                   help="with --workdir: report any of these tree-relative paths a peer claims")
    c.add_argument("--session", default="",
                   help="with --path: the asking session, whose own claims are excused")
    c.add_argument("--require-claim", action="store_true",
                   help="with --path: also report any of these the asking session has not "
                        "claimed")
    c.set_defaults(func=cmd_check)

    cl = sub.add_parser("claim", help="add paths to this session's claims, keeping the rest")
    repo_arg(cl)
    cl.add_argument("--session", default="", help="this session's id (default: found by pane)")
    cl.add_argument("--wait", type=float, default=5.0)
    cl.add_argument("paths", nargs="+", help="paths or globs, relative to the repo root")
    cl.set_defaults(func=cmd_claim)

    s = sub.add_parser("sweep", help="drop records whose session is gone")
    repo_arg(s)
    s.add_argument("--wait", type=float, default=5.0)
    s.set_defaults(func=cmd_sweep)

    dr = sub.add_parser("deregister", help="remove one session's record by id")
    repo_arg(dr)
    dr.add_argument("--session", required=True)
    dr.add_argument("--wait", type=float, default=5.0)
    dr.set_defaults(func=cmd_deregister)

    cond = sub.add_parser("conductor", help="who sequences the sessions here")
    cond.add_argument("action", choices=["claim", "show", "release"])
    repo_arg(cond)
    cond.add_argument("--session")
    cond.add_argument("--wait", type=float, default=30.0,
                      help="seconds to wait for the presence lock (default 30)")
    cond.set_defaults(func=cmd_conductor)

    a = sub.add_parser("ask", help="the one queue the user reads")
    a.add_argument("action", choices=["add", "list", "answer", "dissolve"])
    repo_arg(a)
    a.add_argument("--session")
    a.add_argument("--window")
    a.add_argument("--tag")
    a.add_argument("--kind", choices=["A", "Q"])
    a.add_argument("--text")
    a.add_argument("--depends-on", action="append", dest="depends_on")
    a.add_argument("--wait", type=float, default=30.0,
                   help="seconds to wait for the queue lock (default 30)")
    a.set_defaults(func=cmd_ask)

    m = sub.add_parser("msg", help="type a message into another session's prompt")
    repo_arg(m)
    m.add_argument("target", help="pane id (%%67), window label (infra:11) or session id")
    m.add_argument("text", help="the message body; `-` reads it from stdin")
    m.add_argument("--subject", help="a short line shown above the body in the inbox")
    m.add_argument("--sender", help="how to label this message (default: this session's window)")
    m.add_argument("--force", action="store_true",
                   help="send even if the target's prompt has unsubmitted text in it")
    m.add_argument("--pid", type=int,
                   help="the claude pid this message is addressed to; refuses if the pane "
                        "holds a different one. Looked up from the board when omitted.")
    m.add_argument("--session", default="",
                   help="the sending session id, for the doorbell budget")
    m.add_argument("--reply", default="none", choices=["none", "required"],
                   help="whether an answer is wanted. Default none, and the body carries a marker saying so -- acknowledgement is not collegial, it is a turn.")
    m.add_argument("--override", default="",
                   help="one line saying what breaks if this waits; rings past the budget and records the reason.")
    m.add_argument("--no-ring", dest="no_ring", action="store_true",
                   help=argparse.SUPPRESS)  # file without ringing: set by a remote sender
    m.add_argument("--sender-name", dest="sender_name", default="",
                   help=argparse.SUPPRESS)  # the reply address, set by a remote sender
    m.set_defaults(func=cmd_msg)

    lk = sub.add_parser("lock", help="run a command under a named per-repo lock")
    repo_arg(lk)
    lk.add_argument("name", help="lock name, e.g. `merge`")
    lk.add_argument("command", nargs="*", help="the command to run under it (after `--`)")
    lk.add_argument("--wait", type=float, default=300.0, help="seconds to wait (default 300)")
    lk.add_argument("--owner", help="how to name this holder to whoever is waiting")
    lk.add_argument("--status", action="store_true", help="report the holder and exit")
    lk.set_defaults(func=cmd_lock)

    ad = sub.add_parser("addendum", help="print this machine's local rules for the skill, if any")
    ad.set_defaults(func=cmd_addendum)

    ib = sub.add_parser("inbox", help="read and clear messages peers left for this session")
    repo_arg(ib)
    ib.add_argument("--session")
    ib.add_argument("--peek", action="store_true", help="read without marking them read")
    ib.add_argument("--ids", action="store_true",
                    help="print message ids instead of bodies, for a caller that will --ack")
    ib.add_argument("--ack", action="append", default=[],
                    help="mark exactly these ids read (repeatable, or comma-separated); for a "
                         "caller that has already delivered that snapshot")
    ib.add_argument("--quiet", action="store_true", help="say nothing when there is no mail")
    ib.add_argument("--wait", type=float, default=15.0)
    # The UserPromptSubmit hook runs with the prompt's cwd, which can be any subdirectory of a
    # worktree, so it needs the same path-to-repo resolution `check` has rather than relying on
    # the process cwd.
    ib.add_argument("--repo-of", dest="repo_of",
                    help="resolve the repo from this path rather than from cwd")
    ib.set_defaults(func=cmd_inbox)

    h = sub.add_parser("harvest", help="loose ends this session is about to leave behind")
    repo_arg(h)
    h.add_argument("--session")
    h.add_argument("--transcript")
    h.add_argument("--quiet", action="store_true")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_harvest)
    return p


def _fill_session(args) -> None:
    """`--session` defaults to this session, wherever that can be established.

    The skill's commands were written as `--session "$CLAUDE_SESSION_ID"`, but nothing exports
    that variable to the Bash tool, so on a stock install the documented command passed an
    empty id. Fall back to this session's own record: by pane under tmux, by claude pid outside.
    """
    if not hasattr(args, "session") or args.session:
        return
    env = os.environ.get("CLAUDE_SESSION_ID")
    if env:
        args.session = env
        return
    try:
        repo = _resolve_repo(args)
    except Exception:
        return
    me = _me(repo) if repo else None
    if me is not None:
        args.session = me.session_id
    elif args.cmd == "register" and procio.codex_ancestor() is not None:
        # A Codex worker whose hook has not yet registered can still name itself from the
        # thread id exported to its tool environment. The pid and start time are detected by
        # register below; the environment supplies only the session id.
        args.session = os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # `lock <name> -- <command...>`: split at `--` by hand. Python 3.12's argparse hands the
    # tail to a `nargs="*"` positional; 3.9's -- what a stock macOS `python3` is -- rejects
    # it as unrecognised arguments.
    tail = None
    if argv[:1] == ["lock"] and "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if tail is not None:
        args.command = tail
    _fill_session(args)
    return args.func(args)
