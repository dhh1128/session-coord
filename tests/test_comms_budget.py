"""The doorbell budget, reply expectation, and the one-line body rule.

These exist because exhortation was tried and failed in front of us: two sessions diagnosed
their own over-messaging correctly *in the same breath as committing it*. So every test here is
about a mechanism that does not depend on a session agreeing with it.
"""

from __future__ import annotations

import time

import pytest
from sessioncoord import cli, comms, store
from sessioncoord.model import Presence


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    # The sender's identity comes from the PANE, never from --session: trusting the argument
    # made the budget bypassable by inventing an id. So the tests have to inhabit a pane.
    monkeypatch.setenv("TMUX_PANE", "%1")
    return tmp_path


@pytest.fixture
def fake_world(monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 111, "%2": 222})
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda p, run=None: "me:1")
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: 1)


@pytest.fixture
def rings(monkeypatch):
    """Capture doorbells without a tmux server."""
    out = []

    def fake_send(target, text, expect_pid=None, sender="", run=None, force=False,
                  spill_over=None):
        out.append((target, text))
        return "%2"

    monkeypatch.setattr(cli.tmuxio, "send", fake_send)
    return out


def reg(session_id, pane, pid, window):
    store.save(Presence(session_id=session_id, repo_root="/repo", workdir="/repo",
                        pane=pane, claude_pid=pid, window=window, tmux_server=1))


def pair():
    reg("sender", "%1", 111, "me:1")
    reg("peer", "%2", 222, "me:2")


def send(text="a sequencing question", **kw):
    argv = ["msg", "peer", text, "--repo", "/repo"]
    for k, v in kw.items():
        argv += [f"--{k}", str(v)]
    return cli.main(argv)


class TestReplyExpectation:
    def test_none_is_the_default(self):
        assert comms.check_reply("") == comms.REPLY_NONE
        assert comms.check_reply(None) == comms.REPLY_NONE

    def test_anything_else_is_refused(self):
        with pytest.raises(comms.CommsError, match="--reply"):
            comms.check_reply("maybe")


class TestComposedBody:
    def test_a_no_reply_body_is_one_line(self):
        # `tmuxio` refuses control characters, so a newline here made every `--reply none`
        # message unsendable -- the default setting of the default channel, broken, and
        # invisible to a suite that stubs the component doing the refusing.
        body = comms.compose("a message", comms.REPLY_NONE)
        assert "\n" not in body and "\r" not in body
        assert comms.NO_REPLY_MARKER in body

    def test_reply_required_carries_no_marker(self):
        assert comms.NO_REPLY_MARKER not in comms.compose("who owns this?",
                                                          comms.REPLY_REQUIRED)

    def test_multi_line_input_is_flattened_rather_than_refused(self):
        assert "\n" not in comms.compose("first\nsecond", comms.REPLY_NONE)


class TestBudgetAccounting:
    def test_counts_only_this_session(self):
        sends = [comms.Sent("a", "x", 100.0), comms.Sent("b", "x", 100.0)]
        assert len(comms.spent(sends, "a", now=100.0)) == 1

    def test_forgets_sends_older_than_the_window(self):
        sends = [comms.Sent("a", "x", 0.0), comms.Sent("a", "x", 3500.0)]
        assert len(comms.spent(sends, "a", now=3600.0)) == 1

    def test_an_override_still_counts(self):
        # Otherwise urgency buys unlimited headroom. The override records a reason; it does not
        # refund the turn the recipient spent.
        sends = [comms.Sent("a", "x", 1.0, override="prod is down")]
        assert comms.remaining(sends, "a", now=2.0) == comms.BUDGET_PER_HOUR - 1


class TestRefusalText:
    def test_names_what_was_spent_so_triage_is_possible(self):
        recent = [comms.Sent("a", "me:4", 3000.0), comms.Sent("a", "me:5", 1200.0)]
        text = comms.refusal(recent, now=3600.0)
        assert "me:4" in text and "me:5" in text and "--override" in text

    def test_states_the_test_rather_than_asking_for_restraint(self):
        assert "do differently" in comms.refusal([], now=0.0)


