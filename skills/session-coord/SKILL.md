---
name: session-coord
description: Coordinate with the other Claude Code or Codex sessions working in this project — see who else is here and what they are doing, claim the files you will write, claim or hand off the conductor role that sequences them, file a question into the one queue the user reads, message a peer session directly instead of asking the user to carry a paste, and harvest loose ends before a session closes. Use when the session-start notice reports peers, when you are about to write a file another session claims, when you would otherwise ask the user to relay something between sessions, when two sessions' work needs sequencing or their conclusions conflict, or when this session is finishing. Mechanics live in the `session-coord` command; this skill holds the judgement.
---

# Coordinating with the other sessions in this project

The installed hooks register you at session start, deliver peer mail at the start of each turn, and refuse a git staging command in a working tree another live session shares. Claude's close hook also looks for loose ends; Codex's close hook removes its presence record. This skill is for the parts that need judgement.

**First, once per session: run `session-coord addendum`.** If it prints anything, those are this machine's local rules — the user's own trackers, conventions and pipelines — and where they are more specific than this file, they win.

## Why this exists

On 2026-09-17 four sessions were working in one checkout of a private infrastructure repo. One merged `origin/main` into a branch two others had been committing to, not knowing it was shared. Another's working tree held a third's uncommitted audit, and it commits with `git add -A` — one more commit and it would have landed a stranger's work under its own message. Every session behaved correctly in isolation: each hit a wall, each refused to guess, each escalated to the human. The aggregate was an hour of him carrying prompts between windows, and his verdict was *"Three well-behaved sessions producing one badly-behaved system, because nobody was sequencing and you were the only channel between them."*

The same shape shows up without any code. Two sessions researching one market reach different numbers for the same figure and each writes its own into the shared synthesis. Two sessions drafting one memo overwrite each other's last edit. A session asks the user which of two findings is current when the session that produced the newer one is running in the next window. Sequencing is the missing role, not better behaviour.

## The rule that outranks everything here

**The user's attention is the scarce resource.** Before you ask them anything, ask whether a peer session could answer it instead — and if it could, ask the peer.

## Orient first

```
session-coord board          # who else is here, their charter, their claims
session-coord check          # what is currently wrong
session-coord conductor show # who is sequencing
```

If you are alone, all three are nearly silent and you should carry on. Nothing below applies.

A *project* here is the git repository you are in, or, outside git, the nearest folder above you that contains a `CLAUDE.md`, a `.claude/` directory or an empty `.session-coord` marker file — else the folder itself. Sessions see each other only when they resolve to the same project, so if two sessions that should see each other do not, put a `.session-coord` file in the folder that contains them both.

## Say who you are, what you are for, and what you will write

```
session-coord register --name "tam-sizing" \
  --charter "One paragraph: the question this session is answering and roughly where it will stop." \
  --claim 'drafts/market-sizing.md' --claim 'data/tam/**'
```

`--name` is how peers address you. Under tmux the pane title already serves; outside tmux, give yourself a short stable name the first time the board shows peers. `--session` is filled in automatically.

**Claims are how the collision nobody can see gets caught.** In git, a file can read identically on both sides of a merge and still be wrong, because two sessions each made an edit that is right alone and contradictory together; in a folder of drafts there is not even a merge to look at. Claim the files you will *write* — the synthesis, the memo, the deck, the file carrying a count or a list — not every file you might read. One session owns each output document. If you need a change in a document another session claims, message that session rather than editing it.

**A claim means you get told, not that you hold the pen.** If a peer needs one line in a file you claimed, the answer is usually "go ahead, tell me when it lands". Claim to be consulted, not to block.

**Registration is a snapshot.** Re-register when your charter changes, and in git after you move to a worktree — `register` warns you when the directory it has recorded is not the one you are in.

## In a git repo: one working tree per session

When peers share a git repo, each session should work in its own worktree: `git worktree add <repo>/.worktrees/<topic> -b <topic> origin/main`, then move the *session* there. In Claude Code use the **`EnterWorktree`** tool, not a shell `cd`. In Codex, start the session in the worktree and register there. The gate is handed the session's directory, so a shell `cd` leaves you looking, to the gate, like a session still sitting in the shared checkout — refused, and shown a remedy you think you have already followed.

Where a repo shares its checkout on purpose (it carries a `.session-coord-shared-checkout` file, and the board calls the shared tree a warning), or where you only need to commit files you wrote: name every file. `git add a.md b.md && git commit -s -m '...'` and `git commit -s -m '...' -- a.md b.md` both pass a shared tree, as long as no peer in it has claimed those files and, for the first form, nothing else is staged. Claim your files when you start (`--claim`), because a claim is the only thing that tells the gate a file is yours rather than a peer's. In a repo that shares its checkout on purpose, the gate also refuses a commit of any file this session has not claimed, since two sessions may have edited it and an explicit commit would carry both sets of edits. To add a file to what you already claim, run `session-coord claim -- <file>`. It appends, whereas `register --claim` replaces the whole list. If a refusal gives you a corrected command, run that command; it is built from yours.

If you need to commit into a different repository from the one the session started in, use `git -C /literal/path …` or `cd /literal/path && git …` with a literal path. The gate checks only the tree those name. With `;` instead of `&&`, a path held in a shell variable, or `bash -c`, it cannot be sure where the write lands and checks your session's own tree as well.

