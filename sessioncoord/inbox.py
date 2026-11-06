"""A durable queue of messages between sessions, and a doorbell that says one has arrived.

Daniel's design, 2026-09-18. Typing a whole message into a peer's prompt has three problems
that got worse the more of it we did:

* It lands mid-sentence. His own half-typed message was submitted along with a peer's, arriving
  as one turn he could not attribute -- and the submission, not the garbling, is the real harm.
* It cannot carry prose. `tmuxio._clean` refuses control characters in anything typed, so a
  multi-line message was impossible until the body moved to a file.
* A busy peer is unreachable. The busy-prompt guard correctly refuses to type over somebody
  mid-sentence, and then there is nowhere to put the message. Three retries to one peer failed
  in a row while the thing that needed saying sat in a file nobody was told about.

So the body goes in a queue the recipient owns, and the only thing typed is a constant, short
doorbell. The recipient drains at its own pace, N pending messages collapse into one signal, and
nothing is ever blocked on the state of someone's prompt.

The doorbell is a RUNNABLE COMMAND rather than a bare sigil, which is the one place this departs
from the original sketch. The thing receiving it is a language model reading its own prompt, not
a parser: a bare mark needs a decoder and would be met with "what does this mean?", routed
straight back to Daniel. `>>> session-coord inbox` needs no convention, and any session can act
on it whether or not it has ever loaded the skill.

Pure over the queue; `store` owns the files.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

# What gets typed into a peer's prompt, and the only thing that ever does. Constant, so a reader
# learns it once; a command, so a reader who has not needs no explanation.
#
# BARE, with no sigil and no leading space: `tmuxio.send` adds ` >>> ` to everything it types,
# so a constant that carried its own produced ` >>>  >>> session-coord inbox` -- not the
# runnable command this promises. The decorated form is `tmuxio`'s to build, and it builds it
# for messages and doorbells alike.
DOORBELL = "session-coord inbox"


@dataclass(frozen=True)
class Message:
    """One thing a session said to another. `read` is the recipient's state, not the sender's."""

    id: str
    sender: str
    recipient: str
    body: str
    created: str = ""
    read: bool = False
    subject: str = ""
    # "none" | "required". Metadata rather than prose: the recipient needs to know whether an
    # answer is wanted, and appending that to the body would rewrite what the sender actually
    # said. Defaulted so messages filed before this existed still load.
    reply: str = "none"
    #: The sender's stable name (its pane title, else a session-id prefix), which is what a
    #: reply should be addressed to. `sender` is display text and carries a window that will
    #: have moved by the time anyone replies.
    sender_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Message":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def unread(queue: Iterable[Message], recipient: str) -> list[Message]:
    """This recipient's unread messages, oldest first -- the order they were said in."""
    mine = [m for m in queue if m.recipient == recipient and not m.read]
    return sorted(mine, key=lambda m: (m.created, m.id))


def mark_read(queue: Sequence[Message], ids: Iterable[str]) -> list[Message]:
    """Mark specific messages read. Never bulk-clears: a drain that read only part of the
    queue must not mark the rest, or the unread ones vanish without anyone seeing them."""
    want = set(ids)
    return [Message(**{**m.to_dict(), "read": True}) if m.id in want else m for m in queue]


# Ring again if the oldest unread has been sitting this long. See `needs_doorbell`.
RING_AGAIN_AFTER_SECONDS = 600


def needs_doorbell(queue: Iterable[Message], recipient: str, now: str = "") -> bool:
    """Should a doorbell be rung for this recipient?

    Rings on their FIRST unread -- and again when the oldest unread has been waiting past
    `RING_AGAIN_AFTER_SECONDS`, which is the correction.

    The original rule was "ring only on the first unread", reasoning that a peer with mail
    waiting has already been told and a second ring only spends another keystroke. That
    reasoning silently bound "has been told" to "has unread mail", and the two stopped being
    the same thing the moment a peer did not drain its inbox.

    They did not. Within two hours two peers had accumulated three and six unread messages and
    read none; one of them then re-proposed a diagnosis that my refutation, sitting unread in
    its inbox, had already answered. Nine messages were filed, one doorbell was rung, and the
    channel had gone quiet without anyone noticing -- including me, who wrote it.

    This is the stale-claim family in the thing built to coordinate around it: a claim true once
    ("they have been told") reused after it stopped being true. The remedy here is the weakest
    of the three -- make staleness detectable by giving the claim a lifetime -- because the
    stronger ones are not available: nothing in this tool can make a peer read.
    """
    mine = unread(queue, recipient)
    if not mine:
        return False
    if len(mine) == 1:
        return True
    if not now or not mine[0].created:
        return False
    try:
        from datetime import datetime
        oldest = datetime.fromisoformat(mine[0].created)
        current = datetime.fromisoformat(now)
        # A naive timestamp and an aware one cannot be subtracted, and `TypeError` is not a
        # `ValueError`. `_now()` is aware, but a message filed by an older build, edited by
        # hand, or carried from another box may not be -- and this runs AFTER the new message
        # has been saved, so the crash lost nothing but reported a delivery as a traceback.
        # Compare on the common ground rather than refusing to compare.
        if (oldest.tzinfo is None) != (current.tzinfo is None):
            oldest, current = oldest.replace(tzinfo=None), current.replace(tzinfo=None)
        return (current - oldest).total_seconds() >= RING_AGAIN_AFTER_SECONDS
    except (TypeError, ValueError):
        return False


