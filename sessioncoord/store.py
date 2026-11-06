"""Where coordination state lives: `~/.local/state/claude-coord/<repo-key>/`.

Not in the repo. Daniel picked this on 2026-09-17 and the reasons are worth keeping: a session
touches several repos, so per-repo state under `.ignored/` fragments the very thing that has to
be global; `.ignored/` multiplies once per worktree and dies when the worktree is removed,
taking the record of who was working there with it; and one state root answers "who is where
across the estate" with a single scan instead of a walk of ~50 repos.

One file per session rather than one file per repo, deliberately: several sessions register
concurrently and a shared file would need locking. Separate files make concurrent writes
non-interfering without any lock at all, which is also why `flock` turned out to be the wrong
primitive for presence -- a lock nobody blocks on is just a file, and the Bash tool spawns a
fresh shell per call so nothing can hold one across a turn anyway.

The ask queue is the exception and does need a lock, because it is one shared list that every
session read-modifies-writes. `cli` takes `locks.hold(repo, "asks")` around the whole
load/transform/save, not just the write -- an atomic rename stops a torn read, it does not make
a read-modify-write transaction atomic. Both PR #3 reviewers caught that independently.

This is an adapter: one of the three modules that touches the world.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .inbox import Message
from .liveness import PaneMap, StartMap, partition
from .comms import Sent
from .model import Ask, Presence

_SLUG = re.compile(r"[^a-zA-Z0-9]+")
# Identifiers that become path components. Session ids are uuids and lock names are ours, but
# both arrive through the CLI, so `--session ../../elsewhere` is reachable input rather than a
# hypothetical. Anything outside this alphabet is refused rather than sanitised: quietly
# rewriting an identifier would file a record under a name its owner cannot find again.
_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class UnsafeName(ValueError):
    """An identifier that would escape its directory, or is otherwise unusable as a filename."""


def check_name(value: str, what: str) -> str:
    if not _SAFE_ID.match(value or "") or ".." in value:
        raise UnsafeName(
            f"{what} {value!r} is not usable as a filename: letters, digits, dot, dash and "
            "underscore only, and no path separators."
        )
    return value


def home() -> Path:
    """The state root. `CLAUDE_COORD_HOME` overrides it, which is what the suite uses."""
    env = os.environ.get("CLAUDE_COORD_HOME")
    if env:
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "claude-coord"


def repo_key(repo_root: str) -> str:
    """A stable, human-findable directory name for a CANONICAL repo root.

    Takes the path it is given and does not consult git. Callers pass `gitio.repo_root(...)`,
    which is what collapses a worktree onto the main checkout it hangs off; handing this a
    worktree path directly yields a different key, by design -- an effectful git lookup does
    not belong in the store.

    Readable rather than hashed: Daniel debugs by looking, and an opaque key would make the
    state directory useless to him. The trailing digest keeps two repos with the same basename
    -- `acme/keripy` and `wot/keripy` -- from colliding.
    """
    canon = os.path.realpath(os.path.expanduser(repo_root)).rstrip("/")
    name = _SLUG.sub("-", os.path.basename(canon)).strip("-").lower() or "repo"
    # sha1 rather than the builtin hash(), which is salted per process and would hand two
    # sessions different directories for the same repo.
    digest = hashlib.sha1(canon.encode()).hexdigest()[:8]
    return f"{name}-{digest}"


def state_dir(repo_root: str) -> Path:
    return home() / repo_key(repo_root)


def _sessions_dir(repo_root: str) -> Path:
    return state_dir(repo_root) / "sessions"


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file UNIQUE to this process, then rename.

    The uniqueness matters as much as the rename. A shared `<name>.tmp` lets two concurrent
    writers interleave -- A writes the temp, B truncates and rewrites it, A renames B's bytes
    into place, then B's rename fails because the file it was about to move is gone. Both
    reviewers of PR #3 found this; the original used one temp name per target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass  # already renamed, which is the success path


def save(p: Presence) -> Path:
    """Write one session's record, replacing any earlier one for the same session."""
    check_name(p.session_id, "session id")
    d = _sessions_dir(p.repo_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{p.session_id}.json"
    # Record FIRST, marker second. The other order let a concurrent save write the marker, be
    # observed by a `remove` that saw no JSON yet, have its marker deleted, and then write its
    # record -- leaving a live repo absent from known_repos() with nothing to fix it.
    _write_atomic(path, p.to_json())
    _write_atomic(d / "repo-root",
                  os.path.realpath(os.path.expanduser(p.repo_root)) + "\n")
    return path


def load_all(repo_root: str) -> list[Presence]:
    """Every registered session for this repo, live or not, in stable order.

    A record that fails to parse is skipped rather than raised. Two sessions may be running
    different checkouts of this tool mid-upgrade, and one unreadable file must not take the
    board down for everybody -- the board's whole value is that it still answers.
    """
    d = _sessions_dir(repo_root)
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
            # Valid JSON is not necessarily a record. `[]` parses fine and then raises
            # AttributeError inside from_dict, which nothing caught -- so one malformed state
            # file tracebacked every board and check command in the repo.
            if not isinstance(raw, dict):
                continue
            out.append(Presence.from_dict(raw))
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    return sorted(out, key=lambda p: p.session_id)


def remove(repo_root: str, session_id: str) -> bool:
    """Drop one session's record, and the repo marker with the last of them.

    Leaving the marker made `known_repos()` report repos nobody is in, so an estate-wide scan
    accumulated phantoms forever. Its docstring promised repos with a registrant.
    """
    # Validated for the same reason `save` is: `deregister --session` and the SessionEnd hook
    # both reach here with input from outside, and an `../` identifier would make this unlink
    # a file outside the sessions directory. `save` had the check and `remove` did not, which
    # is the more dangerous half -- one writes, the other deletes.
    check_name(session_id, "session id")
    d = _sessions_dir(repo_root)
    path = d / f"{session_id}.json"
    try:
        path.unlink()
        gone = True
    except OSError:
        gone = False
    if gone and not any(d.glob("*.json")):
        try:
            (d / "repo-root").unlink()
        except OSError:
            pass
    return gone


def sweep(repo_root: str, pane_claude_pids: PaneMap,
          server: str | None = None, pid_starts: StartMap | bool = True) -> list[Presence]:
    """Delete records whose session is gone. Returns what was dropped.

    Deletes no PANE record when `pane_claude_pids` is None, which is how `tmuxio` reports that
    it could not consult tmux. That guard is the whole reason the sentinel exists: without it a
    ten-second tmux timeout erased every presence record in a repo, and the PreToolUse gate
    then saw no peers and permitted the commit it exists to refuse. Pane-less records are
    judged by their pid's start time instead, under the same rule (`liveness.is_live`), so a
    box with no tmux at all still sweeps its dead sessions.

    Runs on every registration, so the registry self-cleans without anybody remembering to
    tidy it -- a crashed session, a closed window, a reboot.
    """
    records = load_all(repo_root)
    if pid_starts is True:
        # Gathered here rather than by every caller: only the pids the registry names, and only
        # for records that rely on them. `True` means "look it up"; tests pass a literal map.
        from . import procio
        pids = [r.claude_pid for r in records if not r.pane and r.claude_pid is not None]
        pid_starts = procio.starts(pids) if pids else {}
    live, dead = partition(records, pane_claude_pids, server, pid_starts)
    dropped = []
    for p in dead:
        try:
            if remove(repo_root, p.session_id):
                dropped.append(p)
        except UnsafeName:
            continue  # a hand-placed record with a hostile id: leave it, never act on it
    # Only what actually went. Returning every candidate made `sweep` report a refused record
    # as dropped, and report it again on every subsequent sweep -- a false statement, repeated,
    # about the one thing this command exists to tell you.
    return dropped


def _asks_path(repo_root: str) -> Path:
    return state_dir(repo_root) / "asks.json"


class UnreadableQueue(RuntimeError):
    """The ask file exists and cannot be understood. Distinct from an empty queue."""


def load_asks(repo_root: str) -> list[Ask]:
    """The queue, or UnreadableQueue if the file exists and is not a list of records.

    An absent file is an empty queue; a corrupt one is NOT. Returning [] for both let
    `cmd_ask`'s load/transform/save write an empty list over a file it had failed to read --
    the round-eight data-loss bug, still open through the container rather than the items.
    A reader that cannot distinguish "nothing here" from "I could not read this" hands its
    caller a licence to destroy the thing it failed to read.
    """
    path = _asks_path(repo_root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise UnreadableQueue(f"{path} could not be read: {e}") from e
    if not isinstance(raw, list):
        raise UnreadableQueue(f"{path} is not a list of asks; refusing to act on it.")
    # Per-ITEM recovery, not per-file. Wrapping the whole comprehension meant one malformed
    # entry returned [] for the entire queue -- and the next `ask add` then SAVED that empty
    # list over every real ask. A tolerant reader that loses data is worse than a strict one
    # that refuses, because the loss is silent and the file it overwrites is Daniel's queue.
    out = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            out.append(Ask.from_dict(d))
        except (TypeError, AttributeError, KeyError):
            continue
    return out


def save_asks(repo_root: str, asks: Iterable[Ask]) -> Path:
    """Replace the queue. Callers MUST hold `locks.hold(repo, "asks")` across load and save."""
    path = _asks_path(repo_root)
    _write_atomic(path, json.dumps([a.to_dict() for a in asks], indent=2, sort_keys=True))
    return path


def _inbox_path(repo_root: str) -> Path:
    return state_dir(repo_root) / "inbox.json"


def load_inbox(repo_root: str) -> list[Message]:
    """Every message, read and unread. Per-ITEM recovery, like the ask queue.

    The two kinds of damage are not the same and this build no longer treats them alike. A
    single malformed ENTRY is skipped, because an inbox holds transient peer chatter and
    refusing to deliver nine messages over one bad tenth would break the channel to fix a
    nuisance. A malformed CONTAINER raises, because the caller's next move is a save: `cmd_msg`
    loads, appends and writes, so returning `[]` for a file it had failed to read handed it a
    licence to replace every message in it with one. That is the round-eight ask-queue data
    loss arriving through the inbox door, and the tolerant docstring that used to sit here
    argued for exactly the behaviour that caused it.

    `quarantine_inbox` is what a writer does with this: preserve, then proceed. Delivery still
    works, and nothing is destroyed to make it work.
    """
    path = _inbox_path(repo_root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise UnreadableQueue(f"{path} could not be read: {e}") from e
    if not isinstance(raw, list):
        raise UnreadableQueue(f"{path} is not a list of messages; refusing to act on it.")
    out = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            out.append(Message.from_dict(d))
        except (TypeError, AttributeError, KeyError):
            continue
    return out


def quarantine_inbox(repo_root: str) -> Path | None:
    """Move an unreadable inbox aside so a write can proceed without destroying it.

    The alternative was refusing the write, and that is wrong here for the reason the whole
    queue exists: a message that cannot be filed goes nowhere, and a peer is unreachable until
    somebody notices a corrupt file nobody is looking at. Renaming keeps both properties --
    the bytes survive for whoever wants them, and the channel stays open.
    """
    path = _inbox_path(repo_root)
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"inbox.unreadable-{stamp}.json")
    try:
        path.replace(dest)
    except OSError:
        return None
    return dest


def save_inbox(repo_root: str, messages: Iterable[Message]) -> Path:
    """Replace the inbox. Callers MUST hold `locks.hold(repo, "inbox")` across load and save."""
    path = _inbox_path(repo_root)
    _write_atomic(path, json.dumps([m.to_dict() for m in messages], indent=2, sort_keys=True))
    return path


def _held_path() -> Path:
    return home() / "held.json"


def load_held() -> list[Message]:
    """Estate-wide mail for names that had no registered recipient when it was sent."""
    path = _held_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise UnreadableQueue(f"{path} could not be read: {e}") from e
    if not isinstance(raw, list):
        raise UnreadableQueue(f"{path} is not a list of messages; refusing to act on it.")
    out = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            out.append(Message.from_dict(d))
        except (TypeError, AttributeError, KeyError):
            continue
    return out


def quarantine_held() -> Path | None:
    """Preserve unreadable held mail before a writer starts a fresh queue."""
    path = _held_path()
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"held.unreadable-{stamp}.json")
    try:
        path.replace(dest)
    except OSError:
        return None
    return dest


#: How long held mail waits for a session to take its name. 2026-11-05: held.json on kelun had
#: 278 unread messages going back to 2026-10-13, every one addressed to a name no session had
#: taken since. Mail that old describes a state of the work nobody is in any more, so delivering
#: it late is worse than not delivering it, and the sender is told the deadline when it holds.
HELD_TTL = timedelta(hours=72)
#: A `created` this far ahead of the clock is treated as unreadable. Without a bound, one bad
#: timestamp sits in the queue forever -- the `inf` case from the budget log, one file over.
_SKEW = timedelta(hours=1)


def unexpired_held(messages: Iterable[Message],
                   now: datetime | None = None) -> tuple[list[Message], list[Message]]:
    """(kept, expired). Expires mail older than HELD_TTL.

    A `created` that cannot be read -- empty, malformed, without a timezone, or more than an
    hour in the future -- counts as expired rather than kept: kept, it could never age out,
    which is the failure this exists to end. Pure, so the caller decides when to write.
    """
    now = now or datetime.now(timezone.utc)
    kept, expired = [], []
    for m in messages:
        try:
            t = datetime.fromisoformat(m.created)
            fresh = (t.tzinfo is not None and t <= now + _SKEW and now - t <= HELD_TTL)
        except (TypeError, ValueError):
            fresh = False
        (kept if fresh else expired).append(m)
    return kept, expired


def save_held(messages: Iterable[Message]) -> Path:
    """Replace held mail. Callers MUST hold the estate `held` lock across load and save."""
    path = _held_path()
    _write_atomic(path, json.dumps([m.to_dict() for m in messages], indent=2, sort_keys=True))
    return path


class UnreadableSends(RuntimeError):
    """The doorbell budget log exists but could not be read."""


def _sends_path(repo_root: str | None = None) -> Path:
    """The doorbell log, at the ESTATE root rather than per repo.

    The contract is three doorbells per session-hour, and it was implemented per repo -- so
    spending three rings here and switching repositories bought three more, which is not a
    limit. A session touches several repos, which is the same reasoning that put presence at
    this root instead of under each `.ignored/`. `repo_root` is accepted and ignored so callers
    read naturally and no signature churns.
    """
    return home() / "sends.jsonl"


def record_send(repo_root: str, sent: Sent) -> Path:
    """Append one interrupting message to the budget log.

    Append-only JSONL under O_APPEND rather than read-modify-write: several sessions record
    concurrently and a rewrite would drop entries, silently handing somebody budget they had
    already spent.
    """
    path = _sends_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(sent.to_dict(), sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def terminate_sends(repo_root: str) -> bool:
    """Close an unterminated final record with a newline, so the next append cannot splice.

    `load_sends` treats an unterminated tail as a half-written append and skips it. Appending
    after those bytes would join them to the new record, making the send just charged
    unparseable on the next read -- a charge that vanishes, which is the fail-open direction.
    Writing the newline turns the tail into a complete record that is skipped instead of a
    splice point. Returns True when it changed anything.
    """
    path = _sends_path(repo_root)
    try:
        raw = path.read_text()
    except (FileNotFoundError, UnicodeError, OSError):
        return False
    if not raw or raw.endswith("\n"):
        return False
    with open(path, "a") as f:
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    return True


def load_sends(repo_root: str) -> list[Sent]:
    """Every recorded send. A torn line from a concurrent append is skipped, never fatal."""
    path = _sends_path(repo_root)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        # The ONLY absence. `Path.exists()` was standing in for this and answers False for a
        # permission error on a parent directory too, so an inaccessible log read as an empty
        # one and `check` reported no budget spent -- absence and damage sharing a
        # representation again, in the one line that was supposed to separate them.
        return []
    except (OSError, UnicodeError) as e:
        # UnicodeError because `read_text` decodes: invalid UTF-8 raised straight past this
        # boundary and gave `check` and `msg` a traceback rather than a fail-closed budget.
        # NOT an empty list. Returning one made an unreadable log read as "nothing spent", so a
        # permission error or a transient I/O failure handed the session a fresh budget -- the
        # accounting failing open, which is the direction that removes the limit silently.
        raise UnreadableSends(f"{path} could not be read: {e}") from e
    lines = raw.splitlines()
    # A torn append is an UNTERMINATED final line -- the writer was interrupted before its
    # newline landed. A final line that ends in a newline is a complete record, so if it does
    # not parse it is corrupt, and skipping it understates the budget exactly like every other
    # lost row. Tolerating "the last line" rather than "an unterminated last line" was one
    # word too generous.
    torn_tail = bool(raw) and not raw.endswith("\n")
    out = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Sent.from_dict(json.loads(line)))
        except (ValueError, TypeError) as e:
            # ONLY the last line may be tolerated. A torn final record is a concurrent append
            # caught mid-write and costs nothing to ignore. A malformed record anywhere EARLIER
            # is a send that was recorded and then lost, and skipping it hands back headroom
            # that was already spent -- the same fail-open shape as reading an unreadable log
            # as empty, one level down.
            if i == len(lines) - 1 and torn_tail:
                continue
            where = ("the final record and it is newline-terminated, so it is corrupt "
                     "rather than half-written" if i == len(lines) - 1
                     else "not the final record")
            raise UnreadableSends(
                f"{path} line {i + 1} is malformed and is {where}, so a recorded doorbell has "
                f"been lost: {e}") from e
    return out


def known_repos() -> list[str]:
    """Every repo that has a registrant, for the estate-wide view."""
    root = home()
    if not root.is_dir():
        return []
    out = []
    for marker in sorted(root.glob("*/sessions/repo-root")):
        try:
            out.append(marker.read_text().strip())
        except OSError:
            continue
    return out
