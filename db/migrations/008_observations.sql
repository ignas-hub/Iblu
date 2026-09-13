-- 008_observations — the log of things IBLU noticed were wrong.
--
-- Until now, every time IBLU caught itself being wrong it wrote a line to the
-- journal and forgot. The judge rejecting an LLM response, the ping composer
-- falling back, the weekly review failing its own language check, a collector
-- erroring for one account — roughly thirty places that each noticed something
-- real and then let it rotate away. Nobody could answer "what has IBLU been
-- quietly catching?", which is exactly the question a later session needs.
--
-- This table is that answer. Two kinds of entry live here and are kept apart by
-- `detected_by`:
--   'rule' — a deterministic invariant failed (blocks overlap, a watermark did
--            not advance, an LLM response was rejected by its schema);
--   'llm'  — the sense-check pass read what the scripts produced and said it
--            looked wrong. That one is a suspicion, never a fact, and the
--            column exists so nothing downstream can confuse the two.
--
-- Deduplicated on `fingerprint`: the same failure every ten minutes is one
-- observation with a count, not 144 rows a day. Resolving is a state change,
-- never a delete — the same rule as everywhere else in IBLU.

CREATE TABLE IF NOT EXISTS observations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    occurrences  INT NOT NULL DEFAULT 1,

    source       TEXT NOT NULL,          -- 'judge'|'composer'|'weekly'|'tick'|'analyst'|'collector'|'sensecheck'
    kind         TEXT NOT NULL,          -- short slug: 'llm_output_rejected', 'blocks_overlap', ...
    severity     TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('info','warn','error')),
    -- Whether a rule caught this or a model suspects it. Never merge the two:
    -- a failed invariant is a fact, an LLM's opinion is a lead.
    detected_by  TEXT NOT NULL DEFAULT 'rule' CHECK (detected_by IN ('rule','llm')),

    summary      TEXT NOT NULL,          -- one line, readable on its own
    detail       TEXT,                   -- the longer story, including any exception text
    evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,  -- ids, dates, counts — enough to reproduce

    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    resolved_at  TIMESTAMPTZ,
    resolution   TEXT,

    -- Stable across repeats of the same problem; supplied by the caller.
    fingerprint  TEXT NOT NULL
);

-- One OPEN row per distinct problem. A resolved row stays as history, and the
-- same problem recurring afterwards correctly opens a new one.
CREATE UNIQUE INDEX observations_open_fingerprint
    ON observations (fingerprint) WHERE status <> 'resolved';

CREATE INDEX observations_open_idx ON observations (last_seen_at DESC) WHERE status = 'open';
CREATE INDEX observations_source_idx ON observations (source, last_seen_at DESC);

INSERT INTO schema_migrations (version) VALUES ('008_observations');
