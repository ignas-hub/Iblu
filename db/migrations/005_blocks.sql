-- 005_blocks — the reconstructed day (plan §12) + Slack as a signal source.
--
-- `blocks` is IBLU's answer to "where did the day actually go". It is derived
-- data: every block is rebuildable from `signals`, and nothing here is ever the
-- only copy of anything.
--
-- Two rules are encoded in the schema itself:
--   * attention is a three-valued fact, never a boolean — 'ambiguous' is a
--     first-class answer, because silence is never presence;
--   * a correction supersedes, it never deletes (D9) — rebuilding a day marks
--     the old blocks `superseded_by` the new ones and keeps them.

-- Slack joins gmail/chat/calendar as a source of "what I wrote".
ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_source_check;
ALTER TABLE signals ADD CONSTRAINT signals_source_check
    CHECK (source IN ('gmail','chat','calendar','slack'));

CREATE TABLE IF NOT EXISTS blocks (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    local_date        DATE NOT NULL,
    starts_at         TIMESTAMPTZ NOT NULL,
    ends_at           TIMESTAMPTZ NOT NULL,
    venture           TEXT REFERENCES ventures(code),
    work_type         TEXT REFERENCES work_types(code),
    project           TEXT,
    -- What his attention was doing, judged against the intent calendar:
    --   present    — the evidence agrees with what the calendar said he'd do,
    --                or he was working with nothing else claiming the time;
    --   displaced  — an intent existed and the evidence says another venture;
    --   ambiguous  — an intent existed and produced no evidence at all.
    attention         TEXT NOT NULL CHECK (attention IN ('present','displaced','ambiguous')),
    confidence        TEXT NOT NULL CHECK (confidence IN ('fact','inferred')),
    evidence          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- signal ids
    reasoning         TEXT,                                  -- one line, shown on the card
    calendar_event_id TEXT,                                  -- mirror event in SECRETARY_CALENDAR_ID
    intent_event_id   TEXT,                                  -- the primary-calendar event it was judged against
    source            TEXT NOT NULL DEFAULT 'analyst'
                      CHECK (source IN ('analyst','ping','human')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by     BIGINT REFERENCES blocks(id),
    CHECK (ends_at > starts_at)
);

CREATE INDEX blocks_date_idx  ON blocks (local_date, starts_at);
-- The live day: the blocks that have not been corrected away.
CREATE INDEX blocks_live_idx  ON blocks (local_date, starts_at) WHERE superseded_by IS NULL;

-- The analyst owns a watermark like any collector.
INSERT INTO collector_state (name) VALUES ('analyst_blocks')
ON CONFLICT (name) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('005_blocks');
