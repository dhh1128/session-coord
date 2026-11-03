# session-coord — design

`SKILL.md` holds judgement for the session doing the work. This holds the decisions and why, for whoever comes next — including the reversals, because a design doc that records only the final state hides the most useful part.

## The incident

On 2026-09-17 four Claude sessions were sited in `acme/infra`, all in one checkout. One merged `origin/main` into a branch two others had been committing to, not knowing it was shared. A second's working tree held a third's uncommitted audit — 44 lines of `inventory.md` plus two untracked files — and that session commits with `git add -A`; one more commit and it would have landed a stranger's work under its own message, silently. Seventeen commits sat on one branch split 10 / 4 / 3, and three belonged to nobody: afterwards no one could say who wrote them.

Every session behaved correctly in isolation. Each hit a wall, each refused to guess, each escalated to Daniel. The aggregate was an hour of him carrying prompts between tmux windows by hand, and his verdict is the design brief: *"Three well-behaved sessions producing one badly-behaved system, because nobody was sequencing and you were the only channel between them."*

So the thing being conserved is not correctness of commits. It is Daniel's attention.

## The distinction everything hangs on

**The hazard is the shared working tree, not the shared branch.** Four sessions on four branches in one checkout has the identical `git add -A` bug, because a working tree holds one branch's files at a time and the sessions still share them. Worktrees are the fix precisely because each gets its own files. `CLAUDE.core.md` states this as a global default; this package is what makes it observable.

A corollary that took eleven review rounds to surface: `git pull` is a write. The incident *was* a session merging `origin/main` into a shared branch, and the gate built from that incident did not gate the command that causes it, because attention kept going to exotic plumbing verbs while nobody re-read the porcelain list against the founding event.

## Decisions, with the reasoning

**State lives in `~/.local/state/claude-coord/`, not in the repo.** Daniel's call. A session touches several repos, so per-repo state fragments the thing that has to be global; `.ignored/` multiplies once per worktree and dies when the worktree is removed, taking the record of who was working there with it; and one state root answers "who is where across the estate" with a single scan rather than a walk of fifty repos.

**Presence is a liveness fact, not a lock.** `flock` was the first instinct and is wrong here twice over: the Bash tool spawns a fresh shell per call, so nothing can hold a lock across a turn, and a lock nobody blocks on is just a file. The predicate is positional instead — the recorded pid must still be the claude process in the recorded pane, within the recorded tmux server incarnation. A pane id means nothing across a server restart, and pids get recycled.

**A merge *is* the lock case, and that is where `flock` belongs.** `pr-merge run` is one long-lived process that can hold a lock for its whole life, and a second merge genuinely should wait. `locks.py` covers the merge-and-cleanup tail, never the CI wait — that is where the minutes are, and serializing it would throw away the parallelism that makes running several sessions worthwhile.

**The conductor is a role, not better behaviour.** No amount of session discipline substitutes for somebody sequencing. On the day, three sessions each escalated correctly and produced a badly-behaved system; one asked Daniel for permission to deviate from a rule he had set an hour earlier, which was the right instinct aimed at the wrong recipient. Exactly one holder at a time, enforced under a lock, because two conductors is worse than none — peers would route the same question to different arbiters and nothing would surface the split.

**Silence in the common case is a contract.** One session alone in a repo is almost every session, and every command produces exactly nothing there. A board that speaks when there is nothing to say gets muted, and a muted board protects nobody. `repo-status -q` already works this way for the same reason, and there are tests asserting the silence.

## The rule that kept being rediscovered

**A function that answers a question about the world must be able to say "I could not look", and every caller must treat that differently from "nothing is there".**

This arrived five times through five different doors, across eleven review rounds, and each fix that named a *source* rather than the *property* left it broken everywhere else: a swallowed tmux error; an unreadable `/proc` listing; a malformed pane row; a refused per-process `stat`; a refused `comm` read. Each time the collapse produced the same catastrophe — `sweep` deleting every live presence record, after which the gate reported no peers and permitted exactly the commit it exists to refuse.

It reappeared a sixth time in the storage layer, where it is subtler: a *tolerant reader that loses data*. `load_asks` returned `[]` for a file it could not parse, and the next write saved that empty list over it. A reader that cannot distinguish "nothing here" from "I could not read this" hands its caller a licence to destroy the thing it failed to read.

## The gate has two failure directions and they are not symmetric

