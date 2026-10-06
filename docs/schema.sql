-- =============================================================================
-- Guru — PostgreSQL schema DRAFT v0.1 (design artifact, όχι migration)
-- Target: PostgreSQL 16+, pgvector >= 0.8, pg_trgm
-- Κανόνες:
--   * text + CHECK αντί για ENUM (ευκολότερα migrations)
--   * bigint identity PKs· Discord snowflakes = bigint
--   * Derived πεδία των claims γράφονται ΜΟΝΟ από knowledge.recompute()
--   * Revisions & audit = append-only
--   * search_text = normalized από την εφαρμογή (ίδια συνάρτηση για docs & queries)
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- FTS config χωρίς stemming για mixed/άγνωστες γλώσσες (η normalization γίνεται στην εφαρμογή).
-- Per-language configs (english, greek) χρησιμοποιούνται όπου υπάρχουν — verify στο target PG.
CREATE TEXT SEARCH CONFIGURATION guru_simple (COPY = simple);

-- -----------------------------------------------------------------------------
-- Actors (ενιαίο μοντέλο για provenance & audit)
-- -----------------------------------------------------------------------------
CREATE TABLE actors (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind             text NOT NULL CHECK (kind IN ('discord_user','system','cli','api')),
  discord_user_id  bigint UNIQUE,
  label            text NOT NULL,                 -- π.χ. 'worker:extract', όχι προσωπικά δεδομένα
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (kind <> 'discord_user' OR discord_user_id IS NOT NULL)
);

