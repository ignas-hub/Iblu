"""The project registry — pure logic, no database (plan §1.5 / §1.6).

`FakeConn` stands in for psycopg's connection just enough to run the SQL
`store.projects` actually issues: an in-memory dict of projects keyed by
code, a signals list for `unregistered()`, and a history log for
`set_stage()`. Modelled on `tests/test_slack_sent.py`'s `FakeConn`.

Anything that needs real Postgres (the FK to `ventures`/`stages`, the GIN
index, migration 007 itself) is left to `tests/test_migrations.py`'s
`requires_db` pattern — skipped, never failed, when `DATABASE_URL` is unset.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from iblu_keeper.collectors import venture_hints
from iblu_keeper.store import projects as P


class _Result:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Enough of psycopg.Connection to exercise store.projects."""

    def __init__(self):
        self.projects: dict[str, dict] = {}
        self.signals: list[dict] = []
        self.history: list[tuple] = []

    def add_project(self, **fields) -> dict:
        row = {
            "code": fields["code"],
            "venture": fields.get("venture", "blt"),
            "name": fields.get("name", fields["code"]),
            "stage": fields.get("stage", "build"),
            "stage_since": fields.get("stage_since", date(2026, 9, 13)),
            "stage_confidence": fields.get("stage_confidence", "inferred"),
            "owner": fields.get("owner"),
            "done_means": fields.get("done_means"),
            "aliases": list(fields.get("aliases", [])),
            "active": fields.get("active", True),
        }
        self.projects[row["code"]] = row
        return row

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = tuple(params) if params else ()

        if s.startswith("SELECT * FROM projects WHERE code = %s"):
            (code,) = params
            row = self.projects.get(code)
            return _Result([dict(row)] if row else [])

        if s.startswith("SELECT code FROM projects WHERE code = %s"):
            (code,) = params
            row = self.projects.get(code)
            return _Result([{"code": row["code"]}] if row else [])

        if "lower(name) = lower(%s)" in s:
            (text,) = params
            for row in self.projects.values():
                if row["name"].lower() == text.lower():
                    return _Result([{"code": row["code"]}])
            return _Result([])

        if "= ANY(aliases)" in s:
            (text,) = params
            for row in self.projects.values():
                if text in row["aliases"]:
                    return _Result([{"code": row["code"]}])
            return _Result([])

        if s.startswith("SELECT * FROM projects") and "stage_since <=" in s:
            stages, cutoff = params
            rows = [
                dict(r) for r in self.projects.values()
                if r["active"] and r["stage"] in stages and r["stage_since"] <= cutoff
            ]
            rows.sort(key=lambda r: (r["stage_since"], r["code"]))
            return _Result(rows)

        if s.startswith("SELECT * FROM projects"):
            rows = list(self.projects.values())
            remaining = list(params)
            if "venture = %s" in s:
                venture = remaining.pop(0)
                rows = [r for r in rows if r["venture"] == venture]
            if "active = %s" in s:
                active = remaining.pop(0)
                rows = [r for r in rows if r["active"] == active]
            rows = sorted(rows, key=lambda r: (r["venture"], r["code"]))
            return _Result([dict(r) for r in rows])

        if s.startswith("SELECT project, COUNT(*)"):
            (since,) = params
            counts: dict[str, int] = {}
            for sig in self.signals:
                if sig.get("project") is not None and sig["occurred_at"] >= since:
                    counts[sig["project"]] = counts.get(sig["project"], 0) + 1
            rows = [{"project": k, "n": v} for k, v in counts.items()]
            rows.sort(key=lambda r: (-r["n"], r["project"]))
            return _Result(rows)

        if s.startswith("UPDATE projects SET"):
            set_clause, _, _where = s.partition(" WHERE ")
            cols = re.findall(r"(\w+) = %s", set_clause)
            code = params[-1]
            values = params[:-1]
            row = self.projects[code]
            for col, val in zip(cols, values):
                row[col] = val
            if "stage_since = CURRENT_DATE" in set_clause:
                row["stage_since"] = date.today()
            return _Result([])

        if s.startswith("INSERT INTO project_stage_history"):
            self.history.append(params)
            return _Result([])

        if s.startswith("INSERT INTO projects"):
            code, venture, name, stage, stage_since, owner, aliases = params
            if code in self.projects:
                return _Result([])  # ON CONFLICT (code) DO NOTHING
            self.projects[code] = {
                "code": code, "venture": venture, "name": name, "stage": stage,
                "stage_since": stage_since, "stage_confidence": "inferred",
                "owner": owner, "done_means": None, "aliases": list(aliases),
                "active": True,
            }
            return _Result([{"code": code}])

        raise AssertionError(f"unexpected SQL: {sql[:80]!r}")