A missed write is the collision this package exists to prevent. A false refusal blocks legitimate work — and a gate that blocks legitimate work gets switched off, after which it misses *every* write. So the false refusal is the more dangerous failure, and several decisions follow from that ranking.

The checker tokenises with `shlex` rather than matching a regex, after five rounds in which a pattern was widened five times and grew a new hole each time; a regex cannot see quoting, and those were not five bugs but one wearing five hats. Line splitting is quote-aware and skips heredoc bodies, because splitting on every newline refused a peer's message for spanning two lines and then refused the commit of its own fix. Unparseable input is not automatically a write — it scans for a verb first — because denying what was never identified as a write refused ordinary multi-line commands with no git in them. And the question is always scoped to a named tree, never asked repo-wide, because a repo-wide answer includes the main checkout's collision and therefore denies the very sessions that took worktrees.

The residual gap is deliberate: a write into a tree nobody named, such as `cd $VAR && git add -A`, is missed. That is the right trade against denying every compliant session on every ambiguous command.

## Explicit paths pass a shared tree, 2026-11-02

The gate refused every staging command in a shared tree, and that turned out to be broader than the hazard. In a docs repo whose owner commits straight to main from the main checkout, two sessions were registered. One ran `git add products.md components.md && git commit -s -m ...` and later the same shape for two roadmap files. The gate refused both. The other session's only uncommitted change was a third file that neither command named. Daniel ran the commits himself, through a script, and the refusal told the session to take a worktree, which that repo's own rule forbids.

The 2026-09-17 hazard is a broad pathspec: `git add -A` reaching a stranger's audit. A list of files the caller names and nobody else claims cannot reach anything, so refusing it cost a round trip and bought nothing. The gate now parses `git add` and `git commit` and lets them through when every path is explicit and no peer in the same tree claims one. Everything it cannot read refuses as before. That covers `-A`, `-u`, `.`, `commit -a`, directories, globs, magic pathspecs, `--pathspec-from-file`, `--amend`, any option not on its allow-list, a write into a tree it cannot name, and any other write verb.

A bare `git commit` was the subtle case. It commits whatever is staged, so on its own it can carry a peer's staging under your message, and it stays refused. But the incident's commands were exactly `git add <files> && git commit`, which is the ordinary way to commit and is safe when the index holds nothing else. So the gate reads the index for that one shape and allows it only when the staged set is a subset of what the same command adds. The read is fail-closed, and there is a window between the read and the commit in which a peer could stage something. That gap is accepted: it needs a peer to stage in the same second.

"Modified by another session" is approximated by claims, because nothing records which session touched a dirty file. An unclaimed file a peer has edited will pass. The board's advice to claim files before editing them is what closes that, and the refusal names the peer when a claim is what stopped it.

**The opt-out is a file on the remote.** A repo that shares its checkout on purpose commits `.session-coord-shared-checkout` and pushes it to the default branch. That downgrades shared-tree to a warning and replaces the worktree remedy with the explicit-path one. It deliberately does not loosen the gate: the hazard in such a repo is identical, and the explicit-path rule is what makes sharing survivable there. Reading it from `origin/<default>` rather than the working tree is the point. A session can create a file, and can commit one, without anybody noticing. Getting it onto the remote's default branch is an act the owner sees.

The first hostile pass on the PR found five ways a command that looked explicit was not, all reproduced: a dash-prefixed path that broke the board query and read as "no claims"; shell brace expansion; git's own pathspec switches (`--icase-pathspecs`, `GIT_GLOB_PATHSPECS`, a backslash escape); glob claims that prefix overlap never interpreted; and a deleted tracked directory, which is no longer on disk but is still a directory to git. Each is now refused or matched, and claims are compared casefolded. The general lesson is that the gate sees one spelling of a path, while git and the shell each rewrite it, and "explicit" has to hold after all of them have had their turn.

The session that hit the original incident then hit a second one: `cd ~/code/tessera/brand && git add <files>` was refused using the docs repo's finding, a repo the command never touched, and was told to take a worktree there. A literal `cd <dir> && …` is now resolved exactly as `git -C` is, because `&&` guarantees the rest runs only in that directory. `;`, `||`, variables and substitutions stay unresolved. When the gate still cannot name the tree, the refusal says so and offers `git -C /literal/path` rather than worktree advice that is about the wrong repo.

## The gate follows a leading `cd` across lines, 2026-11-03

