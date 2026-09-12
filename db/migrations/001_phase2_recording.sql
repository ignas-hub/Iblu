-- 001_phase2_recording — Phase 2 "recording v1" (the spy).
-- Plan: docs/plans/2026-09-14-recording-v1.md §4.
-- Applied by `python -m iblu_keeper.db migrate`; tracked in schema_migrations.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ventures (
    code       TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT true,
    sort_order INT NOT NULL DEFAULT 100
);
INSERT INTO ventures (code, label, sort_order) VALUES
    ('blt',      'Blank Label Team',                                             10),
    ('choco',    'Choco Agency (Opera client; operationally BLT)',               20),
    ('deadlift', 'Deadlift.io (ads agency; Machina = its main software project)',30),
    ('jakusi',   'Jakusi — house / property (Opatija)',                          40),
    ('family',   'Family & personal life',                                       50),
    ('personal', 'Own tooling & infra (IBLU, accounting bot, servers)',          60)
ON CONFLICT DO NOTHING;

CREATE TABLE work_types (
    code       TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    sort_order INT NOT NULL DEFAULT 100
);
INSERT INTO work_types (code, label, sort_order) VALUES
    ('sales',    'Sales / BD / pitches',                      10),
    ('client',   'Existing-client communication & management',20),
    ('delivery', 'Doing the work (campaigns, creative, ads)', 30),
    ('people',   'Managing / hiring / team',                  40),
    ('finance',  'Finance, invoices, legal, admin',           50),
    ('build',    'Software, automation, infrastructure',      60),
    ('admin',    'Ops, tooling, scheduling, housekeeping',    70),
    ('life',     'Non-work: family, home, health',            80)
ON CONFLICT DO NOTHING;

-- Observations. Never read directly into the brief (D1).
CREATE TABLE signals (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source             TEXT NOT NULL CHECK (source IN ('gmail','chat','calendar')),
    kind               TEXT NOT NULL,            -- gmail/chat: 'sent' ; calendar: 'created'|'moved'|'changed'|'cancelled'
    account            TEXT NOT NULL,            -- 'ignas@blanklabel.team' (multi-account in week 2)
    occurred_at        TIMESTAMPTZ NOT NULL,
    actor              TEXT NOT NULL DEFAULT 'me' CHECK (actor IN ('me','other')),
    initiator          TEXT CHECK (initiator IN ('me','other')),   -- who started the thread
    counterpart        TEXT,                     -- email / person / space display name
    container          TEXT,                     -- gmail thread id / chat space name / calendar id
    subject            TEXT,                     -- email subject / space name / event title
    snippet            TEXT,                     -- <=300 chars of what I wrote / the change
    ask_snippet        TEXT,                     -- <=300 chars of the message I was responding to
    length_chars       INT,
    venture            TEXT REFERENCES ventures(code),
    venture_confidence TEXT NOT NULL DEFAULT 'inferred' CHECK (venture_confidence IN ('fact','inferred')),
    work_type          TEXT REFERENCES work_types(code),
    project            TEXT,                     -- free text, e.g. 'machina', 'bt', 'email-writer'
    summary            TEXT,                     -- one line per exchange; filled by the analyst pass (week 2)
    source_ref         TEXT NOT NULL,            -- native id
    meta               JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_ref)
);
CREATE INDEX signals_occurred_idx  ON signals (occurred_at DESC);
CREATE INDEX signals_venture_idx   ON signals (venture, occurred_at DESC);
CREATE INDEX signals_container_idx ON signals (container, occurred_at DESC);

-- Durable facts, decisions, preferences, and quiz answers (work_log).
CREATE TABLE context_entries (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type               TEXT NOT NULL,            -- 'work_log'|'fact'|'preference'|'decision'|'correction'|'conversation_note'
    content            TEXT NOT NULL,
    importance         SMALLINT NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    tags               TEXT[] NOT NULL DEFAULT '{}',
    venture            TEXT REFERENCES ventures(code),
    work_type          TEXT REFERENCES work_types(code),
    project            TEXT,                     -- free text: 'iblu','bt','email-writer','accounting-bot','machina',...
    source             TEXT NOT NULL DEFAULT 'claude',   -- 'claude'|'ping'|'chat_reply'|'analyst'
    source_ref         TEXT,                     -- 'ping:<id>:<qid>' / chat message name / ...
    occurred_at        TIMESTAMPTZ,              -- when the thing happened (work_log); NULL for timeless facts
    meta               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ,
    superseded_by      UUID REFERENCES context_entries(id),
    last_referenced_at TIMESTAMPTZ
);
CREATE INDEX ce_type_idx     ON context_entries (type, created_at DESC);
CREATE INDEX ce_occurred_idx ON context_entries (occurred_at DESC);
CREATE INDEX ce_tags_idx     ON context_entries USING GIN (tags);
CREATE INDEX ce_fts_idx      ON context_entries USING GIN (to_tsvector('simple', content));

-- Singleton. Only the (future) compactor writes here.
CREATE TABLE context_brief (
    id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    content          TEXT NOT NULL DEFAULT '',
    generated_at     TIMESTAMPTZ,
    source_entry_ids UUID[] NOT NULL DEFAULT '{}'
);
INSERT INTO context_brief (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE pings (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('midday','evening','test')),
    local_date       DATE NOT NULL,
    window_start     TIMESTAMPTZ NOT NULL,
    window_end       TIMESTAMPTZ NOT NULL,
    covers_from      TIMESTAMPTZ NOT NULL,       -- the period the questions are about
    covers_to        TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','answered','expired','failed')),
    sent_at          TIMESTAMPTZ,
    chat_message_ref TEXT,                       -- webhook response .name
    chat_thread_ref  TEXT,                       -- webhook response .thread.name
    questions        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- snapshot: [{qid,text,options:[{key,label,payload}]}]
    composer         TEXT CHECK (composer IN ('llm','fallback')),
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX pings_one_per_day ON pings (kind, local_date) WHERE kind IN ('midday','evening');

-- Calendar diff baseline.
CREATE TABLE calendar_seen (
    calendar_id  TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    updated      TIMESTAMPTZ,
    fingerprint  TEXT NOT NULL,                  -- sha256(start,end,summary,status,self_response,sorted attendee emails)
    payload      JSONB NOT NULL,                 -- compact copy of those fields
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (calendar_id, event_id)
);

CREATE TABLE collector_state (
    name        TEXT PRIMARY KEY,                -- 'gmail_sent'|'chat_sent'|'calendar_changes'|'secretary_replies'
    watermark   TIMESTAMPTZ,
    cursor      TEXT,
    last_run_at TIMESTAMPTZ,
    last_error  TEXT
);

INSERT INTO schema_migrations (version) VALUES ('001_phase2_recording');
