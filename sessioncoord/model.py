"""The data every other module reasons over. Plain, frozen, JSON-round-trippable.

Nothing here touches the filesystem, git, or tmux. That is what lets `detect` be tested against
the 2026-09-17 `acme/infra` tangle as a literal, rather than by standing up four sessions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

CONDUCTOR = "conductor"


@dataclass(frozen=True)
class Presence:
    """One session's claim to be working in one repo.

    A session that touches several repos has one of these per repo, which is why `repo_root`
    is a field rather than implied by the directory the record is filed under. `workdir` is
    where it actually edits -- usually a worktree under `<repo_root>/.worktrees/` -- and the
    gap between the two is precisely what the shared-tree detector looks at.
    """

    session_id: str
    repo_root: str
    workdir: str
    branch: str | None = None
    pane: str | None = None
    window: str | None = None
    #: The session's name as Claude Code titles its pane ("conf-suite-open"). Unlike the window,
    #: it does not change when Daniel closes another window: tmux renumbers windows on close
    #: (`renumber-windows on`, which he keeps), so a stored window label goes stale and sessions
    #: told each other "infra:7" about a session that had become infra:6 (2026-10-10).
    title: str = ""
    claude_pid: int | None = None
    #: The name the record was registered with, kept beside a live title that replaced it.
    #: Set only by `cli._live`, never stored: under tmux `title` is read from the pane, which
    #: Claude Code rewrites as a conversation goes, and mail addressed to the `--name` a session
    #: registered with must still find it (2026-11-06).
    registered_name: str = ""
    # Process occupying the pane is stored in claude_pid for compatibility with old records.
    engine: str = "claude"
    charter: str = ""
    claims: tuple[str, ...] = ()
    role: str | None = None
    started: str = ""
    updated: str = ""
    transcript: str = ""
    # Which tmux server incarnation the pane id belongs to. A restarted server hands out the
    # same `%N` ids from scratch, so pane+pid alone can match a record to a session that is not
    # it -- which would let a dead session keep its claims and its conductor role, and receive
    # another session's messages. `claude-session-registry` records this for the same reason.
    # Empty means a record written before this field existed; treated as matching, so an
    # upgrade does not sweep everyone.
    tmux_server: str = ""
    # When the recorded claude process started, as `procio.start_of` fingerprints it. This is
    # the liveness proof for a session with no tmux pane -- the ordinary case on a laptop. A pid
    # alone is recycled; a pid that started at the recorded moment is the same process.
    pid_started: str = ""

    @property
    def label(self) -> str:
        """What to call this session when talking to Daniel.

        The name first, because it is stable, and the window after it, marked as where the
        session is NOW: windows renumber when one closes, and a label that was only a window
        sent sessions looking for each other in the wrong place. The window is filled from tmux
        at the moment of display (`cli._live`), never trusted from the stored record. Never key
        anything on label; `session_id` remains the identity.
        """
        name = self.name
        if self.window and name != self.window:
            return f"{name} (now {self.window})"
        return name

    @property
    def name(self) -> str:
        """The stable handle: the pane's title, else a session-id prefix. Never the window."""
        return self.title or self.session_id[:8]

    @property
    def in_main_checkout(self) -> bool:
        return self.workdir.rstrip("/") == self.repo_root.rstrip("/")

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("registered_name", None)  # derived at read time; see the field
        return json.dumps(d, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Presence":
        known = {f for f in cls.__dataclass_fields__}  # tolerate fields a newer version added
        kw = {k: v for k, v in d.items() if k in known}
        if "claims" in kw and kw["claims"] is not None:
            kw["claims"] = tuple(kw["claims"])
        return cls(**kw)

    def with_(self, **kw: Any) -> "Presence":
        return replace(self, **kw)


@dataclass(frozen=True)
class Finding:
    """Something the board noticed. Ordered by severity, then by code, so output is stable."""

    code: str
    severity: str  # "block" | "warn" | "note"
    summary: str
    detail: str = ""
    sessions: tuple[str, ...] = ()  # labels, for talking to a human
    remedy: str = ""
    # The working trees this finding is ABOUT. The PreToolUse gate asks on behalf of one
    # session and must not refuse a session that is safely in its own worktree because two
    # others share the main checkout -- that would deny precisely the sessions that complied.
    # Empty means repo-wide: it applies to everybody here.
    workdirs: tuple[str, ...] = ()

    _RANK = {"block": 0, "warn": 1, "note": 2}

    @property
    def rank(self) -> int:
        return self._RANK.get(self.severity, 99)


@dataclass(frozen=True)
class Ask:
    """One thing a session needs from Daniel, in the one queue he reads.

    `kind` follows his chat-voice tags: A means he must do something outside the chat, Q means
    a reply discharges it. `text` must stand on its own -- the 2026-09-17 failure was sessions
    asking him about `@idymqyp3` and `Q-8RJN` as though he shared their context.
    """

    tag: str
    kind: str  # "A" | "Q"
    text: str
    session_id: str
    window: str = ""
    created: str = ""
    state: str = "open"  # "open" | "answered" | "dissolved"
    depends_on: tuple[str, ...] = ()
    answer: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Ask":
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        if "depends_on" in kw and kw["depends_on"] is not None:
            kw["depends_on"] = tuple(kw["depends_on"])
        return cls(**kw)


@dataclass(frozen=True)
class RepoFacts:
    """What git says about the repo right now. Gathered once, passed in, never re-queried.

    Keeping this separate from Presence is what stops a detector from shelling out mid-test.
    """

    repo_root: str
    default_branch: str = "main"
    local_default_sha: str = ""
    remote_default_sha: str = ""
    dirty_paths: tuple[str, ...] = ()
    branches_with_commits: Mapping[str, int] = field(default_factory=dict)
    #: False for a plain folder used as a workspace. The git-shaped hazards -- `git add -A`,
    #: branch switches, worktrees -- do not exist there, and remedies naming them are noise.
    is_git: bool = True
    #: The repo's owner has declared, in a file on the remote's default branch, that sessions
    #: share the main checkout on purpose. See `gitio.SHARED_CHECKOUT_MARKER`.
    shared_checkout: bool = False

    @property
    def default_branch_in_sync(self) -> bool:
        if not self.local_default_sha or not self.remote_default_sha:
            return True  # unknown is not a finding; say nothing rather than cry wolf
        return self.local_default_sha == self.remote_default_sha


@dataclass(frozen=True)
class Board:
    """Every live session in one repo, plus what git says about it. The detector input."""

    repo_root: str
    sessions: tuple[Presence, ...] = ()
    facts: RepoFacts | None = None

    @classmethod
    def of(
        cls,
        repo_root: str,
        sessions: Iterable[Presence],
        facts: RepoFacts | None = None,
    ) -> "Board":
        return cls(repo_root=repo_root, sessions=tuple(sessions), facts=facts)

    @property
    def conductor(self) -> Presence | None:
        for s in self.sessions:
            if s.role == CONDUCTOR:
                return s
        return None

    def others(self, session_id: str) -> tuple[Presence, ...]:
        return tuple(s for s in self.sessions if s.session_id != session_id)

    def by_id(self, session_id: str) -> Presence | None:
        for s in self.sessions:
            if s.session_id == session_id:
                return s
        return None


def order_findings(findings: Sequence[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.rank, f.code))
