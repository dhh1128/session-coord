"""Codex process, pane, hook and install contracts."""

import json
import io
import runpy
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessioncoord import cli, liveness, procio, tmuxio
from sessioncoord.tmuxio import SendRefused
from sessioncoord.model import Presence
from codex import install as codex_install

ROOT = Path(__file__).resolve().parents[1]


def codex_screen(name):
    """Restore the captured ESC bytes while keeping the fixture diff printable."""
    stored = (ROOT / "tests" / "data" / f"codex-{name}.txt").read_text()
    assert "\x1b" not in stored
    return stored.replace(r"\x1b", "\x1b")


@pytest.mark.parametrize("argv,want", [
    (["/opt/codex/bin/codex", "--dangerously-bypass-approvals-and-sandbox"], True),
    (["node", "/opt/lib/node_modules/@openai/codex/bin/codex.js"], True),
    (["guarded", "codex"], False),
    (["node", "/opt/other/codex.js"], False),
    (["claude"], False),
])
def test_codex_process_identity(argv, want):
    assert procio.is_codex(argv) is want


def test_codex_ancestor_walks_through_guarded_wrapper(monkeypatch):
    parents = {40: 30, 30: 20, 20: 10}
    argv = {30: ["sh", "hook"], 20: ["/opt/bin/codex"], 10: ["guarded", "codex"]}
    monkeypatch.setattr(procio, "parent_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(procio, "argv_of", lambda pid: argv.get(pid))
    assert procio.codex_ancestor(40) == 20


@pytest.mark.parametrize("argv,want", [
    (["/opt/bin/codex", "exec", "one-shot"], True),
    (["node", "/opt/node_modules/@openai/codex/bin/codex.js", "exec", "one-shot"], True),
    (["/opt/bin/codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "one-shot"], True),
    (["node", "/opt/node_modules/@openai/codex/bin/codex.js", "--dangerously-bypass-approvals-and-sandbox", "exec", "one-shot"], True),
    (["/opt/bin/codex", "e", "one-shot"], True),
    (["/opt/bin/codex", "review", "--uncommitted"], True),
    (["node", "/opt/node_modules/@openai/codex/bin/codex.js", "review", "--uncommitted"], True),
    (["/opt/bin/codex", "-C", "review"], False),
    (["/opt/bin/codex", "--model", "review", "reply OK"], False),
    (["/opt/bin/codex", "--cd=review", "reply OK"], False),
    (["/opt/bin/codex", "--image", "one.png", "two.png", "--model", "x", "exec"], True),
    (["/opt/bin/codex", "--image", "/etc/hosts", "exec"], False),
    (["/opt/bin/codex", "--image=one.png", "exec", "--help"], True),
    (["/opt/bin/codex", "--image=/etc/hosts", "/etc/hosts", "exec", "--help"], True),
    (["/opt/bin/codex", "-ione.png", "review", "--help"], True),
    (["/opt/bin/codex", "-i", "review", "two.png", "--model", "x", "reply OK"], False),
    (["/opt/bin/codex", "--", "review"], False),
    (["/opt/bin/codex", "--dangerously-bypass-approvals-and-sandbox", "prompt"], False),
    (["/opt/bin/codex", "apply", "latest"], True),
    (["/opt/bin/codex", "a", "latest"], True),
    (["/opt/bin/codex", "sandbox", "linux"], True),
    (["/opt/bin/codex", "doctor"], True),
    (["/opt/bin/codex", "app-server"], True),
    (["/opt/bin/codex", "exec-server"], True),
    (["/opt/bin/codex", "resume", "--last"], False),
    (["/opt/bin/codex", "fork", "--last"], False),
])
def test_headless_codex_is_not_a_session(argv, want):
    assert procio.is_codex_headless(argv) is want


def test_codex_pane_discovers_native_child_after_node_wrapper(monkeypatch):
    kids = {10: [20], 20: [30]}
    monkeypatch.setattr(tmuxio, "_children", lambda: kids)
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: False)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        10: ["/bin/bash"],
        20: ["node", "/opt/lib/node_modules/@openai/codex/bin/codex.js"],
        30: ["/opt/lib/vendor/bin/codex"],
    }.get(pid))
    def run(argv):
        if argv[1] == "list-panes":
            return "%3\tprobe:1\t10\t/repo\tcodex-coord"
        if argv[1] == "capture-pane":
            return codex_screen("idle")
        return "ok"
    table = tmuxio.panes(run)
    assert table["%3"]["engine"] == "codex"
    assert table["%3"]["claude_pid"] == 30
    assert tmuxio.prompt_is_busy("%3", run, engine="codex") is False