-- -----------------------------------------------------------------------------
-- Tenancy & config
-- -----------------------------------------------------------------------------
CREATE TABLE guilds (
  guild_id            bigint PRIMARY KEY,
  name                text NOT NULL,
  default_profile_id  bigint,                     -- FK μετά τα profiles
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE profiles (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug              text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{1,40}$'),
  name              text NOT NULL,
  guild_id          bigint NOT NULL REFERENCES guilds,
  status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
  active_config_id  bigint,                       -- FK μετά τα config versions
  knowledge_epoch   bigint NOT NULL DEFAULT 0,    -- ++ σε κάθε αλλαγή γνώσης → invalidates answer cache
  created_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE guilds ADD FOREIGN KEY (default_profile_id) REFERENCES profiles;

CREATE TABLE profile_config_versions (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id      bigint NOT NULL REFERENCES profiles,
  version         int NOT NULL,
  schema_version  int NOT NULL,
  config          jsonb NOT NULL,                 -- πλήρες validated snapshot
  config_hash     bytea NOT NULL,
  created_by      bigint NOT NULL REFERENCES actors,
  comment         text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id, version)
);
ALTER TABLE profiles ADD FOREIGN KEY (active_config_id) REFERENCES profile_config_versions;

-- -----------------------------------------------------------------------------
-- Materialized config (rebuilt transactionally on apply)
-- -----------------------------------------------------------------------------
CREATE TABLE categories (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  key          text NOT NULL,
  parent_id    bigint REFERENCES categories,
  name         text NOT NULL,
  description  text,
  settings     jsonb NOT NULL DEFAULT '{}',       -- half_life_days, volatile_on_version, faq_eligible, ...
  enabled      boolean NOT NULL DEFAULT true,
  UNIQUE (profile_id, key)
);

-- Applicability dimensions timeline (π.χ. game_version, region)
CREATE TABLE dimension_values (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id  bigint NOT NULL REFERENCES profiles,
  dimension   text NOT NULL,
  value       text NOT NULL,
  ordinal     int,                                -- σειρά για ordered dims (versions)
  valid_from  timestamptz,                        -- release date (ανά scope)
  scope       jsonb NOT NULL DEFAULT '{}',        -- π.χ. {"region":"KR"}
  UNIQUE (profile_id, dimension, value, scope)
);

CREATE TABLE entity_types (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id        bigint NOT NULL REFERENCES profiles,
  key               text NOT NULL,
  name              text NOT NULL,
  default_category_id bigint REFERENCES categories,
  attribute_schema  jsonb NOT NULL DEFAULT '{}',  -- { attr_key: {json_schema, label, answer_template} }
  UNIQUE (profile_id, key)
);

CREATE TABLE intents (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id  bigint NOT NULL REFERENCES profiles,
  key         text NOT NULL,
  description text,
  target      jsonb NOT NULL,                     -- {"category":"bosses","attribute":"respawn","answer_mode":"structured"}
  patterns    text[] NOT NULL DEFAULT '{}',       -- RE2-compatible, validated on apply
  examples    text[] NOT NULL DEFAULT '{}',       -- → embedding prototypes
  UNIQUE (profile_id, key)
);

CREATE TABLE channel_bindings (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id          bigint NOT NULL REFERENCES profiles,
  guild_id            bigint NOT NULL,
  channel_id          bigint NOT NULL,
  role                text NOT NULL CHECK (role IN ('watch','ask','faq_publish','faq_review','admin_log')),
  default_category_id bigint REFERENCES categories,
  audience            text NOT NULL DEFAULT 'public' CHECK (audience IN ('public','restricted')),
  settings            jsonb NOT NULL DEFAULT '{}', -- ingest mode, min_author_tier, include_threads, ...
  enabled             boolean NOT NULL DEFAULT true,
  UNIQUE (profile_id, channel_id, role)
);
CREATE UNIQUE INDEX channel_bindings_one_ask_profile
  ON channel_bindings (channel_id) WHERE role = 'ask' AND enabled;

CREATE TABLE sources (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id           bigint NOT NULL REFERENCES profiles,
  key                  text NOT NULL,
  kind                 text NOT NULL CHECK (kind IN ('discord_channel','web_page','web_feed','web_sitemap','web_search','manual','import')),
  name                 text NOT NULL,
  locator              text,                      -- URL / channel id / query
  domain               text,                      -- registrable domain (Public Suffix List)
  independence_group   text NOT NULL,             -- ίδιο group = όχι ανεξάρτητες πηγές
  trust_tier           smallint NOT NULL CHECK (trust_tier BETWEEN 0 AND 4),
  origin               text NOT NULL DEFAULT 'config' CHECK (origin IN ('config','dynamic')),
  fetch_config         jsonb NOT NULL DEFAULT '{}',
  schedule_seconds     int,
  enabled              boolean NOT NULL DEFAULT true,
  -- crawl state
  etag                 text,
  last_modified        text,
  last_fetched_at      timestamptz,
  next_fetch_at        timestamptz,
  consecutive_failures int NOT NULL DEFAULT 0,
  circuit_open_until   timestamptz,
  UNIQUE (profile_id, key)
);
CREATE INDEX sources_due ON sources (next_fetch_at) WHERE enabled;

CREATE TABLE permission_grants (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  subject_type text NOT NULL CHECK (subject_type IN ('role','user')),
  subject_id   bigint NOT NULL,
  capability   text NOT NULL,
  expires_at   timestamptz,
  UNIQUE (profile_id, subject_type, subject_id, capability)
);

CREATE TABLE trust_assignments (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  subject_type text NOT NULL CHECK (subject_type IN ('role','user','webhook')),
  subject_id   bigint NOT NULL,
  trust_tier   smallint NOT NULL CHECK (trust_tier BETWEEN 0 AND 4),
  UNIQUE (profile_id, subject_type, subject_id)
);

-- -----------------------------------------------------------------------------
-- Reference data (όχι μέρος του config snapshot· audited)
-- -----------------------------------------------------------------------------
CREATE TABLE entities (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id      bigint NOT NULL REFERENCES profiles,
  entity_type_id  bigint NOT NULL REFERENCES entity_types,
  canonical_name  text NOT NULL,
  category_id     bigint REFERENCES categories,
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','retired')),
  merged_into     bigint REFERENCES entities,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id, entity_type_id, canonical_name)
);

