"""Where sessions live, and how one speaks to another.

The pane -> claude-pid map is the liveness substrate. `claude_under` walks descendants of the
pane's shell rather than trusting `#{pane_current_command}`, because a session mid tool-call has
a bash child in the foreground and would otherwise read as a plain shell -- the same reasoning,
and the same walk, as `tmux-claude`.

Two rules here came out of the PR #3 review and both are load-bearing.

**A failed tmux call returns None, never "".** Every consumer distinguishes "could not look"
from "looked and found nothing", because collapsing them let a transient tmux failure erase a
repo's entire presence registry.

**`send` is hostile input by construction.** It types text into a terminal somebody else is
using, so the message is filtered for control bytes and sent with `send-keys -l`. Without `-l`,
tmux parses key NAMES: a message containing the word `Enter` submits the recipient's
half-written prompt, and one containing `C-c` interrupts their work. That is not a hypothetical
-- it is the default behaviour of the command.

`send` is the capability Daniel authorised on 2026-09-17: one session types into another's
prompt, which is the mechanical form of him carrying a prompt to three windows by hand. It is
also the most alarming thing in this package, which is why it refuses more than it accepts.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import procio

Runner = Callable[[list[str]], "str | None"]

# Anything that can move a cursor, submit a line, interrupt a process, or start an escape
# sequence in the recipient's terminal. Tab is excluded too: it triggers completion menus.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_MESSAGE = 4000

# Beyond this, the body goes to a file and only a pointer is typed. Short enough that a
# collision the busy-prompt check could not see costs a line rather than a paragraph, and long
# enough that "your blocker cleared" still arrives inline where it is read immediately.
INLINE_LIMIT = 200

# Marks the seam when a peer's text lands mid-line. Leading space so it does not read as part
# of whatever it landed after.
SIGIL = ">>>"


def _run(argv: list[str]) -> str | None:
    """stdout on success, None on any failure. None means "could not look", not "nothing"."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _children() -> dict[int, list[int]] | None:
    """The process tree, or None if /proc could not be read.

    None rather than `{}`, for exactly the reason a failed tmux call is None: an empty child
    map is a successful observation in which no pane holds a claude, so it flows through
    `panes()` into an authoritative map of Nones and `sweep()` deletes the whole board. That is
    the PR #3 blocker arriving through the process-tree door instead of the tmux one -- found
    by the second review round, after the first fix closed only the tmux path.
    """
    if not procio._PROC:
        return _children_from_ps()
    kids: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except PermissionError:
            # We are not ALLOWED to look -- `hidepid=2`, a different user's process, a
            # container boundary. That is unknown, not absent: the process may well be a peer's
            # claude, and returning a partial tree would let `sweep` delete a live record on
            # the strength of a question we were refused an answer to. Fourth distinct door
            # onto this one property across five review rounds, so it is checked precisely
            # rather than lumped in with the benign case below.
            return None
        except OSError:
            # Almost always ENOENT: the process exited between the listdir and the open. That
            # happens constantly on a busy box and is genuinely "not there", so skipping it is
            # correct and treating it as unknown would make the tree permanently unreadable.
            continue
        # comm can contain spaces and parens; everything after the last ')' is positional
        tail = stat[stat.rfind(")") + 2:].split()
        if len(tail) < 2:
            continue
        kids.setdefault(int(tail[1]), []).append(int(entry))
    return kids


def _children_from_ps() -> dict[int, list[int]] | None:
    """The process tree from one `ps` listing, for a box with no `/proc` (every Mac).

    Same contract as the `/proc` walk: None when we could not look, never a partial tree. A
    failed `ps`, or a line that is not two integers, means the answer is not the shape we asked
    for, and a half-read tree would let `sweep` delete a live record. BSD `ps -A` lists every
    user's processes, so there is no hidepid-style partial view to worry about here.
    """
    out = _run(["ps", "-A", "-o", "pid=,ppid="])
    if not out:
        return None
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            return None
        kids.setdefault(ppid, []).append(pid)
    return kids