def test_claude_tool_child_does_not_relabel_codex_pane(monkeypatch):
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 30)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        20: ["/opt/bin/codex"], 30: ["claude", "-p", "one-shot"]}.get(pid))
    assert tmuxio.claude_under(10, {10: [20], 20: [30]}) is None


def test_codex_busy_fixture_overrides_empty_input():
    screen = codex_screen("busy")
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is True


def test_visible_working_line_overrides_nearer_completed_line():
    screen = ("• Working (12s • esc to interrupt)\nWorked for 1m 2s\n"
              "›  \x1b[2mAsk Codex to do anything\x1b[0m\n")
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is True


def test_codex_busy_line_beyond_eight_rows_is_not_idle():
    screen = "• Working (12s • esc to interrupt)\n" + ("detail\n" * 20) + "›  \x1b[2mAsk Codex to do anything\x1b[0m\n"
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is True


def test_codex_with_no_visible_activity_state_is_unknown():
    screen = ("detail\n" * 20) + "›  \x1b[2mAsk Codex to do anything\x1b[0m\n"
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is None


def test_exited_grandchild_does_not_hide_all_panes(monkeypatch):
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        10: ["/bin/bash"],
        20: ["node", "/opt/node_modules/@openai/codex/bin/codex.js"],
        30: ["/opt/bin/codex"], 40: None}.get(pid))
    monkeypatch.setattr(tmuxio, "_pid_exited", lambda pid: pid == 40)
    assert tmuxio.codex_under(10, {10: [20], 20: [40, 30]}) == 30


def test_codex_pane_process_wins_over_one_shot_child(monkeypatch):
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        30: ["/opt/bin/codex"], 40: ["/opt/bin/codex", "exec", "one-shot"]}.get(pid))
    assert tmuxio.codex_under(30, {30: [40]}) == 30
    peer = Presence(session_id="peer", repo_root="/repo", workdir="/repo",
                    pane="%3", claude_pid=30, title="peer", window="probe:1")
    assert liveness.is_live(peer, {"%3": tmuxio.codex_under(30, {30: [40]})})


def test_unreadable_process_is_unknown_for_its_pane_only(monkeypatch):
    monkeypatch.setattr(tmuxio, "_children", lambda: {10: [40], 11: [50]})
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: False)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        10: ["/bin/bash"], 11: ["/bin/bash"],
        40: None, 50: ["/opt/bin/codex"]}.get(pid))
    monkeypatch.setattr(tmuxio, "_pid_exited", lambda pid: False)
    def run(argv):
        if argv[1] == "list-panes":
            return "%3\tprobe:1\t10\t/repo\tpeer\n%4\tprobe:2\t11\t/repo\tother"
        return "ok"
    table = tmuxio.panes(run)
    assert table["%3"]["claude_pid"] is tmuxio.UNKNOWN_PID
    assert table["%4"]["claude_pid"] == 50
    peer = Presence(session_id="peer", repo_root="/repo", workdir="/repo",
                    pane="%3", claude_pid=40, title="peer", window="probe:1")
    assert liveness.is_live(peer, tmuxio.claude_pids(run))
    assert cli._live([peer], table)[0].pane == "%3"
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: table)
    with pytest.raises(SendRefused, match="process could not be read"):
        tmuxio.send("%3", "session-coord inbox", run)


