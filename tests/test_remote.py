"""Sessions on other boxes: the hosts file, the tailnet, the parallel fetch, the board."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from sessioncoord import cli, remote


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self.out, self.err, self.returncode, self.hang = out, err, code, hang
        self.killed = False
        self.sent = None

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
        assert "msg 'peer one' - --sender a-peer@sumago" in seen["argv"][-1].replace(
            "'a peer@sumago'", "a-peer@sumago")
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
        assert "--sender guard@sumago" in seen["argv"][-1]
        # and on the far box, that sender names a configured host and splits back cleanly
        assert remote.split_address("guard@sumago", [remote.Host("sumago")])[0] == "guard"


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