def _signal_probe(pid: int) -> "bool | None":
    """True if `pid` exists, False if it is gone, None if the probe itself failed.

    Signal 0 checks existence without delivering anything. EPERM means the process is there and
    belongs to someone else, which is "there".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class Unknown(Exception):
    """We were refused sight of a process. Distinct from having looked and found nothing."""


class _UnknownPid:
    """A per-pane observation failure, distinct from an empty pane."""


UNKNOWN_PID = _UnknownPid()


def _is_claude(pid: int) -> bool:
    """True/False when we could look; raises Unknown when we were not allowed to.

    Fifth door onto the same property. Collapsing every OSError into False reported a LIVE
    claude as absent whenever `/proc/<pid>/comm` was unreadable, and the sweeper then deleted
    that session's record. ENOENT is the benign case -- the process exited mid-walk, which
    happens constantly -- and everything else means we were refused.
    """
    if not procio._PROC:
        return _is_claude_from_ps(pid)
    try:
        with open(f"/proc/{pid}/comm") as f:
            if f.read().strip() == "claude":
                return True
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except FileNotFoundError:
        return False
    except OSError as e:
        raise Unknown(str(e)) from e
    return bool(argv) and os.path.basename(argv[0]) == "claude"


def _is_claude_from_ps(pid: int) -> bool:
    """`_is_claude` for a box with no `/proc`, with the same three answers.

    `ps` exits non-zero both for a pid that is gone and for one it could not describe, so its
    silence is disambiguated with a signal probe: gone is False, anything still there that `ps`
    would not name is Unknown.
    """
    comm = procio._ps(pid, "comm")
    if comm is None:
        if _signal_probe(pid) is False:
            return False
        raise Unknown(f"ps could not describe live pid {pid}")
    if os.path.basename(comm) == "claude":
        return True
    argv = procio.argv_of(pid)
    if argv is None:
        if _signal_probe(pid) is False:
            return False
        raise Unknown(f"ps could not read the argv of live pid {pid}")
    # procio.is_claude, not a basename check: an npm or bun install runs as `node`/`bun` with
    # claude in the first argument, and reading that live session as absent would let a sweep
    # delete its record (hostile pass on PR #6).
    return procio.is_claude(argv)


def claude_under(pane_pid: int, kids: dict[int, list[int]]) -> "int | None":
    """The claude under a pane, or None. Propagates Unknown rather than guessing."""
    seen: set[int] = set()
    queue = list(kids.get(int(pane_pid), []))
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        if _is_claude(pid):
            return pid  # do not descend: a session's own subagents are not the session
        if procio.is_codex(_argv_of(pid)):
            continue  # a Claude started by a Codex tool is not the pane's session
        queue.extend(kids.get(pid, []))
    return None


def _argv_of(pid: int) -> list[str] | None:
    return procio.argv_of(pid)


def _pid_exited(pid: int) -> bool:
    """True only when the OS confirms a vanished process, not when access was refused."""
    if not procio._PROC:
        return _signal_probe(pid) is False
    try:
        os.stat(f"/proc/{pid}")
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def codex_under(pane_pid: int, kids: dict[int, list[int]]) -> "int | None":
    """Prefer the nearest interactive native Codex process, including the pane pid itself."""
    seen: set[int] = set()
    queue = [int(pane_pid)]
    launcher = None
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        argv = _argv_of(pid)
        if argv is None:
            if _pid_exited(pid):
                continue
            raise Unknown(f"cannot read argv of {pid}")
        if procio.is_codex(argv):
            if procio.is_codex_headless(argv):
                continue
            if os.path.basename(argv[0]) == "codex":
                return pid
            launcher = pid
        queue.extend(kids.get(pid, []))
    return launcher


def server_pid(run: Runner = _run) -> "str | None":
    """The tmux server's own pid: an incarnation marker.

    A restarted server hands out the SAME `%N` pane ids from scratch. Pair that with a recycled
    process id and an old presence record matches a session that is not it -- keeping its claims
    and its conductor role, and receiving its messages. `claude-session-registry` records the
    server for exactly this reason; this borrowed its liveness idiom and not this part.
    """
    return run(["tmux", "display-message", "-p", "#{pid}"]) or None


def panes(run: Runner = _run) -> dict[str, dict] | None:
    """Every pane tmux knows about, keyed by pane id. None when it could not be consulted.

    `list-panes -a`, not `list-windows -a`. The latter emits one record per WINDOW -- the
    active pane of each -- so a session running in a split window's secondary pane is simply
    absent, and the sweep then deletes a live session's record while the gate reports no peers.
    `tmux-claude` already used `list-panes -a` for this reason; the second PR #3 review round
    caught that this did not.
    """
    fmt = ("#{pane_id}\t#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_current_path}"
           "\t#{pane_title}")
    out = run(["tmux", "list-panes", "-a", "-F", fmt])
    if out is None:
        return None
    kids = _children() if out else {}
    if kids is None:
        return None  # /proc unreadable: we cannot say who holds what, so we do not guess
    result: dict[str, dict] = {}
    for line in out.splitlines():
        # The title is last and split off at most once, since it is free text. A row without
        # it is the older four-field shape, read with an empty title.
        parts = line.split("\t", 4)
        title = clean_title(parts.pop()) if len(parts) == 5 else ""
        # A row we cannot parse means the response is not the shape we asked for, and a
        # partially-understood answer is not an answer. Skipping the row would hand `sweep` an
        # authoritative map missing exactly the panes we failed to read, and it would delete
        # those live sessions' records -- the same fail-open as a swallowed tmux error, in a
        # narrower window.
        if len(parts) != 4:
            return None
        pane, window, pane_pid, path = parts
        try:
            claude = claude_under(int(pane_pid), kids)
        except (ValueError, Unknown):
            return None
        if claude is not None:
            codex = None
        else:
            try:
                codex = codex_under(int(pane_pid), kids)
            except Unknown:
                result[pane] = {"pane": pane, "window": window, "path": path,
                                "claude_pid": UNKNOWN_PID, "engine": None, "title": title}
                continue
        result[pane] = {"pane": pane, "window": window, "path": path,
                        "claude_pid": claude if claude is not None else codex,
                        "engine": ("claude" if claude is not None else
                                   ("codex" if codex is not None else None)),
                        "title": title}
    return result


def clean_title(raw: str) -> str:
    """A pane title as a name: Claude Code prefixes a status glyph ("✳ ", a spinner frame)."""
    return re.sub(r"^[^0-9A-Za-z]+", "", raw or "").strip()


def pane_of_title(title: str, run: Runner = _run) -> str | None:
    """The single live Claude pane whose title is `title` (case-insensitive), or None.

    Raises `AmbiguousTarget` when two panes share it, for `pane_of_window`'s reason: guessing
    between two sessions is the worst thing this module can do.
    """
    table = panes(run)
    want = clean_title(title).lower()
    if not table or not want:
        return None
    hits = [pane for pane, info in table.items()
            if info["claude_pid"] is not None and info.get("title", "").lower() == want]
    if len(hits) > 1:
        raise AmbiguousTarget(
            f"{title!r} is the title of {len(hits)} panes ({', '.join(sorted(hits))}). Address "
            "a pane id or a session id instead."
        )
    return hits[0] if hits else None


def claude_pids(run: Runner = _run) -> dict[str, int | None | _UnknownPid] | None:
    """The liveness map, or None when tmux could not be consulted. See `liveness.PaneMap`."""
    table = panes(run)
    if table is None:
        return None
    return {pane: info["claude_pid"] for pane, info in table.items()}


def window_of(pane: str, run: Runner = _run) -> str | None:
    table = panes(run)
    if not table:
        return None
    info = table.get(pane)
    return info["window"] if info else None


class AmbiguousTarget(RuntimeError):
    """A window label names more than one pane, so it does not name a session."""


def pane_of_window(window: str, run: Runner = _run) -> str | None:
    """The single pane of a window, or None. Raises when the label is ambiguous.

    A window can be split, and discovery deliberately finds Claude in every pane of it -- so
    returning the first match made `send("me:1", ...)` type into whichever pane happened to sort
    first. Silently addressing the wrong session is the worst thing this module can do, and it
    is the one case where refusing is clearly better than guessing: the caller has a pane id and
    a session id available and can name either.
    """
    table = panes(run)
    if not table:
        return None
    # Only panes holding a live Claude count. A window split between a session and a plain
    # shell names exactly one session, and refusing it was a false refusal introduced while
    # fixing a real one -- the shell was never an addressable target in the first place.
    hits = [pane for pane, info in table.items()
            if info["window"] == window and info["claude_pid"] is not None]
    if not hits:
        hits = [pane for pane, info in table.items() if info["window"] == window]
    if len(hits) > 1:
        raise AmbiguousTarget(
            f"{window} has {len(hits)} panes ({', '.join(sorted(hits))}) and so does not name "
            "one session. Address a pane id or a session id instead."
        )
    return hits[0] if hits else None


class SendRefused(RuntimeError):
    """Refused to type into another pane. The message says which guard fired."""


class SendUnknown(SendRefused):
    """Keys were sent, but the recipient's prompt did not confirm submission."""


