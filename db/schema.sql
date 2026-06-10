-- iblu-keeper — Postgres schema sketch (PHASE 2, not used in Phase 1).
--
-- Included now so the memory layer plugs in without rewrites. The
-- context.* tool stubs (src/iblu_keeper/tools/context.py) will persist to
-- these tables in Phase 2. Apply with: psql "$DATABASE_URL" -f db/schema.sql

-- Raw conversation turns (Chat + email + assistant), for long-term memory.
CREATE TABLE IF NOT EXISTS conversations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       TEXT        NOT NULL,            -- 'chat' | 'email' | 'assistant'
    conversation TEXT        NOT NULL,            -- space id / thread id
    role         TEXT        NOT NULL,            -- 'user' | 'assistant' | 'contact'
    sender       TEXT,
    text         TEXT        NOT NULL,
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_conv ON conversations (conversation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations (created_at DESC);

-- Rolling summaries ("what happened in the last day").
CREATE TABLE IF NOT EXISTS summaries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    window       TEXT        NOT NULL,            -- '1d' | '7d' | ...
    period_start TIMESTAMPTZ NOT NULL,
    period_end   TIMESTAMPTZ NOT NULL,
    summary      TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Drafts (Phase 1 stores these as JSONL on disk; Phase 2 moves them here).
CREATE TABLE IF NOT EXISTS drafts (
    id         TEXT PRIMARY KEY,
    kind       TEXT        NOT NULL,              -- 'chat' | 'email'
    status     TEXT        NOT NULL DEFAULT 'pending',
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 3: goals & priorities the assistant reasons over
-- (e.g. "spend 30% of time on sales").
CREATE TABLE IF NOT EXISTS goals (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label      TEXT        NOT NULL,
    weight     NUMERIC,
    details    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    active     BOOLEAN     NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