class Unsafe(ValueError):
    """A body or subject carrying terminal control bytes. Refused at the filing boundary."""


# Every control byte except the ones prose legitimately contains. Newline and tab are what the
# queue exists to carry -- a filed message may be a list, a diff, a block of code -- and they
# are harmless in `render`'s output, which is written a line at a time rather than typed into
# a prompt. Everything else moves a cursor, submits a line, or starts an escape sequence.
_BAD_IN_BODY = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def validate(body: str, subject: str = "") -> None:
    """Refuse control bytes before a message is filed. Raises `Unsafe`.

    The direct-send path has always filtered these (`tmuxio._clean`), and the filed path
    inherited none of it -- so the queue was the way around the guard. What `render` prints is
    read by a terminal whether a session runs it or Daniel does, and an ESC there reconfigures
    that terminal exactly as it would in a prompt.

    Refuses rather than stripping, for `_clean`'s reason: silently altering what one session
    said to another is worse than declining to carry it. The subject is held to the stricter
    rule because it is rendered inline in a header, where a newline breaks the shape.
    """
    bad = _BAD_IN_BODY.findall(body) + list(re.findall(r"[\x00-\x1f\x7f]", subject))
    if bad:
        names = ", ".join(sorted({repr(c) for c in bad}))
        raise Unsafe(
            f"message contains terminal control characters ({names}) and was not filed. "
            "Newlines and tabs are fine -- a filed message may be prose, a list or a diff -- "
            "but an escape sequence would reconfigure the reader's terminal."
        )


def _safe(text: str) -> str:
    """Escape any control byte that reached the queue before `validate` existed, or around it.

    Validation covers what this build files; it cannot cover what is already on disk, or what a
    hand-edited `inbox.json` holds. Rendering is the last boundary before a terminal sees it,
    so it escapes rather than trusting the writer.
    """
    return _BAD_IN_BODY.sub(lambda mo: repr(mo.group(0))[1:-1], text)


def render(messages: Sequence[Message], marked_read: bool = True, you: str = "") -> str:
    """The inbox as a session reads it. Full bodies -- this is the delivery, not a summary.

    `marked_read` is the caller's report of what it actually persisted, not a formatting
    choice: `--peek` deliberately leaves the queue unread, and an unconditional "Marked read."
    told the reader the opposite of the truth about their own mail.
    """
    if not messages:
        return "Inbox empty.\n"
    out = [f"{len(messages)} unread message(s), oldest first.", ""]
    if you:
        out[1:1] = [_safe(you)]
    for m in messages:
        head = f"  from {_safe(m.sender)}"
        if m.subject:
            head += f" -- {_safe(m.subject)}"
        if (m.reply or "none") != "required":
            head += "  [no reply needed]"
        out.append(head)
        out.append("  " + "-" * (len(head) - 2))
        for line in _safe(m.body).rstrip().splitlines():
            out.append(f"    {line}")
        out.append("")
    state = "Marked read." if marked_read else "Still unread -- this was a peek."
    names = sorted({m.sender_name for m in messages if m.sender_name})
    to = names[0] if len(names) == 1 else "<their name>"
    # Single-quoted for the shell, never double: the hint is meant to be pasted, and inside
    # double quotes a name carrying `$(...)` or a backtick runs (hostile pass on PR #12).
    quoted = _safe(to) if to == "<their name>" else shlex.quote(_safe(to))
    out.append(f"{state} Reply by name, not by window: `session-coord msg {quoted} "
               "\"...\"`.")
    return "\n".join(out) + "\n"
