"""Process facts that work on Linux and macOS alike, with or without tmux.

The package was born on a Linux box where every session lived in a tmux pane, so it identified
a session by its pane and read the process tree out of `/proc`. Neither exists on a stock
MacBook. This module is the replacement for the second, and the basis for the first: a session
is the `claude` process that is an ancestor of whatever is asking, and a recorded pid is still
that session only if the process holding it now started at the same moment it did then.

The start-time fingerprint is what makes a bare pid safe to trust. Pids get recycled, so
`kill -0` would let a stale record read as live forever once some unrelated process inherited
its number -- the reason the tmux path matches pid against pane rather than trusting either
alone. Start time does the same job without a pane: a recycled pid has a different one.

`/proc` is preferred where it exists because it is a file read rather than a process spawn, and
this runs on the hot path of session start. `ps` is the portable fallback; its `-o` fields used
here (`ppid=`, `comm=`, `args=`, `lstart=`) behave the same on procps and BSD.

Every function returns None when it could not look, never a guess. The same rule as `liveness`:
an unavailable observation is not an observation of absence.
"""

from __future__ import annotations

import os
import subprocess
from typing import Iterable

_PROC = os.path.isdir("/proc/self")


def _ps(pid: int, field: str) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def parent_of(pid: int) -> int | None:
    if _PROC:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            return int(stat[stat.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    raw = _ps(pid, "ppid")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def argv_of(pid: int) -> list[str] | None:
    if _PROC:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            return None
    raw = _ps(pid, "args")
    return raw.split() if raw else None


def start_of(pid: int) -> str | None:
    """An opaque fingerprint of when `pid` started. Comparable only on the box that made it."""
    if _PROC:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            # Field 22, starttime in clock ticks since boot. Counted from after the `)` that
            # closes comm, because comm may itself contain spaces and parentheses.
            return "proc:" + stat[stat.rfind(")") + 2:].split()[19]
        except (OSError, IndexError):
            return None
    raw = _ps(pid, "lstart")
    return f"ps:{raw}" if raw else None


def starts(pids: Iterable[int]) -> dict[int, str | None] | None:
    """Fingerprints for several pids. A pid that is gone maps to None.

    Returns None as a whole only when the platform gave us no way to look at all, which is the
    "assume everyone is still there" signal. A missing entry for one pid is an answer.
    """
    if not _PROC:
        try:
            subprocess.run(["ps", "-o", "pid=", "-p", str(os.getpid())],
                           capture_output=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
    return {pid: start_of(pid) for pid in set(pids)}


def is_claude(argv: list[str] | None) -> bool:
    """Is this argv a Claude Code process?

    Native installs run a `claude` binary. npm installs run `node .../claude` or
    `node .../@anthropic-ai/claude-code/cli.js`, where the executable is node and the name is
    in the first argument.
    """
    if not argv:
        return False
    if os.path.basename(argv[0]) == "claude":
        return True
    if os.path.basename(argv[0]) in ("node", "bun") and len(argv) > 1:
        first = argv[1]
        # The package directory, not the substring "claude-code": that also matched any script
        # under a directory like claude-code-dashboard/, which then read as a live session.
        return (os.path.basename(first) == "claude"
                # The leading "/" lets a relative `@anthropic-ai/claude-code/cli.js`, run from
                # inside node_modules, match the same way an absolute path does.
                or "/@anthropic-ai/claude-code/" in "/" + first.replace("\\", "/"))
    return False


def is_codex(argv: list[str] | None) -> bool:
    """Recognise the native Codex binary and its official npm launcher."""
    if not argv:
        return False
    if os.path.basename(argv[0]) == "codex":
        return True
    return (os.path.basename(argv[0]) == "node" and len(argv) > 1
            and "/@openai/codex/bin/codex.js" in argv[1])


def is_codex_headless(argv: list[str] | None) -> bool:
    """One-shot Codex commands do not occupy the pane as peer sessions."""
    return _codex_subcommand(argv) is not None


def _codex_subcommand(argv: list[str] | None) -> str | None:
    """The non-conversation subcommand a Codex argv runs, such as `exec` or `app-server`.

    None for an interactive TUI, or for anything that is not Codex. Option values are skipped
    by the same rules Codex's parser uses, so `--config x app-server` names `app-server`.
    """
    if not is_codex(argv):
        return None
    subcommand = 2 if os.path.basename(argv[0]) == "node" else 1
    args = argv[subcommand:]
    value_options = {"-c", "--config", "--enable", "--disable", "--remote",
                     "--remote-auth-token-env", "-i", "--image", "-m", "--model",
                     "--local-provider", "-p", "--profile", "-s", "--sandbox",
                     "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}
    short_values = {opt for opt in value_options if opt.startswith("-") and
                    not opt.startswith("--")}
    headless = {"agents", "exec", "e", "review", "login", "logout", "mcp",
                "plugin", "app-server", "remote-control", "completion", "update",
                "doctor", "sandbox", "debug", "apply", "a", "queue", "archive",
                "delete", "migrate-rollouts", "unarchive", "cloud", "exec-server",
                "features", "help"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return None
        if arg in ("-i", "--image"):
            # This form consumes bare words through the next option, including filenames
            # spelled exec, e, or review.
            i += 2
            while i < len(args) and not args[i].startswith("-"):
                i += 1
            continue
        if arg.startswith("--image=") or (arg.startswith("-i") and len(arg) > 2):
            # An attached value is complete, but more image paths can follow it.
            i += 1
            while i < len(args) and not args[i].startswith("-") and args[i] not in headless:
                i += 1
            continue
        if arg in value_options:
            i += 2
            continue
        if (any(arg.startswith(opt + "=") for opt in value_options if opt.startswith("--"))
                or any(arg.startswith(opt) and len(arg) > len(opt) for opt in short_values)):
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg if arg in headless else None
    return None


CODEX_SERVERS = frozenset({"app-server", "exec-server"})


def is_codex_server(argv: list[str] | None) -> bool:
    """A Codex server process, such as the shared `codex app-server` daemon.

    Codex 0.163 and later run a TUI's tools and hooks inside a per-user daemon unless the TUI
    was started with `--no-daemon` or a `-c` override. The daemon is a child of whichever TUI
    started it, it inherited that TUI's environment, and later TUIs attach to its socket. So
    under it, the ancestry, `TMUX_PANE` and `PATH` all describe the FIRST TUI, not the caller.
    """
    return _codex_subcommand(argv) in CODEX_SERVERS


def nearest_engine(pid: int | None = None) -> tuple[str, int] | None:
    """The nearest engine process above `pid`: ("claude", pid), ("codex", pid) or
    ("codex-server", pid). None when there is none or the walk could not finish.

    One walk, so a Claude started by a Codex tool (or the reverse) is attributed to the engine
    it actually runs under rather than to whichever check happened to run first.
    """
    pid = os.getpid() if pid is None else pid
    for _ in range(16):
        ppid = parent_of(pid)
        if ppid is None or ppid <= 1:
            return None
        argv = argv_of(ppid)
        if is_claude(argv):
            return ("claude", ppid)
        if is_codex(argv):
            return ("codex-server" if is_codex_server(argv) else "codex", ppid)
        pid = ppid
    return None


def under_codex_server(pid: int | None = None) -> bool:
    """True when the caller runs under Codex's shared daemon, whose environment is not its own."""
    found = nearest_engine(pid)
    return found is not None and found[0] == "codex-server"


def claude_ancestor(pid: int | None = None) -> int | None:
    """The nearest `claude` process above `pid` (default: this process), or None."""
    pid = os.getpid() if pid is None else pid
    for _ in range(16):
        ppid = parent_of(pid)
        if ppid is None or ppid <= 1:
            return None
        if is_claude(argv_of(ppid)):
            return ppid
        pid = ppid
    return None


def codex_ancestor(pid: int | None = None) -> int | None:
    """The nearest actual Codex process, skipping a guarded shell wrapper."""
    pid = os.getpid() if pid is None else pid
    for _ in range(16):
        ppid = parent_of(pid)
        if ppid is None or ppid <= 1:
            return None
        if is_codex(argv_of(ppid)):
            return ppid
        pid = ppid
    return None


HEADLESS_FLAGS = frozenset({"-p", "--print", "--bg", "--background"})


def is_headless(pid: int) -> bool:
    """A `claude -p` run started from inside a real session, which must not register."""
    argv = argv_of(pid) or []
    return any(a in HEADLESS_FLAGS or a.split("=")[0] in HEADLESS_FLAGS for a in argv[1:])
