"""Turning a raw message body into a storable snippet.

Plan D8: store a snippet plus the native id, never full content. Snippets are
<=300 chars with quoted text and signatures stripped — otherwise every reply
snippet is just the previous message quoted back, and the recording is useless.

Kept separate from the collectors so the stripping rules are unit-testable
without any Google API.
"""

from __future__ import annotations

import re

SNIPPET_MAX = 300

# "On Mon, 15 Jun 2026 at 10:05, Tamara <t@x.com> wrote:" — the attribution
# line Gmail and most clients put above quoted text. May wrap onto two lines,
# so the trailing colon is optional when the line starts with "On ".
_QUOTE_ATTRIBUTION = re.compile(
    r"^\s*(On .*wrote:\s*$|On .{0,120},\s*$|-{2,}\s*Original Message\s*-{2,}|"
    r"From:\s.+|_{10,}|Sent from my \w+)",
    re.IGNORECASE,
)

# A signature delimiter: "-- " on its own line (RFC 3676), or a bare "--".
_SIGNATURE = re.compile(r"^--\s*$")


def strip_quoted(text: str) -> str:
    """Drop quoted history and anything after a signature delimiter.

    Cuts at the first line that is quoted (`>`), is a quote attribution
    ("On ... wrote:"), or is a signature delimiter ("-- ").
    """
    if not text:
        return ""

    kept: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(">"):
            break
        if _SIGNATURE.match(line.rstrip()):
            break
        if _QUOTE_ATTRIBUTION.match(line):
            break
        kept.append(line)

    return "\n".join(kept).strip()


def collapse(text: str) -> str:
    """Squeeze whitespace so a snippet reads as one compact line."""
    return re.sub(r"\s+", " ", text or "").strip()


def snippet(text: str, limit: int = SNIPPET_MAX) -> str:
    """The storable form: unquoted, unsigned, collapsed, truncated."""
    cleaned = collapse(strip_quoted(text))
    if len(cleaned) <= limit:
        return cleaned
    # Prefer a word boundary so snippets don't end mid-word.
    cut = cleaned[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def unquoted_length(text: str) -> int:
    """Characters I actually wrote — the effort signal, before truncation."""
    return len(collapse(strip_quoted(text)))
