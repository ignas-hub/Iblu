"""Mail a machine sent, filed under his name.

The 04:45 block in the screenshot: five identical "Machina: Tagger accuracy
below target" messages across three seconds, posted by a monitoring job through
`machina@deadlift.io` — a verified send-as alias on his own Deadlift mailbox,
which is why the alias check admitted them as his work.

Two mechanisms, tested separately because they fail differently. The named
service identity is a stated fact and can be wrong only by being out of date.
The fan-out rule is an inference, and the thing worth pinning about an
inference is where its edges are.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from iblu_keeper.collectors import automated

NOW = datetime(2026, 9, 30, 2, 58, tzinfo=timezone.utc)


class _FakeConn:
    """Answers the SELECT, records the UPDATE. Enough to test the decision."""

    def __init__(self, rows):
        self._rows = rows
        self.updates: list[tuple] = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append(params)
            return self
        return self

    def fetchall(self):
        return self._rows


def _row(id: int, peers: int, *, subject="Machina: Tagger accuracy below target"):
    return {
        "id": id, "account": "admin@deadlift.io", "subject": subject,
        "occurred_at": NOW, "peers": peers,
    }


# --- the burst ---------------------------------------------------------------


def test_a_burst_of_identical_subjects_is_excluded():
    conn = _FakeConn([_row(1, 5), _row(2, 5), _row(3, 5)])
    marked = automated.mark_fan_out(conn)
    assert [m["id"] for m in marked] == [1, 2, 3]
    assert conn.updates, "the rows must actually be marked"
    reason, ids = conn.updates[0]
    assert "automated fan-out" in reason
    assert ids == [1, 2, 3]


def test_two_messages_are_not_a_burst():
    """A human can send two. The threshold is where the inference starts."""
    conn = _FakeConn([_row(1, 2), _row(2, 2)])
    assert automated.mark_fan_out(conn) == []
    assert not conn.updates


def test_the_window_is_too_short_to_compose_by_hand():
    """Two minutes, not five.

    The first draft used five minutes, which would also have swallowed a
    genuine morning of forwarding one contract to three people separately —
    under-reporting his work in order to fix over-reporting it. Nobody writes
    three separate messages in under two minutes; a cron does it in three
    seconds.
    """
    assert automated.BURST_WINDOW <= timedelta(seconds=60)
    assert automated.BURST_MIN >= 3


def test_dry_reports_without_writing():
    conn = _FakeConn([_row(1, 5), _row(2, 5), _row(3, 5)])
    marked = automated.mark_fan_out(conn, dry=True)
    assert len(marked) == 3
    assert not conn.updates, "DRY means write nothing"


def test_the_sweep_does_not_eat_its_own_evidence():
    """Re-running it must reach the same verdict, so peers ignore exclusion.

    If the peer count only counted unexcluded rows, the first run would mark
    three, the second would find two peers, fall under the threshold, and the
    function would quietly stop recognising a burst it had already identified.
    """
    sql = inspect.getsource(automated.mark_fan_out)
    peer_clause = sql.split("(SELECT count(*) FROM signals p")[1].split(") AS peers")[0]
    assert "excluded_reason" not in peer_clause
    # ...while the rows being *considered* must skip what is already excluded,
    # so a second run is cheap and idempotent.
    assert "s.excluded_reason IS NULL" in sql


def test_only_his_own_sent_mail_is_considered():
    """Inbound and other people's mail are already excluded by `actor`."""
    sql = inspect.getsource(automated.mark_fan_out)
    assert "s.actor = 'me'" in sql
    assert "s.source = 'gmail'" in sql
    # A burst needs a subject to be a burst; empty subjects are not identical,
    # they are unknown.
    assert "s.subject IS NOT NULL" in sql


# --- the named service identity ----------------------------------------------


def test_the_machina_alias_is_not_an_address_he_writes_from():
    from iblu_keeper.config import settings

    assert "machina@deadlift.io" in settings.automated_sender_addresses


def test_an_automated_sender_is_recorded_and_marked_not_dropped():
    """Marked, never discarded — `jobs/audit.py` must still be able to count it."""
    from iblu_keeper.collectors import gmail_sent

    source = inspect.getsource(gmail_sent.collect)
    assert "automated_sender_addresses" in source
    assert "excluded_reason" in source
    assert "continue" not in source.split("automated = from_address")[1][:400], (
        "an automated sender must not short-circuit the insert"
    )


def test_the_sender_address_is_always_recorded():
    """Without it the Machina rows could only be found by their subject."""
    from iblu_keeper.collectors import gmail_sent

    source = inspect.getsource(gmail_sent.collect)
    assert '"from": from_address' in source


def test_insert_signal_can_carry_an_exclusion():
    from iblu_keeper import collectors

    source = inspect.getsource(collectors.insert_signal)
    assert "excluded_reason" in source
    assert '"excluded_reason": None' in source, "default is: this IS his attention"
