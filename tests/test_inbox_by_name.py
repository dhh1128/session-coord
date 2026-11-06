"""A message remains deliverable when its recipient is outside the sender's repo or absent."""

from __future__ import annotations

import pytest

from sessioncoord import cli, inbox, store
from sessioncoord.model import Presence

SENDER = "/sender"
OTHER = "/other"
THIRD = "/third"


@pytest.fixture
def estate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(cli.gitio, "workspace_root", lambda path: path)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    store.save(Presence(session_id="sender", repo_root=SENDER, workdir=SENDER,
                        pane="%1", window="sender:1", title="writer",
                        claude_pid=101, tmux_server="999"))
    return monkeypatch


def peer(repo=OTHER, sid="peer", pane="%2", title="foreman-imbu", window="other:2",
         pid=102):
    store.save(Presence(session_id=sid, repo_root=repo, workdir=repo, pane=pane,
                        window=window, title=title, claude_pid=pid,
                        tmux_server="999"))


def hold_budget(monkeypatch):
    monkeypatch.setattr(cli._Doorbell, "reserve", lambda self: "budget exhausted")


def test_cross_repo_recipient_files_in_its_own_inbox(estate):
    peer()
    sent = []
    estate.setattr(cli.tmuxio, "send", lambda target, text, **kw: sent.append((target, text, kw)) or "%2")
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "a note"]) == 0
    assert store.load_inbox(SENDER) == []
    assert [(m.recipient, m.body) for m in store.load_inbox(OTHER)] == [("peer", "a note")]
    assert sent[0][0:2] == ("%2", inbox.DOORBELL)
    assert sent[0][2]["expect_pid"] == 102


def test_cross_repo_ambiguity_refuses_with_repos_and_ids(estate, capsys):
    peer()
    peer(THIRD, "other-peer", "%3", "foreman-imbu", "third:3")
    sent = []
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: sent.append(a))
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "a note"]) == 2
    assert store.load_inbox(OTHER) == store.load_inbox(THIRD) == []
    assert sent == []
    err = capsys.readouterr().err
    assert f"{OTHER}: peer" in err and f"{THIRD}: other-peer" in err


def test_unregistered_over_budget_is_held(estate, capsys):
    hold_budget(estate)
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: pytest.fail("budgeted send"))
    assert cli.main(["msg", "--repo", SENDER, "✳ Foreman-Imbu", "wait for me"]) == 0
    assert [(m.recipient, m.body) for m in store.load_held()] == [
        ("foreman-imbu", "wait for me")]
    out = capsys.readouterr().out
    assert "held for" in out
    # The outcome survives `| tail -3`: a multi-line budget refusal used to follow it and push
    # it off, and the sender resent a message that had been held (handoff V8).
    assert out.strip().splitlines()[0].startswith("held for")
    assert len(out.strip().splitlines()) == 2 and "--override" not in out


def test_unregistered_success_is_typed_only(estate):
    sent = []
    estate.setattr(cli.tmuxio, "send", lambda target, text, **kw: sent.append(text) or "%9")
    assert cli.main(["msg", "--repo", SENDER, "%9", "direct note"]) == 0
    assert "direct note" in sent[0]
    assert store.load_held() == []


def test_unknown_direct_send_is_not_held_or_reported_delivered(estate, capsys):
    def unknown(*a, **kw):
        raise cli.tmuxio.SendUnknown("composer still holds the message")
    estate.setattr(cli.tmuxio, "send", unknown)
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "direct note"]) == 2
    assert store.load_held() == []
    err = capsys.readouterr().err
    assert "send outcome unknown" in err
    assert "nothing was filed" in err
    assert "may be sitting unsubmitted" in err


@pytest.mark.parametrize("target", ["%9", "other:2"])
def test_failed_direct_send_to_tmux_address_files_nothing(estate, capsys, target):
    def refuse(*a, **kw):
        raise cli.tmuxio.SendRefused("no pane")
    estate.setattr(cli.tmuxio, "send", refuse)
    assert cli.main(["msg", "--repo", SENDER, target, "direct note"]) == 2
    assert store.load_held() == []
    assert "NOT sent, and nothing was filed" in capsys.readouterr().err


def test_inbox_without_held_mail_skips_estate_scan(estate, capsys):
    estate.setattr(store, "known_repos", lambda: pytest.fail("scanned the estate"))
    assert not (store.home() / "held.json").exists()
    assert cli.main(["inbox", "--repo", SENDER]) == 0
    assert "Inbox empty" in capsys.readouterr().out


