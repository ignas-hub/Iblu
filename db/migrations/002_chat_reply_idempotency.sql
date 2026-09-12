-- 002_chat_reply_idempotency
--
-- Free-text ping replies are read with a 5-minute watermark overlap (so a
-- message arriving mid-run is never missed), which means the same Chat message
-- is seen on consecutive ticks. `signals` survives that because of
-- UNIQUE (source, source_ref); `context_entries` had no equivalent, so replies
-- were inserted once per tick.
--
-- The index must stay narrow: taps deliberately write several rows with the
-- same source_ref ('ping:<id>:<qid>') to form the supersede chain (D9), so a
-- unique constraint across all sources would break corrections.

DELETE FROM context_entries a
USING context_entries b
WHERE a.source = 'chat_reply'
  AND b.source = 'chat_reply'
  AND a.source_ref = b.source_ref
  AND a.ctid > b.ctid;

CREATE UNIQUE INDEX IF NOT EXISTS ce_chat_reply_unique
    ON context_entries (source_ref)
    WHERE source = 'chat_reply';

INSERT INTO schema_migrations (version) VALUES ('002_chat_reply_idempotency');
