"""The project registry — plan §1.5 / §1.6, migration 007.

`stages` (seeded by the migration) is the fixed ladder every initiative
climbs. `projects` is the registry of named initiatives; `resolve()` is the
single place free text (`signals.project`, `context_entries.project`,
`blocks.project`) gets mapped onto a `projects.code` — a name it cannot map
is kept as-is and is the caller's problem, not an error.

Two rules live here rather than in SQL, because a CHECK constraint can't
phrase a friendly cross-column error:

  * a project cannot move to `'autonomous'` while `done_means` is NULL —
    reaching the top of the ladder only means something if "done" was
    written down before it happened;
  * every stage change is also a `project_stage_history` row, written in the
    same transaction as the move, so `stuck()` and the weekly review can
    trust `stage_since` instead of re-deriving it from `signals`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

# The ladder, in order. Mirrors the `stages` table seeded by migration 007;
# kept here too so pure-logic validation (does this stage code even exist?)
# doesn't need a database round trip.
STAGE_ORDER: tuple[str, ...] = (
    "research", "initiate", "build", "trial", "implement", "deliver",
    "maintain", "autonomous",
)

# Stages `set_stage`/the WIP guard treat as "in flight, not yet finished".
STUCK_STAGES: tuple[str, ...] = ("implement", "deliver", "maintain")
STUCK_DAYS = 30

# The day the registry was seeded (plan §1.5). Every seed row starts here,
# `stage_confidence='inferred'`, until Ignas confirms via the evening `stage`
# question (§1.5) or a tap moves it.
SEED_DATE = date(2026, 9, 13)


class ProjectError(ValueError):
    """A bad registry operation. The message always names the project (or
    value) at fault — this is surfaced straight to Ignas via pings/tools."""


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def list_projects(
    conn, venture: str | None = None, active: bool | None = True
) -> list[dict]:
    """Registered projects, optionally filtered by venture and/or `active`.

    `active=None` returns both live and archived projects.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if venture is not None:
        clauses.append("venture = %s")
        params.append(venture)
    if active is not None:
        clauses.append("active = %s")
        params.append(active)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM projects {where} ORDER BY venture, code", params
    ).fetchall()
    return list(rows)


def get_project(conn, code: str) -> dict | None:
    """A single project row, or None if `code` is not registered."""
    row = conn.execute(
        "SELECT * FROM projects WHERE code = %s", (code,)
    ).fetchone()
    return dict(row) if row else None


def resolve(conn, name: str | None) -> str | None:
    """Map free text to a `projects.code`, or None if nothing matches.

    Tried in order: exact code match, case-insensitive name match, then
    membership in `aliases` (aliases are stored lowercase; the input is
    lowered to match). The caller keeps the original text when this returns
    None — an unregistered name is not an error.
    """
    if not name or not name.strip():
        return None
    text = name.strip()

    row = conn.execute(
        "SELECT code FROM projects WHERE code = %s", (text,)
    ).fetchone()
    if row:
        return row["code"]

    row = conn.execute(
        "SELECT code FROM projects WHERE lower(name) = lower(%s)", (text,)
    ).fetchone()
    if row:
        return row["code"]

    row = conn.execute(
        "SELECT code FROM projects WHERE %s = ANY(aliases)", (text.lower(),)
    ).fetchone()
    if row:
        return row["code"]

    return None


def unregistered(conn, since: datetime) -> list[tuple[str, int]]:
    """`(project_text, count)` for `signals.project` strings `resolve()`
    cannot map, since `since`. Powers the review's "unregistered" list and
    the WIP guard's "a new thing is starting" check (plan §1.6).
    """
    rows = conn.execute(
        """
        SELECT project, COUNT(*) AS n
          FROM signals
         WHERE project IS NOT NULL AND occurred_at >= %s
         GROUP BY project
         ORDER BY n DESC, project
        """,
        (since,),
    ).fetchall()
    return [(r["project"], r["n"]) for r in rows if resolve(conn, r["project"]) is None]