A session launched in a devenv checkout that a second live session also had open did its work in a worktree of a different repo, claude-zamu, and committed there with `cd <claude-zamu worktree> && git add -A && git commit -qs -F - <<'EOF'`, the message on the following lines. The gate refused it with devenv's shared-tree finding, though nothing in devenv was at risk and nobody else was in claude-zamu. The `cd` rule above already covered this shape on one line; it gave up on any multi-line command, and a heredoc commit message is the ordinary way to write one here. The refusal's advice, EnterWorktree, could not help either, because EnterWorktree only enters worktrees of the session's own repo. A session in repo A had no way to commit in repo B.

The decision: a leading literal `cd` or `pushd` is followed when it ends in `&&`, `;` or a newline, and the rest of the command, every later line included, is judged in that directory. Each resolved tree is put to the board, which resolves it to its toplevel and repo, and the command is refused only if that tree is shared. `git -C` was already handled this way. A second leading `cd` is followed in turn, so `cd mine && cd shared && git add -A` is still refused.

This reverses the earlier exclusion of `;`. The argument for excluding it was that the rest runs even when the `cd` fails, in the session's directory. The resolver now requires the target to exist and be searchable, and with a literal name and no options a `cd` to such a directory does not fail, so `;`, a newline and `&&` all leave the rest running in the same place. The same assumption is what licenses following the `cd` onto later lines, which run after it whatever joined them. A relative name while `CDPATH` is set is not followed, because bash would search `CDPATH` first.

Everything less certain falls back to the session's directory plus every directory the command names, which is today's behaviour: `$VAR`, a backtick, `$(…)`, a subshell, `||`, a pipe, a line continuation inside the `cd`, a `cd` that is not the first command, and an option or a `+N` stack argument. Two alternatives were rejected. Asking both the `cd` target and the session's directory would have kept the refusal in exactly the case that prompted this. Tracking every `cd` through the whole script would follow `cd` inside `if` blocks and functions, which this parser does not model, and a gate that guesses at control flow fails open.

Not verified: the check that a `cd` cannot fail was not tested against a directory removed between the hook running and the command running, which is a race of milliseconds. The hook was probed against the real board on 2026-11-03 in both directions (a commit into claude-zamu passes, the same command aimed at devenv is refused), by piping a payload to it rather than through a real Bash tool call.

## Messaging

Typing into a peer's prompt is the mechanical form of what Daniel was doing by hand, and he authorised it explicitly. It is also the most alarming capability here, so it refuses more than it accepts: control bytes are rejected rather than stripped, text goes out with `send-keys -l` because tmux otherwise parses key *names* out of the message, both tmux calls are checked, and the target must still hold the expected claude process in the expected server.

**The prompt is read before typing, and a busy one refuses.** A peer's message once landed in Daniel's half-typed sentence and both were submitted as one turn; the confusion was the lesser harm, since the Enter also sent something he had not finished writing. An *unreadable* prompt does not refuse, deliberately — the glyph is a TUI detail that will change, and failing closed would silently kill peer messaging estate-wide, which is worse than an occasional collision that the sigil at least makes legible.

**The body is filed in an inbox and only a doorbell is typed.** Filing happens first and ringing second, which is the whole point: the message is delivered when it is filed, and the doorbell only shortens how long it sits unread. A busy or unreachable peer becomes a delay rather than a failure. The doorbell is a runnable command rather than a bare sigil because the recipient is a language model reading its own prompt, not a parser — a bare mark needs a decoder and would be met with "what does this mean?", routed straight back to Daniel.

## Deliberately not done

No MCP server: it would cost tool schemas in every session's context for something that matters in a fraction of them, and needs a process kept alive across reboots. No daemon, for the same reason. Messages are swept by age and never deleted on read, because the pointer outlives the reading and a third session may be sent the same path. The shim in front of the checker is an optimisation only — it was briefly load-bearing for correctness, which is now fixed and commented, so it can be changed freely.

## Reversed

**The ask queue.** Designed as the single ordered list of things Daniel owes an answer to, documented in the skill, and never used once — `asks.json` existed in none of twelve repos with state, including by the session that wrote it. It did not fail because it was per-repo or limited to two tag kinds. It failed because nothing ever prompts a session to file. `tagged` absorbs it and fixes the actual cause structurally, by making `tagged add` the sole source of ids so that minting and filing are one act rather than two.

**A test can pin a wrong decision.** `test_a_malformed_ask_queue_degrades_instead_of_raising` asserted, in green, precisely the behaviour that caused the data loss above, and did so for two rounds. When a test fights a fix, check which of them is wrong.


