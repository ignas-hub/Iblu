-- 017_rules — the rules Ignas gives Claude, kept where every session can read them.
--
-- Ignas, 2026-11-24: "I want to have a system where I report what's new and
-- I want IBLU to be updating things, my instructions." Until now a rule he
-- stated in a chat lived in that chat. It reached other sessions only if he
-- pasted it into a Google Doc, then into ~/.claude/CLAUDE.md on each machine,
-- then into claude.ai's preference and Project fields. Most never made it.
--
-- This table is the source of truth. `rules_write` adds a row from any Claude
-- chat ("update my rules at iblu: ..."); `rules_read` renders the active rows
-- as a paste-ready block per destination; the SessionStart hook on this box
-- puts them in front of every Claude Code session without any file edit.
--
-- D9 applies: a correction supersedes, it never deletes. `edit` inserts the
-- new wording and retires the old row with `superseded_by` pointing forward.

CREATE TABLE IF NOT EXISTS rules (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    text           TEXT NOT NULL CHECK (length(btrim(text)) > 0),
    -- rule: how Claude must behave. frustration: what went wrong, in his
    -- words, not yet distilled into a rule. fact: a definition or a name he
    -- corrected ("a person is not a persona").
    kind           TEXT NOT NULL DEFAULT 'rule'
                   CHECK (kind IN ('rule', 'frustration', 'fact')),
    -- where it applies: every destination, Claude Code sessions, claude.ai
    -- chats (account preferences), or a Project's instructions.
    scope          TEXT NOT NULL DEFAULT 'all'
                   CHECK (scope IN ('all', 'code', 'chat', 'project')),
    context        TEXT,
    source         TEXT NOT NULL DEFAULT 'chat',
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'retired')),
    retired_at     TIMESTAMPTZ,
    retire_note    TEXT,
    superseded_by  BIGINT REFERENCES rules (id),
    doc_synced_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS rules_active ON rules (scope, created_at) WHERE status = 'active';

COMMENT ON COLUMN rules.context IS
    'What happened that produced the rule, in Ignas''s words. Kept so a later '
    'reader knows why the rule exists; never shown to clients.';
COMMENT ON COLUMN rules.doc_synced_at IS
    'When this row was appended to the Google Doc named by RULES_DOC_ID. NULL '
    'means the append failed or the doc is not configured; the row is still '
    'the truth.';
