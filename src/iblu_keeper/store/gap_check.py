"""Noticing when a goal is written as a Gap, and saying so once.

Ignas measures himself against ideals and beats himself up for what he has not
reached. The practice IBLU is built around is the opposite: progress stated
backward, as dated evidence of what now exists. A priority phrased as "become
the best" or "beat X" cannot be measured backward at all — in a year it will
still be a distance, never an arrival.

So when a decision or preference is written in Gap language, the tool response
carries a `gap_warning` with a backward-measurable rewrite. It **never rejects
the write.** A warning that blocks is a grader, and IBLU measures; it does not
grade. Ignas gets to phrase his own goals however he likes; IBLU just says once
what that phrasing will cost him when the year is up.
"""

from __future__ import annotations

import re

# Each rule is (compiled pattern, what it is, the rewrite to suggest).
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\b(?:the\s+)?(?:best|biggest|largest|leading|number one|no\.?\s*1|#1|top)\b", re.I),
        "a superlative",
        "name the thing that will exist — \"three retained clients over 20K/mo\" "
        "rather than \"the best agency\"",
    ),
    (
        re.compile(r"\b(?:better|faster|bigger|stronger|cheaper|more)\s+than\b", re.I),
        "a comparison to someone else",
        "measure against where this venture stood on the baseline date, not "
        "against another company",
    ),
    (
        re.compile(r"\b(?:beat|outperform|overtake|catch up (?:to|with)|keep up with)\b", re.I),
        "a race against someone else",
        "state the outcome you want to exist, independent of what anyone else does",
    ),
    (
        re.compile(r"\bcompared to\b|\bvs\.?\s+\w+", re.I),
        "a comparison to someone else",
        "compare to the baseline instead",
    ),
    (
        re.compile(r"\b(?:should|must|need to)\s+(?:be|have|reach|hit)\b", re.I),
        "an obligation rather than an outcome",
        "describe what will be true, not what you owe",
    ),
    (
        re.compile(r"\b(?:not|never)\s+(?:enough|there yet)\b|\bstill\s+(?:not|behind)\b", re.I),
        "a distance from an ideal",
        "say what exists now and what will exist; the gap between them is the work",
    ),
]

# A priority that names no observable end state cannot be checked off, which is
# the quiet version of the same problem.
_MEASURABLE = re.compile(
    r"\d|\b(?:at least|by |signed|launched|live|hired|shipped|closed|handed over|autonomous)\b",
    re.I,
)


def check(type: str, content: str, tags: list[str] | None = None) -> str | None:
    """One line of advice, or None. Never raises, never blocks the write."""
    if type not in {"decision", "preference"}:
        return None
    tags = tags or []

    for pattern, what, suggestion in _RULES:
        if pattern.search(content or ""):
            return (
                f"This reads as a Gap — it contains {what}. "
                f"{suggestion[0].upper()}{suggestion[1:]}."
            )

    # Only held to the stricter standard when it claims to be a priority: an
    # ordinary decision is allowed to be a sentence about a choice.
    if "priority" in tags and not _MEASURABLE.search(content or ""):
        return (
            "This priority names no observable end state, so in twelve months "
            "there will be nothing to measure backward to. Add the thing that "
            "will exist — a number, a date, or a named handover."
        )
    return None