# Claude Code's prompt line. An idle prompt is this glyph, a non-breaking space, and nothing.
PROMPT_GLYPH = "❯"
CODEX_PROMPT_GLYPH = "›"


def _codex_prompt_state(pane: str, run: Runner = _run) -> str | None:
    """Return idle, typed, working, or unknown from one visible Codex screen."""
    out = run(["tmux", "capture-pane", "-p", "-e", "-t", pane])
    if out is None:
        return None
    lines = out.splitlines()
    prompt = next((i for i in range(len(lines) - 1, -1, -1)
                   if _typed_text(lines[i]).strip().startswith(CODEX_PROMPT_GLYPH)), None)
    if prompt is None:
        return None
    visible = [_visible_text(line).strip() for line in lines[:prompt]]
    if any(re.match(r"^•\s+Working\s*\(", line) for line in visible):
        return "working"
    rest = _typed_text(lines[prompt]).strip()[len(CODEX_PROMPT_GLYPH):].strip()
    if rest:
        return "typed"
    for line in reversed(visible):
        if re.search(r"\bWorked for\b", line):
            return "idle"
        if re.match(r"^•\s+(?:Ran|Running|Called|Failed|Waiting)\b", line):
            return None  # a tool card with no visible turn status is not proof of idle
    return "idle" if any("OpenAI Codex" in line for line in visible) else None