class TestBudgetGatesTheDoorbellNotTheFiling:
    def test_rings_while_under_budget(self, home, fake_world, rings, capsys):
        pair()
        assert send() == 0
        assert len(rings) == 1
        assert "doorbells left this hour" in capsys.readouterr().out

    def test_over_budget_still_FILES_but_does_not_ring(self, home, fake_world, rings, capsys):
        # The whole point of the inbox: filing costs the recipient nothing, so rationing it
        # would ration the wrong thing. Over budget the message is still delivered, quietly.
        pair()
        for i in range(comms.BUDGET_PER_HOUR):
            send(f"m{i}")
            store.save_inbox("/repo", [])  # drain, so `needs_doorbell` keeps ringing
        rings.clear()
        capsys.readouterr()

        assert send("one more") == 0
        assert rings == [], "the doorbell must not ring past the budget"
        queue = store.load_inbox("/repo")
        assert any("one more" in m.body for m in queue), "but the message must still be filed"
        assert "NOT rung" in capsys.readouterr().err

    def test_an_override_rings_past_the_budget_and_records_why(self, home, fake_world, rings):
        pair()
        for i in range(comms.BUDGET_PER_HOUR):
            send(f"m{i}")
            store.save_inbox("/repo", [])
        rings.clear()
        assert send("prod is down", override="the deploy will corrupt state") == 0
        assert len(rings) == 1
        assert any(s.override for s in store.load_sends("/repo"))

    def test_one_session_cannot_spend_anothers_budget(self, home, fake_world, rings,
                                                       monkeypatch):
        pair()
        reg("third", "%3", 333, "me:3")
        for i in range(comms.BUDGET_PER_HOUR):
            send(f"m{i}")
            store.save_inbox("/repo", [])
        rings.clear()
        # A different pane is a different session, and so a different budget.
        monkeypatch.setenv("TMUX_PANE", "%3")
        assert cli.main(["msg", "peer", "hello", "--repo", "/repo"]) == 0
        assert len(rings) == 1


