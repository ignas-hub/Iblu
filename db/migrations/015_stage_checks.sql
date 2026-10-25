-- 015_stage_checks — the Friday question that produces the Gain.
--
-- The mission defines the Gain precisely: "Progress is measured backward
-- against the baseline... an initiative reaching 'autonomous' is the largest
-- gain." The ladder (`stages`), the baselines and 28 `projects` all existed on
-- 2026-09-13. Six weeks later `project_stage_history` had zero rows and every
-- project still sat on its seeded rung, so every weekly review could only list
-- tasks — and his verdict on them was "random tasks closed... they do not show
-- how I'm getting closer to my goals".
--
-- Nothing records a rung climbed because nothing asks. This table is the ask:
-- one row per Friday, a snapshot of the projects that had evidence that week
-- with the rung the evidence suggests, and one tap per project to confirm,
-- hold, or declare autonomous. A tap writes `project_stage_history` and moves
-- `projects.stage`, and the following Friday's review opens with the rung as
-- a Gain. His tap produces the thing he reads — the exchange the day card
-- never offered.
--
-- `proposals` is a snapshot for the same reason `day_cards.lines` is: a tap can
-- arrive days later, after stages have moved for other reasons, and letter "B"
-- must still mean the project it named when he saw it.

CREATE TABLE IF NOT EXISTS stage_checks (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    local_date       DATE NOT NULL,
    status           TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'answered')),
    sent_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    chat_message_ref TEXT,
    chat_thread_ref  TEXT,
    proposals        JSONB NOT NULL DEFAULT '[]'::jsonb,
    answers          JSONB NOT NULL DEFAULT '{}'::jsonb,
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS stage_checks_one_per_day ON stage_checks (local_date);

COMMENT ON COLUMN stage_checks.proposals IS
    'Snapshot at send time: [{letter, project, name, venture, from_stage, '
    'from_ordinal, next_stage, evidence:{signals, sources, subjects}}]. A tap '
    'resolves its letter against this, never against the live projects table.';

COMMENT ON COLUMN stage_checks.answers IS
    '{letter: {stage, at, moved}} — one entry per project he answered. '
    'The authoritative record of a move is project_stage_history; this is '
    'the card remembering what was tapped.';

INSERT INTO schema_migrations (version) VALUES ('015_stage_checks')
    ON CONFLICT DO NOTHING;
