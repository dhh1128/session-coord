# session-coord

A Claude Code plugin with a Codex CLI adapter for people who run several agent sessions at once in the same project, and have started to notice them tripping over each other.

It gives the sessions a shared board, so each one knows who else is working here, what they are for and which files they are writing. One session takes the conductor role and sequences the rest. Sessions message each other directly instead of asking you to copy and paste between windows, and they keep one ordered queue of the questions they need you to answer. Everything stays on your machine.

It was built for a heavy multi-session coding workflow and then generalized. It works in a git repo or in a plain folder of research and drafts, with or without tmux, on macOS or Linux.

## What problem it solves

Run three or four sessions in one project and three things go wrong, none of them visible from inside any one session:

- **Two sessions write the same thing.** One overwrites the other's last edit to the shared synthesis, or two sessions put different figures for the same number into adjacent sections, and neither sees the other's.
- **You become the switchboard.** Every session that hits a question escalates to you, including questions another session could answer, and you spend your time carrying context between windows.
- **Loose ends die with the window.** A finding or a decision that lived only in one conversation is gone when that session closes.

Each session behaving well does not fix this. It needs a role — someone sequencing — and a channel between sessions that is not you.

## Install

In Claude Code:

```
/plugin marketplace add dhh1128/session-coord
/plugin install session-coord@session-coord
```

Then restart Claude Code, or run `/reload-plugins`. The plugin adds a `session-coord` command to the Bash tool's PATH, a skill of the same name, and four hooks.

Requirements: `python3` 3.9 or later, which macOS already has, and `ps`. git and tmux are optional.

### Codex CLI

Clone `https://github.com/dhh1128/session-coord.git`, then run `python3 codex/install.py` from the checkout. It copies the runtime to `~/.codex/session-coord/`, the skill to `~/.codex/skills/session-coord/`, and appends a marked hook block to `~/.codex/config.toml`. Re-running it updates the copies and replaces that block without duplicating it or changing other settings. Restart Codex, then open `/hooks` and review and trust the new hooks. Codex requires that review before user-level hooks run. If your config disables hooks, set `[features] hooks = true` first.

The Codex hooks register a session at `SessionStart`, deliver inbox mail at `UserPromptSubmit`, check git writes at `PreToolUse`, and remove presence at `SessionEnd`. In a Codex CLI 0.161 probe, a session launched with an initial prompt fired `SessionStart` and `UserPromptSubmit`; a bare TUI did not fire `SessionStart` while it waited for its first turn, so it is not yet on the board at that point. Once the session is registered, `session-coord msg <name> "<text>"` files mail and rings an idle Codex pane under tmux; without tmux, mail waits for its next turn. Use `session-coord register --name <name>` in the Codex session to give peers a short addressable name. The Codex adapter uses the same board and inbox as Claude sessions in the same project. The mail and gate paths have Codex-shaped tests; live mail rendering and a live git-write refusal remain unverified.

## What happens once it is installed

When only one session is in a project, nothing happens. The hooks print nothing, and that silence is deliberate.

When a second session starts in the same project, it is told about the sessions already there, and the skill tells it how to proceed: name themselves, state their charter, claim the files they will write, and pick a conductor. From then on:

- `session-coord board` shows who is here, what each session is for, and what each has claimed.
- `session-coord check` reports problems: overlapping claims, sessions sharing a working tree, and no conductor.
- `session-coord msg <name> "<text>"` leaves a message for another session. It is delivered into that session's context at the start of its next turn.
- `session-coord ask add` / `ask list` keep the one queue of things you need to answer, in dependency order, each written to stand on its own.
- In a git repo, a hook refuses `git add`/`commit`/etc. in a working tree another live session is also using, because one session's `git add -A` would sweep up the other's uncommitted work. It lets through a `git add` or `git commit` that names every file explicitly, provided no other session in that tree has claimed one of them. A bare `git commit` passes only right after an explicit `git add` in the same command, and only when nothing else is staged. Broad forms (`add -A`, `add .`, `add -u`, `commit -a`, directories, globs, magic pathspecs, `--pathspec-from-file`) are always refused, and the refusal says what it would accept instead.

### What counts as "the same project"

A git repository, all of its worktrees included. Outside git, the nearest folder above the session that contains a `CLAUDE.md`, a `.claude/` folder or an empty `.session-coord` file. Failing those, the folder the session started in. If two sessions should see each other and don't, `touch .session-coord` in the folder that contains them both.

### With tmux and without it

Under tmux, a message also types a short doorbell into the recipient's prompt. That wakes a session which would otherwise sit idle. A peer in another repo receives the message in its own inbox; a name not registered anywhere gets held mail delivered at that session's next turn. The budget only decides whether to ring, never whether to file. Without tmux there is no doorbell. The message waits for the recipient's next turn, so an idle session reads it when you next switch to it. Nothing else differs: sessions are identified by their process rather than their tmux pane.

## Configure

Both settings are optional.

**Your name.** Messages from the tool refer to "the user" by default. To have them use your name, add it to the `env` block of `~/.claude/settings.json`:

```json
{ "env": { "SESSION_COORD_USER": "Pat" } }
```

**Your own rules.** The skill is deliberately generic. It knows nothing about your task tracker, your document conventions or your review process. Put your house rules in `~/.config/session-coord/addendum.md`, or point `SESSION_COORD_ADDENDUM` at a file elsewhere. The skill reads that file when it loads and treats it as more specific than itself. For example:

```markdown
- Research findings go in `findings.md` at the project root, one dated bullet each, with the source URL.
- The conductor owns `synthesis.md`. Other sessions send it changes by message rather than editing it.
- Park unfinished threads in Things, project "Research", before closing a session.
```

This is the intended way to customize it. Fork the repository only to change the mechanics.

**A repo that shares its checkout on purpose.** Some repos are meant to be worked in from the main checkout, with no worktree and no branch: a docs repo that commits straight to main, for example. To say so, commit a file named `.session-coord-shared-checkout` at the repo root and push it to the remote's default branch. Its contents are for humans, so write down why. The board then reports a shared tree there as a warning rather than a block, and it stops telling sessions to take a worktree. The hook still refuses broad pathspecs and claimed files there, since those are what actually sweep up another session's work. The file counts only once it is on `origin/<default branch>`. A copy that is only in the working tree, or only committed locally, is ignored, so a session cannot quietly opt itself out.

## Where it keeps things

`~/.local/state/claude-coord/<project>/` holds one small JSON file per live session, plus the ask queue, the inboxes and lock files. A session's file is removed when the session's process is gone. Nothing is sent anywhere. To start over, delete the directory.

## Uninstall

```
/plugin uninstall session-coord@session-coord
```

## Development

```
uv run --no-project --with pytest python -m pytest tests -q
```

`skills/session-coord/DESIGN.md` explains why the design is the way it is: the incident that started it, the decisions and the ones that were reversed. Read it before changing behaviour. Most of the odd-looking choices in the code are answers to something that actually went wrong.