def test_codex_unknown_prompt_refuses_doorbell(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    calls = []
    def run(argv):
        calls.append(argv)
        return None if argv[1] == "capture-pane" else "ok"
    with pytest.raises(SendRefused, match="prompt"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert not any(argv[1] == "send-keys" for argv in calls)


def test_idle_codex_receives_literal_doorbell(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    screen = codex_screen("idle")
    working = "• Working (1s • esc to interrupt)\n›  \x1b[2mAsk Codex to do anything\x1b[0m\n"
    calls = []
    def run(argv):
        calls.append(argv)
        if argv[1] == "capture-pane":
            return screen if sum(call[1] == "capture-pane" for call in calls) == 1 else working
        return "ok"
    assert tmuxio.send("%3", "session-coord inbox", run, expect_pid=30) == "%3"
    sends = [argv for argv in calls if argv[1] == "send-keys"]
    assert sends[0][:6] == ["tmux", "send-keys", "-t", "%3", "-l", "--"]
    assert sends[1][-1] == "Enter"
    assert len(sends) == 2
    assert sum(argv[1] == "capture-pane" for argv in calls) == 2


@pytest.mark.parametrize("clears_after_retry", [True, False])
def test_codex_retries_enter_once_when_paste_remains(monkeypatch, clears_after_retry):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    idle = codex_screen("idle")
    working = "• Working (1s • esc to interrupt)\n›  \x1b[2mAsk Codex to do anything\x1b[0m\n"
    calls = []
    typed = None
    enters = 0

    def run(argv):
        nonlocal typed, enters
        calls.append(argv)
        if argv[1] == "send-keys":
            if "-l" in argv:
                typed = argv[-1]
            else:
                enters += 1
            return "ok"
        if argv[1] == "capture-pane":
            if enters == 0:
                return idle
            if enters == 2 and clears_after_retry:
                return working
            return f"  >_ OpenAI Codex (v0.161.0)\n\n› {typed}\n"
        return "ok"

    message = "session-coord inbox " + "long paste " * 16
    if clears_after_retry:
        assert tmuxio.send("%3", message, run, spill_over=0) == "%3"
    else:
        with pytest.raises(tmuxio.SendUnknown, match="unknown"):
            tmuxio.send("%3", message, run, spill_over=0)
    assert enters == 2
    assert sum(argv[1] == "capture-pane" for argv in calls) == 3
    assert all("-J" in argv for argv in calls if argv[1] == "capture-pane" and
               argv != calls[0])


def test_codex_stale_empty_capture_is_unknown(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    idle = codex_screen("idle")
    calls = []
    def run(argv):
        calls.append(argv)
        return idle if argv[1] == "capture-pane" else "ok"
    with pytest.raises(tmuxio.SendUnknown, match="unknown"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert sum(argv[1] == "send-keys" and argv[-1] == "Enter" for argv in calls) == 1


def test_codex_dim_sent_text_is_still_pending(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    idle = codex_screen("idle")
    calls = []
    typed = ""
    def run(argv):
        nonlocal typed
        calls.append(argv)
        if argv[1] == "send-keys" and "-l" in argv:
            typed = argv[-1]
        if argv[1] == "capture-pane":
            return idle if not typed else f"  >_ OpenAI Codex\n›  \x1b[2m{typed}\x1b[0m\n"
        return "ok"
    with pytest.raises(tmuxio.SendUnknown, match="unknown"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert sum(argv[1] == "send-keys" and argv[-1] == "Enter" for argv in calls) == 2


def test_codex_does_not_submit_a_different_composer(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    idle = codex_screen("idle")
    calls = []
    def run(argv):
        calls.append(argv)
        if argv[1] == "capture-pane":
            return idle if len([call for call in calls if call[1] == "capture-pane"]) == 1 else (
                "  >_ OpenAI Codex (v0.161.0)\n\n› another sentence\n")
        return "ok"
    with pytest.raises(tmuxio.SendUnknown, match="unknown"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert sum(argv[1] == "send-keys" and argv[-1] == "Enter" for argv in calls) == 1


def test_working_codex_keeps_doorbell_untyped(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    screen = ("• Working (12s • esc to interrupt)\nWorked for 1m 2s\n"
              "›  \x1b[2mAsk Codex to do anything\x1b[0m\n")
    calls = []
    def run(argv):
        calls.append(argv)
        return screen if argv[1] == "capture-pane" else "ok"
    with pytest.raises(SendRefused, match="is working"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert not any(argv[1] == "send-keys" for argv in calls)


def test_session_start_hook_registers_codex_process(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "startup",
        "transcript_path": str(tmp_path / "rollout.jsonl")})))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        calls.append((argv, kwargs)) or SimpleNamespace(returncode=0, stdout="")))
    assert module["main"]() == 0
    # Only the register call. Without /proc (macOS) the hook also asks `ps` about the codex
    # process, and that call comes first.
    argv, opts = next(c for c in calls if c[0][1:2] == ["register"])
    assert argv[1:5] == ["register", "--engine", "codex", "--session"]
    assert "--pid" in argv and argv[argv.index("--pid") + 1] == "1234"
    assert "--pane" in argv and argv[argv.index("--pane") + 1] == "%3"
    assert opts["cwd"] == str(tmp_path)
    parsed = cli.build_parser().parse_args(argv[1:])
    assert parsed.engine == "codex" and parsed.session == "thread-1"
    assert parsed.transcript == str(tmp_path / "rollout.jsonl")


def test_session_start_hook_registers_a_forked_thread(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "fork"})))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        calls.append(argv) or SimpleNamespace(returncode=0, stdout="")))
    assert module["main"]() == 0
    registers = [c for c in calls if c[1:2] == ["register"]]
    assert registers and "thread-1" in registers[0]


@pytest.mark.parametrize("args", [["e"], ["review"],
                                  ["apply"], ["a"], ["sandbox"], ["doctor"],
                                  ["app-server"], ["exec-server"],
                                  ["--image", "one.png", "two.png", "--model", "x", "exec"],
                                  ["--image=one.png", "exec", "--help"],
                                  ["--image=/etc/hosts", "/etc/hosts", "exec", "--help"]])
def test_session_start_hook_ignores_other_one_shot_commands(monkeypatch, tmp_path, args):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setattr(procio, "argv_of", lambda pid: ["/opt/bin/codex", *args])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "startup"})))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("registered one-shot"))
    assert module["main"]() == 0


