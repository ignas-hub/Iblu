-- 003_mission
--
-- The mission gets its own column rather than a section inside
-- context_brief.content (decision M1). The June constraint is that only the
-- compactor writes to context_brief; a separate column makes it structurally
-- impossible for a future context_compact run to rewrite or drop the mission.
-- `content` stays the compactor's.
--
-- docs/MISSION.md is the source of truth; this row is the runtime copy, seeded
-- by `python -m iblu_keeper.db seed-mission` and compared by sha so drift is
-- visible rather than silent.

ALTER TABLE context_brief
    ADD COLUMN mission           TEXT NOT NULL DEFAULT '',
    ADD COLUMN mission_sha       TEXT,
    ADD COLUMN mission_seeded_at TIMESTAMPTZ;

INSERT INTO schema_migrations (version) VALUES ('003_mission');