def stuck(
    conn,
    stages: Iterable[str] = STUCK_STAGES,
    days: int = STUCK_DAYS,
    *,
    today: date | None = None,
) -> list[dict]:
    """Active registered projects sitting in one of `stages` for >= `days`,
    measured from `stage_since`. Feeds the WIP guard and the weekly review's
    "stuck ≥30 days" line.

    `today` is only for tests — production always measures from the real
    date.
    """
    stages = tuple(stages)
    cutoff = (today or date.today()) - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT * FROM projects
         WHERE active AND stage = ANY(%s) AND stage_since <= %s
         ORDER BY stage_since, code
        """,
        (list(stages), cutoff),
    ).fetchall()
    return list(rows)


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def _require_project(conn, code: str) -> dict:
    project = get_project(conn, code)
    if project is None:
        raise ProjectError(f"unknown project {code!r}")
    return project


def set_stage(
    conn,
    code: str,
    to_stage: str,
    *,
    changed_by: str,
    note: str | None = None,
    confidence: str | None = None,
) -> dict:
    """Move `code` to `to_stage` and record the move, in one transaction.

    Raises `ProjectError` for an unknown project, an unknown stage, or a move
    into `'autonomous'` while `done_means` is not set — the one rule the
    schema itself cannot express.
    """
    if to_stage not in STAGE_ORDER:
        raise ProjectError(
            f"unknown stage {to_stage!r} — expected one of "
            f"{', '.join(STAGE_ORDER)}"
        )

    project = _require_project(conn, code)

    if to_stage == "autonomous" and not project.get("done_means"):
        raise ProjectError(
            f"{code} cannot enter 'autonomous' while done_means is not set"
        )

    from_stage = project["stage"]

    set_parts = ["stage = %s", "stage_since = CURRENT_DATE"]
    params: list[Any] = [to_stage]
    if confidence is not None:
        set_parts.append("stage_confidence = %s")
        params.append(confidence)
    params.append(code)
    conn.execute(
        f"UPDATE projects SET {', '.join(set_parts)} WHERE code = %s", params
    )

    conn.execute(
        """
        INSERT INTO project_stage_history
            (project, from_stage, to_stage, changed_by, note)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (code, from_stage, to_stage, changed_by, note),
    )

    return get_project(conn, code)  # type: ignore[return-value]


def set_owner(conn, code: str, owner: str | None) -> dict:
    """Set who runs the project once it's finished (`None` = Ignas)."""
    _require_project(conn, code)
    conn.execute("UPDATE projects SET owner = %s WHERE code = %s", (owner, code))
    return get_project(conn, code)  # type: ignore[return-value]


def set_done_means(conn, code: str, text: str) -> dict:
    """Set the internal finish line — required before `set_stage` will allow
    a move to `'autonomous'`."""
    _require_project(conn, code)
    conn.execute(
        "UPDATE projects SET done_means = %s WHERE code = %s", (text, code)
    )
    return get_project(conn, code)  # type: ignore[return-value]


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------