def test_corrupt_held_mail_does_not_block_own_inbox(estate, capsys):
    path = store.home() / "held.json"
    path.write_text("{broken")
    store.save_inbox(SENDER, [inbox.Message(id="normal", sender="peer",
                                            recipient="sender", body="normal mail")])
    assert cli.main(["inbox", "--repo", SENDER]) == 0
    output = capsys.readouterr()
    assert "normal mail" in output.out
    assert len(output.err.splitlines()) == 1
    assert str(path) in output.err
    assert path.read_text() == "{broken"
    assert list(store.home().glob("held.unreadable-*.json")) == []


def test_pid_mismatch_to_bare_pane_files_nothing(estate, capsys):
    def mismatched(target, text, **kw):
        assert target == "%9" and kw["expect_pid"] == 102
        raise cli.tmuxio.SendRefused("pane belongs to a different pid")

    estate.setattr(cli.tmuxio, "send", mismatched)
    assert cli.main(["msg", "--repo", SENDER, "%9", "private note", "--pid", "102"]) == 2
    assert store.load_held() == []
    assert "NOT sent, and nothing was filed" in capsys.readouterr().err


def test_held_mail_peeks_then_drains_once(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "Foreman-Imbu", "held body"]) == 0
    capsys.readouterr()
    peer()
    estate.setenv("TMUX_PANE", "%2")
    assert cli.main(["inbox", "--repo", OTHER, "--peek", "--ids"]) == 0
    first = capsys.readouterr().out
    assert "held body" in first and first.startswith(cli.IDS_PREFIX)
    assert len(inbox.unread(store.load_inbox(OTHER), "peer")) == 1
    assert store.load_held() == []
    assert cli.main(["inbox", "--repo", OTHER, "--peek"]) == 0
    assert "held body" in capsys.readouterr().out
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "held body" in capsys.readouterr().out
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "Inbox empty" in capsys.readouterr().out


@pytest.mark.parametrize("target", ["foreman-imbu", "peer"])
def test_held_mail_matches_title_or_session_id(estate, capsys, target):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, target, "found later"]) == 0
    peer()
    estate.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER, "--peek", "--ids"]) == 0
    out = capsys.readouterr().out
    assert "found later" in out
    ids = out.splitlines()[0][len(cli.IDS_PREFIX):]
    assert cli.main(["inbox", "--repo", OTHER, "--ack", ids]) == 0
    assert inbox.unread(store.load_inbox(OTHER), "peer") == []
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "Inbox empty" in capsys.readouterr().out


def test_two_live_sessions_sharing_name_get_nothing(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "private note"]) == 0
    peer()
    peer(THIRD, "other-peer", "%3", "foreman-imbu", "third:3")
    for repo, pane in [(OTHER, "%2"), (THIRD, "%3")]:
        estate.setenv("TMUX_PANE", pane)
        assert cli.main(["inbox", "--repo", repo, "--quiet"]) == 0
        out = capsys.readouterr().out
        assert "ambiguous" in out and "private note" not in out
        assert store.load_inbox(repo) == []
    assert len(store.load_held()) == 1


def test_two_processes_with_one_session_id_keep_held_mail_ambiguous(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "private note"]) == 0
    peer()
    peer(THIRD, "peer", "%3", "foreman-imbu", "third:3", pid=103)
    for repo, pane in [(OTHER, "%2"), (THIRD, "%3")]:
        estate.setenv("TMUX_PANE", pane)
        assert cli.main(["inbox", "--repo", repo, "--quiet"]) == 0
        out = capsys.readouterr().out
        assert "ambiguous" in out and "private note" not in out
        assert store.load_inbox(repo) == []
    assert [(m.recipient, m.body) for m in store.load_held()] == [
        ("foreman-imbu", "private note")]


def test_non_latin_name_delivers_held_mail(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "会議", "meeting note"]) == 0
    assert [(m.recipient, m.body) for m in store.load_held()] == [("会議", "meeting note")]
    peer(title="会議")
    estate.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "meeting note" in capsys.readouterr().out
    assert store.load_held() == []


def test_empty_held_name_is_refused(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "   ", "private note"]) == 2
    assert store.load_held() == []
    assert "NOT sent, and nothing was filed" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["%9 ", " %9", "%%9"])
def test_a_padded_or_doubled_pane_address_is_never_held(estate, capsys, target):
    # The fix pass found "%9 " held under "9", which a session titled "9" then read.
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, target, "private note"]) == 2
    assert store.load_held() == []
    assert "NOT sent, and nothing was filed" in capsys.readouterr().err


def test_a_mixed_script_name_does_not_share_a_key_with_its_latin_tail(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "会議A", "private note"]) == 0
    peer(title="A")
    estate.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "private note" not in capsys.readouterr().out
    assert len(store.load_held()) == 1