CREATE TABLE aliases (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  target_type  text NOT NULL CHECK (target_type IN ('entity','category','intent','profile')),
  target_id    bigint,                            -- NULL για target_type='profile' (relevance keywords)
  alias        text NOT NULL,
  alias_norm   text NOT NULL,                     -- normalized (casefold, accents, whitespace)
  lang         text,
  match        text NOT NULL DEFAULT 'token' CHECK (match IN ('token','phrase')),
  weight       real NOT NULL DEFAULT 1.0,
  origin       text NOT NULL CHECK (origin IN ('config','import','admin','learned')),
  UNIQUE NULLS NOT DISTINCT (profile_id, target_type, target_id, alias_norm)
);
CREATE INDEX aliases_trgm ON aliases USING gin (alias_norm gin_trgm_ops);

-- -----------------------------------------------------------------------------
-- Raw layer (L0)
-- -----------------------------------------------------------------------------
CREATE TABLE observations (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id          bigint NOT NULL REFERENCES profiles,
  source_id           bigint NOT NULL REFERENCES sources,
  kind                text NOT NULL CHECK (kind IN ('discord_message','web_document','manual','import')),
  external_id         text NOT NULL,              -- discord message id | canonical URL | import key
  capture_mode        text NOT NULL CHECK (capture_mode IN ('passive','explicit','context','backfill','crawl','search','manual')),
  -- Discord provenance
  guild_id            bigint,
  channel_id          bigint,
  thread_id           bigint,
  message_id          bigint,
  reply_to_message_id bigint,
  author_actor_id     bigint REFERENCES actors,
  author_trust_tier   smallint,                   -- snapshot τη στιγμή του capture
  -- Web provenance
  url                 text,
  canonical_url       text,
  title               text,
  site_name           text,
  syndicated_of       bigint REFERENCES observations, -- near-duplicate του αρχικού
  -- Content
  content             text,                       -- NULL μετά από purge
  content_hash        bytea,
  lang                text,
  current_rev         int NOT NULL DEFAULT 1,
  -- Time
  published_at        timestamptz,                -- Discord created_at | page published
  source_updated_at   timestamptz,                -- Discord edited_at | page modified
  retrieved_at        timestamptz NOT NULL DEFAULT now(),
  last_confirmed_at   timestamptz,                -- web: τελευταίος έλεγχος χωρίς αλλαγή
  date_confidence     text CHECK (date_confidence IN ('exact','metadata','heuristic','unknown')),
  -- State
  status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted','purged','unreachable')),
  processing_state    text NOT NULL DEFAULT 'pending'
                      CHECK (processing_state IN ('pending','context_only','queued','extracted','irrelevant','failed')),
  audience_channel_id bigint,                     -- NULL = public· αλλιώς ορατό μόνο σε όσους βλέπουν το channel
  meta                jsonb NOT NULL DEFAULT '{}',-- prefilter score/reasons, http headers, ...
  expires_at          timestamptz,                -- retention
  UNIQUE (source_id, external_id)
);
CREATE INDEX observations_state   ON observations (profile_id, processing_state);
CREATE INDEX observations_message ON observations (message_id) WHERE message_id IS NOT NULL;
CREATE INDEX observations_author  ON observations (author_actor_id);
CREATE INDEX observations_expiry  ON observations (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE observation_revisions (
  observation_id  bigint NOT NULL REFERENCES observations ON DELETE CASCADE,
  rev             int NOT NULL,
  content         text,                           -- NULL μετά από purge
  content_hash    bytea NOT NULL,
  observed_at     timestamptz NOT NULL DEFAULT now(),
  reason          text NOT NULL CHECK (reason IN ('initial','discord_edit','web_change','purge')),
  PRIMARY KEY (observation_id, rev)
);

CREATE TABLE chunks (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  observation_id  bigint NOT NULL REFERENCES observations ON DELETE CASCADE,
  rev             int NOT NULL,
  ordinal         int NOT NULL,
  heading_path    text,
  text            text NOT NULL,
  search_text     text NOT NULL,
  token_count     int NOT NULL,
  lang            text,
  active          boolean NOT NULL DEFAULT true,  -- false όταν υπάρχει νεότερο rev
  ts_config       regconfig NOT NULL DEFAULT 'guru_simple',
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, search_text)) STORED,
  UNIQUE (observation_id, rev, ordinal)
);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv) WHERE active;