def _codex_composer_after_send(pane: str, text: str, run: Runner) -> str | None:
    """Return submitted, pending, or unknown for the exact text we just typed.

    `-J` joins terminal-wrapped rows, so a long single-line paste can be compared to the
    original text. A different typed sentence is unknown, not an invitation to submit it.
    """
    out = run(["tmux", "capture-pane", "-p", "-e", "-J", "-t", pane])
    if out is None:
        return None
    lines = out.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        stripped = _typed_text(line).strip()
        if stripped.startswith(CODEX_PROMPT_GLYPH):
            rest = stripped[len(CODEX_PROMPT_GLYPH):].strip()
            visible = _visible_text(line).strip()
            visible_rest = visible[len(CODEX_PROMPT_GLYPH):].strip()
            if rest or visible_rest == text.strip():
                return "pending" if rest == text.strip() or visible_rest == text.strip() else None
            activity = [_visible_text(row).strip() for row in lines[:index]]
            if any(re.match(r"^•\s+Working\s*\(", row) for row in activity):
                return "submitted"
            return None  # an unchanged idle screen may precede the paste being painted
    return None


def prompt_is_busy(pane: str, run: Runner = _run, engine: str = "claude") -> "bool | None":
    """Is the target already holding text somebody typed and has not submitted?

    True/False when the prompt line can be found, None when it cannot.

    This exists because of an observed collision rather than a theoretical one. On 2026-09-18 a
    peer's message landed in Daniel's pane while he was mid-sentence, and the two were submitted
    as one turn: "okay, me:3 and me:6 should now see the skillFrom me:4. (1) Registered:". He
    could not tell whose words were whose -- and the Enter that delivered the peer's message
    also submitted his unfinished one, which is the real damage.
    """
    # `-e` keeps the SGR attributes, because the one that matters is DIM. Claude Code shows a
    # suggested next prompt in dim text on an empty prompt line, and read as plain text it is
    # indistinguishable from a sentence Daniel is typing. On 2026-10-10 every idle peer in three
    # repos carried one ("low stakes, quick check and merge"), so every message to them was
    # refused or left un-rung as "somebody is typing", and he was told to press Enter on
    # sentences he had never written.
    if engine == "codex":
        state = _codex_prompt_state(pane, run)
        return None if state is None else state != "idle"
    out = run(["tmux", "capture-pane", "-p", "-e", "-t", pane])
    if out is None:
        return None
    for line in reversed(out.splitlines()):
        stripped = _typed_text(line).strip()
        if stripped.startswith(PROMPT_GLYPH):
            rest = stripped[len(PROMPT_GLYPH):]
            return bool(rest.replace(" ", "").strip())
    return None