## The doorbell budget, and what it is priced against

Added after the inbox, by the session that had built a competing one and deleted it.

Daniel, 2026-09-18: *"chatty responses that provoke navel-gazing and self-reinforcing
sycophancy is a waste of my time and token budget... It's kind of like teaching the AIs to treat
my attention as a precious resource."* The episode: one session relayed to another something it
had already told him directly; the second messaged back to stop the relay, arriving after it
was sent; both then narrated the exchange to him. Four turns of his attention, no decision
changed.

**Why a budget and not a rule.** Both sessions diagnosed the problem *in the same breath as
committing it* — one wrote, in its own pane, "Each relay was defensible on its own; the
aggregate is precisely the attention-spend the whole apparatus exists to stop." Self-awareness
is not the missing ingredient. A rule phrased "ask whether this is necessary" gets a sincere yes
every time, because each individual message genuinely is defensible. Same shape as this tool's
own ask queue, documented and used zero times across twelve repos until something enforced it.

**The inbox changed what should be rationed, and improved the design.** The original proposal
refused the send outright past the budget. With filing in place that would throw a message away
for being the fourth. So the budget gates the **doorbell only**: over budget, the message is
still filed and still delivered, and only the interruption is withheld.

**The doorbell is charged on ATTEMPT, and never refunded.** An earlier version refunded a ring the terminal refused, so an unreachable peer cost nothing. That nicety produced three review findings in a row, each fix more elaborate than the last: charging after the ring left a window in which an interruption was free; charging before it and refunding needed an append-only reversal; and the refund was itself wrong, because `SendRefused` covers both *nothing was typed* and *the text was typed but the submit keystroke failed* — refunding the second leaves a doorbell sitting in the peer's prompt, uncharged, which interrupts them the moment they submit. Charging on attempt removes the window, the reversal and the ambiguous outcome together, and prices the thing that was actually spent. It costs a sender one doorbell an hour when a peer is unreachable, which is visible in the log and gone within the hour.

**Decisions.** Three per session-hour, small enough to force triage. The refusal names the three
already rung, because refusing without showing them is a wall while showing them asks the
question that matters. `--override` sends past it and still counts, so urgency cannot buy
headroom. An unattributable send is not budgeted — refusing would block a legitimate message
over bookkeeping, and charging it to a guessed session would spend somebody else's. The budget
line rides on `check`, silent below half and always silent under `--quiet`, which is the gate's
contract for "blocking findings only".

**Replies are opt-in**, `none` by default, with the marker in the body rather than left to
convention — because the convention is what failed: answering felt collegial and cost a turn on
both sides.

## Mail is delivered into the turn, not waited for

`claude/hooks/session-coord-mail` drains the inbox on every `UserPromptSubmit` and puts it in
context. Daniel, on being told nothing prompted a session to read its inbox: *"Shouldn't the
session check its inbox at the start of every turn?"*

It uses `inbox --quiet`, which already prints nothing when empty — so no new rendering path was
needed, and a hook that runs on every prompt in every session stays silent in the common case.

This sharpens the two channels rather than collapsing them. **The doorbell wakes a parked
session; the hook serves one that was going to take a turn anyway.** Both are needed, and saying
which is which is what stopped a second inbox from being built.

## A duplicate implementation that was deleted rather than merged

This subsystem was built twice, independently, within an hour, because two sessions each had
something to say and no way to know the other was already saying it. The other implementation
had notes, an inbox, a `--hook` render path and its own store — roughly the same surface.

It was deleted. Its author's reasoning, recorded because the instinct to merge both was strong:
the filing half here is better on the case it never handled — a busy peer whose prompt guard
refuses the send, leaving the message nowhere to go — and two mechanisms for one job is worse
than either alone. What survived from it is the half this branch did not have: the budget, the
reply expectation, the discipline sections above, and the delivery hook.

The `--repo` normalisation fix arrived the same way and landed here first: `_resolve_repo` took
an explicit `--repo` literally, so a session passing `--repo "$PWD"` from inside a worktree keyed
its state on the worktree, registered on a private board, saw no peers and was invisible to
them. Silent isolation, reached by using the tool as documented.

## Names, not window numbers, and the window read live (2026-10-10)