-- -----------------------------------------------------------------------------
-- Knowledge layer
-- -----------------------------------------------------------------------------
CREATE TABLE claims (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id           bigint NOT NULL REFERENCES profiles,
  category_id          bigint REFERENCES categories,
  kind                 text NOT NULL CHECK (kind IN ('structured','statement')),
  claim_type           text NOT NULL DEFAULT 'fact' CHECK (claim_type IN ('fact','tip','procedure')),
  -- structured
  entity_id            bigint REFERENCES entities,
  attribute_key        text,
  value                jsonb,
  slot_key             bytea,                     -- H(entity_id, attribute_key)
  value_hash           bytea,
  -- content (και για structured: rendered statement)
  statement            text NOT NULL,
  search_text          text NOT NULL,
  lang                 text NOT NULL,
  -- context
  applicability        jsonb NOT NULL DEFAULT '{}', -- {"game_version":{"min":"1.2","max":null},"region":["KR"]}
  applicability_hash   bytea NOT NULL,
  -- lifecycle (rules/humans)
  lifecycle            text NOT NULL DEFAULT 'active'
                       CHECK (lifecycle IN ('active','superseded','obsolete','retracted','merged','rejected')),
  lifecycle_reason     text,
  merged_into          bigint REFERENCES claims,
  needs_review         boolean NOT NULL DEFAULT false,
  -- verification (DERIVED — μόνο από recompute, εκτός από human verification fields)
  verification         text NOT NULL DEFAULT 'unverified'
                       CHECK (verification IN ('unverified','corroborated','verified','disputed')),
  verification_basis   text CHECK (verification_basis IN ('human','official_source','corroboration')),
  human_verified_by    bigint REFERENCES actors,
  human_verified_at    timestamptz,
  evidence_summary     jsonb NOT NULL DEFAULT '{}',
  support_score        real NOT NULL DEFAULT 0,   -- μόνο για ranking
  origins              text[] NOT NULL DEFAULT '{}', -- {'discord','web','manual','import'} από active evidence
  is_public            boolean NOT NULL DEFAULT false,
  audience_channel_ids bigint[] NOT NULL DEFAULT '{}',
  last_evidence_at     timestamptz,
  -- bookkeeping
  rev                  int NOT NULL DEFAULT 1,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  ts_config            regconfig NOT NULL DEFAULT 'guru_simple',
  tsv                  tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, search_text)) STORED,
  CHECK (kind <> 'structured' OR (entity_id IS NOT NULL AND attribute_key IS NOT NULL
                                  AND value IS NOT NULL AND slot_key IS NOT NULL AND value_hash IS NOT NULL)),
  CHECK (lifecycle <> 'merged' OR merged_into IS NOT NULL),
  CHECK (verification <> 'verified' OR verification_basis IS NOT NULL)
);
CREATE UNIQUE INDEX claims_structured_dedupe
  ON claims (profile_id, slot_key, applicability_hash, value_hash)
  WHERE kind = 'structured' AND lifecycle = 'active';