# --- resolve() --------------------------------------------------------------


def test_resolve_by_exact_code():
    conn = FakeConn()
    conn.add_project(code="iblu", name="Iblu", aliases=["iblu"])
    assert P.resolve(conn, "iblu") == "iblu"


def test_resolve_by_case_insensitive_name():
    conn = FakeConn()
    conn.add_project(code="email-writer", name="Email Writer", aliases=["mixmax"])
    assert P.resolve(conn, "EMAIL WRITER") == "email-writer"


def test_resolve_by_alias():
    conn = FakeConn()
    conn.add_project(code="email-writer", name="Email Writer", aliases=["mixmax"])
    assert P.resolve(conn, "mixmax") == "email-writer"
    assert P.resolve(conn, "MixMax") == "email-writer"  # input is lowered before the alias check


def test_resolve_returns_none_when_unregistered():
    conn = FakeConn()
    conn.add_project(code="iblu", name="Iblu", aliases=["iblu"])
    assert P.resolve(conn, "some brand new client thing") is None


def test_resolve_of_empty_or_none_is_none():
    conn = FakeConn()
    assert P.resolve(conn, None) is None
    assert P.resolve(conn, "   ") is None


# --- set_stage ----------------------------------------------------------


def test_set_stage_rejects_autonomous_without_done_means():
    conn = FakeConn()
    conn.add_project(code="scout", stage="maintain", done_means=None)
    with pytest.raises(P.ProjectError, match="scout"):
        P.set_stage(conn, "scout", "autonomous", changed_by="ignas")
    # Explicitly names the missing precondition, not just the project.
    with pytest.raises(P.ProjectError, match="done_means"):
        P.set_stage(conn, "scout", "autonomous", changed_by="ignas")


def test_set_stage_allows_autonomous_once_done_means_is_set():
    conn = FakeConn()
    conn.add_project(code="scout", stage="maintain", done_means="runs unattended for a month")
    updated = P.set_stage(conn, "scout", "autonomous", changed_by="ignas")
    assert updated["stage"] == "autonomous"
    assert conn.history[-1][:3] == ("scout", "maintain", "autonomous")


def test_set_stage_rejects_an_unknown_stage():
    conn = FakeConn()
    conn.add_project(code="scout", stage="maintain")
    with pytest.raises(P.ProjectError) as exc:
        P.set_stage(conn, "scout", "orbiting", changed_by="ignas")
    assert "orbiting" in str(exc.value)
    for stage in P.STAGE_ORDER:
        assert stage in str(exc.value)


def test_set_stage_rejects_an_unknown_project():
    conn = FakeConn()
    with pytest.raises(P.ProjectError, match="nope"):
        P.set_stage(conn, "nope", "build", changed_by="ignas")


def test_set_stage_writes_history_and_moves_stage_since():
    conn = FakeConn()
    conn.add_project(code="machina", stage="build", stage_since=date(2026, 1, 1))
    updated = P.set_stage(conn, "machina", "trial", changed_by="ignas", note="tapped")
    assert updated["stage"] == "trial"
    assert updated["stage_since"] == date.today()
    project, from_stage, to_stage, changed_by, note = conn.history[-1]
    assert (project, from_stage, to_stage, changed_by, note) == (
        "machina", "build", "trial", "ignas", "tapped",
    )


def test_set_stage_can_also_promote_confidence_to_fact():
    conn = FakeConn()
    conn.add_project(code="machina", stage="build", stage_confidence="inferred")
    updated = P.set_stage(conn, "machina", "trial", changed_by="ignas", confidence="fact")
    assert updated["stage_confidence"] == "fact"


# --- set_owner / set_done_means -----------------------------------------


def test_set_owner_and_set_done_means():
    conn = FakeConn()
    conn.add_project(code="scout")
    P.set_owner(conn, "scout", "Edo")
    P.set_done_means(conn, "scout", "runs unattended for a month")
    row = P.get_project(conn, "scout")
    assert row["owner"] == "Edo"
    assert row["done_means"] == "runs unattended for a month"


def test_set_owner_of_an_unknown_project_raises():
    conn = FakeConn()
    with pytest.raises(P.ProjectError, match="nope"):
        P.set_owner(conn, "nope", "Edo")


