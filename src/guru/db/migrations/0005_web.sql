-- 0005: web ingestion bookkeeping (near-duplicate fingerprints, source reputation)

ALTER TABLE observations ADD COLUMN simhash bigint;
CREATE INDEX observations_web ON observations (profile_id, retrieved_at DESC) WHERE kind = 'web_document';
CREATE INDEX observations_url ON observations (profile_id, canonical_url) WHERE canonical_url IS NOT NULL;

ALTER TABLE sources ADD COLUMN reputation jsonb NOT NULL DEFAULT '{}';