CREATE INDEX claims_route   ON claims (profile_id, category_id, lifecycle, verification);
CREATE INDEX claims_slot    ON claims (profile_id, slot_key) WHERE kind = 'structured';
CREATE INDEX claims_entity  ON claims (entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX claims_tsv     ON claims USING gin (tsv) WHERE lifecycle IN ('active','superseded');
CREATE INDEX claims_review  ON claims (profile_id) WHERE needs_review;

CREATE TABLE claim_revisions (
  claim_id       bigint NOT NULL REFERENCES claims,
  rev            int NOT NULL,
  statement      text NOT NULL,
  value          jsonb,
  applicability  jsonb NOT NULL,
  category_id    bigint,
  change_type    text NOT NULL CHECK (change_type IN ('create','rephrase','recategorize','reapply')),
  reason         text,
  actor_id       bigint NOT NULL REFERENCES actors,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (claim_id, rev)
);

CREATE TABLE claim_evidence (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  claim_id            bigint NOT NULL REFERENCES claims,
  observation_id      bigint NOT NULL REFERENCES observations,
  observation_rev     int NOT NULL,
  chunk_id            bigint REFERENCES chunks,
  stance              text NOT NULL CHECK (stance IN ('supports','contradicts')),
  quote               text,                       -- verbatim span· NULL μετά από purge
  quote_verified      boolean NOT NULL,
  extractor           text NOT NULL,              -- 'human' | 'llm:<model>@<prompt_ver>' | 'rule:<id>' | 'import'
  -- snapshots τη στιγμή του link (η ιστορία δεν ξαναγράφεται)
  trust_tier          smallint NOT NULL,
  independence_group  text NOT NULL,
  origin              text NOT NULL CHECK (origin IN ('discord','web','manual','import')),
  evidence_at         timestamptz NOT NULL,       -- published/updated time της πηγής
  version_inferred    text,
  -- state
  active              boolean NOT NULL DEFAULT true,
  deactivated_reason  text CHECK (deactivated_reason IN ('source_edited','source_deleted','source_changed','unlinked','purged','merged')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (claim_id, observation_id, observation_rev, stance)
);
CREATE INDEX claim_evidence_claim ON claim_evidence (claim_id) WHERE active;
CREATE INDEX claim_evidence_obs   ON claim_evidence (observation_id);

CREATE TABLE claim_relations (
  from_claim  bigint NOT NULL REFERENCES claims,
  to_claim    bigint NOT NULL REFERENCES claims,
  type        text NOT NULL CHECK (type IN ('supersedes','corrects','duplicate_of','contradicts','refines','related')),
  origin      text NOT NULL CHECK (origin IN ('rule','llm','human')),
  created_by  bigint REFERENCES actors,
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (from_claim, to_claim, type),
  CHECK (from_claim <> to_claim)
);
CREATE INDEX claim_relations_to ON claim_relations (to_claim);

CREATE TABLE conflicts (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  kind         text NOT NULL CHECK (kind IN ('structured_value','statement')),
  slot_key     bytea,
  status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open','auto_resolved','resolved','acknowledged')),
  resolution   jsonb,                             -- {winner_claim_id, rule|actor, reason}
  opened_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at  timestamptz,
  resolved_by  bigint REFERENCES actors
);
CREATE INDEX conflicts_open ON conflicts (profile_id) WHERE status = 'open';

CREATE TABLE conflict_members (
  conflict_id  bigint NOT NULL REFERENCES conflicts ON DELETE CASCADE,
  claim_id     bigint NOT NULL REFERENCES claims,
  PRIMARY KEY (conflict_id, claim_id)
);

-- -----------------------------------------------------------------------------
-- Embeddings (polymorphic, model-versioned)
-- -----------------------------------------------------------------------------
CREATE TABLE embedding_models (
  id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name    text NOT NULL UNIQUE,                   -- π.χ. 'intfloat/multilingual-e5-small@onnx'
  dims    int NOT NULL,
  status  text NOT NULL CHECK (status IN ('active','backfilling','retired'))
);

CREATE TABLE embeddings (
  owner_type    text NOT NULL CHECK (owner_type IN ('claim','chunk','faq','category_proto','intent_proto','profile_proto')),
  owner_id      bigint NOT NULL,
  ordinal       smallint NOT NULL DEFAULT 0,      -- πολλαπλά examples ανά prototype
  model_id      smallint NOT NULL REFERENCES embedding_models,
  profile_id    bigint NOT NULL,
  content_hash  bytea NOT NULL,                   -- re-embed μόνο αν άλλαξε το κείμενο
  embedding     vector NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_type, owner_id, ordinal, model_id)
);
-- Ένα partial HNSW index ανά (model, owner_type). Παράδειγμα για model_id=1 με 384 dims:
--   CREATE INDEX emb_claim_m1 ON embeddings
--     USING hnsw ((embedding::vector(384)) vector_cosine_ops)
--     WHERE model_id = 1 AND owner_type = 'claim';
-- Το query πρέπει να χρησιμοποιεί ΙΔΙΟ expression & WHERE:
--   ORDER BY embedding::vector(384) <=> $1 ... WHERE model_id = 1 AND owner_type = 'claim'
-- Σε μικρό scale (< ~50k/profile) αρκεί exact scan με filters.

-- -----------------------------------------------------------------------------
-- FAQ (projection των claims)
-- -----------------------------------------------------------------------------
CREATE TABLE faq_entries (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id     bigint NOT NULL REFERENCES profiles,
  category_id    bigint REFERENCES categories,
  slug           text NOT NULL,
  question       text NOT NULL,
  answer         text NOT NULL,
  search_text    text NOT NULL,
  lang           text NOT NULL,
  status         text NOT NULL CHECK (status IN ('draft','pending_approval','approved','published',
                                                 'needs_review','deprecated','superseded','rejected')),
  rev            int NOT NULL DEFAULT 1,
  superseded_by  bigint REFERENCES faq_entries,
  origin         text NOT NULL CHECK (origin IN ('verified_claim','popular_query','admin')),
  created_by     bigint REFERENCES actors,
  approved_by    bigint REFERENCES actors,
  approved_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  ts_config      regconfig NOT NULL DEFAULT 'guru_simple',
  tsv            tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, search_text)) STORED,
  UNIQUE (profile_id, slug)
);
CREATE INDEX faq_tsv ON faq_entries USING gin (tsv) WHERE status = 'published';
CREATE INDEX faq_question_trgm ON faq_entries USING gin (question gin_trgm_ops);