_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _visible_text(line: str) -> str:
    """Remove terminal escapes while retaining dim Codex activity text."""
    return _OTHER_ESCAPE.sub("", _SGR.sub("", line))


def _typed_text(line: str) -> str:
    """The line with escapes removed and every run rendered DIM dropped: what was typed.

    SGR 2 turns dim on; 22 (normal intensity) or a reset (0, or an empty parameter list) turns
    it off. A line captured without escapes passes through unchanged.
    """
    out, dim, pos = [], False, 0
    for match in _SGR.finditer(line):
        if not dim:
            out.append(line[pos:match.start()])
        for param in (match.group(1) or "0").split(";"):
            if param == "2":
                dim = True
            elif param in ("0", "", "22"):
                dim = False
        pos = match.end()
    if not dim:
        out.append(line[pos:])
    return _OTHER_ESCAPE.sub("", "".join(out))


def _reject_control(text: str, what: str) -> None:
    """Refuse any terminal control byte in something about to be typed into a prompt."""
    bad = CONTROL_RE.findall(text)
    if bad:
        names = ", ".join(sorted({repr(c) for c in bad}))
        raise SendRefused(
            f"{what} contains terminal control characters ({names}). A carriage return would "
            "submit it early as a truncated instruction, and an escape sequence would "
            "reconfigure the recipient's terminal. Nothing was sent; send a single line, or "
            "write the detail to a file and send the path."
        )


def _clean(text: str) -> str:
    """Reject a message that carries terminal control bytes.

    An embedded carriage return submits the message early -- splitting it into two prompts,
    the first of which is a truncated instruction. An ESC can reconfigure the recipient's
    terminal. Neither belongs in a sentence one session sends another, so this refuses rather
    than stripping: silently altering a message is worse than declining to send it.
    """
    text = text.strip()
    if not text:
        raise SendRefused("refusing to send an empty message: a bare Enter would submit "
                          "whatever the recipient had already typed.")
    if len(text) > MAX_MESSAGE:
        raise SendRefused(
            f"message is {len(text)} characters; the cap is {MAX_MESSAGE}. Put the detail in a "
            "file and send the path -- that is what the recipient will do with it anyway."
        )
    _reject_control(text, "message")
    return text


MESSAGE_TTL_HOURS = 12


def _sweep_old(d: Path, ttl_hours: float = MESSAGE_TTL_HOURS) -> int:
    """Drop message files older than the TTL. Age-based, NOT delete-on-read.

    Daniel asked whether the reader should delete these once read. It should not, and the reason
    is worth keeping: the pointer outlives the reading. A session sees `>>> see /tmp/...`, gets
    interrupted, compacts, resumes an hour later and follows the link -- or a THIRD session is
    pointed at the same file, or Daniel opens it himself to see what his sessions told each
    other. Delete-on-read breaks all three, and it breaks them silently, leaving a live pointer
    to a file that no longer exists.

    Age is the right axis instead. It matches what `/tmp` does anyway under systemd-tmpfiles,
    and it matches the actual lifetime of the thing: an inter-session message is interesting for
    minutes and worthless by tomorrow. Swept on send rather than on a timer, so it costs nothing
    when nobody is messaging.
    """
    cutoff = datetime.now().timestamp() - ttl_hours * 3600
    dropped = 0
    try:
        entries = list(d.glob("*.md"))
    except OSError:
        return 0
    for f in entries:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                dropped += 1
        except OSError:
            continue
    return dropped


