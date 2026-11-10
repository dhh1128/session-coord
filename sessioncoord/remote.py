"""Sessions on Daniel's other boxes, fetched over ssh, never from a hook.

Everything here is optional. A box with no hosts file behaves exactly as before, and a box
whose network is down gets one line saying so and the local board. The design is in
`skills/session-coord/DESIGN.md`, "Sessions on other boxes".

Three seams keep the suite off the network: `_tailscale` reads `tailscale status --json`,
`_spawn` starts one ssh, and `cache_dir` is under the state home the tests already redirect.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Where session-coord lives on a peer. Not on a non-interactive PATH on kelun (2026-11-05),
#: so the remote command names it. `~` is expanded by the remote shell.
DEFAULT_REMOTE_CMD = "~/.local/bin/session-coord"

#: The whole fetch, every host in parallel, gets this long. A cold ssh to kelun took 3.2s;
#: a reused connection takes a fraction of that.
DEADLINE = 5.0

SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=4",
    # An ssh config with LocalForward (8800, for `preview`) fails to bind when a second ssh
    # opens while the first holds the port; the warning is noise and the forward is not ours.
    "-o", "ClearAllForwardings=yes",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/session-coord-%C",
    "-o", "ControlPersist=10m",
)


@dataclass(frozen=True)
class Host:
    name: str
    command: str = DEFAULT_REMOTE_CMD


@dataclass
class HostView:
    """What one peer box said, or why it said nothing."""

    host: str
    sessions: list[dict] = field(default_factory=list)
    worktrees: list[dict] = field(default_factory=list)
    #: "" when fresh; otherwise why this view is not a live answer.
    problem: str = ""
    #: When `sessions` was fetched (ISO). Older than now when it came from the cache.
    fetched: str = ""
    from_cache: bool = False
    offline: bool = False
    last_seen: str = ""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "session-coord"


def hosts_path() -> Path:
    env = os.environ.get("SESSION_COORD_HOSTS")
    return Path(os.path.expanduser(env)) if env else config_dir() / "hosts"


def load_hosts(path: Path | None = None) -> list[Host]:
    """One host per line, optionally followed by the remote command. `#` starts a comment.

    This box is dropped, under either its short hostname or its tailscale name, so one file
    can be copied to every box unchanged.
    """
    path = path or hosts_path()
    try:
        text = path.read_text()
    except OSError:
        return []
    out, seen = [], set()
    me = {n.lower() for n in local_names()}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, rest = line.partition(" ")
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            continue
        if name.lower() in me or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(Host(name, rest.strip() or DEFAULT_REMOTE_CMD))
    return out


def local_names() -> set[str]:
    full = socket.gethostname()
    return {full, full.split(".", 1)[0]}


def local_host() -> str:
    """How this box names itself to peers: its short hostname."""
    return socket.gethostname().split(".", 1)[0]


# ---- tailscale -----------------------------------------------------------------------

_TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _tailscale() -> str | None:
    """`tailscale status --json`, or None when tailscale cannot be asked."""
    exe = shutil.which("tailscale") or (_TAILSCALE_APP if os.path.exists(_TAILSCALE_APP)
                                        else None)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                             timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def tailnet(raw: str | None) -> dict[str, dict] | None:
    """Peers by lowercase short name, each {online, last_seen}. None: tailscale is not up.

    "Not up" includes a daemon that answers but is stopped or logged out, since then no peer
    is reachable over the tailnet whatever its own state.
    """
    if raw is None:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("BackendState") not in (None, "Running"):
        return None
    peers = d.get("Peer") or {}
    if not isinstance(peers, dict):
        return None
    out = {}
    for p in peers.values():
        if not isinstance(p, dict):
            continue
        names = {str(p.get("HostName") or "").lower(),
                 str(p.get("DNSName") or "").split(".", 1)[0].lower()}
        info = {"online": bool(p.get("Online")), "last_seen": str(p.get("LastSeen") or "")}
        for n in names - {""}:
            out[n] = info
    return out


# ---- fetching ------------------------------------------------------------------------

def sandboxed() -> str:
    """Why this process cannot reach another box, when it is a Codex sandbox that says so.

    Codex's default workspace-write sandbox turns the network off and sets this variable for
    the commands it runs. ssh then fails with a bare connect error that reads like kelun is
    down, which sends a session to debug the wrong box.
    """
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        return ("this command runs in Codex's sandbox with the network off, so no other box "
                "can be asked; run it with network access (or escalated) to see them")
    return ""


def remote_argv(host: Host, *args: str) -> list[str]:
    """ssh argv running session-coord on `host` with `args`, each one shell-quoted.

    ssh joins its trailing words with spaces and hands them to the remote shell, so an
    unquoted argument is re-parsed there. The command itself is left unquoted on purpose, so
    `~` expands; it comes from Daniel's own hosts file.
    """
    remote = " ".join([host.command, *(shlex.quote(a) for a in args)])
    return ["ssh", *SSH_OPTS, host.name, remote]


def _spawn(argv: list[str], stdin: bytes | None = None) -> subprocess.Popen:
    # Its own process group, so a kill reaches anything ssh forked: a child that inherited
    # the pipes would otherwise keep a reader blocked after ssh itself is dead.
    return subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None
                            else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)


def _kill(proc) -> None:
    """Kill the process and its group; a fake or an already-reaped process just gets kill()."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def cache_dir() -> Path:
    from . import store
    return store.home() / "remote"