CREATE TABLE faq_revisions (
  faq_id          bigint NOT NULL REFERENCES faq_entries,
  rev             int NOT NULL,
  question        text NOT NULL,
  answer          text NOT NULL,
  claim_ids       bigint[] NOT NULL,
  change_summary  text,
  actor_id        bigint NOT NULL REFERENCES actors,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (faq_id, rev)
);

CREATE TABLE faq_claims (
  faq_id    bigint NOT NULL REFERENCES faq_entries,
  claim_id  bigint NOT NULL REFERENCES claims,
  role      text NOT NULL CHECK (role IN ('primary','supporting')),
  PRIMARY KEY (faq_id, claim_id)
);
CREATE INDEX faq_claims_claim ON faq_claims (claim_id);

-- Desired vs published state για τον reconciler
CREATE TABLE faq_publications (
  faq_id           bigint PRIMARY KEY REFERENCES faq_entries,
  channel_id       bigint NOT NULL,
  thread_id        bigint,
  message_id       bigint,
  desired_state    text NOT NULL CHECK (desired_state IN ('published','deprecated','removed')),
  desired_rev      int NOT NULL,
  published_state  text CHECK (published_state IN ('published','deprecated','removed')),
  published_rev    int,
  content_hash     bytea,                         -- τι στάλθηκε τελευταία φορά
  sync_status      text NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending','in_sync','error')),
  last_synced_at   timestamptz,
  last_error       text,
  attempts         int NOT NULL DEFAULT 0
);
CREATE INDEX faq_publications_dirty ON faq_publications (faq_id) WHERE sync_status <> 'in_sync';

-- -----------------------------------------------------------------------------
-- Workflow
-- -----------------------------------------------------------------------------
CREATE TABLE review_tasks (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id          bigint NOT NULL REFERENCES profiles,
  kind                text NOT NULL CHECK (kind IN ('faq_approval','conflict','claim_report','claim_review','source_review')),
  target_type         text NOT NULL,
  target_id           bigint NOT NULL,
  status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dismissed')),
  priority            smallint NOT NULL DEFAULT 100,
  discord_message_id  bigint,                     -- review post
  payload             jsonb NOT NULL DEFAULT '{}',
  created_at          timestamptz NOT NULL DEFAULT now(),
  closed_by           bigint REFERENCES actors,
  closed_at           timestamptz,
  outcome             jsonb
);
CREATE UNIQUE INDEX review_tasks_one_open ON review_tasks (kind, target_type, target_id) WHERE status = 'open';

