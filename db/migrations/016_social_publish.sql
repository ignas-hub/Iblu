-- 016_social_publish — previews, publications and schedules for social_publish.
--
-- Spec (2026-11-07) asked for SQLite. IBLU's state already lives in this
-- Postgres, which is backed up nightly (iblu-backup.timer) and migrated by one
-- runner; a second database file would have neither, and the obvious place for
-- it (data/) is off-limits to sessions. Same tables, one store.
--
-- The safety rules live in these rows:
--   * a preview freezes the exact bytes he saw: `slides[].jpg_sha256` for the
--     JPG that is hosted publicly, `slides[].source_sha256` for the artboard it
--     came from. publish refuses if either has moved.
--   * one publication row per (preview, platform) attempt. A second attempt to
--     a platform that already has a `published`/`publishing` row is refused
--     unless force=true, and that row records forced=true.
--   * nothing is deleted. A cancelled schedule keeps its row.

CREATE TABLE IF NOT EXISTS social_previews (
    preview_id       TEXT PRIMARY KEY,
    post_id          TEXT NOT NULL,
    caption          TEXT NOT NULL,
    slides           JSONB NOT NULL,
    hashtag_count    INTEGER NOT NULL,
    char_count       INTEGER NOT NULL,
    warnings         JSONB NOT NULL DEFAULT '[]'::jsonb,
    media_token      TEXT NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    media_deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS social_previews_post ON social_previews (post_id, created_at DESC);

CREATE TABLE IF NOT EXISTS social_schedules (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    preview_id   TEXT NOT NULL REFERENCES social_previews (preview_id),
    platforms    TEXT[] NOT NULL,
    publish_at   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'publishing', 'done', 'failed', 'cancelled')),
    dry_run      BOOLEAN NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    result       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS social_schedules_due ON social_schedules (publish_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS social_publications (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    preview_id   TEXT NOT NULL REFERENCES social_previews (preview_id),
    platform     TEXT NOT NULL CHECK (platform IN ('instagram', 'facebook')),
    status       TEXT NOT NULL
                 CHECK (status IN ('publishing', 'published', 'failed', 'dry_run')),
    dry_run      BOOLEAN NOT NULL,
    forced       BOOLEAN NOT NULL DEFAULT false,
    schedule_id  BIGINT REFERENCES social_schedules (id),
    remote_id    TEXT,
    permalink    TEXT,
    error        TEXT,
    calls        JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS social_publications_preview ON social_publications (preview_id, platform);

COMMENT ON COLUMN social_publications.calls IS
    'Every Graph API call made (or, in dry-run, that would have been made): '
    '[{method, path, params, dry_run, status_code?, response?}]. The access '
    'token is never stored here.';