Daniel keeps tmux's `renumber-windows on`, because double-digit window numbers are awkward to
type, and he closes a window whenever its session finishes. So window numbers shift during the
day. The board stored the window at registration and used it as the session's label, and
sessions copied those labels into their own context. Within one day this went wrong twice. A
session in infra:6 introduced itself to a peer as infra:7. Another reported it had no
response "from infra:7" while it was itself running in infra:7. Daniel, reading both, could
not tell which window to go to.

Three changes. The window is now read from tmux every time a record is displayed or matched
(`cli._live`), and the stored one is only a fallback when tmux cannot be consulted. A record
whose pane now holds a different claude is left alone, because that pane belongs to somebody
else and the sweep is what retires the record. The stable handle is the pane's title, which
Claude Code sets to the session's name. The board prints `name (now window)`, `msg` accepts
the name, and messages carry the sender's name so that the reply hint names it. And the inbox
opens by telling the reader who it is and which window it is in now.

Rejected: `renumber-windows off`, which makes numbers stable at the cost of gaps. Daniel
declined it. It would also not have fixed the case of a number reused after a close.

The same day also fixed the busy-prompt check. Claude Code shows a suggested next prompt in dim
text on an empty prompt line, and the check read it as somebody typing. Every idle peer then
refused the doorbell, and Daniel was told to press Enter on sentences he had never written. The
capture now keeps attributes and ignores dim text.

## Known gaps

**Two reads cannot be made consistent by ordering them more carefully.** The mail delivery handshake was wrong in three consecutive review rounds, and each fix added a call rather than making one call sufficient: render-and-clear together lost mail on a crash; peek-then-drain let a message filed in the gap be cleared undelivered; peek-bodies, peek-ids, ack-by-id reintroduced the same bug between its own two peeks. `inbox --ids` now returns ids and bodies from one lock-held snapshot. When two operations must agree about a snapshot, they have to be one operation.

**A missing file is empty; everything else raises.** Four rounds of review on PR #12 found the same defect four times, each a level further in: an unreadable log read as empty, a malformed line skipped anywhere rather than only as a torn final append, a record that parsed cleanly and belonged to nobody, and a timestamp of `NaN` that compares false against every bound and so leaves the window silently. In every case damage was representable as absence, and the accounting reported less than had happened -- the direction that removes a limit rather than enforcing one. `inf` is the mirror: it sits in the window forever and jams the budget at zero. The rule belongs in this module's head, not in its fourth review round.



Tracked as ticks in the private repository this was extracted from, rather than described here, so they cannot drift from the truth (the ids are meaningful only there): `~7zua` analyser bypasses, `~7ctg` the checker treating its own timeout as success, `~7wnu` the messaging race across three separate observations, `~7yjt` harvest noise, `~5in5` the three hand-maintained append arrays in `install.sh`, `~2plc` a Stop-hook scan for unfiled ids, `~2xfh` claim-overlap reporting delegation as collision, `~4jtt` the inbox orphaning mail when a session id changes, `~7tpx` the board trusting a recorded workdir that may no longer exist.

That last pair was found after the branch's last commit and is not fixed here. The two were minted as tagged items in a PR body first, which is the drift this section is supposed to be immune to: a gap named in prose on a pull request is invisible to the next session, and the list that was supposed to be authoritative did not contain them. A gap gets a tick id the moment it is named, or it does not count as tracked.

## A note on how the defects were found

Eleven rounds of automated review found 83 real defects in this code and none of the three that made the tool unusable in practice. Those three — the gate refusing peer messages, messages landing mid-sentence, and compliant sessions being blocked — turned up within an hour of it going live, from four sessions actually using it.

Worth pairing with the other pattern from that hour: every peer report of a symptom was accurate, and every cause attached to one was wrong, including mine. A session sees one layer of a system it did not build. Take the observation and re-derive the cause.

## Ported to a plugin, 2026-10-12

The tool was extracted from a private dev-environment repository so it could be shared, first with a product manager who uses Claude Code for research and writing rather than code. He uses a MacBook, probably without tmux, and many of his projects are folders rather than repos. Measured against that user, the original did nothing at all, and said nothing about doing nothing. The SessionStart hook returned early without a tmux pane. Liveness had no way to call a pane-less record live, so it would have been swept even if it had registered. Every command stopped at "not a git repo". Nothing failed. It was simply absent, which is the worst way for a coordination tool to fail.