## The conductor

One session per project sequences the others. Claim it if you have the widest view:

```
session-coord conductor claim
```

**Holding it means:** peers route ordering questions to you rather than to the user; you decide what lands first when several pieces of work are in flight; you **adjudicate contradictions between sessions' outputs** — two figures for one number, two readings of one clause; and when you must go to the user, you go **once**, with a single ordered list.

**Not holding it means:** when you hit a sequencing question, or find that your conclusion contradicts a peer's, send it to the conductor, not to the user.

Release it before you close (`session-coord conductor release`), or hand it over explicitly. A conductor that vanishes silently puts the user back in the middle.

## When the tooling itself misbehaves, tell the conductor

If `session-coord` refuses something you believe is legitimate, that goes to the conductor first. **Report the observation; do not attach a cause.** Say what you ran, what you expected, what happened, and what you checked. A session can see one layer of a system it did not build, and the cause it infers is plausible and usually wrong. If there is no conductor, or it is the problem, then the user — but say that you tried.

## Asks: one queue, self-contained, ordered

```
session-coord ask add --tag Q-ZNYN --kind Q \
  --text "Use the 2025 or the 2026 analyst figure for the TAM baseline? They differ by 18%."
session-coord ask list
```

- **An ask must stand on its own.** The user does not share your context. Restate the subject inside the ask; an identifier only the sessions understand makes them reconstruct the context before they can answer. The command refuses a bare identifier.
- **Say what blocks what.** Use `--depends-on` so the list orders itself. Not knowing whether to answer one question first or go and do something else first is the thing that wastes the most time.
- `--kind A` is something the user must go and do outside the chat; `--kind Q` is something a typed reply discharges. Fifteen minutes versus one word — never conflate them.

When a peer's work makes your question moot, retire it yourself: `session-coord ask dissolve --tag Q-ZNYN`.

## Talk to peers directly

```
session-coord msg "legal-memo" "tam-sizing here. The TAM figure in drafts/market-sizing.md moved from 3.2B to 4.1B (new source, cited inline). Your section 3 quotes the old one."
```

**Name sessions by name**, quoted if it has spaces. A body of `-` is read from stdin (`session-coord msg <peer> - < note.txt`), for prose a command guard would otherwise inspect. The body is filed in the recipient's inbox, including when that peer is in another repo, and a hook delivers it into their context at the start of their next turn. A name not registered anywhere gets held mail delivered at that session's next turn; the budget only decides whether to ring, never whether to file. A busy peer is a delay, never a failure, and the message can be prose of any length.

Under tmux, a short doorbell (` >>> session-coord inbox`) is also typed into the peer's prompt, which wakes a session that would otherwise sit idle. A Codex pane is rung only when its `›` input is visibly idle; an unreadable Codex prompt leaves the mail filed for its next turn. Outside tmux there is no doorbell: the message waits for that session's next turn, and an idle session reads it when the user next switches to it. If a peer needs to act *now* and is idle, that is the one case to tell the user, in one line: "switch to *legal-memo*; it has a message that changes its section 3."

Say who you are in the message. Do **not** use messages to poll peers for opinions.

### Before you ring: what does the recipient do differently?

Doorbells are budgeted at three per hour per session; filing is not. Past the budget a message is still filed and delivered, just without the doorbell. Before sending, answer: **what does the recipient do differently because of this? Name the action.** Corrections, sequencing, claims and changed facts pass. Thanks, agreement that changes nothing, status nobody is blocked on, and confirming receipt fail, always.

**Replies are opt-in.** `--reply none` is the default and the body carries `[no reply needed]`. Answering one costs two turns and buys nothing.

### Never narrate peer traffic to the user

Do not report what you said to a peer, or what a peer said to you, unless it changes a decision the user holds. Coordination is infrastructure: they see its results, not its transcript.

## Serializing an operation

```
session-coord lock publish --owner "tam-sizing exporting the deck" -- ./export-deck.sh
session-coord lock publish --status
```

Runs a command under a named per-project lock, so two sessions cannot run the same shared-state operation at once — a merge, a publish, a regeneration of a shared file. Exit 75 means another session holds it: retryable, not a failure. Report it and wait; never route around it.

## Before this session closes

```
session-coord harvest --quiet
```

It scans this session's transcript for tagged items — `[ F-5CSC ]`-style ids for findings, problems, recommendations, asks and questions — that were raised and never answered or cited. **It is a net, not a verdict.** If you do not use tags it will find nothing, and the job is yours by hand: list what this session found or decided that is recorded only in the conversation, and put each somewhere that outlives it — the project's notes, a tracker, or an ask. Then release the conductor role if you hold it.

The reason: a session closed carrying a task that was done but still open, a contradiction between two sessions neither had seen, and a change the user had made that was recorded nowhere. *"The major work products get captured, but minor details are left dangling."*

## When you find a tangle

Escalate early and escalate **once**. Do not hand the user a coordination job — that is the mistake that produced this tool. Work out the sequence yourself, name the one thing you need from them, and say plainly what they should do first. If several sessions are involved, message them yourself rather than asking the user to carry pastes.

The shape that worked, once somebody finally did it: one change landed everything, two sessions closed, and the user's entire part was the word "go".
