"""Sessions on other boxes: the hosts file, the tailnet, the parallel fetch, the board."""

from __future__ import annotations

import io
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from sessioncoord import cli, remote


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self.out, self.err, self.returncode, self.hang = out, err, code, hang
        self.killed = False
        self.sent = None
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(err)

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode

    def communicate(self, input=None, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ssh", timeout)
        self.sent = input
        return self.out, self.err

    def kill(self):
        self.killed = True


def estate(*sessions):
    return json.dumps({"host": "kelun", "sessions": list(sessions)}).encode()


def sess(name, origin="github.com/o/r", repo="/home/d/r", **kw):
    return {"name": name, "label": name, "repo_root": repo, "workdir": repo,
            "branch": "main", "origin": origin, **kw}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(remote, "local_names", lambda: {"sumago", "sumago.local"})
    monkeypatch.setattr(remote, "local_host", lambda: "sumago")
    return tmp_path


def hosts_file(tmp_path, monkeypatch, text):
    f = tmp_path / "hosts"
    f.write_text(text)
    monkeypatch.setenv("SESSION_COORD_HOSTS", str(f))
    return f


class TestNormaliseOrigin:
    @pytest.mark.parametrize("url", [
        "git@github.com:O/R.git", "https://github.com/o/r", "https://github.com/o/r.git/",
        "ssh://git@github.com/o/r", "ssh://git@github.com:22/o/r.git",
        "https://user@github.com/o/r"])
    def test_every_spelling_of_one_repo_agrees(self, url):
        assert remote.normalise_origin(url) == "github.com/o/r"

    def test_no_origin_matches_nothing(self):
        assert remote.normalise_origin("") == ""


class TestHostsFile:
    def test_comments_blank_lines_and_this_box_are_dropped(self, home, monkeypatch):
        hosts_file(home, monkeypatch, "# boxes\nkelun\n\nsumago\nSUMAGO.local\nrafe  # win\n")
        assert [h.name for h in remote.load_hosts()] == ["kelun", "rafe"]

    def test_a_second_word_is_the_remote_command(self, home, monkeypatch):
        hosts_file(home, monkeypatch, "kelun /opt/sc/bin/session-coord\n")
        assert remote.load_hosts()[0].command == "/opt/sc/bin/session-coord"

    def test_a_name_ssh_would_read_as_an_option_is_refused(self, home, monkeypatch):
        hosts_file(home, monkeypatch, "-oProxyCommand=x\nkelun\nkelun\n")
        assert [h.name for h in remote.load_hosts()] == ["kelun"]

    def test_no_file_is_no_hosts(self, home):
        assert remote.load_hosts() == []


class TestTailnet:
    def test_peers_by_short_name(self):
        raw = json.dumps({"BackendState": "Running", "Peer": {
            "a": {"HostName": "Kelun", "DNSName": "kelun.tail.ts.net.", "Online": True},
            "b": {"HostName": "rafe", "DNSName": "rafe.tail.ts.net.", "Online": False,
                  "LastSeen": "2026-11-03T16:56:39Z"}}})
        net = remote.tailnet(raw)
        assert net["kelun"]["online"] and not net["rafe"]["online"]

    @pytest.mark.parametrize("raw", [None, "not json", json.dumps({"BackendState": "Stopped"})])
    def test_a_stopped_or_absent_tailscale_is_none(self, raw):
        assert remote.tailnet(raw) is None


class TestFetch:
    def test_offline_hosts_are_not_dialled_and_online_ones_are(self, home, monkeypatch):
        dialled = []
        monkeypatch.setattr(remote, "_spawn",
                            lambda argv, stdin=None: dialled.append(argv) or
                            FakeProc(estate(sess("a"))))
        net = {"kelun": {"online": True, "last_seen": ""},
               "rafe": {"online": False, "last_seen": "2026-11-03T16:56:39Z"}}
        views, note = remote.fetch([remote.Host("kelun"), remote.Host("rafe")], net=net)
        assert len(dialled) == 1 and dialled[0][-2] == "kelun"
        assert views[0].sessions[0]["name"] == "a" and views[1].offline
        assert note == ""

    def test_tailscale_down_dials_nobody_and_says_so(self, home, monkeypatch):
        monkeypatch.setattr(remote, "_tailscale", lambda: None)
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: pytest.fail("dialled"))
        views, note = remote.fetch([remote.Host("kelun")])
        assert "tailscale is not running" in note and views[0].problem

    def test_a_host_that_hangs_is_cut_off_at_the_deadline(self, home, monkeypatch):
        proc = FakeProc(hang=True)
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: proc)
        views, _ = remote.fetch([remote.Host("kelun")], deadline=0.01, net={})
        assert proc.killed and "no answer" in views[0].problem

    def test_a_failed_fetch_shows_the_last_answer_with_its_age(self, home, monkeypatch):
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: FakeProc(estate(sess("a"))))
        remote.fetch([remote.Host("kelun")], net={})
        monkeypatch.setattr(remote, "_spawn",
                            lambda *a, **k: FakeProc(err=b"ssh: connect refused", code=255))
        views, _ = remote.fetch([remote.Host("kelun")], net={})
        v = views[0]
        assert v.from_cache and v.sessions[0]["name"] == "a" and "ssh failed" in v.problem
        assert "showing what it said just now" in remote.render(views, "github.com/o/r")

    def test_an_old_peer_is_named_as_old(self, home, monkeypatch):
        err = b"session-coord: error: unrecognized arguments: --estate\n"
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: FakeProc(err=err, code=2))
        views, _ = remote.fetch([remote.Host("kelun")], net={})
        assert "too old" in views[0].problem