# Ignas's inferred portfolio as of 2026-09-13 (plan §1.5), cross-checked
# against signals, the local `iblu` and `automations` checkouts (via
# tools/repo.py's ROOTS) and Drive folder names visible to the collectors.
# All `stage_confidence='inferred'` and `stage_since=SEED_DATE` — nothing
# here is asked of Ignas; he confirms (or corrects) each one via the evening
# `stage` question as it appears in real signals (plan §1.5, last
# paragraph). `owner=None` means "Ignas himself" per the `projects.owner`
# column comment.
#
# Aliases are deliberately conservative (plan instruction: a wrong alias
# silently mis-attributes work, which is worse than an unregistered name).
# Three words were left OUT of every alias list because they are genuinely
# ambiguous between two seeded projects and a wrong guess would be worse
# than silence:
#   'opera'  — could mean blt's `opera-desktop` (the client account) or
#              choco's `choco-opera` (the operational side of the same
#              client); venture_hints already resolves the venture from
#              other signals, so the project is left for a tap to disambiguate.
#   'alexan' — could mean `alexan-deadlift` (Sparkleads lead gen using
#              Alexan, run for Deadlift) or `gostellar-alexan` (Greta
#              signing Alexan as a GoStellar client) — same person/company,
#              two different initiatives.
#   'emory'  — could mean `bd-global` (Emory's BLT hire) or
#              `gostellar-emory` (Greta working with Emory) — same person,
#              two different initiatives.
SEED_PROJECTS: list[dict[str, Any]] = [
    {"code": "email-writer", "venture": "blt", "name": "Email Writer",
     "stage": "deliver", "owner": "Edo",
     "aliases": {"email writer", "emailwriter", "mixmax"}},
    {"code": "recruiter", "venture": "blt", "name": "Recruiter",
     "stage": "deliver", "owner": None,
     "aliases": {"recruiter"}},
    {"code": "scout", "venture": "blt", "name": "Scout scripts (n8n server)",
     "stage": "maintain", "owner": None,
     "aliases": {"scout"}},
    {"code": "signals-outreach", "venture": "blt",
     "name": "Signals / AI outreach system", "stage": "trial", "owner": None,
     "aliases": {"signals outreach", "outreach system", "outreach-system", "ai outreach"}},
    {"code": "bt-quality-check", "venture": "blt",
     "name": "AI quality-check on BT (n8n webhooks)", "stage": "trial", "owner": None,
     "aliases": {"quality check", "quality-check", "bt quality check"}},
    {"code": "bt-assistant", "venture": "blt",
     "name": '"Assistant" app for specialists', "stage": "initiate", "owner": None,
     "aliases": {"bt assistant", "assistant app"}},
    {"code": "editorial-kb", "venture": "blt",
     "name": "Editorial authority campaign + client case-study KB",
     "stage": "implement", "owner": None,
     "aliases": {"editorial authority", "editorial kb", "case-study kb", "case study kb"}},
    {"code": "bd-global", "venture": "blt",
     "name": "Global BD hire (Emory; starts 2026-09-14)",
     "stage": "implement", "owner": "Emory",
     "aliases": {"global bd", "bd hire"}},
    {"code": "opera-desktop", "venture": "blt",
     "name": "Opera Desktop account, incl. internal approvals",
     "stage": "maintain", "owner": "Ignas + Ante",
     "aliases": {"opera desktop", "opera account"}},
    {"code": "womanizer", "venture": "blt",
     "name": "Womanizer/WeVibe gifting, incl. monthly reports",
     "stage": "maintain", "owner": "team",
     "aliases": {"womanizer", "wevibe", "we-vibe"}},
    {"code": "noshinku", "venture": "blt",
     "name": "Noshinku gifting (from Sept 2026)", "stage": "implement", "owner": "team",
     "aliases": {"noshinku"}},
    {"code": "choco-opera", "venture": "choco",
     "name": "Choco / Opera Desktop operations", "stage": "maintain", "owner": "4 FTE team",
     "aliases": {"choco opera", "opera operations"}},
    {"code": "machina", "venture": "deadlift",
     "name": "Machina (library + approvals live; loop)", "stage": "trial", "owner": None,
     "aliases": {"machina"}},
    {"code": "ad-inspector", "venture": "deadlift",
     "name": "Ad Inspector (auto-check ads, report to Greta)",
     "stage": "build", "owner": None,
     "aliases": {"ad inspector", "adinspector"}},
    {"code": "deadlift-legal", "venture": "deadlift",
     "name": "Legal docs for AI creative services", "stage": "implement", "owner": None,
     "aliases": {"deadlift legal"}},
    {"code": "deadlift-pitches", "venture": "deadlift",
     "name": "Big client pitches (>=200K/mo)", "stage": "research", "owner": None,
     "aliases": {"deadlift pitches"}},
    {"code": "alexan-deadlift", "venture": "deadlift",
     "name": "Sparkleads lead gen for Deadlift (setup with Srdjan)",
     "stage": "initiate", "owner": None,
     "aliases": {"sparkleads", "alexan deadlift"}},
    {"code": "radovi", "venture": "jakusi",
     "name": "Radovi — photo-verified worker task app", "stage": "build", "owner": None,
     "aliases": {"radovi"}},
    {"code": "jakusi-property", "venture": "jakusi",
     "name": "Property upkeep, smart home, access, irrigation, landscaping",
     "stage": "maintain", "owner": "workers",
     "aliases": {"jakusi property", "property upkeep"}},
    {"code": "iblu", "venture": "personal", "name": "Iblu",
     "stage": "trial", "owner": None,
     "aliases": {"iblu"}},
    {"code": "accounting-app", "venture": "personal",
     "name": "Accounting automator (invoice collection, reconciliation, accountants)",
     "stage": "deliver", "owner": None,
     "aliases": {"accounting bot", "accounting automator"}},
    {"code": "iblu-games", "venture": "personal", "name": "IBLU Games",
     "stage": "maintain", "owner": None,
     "aliases": {"iblu games"}},
    {"code": "leo-days", "venture": "family",
     "name": "Twice-monthly Leo days", "stage": "initiate", "owner": None,
     "aliases": {"leo days", "leo day"}},
    {"code": "brazil-trip", "venture": "family",
     "name": "Brazil Dec 27 - Jan 10, football practice for Leo",
     "stage": "initiate", "owner": None,
     "aliases": {"brazil trip"}},
    {"code": "leo-programming", "venture": "family",
     "name": "Leo building games by directing AI", "stage": "trial", "owner": "Ignas + Leo",
     "aliases": {"leo programming", "leo coding"}},
    {"code": "gostellar-alexan", "venture": "gostellar",
     "name": "Greta signing Alexan", "stage": "initiate", "owner": None,
     "aliases": {"gostellar alexan"}},
    {"code": "gostellar-emory", "venture": "gostellar",
     "name": "Greta working with Emory", "stage": "research", "owner": None,
     "aliases": {"gostellar emory"}},
    {"code": "gostellar-ai-tool", "venture": "gostellar",
     "name": "Competitor-customer AI tool (Andrei building)",
     "stage": "build", "owner": "Andrei",
     "aliases": {"gostellar ai tool", "competitor ai tool"}},
]


