-- 004_multi_account_calendar
--
-- calendar_seen is the memory of which events have already been reported. With
-- one Workspace its primary key (calendar_id, event_id) was enough. With three,
-- two different Workspaces can each have a calendar called 'primary' — and
-- event ids are only unique within a calendar — so without an account column
-- one Workspace's baseline would answer for another's, and IBLU would either
-- miss real changes or report another company's meetings as Ignas's.

ALTER TABLE calendar_seen ADD COLUMN account TEXT;

-- Everything recorded so far belongs to the primary account.
UPDATE calendar_seen SET account = 'ignas@blanklabel.team' WHERE account IS NULL;

ALTER TABLE calendar_seen ALTER COLUMN account SET NOT NULL;

ALTER TABLE calendar_seen DROP CONSTRAINT calendar_seen_pkey;
ALTER TABLE calendar_seen ADD PRIMARY KEY (account, calendar_id, event_id);

INSERT INTO schema_migrations (version) VALUES ('004_multi_account_calendar');
