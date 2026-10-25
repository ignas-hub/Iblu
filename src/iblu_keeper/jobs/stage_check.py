"""The Friday question that produces the Gain.

    python -m iblu_keeper.jobs.stage_check [--date YYYY-MM-DD] [--dry]

The mission says what a Gain is: "Progress is measured backward against the
baseline... an initiative reaching 'autonomous' is the largest gain." So the
unit of progress is a RUNG — a project moving up `stages` toward autonomous,
measured from the 2026-09-13 baselines.

Six weeks after those baselines, `project_stage_history` had zero rows. Every
project sat on the rung it was seeded with. The weekly review could therefore
only ever list tasks, and he said so: "random tasks closed... they do not show
how I'm getting closer to my goals". Nothing recorded a rung climbed because
nothing asked.

This asks. Each Friday it takes the projects that had evidence that week,
proposes the rung the evidence suggests, and sends one card: one line per
project, three taps — moved up, stayed, or autonomous. A tap writes
`project_stage_history`, moves `projects.stage`, and marks the stage a `fact`
he confirmed rather than a guess. The next review opens with it as a Gain.

Two rules, both from watching the day card go unanswered ten times:

  * **The question must already carry the value.** The card does not ask
    "what stage is Email Writer?" — it says "Email Writer: deliver since 13 Sep,
    106 signals this week, looks like → maintain". Reading the card IS seeing
    the week's progress; the tap only makes it count.
  * **Scripts propose, he decides, and the proposal is never written.** A
    suggested rung lives only in the card snapshot. Only his tap touches
    `projects` or `project_stage_history`, and the row says `changed_by =
    'ignas'`. An inferred stage got 28 projects onto the ladder; a confirmed
    one is the only kind that counts as a Gain.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .. import db
from ..config import settings
from ..store import observations as obs

logger = logging.getLogger("iblu_keeper.jobs.stage_check")

# How much a project must have happened to be worth a tap. Below this the
# honest proposal is "nothing to say", and a card full of projects with two
# signals each is a card he stops answering.
MIN_EVIDENCE = 8

# How many projects one card asks about. Four taps is an exchange; eight is a
# form.
MAX_PROPOSALS = 4

TOP_STAGE = "autonomous"
LETTERS = "ABCDEFGH"

# Tap keys: the letter plus one of these. Short because they ride in a signed
# token in a URL on a phone.
CHOICE_UP, CHOICE_SAME, CHOICE_TOP = "u", "s", "t"

TIMEOUT = 20


def short_name(name: str | None, code: str = "") -> str:
    """The project as a phrase, not a paragraph.

    `projects.name` carries the seeded description — "Property upkeep, smart
    home, access, irrigation, landscaping" — and six of those joined by commas
    is a line nobody can read. The first clause is the name; the rest was
    always a note.
    """
    text = (name or code or "").strip()
    text = re.split(r",| \(| — | - ", text, maxsplit=1)[0].strip()
    return text or code


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


# ---------------------------------------------------------------------------
# proposing
# ---------------------------------------------------------------------------


def _ladder(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT code, label, ordinal FROM stages ORDER BY ordinal"
    ).fetchall()]


def _next_stage(ladder: list[dict], current: str) -> dict | None:
    """The rung above `current`, or None at the top."""
    codes = [s["code"] for s in ladder]
    if current not in codes:
        return None
    i = codes.index(current)
    return ladder[i + 1] if i + 1 < len(ladder) else None


def propose(conn, since: datetime, until: datetime) -> list[dict]:
    """Projects with evidence this window, each with the rung it suggests.

    Evidence is counted from `signals.project`, which is set by the collectors'
    venture/project inference — so a project nobody tags never appears here,
    and a week in which 60% of signals carry no project (true of 19–25 Oct)
    under-asks. That is a limit of the tagging, not of this question, and the
    card says how much evidence it is standing on.
    """
    ladder = _ladder(conn)
    by_ordinal = {s["ordinal"]: s for s in ladder}
    rows = conn.execute(
        """
        SELECT p.code, p.name, p.venture, p.stage, p.stage_since, s.ordinal,
               count(sg.id) AS signals,
               count(DISTINCT sg.source) AS sources,
               array_remove(array_agg(DISTINCT left(sg.subject, 70)), NULL) AS subjects
          FROM projects p
          JOIN stages s ON s.code = p.stage
          JOIN signals sg ON sg.project = p.code
         WHERE p.active
           AND sg.actor = 'me' AND sg.excluded_reason IS NULL
           AND sg.occurred_at >= %s AND sg.occurred_at < %s
         GROUP BY p.code, p.name, p.venture, p.stage, p.stage_since, s.ordinal
        HAVING count(sg.id) >= %s
         ORDER BY count(sg.id) DESC
         LIMIT %s
        """,
        (since, until, MIN_EVIDENCE, MAX_PROPOSALS),
    ).fetchall()

    out = []
    for letter, row in zip(LETTERS, rows):
        nxt = _next_stage(ladder, row["stage"])
        if nxt is None:
            # Already autonomous. There is no rung above, and asking "did it
            # stay autonomous?" every Friday is noise.
            continue
        out.append({
            "letter": letter,
            "project": row["code"],
            "name": short_name(row["name"], row["code"]),
            "venture": row["venture"],
            "from_stage": row["stage"],
            "from_ordinal": int(row["ordinal"]),
            "from_since": row["stage_since"].isoformat() if row["stage_since"] else None,
            "next_stage": nxt["code"],
            "next_label": nxt["label"],
            "top_stage": TOP_STAGE,
            "evidence": {
                "signals": int(row["signals"]),
                "sources": int(row["sources"]),
                "subjects": [s for s in (row["subjects"] or []) if s][:3],
            },
        })
    # Re-letter after the skip so letters stay contiguous on the card.
    for letter, item in zip(LETTERS, out):
        item["letter"] = letter
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _since_label(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return f" since {datetime.fromisoformat(iso).astimezone(_tz()):%-d %b}"
    except Exception:  # noqa: BLE001
        return ""


def render_text(proposals: list[dict]) -> str:
    """The plain-text body — what `--dry` prints and what Chat shows as the
    fallback for a client that cannot render cards."""
    lines = ["Did any of these climb a rung this week?", ""]
    for p in proposals:
        ev = p["evidence"]
        lines.append(
            f"{p['letter']}  {p['name']} — {p['from_stage']}{_since_label(p['from_since'])}"
        )
        lines.append(
            f"     {ev['signals']} signals this week"
            + (f" · {ev['sources']} sources" if ev["sources"] > 1 else "")
            + f" · looks like → {p['next_stage']}"
        )
    lines.append("")
    lines.append("Tap one per line. A rung you confirm is next Friday's first line.")
    return "\n".join(lines)


def build_card(check_id: int, proposals: list[dict], self_id: str,
               base_url: str, secret: str) -> dict:
    from ..pings.tokens import make_token, tap_url

    def button(text: str, letter: str, choice: str) -> dict:
        token = make_token(check_id, "stage", f"{letter}{choice}", secret)
        return {"text": text, "onClick": {"openLink": {"url": tap_url(base_url, token)}}}

    sections = []
    for p in proposals:
        ev = p["evidence"]
        header = f"<b>{p['letter']}  {p['name']}</b> — {p['from_stage']}{_since_label(p['from_since'])}"
        evidence = (
            f"{ev['signals']} signals this week"
            + (f" · {ev['sources']} sources" if ev["sources"] > 1 else "")
        )
        if ev["subjects"]:
            evidence += "<br>" + " · ".join(ev["subjects"][:2])
        buttons = [button(f"→ {p['next_stage']}", p["letter"], CHOICE_UP)]
        if p["next_stage"] != TOP_STAGE:
            buttons.append(button(f"→ {TOP_STAGE}", p["letter"], CHOICE_TOP))
        buttons.append(button(f"stayed {p['from_stage']}", p["letter"], CHOICE_SAME))
        sections.append({"widgets": [
            {"textParagraph": {"text": header}},
            {"textParagraph": {"text": evidence}},
            {"buttonList": {"buttons": buttons}},
        ]})

    mention = f"<users/{self_id}> " if self_id else ""
    return {
        "text": f"{mention}Did any of these climb a rung this week?",
        "cardsV2": [{
            "cardId": f"stage-{check_id}",
            "card": {"sections": sections + [{"widgets": [{"textParagraph": {
                "text": "One tap per line. A rung you confirm is next Friday's first line.",
            }}]}]},
        }],
    }


# ---------------------------------------------------------------------------
# storage and delivery
# ---------------------------------------------------------------------------


def _upsert(conn, on: date, proposals: list[dict]) -> int:
    """One row per day. The snapshot is replaced only while nothing has
    reached Chat — the same discipline as `day_cards`, for the same reason: a
    tap on a card already on his phone must mean what he saw."""
    row = conn.execute(
        """
        INSERT INTO stage_checks (local_date, proposals)
        VALUES (%s, %s)
        ON CONFLICT (local_date) DO UPDATE
           SET proposals = CASE WHEN stage_checks.chat_message_ref IS NULL
                                THEN EXCLUDED.proposals ELSE stage_checks.proposals END
        RETURNING id
        """,
        (on, Jsonb(proposals)),
    ).fetchone()
    return int(row["id"])


def send(check_id: int, proposals: list[dict], self_id: str) -> dict:
    import requests

    from ..pings.deliver import DeliveryError, _scrub, preflight_tap_route

    if not settings.secretary_webhook_url:
        raise DeliveryError("SECRETARY_WEBHOOK_URL is not set")
    if not settings.ping_signing_secret:
        raise DeliveryError("PING_SIGNING_SECRET is not set — tap links would be unsignable")
    if not settings.mcp_public_base_url:
        raise DeliveryError("MCP_PUBLIC_BASE_URL is not set — tap links would be relative")
    preflight_tap_route()

    body = build_card(check_id, proposals, self_id,
                      settings.mcp_public_base_url, settings.ping_signing_secret)
    url = settings.secretary_webhook_url
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}threadKey=stage-{check_id}&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    try:
        response = requests.post(url, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DeliveryError(f"webhook unreachable: {_scrub(exc)}") from exc
    if response.status_code >= 400:
        raise DeliveryError(f"webhook returned {response.status_code}: {response.text[:300]}")
    data = response.json()
    return {
        "message_ref": data.get("name"),
        "thread_ref": (data.get("thread") or {}).get("name"),
    }


# ---------------------------------------------------------------------------
# the tap
# ---------------------------------------------------------------------------


def record_stage_tap(conn, check_id: int, key: str) -> dict:
    """One tap: confirm a rung. The only code path that moves `projects.stage`.

    Called from `pings.answers.record_tap` when the token's qid is `stage`.
    The letter resolves against the card's own snapshot, never the live table,
    so a tap days later still names the project he saw.
    """
    from ..pings.answers import UnknownPing

    row = conn.execute(
        "SELECT id, local_date, proposals, answers FROM stage_checks WHERE id = %s",
        (check_id,),
    ).fetchone()
    if row is None:
        raise UnknownPing(f"stage check {check_id} no longer exists")

    letter, choice = key[:1], key[1:]
    proposal = next((p for p in row["proposals"] if p["letter"] == letter), None)
    if proposal is None or choice not in (CHOICE_UP, CHOICE_SAME, CHOICE_TOP):
        raise UnknownPing(f"stage check {check_id} has no option {key!r}")

    target = {
        CHOICE_UP: proposal["next_stage"],
        CHOICE_TOP: TOP_STAGE,
        CHOICE_SAME: proposal["from_stage"],
    }[choice]
    moved = target != proposal["from_stage"]
    project = proposal["project"]

    # One writer per project at a time: a double-tap must not produce two
    # history rows for one climb.
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (8172, project))

    current = conn.execute(
        "SELECT stage FROM projects WHERE code = %s", (project,)
    ).fetchone()
    already = current is not None and current["stage"] == target

    if moved and not already:
        conn.execute(
            """
            INSERT INTO project_stage_history
                (project, from_stage, to_stage, changed_at, changed_by, note)
            VALUES (%s, %s, %s, now(), 'ignas', %s)
            """,
            (project, current["stage"] if current else proposal["from_stage"], target,
             f"confirmed on the Friday stage card ({row['local_date']}), "
             f"{proposal['evidence']['signals']} signals that week"),
        )
    # Whether it moved or stayed, he has now looked at it: the stage is a fact.
    conn.execute(
        """
        UPDATE projects
           SET stage = %s,
               stage_since = CASE WHEN stage <> %s THEN now() ELSE stage_since END,
               stage_confidence = 'fact'
         WHERE code = %s
        """,
        (target, target, project),
    )

    answers = dict(row["answers"] or {})
    answers[letter] = {
        "stage": target, "moved": moved,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        "UPDATE stage_checks SET answers = %s, status = 'answered' WHERE id = %s",
        (Jsonb(answers), check_id),
    )

    if moved:
        label = f"{proposal['name']}: {proposal['from_stage']} → {target}"
    else:
        label = f"{proposal['name']} stays at {target}"
    return {
        "qid": "stage", "key": key, "label": label, "moved": moved,
        "superseded": already, "project": project, "stage": target,
    }


# ---------------------------------------------------------------------------
# the ladder, for the weekly
# ---------------------------------------------------------------------------


def ladder_gains(conn, since: datetime, until: datetime) -> dict:
    """Rungs climbed — this window and since the baseline — plus what is one
    rung from the top. Pure counts from `project_stage_history`; the weekly
    renders them and must not paraphrase them."""
    baseline = date(2026, 9, 13)
    this_week = [dict(r) for r in conn.execute(
        """
        SELECT h.project, p.name, p.venture, h.from_stage, h.to_stage, h.changed_at,
               sf.ordinal AS from_ord, st.ordinal AS to_ord
          FROM project_stage_history h
          JOIN projects p ON p.code = h.project
          JOIN stages sf ON sf.code = h.from_stage
          JOIN stages st ON st.code = h.to_stage
         WHERE h.changed_at >= %s AND h.changed_at < %s
         ORDER BY h.changed_at
        """,
        (since, until),
    ).fetchall()]
    all_time = conn.execute(
        """
        SELECT count(*) AS moves,
               coalesce(sum(st.ordinal - sf.ordinal), 0) AS rungs,
               count(DISTINCT h.project) AS projects
          FROM project_stage_history h
          JOIN stages sf ON sf.code = h.from_stage
          JOIN stages st ON st.code = h.to_stage
         WHERE h.changed_at >= %s AND h.changed_by = 'ignas'
        """,
        (datetime.combine(baseline, datetime.min.time(), timezone.utc),),
    ).fetchone()
    near_top = [dict(r) for r in conn.execute(
        """
        SELECT p.code, p.name, p.venture, p.stage_confidence
          FROM projects p JOIN stages s ON s.code = p.stage
         WHERE p.active AND s.ordinal = (SELECT max(ordinal) FROM stages) - 1
         ORDER BY p.venture, p.name
        """
    ).fetchall()]
    at_top = conn.execute(
        "SELECT count(*) AS n FROM projects p JOIN stages s ON s.code = p.stage "
        " WHERE p.active AND s.code = %s", (TOP_STAGE,)
    ).fetchone()["n"]
    total = conn.execute("SELECT count(*) AS n FROM projects WHERE active").fetchone()["n"]
    return {
        "baseline": baseline.isoformat(),
        "this_week": this_week,
        "since_baseline": {
            "moves": int(all_time["moves"] or 0),
            "rungs": int(all_time["rungs"] or 0),
            "projects": int(all_time["projects"] or 0),
        },
        "one_from_top": near_top,
        "at_top": int(at_top or 0),
        "total_active": int(total or 0),
    }


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------


def _window(on: date) -> tuple[datetime, datetime]:
    """Monday 00:00 → the moment of sending, local."""
    tz = _tz()
    end = datetime.combine(on, datetime.min.time(), tz) + timedelta(days=1)
    start = end - timedelta(days=7)
    return start.astimezone(timezone.utc), min(end, datetime.now(tz)).astimezone(timezone.utc)


def run(on: date | None = None, dry: bool = False) -> int:
    if settings.use_mock:
        logger.error("stage_check: refusing to run in mock mode (DRY_RUN=true)")
        return 1
    if not db.is_configured():
        logger.error("stage_check: no DATABASE_URL")
        return 1

    day = on or datetime.now(_tz()).date()
    since, until = _window(day)

    with db.get_conn() as conn:
        proposals = propose(conn, since, until)
        if not proposals:
            logger.info("stage_check %s: no project with %d+ signals — sending nothing",
                        day, MIN_EVIDENCE)
            return 0
        if dry:
            print(render_text(proposals))
            return 0

        check_id = _upsert(conn, day, proposals)
        from ..tools.chat import get_backend

        try:
            self_id = get_backend()._ensure_self_id()
        except Exception:  # noqa: BLE001 - the mention is nice-to-have
            self_id = ""
        try:
            refs = send(check_id, proposals, self_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("stage_check: delivery failed: %s", exc)
            obs.record_safe(
                source="weekly", kind="stage_check_undelivered", severity="error",
                summary="the Friday stage card could not be sent",
                detail=str(exc)[:500], evidence={"date": str(day)},
                fp=obs.fingerprint("stage_check", "undelivered"),
            )
            return 1
        conn.execute(
            "UPDATE stage_checks SET chat_message_ref = %s, chat_thread_ref = %s WHERE id = %s",
            (refs["message_ref"], refs["thread_ref"], check_id),
        )
        logger.info("stage_check %s: sent %d proposal(s) as %s",
                    day, len(proposals), refs["message_ref"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.stage_check",
        description="Ask which projects climbed a rung this week.",
    )
    parser.add_argument("--date", help="local date (default: today)")
    parser.add_argument("--dry", action="store_true", help="print the card, write and send nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    on = date.fromisoformat(args.date) if args.date else None
    try:
        return run(on=on, dry=args.dry)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