def test_a_status_glyph_on_a_non_latin_name_still_delivers(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "✳ 会議", "meeting note"]) == 0
    peer(title="会議")
    estate.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "meeting note" in capsys.readouterr().out
    assert store.load_held() == []


@pytest.mark.parametrize("target,title", [("%9 ", "9"), ("会議A", "A")])
def test_a_registered_title_is_matched_on_the_held_key(estate, capsys, target, title):
    # The third pass found the registered path still used `clean_title`, so these filed
    # straight into the inbox of a session with a different name.
    hold_budget(estate)
    peer(title=title)
    cli.main(["msg", "--repo", SENDER, target, "private note"])
    assert store.load_inbox(OTHER) == []


def test_a_decorated_pane_address_is_never_held(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "✳ %9", "private note"]) == 2
    assert store.load_held() == []


def test_names_that_only_casefold_alike_stay_distinct(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "Straße", "private note"]) == 0
    peer(title="Strasse")
    estate.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "private note" not in capsys.readouterr().out
    assert len(store.load_held()) == 1


def test_a_dash_body_reads_stdin_like_zamu_tell(estate, monkeypatch):
    # 2026-11-05: a session wrote `msg <peer> - < file`, `zamu tell`'s convention, and two
    # messages went out whose whole body was "-". The peer received nothing it could use.
    import io
    hold_budget(estate)
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: pytest.fail("budgeted send"))
    monkeypatch.setattr("sys.stdin", _Stdin(b"the real body\nline two\n"))
    assert cli.main(["msg", "--repo", SENDER, "keripy-tls-pin", "-"]) == 0
    assert [m.body for m in store.load_held()] == ["the real body\nline two"]


def test_a_dash_body_with_empty_stdin_is_refused(estate, monkeypatch, capsys):
    import io
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: pytest.fail("sent an empty body"))
    monkeypatch.setattr("sys.stdin", _Stdin(b"  \n"))
    assert cli.main(["msg", "--repo", SENDER, "keripy-tls-pin", "-"]) == 2
    assert store.load_held() == []
    assert "stdin" in capsys.readouterr().err


class _Stdin:
    def __init__(self, data: bytes = b"", exc: Exception | None = None):
        import io
        self.buffer = io.BytesIO(data)
        if exc is not None:
            def boom(*a, **k):
                raise exc
            self.buffer.read = boom


@pytest.mark.parametrize("stdin", [
    _Stdin(b"\xff\xfe not utf-8"),       # p1.3: a traceback, not a refusal
    _Stdin(exc=OSError("read failed")),  # p1.3
    _Stdin(b"x" * (cli.MSG_STDIN_LIMIT + 1)),  # p1.2: unbounded read
])
def test_a_dash_body_refuses_unreadable_or_oversized_stdin(estate, monkeypatch, capsys, stdin):
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: pytest.fail("sent"))
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["msg", "--repo", SENDER, "keripy-tls-pin", "-"]) == 2
    assert store.load_held() == []
    assert "stdin" in capsys.readouterr().err


def test_a_crlf_stdin_body_is_delivered_with_plain_newlines(estate, monkeypatch):
    # Last-look p3.3 (~3d2d): the binary read keeps CR, which validate then refuses.
    hold_budget(estate)
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: pytest.fail("budgeted send"))
    monkeypatch.setattr("sys.stdin", _Stdin(b"hello\r\nworld\r\n"))
    assert cli.main(["msg", "--repo", SENDER, "keripy-tls-pin", "-"]) == 0
    assert [m.body for m in store.load_held()] == ["hello\nworld"]


# ----- held mail expires after 72 hours (2026-11-05) -------------------------------------

def _held(mid, created, recipient="foreman-imbu"):
    return inbox.Message(id=mid, sender="a peer", recipient=recipient, body=f"body {mid}",
                         created=created)


def test_unexpired_held_keeps_fresh_and_drops_stale_and_unreadable():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    iso = lambda d: d.isoformat(timespec="seconds")
    msgs = [
        _held("fresh", iso(now - timedelta(hours=71))),
        _held("edge", iso(now - store.HELD_TTL)),
        _held("stale", iso(now - timedelta(hours=73))),
        _held("empty", ""),
        _held("garbage", "last tuesday"),
        _held("naive", "2026-11-05T11:00:00"),
        _held("future", iso(now + timedelta(days=30))),
        _held("skewed", iso(now + timedelta(minutes=10))),
    ]
    kept, expired = store.unexpired_held(msgs, now)
    assert [m.id for m in kept] == ["fresh", "edge", "skewed"]
    assert [m.id for m in expired] == ["stale", "empty", "garbage", "naive", "future"]


