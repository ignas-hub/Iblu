"""The index at the top of HANDOFF.md must match the sections below it.

An index that drifts is worse than none: a reader trusts it to say what exists,
so a missing row makes a section invisible in exactly the situation the index
was added for — a truncated read that cannot see the bottom of the file.

Why it was added. IBLU's own `repo` tool returns at most 20,000 characters per
`read`, and `HANDOFF.md` is 56,000. A session reading it in one call got
sections 1–10 of 29 and reported the record as ending in September; §§28–29,
which carry the rules currently in force, were past the cut. The index sits in
the first ~3,000 characters so it survives any truncation, and tells a reader
what it has not reached yet.
"""

from __future__ import annotations

import pathlib
import re

import pytest

HANDOFF = pathlib.Path(__file__).resolve().parents[1] / "HANDOFF.md"
TEXT = HANDOFF.read_text()

# `## 10.` and `### 12.` both count — the levels are inconsistent for historical
# reasons and the numbers are what code comments cite.
SECTION_RE = re.compile(r"^#{2,3} (\d+)\. *(.+)$", re.M)
ROW_RE = re.compile(r"^\| (\d+) \| (\d{4}-\d{2}-\d{2}|\?) \| (.+?) \|$", re.M)


def _sections() -> list[tuple[int, str]]:
    return [(int(n), t.strip()) for n, t in SECTION_RE.findall(TEXT)]


def _rows() -> list[tuple[int, str, str]]:
    return [(int(n), d, t.strip()) for n, d, t in ROW_RE.findall(TEXT)]


def test_every_section_is_in_the_index():
    missing = {n for n, _ in _sections()} - {n for n, _, _ in _rows()}
    assert not missing, (
        f"HANDOFF.md §{sorted(missing)} missing from the index. Add a row "
        "`| <n> | <YYYY-MM-DD> | <title> |` to the Index table — a section the "
        "index does not list is invisible to a truncated read."
    )


def test_the_index_invents_nothing():
    phantom = {n for n, _, _ in _rows()} - {n for n, _ in _sections()}
    assert not phantom, f"index lists §{sorted(phantom)}, which do not exist"


def test_the_index_is_in_section_order():
    numbers = [n for n, _, _ in _rows()]
    assert numbers == sorted(numbers), "index rows are out of order"


def test_each_title_matches_its_section():
    """A retitled section must not keep its old index row.

    The trailing `(YYYY-MM-DD)` some headings carry is stripped in the index —
    the date has its own column — so compare on the rest.
    """
    by_number = dict(_sections())
    for number, _date, title in _rows():
        heading = re.sub(r"\s*\(20\d\d-\d\d-\d\d\)\s*$", "", by_number[number])
        assert title == heading, (
            f"§{number} index row says {title!r} but the heading says {heading!r}"
        )


def test_the_index_survives_truncation():
    """It is useless unless it is inside the first window of any capped read."""
    from iblu_keeper.tools.repo import MAX_CHARS

    end = TEXT.index("There is no §11")
    assert end < MAX_CHARS // 2, (
        f"the index ends at character {end}, too deep to be sure it survives a "
        f"{MAX_CHARS}-character read"
    )


def test_the_index_says_how_to_read_the_rest():
    """The one instruction a truncated reader needs, where it will see it."""
    head = TEXT[:4000]
    assert "next_offset" in head
    assert "truncated" in head
    assert "Do not summarise" in head


def test_the_missing_eleventh_section_is_explained():
    """Otherwise it reads as a mistake in the index rather than in history."""
    assert "There is no §11" in TEXT
    assert "never renumbered" in TEXT
    assert 11 not in {n for n, _ in _sections()}


@pytest.mark.parametrize("stale", ["_Last updated: 2026-06-10_"])
def test_the_header_no_longer_carries_a_hand_maintained_date(stale):
    """It was four months and nineteen sections out of date when found."""
    assert stale not in TEXT
