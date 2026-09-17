-- 013_intent_attendance — a third attendance state: maybe.
--
-- "Sometimes I go, sometimes I don't" (Ignas, about his son's football
-- practice and school parents' meetings) fits neither is_ignas_commitment
-- =true (his own activity) nor =false (someone else's plan, a whereabouts
-- marker). The boolean forced every family-venture event shared with a
-- child or a spouse into one of two wrong answers. `attendance` replaces it
-- with a third value:
--
--   his     — an activity Ignas himself attends. Kept in sync with
--             is_ignas_commitment = true.
--   maybe   — a family activity he sometimes joins but is not implied to
--             attend by default (a child's sport, a school event). Never a
--             commitment, never displaces work, but its silence is still
--             read the same cautious way a confirmed commitment's is
--             (analyst/blocks.py, _apply_maybe_family_inference).
--   not_his — someone else's appointment or whereabouts, a reminder, a
--             delivery, a birthday. Kept in sync with is_ignas_commitment
--             = false.
--
-- is_ignas_commitment / commitment_confidence are kept, still written as
-- attendance == 'his' and attendance_confidence, for anything still reading
-- those columns directly (analyst/intents.py).
--
-- A row with attendance IS NULL is a label made before this migration —
-- analyst/intents.py treats that as a cache MISS for the commitment-check
-- half of a classification and re-asks, so every existing label upgrades to
-- the three-way answer on its own, on the next reconstruct that touches it,
-- with no backfill needed.

ALTER TABLE intent_labels ADD COLUMN IF NOT EXISTS attendance TEXT;
ALTER TABLE intent_labels ADD COLUMN IF NOT EXISTS attendance_confidence TEXT;

COMMENT ON COLUMN intent_labels.attendance IS
    'his, maybe or not_his — see analyst/intents.py. NULL means this row '
    'predates the migration (or the migration is not applied yet) and must '
    'be re-classified, not that the event has no attendance concept.';

COMMENT ON COLUMN intent_labels.attendance_confidence IS
    'high or low, same meaning as the old commitment_confidence. Only a '
    'high-confidence ''his''/''not_his'' is ever applied to an Interval; '
    '''maybe'' is applied at any confidence — it is already the cautious '
    'answer.';

INSERT INTO schema_migrations (version) VALUES ('013_intent_attendance')
    ON CONFLICT DO NOTHING;