class TestSendLog:
    def test_appends_rather_than_rewrites(self, home):
        for who in ("a", "a", "b"):
            store.record_send("/repo", comms.Sent(who, "x", 1.0))
        assert len(store.load_sends("/repo")) == 3

    def test_an_UNTERMINATED_final_line_is_skipped_not_fatal(self, home):
        # What a torn append actually looks like: the writer was interrupted before its
        # newline landed. The earlier version of this test wrote a trailing newline and still
        # called the line torn -- the same mistake the code made, which is why both passed.
        store.record_send("/repo", comms.Sent("a", "x", 1.0))
        with open(store._sends_path("/repo"), "a") as f:
            f.write('{"session_id": "a", "at": ')  # no newline: mid-write
        assert len(store.load_sends("/repo")) == 1

    def test_a_TERMINATED_but_corrupt_final_line_is_fatal(self, home):
        # It ends in a newline, so the writer finished. A complete record that does not parse
        # is corrupt, and skipping it understates the budget like any other lost row.
        store.record_send("/repo", comms.Sent("a", "x", 1.0))
        with open(store._sends_path("/repo"), "a") as f:
            f.write('{"session_id": "a", "at": \n')
        with pytest.raises(store.UnreadableSends, match="corrupt rather than half-written"):
            store.load_sends("/repo")

    def test_a_malformed_EARLIER_line_makes_the_budget_unavailable(self, home):
        # Copilot, PR #12 review body: skipping this hands back headroom already spent. A
        # record lost from the middle is a doorbell that rang and is no longer counted.
        path = store._sends_path("/repo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"session_id": "a", "at": \n{"session_id": "a", "at": 2.0}\n')
        with pytest.raises(store.UnreadableSends, match="not the final record"):
            store.load_sends("/repo")

    def test_an_absent_log_is_empty(self, home):
        assert store.load_sends("/repo") == []


class TestBudgetLine:
    def test_silent_well_below_the_budget(self):
        assert comms.budget_line([], "a", now=0.0) == ""

    def test_speaks_once_half_is_gone(self):
        sends = [comms.Sent("a", "x", 1.0), comms.Sent("a", "x", 2.0)]
        assert "2/3" in comms.budget_line(sends, "a", now=3.0)

    def test_check_never_mentions_the_budget_under_quiet(self, home, fake_world, capsys):
        # `_me` resolves by pane, and the fixture puts this test in %1 = "sender".
        # `--quiet` is the PreToolUse gate's contract for "blocking findings only". Anything
        # else printed there is noise on every tool call in every session.
        pair()
        for i in range(comms.BUDGET_PER_HOUR):
            store.record_send("/repo", comms.Sent("sender", "peer", 1.0 + i))
        cli.main(["check", "--repo", "/repo", "--quiet"])
        assert "comms budget" not in capsys.readouterr().out

class TestTheReplyExpectationReachesTheRecipient:
    """Copilot, PR #12: `compose` was written, tested, imported -- and never called."""

    def test_a_filed_message_carries_the_expectation_as_metadata(self, home, fake_world, rings):
        # As a FIELD, not appended to the body. The body is what the sender wrote, and
        # rewriting prose to carry metadata destroys the formatting filing exists to preserve.
        pair()
        send("a sequencing question")
        m = store.load_inbox("/repo")[0]
        assert m.body == "a sequencing question", "the body must be untouched"
        assert m.reply == comms.REPLY_NONE

    def test_the_recipient_sees_it_when_the_inbox_renders(self, home, fake_world, rings,
                                                          capsys, monkeypatch):
        pair()
        send("fyi")
        capsys.readouterr()
        monkeypatch.setenv("TMUX_PANE", "%2")
        cli.main(["inbox", "--repo", "/repo"])
        assert comms.NO_REPLY_MARKER in capsys.readouterr().out

    def test_reply_required_says_nothing_about_not_replying(self, home, fake_world, rings,
                                                            capsys, monkeypatch):
        pair()
        send("who owns this?", reply="required")
        capsys.readouterr()
        monkeypatch.setenv("TMUX_PANE", "%2")
        cli.main(["inbox", "--repo", "/repo"])
        assert comms.NO_REPLY_MARKER not in capsys.readouterr().out


class TestIdentityCannotBeSpoofed:
    """Copilot, PR #12: trusting --session made the limit bypassable by inventing an id."""

    def test_an_invented_session_is_refused_rather_than_given_a_fresh_budget(
            self, home, fake_world, rings, capsys):
        pair()
        assert cli.main(["msg", "peer", "hello", "--repo", "/repo",
                         "--session", "not-this-pane"]) == 2
        assert "not this pane's session" in capsys.readouterr().err
        assert rings == []

    def test_naming_your_own_session_is_allowed(self, home, fake_world, rings):
        pair()
        assert cli.main(["msg", "peer", "hello", "--repo", "/repo",
                         "--session", "sender"]) == 0
        assert len(rings) == 1


class TestBudgetFailsClosed:
    """Copilot, PR #12: an unreadable log read as `nothing spent`, granting a fresh budget."""

    def test_an_unreadable_log_withholds_the_ring_but_still_files(self, home, fake_world,
                                                                  rings, capsys):
        pair()
        store._sends_path("/repo").parent.mkdir(parents=True, exist_ok=True)
        store._sends_path("/repo").write_text("{}")
        store._sends_path("/repo").chmod(0o000)
        try:
            rc = send("something worth saying")
        finally:
            store._sends_path("/repo").chmod(0o644)
        assert rc == 0
        assert rings == [], "an uncheckable budget is assumed spent"
        assert any("worth saying" in m.body for m in store.load_inbox("/repo"))
        assert "NOT rung" in capsys.readouterr().err

    def test_a_missing_log_is_simply_empty(self, home):
        assert store.load_sends("/repo") == []

    def test_an_unreadable_log_raises_rather_than_reading_as_empty(self, home):
        store._sends_path("/repo").parent.mkdir(parents=True, exist_ok=True)
        store._sends_path("/repo").write_text("{}")
        store._sends_path("/repo").chmod(0o000)
        try:
            with pytest.raises(store.UnreadableSends):
                store.load_sends("/repo")
        finally:
            store._sends_path("/repo").chmod(0o644)

class TestTheBudgetIsPerSessionNotPerRepo:
    """Copilot, PR #12: it was keyed on the repo, so switching repos bought three more."""

    def test_the_log_does_not_depend_on_the_repo(self, home):
        assert store._sends_path("/repo") == store._sends_path("/some/other/repo")

    def test_rings_in_one_repo_count_against_another(self, home):
        for i in range(comms.BUDGET_PER_HOUR):
            store.record_send("/repo", comms.Sent("sender", "peer", 1.0 + i, id=f"i{i}"))
        assert comms.remaining(store.load_sends("/elsewhere"), "sender", now=2.0) == 0


class TestChargedOnAttempt:
    """Charging on attempt, after three rounds of trying to make a refund safe."""

    def test_a_refused_ring_is_still_charged(self, home, fake_world, monkeypatch, capsys):
        # The refund is gone on purpose. It produced three findings in a row: a window where
        # an interruption was free, a reversal to close it, and then the discovery that
        # `SendRefused` covers "typed but not submitted" too -- so refunding could leave an
        # uncharged doorbell sitting in a peer's prompt. Charging on attempt removes all three,
        # at the cost of one doorbell an hour when a peer is unreachable.
        pair()

        def refuse(*a, **kw):
            raise cli.tmuxio.SendRefused("no pane matches")

        monkeypatch.setattr(cli.tmuxio, "send", refuse)
        assert send("something") == 0
        capsys.readouterr()
        assert len(store.load_sends("/repo")) == 1, "one charge, and no reversal beside it"
        assert comms.remaining(store.load_sends("/repo"), "sender",
                               now=time.time()) == comms.BUDGET_PER_HOUR - 1

    def test_the_message_is_still_filed_when_the_ring_is_refused(self, home, fake_world,
                                                                 monkeypatch, capsys):
        # The charge is for the attempt; the delivery happened regardless, which is what makes
        # charging for it honest rather than punitive.
        pair()
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                cli.tmuxio.SendRefused("no pane")))
        send("still delivered")
        capsys.readouterr()
        assert any("still delivered" in m.body for m in store.load_inbox("/repo"))

    def test_a_reversal_written_by_an_older_build_is_still_honoured(self):
        # Nothing writes these any more, but a log from an older build can hold them, and
        # counting a cancelled charge would overstate what a session had spent.
        sends = [
            comms.Sent("a", "x", 1.0, id="one"),
            comms.Sent("a", "x", 2.0, id="rev", reverses="one"),
        ]
        assert comms.spent(sends, "a", now=3.0) == []

    def test_a_successful_ring_stays_charged(self, home, fake_world, rings):
        pair()
        send("something")
        assert comms.remaining(store.load_sends("/repo"), "sender",
                               now=time.time()) == comms.BUDGET_PER_HOUR - 1

    def test_a_reversal_cancels_only_its_own_charge(self):
        sends = [
            comms.Sent("a", "x", 1.0, id="one"),
            comms.Sent("a", "x", 2.0, id="two"),
            comms.Sent("a", "x", 3.0, id="rev", reverses="one"),
        ]
        assert [s.id for s in comms.spent(sends, "a", now=4.0)] == ["two"]


