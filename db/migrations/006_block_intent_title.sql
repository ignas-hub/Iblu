-- 006_block_intent_title — remember WHICH intent a block was judged against.
--
-- `intent_event_id` identifies the calendar event, but resolving it back to a
-- title means an API call per block, and the event may have been edited or
-- deleted since. A block is a record of what was true when it was made, so it
-- keeps the title it was judged against. Without this the Secretary calendar
-- can only say "unattributed" for every ambiguous hour, when what it knows is
-- "the calendar said 'Womanizer alignment' and nothing happened".

ALTER TABLE blocks ADD COLUMN IF NOT EXISTS intent_title TEXT;

INSERT INTO schema_migrations (version) VALUES ('006_block_intent_title');