**Identity without tmux.** A session is the `claude` process above whatever is asking (`procio.claude_ancestor`). A recorded pid counts as that session only if the process holding the pid now started at the recorded moment (`pid_started`). A bare pid would let a recycled number keep a dead session alive. That is the same reason the tmux path matches pid against pane rather than trusting either alone. `/proc` is read where it exists, and `ps -o ppid=,args=,lstart=` is used where it does not; those fields behave the same on procps and BSD. Each liveness proof fails open on its own source, under the rule already stated above: being unable to observe something is not observing its absence.

**`--session` defaults to this session.** The skill had documented `--session "$CLAUDE_SESSION_ID"`, but nothing exports that variable to the Bash tool, so on a stock install the documented command passed an empty id. The CLI now finds the caller's own record, by pane under tmux and by claude pid outside it.

**A folder is a workspace.** The key is the git main checkout when there is one. Otherwise it is the nearest folder above the session that carries a marker (`.session-coord`, `CLAUDE.md` or `.claude/`), stopping short of `$HOME`, because `~/.claude` exists on every machine that runs Claude Code. Failing those, it is the folder itself. Keying on the exact cwd was rejected: sessions in `Acme/` and `Acme/legal/` would then each have a board of one. In a plain folder, two sessions in the same directory produce a *warning* rather than a block, because there is no `git add -A` for the gate to stop, and the remedy is claims rather than worktrees.

**No doorbell outside tmux.** Typing into another session's prompt needs a terminal multiplexer. Mail is still filed and still delivered at the recipient's next turn by the UserPromptSubmit hook. What is lost is only waking an idle session, and the skill says to ask the human to switch windows in the rare case where that matters.

**Generic skill, local addendum.** The original skill named its author, his task tracker, his PR pipeline and his tagging convention. Those references were what made it useful to him and noise to anyone else. Deleting them would have made it worse for the person it was built for. Instead the skill now says "the user", and `session-coord addendum` prints `~/.config/session-coord/addendum.md` (or `$SESSION_COORD_ADDENDUM`), which the skill reads on load and treats as more specific than itself. `SESSION_COORD_USER` supplies the name used in printed messages. Both are plain environment, so the `env` block of Claude Code's settings reaches hooks and the Bash tool alike. Plugin `userConfig` was considered and rejected: it is exported to hook processes but not to the Bash tool, so the CLI would have needed a second source for the same value.

**Python 3.9.** That is what `/usr/bin/python3` is on a stock Mac. Two things broke: a runtime `X | None` in a type alias, and argparse 3.9 rejecting `lock <name> -- <cmd>`. Both are fixed, and the suite passes on 3.9 and 3.12.

**Names anonymized.** The incident narratives are kept because they are the reasons. The private repository they happened in is called `acme/infra` here, and its window labels are `infra:N`.

**Unverified.** These have not been tested on a real Mac. The `ps` path is exercised on Linux by forcing the fallback, and the `lstart` format is assumed to be stable for the life of a process, which it should be. Two sessions outside tmux have been simulated end to end (register, board, claim overlap, message, delivery, sweep of the dead peer) but have never been run as two real Claude sessions.

## Repository renamed, 2026-10-15

The public repository and local clone take the name `session-coord` because the same tool serves Codex as well as Claude. Keeping the original `claude-session-coord` repository name would imply a Claude-only scope; the plugin, marketplace, CLI, and skill already use `session-coord`. The earlier name remains only in captured screen fixtures, where it records the path visible during the Codex probe.

## Codex hook and TUI facts, 2026-10-14

OpenAI's [Hooks documentation](https://learn.chatgpt.com/docs/hooks) lists `SessionStart`, `UserPromptSubmit`, and `SessionEnd` for Codex CLI. Command hooks receive JSON on stdin with `session_id`, `cwd`, `transcript_path`, and `hook_event_name`; `SessionStart` also receives `source` (`startup`, `resume`, `clear`, `compact`, or `fork`). `UserPromptSubmit` can add context with the same `hookSpecificOutput` shape the Claude mail hook uses. User-level hooks in `~/.codex/config.toml` are an active source even for an untrusted project, but a changed non-managed hook must be reviewed and trusted through `/hooks` before it runs. Hooks are a stable feature in the installed Codex CLI 0.161.0. Its `[[hooks.SessionStart]]` TOML form is used in a local project on this machine.