def test_session_start_hook_accepts_review_as_directory_name(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setattr(procio, "argv_of", lambda pid: ["/opt/bin/codex", "-C", "review"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "startup"})))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        calls.append(argv) or SimpleNamespace(returncode=0, stdout="")))
    assert module["main"]() == 0
    registers = [c for c in calls if c[1:2] == ["register"]]
    assert registers and "thread-1" in registers[0]


def test_session_start_hook_accepts_exec_as_separate_image_value(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setattr(procio, "argv_of", lambda pid: ["/opt/bin/codex", "--image",
                                                       "/etc/hosts", "exec"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "startup"})))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        calls.append(argv) or SimpleNamespace(returncode=0, stdout="")))
    assert module["main"]() == 0
    registers = [c for c in calls if c[1:2] == ["register"]]
    assert registers and "thread-1" in registers[0]


def test_codex_pretool_payload_reaches_shared_tree_gate(tmp_path):
    hooks = tmp_path / "hooks"
    binary = tmp_path / "bin"
    hooks.mkdir()
    binary.mkdir()
    for name in ("session-coord-gate", "session-coord-gate-check"):
        shutil.copy2(ROOT / "hooks" / name, hooks / name)
    fake = binary / "session-coord"
    fake.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps([{'code': 'shared-tree', 'sessions': ['peer'], 'detail': 'same tree', 'remedy': 'use worktrees'}]))\n")
    fake.chmod(0o755)
    payload = json.dumps({
        "session_id": "thread-123", "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_use_id": "tool-1", "tool_input": {"command": "git add -A"},
        "permission_mode": "bypassPermissions",
    })
    out = subprocess.run([str(hooks / "session-coord-gate")], input=payload,
                         text=True, capture_output=True, cwd=tmp_path, timeout=30)
    assert out.returncode == 0
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_installer_is_idempotent_and_preserves_existing_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[features]\nhooks = true\n\n[[hooks.PreToolUse]]\nmatcher = "Bash"\n')
    skill = tmp_path / "skills"
    runtime = tmp_path / "session-coord"
    cmd = [sys.executable, str(ROOT / "codex" / "install.py"), "--config", str(config),
           "--skills", str(skill), "--runtime", str(runtime)]
    subprocess.run(cmd, check=True)
    once = config.read_text()
    subprocess.run(cmd, check=True)
    assert config.read_text() == once
    assert once.count("session-coord-codex-register") == 1
    assert "UserPromptSubmit" in once
    assert "SessionEnd" in once
    assert 'matcher = "Bash"' in once
    assert (skill / "session-coord" / "SKILL.md").exists()
    assert (runtime / "hooks" / "session-coord-codex-register").exists()
    assert (runtime / "hooks" / "session-coord-gate").exists()
    assert (runtime / "hooks" / "session-coord-mail").exists()
    parsed = tomllib.loads(once)
    assert parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["type"] == "command"
    assert parsed["hooks"]["PreToolUse"][-1]["matcher"] == ""


