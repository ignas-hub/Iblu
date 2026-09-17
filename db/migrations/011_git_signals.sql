-- 011_git_signals — git commits join gmail/chat/calendar/slack as evidence.
--
-- About 30% of a reconstructed workday was unaccounted for, and a large share
-- of Ignas's real work is writing code (IBLU itself, Radovi, Machina, BT
-- automations) that leaves no email or chat trace. A commit he authored is as
-- real a signal as a message he sent — this just widens `signals.source` to
-- admit it, same DROP/ADD pattern 005_blocks.sql used to add 'slack'.

ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_source_check;
ALTER TABLE signals ADD CONSTRAINT signals_source_check
    CHECK (source IN ('gmail','chat','calendar','slack','git'));

INSERT INTO schema_migrations (version) VALUES ('011_git_signals');
