"""Language rules for the weekly review (plan §1.7, context in §7).

Ignas measures himself against ideals he has not reached (the Gap) and beats
himself up for the distance. IBLU's entire point is the opposite: evidence,
measured backward from a baseline (the Gain). A review that slips into Gap
phrasing — "only 40% of target", "still behind on X", "great job!" — is not a
harmless stylistic lapse here; it is the exact failure mode this system exists
to prevent.

`validate_language` is therefore a hard gate, not a linter: the caller (the
weekly job) never sends text this rejects. It falls back to the deterministic
template instead, which is built from fixed phrasings that are tested to pass.

Two things matter about *how* the checks are written, not just what they
check:

  * "behind" must not fire on "behind the scenes" — a real, common phrase that
    has nothing to do with grading.
  * a `%` describing a measured share of attention ("38% of the week was
    BLT") must pass. Only a `%` framed against a goal/target must fail. The
    review's own Truth section reports real percentages (ping answer rate,
    venture share) — the rule targets the *framing*, not the digit.
"""

from __future__ import annotations

import re

# --- forward-gap phrasing (measuring against an ideal instead of backward) --

# "still <...> away" — e.g. "still 3 clients away from the goal". Bounded
# window so it doesn't reach across unrelated sentences.
_STILL_AWAY = re.compile(r"\bstill\b[^.\n]{0,60}\baway\b", re.IGNORECASE)

# "only <...> of target/goal" — e.g. "only answered 4 of the target 5".
_ONLY_OF_TARGET = re.compile(
    r"\bonly\b[^.\n]{0,60}\bof (?:the |his |your )?(?:target|goal)\b", re.IGNORECASE
)

# "behind", but not "behind the scenes" (a real idiom, not a grading word).
_BEHIND = re.compile(r"\bbehind\b(?!\s+the\s+scenes)", re.IGNORECASE)

_SHOULD_HAVE = re.compile(r"\bshould(?:'ve| have)\b", re.IGNORECASE)
_NOT_ENOUGH = re.compile(r"\bnot\s+enough\b", re.IGNORECASE)

# "N% ... of a goal/target" in either order — "38% of target", "target is 80%,
# hit 60%" style is caught elsewhere by the "%"+"target" proximity check below.
# A bare attention share ("38% of the week was BLT") must NOT match: the word
# after "of" has to be goal/target-shaped, not "the week"/"the window"/etc.
#
# Deliberately one-directional: "the target is 80%" (stating what the target
# IS, which the Truth section is required to do for the ping answer rate) must
# PASS — only a percentage explicitly framed as a share *of* the goal/target
# is the forbidden grading move.
_PERCENT_OF_GOAL = re.compile(
    r"\d+(?:\.\d+)?%\s*(?:of\s+(?:the\s+|his\s+|your\s+)?)?(?:goal|target)\b",
    re.IGNORECASE,
)

# --- comparison to other people or companies --------------------------------

_COMPARISON_PHRASES = re.compile(
    r"\bcompared to\b|\bbetter than\b|\bworse than\b", re.IGNORECASE
)
# "than <Name>" — a capitalised word right after "than" is almost always a
# person or a company, never a metric ("more than 12" has a digit, not a name).
_THAN_NAME = re.compile(r"\bthan\s+[A-Z][a-z]+\b")

# --- generic praise ----------------------------------------------------------

_GENERIC_PRAISE = re.compile(
    r"\bgreat job\b|\bwell done\b|\bamazing\b|\bimpressive\b|\bcrushing it\b",
    re.IGNORECASE,
)

# --- a plan presented as a gain (future tense) — checked only in the Gains
# section, since "One removal" is explicitly a forward-looking step and must
# not be rejected for saying so. -------------------------------------------

_FUTURE_TENSE = re.compile(
    r"\bwill\b|\bgoing to\b|\bplan(?:s|ning)? to\b|\bnext week i\b", re.IGNORECASE
)

# Section headers the deterministic and LLM-composed text both use. Matched
# loosely (## / **, any case) so either rendering style is recognised.
_SECTION_RE = re.compile(
    r"^\s*#{0,3}\s*\**\s*(gains|truth|one removal)\s*\**\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _gains_section(text: str) -> str | None:
    """The text between a 'Gains' header and the next header, if any."""
    headers = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(headers):
        if m.group(1).lower() == "gains":
            start = m.end()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            return text[start:end]
    return None


def validate_language(text: str) -> list[str]:
    """Return every rule violation found in `text`; empty means it may be sent.

    Each entry is `"<rule>: <matched text>"` — enough for the fallback log
    line to say which validator fired (plan §1.7), never enough to need the
    caller to re-derive what happened.
    """
    violations: list[str] = []

    def _check(pattern: re.Pattern, label: str, haystack: str) -> None:
        m = pattern.search(haystack)
        if m:
            violations.append(f"{label}: {m.group(0)!r}")

    _check(_STILL_AWAY, "forward_gap:still_away", text)
    _check(_ONLY_OF_TARGET, "forward_gap:only_of_target", text)
    _check(_BEHIND, "forward_gap:behind", text)
    _check(_SHOULD_HAVE, "forward_gap:should_have", text)
    _check(_NOT_ENOUGH, "forward_gap:not_enough", text)
    _check(_PERCENT_OF_GOAL, "forward_gap:percent_of_goal", text)

    _check(_COMPARISON_PHRASES, "comparison:other", text)
    _check(_THAN_NAME, "comparison:other", text)

    _check(_GENERIC_PRAISE, "praise:generic", text)

    gains_text = _gains_section(text)
    if gains_text is not None:
        _check(_FUTURE_TENSE, "gains:future_tense_as_gain", gains_text)
    else:
        # No recognisable section structure at all (e.g. a one-off LLM
        # rendering that dropped the headers) — safer to scan the whole thing
        # than to let a plan slip through unseen.
        _check(_FUTURE_TENSE, "gains:future_tense_as_gain", text)

    return violations