CREATE TABLE jobs (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind             text NOT NULL,
  payload          jsonb NOT NULL,
  priority         smallint NOT NULL DEFAULT 100, -- μικρότερο = νωρίτερα
  status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','dead')),
  idempotency_key  text UNIQUE,
  attempts         int NOT NULL DEFAULT 0,
  max_attempts     int NOT NULL DEFAULT 5,
  run_after        timestamptz NOT NULL DEFAULT now(),
  locked_by        text,
  locked_until     timestamptz,                   -- lease· expired = reclaimable
  last_error       text,
  correlation_id   uuid,
  created_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz
);
CREATE INDEX jobs_ready   ON jobs (priority, run_after) WHERE status = 'queued';
CREATE INDEX jobs_leases  ON jobs (locked_until) WHERE status = 'running';
-- Dequeue:
--   UPDATE jobs SET status='running', locked_by=$1, locked_until=now()+$2, attempts=attempts+1
--   WHERE id = (SELECT id FROM jobs
--               WHERE status='queued' AND run_after<=now() AND kind = ANY($3)
--               ORDER BY priority, run_after
--               FOR UPDATE SKIP LOCKED LIMIT 1)
--   RETURNING *;

CREATE TABLE schedules (
  key               text PRIMARY KEY,
  kind              text NOT NULL,
  payload           jsonb NOT NULL DEFAULT '{}',
  interval_seconds  int NOT NULL,
  next_run_at       timestamptz NOT NULL,
  enabled           boolean NOT NULL DEFAULT true
);

CREATE TABLE channel_cursors (
  channel_id        bigint PRIMARY KEY,
  last_message_id   bigint,
  last_backfill_at  timestamptz
);

-- -----------------------------------------------------------------------------
-- Query path
-- -----------------------------------------------------------------------------
CREATE TABLE query_log (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id             bigint REFERENCES profiles,
  guild_id               bigint,
  channel_id             bigint,
  user_hash              bytea,                   -- salted hash
  bot_message_id         bigint,                  -- για follow-ups & feedback
  query_text             text,                    -- retention-bound
  query_norm_hash        bytea,
  lang                   text,
  scope                  text,
  category_ids           bigint[],
  intent_key             text,
  entity_ids             bigint[],
  version_context        jsonb,
  relevance              real,
  route                  text[],
  answered_by            text CHECK (answered_by IN ('cache','faq','structured','extractive','llm','no_answer','rejected','error')),
  claim_ids              bigint[],
  faq_id                 bigint,
  stage_ms               jsonb,
  llm_prompt_tokens      int,
  llm_completion_tokens  int,
  feedback               smallint CHECK (feedback IN (-1, 1)),
  created_at             timestamptz NOT NULL DEFAULT now(),
  expires_at             timestamptz
);
CREATE INDEX query_log_bot_msg ON query_log (bot_message_id) WHERE bot_message_id IS NOT NULL;
CREATE INDEX query_log_claims  ON query_log USING gin (claim_ids);

CREATE TABLE answer_cache (
  cache_key        bytea PRIMARY KEY,             -- H(profile, norm_query, scope, categories, version_ctx, visibility_class, lang)
  profile_id       bigint NOT NULL,
  knowledge_epoch  bigint NOT NULL,               -- mismatch με profiles.knowledge_epoch = stale
  config_version   int NOT NULL,
  response         jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_hit_at      timestamptz,
  hits             int NOT NULL DEFAULT 0
);

-- -----------------------------------------------------------------------------
-- Audit (append-only, hash chain)
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor_id        bigint NOT NULL REFERENCES actors,
  action          text NOT NULL,
  profile_id      bigint,
  target_type     text,
  target_id       text,
  before          jsonb,
  after           jsonb,
  reason          text,
  correlation_id  uuid,
  prev_hash       bytea,
  row_hash        bytea NOT NULL                  -- H(prev_hash || canonical(row)), trigger + advisory lock
);
CREATE INDEX audit_target ON audit_log (target_type, target_id);
CREATE INDEX audit_actor  ON audit_log (actor_id, ts);

-- Least privilege (ενδεικτικά):
--   guru_app: SELECT/INSERT/UPDATE/DELETE σε όλα ΕΚΤΟΣ audit_log (μόνο SELECT, INSERT)
--   REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM guru_app;
--   guru_ro: SELECT (για reports/eval)
