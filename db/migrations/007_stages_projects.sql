-- 007_stages_projects — the initiative registry (plan §1.5).
--
-- Everything IBLU has tracked so far is either a raw observation (`signals`)
-- or a durable note (`context_entries`); neither answers "what state is this
-- *thing* in, and for how long". `stages` fixes the ladder every initiative
-- climbs — from a first look to running without Ignas — and `projects` gives
-- that ladder something to apply to: a named row instead of whatever free
-- text happened to show up in a mailbox that week.
--
-- Two rules matter more than the columns:
--
--   * a project cannot enter the top of the ladder, 'autonomous', while
--     `done_means` is NULL. Reaching the end of the ladder only means
--     something if "done" was written down before it happened — otherwise
--     'autonomous' is just a label nobody can check. This is enforced in
--     code (store/projects.py), not by a CHECK, because the DB has no way to
--     express "NULL unless this other column is set" as a friendly error.
--   * every stage change is also a `project_stage_history` row. `stage` and
--     `stage_since` on `projects` are the current fact; the history table is
--     what makes "how long has this actually sat here" and "who moved it"
--     answerable without trusting a single mutable timestamp.
--
-- `projects` rows are NOT seeded here. The ~28 candidate projects in plan
-- §1.5 are Ignas's *inferred* portfolio as of 2026-09-13 — cross-checked
-- against signals, repos and Drive rather than asked of him — and that
-- cross-checking is code, not SQL, so it can be re-run as the evidence
-- changes. See store/projects.py:seed().
--
-- `signals.project`, `context_entries.project` and `blocks.project` stay
-- free text and are resolved against `projects.code` / `.aliases` at write
-- time (store/projects.py:resolve()); a name nothing here recognises is kept
-- as-is and surfaced by `unregistered()` rather than silently dropped.

CREATE TABLE IF NOT EXISTS stages (
    code    TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    ordinal SMALLINT NOT NULL UNIQUE
);
INSERT INTO stages (code, label, ordinal) VALUES
    ('research',   'Researching something new',                     1),
    ('initiate',   'Initiating',                                    2),
    ('build',      'Building a trial',                              3),
    ('trial',      'Trialing',                                      4),
    ('implement',  'Implementing fully',                            5),
    ('deliver',    'Making sure it delivers value now',             6),
    ('maintain',   'Maintaining',                                   7),
    ('autonomous', 'Delivers value in the long run without Ignas',  8)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS projects (
    code             TEXT PRIMARY KEY,
    venture          TEXT NOT NULL REFERENCES ventures(code),
    name             TEXT NOT NULL,
    stage            TEXT NOT NULL REFERENCES stages(code),
    stage_since      DATE NOT NULL,
    stage_confidence TEXT NOT NULL DEFAULT 'inferred'
                     CHECK (stage_confidence IN ('fact','inferred')),
    owner            TEXT,     -- person or automation that runs it; NULL = Ignas
    done_means       TEXT,     -- the internal finish line for 'autonomous'; required to enter it (enforced in code)
    aliases          TEXT[] NOT NULL DEFAULT '{}',   -- names as they appear in mail/chat/repos, lowercase
    active           BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- "What's live for this venture" is the review's most common lookup.
CREATE INDEX projects_venture_active_idx ON projects (venture, active);
-- resolve() checks alias membership on every signal write; without this it's
-- a sequential scan of the whole registry per signal.
CREATE INDEX projects_aliases_idx ON projects USING GIN (aliases);

CREATE TABLE IF NOT EXISTS project_stage_history (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project    TEXT NOT NULL REFERENCES projects(code),
    from_stage TEXT REFERENCES stages(code),      -- NULL only if a project could be created mid-history; not currently possible
    to_stage   TEXT NOT NULL REFERENCES stages(code),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by TEXT NOT NULL,                      -- 'ignas' (a tap) / 'claude' / 'seed'
    note       TEXT
);
CREATE INDEX project_stage_history_project_idx ON project_stage_history (project, changed_at DESC);

INSERT INTO schema_migrations (version) VALUES ('007_stages_projects');