class TestRemoteArgv:
    def test_every_argument_is_quoted_for_the_remote_shell(self):
        argv = remote.remote_argv(remote.Host("kelun"), "msg", "a b; rm -rf ~", "-")
        assert argv[-2] == "kelun"
        assert argv[-1] == "~/.local/bin/session-coord msg 'a b; rm -rf ~' -"
        assert "BatchMode=yes" in argv


class TestRender:
    def test_this_repos_peers_in_full_and_the_rest_summarised(self):
        v = remote.HostView("kelun", sessions=[
            sess("same", charter="doing x", claims=["a.py"]),
            sess("other", origin="github.com/o/q", repo="/home/d/q"),
            sess("third", origin="github.com/o/z", repo="/home/d/z")])
        out = remote.render([v], "github.com/o/r")
        assert "address as: same@kelun" in out and "doing x" in out and "claims: a.py" in out
        assert "2 sessions in 2 other projects" in out and "other@kelun" not in out
        assert "other@kelun" in remote.render([v], "github.com/o/r", show_all=True)

    def test_offline_says_when_it_was_last_seen(self):
        seen = (datetime.now(timezone.utc) - timedelta(days=2, hours=1)).isoformat()
        v = remote.HostView("rafe", offline=True, last_seen=seen, problem="offline")
        assert "rafe: offline, last seen 2d ago" in remote.render([v], "x")

    def test_no_hosts_and_no_note_says_nothing(self):
        assert remote.render([], "x") == ""


