"""Tool annotation policy.

IMPORTANT — what this does NOT do. Verified 2026-09-13: claude.ai does **not**
derive its per-tool Allow/Ask setting from these annotations. Five tools with
byte-identical annotations show different states in the connector UI, split
purely by when each was first registered — tools present when the connector was
last configured are Allow, tools that appeared afterwards default to Ask. The
permission is a preference stored client-side per connector; no annotation,
`_meta` field or server capability overrides it.

What the annotations DO control is the grouping in that UI: the 16 tools with
`readOnlyHint: true` are exactly the "Read-only tools (16)" group, which is why
getting them right still matters — a correct group makes the group-level
"always allow" control usable in one action instead of tool by tool.

Ignas's policy, stated 2026-09-13: **only sending a message to another human
should ask.** Everything else — reads, drafts, Drive and Docs writes, memory
writes — should be allowed, because a prompt on every call makes the assistant
unusable by voice, which is its primary mode.

Revised the same evening: a reply is a message to another human too. A threaded
reply lands in someone's inbox exactly as a new mail does, and the thread makes
it *more* likely to be read, not less. `gmail_reply` joined the ask class.

This test keeps the annotations honest so the grouping stays correct and the
intent is recorded. It cannot enforce the client's behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "src" / "iblu_keeper" / "server.py"

# The only tools that may ask. Each puts a message in front of another person
# and cannot be taken back.
MUST_ASK = {"gmail_send_email", "chat_send_message", "gmail_reply"}

ANNOTATION_KEYS = {
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
}

_TOOL = re.compile(r'@mcp\.tool\(name="([a-z_]+)",\s*annotations=\{(.*?)\}\)', re.S)


def _tools() -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    for name, block in _TOOL.findall(SERVER.read_text()):
        flags = {}
        for key in ANNOTATION_KEYS:
            m = re.search(rf'"{key}":\s*(True|False)', block)
            if m:
                flags[key] = m.group(1) == "True"
        out[name] = flags
    return out


def _asks(flags: dict[str, bool]) -> bool:
    return flags.get("destructiveHint", False) or flags.get("openWorldHint", False)


def test_every_tool_declares_all_four_annotations():
    """A missing hint means the client guesses, and clients guess 'ask'."""
    missing = {
        name: sorted(ANNOTATION_KEYS - set(flags))
        for name, flags in _tools().items()
        if set(flags) != ANNOTATION_KEYS
    }
    assert not missing, f"tools with incomplete annotations: {missing}"


def test_only_sending_to_a_human_is_marked_as_needing_confirmation():
    asking = {name for name, flags in _tools().items() if _asks(flags)}
    assert asking == MUST_ASK, (
        f"permission policy drift.\n"
        f"  unexpectedly asking : {sorted(asking - MUST_ASK)}\n"
        f"  should ask but does not: {sorted(MUST_ASK - asking)}"
    )


@pytest.mark.parametrize("name", sorted(MUST_ASK))
def test_every_send_keeps_both_flags(name):
    """Either flag alone is enough to prompt; require both, explicitly."""
    flags = _tools()[name]
    assert flags["destructiveHint"] is True
    assert flags["openWorldHint"] is True


def test_read_only_tools_never_claim_to_write():
    for name, flags in _tools().items():
        if flags.get("readOnlyHint"):
            assert not flags["destructiveHint"], f"{name} is readOnly yet destructive"
            assert not flags["openWorldHint"], f"{name} is readOnly yet open-world"


def test_the_tool_count_is_what_the_docs_claim():
    """README and STATE.md quote a number; keep them honest."""
    count = len(_tools())
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert f"**{count} tools**" in readme, (
        f"{count} tools registered, README does not say so"
    )