class TestCheckSurvivesADamagedBudgetLog:
    """Copilot, PR #12: my own fail-closed fix gave `check` a traceback path."""

    def test_check_reports_unavailable_rather_than_raising(self, home, fake_world, capsys):
        pair()
        store._sends_path("/repo").parent.mkdir(parents=True, exist_ok=True)
        store._sends_path("/repo").write_text("{}")
        store._sends_path("/repo").chmod(0o000)
        try:
            rc = cli.main(["check", "--repo", "/repo"])
        finally:
            store._sends_path("/repo").chmod(0o644)
        assert rc in (0, 1, 2), "check must return a finding code, not raise"
        assert "budget: unavailable" in capsys.readouterr().out

class TestTheDirectPathIsBudgetedToo:
    """Copilot, PR #12: the limit lived only in the inbox branch, so addressing an
    unregistered target bypassed it entirely -- and that path types the WHOLE message."""

    def test_an_unregistered_target_still_spends_budget(self, home, fake_world, rings):
        reg("sender", "%1", 111, "me:1")
        assert cli.main(["msg", "%9", "hello", "--repo", "/repo"]) == 0
        assert len(rings) == 1
        assert comms.remaining(store.load_sends("/repo"), "sender",
                               now=time.time()) == comms.BUDGET_PER_HOUR - 1

    def test_over_budget_the_direct_send_is_refused_and_says_nothing_was_filed(
            self, home, fake_world, rings, capsys):
        reg("sender", "%1", 111, "me:1")
        for i in range(comms.BUDGET_PER_HOUR):
            cli.main(["msg", "%9", f"m{i}", "--repo", "/repo"])
        rings.clear()
        capsys.readouterr()
        # Refusing here does NOT deliver: there is no inbox for an unregistered target, so the
        # message is lost rather than delayed, and the refusal has to say so.
        assert cli.main(["msg", "%9", "one more", "--repo", "/repo"]) == 2
        assert rings == []
        assert store.load_held() == []
        assert "nothing was filed" in capsys.readouterr().err