class TestMsgToAnotherBox:
    def test_name_at_a_configured_host_goes_over_ssh_with_the_body_on_stdin(
            self, home, monkeypatch, capsys):
        hosts_file(home, monkeypatch, "kelun\n")
        seen = {}

        def spawn(argv, stdin=None):
            seen["argv"] = argv
            seen["proc"] = FakeProc(out=b"filed for peer and rang (%1)\n")
            return seen["proc"]
        monkeypatch.setattr(remote, "_spawn", spawn)
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: None)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"hello; $(x)\n")))
        rc = cli.main(["msg", "peer one@kelun", "-"])
        assert rc == 0
        assert seen["argv"][-2] == "kelun"
        assert "msg 'peer one' - --sender 'a peer@sumago'" in seen["argv"][-1]
        assert seen["proc"].sent == b"hello; $(x)"
        assert "filed for peer" in capsys.readouterr().out

    def test_an_at_sign_naming_no_configured_host_stays_local(self, home, monkeypatch):
        hosts_file(home, monkeypatch, "kelun\n")
        assert remote.split_address("me@work", remote.load_hosts()) is None
        assert remote.split_address("@kelun", remote.load_hosts()) is None
        assert remote.split_address("a@b@kelun", remote.load_hosts())[0] == "a@b"

    def test_the_sender_line_is_an_address_the_recipient_can_answer(
            self, home, monkeypatch):
        from sessioncoord.model import Presence
        hosts_file(home, monkeypatch, "kelun\n")
        seen = {}
        monkeypatch.setattr(remote, "_spawn",
                            lambda argv, stdin=None: seen.setdefault("argv", argv) and FakeProc())
        me = Presence(session_id="s1", repo_root="/r", workdir="/r", title="guard",
                      window="me:2", pane="%4")
        assert me.label == "guard (now me:2)"
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: me)
        cli.main(["msg", "peer@kelun", "hi"])
        assert "--sender 'guard@sumago (reply to s1@sumago)'" in seen["argv"][-1]
        # The reply address is the session id, which survives a rename and a window renumber,
        # and on the far box it names a configured host and splits back cleanly.
        assert remote.split_address("s1@sumago", [remote.Host("sumago")])[0] == "s1"


class TestPeerInputIsUntrusted:
    def test_malformed_fields_cost_the_field_not_the_board(self):
        body = json.dumps({"sessions": [{"name": "a", "claims": [1, "ok"], "workdir": []}],
                           "worktrees": [{"path": {}, "origin": "o"}]}).encode()
        sessions, trees = remote._parse(body)
        assert sessions[0]["claims"] == ["ok"] and sessions[0]["workdir"] == ""
        remote.render([remote.HostView("k", sessions=sessions, worktrees=trees)], "o")

    @pytest.mark.parametrize("raw", ["[]", '{"Peer": {"x": null}}', '{"Peer": []}'])
    def test_odd_tailscale_json_is_handled(self, raw):
        net = remote.tailnet(raw)
        assert net is None or net == {}


class TestSlowHostDoesNotHideAFastOne:
    def test_a_fast_answer_after_a_slow_host_is_kept(self, home, monkeypatch):
        import sys
        slow = [sys.executable, "-c", "import time; time.sleep(3)"]
        fast = [sys.executable, "-c", "print('{\"sessions\": [{\"name\": \"f\"}]}')"]
        argvs = iter([slow, fast])
        monkeypatch.setattr(remote, "remote_argv", lambda h, *a: next(argvs))
        views, _ = remote.fetch([remote.Host("slow"), remote.Host("fast")], deadline=1.0,
                                net={})
        assert "no answer" in views[0].problem
        assert views[1].sessions[0]["name"] == "f" and not views[1].problem


class TestOriginsOnOtherServers:
    def test_a_non_default_port_is_another_service(self):
        assert (remote.normalise_origin("ssh://git@example.com:2222/t/r")
                != remote.normalise_origin("ssh://git@example.com:2223/t/r"))

    def test_path_case_is_kept_off_github(self):
        assert remote.normalise_origin("https://Example.com/T/R") == "example.com/T/R"


def test_remote_msg_carries_pid_and_force(home, monkeypatch):
    hosts_file(home, monkeypatch, "kelun\n")
    seen = {}
    monkeypatch.setattr(remote, "_spawn",
                        lambda argv, stdin=None: seen.setdefault("argv", argv) and FakeProc())
    monkeypatch.setattr(cli, "_me_anywhere", lambda repo: None)
    cli.main(["msg", "%9@kelun", "hi", "--pid", "123", "--force"])
    tail = seen["argv"][-1]
    assert "--pid 123" in tail and "--force" in tail


class TestCodexSandbox:
    """Codex runs commands with the network off by default and says so in the environment."""

    def test_the_board_says_why_instead_of_blaming_the_other_box(self, home, monkeypatch):
        monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: pytest.fail("dialled"))
        views, note = remote.fetch([remote.Host("kelun")], net={})
        assert "Codex's sandbox" in note and views[0].problem == "not asked"

    def test_msg_refuses_with_the_reason_and_sends_nothing(self, home, monkeypatch, capsys):
        monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
        hosts_file(home, monkeypatch, "kelun\n")
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: pytest.fail("dialled"))
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: None)
        assert cli.main(["msg", "peer@kelun", "hi"]) == 2
        assert "Codex's sandbox" in capsys.readouterr().err