def test_installer_names_each_codex_hook(tmp_path):
    parsed = tomllib.loads(codex_install.hook_block(tmp_path / "runtime"))
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "SessionEnd"):
        handlers = parsed["hooks"][event][0]["hooks"]
        assert len(handlers) == 1
        message = handlers[0]["statusMessage"]
        assert message.startswith("session-coord:")
        assert message.removeprefix("session-coord:").strip()
        if event == "PreToolUse":
            assert message == "session-coord: check git writes in a shared tree"


def test_failed_reinstall_keeps_active_gate(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    runtime = tmp_path / "runtime"
    codex_install.install(config, tmp_path / "skills", runtime)
    before = config.read_bytes()
    gate = runtime / "hooks" / "session-coord-gate"
    gate_before = gate.read_bytes()
    original = codex_install.shutil.copytree

    def fail_hooks(src, dst, *args, **kwargs):
        if Path(src).name == "hooks":
            raise OSError("simulated copy failure")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(codex_install.shutil, "copytree", fail_hooks)
    with pytest.raises(OSError, match="simulated copy failure"):
        codex_install.install(config, tmp_path / "skills", runtime)
    assert gate.read_bytes() == gate_before
    assert config.read_bytes() == before


def test_interrupted_reinstall_never_removes_gate(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    runtime = tmp_path / "runtime"
    codex_install.install(config, tmp_path / "skills", runtime)
    gate = runtime / "hooks" / "session-coord-gate"
    original = codex_install.os.replace

    def interrupt_at_hook(src, dst):
        if Path(dst) == runtime / "hooks" / "session-coord-codex-register":
            raise KeyboardInterrupt("simulated termination")
        return original(src, dst)

    monkeypatch.setattr(codex_install.os, "replace", interrupt_at_hook)
    with pytest.raises(KeyboardInterrupt, match="simulated termination"):
        codex_install.install(config, tmp_path / "skills", runtime)
    assert gate.exists()
    assert (runtime / "hooks" / "session-coord-codex-register").exists()


def test_installer_keeps_skills_inside_runtime(tmp_path):
    config = tmp_path / "config.toml"
    runtime = tmp_path / "runtime"
    skills = runtime / "skills"
    codex_install.install(config, skills, runtime)
    codex_install.install(config, skills, runtime)
    assert (skills / "session-coord" / "SKILL.md").exists()


@pytest.mark.parametrize("part,file", [("hooks", "session-coord-gate"),
                                         ("sessioncoord", "procio.py")])
def test_installer_refuses_runtime_directory_symlinks(tmp_path, part, file):
    config = tmp_path / "config.toml"
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    runtime.mkdir()
    outside.mkdir()
    (outside / file).write_text("untouched")
    (runtime / part).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        codex_install.install(config, tmp_path / "skills", runtime)
    assert (outside / file).read_text() == "untouched"
    assert not config.exists()


def test_installer_quotes_absolute_runtime_and_toml_controls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = Path("relative runtime\nname\x7f")
    parsed = tomllib.loads(codex_install.hook_block(runtime))
    for event, script in (("SessionStart", "session-coord-codex-register"),
                          ("UserPromptSubmit", "session-coord-mail"),
                          ("PreToolUse", "session-coord-gate"),
                          ("SessionEnd", "session-coord-codex-end")):
        command = parsed["hooks"][event][0]["hooks"][0]["command"]
        assert shlex.split(command) == [str((tmp_path / runtime / "hooks" / script).resolve())]