def seed(conn, *, dry: bool = False) -> dict:
    """Idempotently insert `SEED_PROJECTS`. Safe to re-run: `ON CONFLICT
    (code) DO NOTHING` means an already-registered (or since-edited) project
    is never overwritten.

    `dry=True` reports what *would* be inserted without touching the
    database — useful for re-running the cross-check as new aliases turn up.
    """
    if dry:
        existing = {p["code"] for p in list_projects(conn, active=None)}
        return {
            "would_insert": [p["code"] for p in SEED_PROJECTS if p["code"] not in existing],
            "already_registered": sorted(existing & {p["code"] for p in SEED_PROJECTS}),
            "dry": True,
        }

    inserted: list[str] = []
    for p in SEED_PROJECTS:
        row = conn.execute(
            """
            INSERT INTO projects
                (code, venture, name, stage, stage_since, stage_confidence,
                 owner, aliases)
            VALUES (%s, %s, %s, %s, %s, 'inferred', %s, %s)
            ON CONFLICT (code) DO NOTHING
            RETURNING code
            """,
            (
                p["code"], p["venture"], p["name"], p["stage"], SEED_DATE,
                p["owner"], sorted(p["aliases"]),
            ),
        ).fetchone()
        if row:
            inserted.append(row["code"])
    return {"inserted": inserted, "skipped": len(SEED_PROJECTS) - len(inserted), "dry": False}
