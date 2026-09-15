-- 010_observation_alerts — remember which findings have already been shouted about.
--
-- The observation log answers "what has IBLU been catching?" for a reader who
-- goes looking. That is the wrong shape for a failure that needs acting on
-- today: Deadlift and Choco were dead for 135 ticks and nobody knew, because
-- nobody was going to look.
--
-- So the critical ones get pushed to the Secretary space. This column is what
-- stops that becoming noise: a problem is announced once, then again only if it
-- is still happening after a cooling-off period. Without it, a broken collector
-- would post every thirty minutes forever and be muted within a day — which is
-- the same as not alerting at all, only louder.

ALTER TABLE observations ADD COLUMN IF NOT EXISTS alerted_at TIMESTAMPTZ;

COMMENT ON COLUMN observations.alerted_at IS
    'When this finding was last announced in Chat. NULL means never announced.';

CREATE INDEX IF NOT EXISTS observations_alertable_idx
    ON observations (severity, alerted_at) WHERE status = 'open';

INSERT INTO schema_migrations (version) VALUES ('010_observation_alerts');
