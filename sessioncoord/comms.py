"""How often a session may interrupt another one, and whether it wants an answer.

The inbox in `inbox.py` gives a message somewhere to go. This module governs whether it should
have been sent at all.

Daniel, 2026-09-18, after watching two sessions message each other for an hour while he could
not get a word in: *"chatty responses that provoke navel-gazing and self-reinforcing sycophancy
is a waste of my time and token budget... It's kind of like teaching the AIs to treat my
attention as a precious resource."*

THE EVIDENCE. One session relayed a withdrawal to another that had already been given to Daniel
directly. The second messaged back to stop the relay, arriving after it was sent. Both then
narrated the exchange to him. Four turns of his attention, no decision changed anywhere.

THE REASON A RULE WILL NOT FIX IT. Both sessions diagnosed the problem *in the same breath as
committing it* -- one wrote, in its own pane, "Each relay was defensible on its own; the
aggregate is precisely the attention-spend the whole apparatus exists to stop." Self-awareness
is not the missing ingredient. A rule phrased "ask whether this is necessary" gets a sincere
yes every time, because each individual message genuinely is defensible. That is the same shape
as this tool's own ask queue, documented and used zero times across twelve repos until
something enforced it. So: mechanism over exhortation, and a budget that bites.

WHAT THE BUDGET IS PRICED AGAINST, now that the inbox exists. Filing a message costs the
recipient nothing until it looks. Typing into its prompt makes it spend a turn reading and
usually another replying, and both sessions then narrate the exchange to Daniel. So the thing
being rationed is not "telling a peer something" -- it is *interrupting* one, which is a much
narrower claim.

Pure. Persistence is `store`'s job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

BUDGET_PER_HOUR = 3
WINDOW_SECONDS = 3600

REPLY_REQUIRED = "required"
REPLY_NONE = "none"
REPLIES = (REPLY_NONE, REPLY_REQUIRED)
NO_REPLY_MARKER = "[no reply needed]"

# Things never worth interrupting a peer for. A list, not a principle, because a list is
# checkable and "be judicious" is not.
FORBIDDEN = (
    "credit, thanks, or acknowledgement",
    "agreement that changes nothing",
    '"adopted your suggestion"',
    "status updates nobody is blocked on",
    "relaying something the recipient can already see",
    "confirming receipt",
)

THE_TEST = (
    "What does the recipient do differently in the next hour because of this? Name the action.\n"
    "If you cannot name one, it is a filed message or it is nothing -- \"they would know I "
    "appreciated the catch\" is not an action.\n"
    "Faster form: would you still send this if they could not reply? Credit, agreement and "
    "acknowledgement fail that instantly. Sequencing, claims, corrections and bug reports pass."
)


class CommsError(ValueError):
    """A message refused, phrased for the session that tried to send it."""


@dataclass(frozen=True)
class Sent:
    """One outbound interrupting message, for budget accounting."""

    session_id: str
    target: str
    at: float
    reply: str = REPLY_NONE
    override: str = ""
    id: str = ""
    # Set when this entry CANCELS an earlier one, naming its id. The doorbell is charged before
    # it is rung, so a crash between the ring and the bookkeeping cannot produce an
    # interruption nobody paid for; a ring that is then refused is undone by appending one of
    # these rather than by rewriting the log, which would race the other sessions appending to
    # it. The failure directions are not symmetric: an uncharged interruption silently raises
    # the limit, while a charge for a ring that did not happen costs one message an hour.
    reverses: str = ""
    # The recipient's SESSION ID when this ring was charged to a manager's per-worker budget
    # (`msg --worker`), else empty. A ring with this set is counted only against that
    # (sender, worker) pair and never against the sender's ordinary budget. Keyed on the
    # session id rather than the address typed, so ringing one worker by pane and by id is one
    # worker. An older build that ignores the field counts these as ordinary rings, which errs
    # toward fewer interruptions, the safe direction.
    worker: str = ""

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "target": self.target, "at": self.at,
                "reply": self.reply, "override": self.override, "id": self.id,
                "reverses": self.reverses, "worker": self.worker}

    @classmethod
    def from_dict(cls, d) -> "Sent":
        if not isinstance(d, dict):
            raise ValueError("not a mapping")
        # The ACCOUNTING fields are required; only the optional ones get compatibility
        # defaults. `{}` is syntactically valid JSON and used to become Sent("", "", 0.0),
        # which belongs to no session and is therefore ignored by `spent` -- so a corrupted
        # record silently handed back headroom instead of making the budget unavailable. That
        # is the same fail-open shape as reading an unreadable log as empty, one level further
        # in: the defaults made a damaged row look like a well-formed row for somebody else.
        missing = [k for k in ("session_id", "target", "at") if k not in d]
        if missing:
            raise ValueError(f"send record is missing {', '.join(missing)}")
        # PRESENT IS NOT THE SAME AS VALID. `{"session_id": "", ...}` satisfied the key check,
        # loaded cleanly, belonged to no real session, and was therefore skipped by `spent` --
        # handing back a doorbell that had already been recorded. The key check was the third
        # attempt at this and it still only asked whether the field was there.
        for k in ("session_id", "target"):
            if not isinstance(d[k], str) or not d[k].strip():
                raise ValueError(f"send record has an empty or non-string {k}")
        at = float(d["at"])
        # NaN compares False against everything, so `at > cutoff` was False and the record
        # vanished from the accounting; inf compares True forever, so one would sit in the
        # window permanently. Either way a single odd value silently changes the budget, and
        # neither is a time.
        if not math.isfinite(at):
            raise ValueError(f"send record has a non-finite timestamp: {d['at']!r}")
        return cls(session_id=str(d["session_id"]), target=str(d["target"]),
                   at=at, reply=str(d.get("reply", REPLY_NONE)),
                   override=str(d.get("override", "")), id=str(d.get("id", "")),
                   reverses=str(d.get("reverses", "")),
                   worker=str(d.get("worker", "") or ""))


def check_reply(value: str) -> str:
    """Validate the reply expectation. `none` is the default everywhere, deliberately.

    Every message used to look like it wanted an answer, and answering felt like good collegial
    behaviour, which is how an acknowledgement loop starts. Making the expectation explicit and
    defaulting it to `none` kills the loop mechanically rather than asking for restraint.
    """
    value = (value or REPLY_NONE).strip().lower()
    if value not in REPLIES:
        raise CommsError(f"--reply must be one of {', '.join(REPLIES)}, not {value!r}.")
    return value


def compose(text: str, reply: str) -> str:
    """The body handed to `send`. ONE LINE, ALWAYS.

    `tmuxio` refuses control characters -- a carriage return would submit the message early as
    a truncated instruction. An earlier version appended the no-reply marker after a NEWLINE,
    which made every `--reply none` message unsendable: the default setting of the default
    channel, broken, and invisible to a suite that stubs `send` and so never asks the one
    component that rejects it. Found on the first real message.
    """
    text = " ".join((text or "").split())
    if reply == REPLY_NONE:
        return f"{text}  {NO_REPLY_MARKER}"
    return text


class BudgetUnknown(RuntimeError):
    """The send log could not be read, so the budget cannot be checked.

    Distinct from "nothing spent". An unreadable log that read as empty would grant a fresh
    budget on every permission error or transient I/O failure -- fail-open accounting, where the
    failure silently removes the limit rather than surfacing it.
    """


def spent(sends: Iterable[Sent], session_id: str, now: float,
          window: int = WINDOW_SECONDS, worker: str = "") -> list[Sent]:
    """This session's interrupting messages inside the trailing window, newest first.

    `worker` selects the counter. Empty is the sender's ordinary budget, which excludes every
    per-worker ring; a session id is the manager's budget for ringing that one worker. Per
    worker rather than one pool of 3N, so a looping manager cannot spend every ring on one
    worker and its worker traffic cannot starve its messages to peers (2026-11-12).

    Overrides count. A message sent past the budget still cost the recipient a turn, so
    excluding it would let a session buy unlimited headroom by declaring each one urgent.
    """
    # Reversals are still HONOURED though nothing writes them any more: a log written by an
    # older build can contain them, and silently counting a cancelled charge would overstate
    # what a session had spent. Reading stays permissive about history; writing does not add
    # to it.
    sends = list(sends)
    cancelled = {s.reverses for s in sends if s.reverses}
    cutoff = now - window
    mine = [s for s in sends
            if s.session_id == session_id and s.at > cutoff and s.worker == worker
            and not s.reverses and not (s.id and s.id in cancelled)]
    return sorted(mine, key=lambda s: s.at, reverse=True)


def remaining(sends: Iterable[Sent], session_id: str, now: float,
              budget: int = BUDGET_PER_HOUR, window: int = WINDOW_SECONDS,
              worker: str = "") -> int:
    return max(0, budget - len(spent(sends, session_id, now, window, worker)))


def refusal(recent: Sequence[Sent], now: float, budget: int = BUDGET_PER_HOUR,
            worker: bool = False) -> str:
    """What to say when the budget is gone. Names what was spent, so triage is possible.

    Refusing without showing the messages already sent would be a wall. Showing them turns it
    into the question that matters: which of these mattered less than the one you are about to
    send?
    """
    which = (f"this hour's budget of {budget} doorbells for this worker"
             if worker else f"this hour's budget of {budget} interrupting messages")
    lines = [
        f"You have spent {which}. The next one has to be worth more than one of these:",
        "",
    ]
    for s in recent:
        mins = max(0, int((now - s.at) // 60))
        flag = "  (override)" if s.override else ""
        lines.append(f"  {mins:>3}m ago  -> {s.target}{flag}")
    lines += [
        "",
        THE_TEST,
        "",
        "If it genuinely must interrupt now, say why and it will be sent and recorded:",
        '    session-coord msg <target> "..." --override "<one line: what breaks otherwise>"',
    ]
    return "\n".join(lines)


def budget_line(sends: Iterable[Sent], session_id: str, now: float,
                budget: int = BUDGET_PER_HOUR) -> str:
    """One line for `check`, or empty when there is nothing worth saying.

    Silent below half the budget, for the same reason the board is silent when a session is
    alone: a thing that always speaks is a thing that gets skipped.
    """
    used = len(spent(sends, session_id, now))
    if used * 2 < budget:
        return ""
    left = max(0, budget - used)
    if left == 0:
        return (f"comms budget: {used}/{budget} interrupting messages this hour, none left.")
    return (f"comms budget: {used}/{budget} interrupting messages this hour, {left} left. "
            "Ask what the recipient does differently because of the next one.")