def test_a_new_hold_prunes_expired_mail_and_states_the_deadline(estate, capsys):
    hold_budget(estate)
    store.save_held([_held("old", "2026-10-13T00:00:00+00:00", "someone-gone")])
    assert cli.main(["msg", "--repo", SENDER, "Foreman-Imbu", "new body"]) == 0
    assert "within 72 hours" in capsys.readouterr().out
    assert [m.body for m in store.load_held()] == ["new body"]


def test_reading_an_inbox_prunes_expired_held_mail_it_does_not_deliver(estate, capsys):
    store.save_held([_held("old", "2026-10-13T00:00:00+00:00", "someone-gone"),
                     _held("bad", "", "someone-gone")])
    assert cli.main(["inbox", "--repo", SENDER]) == 0
    assert store.load_held() == []


def test_expired_held_mail_is_never_delivered_to_its_name(estate, capsys):
    store.save_held([_held("old", "2026-10-13T00:00:00+00:00")])
    peer()
    estate.setenv("TMUX_PANE", "%2")
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "body old" not in capsys.readouterr().out
    assert store.load_inbox(OTHER) == [] and store.load_held() == []


def test_a_failed_prune_does_not_block_the_sessions_own_inbox(estate, capsys):
    store.save_held([_held("old", "2026-10-13T00:00:00+00:00", "someone-gone")])
    store.save_inbox(SENDER, [inbox.Message(id="normal", sender="peer",
                                            recipient="sender", body="normal mail")])

    def refuse(messages):
        raise PermissionError("read-only state")

    estate.setattr(store, "save_held", refuse)
    assert cli.main(["inbox", "--repo", SENDER]) == 0
    out = capsys.readouterr()
    assert "normal mail" in out.out
    assert "held mail could not be updated" in out.err


def test_unwritable_held_mail_is_left_held_rather_than_delivered_twice(estate, capsys):
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save_held([_held("mine", fresh)])
    store.save_inbox(OTHER, [inbox.Message(id="normal", sender="x", recipient="peer",
                                           body="normal mail")])
    peer()
    estate.setenv("TMUX_PANE", "%2")
    real_save = store.save_held

    def refuse(messages):
        raise PermissionError("immutable")

    estate.setattr(store, "save_held", refuse)
    assert cli.main(["inbox", "--repo", OTHER, "--peek"]) == 0
    out = capsys.readouterr()
    assert "normal mail" in out.out and "body mine" not in out.out
    assert "stays held" in out.err
    assert [m.id for m in store.load_held()] == ["mine"]
    assert [m.id for m in store.load_inbox(OTHER)] == ["normal"]
    # Once held mail can be written again, the message is delivered exactly once.
    estate.setattr(store, "save_held", real_save)
    assert cli.main(["inbox", "--repo", OTHER, "--peek"]) == 0
    assert "body mine" in capsys.readouterr().out
    assert store.load_held() == []


def test_a_removal_that_fails_after_the_check_takes_the_message_back(estate, capsys):
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save_held([_held("mine", fresh)])
    peer()
    estate.setenv("TMUX_PANE", "%2")
    real_save = store.save_held
    calls = []

    def first_ok_then_refuse(messages):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("disk went away")
        return real_save(messages)

    estate.setattr(store, "save_held", first_ok_then_refuse)
    assert cli.main(["inbox", "--repo", OTHER, "--peek"]) == 0
    out = capsys.readouterr()
    assert "body mine" not in out.out and "stays held" in out.err
    assert [m.id for m in store.load_held()] == ["mine"]
    assert store.load_inbox(OTHER) == []


def test_a_copy_left_by_a_failed_restore_is_taken_back_on_the_next_read(estate, capsys):
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save_held([_held("mine", fresh)])
    # What an earlier read leaves when both its removal and its restore failed.
    store.save_inbox(OTHER, [replace_recipient(_held("mine", fresh), "peer")])
    peer()
    estate.setenv("TMUX_PANE", "%2")
    real_save = store.save_held
    calls = []

    def first_ok_then_refuse(messages):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("still failing")
        return real_save(messages)

    estate.setattr(store, "save_held", first_ok_then_refuse)
    assert cli.main(["inbox", "--repo", OTHER, "--peek"]) == 0
    assert "body mine" not in capsys.readouterr().out
    assert store.load_inbox(OTHER) == []
    assert [m.id for m in store.load_held()] == ["mine"]


def replace_recipient(m, who):
    from dataclasses import replace
    return replace(m, recipient=who)