class TestWorkInAnotherRepo:
    """2026-11-06: a session registered in devenv on kelun was working in a session-coord
    worktree there, and the board matched on origin did not show it."""

    def test_a_worktree_of_this_repo_with_no_session_in_it_is_shown(self):
        v = remote.HostView("kelun", sessions=[
            sess("handoff", origin="github.com/dhh1128/devenv", repo="/home/d/devenv")],
            worktrees=[{"repo_root": "/home/d/sc", "origin": "github.com/dhh1128/session-coord",
                        "path": "/home/d/sc/.worktrees/codex-appserver",
                        "branch": "codex-appserver"}])
        out = remote.render([v], "github.com/dhh1128/session-coord")
        assert "worktree /home/d/sc/.worktrees/codex-appserver  (codex-appserver)" in out

    def test_a_worktree_a_session_is_in_is_not_repeated(self):
        path = "/home/d/sc/.worktrees/x"
        v = remote.HostView("kelun", sessions=[sess("a", workdir=path)],
                            worktrees=[{"origin": "github.com/o/r", "path": path,
                                        "branch": "x"}])
        assert "no session registered" not in remote.render([v], "github.com/o/r")

    def test_the_fetch_and_cache_carry_worktrees(self, home, monkeypatch):
        body = json.dumps({"sessions": [], "worktrees": [{"path": "/p", "origin": "o"}]})
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: FakeProc(body.encode()))
        assert remote.fetch([remote.Host("kelun")], net={})[0][0].worktrees[0]["path"] == "/p"
        monkeypatch.setattr(remote, "_spawn", lambda *a, **k: FakeProc(code=255))
        assert remote.fetch([remote.Host("kelun")], net={})[0][0].worktrees[0]["path"] == "/p"


def test_gitio_worktrees_reads_porcelain():
    from sessioncoord import gitio
    porcelain = ("worktree /r\nHEAD abc\nbranch refs/heads/main\n\n"
                 "worktree /r/.worktrees/x\nHEAD def\nbranch refs/heads/x\n\n"
                 "worktree /r/.worktrees/d\nHEAD 123\ndetached\n")
    assert gitio.worktrees("/r", lambda argv, cwd: porcelain) == [
        ("/r", "main"), ("/r/.worktrees/x", "x"), ("/r/.worktrees/d", "")]



class TestPeerAnswersAreBounded:
    """Hostile pass on PR #12: a peer's answer is input, and the terminal is an output."""

    def test_an_oversized_answer_is_refused_not_read_whole(self, home, monkeypatch):
        import sys
        big = [sys.executable, "-c",
               "import sys; sys.stdout.write('x' * (3 * 1024 * 1024))"]
        monkeypatch.setattr(remote, "remote_argv", lambda h, *a: big)
        views, _ = remote.fetch([remote.Host("kelun")], deadline=5.0, net={})
        assert views[0].problem and not views[0].sessions

    def test_a_long_field_is_capped(self):
        body = json.dumps({"sessions": [{"name": "n" * 10000}]}).encode()
        assert len(remote._parse(body)[0][0]["name"]) == remote.MAX_FIELD

    def test_terminal_escapes_from_a_peer_never_reach_the_screen(self):
        body = json.dumps({"sessions": [{"name": "a", "origin": "o",
                                         "charter": "x\x1b[2J\x1b[Hforged\x9b",
                                         "claims": ["\x1b]0;t\x07"]}]}).encode()
        out = remote.render([remote.HostView("k", sessions=remote._parse(body)[0])], "o")
        assert "\x1b" not in out and "\x9b" not in out and "\x07" not in out
        assert "forged" in out