# --- stuck() --------------------------------------------------------------


def test_stuck_boundary_is_exactly_30_days():
    conn = FakeConn()
    today = date(2026, 10, 13)
    conn.add_project(code="exactly-30", stage="implement", stage_since=today - timedelta(days=30))
    conn.add_project(code="only-29", stage="implement", stage_since=today - timedelta(days=29))

    codes = {r["code"] for r in P.stuck(conn, today=today)}
    assert "exactly-30" in codes
    assert "only-29" not in codes


def test_stuck_ignores_inactive_and_out_of_scope_stages():
    conn = FakeConn()
    today = date(2026, 10, 13)
    old = today - timedelta(days=90)
    conn.add_project(code="archived", stage="implement", stage_since=old, active=False)
    conn.add_project(code="still-trialing", stage="trial", stage_since=old)
    conn.add_project(code="genuinely-stuck", stage="maintain", stage_since=old)

    codes = {r["code"] for r in P.stuck(conn, today=today)}
    assert codes == {"genuinely-stuck"}


# --- unregistered() -------------------------------------------------------


def test_unregistered_counts_names_resolve_cannot_map():
    conn = FakeConn()
    conn.add_project(code="machina", name="Machina", aliases=["machina"])
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    conn.signals = [
        {"project": "machina", "occurred_at": datetime(2026, 9, 10, tzinfo=timezone.utc)},
        {"project": "brand-new-thing", "occurred_at": datetime(2026, 9, 11, tzinfo=timezone.utc)},
        {"project": "brand-new-thing", "occurred_at": datetime(2026, 9, 12, tzinfo=timezone.utc)},
        {"project": None, "occurred_at": datetime(2026, 9, 12, tzinfo=timezone.utc)},
        {"project": "too-old", "occurred_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
    ]
    assert P.unregistered(conn, since) == [("brand-new-thing", 2)]


# --- seed() -----------------------------------------------------------


def test_seed_inserts_every_candidate_and_is_idempotent():
    conn = FakeConn()
    first = P.seed(conn)
    assert len(first["inserted"]) == len(P.SEED_PROJECTS)
    assert first["skipped"] == 0

    second = P.seed(conn)
    assert second["inserted"] == []
    assert second["skipped"] == len(P.SEED_PROJECTS)

    # Spot-check one row landed with the seed defaults from plan §1.5.
    row = P.get_project(conn, "email-writer")
    assert row["venture"] == "blt"
    assert row["stage"] == "deliver"
    assert row["stage_confidence"] == "inferred"
    assert row["stage_since"] == P.SEED_DATE
    assert row["owner"] == "Edo"
    assert "mixmax" in row["aliases"]


def test_seed_dry_run_touches_nothing():
    conn = FakeConn()
    report = P.seed(conn, dry=True)
    assert report["dry"] is True
    assert conn.projects == {}
    assert len(report["would_insert"]) == len(P.SEED_PROJECTS)


def test_every_seed_project_has_a_stage_in_the_ladder():
    for p in P.SEED_PROJECTS:
        assert p["stage"] in P.STAGE_ORDER, p["code"]


def test_seed_aliases_are_all_lowercase():
    for p in P.SEED_PROJECTS:
        for alias in p["aliases"]:
            assert alias == alias.lower(), (p["code"], alias)


# --- venture_hints integration --------------------------------------------


def test_infer_resolves_a_known_project_alias_via_the_seed_registry():
    """`radovi` was already a venture keyword (-> jakusi); it is now also a
    seeded project alias. The new project resolution must not change the
    venture that was already being inferred."""
    venture, project = venture_hints.infer(
        "ignas@blanklabel.team", subject="Radovi worker app update"
    )
    assert venture == "jakusi"  # unchanged from before this change
    assert project == "radovi"  # new: resolved via PROJECT_KEYWORDS


def test_infer_project_keywords_never_override_the_hand_curated_table():
    """`machina` is in both the original KEYWORD_PROJECTS and the seed
    registry; the hand-curated table must still be the one that answers."""
    _, project = venture_hints.infer(
        "ignas@blanklabel.team", subject="Machina nightly report"
    )
    assert project == "machina"


def test_infer_still_returns_none_project_when_nothing_matches():
    venture, project = venture_hints.infer(
        "ignas@blanklabel.team", subject="dentist appointment"
    )
    assert project is None