class TestOverrideOnAnUnreadableLog:
    """Copilot, PR #12: fail-closed ignored --override, which is the one flag for exactly
    the case where the tool cannot decide for you."""

    @staticmethod
    def _corrupt_but_writable():
        """Unreadable CONTENT in a writable file -- the case the finding is actually about.

        `chmod 000` was the first attempt and it tests something else: the file is then
        unwritable too, so the reservation cannot be recorded and the send is refused whatever
        the flag says. That refusal is correct on its own terms -- ringing without recording
        removes the limit silently -- but it meant the test never exercised `--override`.
        """
        path = store._sends_path("/repo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"session_id": "a", "at": \n{"session_id": "a", "at": 2.0}\n')

    def test_override_rings_even_when_the_budget_cannot_be_read(self, home, fake_world, rings):
        pair()
        self._corrupt_but_writable()
        assert send("prod is down", override="the deploy will corrupt state") == 0
        assert len(rings) == 1

    def test_without_override_it_still_fails_closed(self, home, fake_world, rings, capsys):
        pair()
        self._corrupt_but_writable()
        assert send("ordinary") == 0
        assert rings == []
        assert "NOT rung" in capsys.readouterr().err


class TestTheBudgetDecisionIsSerialised:
    """Copilot, PR #12: check-and-ring was not atomic, so two sends could both pass."""

    def test_the_lock_is_estate_wide_not_per_repo(self, home):
        from sessioncoord import locks
        estate = locks._lock_path(locks.ESTATE, "doorbell")
        per_repo = locks._lock_path("/repo", "doorbell")
        assert estate != per_repo
        # Two repos must contend with each other, because the budget spans them.
        assert locks._lock_path(locks.ESTATE, "doorbell") == estate

    def test_a_held_lock_withholds_the_ring_rather_than_ringing_anyway(
            self, home, fake_world, rings, capsys):
        from sessioncoord import locks
        pair()
        with locks.hold(locks.ESTATE, "doorbell", wait=5.0, owner="someone else"):
            rc = send("concurrent", **{"repo": "/repo"}) if False else cli.main(
                ["msg", "peer", "concurrent", "--repo", "/repo"])
        assert rc == 0
        assert rings == [], "a contended budget must not ring on a guess"
        assert "NOT rung" in capsys.readouterr().err

class TestAnIncompleteSendRecordIsNotSilentlyIgnored:
    """Copilot, PR #12 review body: `{}` parsed to Sent("", "", 0.0) and was then ignored."""

    def test_a_record_missing_accounting_fields_is_rejected(self):
        with pytest.raises(ValueError, match="missing"):
            comms.Sent.from_dict({})
        with pytest.raises(ValueError, match="target"):
            comms.Sent.from_dict({"session_id": "a", "at": 1.0})

    def test_optional_fields_still_default(self):
        s = comms.Sent.from_dict({"session_id": "a", "target": "x", "at": 1.0})
        assert s.reply == comms.REPLY_NONE and s.override == "" and s.reverses == ""

    def test_an_incomplete_record_in_the_log_makes_the_budget_unavailable(self, home):
        # It belongs to no session, so `spent` skipped it and the headroom came back -- the
        # same fail-open shape as an unreadable log reading as empty, one level further in.
        path = store._sends_path("/repo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n{"session_id": "a", "target": "x", "at": 2.0}\n')
        with pytest.raises(store.UnreadableSends):
            store.load_sends("/repo")

class TestNonFiniteTimestamps:
    """Copilot, PR #12 review body: NaN and inf are not times, and each moves the budget."""

    def test_nan_is_rejected(self):
        # NaN compares False against everything, so the record vanished from the accounting.
        with pytest.raises(ValueError, match="non-finite"):
            comms.Sent.from_dict({"session_id": "a", "target": "x", "at": float("nan")})

    def test_infinity_is_rejected(self):
        # inf compares True forever, so one would sit in the window permanently.
        with pytest.raises(ValueError, match="non-finite"):
            comms.Sent.from_dict({"session_id": "a", "target": "x", "at": float("inf")})

    def test_an_ordinary_timestamp_still_loads(self):
        assert comms.Sent.from_dict({"session_id": "a", "target": "x", "at": 1.5}).at == 1.5

class TestAccountingIdentitiesMustBeReal:
    """Copilot, PR #12: the key check asked whether the field was there, not whether it said
    anything. `{"session_id": "", ...}` loaded, belonged to nobody, and was skipped."""

    def test_an_empty_session_id_is_rejected(self):
        with pytest.raises(ValueError, match="empty or non-string session_id"):
            comms.Sent.from_dict({"session_id": "", "target": "x", "at": 1.0})

    def test_a_whitespace_target_is_rejected(self):
        with pytest.raises(ValueError, match="empty or non-string target"):
            comms.Sent.from_dict({"session_id": "a", "target": "   ", "at": 1.0})

    def test_a_non_string_identity_is_rejected(self):
        with pytest.raises(ValueError, match="empty or non-string"):
            comms.Sent.from_dict({"session_id": 7, "target": "x", "at": 1.0})

class TestTheSenderIsFoundAcrossTheEstate:
    """Copilot, PR #12: `_me(repo)` looked at the TARGET's board, so a session registered in
    one repo and sending into another was unattributable -- and unattributable means
    unbudgeted. No flag needed, just a --repo pointing where this pane is not registered."""

    def test_a_session_sending_into_another_repo_is_still_budgeted(self, home, fake_world,
                                                                   rings, monkeypatch):
        # Registered HERE, messaging a peer registered THERE.
        reg("sender", "%1", 111, "me:1")
        store.save(Presence(session_id="faraway", repo_root="/other", workdir="/other",
                            pane="%2", claude_pid=222, window="me:2", tmux_server=1))
        monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/other")
        assert cli.main(["msg", "faraway", "hello", "--repo", "/other"]) == 0
        # Charged against the sending session, not skipped as unattributable.
        assert any(s.session_id == "sender" for s in store.load_sends("/other"))


class TestAcknowledgingOnlyWhatWasDelivered:
    """Copilot, PR #12: peek and drain are separate processes, so a message filed between them
    was marked read although it reached nobody."""

    def test_ack_marks_only_the_named_ids(self, home, fake_world, rings):
        from sessioncoord import inbox as inbox_mod
        pair()
        store.save_inbox("/repo", [
            inbox_mod.Message(id="m1", sender="a", recipient="peer", body="first",
                              created="2026-01-01T00:00:00"),
            inbox_mod.Message(id="m2", sender="a", recipient="peer", body="second",
                              created="2026-01-01T00:00:01"),
        ])
        assert cli.main(["inbox", "--repo", "/repo", "--session", "peer", "--quiet",
                         "--ack", "m1"]) == 0
        left = {m.id: m.read for m in store.load_inbox("/repo")}
        assert left["m1"] is True
        assert left["m2"] is False, "a message the caller never saw must stay unread"

    def test_ids_lists_without_clearing(self, home, fake_world, rings, capsys):
        from sessioncoord import inbox as inbox_mod
        pair()
        store.save_inbox("/repo", [
            inbox_mod.Message(id="m1", sender="a", recipient="peer", body="first",
                              created="2026-01-01T00:00:00"),
        ])
        assert cli.main(["inbox", "--repo", "/repo", "--session", "peer", "--quiet",
                         "--peek", "--ids"]) == 0
        # Ids on the sentinel first line, bodies after, from one snapshot.
        out = capsys.readouterr().out
        assert out.startswith(cli.IDS_PREFIX + "m1")
        assert "first" in out, "the bodies ride with the ids, not in a second call"
        assert store.load_inbox("/repo")[0].read is False
        # The hook acknowledges this snapshot right after delivering it, so the footer its
        # reader sees must not say "Still unread" (sumago, 2026-11-06).
        assert "Still unread" not in out and "will not list these again" in out

    def test_an_empty_inbox_points_at_mail_the_hook_delivered(self, home, fake_world, rings,
                                                             capsys):
        pair()
        assert cli.main(["inbox", "--repo", "/repo", "--session", "peer"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("Inbox empty.") and "start-of-turn hook" in out

class TestAClearStrandsMailAndThatIsKnown:
    """The rebind that used to live here was removed; see cli._register_locked.

    It could not fail safely: registration must never fail over mail, so a failed rebind fell
    through to the record removal and orphaned the mail it existed to save. These tests pin the
    CURRENT behaviour -- the gap is real, recorded as P-R1Z5 on PR #11 and as A-W552 -- so that
    whoever fixes it properly has to change a test that says what is broken, rather than
    discovering the gap from a user.
    """

    def test_unread_mail_does_not_follow_the_session_through_a_clear(self, home, fake_world,
                                                                     monkeypatch):
        from sessioncoord import inbox as inbox_mod
        monkeypatch.setenv("TMUX_PANE", "%1")
        reg("before", "%1", 111, "me:1")
        store.save_inbox("/repo", [
            inbox_mod.Message(id="m1", sender="peer", recipient="before", body="important",
                              created="2026-01-01T00:00:00"),
        ])
        assert cli.main(["register", "--repo", "/repo", "--session", "after",
                         "--pane", "%1", "--pid", "111"]) == 0
        # KNOWN GAP: still addressed to the departed id, so `inbox --session after` is silent.
        assert [m.recipient for m in store.load_inbox("/repo")] == ["before"]

    def test_the_mail_is_stranded_rather_than_deleted(self, home, fake_world, monkeypatch):
        # Which is why the gap is tolerable for now: nothing is destroyed, and a fix can
        # recover it later. A rebind that loses mail on failure could not say the same.
        from sessioncoord import inbox as inbox_mod
        monkeypatch.setenv("TMUX_PANE", "%1")
        reg("before", "%1", 111, "me:1")
        store.save_inbox("/repo", [
            inbox_mod.Message(id="m1", sender="peer", recipient="before", body="important",
                              created="2026-01-01T00:00:00"),
        ])
        cli.main(["register", "--repo", "/repo", "--session", "after",
                  "--pane", "%1", "--pid", "111"])
        assert len(store.load_inbox("/repo")) == 1
        assert store.load_inbox("/repo")[0].body == "important"


class TestAStalePaneRecordCannotAbsorbTheCharge:
    """Copilot, PR #12: %N ids are reused across a tmux restart, so a record from a previous
    incarnation could match this pane and be billed instead."""

    def test_a_record_from_another_tmux_server_is_not_this_session(self, home, fake_world,
                                                                    monkeypatch, rings):
        monkeypatch.setenv("TMUX_PANE", "%1")
        # Registered against a DIFFERENT tmux server incarnation.
        store.save(Presence(session_id="ghost", repo_root="/repo", workdir="/repo",
                            pane="%1", claude_pid=111, window="me:1", tmux_server=999))
        reg("peer", "%2", 222, "me:2")
        cli.main(["msg", "peer", "hello", "--repo", "/repo"])
        charged = {s.session_id for s in store.load_sends("/repo")}
        assert "ghost" not in charged, "a stale record must not be billed"

    def test_a_legacy_record_without_a_server_id_still_counts(self, home, fake_world,
                                                              monkeypatch, rings):
        # Written before the field existed: accepted for compatibility rather than treated as
        # a mismatch, which would silently unbudget every pre-upgrade session.
        monkeypatch.setenv("TMUX_PANE", "%1")
        store.save(Presence(session_id="legacy", repo_root="/repo", workdir="/repo",
                            pane="%1", claude_pid=111, window="me:1"))
        reg("peer", "%2", 222, "me:2")
        cli.main(["msg", "peer", "hello", "--repo", "/repo"])
        assert any(s.session_id == "legacy" for s in store.load_sends("/repo"))