def spill(text: str, sender: str = "") -> str:
    """Write a long message to a short-lived file and return the path.

    /tmp is right for this by Daniel's own rule: these are ephemera whose loss is not a problem,
    and they are read within minutes of being written. The body may contain newlines and
    structure -- which is the point, since `_clean` refuses control characters in anything typed
    into a prompt, so inter-session PROSE was impossible until the body moved to a file.
    """
    d = Path(tempfile.gettempdir()) / "session-coord-msgs"
    # A long TMPDIR (common in worktrees with private on-disk temp roots) makes the pointer
    # itself too long to be a safe single line. Keep the same age-based cleanup in state.
    if len(str(d)) > 70:
        d = Path(os.environ.get("CLAUDE_COORD_HOME") or
                 Path.home() / ".local" / "state" / "claude-coord") / "msgs"
    d.mkdir(parents=True, exist_ok=True)
    _sweep_old(d)
    stamp = datetime.now().strftime("%H%M%S")
    who = re.sub(r"[^A-Za-z0-9]+", "-", sender).strip("-").lower() or "peer"
    # sender + time + pid is a READABLE name, not a unique one: two long messages from one
    # process in one second produced the same path, and the second silently overwrote the
    # first -- so the pointer already typed into a peer's prompt opened somebody else's body.
    # `mkstemp` keeps the readable prefix and makes the collision unrepresentable.
    fd, name = tempfile.mkstemp(prefix=f"{who}-{stamp}-{os.getpid()}-", suffix=".md", dir=d)
    with os.fdopen(fd, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return name


def send(target: str, text: str, run: Runner = _run, expect_pid: int | None = None,
         sender: str = "", force: bool = False, spill_over: int = INLINE_LIMIT) -> str:
    """Type `text` into another session's prompt and submit it.

    `target` is a pane id (`%67`) or a window label (`infra:11`). Returns the pane written to.

    Guards, because this is a session acting on Daniel's behalf without him watching:

    * The message is rejected outright if it carries control bytes (see `_clean`).
    * The target must currently hold a live Claude session. Typing into a shell would EXECUTE
      the message.
    * `expect_pid`, when given, must be the claude process in that pane -- so a caller holding
      a presence record talks to the session it meant rather than to whoever inherited the
      pane id. Pane ids are reused.
    * The pane is re-checked immediately before the keystrokes go out. This narrows the
      window between check and send; it cannot close it, because tmux offers no atomic
      send-if-still-this-process. A caller that must not be wrong should pass `expect_pid`.
    * Text goes out with `-l` (literal). Without it tmux parses key names, and a message
      mentioning `Enter` or `C-c` would submit or interrupt the recipient's work.
    * Both tmux calls are checked. Reporting "sent" after a failed send is worse than an
      error, because the sender then believes the peer was told.
    """
    # A long body goes to a file and only a pointer is typed. Done BEFORE `_clean`, so the body
    # is free to contain the newlines and structure that `_clean` refuses in a typed message --
    # which is what made inter-session prose impossible before this.
    if spill_over and len(text.strip()) > spill_over:
        path = spill(text, sender)
        text = f"see {path}"

    text = _clean(text)
    # Always attributed, and always prefixed with a leading space and a sigil.
    #
    # Daniel's design, 2026-09-18, after his own half-typed sentence was submitted along with a
    # peer's message. The sigil is a visual signal that the context switched mid-line, and the
    # leading space keeps it from reading as part of whatever it landed after. It does NOT fix
    # the co-mingling -- a short message appends to an unfinished sentence exactly as a long one
    # does, and the Enter still submits it -- so this sits alongside the busy-prompt refusal
    # rather than in place of it. What it does is bound the damage when that check cannot see.
    #
    # The sender label is filtered too, and separately. `_clean` runs on the body before this
    # line builds the final string, so a control byte arriving through `--sender` -- or through
    # a window label carrying one -- went out to `send-keys -l` having passed no check at all.
    # Validating the assembled string instead would reject the intentional leading space.
    if sender:
        _reject_control(sender, "sender label")
        text = f" {SIGIL} from {sender}: {text}"
    else:
        text = f" {SIGIL} {text}"

    def resolve() -> tuple[str, str]:
        table = panes(run)
        if table is None:
            raise SendRefused("tmux could not be consulted; nothing was sent.")
        try:
            pane = target if target in table else (
                pane_of_window(target, run) or pane_of_title(target, run))
        except AmbiguousTarget as e:
            raise SendRefused(f"{e} Nothing was sent.") from None
        if pane is None or pane not in table:
            raise SendRefused(f"no pane matches {target!r}; nothing was sent.")
        live = table[pane]["claude_pid"]
        engine = table[pane].get("engine", "claude")
        if live is UNKNOWN_PID:
            raise SendRefused(f"{target} process could not be read; nothing was sent.")
        if live is None:
            raise SendRefused(
                f"{target} holds no Claude session or Codex session -- it is a plain shell, and "
                "sending would execute the message as a command. Nothing was sent."
            )
        if engine not in ("claude", "codex"):
            raise SendRefused(f"{target} engine could not be read; nothing was sent.")
        if expect_pid is not None and live != expect_pid:
            raise SendRefused(
                f"{target} now holds session pid {live}, not the {expect_pid} this message was "
                "addressed to. Pane ids are reused; nothing was sent."
            )
        return pane, engine

    _, first_engine = resolve()
    # Re-resolve as late as possible AND use that answer as the destination. Discarding it was
    # worse than not re-checking at all: if `target` is a window label whose pane changed
    # between the two calls, the check validated pane B while the keystrokes still went to the
    # stale pane A -- which may by then be a shell. Narrows the window; tmux offers no atomic
    # send-if-still-this-process, so it cannot close it. Pass `expect_pid` when being wrong is
    # not acceptable.
    pane, engine = resolve()
    if engine != first_engine:
        raise SendRefused(f"{target} changed engines during delivery; nothing was sent.")

    # Never type over somebody mid-sentence. The Enter that delivers this message would submit
    # their unfinished one along with it, and the two arrive as one indistinguishable turn.
    #
    # An unreadable prompt does NOT refuse, deliberately. The glyph is a TUI detail that can
    # change under us, and failing closed there would silently kill inter-session messaging on
    # the next Claude Code release -- a worse outcome than an occasional collision, which the
    # attribution prefix at least makes legible. Refuse only on a positive observation.
    state = _codex_prompt_state(pane, run) if engine == "codex" else None
    busy = (None if state is None else state != "idle") if engine == "codex" else prompt_is_busy(pane, run)
    if not force and engine == "codex" and state is None:
        raise SendRefused(f"{target} Codex prompt could not be read; nothing was sent.")
    if not force and state == "working":
        raise SendRefused(f"{target} is working on a turn; nothing was typed. Retry when it is idle.")
    if not force and busy is True:
        raise SendRefused(
            f"{target} has unsubmitted text in its prompt -- somebody is typing there. Sending "
            "now would append this message to their sentence and submit both as one turn. "
            "Retry shortly, or write the detail to a file and send the path when they are idle."
        )
    if run(["tmux", "send-keys", "-t", pane, "-l", "--", text]) is None:
        raise SendRefused(f"tmux refused to type into {pane}; nothing was sent.")
    if run(["tmux", "send-keys", "-t", pane, "Enter"]) is None:
        raise SendRefused(
            f"the message was typed into {pane} but the submit keystroke failed. It is sitting "
            "unsent in that session's prompt -- tell the user rather than retrying, or the "
            "recipient gets it twice."
        )
    if engine == "codex":
        composer = _codex_composer_after_send(pane, text, run)
        if composer == "pending":
            # Codex sometimes treats a burst of literal keys as a paste and consumes the first
            # Enter without submitting it. Retry only while our exact text is still visible.
            if run(["tmux", "send-keys", "-t", pane, "Enter"]) is None:
                raise SendUnknown(f"send outcome unknown for {pane}: the second submit keystroke "
                                  "failed after the text remained in Codex's composer.")
            composer = _codex_composer_after_send(pane, text, run)
        if composer != "submitted":
            raise SendUnknown(f"send outcome unknown for {pane}: Codex's composer did not "
                              "confirm that the text was submitted. Do not resend automatically.")
    return pane