Codex takes each `/hooks` title from the handler's `statusMessage`, trims it, and shows "Unnamed hook" when it is missing or empty ([Codex source, `hooks_browser_view.rs:420-425`](https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/hooks_browser_view.rs#L420-L425)). The installer therefore gives each command handler a short activity message beginning with `session-coord:`; the same message is shown while the hook runs. The `PreToolUse` title says it checks git writes: the gate only refuses relevant git commands when another live session shares the tree, so saying it refuses all writes would overstate its reach. Changing the message changes the hook's trusted definition, so users must review and trust it once more in `/hooks`. The alternative was leaving the handlers unnamed, which made four distinct hooks indistinguishable in the review screen.

A private `tmux -L coord-probe-codex` TUI probe reached the idle `›` prompt with a dim suggestion and a `GPT-6.1-Sol default · ~/...` footer. Its hook appeared in `/hooks`; the first launch showed it awaiting trust, and trusting it made it active. No `SessionStart` payload was produced on two bare launches while the TUI waited for its first turn. After the PM lifted the no-prompt rule for one launch-prompt probe, a launch with `reply OK` fired `SessionStart` and `UserPromptSubmit`; both stdin objects carried the same session id and cwd, `SessionStart` carried `source: "startup"`, and `UserPromptSubmit` carried `turn_id`, `prompt: "reply OK"`, and `permission_mode`. Thus a Codex TUI started with an initial prompt registers at launch; a bare TUI is not present on the board before its first turn. The running worker's own visible Codex pane supplied a real dim `• Working (1m 41s • esc to interrupt)` line while its empty `›` input was still visible. These observations decide the prompt rule: a visible working line overrides an empty input line, and missing or unreadable engine or busy evidence is unknown, never idle.

The throwaway `CODEX_HOME` received trust for its two user-level hook definitions. On a later launch in an unrelated folder with the same `CODEX_HOME`, `/hooks` showed both active without review, so this trust persisted across launches and projects for those unchanged definitions. Folder trust is a separate prompt: a new worktree may still need folder trust, but the same user-level hook definitions did not need new hook trust in the observed probe. The alternative, assuming trust was per project, would make unattended zamu workers look impossible to register without first asking Daniel in each worktree.

The adapter stays inside session-coord because registration, presence, inboxes and doorbells have the same semantics for both engines. The alternative, a Codex-only skill, would duplicate the coordination judgement without making a Codex pane addressable. The existing `claude_pid` field remains as a compatibility name for the recorded engine process while new records carry an engine tag; renaming the stored field would make an upgrade lose liveness evidence for old Claude records. Codex uses the same `UserPromptSubmit` mail delivery contract when that hook runs; the doorbell remains the way to wake an idle TUI.

The pane scan recognizes the nearest interactive engine process, including the pane pid itself, and does not mistake a Claude subprocess started by a Codex tool for the pane's session. A `codex exec` SessionStart is ignored for the same reason Claude's `-p` registration is ignored: a one-shot child shares the parent pane but is not a peer session. The alternative, registering every SessionStart or choosing a one-shot child over the pane's Codex TUI, would put a phantom record in the pane or sweep its real record and let the short-lived process take its name and mail.

The Codex installer leaves `PreToolUse` matcher empty and lets the existing gate inspect `tool_input.command`. A matcher of `Bash` was considered because the official hook schema calls shell tools `Bash`, but the working Codex hook config on this box uses an empty matcher, and a mismatched matcher would silently skip the gate. The review added tests that pass a Codex-shaped `PreToolUse` object through the installed shell shim and checker and a Codex-shaped `UserPromptSubmit` object through the mail hook. The output contracts match the documented Codex `permissionDecision` and `additionalContext` forms. A live Codex git-write refusal and live mail rendering were not probed.

Review also found three places where a positive fact had been inferred from a missing one. Manual `register` now detects a Codex ancestor and fingerprints its start when `--engine` and `--pid` are omitted; the alternative was a null-pid Claude record that the next sweep deletes. If no record exists yet, `register` can also take the session id from the Codex worker's observed `CODEX_SESSION_ID` or `CODEX_THREAD_ID` tool environment. Codex's empty composer is idle only with a visible welcome or completed-turn marker; a working line anywhere on screen is busy, and a screen with neither marker is unknown. A transient child exit during Codex pane discovery is skipped only after `/proc` confirms it exited. An unreadable live process in that Codex scan marks its pane unknown, keeping its presence record while other panes remain observable. The alternative was converting one process read error into an outage for the whole tmux server, or treating an unreadable live process as absent and deleting its record. The Claude scan keeps its existing server-wide unknown behavior.

