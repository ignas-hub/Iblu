-- 009_signal_exclusion — a way to say "this one was never real work".
--
-- The sense-check pass found a signal built from an acceptance test: a mail to
-- a@b.com with the subject "subj" and the body "body", sent while proving the
-- send path worked, and then counted ever afterwards as fifteen minutes of
-- admin on a Saturday.
--
-- Deleting it is not an option — corrections supersede, they never delete, and
-- a deleted signal would also make the collector forget it had seen the
-- message and re-collect it on the next overlapping window (the same trap as
-- `calendar_seen`, HANDOFF §13). So the row stays and gains a reason for being
-- ignored. The analyst skips excluded signals; everything else can still see
-- them, which is what makes this auditable rather than a quiet edit.

ALTER TABLE signals ADD COLUMN IF NOT EXISTS excluded_reason TEXT;

COMMENT ON COLUMN signals.excluded_reason IS
    'Non-NULL means this signal is real but is not evidence of work — test '
    'sends, fixtures, anything produced while proving a path worked. Never '
    'delete a signal; exclude it and say why.';

-- Partial index: the analyst''s hot path asks only for the ones still counted.
CREATE INDEX IF NOT EXISTS signals_counted_idx
    ON signals (occurred_at DESC) WHERE excluded_reason IS NULL;

INSERT INTO schema_migrations (version) VALUES ('009_signal_exclusion');
