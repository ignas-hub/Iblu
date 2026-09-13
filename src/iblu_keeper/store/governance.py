"""Governance reads over `context_entries` — priorities, baselines, gain rules.

Plan §1.4: these three are "visible everywhere", meaning every consumer that
judges or measures anything (get_context, the ping composer, the weekly
review, reconstruct_day's system prompt) reads through these same three
functions rather than each writing its own query. One definition of "current"
means a consumer can never disagree with another about which priority is live.

All three are plain reads: no write path lives here, and none is needed —
priorities and baselines are written through `context_log`'s normal
supersede chain (plan D9), the same as any other decision or fact.

Rows come back as plain dicts with JSON-safe values (`id` as `str`,
timestamps as ISO-8601 strings) because these cross the MCP tool boundary via
`get_context` — the same normalisation `tools/context.py` does for search
results, done here instead of pushed onto every caller.
"""

from __future__ import annotations

# Long enough to carry the point of a priority or baseline, short enough that
# six ventures' worth still fits a system prompt without crowding out the
# actual conversation.
_EXCERPT_LEN = 200


def _excerpt(content: str) -> str:
    text = (content or "").strip()
    if len(text) <= _EXCERPT_LEN:
        return text
    return text[:_EXCERPT_LEN].rstrip() + "…"


def _out(row: dict) -> dict:
    """Normalise one context_entries row for the tool-response boundary."""
    out = dict(row)
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    if out.get("created_at") is not None:
        out["created_at"] = out["created_at"].isoformat()
    return out


def current_priorities(conn) -> list[dict]:
    """The one live yearly/top priority per venture.

    `DISTINCT ON (venture)` picks the newest non-superseded 'priority'
    decision for each venture — superseded ones are already excluded by the
    WHERE clause, so "newest" and "current" agree by construction.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (venture) id, venture, content, created_at
        FROM context_entries
        WHERE type = 'decision'
          AND 'priority' = ANY(tags)
          AND superseded_by IS NULL
        ORDER BY venture, created_at DESC
        """
    ).fetchall()
    return [_out(r) for r in rows]


def current_baselines(conn) -> list[dict]:
    """The live baseline fact(s) per venture — measured backward from these.

    Keyed by `(venture, source_ref)`, not just `venture`: personal alone has
    two independent baselines (hated tasks A-H, and health + gain) and both
    must survive, and a venture-less baseline (GoStellar's, written before
    the `gostellar` venture existed) must not collide with anything else.
    `source_ref` is returned alongside the usual fields so a caller can tell
    two same-venture baselines apart.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (venture, source_ref)
               id, venture, content, created_at, source_ref
        FROM context_entries
        WHERE type = 'fact'
          AND 'baseline' = ANY(tags)
          AND superseded_by IS NULL
        ORDER BY venture, source_ref, created_at DESC
        """
    ).fetchall()
    return [_out(r) for r in rows]


def gain_rules(conn) -> dict | None:
    """The current gain-practice rules, or None if they were never set.

    `source_ref='gain:practice-rules'` is a fixed key rather than a tag
    filter — there is exactly one of these, so ORDER BY + LIMIT 1 is a
    defensive tie-breaker, not a real ranking.
    """
    row = conn.execute(
        """
        SELECT id, content, created_at, source_ref
        FROM context_entries
        WHERE source_ref = 'gain:practice-rules'
          AND superseded_by IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return _out(row) if row else None


def as_prompt_block(
    priorities: list[dict], baselines: list[dict], rules: dict | None
) -> str:
    """Compact plain-text rendering for embedding in an LLM system prompt.

    Plan §4.6 / §1.4: ping composer, weekly review and reconstruct_day all
    need "mission + priorities + gain rules" in their system prompts without
    each re-deriving its own summary of the same rows. One line per priority
    and per baseline keeps six ventures' worth of both well under the size
    that would start crowding out the rest of the prompt; the gain rules are
    reproduced verbatim because a paraphrase of a practice rule is exactly
    the kind of drift this function exists to prevent.
    """
    lines = ["PRIORITIES:"]
    if priorities:
        for p in priorities:
            lines.append(f"- {p['venture']}: {_excerpt(p['content'])}")
    else:
        lines.append("- (none set)")

    lines.append("")
    lines.append("BASELINES:")
    if baselines:
        for b in baselines:
            venture = b.get("venture") or "(no venture)"
            lines.append(f"- {venture}: {_excerpt(b['content'])}")
    else:
        lines.append("- (none set)")

    lines.append("")
    lines.append("GAIN RULES:")
    lines.append(rules["content"].strip() if rules else "(none set)")

    return "\n".join(lines)