The spill file normally stays under the system temp directory. When its absolute path exceeds the short doorbell's path budget, it goes under the session-coord state root and keeps the same age cleanup. The alternative was to type an overlong pointer or relax the line-length guarantee; both make a collision with a partially typed prompt harder to inspect. This showed up when the required on-disk test TMPDIR lived inside a nested worktree.

A later field observation found a long `tmux send-keys -l` burst sitting in an idle Codex composer for about 40 minutes after the first Enter; a second Enter submitted it. The Codex doorbell now captures the composer after Enter, retries Enter once only when its exact text remains, and captures again. A clear composer alone is not proof: a capture taken before the TUI paints a paste can still show the old idle screen. Delivery is reported only when the text is absent and a new visible `• Working` line shows the turn started; an unreadable or unchanged screen, changed text, or text still present after the retry yields an unknown outcome. The check compares the visible composer too, because dim text matching the sent message is still pending even if the normal typed-text parser strips it. The CLI reports uncertainty and does not file a second copy of an unregistered direct send. The alternative, trusting tmux's successful key injection or an empty prompt, misreports a paste left in the composer as delivered. The check joins terminal-wrapped rows before comparing the text. A turn that finishes before the capture may be reported unknown, which is safer than claiming delivery without evidence.

The captured Codex screen fixtures spell ESC bytes as literal `\x1b` text, and their test loader restores the original bytes before parsing. Leaving raw ESC in the tracked fixtures made `gh pr diff` refuse to print the PR and stopped the landing tool before review. Byte-for-byte restoration was checked against both original captures; only their storage form changed.

The landing review found installer and one-shot classifier faults. Installer copies now complete in a sibling staging directory, then each prepared file atomically replaces its live counterpart. A failed copy leaves the existing gate and config intact, and a process killed during replacement does not leave the hook path missing. The alternative of moving the whole runtime had a small missing-path window if killed between two renames; it also discarded a skill installed beneath the runtime after staging. Per-file replacement preserves that skill, at the cost of a possible mixed runtime version after an interrupted update; obsolete files are left in place. Preexisting symlinked directories in the runtime are refused before any live file is replaced, because otherwise a hook copy could write into their external targets. Hook commands quote resolved absolute script paths, and TOML basic strings escape control characters, so runtime names with spaces or newlines parse and launch. The alternative was relying on shell word splitting and the caller's working directory. The one-shot filter recognizes Codex's non-conversation subcommands, including `exec`, `e`, `review`, `apply`, `sandbox`, `doctor`, and server commands, while leaving `resume` and `fork` interactive. It checks the first positional command after skipping global option values such as `-C review`. Codex treats separate `--image one.png` as a list through the next option, even if a filename is `exec`; with attached `--image=one.png`, it recognizes a following `exec` as the subcommand after any additional image paths. The filter mirrors that observed difference. Registering a one-shot process creates a phantom peer; treating a directory named `review` as a subcommand hides a real interactive peer.

Known verification gaps: the private probe confirmed hook input on a launch-prompt turn, but did not send a peer message into that turn or run a git write, so live mail rendering and gate refusal remain unverified. A bare TUI did not emit `SessionStart` before its first turn; its first manually submitted prompt was not probed. Tmux pane discovery on macOS was closed on 2026-11-02; see the section below.

## Tmux pane discovery without `/proc`, 2026-11-02

A session on sumago, a Mac, reported that `session-coord msg` filed mail but never rang the doorbell, and that a window label like `main:3` failed with "tmux could not be consulted". The cause was the gap the Codex section above had named and left open. `tmuxio` built the process tree, recognised a claude and confirmed a vanished pid by reading `/proc` directly. On a Mac `/proc` does not exist, so `_children()` answered "could not look" on every call. It answered that correctly, but it meant every pane lookup gave up and every tmux-dependent feature went dark on every Mac.

The fix routes those three reads through `ps` and a signal probe when `procio._PROC` is false, and leaves the `/proc` path untouched. The tree comes from one `ps -A -o pid=,ppid=` listing; a failed `ps` or a line that is not two integers returns None rather than a partial tree. A claude is recognised by `ps -o comm=`, then by the basename of `args`. `ps` exits non-zero both for a pid that is gone and for one it would not describe, so its silence is disambiguated with `kill(pid, 0)`: ESRCH means gone and returns False, and anything still there that `ps` would not name raises `Unknown`. That keeps the property this file opens with: being unable to observe something is not observing its absence. Checked live on sumago: the old code returned None for every pane, and the new code resolved all five Claude panes to their pids.
