-- 012_intent_labels — cache for the LLM intent classifier (analyst/intents.py).
--
-- `load_intents` only gives an event a venture when the title/attendees match
-- a keyword or domain, or the calendar it came from has a configured default.
-- Everything else — "Go school", "Tenis 18:00" — reaches the reconstruct with
-- no venture at all, which means it can never make a block `displaced` (an
-- intent with no venture is not a claim on his attention). The classifier
-- fills that gap with one batched LLM call per reconstruct.
--
-- The analyst runs twice a day, so classifying the SAME calendar event again
-- on every run and getting a different answer each time — flapping between
-- 'family' and null, say — would be worse than not classifying it at all.
-- This table makes the same event classify the same way every time.
--
-- `event_key` = sha256(calendar_id + event_id + title): a renamed event is a
-- different key on purpose, so a retitled event is re-classified rather than
-- silently keeping a stale label that no longer matches what it now says.
--
-- Two independent judgements live in one row because they come from the same
-- LLM call about the same event, not because they always travel together:
--   * venture / is_work / confidence         — what this event is ABOUT.
--   * is_ignas_commitment / commitment_confidence — whether it is actually
--     HIS commitment, only asked for events from a calendar that is not
--     exclusively his own (a shared family calendar can hold his wife's or
--     his son's plans just as easily as his). An event that fails this check
--     stays CONTEXT: it is never allowed to create a block or cause
--     `displaced`, no matter how confident the venture guess was.
--
-- Both confidences are stored, not just the booleans, because "classified as
-- false with low confidence" and "classified as false with high confidence"
-- must be told apart — only a `high` confidence result is ever applied
-- (analyst/intents.py), and the low-confidence case still deserves to be
-- remembered so it is not re-asked, and re-answered differently, every run.

CREATE TABLE IF NOT EXISTS intent_labels (
    event_key             TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    venture               TEXT REFERENCES ventures(code),
    is_work               BOOLEAN,
    confidence            TEXT,
    is_ignas_commitment   BOOLEAN,
    commitment_confidence TEXT,
    model                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE intent_labels IS
    'Cache of the analyst intent classifier''s per-event verdicts, keyed so a '
    'renamed event is re-classified. Only high-confidence rows are ever '
    'applied to a reconstruction; see analyst/intents.py.';

INSERT INTO schema_migrations (version) VALUES ('012_intent_labels')
    ON CONFLICT DO NOTHING;
