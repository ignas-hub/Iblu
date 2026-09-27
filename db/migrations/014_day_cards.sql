-- 014_day_cards — the evening day card: "here is what I think you did today".
--
-- Every other ping (`pings`) asks an abstract question ("was that yours to
-- do?"). The day card is different in kind, not just in wording: it shows the
-- whole reconstructed day as a lettered list and asks him to bless it or fix
-- it, in one tap ("All correct") or one sentence ("C was Deadlift ·
-- Machina"). Reusing `pings` itself was considered and rejected — a day card
-- has exactly two tap options (not a snapshot of qid/option payloads to
-- replay) and its own reply grammar (line letters resolving back to specific
-- blocks), and bending `pings.questions` to fit that would make both harder
-- to read.
--
-- The one thing that DOES have to be reused verbatim from the ping is the
-- reason `pings.questions` exists at all: a reply can arrive hours after the
-- card was sent, long after the day has been rebuilt and the block ids the
-- card originally pointed at have changed or been superseded. `lines` is that
-- same kind of snapshot — [{letter, block_ids, starts_at, ends_at, venture,
-- work_type, project, mark}] — so "C" still resolves to the right block(s)
-- no matter what has happened to the day's reconstruction since.
--
-- One row per day, like `pings_one_per_day`: a second run the same day (a
-- manual retry) updates the snapshot only while nothing has actually reached
-- Chat yet, exactly like `pings`' own upsert — so a stale tap on a card
-- already on his phone still means exactly what he saw.

CREATE TABLE IF NOT EXISTS day_cards (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    local_date       DATE NOT NULL,
    status           TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'answered')),
    sent_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    chat_message_ref TEXT,                       -- webhook response .name
    chat_thread_ref  TEXT,                       -- webhook response .thread.name
    lines            JSONB NOT NULL DEFAULT '[]'::jsonb,
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS day_cards_one_per_day ON day_cards (local_date);

COMMENT ON COLUMN day_cards.lines IS
    'Snapshot at send time: [{letter, block_ids, starts_at, ends_at, venture, '
    'work_type, project, mark}]. Read by a reply arriving later so a line '
    'letter always resolves to the block(s) it named, even after the day has '
    'been rebuilt. Never read for display after the card is sent — the '
    'Secretary calendar and `blocks` are the live truth.';

INSERT INTO schema_migrations (version) VALUES ('014_day_cards')
    ON CONFLICT DO NOTHING;