class TestRemoteDoorbellBudget:
    def test_an_over_budget_sender_files_remotely_without_ringing(self, home, monkeypatch):
        from sessioncoord.model import Presence
        hosts_file(home, monkeypatch, "kelun\n")
        seen = {}
        monkeypatch.setattr(remote, "_spawn",
                            lambda argv, stdin=None: seen.setdefault("argv", argv) and FakeProc())
        me = Presence(session_id="s1", repo_root="/r", workdir="/r", title="guard")
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: me)
        monkeypatch.setattr(cli._Doorbell, "reserve", lambda self: "three doorbells this hour")
        cli.main(["msg", "peer@kelun", "hi"])
        assert "--no-ring" in seen["argv"][-1]

    def test_within_budget_the_far_box_may_ring(self, home, monkeypatch):
        hosts_file(home, monkeypatch, "kelun\n")
        seen = {}
        monkeypatch.setattr(remote, "_spawn",
                            lambda argv, stdin=None: seen.setdefault("argv", argv) and FakeProc())
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: None)
        monkeypatch.setattr(cli._Doorbell, "reserve", lambda self: None)
        cli.main(["msg", "peer@kelun", "hi"])
        assert "--no-ring" not in seen["argv"][-1]



class TestCopilotOnPR12:
    def test_hung_hosts_do_not_stretch_the_deadline(self, home, monkeypatch):
        import sys
        hang = [sys.executable, "-c", "import time; time.sleep(30)"]
        monkeypatch.setattr(remote, "remote_argv", lambda h, *a: hang)
        t0 = time.monotonic()
        views, _ = remote.fetch([remote.Host(f"h{i}") for i in range(4)], deadline=0.5,
                                net={})
        assert time.monotonic() - t0 < 2.5
        assert all("no answer" in v.problem for v in views)

    def test_an_ssh_error_line_cannot_carry_escapes(self):
        assert "\x1b" not in remote._why(255, b"ssh: \x1b[2Jboom\n")

    def test_the_reply_address_crosses_with_the_message(self, home, monkeypatch):
        from sessioncoord.model import Presence
        hosts_file(home, monkeypatch, "kelun\n")
        seen = {}
        monkeypatch.setattr(remote, "_spawn",
                            lambda argv, stdin=None: seen.setdefault("argv", argv) and FakeProc())
        me = Presence(session_id="s1", repo_root="/r", workdir="/r", title="guard")
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: me)
        monkeypatch.setattr(cli._Doorbell, "reserve", lambda self: None)
        cli.main(["msg", "peer@kelun", "hi"])
        assert "--sender-name s1@sumago" in seen["argv"][-1]

    def test_the_far_side_records_it_for_the_reply_hint(self, home, monkeypatch):
        from sessioncoord import store
        from sessioncoord.model import Presence
        monkeypatch.setattr(cli, "_resolve_repo", lambda args: "/repo")
        monkeypatch.setattr(cli, "_me", lambda repo: None)
        monkeypatch.setattr(cli, "_me_anywhere", lambda repo: None)
        store.save(Presence(session_id="peer", repo_root="/repo", workdir="/repo", pane=None,
                            claude_pid=1))
        assert cli.main(["msg", "peer", "hi", "--sender", "g@sumago",
                         "--sender-name", "s1@sumago"]) == 0
        assert store.load_inbox("/repo")[0].sender_name == "s1@sumago"

    def test_a_repo_with_no_registrant_still_has_its_worktrees_listed(self, monkeypatch):
        from sessioncoord import gitio, store
        from sessioncoord.model import Presence
        monkeypatch.setattr(store, "known_repos", lambda: ["/devenv"])
        monkeypatch.setattr(store, "load_all", lambda repo: [Presence(
            session_id="s", repo_root="/devenv", workdir="/sc/.worktrees/x")])
        monkeypatch.setattr(gitio, "repo_root",
                            lambda p, run=None: "/sc" if p.startswith("/sc") else "/devenv")
        monkeypatch.setattr(gitio, "worktrees", lambda r, run=None: (
            [("/sc", "main"), ("/sc/.worktrees/x", "x")] if r == "/sc" else [("/devenv", "main")]))
        monkeypatch.setattr(remote, "origin_of", lambda r: "o" + r)
        trees = cli._estate_view()["worktrees"]
        assert [t["path"] for t in trees] == ["/sc/.worktrees/x"]