def _cache_file(host: str) -> Path:
    return cache_dir() / f"{host}.json"


def _save_cache(view: HostView) -> None:
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        tmp = _cache_file(view.host).with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched": view.fetched, "sessions": view.sessions,
                                   "worktrees": view.worktrees}))
        os.replace(tmp, _cache_file(view.host))
    except OSError:
        pass


def _load_cache(host: str) -> tuple[list[dict], list[dict], str] | None:
    try:
        d = json.loads(_cache_file(host).read_text())
        parsed = _parse(json.dumps(d).encode())
        if parsed is None:
            return None
        return parsed[0], parsed[1], str(d["fetched"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_ledger(repo_root: str | None, path: str | None, branch: str | None) -> bool:
    """Whether `path` is the tick ledger's worktree, `<repo>/.tick` on branch `tick`.

    tick keeps its ledger in a worktree of the repo it tracks, so every repo with a ledger has
    one more worktree that no session will ever register in. Listed, it reads as someone's
    abandoned branch on every board. Checked on the receiving side as well as the sending
    side, because a peer on an older version still sends it.
    """
    # Both, not the path alone: a worktree someone happens to put at .tick on a branch of
    # their own is work, and hiding it is the failure this view exists to prevent.
    if not path or branch != "tick":
        return False
    head, tail = os.path.split(path.rstrip("/"))
    if tail != ".tick":
        return False
    return not repo_root or os.path.normpath(head) == os.path.normpath(repo_root)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(out: bytes) -> tuple[list[dict], list[dict]] | None:
    try:
        d = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    sessions = d.get("sessions") if isinstance(d, dict) else None
    if not isinstance(sessions, list):
        return None
    trees = d.get("worktrees")
    trees = trees if isinstance(trees, list) else []
    return ([_clean(x, _SESSION_FIELDS) for x in sessions if isinstance(x, dict)],
            [_clean(x, _TREE_FIELDS) for x in trees if isinstance(x, dict)])


_SESSION_FIELDS = ("name", "label", "repo_root", "workdir", "branch", "charter", "role",
                   "origin", "engine")
_TREE_FIELDS = ("repo_root", "origin", "path", "branch")


def _clean(d: dict, fields: tuple[str, ...]) -> dict:
    """Only the fields the board reads, each a string (claims a list of strings).

    A peer's answer is input like any other: one malformed field must cost that field, not
    the board, and must not be cached in a shape the renderer cannot handle.
    """
    out = {k: _text(d[k]) if isinstance(d.get(k), str) else "" for k in fields}
    if "claims" in d or fields is _SESSION_FIELDS:
        claims = d.get("claims")
        out["claims"] = ([_text(c) for c in claims[:MAX_CLAIMS] if isinstance(c, str)]
                         if isinstance(claims, list) else [])
    return out


MAX_FIELD = 2000
MAX_CLAIMS = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _text(value: str) -> str:
    """A peer's string, safe to print: control characters (which include the escape that
    starts a terminal sequence) become spaces, and length is capped."""
    return _CONTROL.sub(" ", value[:MAX_FIELD])


def _why(code: int | None, err: bytes) -> str:
    text = err.decode("utf-8", "replace").strip().splitlines()
    tail = _text(text[-1]).strip() if text else ""
    if code == 255:
        return f"ssh failed: {tail or 'no detail'}"
    if "unrecognized arguments" in tail or "invalid choice" in tail:
        return "its session-coord is too old to answer (update the plugin there)"
    return f"exit {code}: {tail}" if tail else f"exit {code}"


#: A box's whole answer. Daniel's estate is a few KB; a peer that sends more than this is
#: broken, and reading it all would let it take this box's memory and cache disk.
MAX_ANSWER = 2 * 1024 * 1024


def _bounded(proc, end: float):
    """(stdout, stderr) read with a size cap, or None for no answer by `end` or too much.

    stderr is drained on its own thread, keeping only its first 64 KB, so a peer that writes
    a lot of it cannot stall the stdout read on a full pipe.
    """
    err: list[bytes] = []

    def drain():
        kept = b""
        try:
            for chunk in iter(lambda: proc.stderr.read(65536), b""):
                if len(kept) < 65536:
                    kept += chunk[:65536 - len(kept)]
        except (OSError, ValueError):
            pass
        err.append(kept)
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        out = proc.stdout.read(MAX_ANSWER + 1)
    except (OSError, ValueError):
        return None
    if len(out) > MAX_ANSWER:
        return None
    try:
        proc.wait(timeout=max(0.0, end - time.monotonic()))
    except subprocess.TimeoutExpired:
        return None
    t.join(1.0)
    return out, (err[0] if err else b"")


def fetch(hosts: list[Host], deadline: float = DEADLINE, net=None) -> tuple[list[HostView], str]:
    """Every host's estate, in parallel, inside `deadline`. Also a one-line network note.

    `net` is the parsed tailnet (see `tailnet`); left None it is read here. A host the tailnet
    reports offline is not dialled. A host absent from the tailnet is dialled anyway, since
    ssh may reach it some other way. With tailscale down, nothing is dialled: every peer here
    is a tailnet peer, and waiting out five ssh timeouts to learn that is the cost the cache
    exists to avoid.
    """
    if net is None:
        net = tailnet(_tailscale())
    note = ""
    views: dict[str, HostView] = {}
    live: dict[str, subprocess.Popen] = {}
    blocked = sandboxed()
    if blocked and hosts:
        return [HostView(h.name, problem="not asked") for h in hosts], blocked
    if net is None and hosts:
        note = "tailscale is not running here, so no other box was asked"
    for h in hosts:
        info = (net or {}).get(h.name.lower())
        if net is None:
            views[h.name] = HostView(h.name, problem="tailscale is down")
        elif info is not None and not info["online"]:
            views[h.name] = HostView(h.name, offline=True, last_seen=info["last_seen"],
                                     problem="offline")
        else:
            try:
                live[h.name] = _spawn(remote_argv(h, "board", "--estate", "--json"))
            except OSError as e:
                views[h.name] = HostView(h.name, problem=f"could not start ssh: {e}")
    # One reader thread per host, so a slow first host cannot use up the budget of a fast
    # second one: sequential communicate() calls would leave a finished answer unread until the
    # deadline and then reject it.
    end = time.monotonic() + deadline
    answers: dict[str, tuple[bytes, bytes] | None] = {}

    def read(name, proc):
        answers[name] = _bounded(proc, end)
    threads = [threading.Thread(target=read, args=item, daemon=True) for item in live.items()]
    for t in threads:
        t.start()
    # Join only until the shared deadline, then kill whatever is still running: a kill closes
    # the pipe, which is what unblocks a reader stuck in read(). A per-thread grace period
    # added up to a second for every hung host (Copilot on PR #12).
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    late = {name for name in live if name not in answers}
    for name in late:
        _kill(live[name])
    # One grace period shared by every thread, not half a second each.
    grace = time.monotonic() + 0.5
    for t in threads:
        t.join(max(0.0, grace - time.monotonic()))
    for name, proc in live.items():
        got = None if name in late else answers.get(name)
        if got is None:
            _kill(proc)
            try:
                proc.wait(timeout=0.1)  # reap it; its reader thread owns the pipes
            except subprocess.TimeoutExpired:
                pass
            views[name] = HostView(name, problem=f"no answer within {deadline:g}s")
            continue
        out, err = got
        parsed = _parse(out) if proc.returncode == 0 else None
        if parsed is None:
            problem = (_why(proc.returncode, err) if proc.returncode
                       else "its answer was not readable")
            views[name] = HostView(name, problem=problem)
            continue
        v = HostView(name, sessions=parsed[0], worktrees=parsed[1], fetched=_now())
        _save_cache(v)
        views[name] = v
    # A failed host falls back to the last answer it gave, marked with its age. An offline
    # box keeps its sessions too: they were alive when it went quiet, which is worth knowing
    # when the question is "is anyone else on this repo".
    for v in views.values():
        if v.problem:
            cached = _load_cache(v.host)
            if cached:
                v.sessions, v.worktrees, v.fetched = cached
                v.from_cache = True
    return [views[h.name] for h in hosts if h.name in views], note


# ---- matching repos across boxes ----------------------------------------------------

def normalise_origin(url: str) -> str:
    """`git@github.com:o/r.git`, `https://github.com/o/r` and `ssh://git@github.com/o/r`
    all become `github.com/o/r`. Paths differ between boxes (/Users vs /home), so the
    origin is the only name two checkouts of one repo share."""
    u = (url or "").strip()
    if not u:
        return ""
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    m = re.match(r"^([^/:]+):(?!\d+/)(.*)$", u)
    if m:
        u = f"{m.group(1)}/{m.group(2)}"
    # Only the default ports are dropped; another port is another service.
    u = re.sub(r"^([^/]+):(?:22|443)/", r"\1/", u, count=1)
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    host, _, path = u.partition("/")
    host = host.lower()
    # GitHub and GitLab treat owner/repo case-insensitively; another server may not.
    if host in ("github.com", "gitlab.com"):
        path = path.lower()
    return f"{host}/{path}" if path else host


def origin_of(repo: str) -> str:
    from . import gitio
    return normalise_origin(gitio._run(["git", "config", "--get", "remote.origin.url"], repo))


# ---- rendering -----------------------------------------------------------------------

def age(iso: str, now: datetime | None = None) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "at an unknown time"
    if then.tzinfo is None or then.year < 2000:
        return "at an unknown time"
    secs = ((now or datetime.now(timezone.utc)) - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _status(v: HostView) -> str:
    if v.offline:
        seen = f", last seen {age(v.last_seen)}" if v.last_seen else ""
        lead = f"offline{seen}"
    elif v.problem:
        lead = v.problem
    else:
        return ""
    if v.from_cache:
        return f"{lead}; showing what it said {age(v.fetched)}"
    return lead


def render(views: list[HostView], origin: str, note: str = "", show_all: bool = False) -> str:
    """The remote part of the board: this repo's remote peers in full, the rest summarised."""
    if not views and not note:
        return ""
    lines = ["on other boxes:"]
    if note:
        lines.append(f"  {note}")
    for v in views:
        status = _status(v)
        if status and not v.sessions and not v.worktrees:
            lines.append(f"  {v.host}: {status}")
            continue
        if status:
            lines.append(f"  {v.host}: {status}")
        same = [s for s in v.sessions if origin and s.get("origin") == origin]
        rest = [s for s in v.sessions if s not in same]
        for s in same:
            branch = s.get("branch") or "detached"
            lines.append(f"  {v.host}: {s.get('label') or s.get('name')}  "
                         f"{s.get('workdir', '')}  ({branch})")
            lines.append(f"      address as: {s.get('name')}@{v.host}")
            if s.get("charter"):
                lines.append(f"      {s['charter']}")
            if s.get("claims"):
                lines.append(f"      claims: {', '.join(s['claims'])}")
        # A worktree of this repo that no session there is working in. Its session may be
        # registered in another repo, or gone; either way someone may own that branch.
        busy = {s.get("workdir") for s in v.sessions}
        for t in v.worktrees:
            if (origin and t.get("origin") == origin and t.get("path") not in busy
                    and not is_ledger(t.get("repo_root"), t.get("path"), t.get("branch"))):
                lines.append(f"  {v.host}: worktree {t.get('path')}  "
                             f"({t.get('branch') or 'detached'}), no session registered in it")
        if show_all:
            for s in rest:
                lines.append(f"  {v.host}: {s.get('name')}@{v.host}  "
                             f"{s.get('repo_root', '')}  ({s.get('branch') or 'detached'})")
        elif rest:
            projects = len({s.get("repo_root") for s in rest})
            noun = "session" if len(rest) == 1 else "sessions"
            pl = "project" if projects == 1 else "projects"
            lead = "and" if same else ""
            lines.append(f"  {v.host}: {lead + ' ' if lead else ''}{len(rest)} {noun} in "
                         f"{projects} other {pl} (`board --hosts --all` lists them)")
        elif not same and not status:
            lines.append(f"  {v.host}: no sessions")
    return "\n".join(lines) + "\n"


def split_address(target: str, hosts: list[Host]) -> tuple[str, Host] | None:
    """`name@host` → (name, Host) when `host` is a configured peer; otherwise None.

    Only a configured host routes, so an `@` in an ordinary session title stays local.
    """
    name, at, host = target.rpartition("@")
    if not at or not name.strip():
        return None
    for h in hosts:
        if h.name.lower() == host.lower():
            return name, h
    return None
