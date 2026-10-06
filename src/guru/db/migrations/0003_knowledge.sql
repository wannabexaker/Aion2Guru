-- 0003: raw layer, knowledge layer, embeddings, review workflow, query path, usage limits

-- ---------------------------------------------------------------- raw layer (L0)
CREATE TABLE observations (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id          bigint NOT NULL REFERENCES profiles,
  source_id           bigint NOT NULL REFERENCES sources,
  kind                text NOT NULL CHECK (kind IN ('discord_message','web_document','manual','import')),
  external_id         text NOT NULL,
  capture_mode        text NOT NULL CHECK (capture_mode IN ('passive','explicit','context','backfill','crawl','search','manual')),
  guild_id            bigint,
  channel_id          bigint,
  thread_id           bigint,
  message_id          bigint,
  reply_to_message_id bigint,
  author_actor_id     bigint REFERENCES actors,
  author_trust_tier   smallint,
  url                 text,
  canonical_url       text,
  title               text,
  site_name           text,
  syndicated_of       bigint REFERENCES observations,
  content             text,
  content_hash        bytea,
  lang                text,
  current_rev         int NOT NULL DEFAULT 1,
  published_at        timestamptz,
  source_updated_at   timestamptz,
  retrieved_at        timestamptz NOT NULL DEFAULT now(),
  last_confirmed_at   timestamptz,
  date_confidence     text CHECK (date_confidence IN ('exact','metadata','heuristic','unknown')),
  status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted','purged','unreachable')),
  processing_state    text NOT NULL DEFAULT 'pending'
                      CHECK (processing_state IN ('pending','context_only','queued','extracted','irrelevant','failed')),
  audience_channel_id bigint,
  meta                jsonb NOT NULL DEFAULT '{}',
  expires_at          timestamptz,
  UNIQUE (source_id, external_id)
);
CREATE INDEX observations_state   ON observations (profile_id, processing_state);
CREATE INDEX observations_message ON observations (message_id) WHERE message_id IS NOT NULL;
CREATE INDEX observations_author  ON observations (author_actor_id);
CREATE INDEX observations_expiry  ON observations (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE observation_revisions (
  observation_id  bigint NOT NULL REFERENCES observations ON DELETE CASCADE,
  rev             int NOT NULL,
  content         text,
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
  active          boolean NOT NULL DEFAULT true,
  ts_config       regconfig NOT NULL DEFAULT 'guru_simple',
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, search_text)) STORED,
  UNIQUE (observation_id, rev, ordinal)
);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv) WHERE active;

-- ---------------------------------------------------------------- knowledge layer
CREATE TABLE claims (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id           bigint NOT NULL REFERENCES profiles,
  category_id          bigint REFERENCES categories,
  kind                 text NOT NULL CHECK (kind IN ('structured','statement')),
  claim_type           text NOT NULL DEFAULT 'fact' CHECK (claim_type IN ('fact','tip','procedure')),
  entity_id            bigint REFERENCES entities,
  attribute_key        text,
  value                jsonb,
  slot_key             bytea,
  value_hash           bytea,
  statement            text NOT NULL,
  search_text          text NOT NULL,
  lang                 text NOT NULL,
  applicability        jsonb NOT NULL DEFAULT '{}',
  applicability_hash   bytea NOT NULL,
  lifecycle            text NOT NULL DEFAULT 'active'
                       CHECK (lifecycle IN ('active','superseded','obsolete','retracted','merged','rejected')),
  lifecycle_reason     text,
  merged_into          bigint REFERENCES claims,
  needs_review         boolean NOT NULL DEFAULT false,
  verification         text NOT NULL DEFAULT 'unverified'
                       CHECK (verification IN ('unverified','corroborated','verified','disputed')),
  verification_basis   text CHECK (verification_basis IN ('human','official_source','corroboration')),
  human_verified_by    bigint REFERENCES actors,
  human_verified_at    timestamptz,
  evidence_summary     jsonb NOT NULL DEFAULT '{}',
  support_score        real NOT NULL DEFAULT 0,
  origins              text[] NOT NULL DEFAULT '{}',
  is_public            boolean NOT NULL DEFAULT false,
  audience_channel_ids bigint[] NOT NULL DEFAULT '{}',
  last_evidence_at     timestamptz,
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
CREATE INDEX claims_route  ON claims (profile_id, category_id, lifecycle, verification);
CREATE INDEX claims_slot   ON claims (profile_id, slot_key) WHERE kind = 'structured';
CREATE INDEX claims_entity ON claims (entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX claims_tsv    ON claims USING gin (tsv) WHERE lifecycle IN ('active','superseded');
CREATE INDEX claims_review ON claims (profile_id) WHERE needs_review;
CREATE INDEX claims_trgm   ON claims USING gin (search_text gin_trgm_ops) WHERE lifecycle = 'active';

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
  quote               text,
  quote_verified      boolean NOT NULL,
  extractor           text NOT NULL,
  trust_tier          smallint NOT NULL CHECK (trust_tier BETWEEN 0 AND 4),
  independence_group  text NOT NULL,
  origin              text NOT NULL CHECK (origin IN ('discord','web','manual','import')),
  evidence_at         timestamptz NOT NULL,
  version_inferred    text,
  audience_channel_id bigint,                     -- NULL = public evidence
  active              boolean NOT NULL DEFAULT true,
  deactivated_reason  text CHECK (deactivated_reason IN ('source_edited','source_deleted','source_changed','unlinked','purged','merged','rejected')),
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
  resolution   jsonb,
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
CREATE INDEX conflict_members_claim ON conflict_members (claim_id);

-- ---------------------------------------------------------------- embeddings
CREATE TABLE embedding_models (
  id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name    text NOT NULL UNIQUE,
  dims    int NOT NULL CHECK (dims > 0),
  status  text NOT NULL CHECK (status IN ('active','backfilling','retired'))
);
CREATE UNIQUE INDEX embedding_models_one_active ON embedding_models ((true)) WHERE status = 'active';

CREATE TABLE embeddings (
  owner_type    text NOT NULL CHECK (owner_type IN ('claim','chunk','faq','category_proto','intent_proto','profile_proto','review_item')),
  owner_id      bigint NOT NULL,
  ordinal       smallint NOT NULL DEFAULT 0,
  model_id      smallint NOT NULL REFERENCES embedding_models,
  profile_id    bigint NOT NULL,
  content_hash  bytea NOT NULL,
  embedding     vector NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_type, owner_id, ordinal, model_id)
);
CREATE INDEX embeddings_profile ON embeddings (profile_id, owner_type, model_id);

-- ---------------------------------------------------------------- review workflow (moderators)
CREATE TABLE review_tasks (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id          bigint NOT NULL REFERENCES profiles,
  kind                text NOT NULL CHECK (kind IN ('claim_keep','answer_rating','faq_approval','conflict','claim_report','claim_review','source_review')),
  target_type         text NOT NULL,
  target_id           bigint NOT NULL,
  status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dismissed','auto')),
  priority            smallint NOT NULL DEFAULT 100,
  channel_id          bigint,
  discord_message_id  bigint,
  payload             jsonb NOT NULL DEFAULT '{}',
  created_at          timestamptz NOT NULL DEFAULT now(),
  closed_by           bigint REFERENCES actors,
  closed_at           timestamptz,
  outcome             jsonb
);
CREATE UNIQUE INDEX review_tasks_one_open ON review_tasks (kind, target_type, target_id) WHERE status = 'open';
CREATE INDEX review_tasks_message ON review_tasks (discord_message_id) WHERE discord_message_id IS NOT NULL;

CREATE TABLE review_votes (
  task_id     bigint NOT NULL REFERENCES review_tasks ON DELETE CASCADE,
  actor_id    bigint NOT NULL REFERENCES actors,
  decision    text NOT NULL,
  note        text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, actor_id)
);

-- Moderator decisions as training labels for the learned gate (D-27).
CREATE TABLE decision_labels (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id    bigint NOT NULL REFERENCES profiles,
  decision_kind text NOT NULL,                    -- 'claim_keep' | 'answer_rating' | ...
  target_type   text NOT NULL,
  target_id     bigint NOT NULL,
  label         smallint NOT NULL CHECK (label IN (0, 1)),
  features      jsonb NOT NULL DEFAULT '{}',
  source        text NOT NULL CHECK (source IN ('human','auto')),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decision_labels_kind ON decision_labels (profile_id, decision_kind, created_at);

-- ---------------------------------------------------------------- discord sync
CREATE TABLE channel_cursors (
  channel_id        bigint PRIMARY KEY,
  last_message_id   bigint,
  last_backfill_at  timestamptz
);

-- ---------------------------------------------------------------- query path
CREATE TABLE query_log (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id             bigint REFERENCES profiles,
  guild_id               bigint,
  channel_id             bigint,
  user_hash              bytea,
  request_message_id     bigint,
  bot_message_id         bigint,
  query_text             text,
  query_norm_hash        bytea,
  lang                   text,
  script                 text,
  scope                  text,
  category_ids           bigint[],
  intent_key             text,
  entity_ids             bigint[],
  version_context        jsonb,
  relevance              real,
  route                  text[],
  answered_by            text CHECK (answered_by IN ('cache','faq','structured','extractive','llm','no_answer','rejected','limited','error')),
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
CREATE INDEX query_log_expiry  ON query_log (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE answer_cache (
  cache_key        bytea PRIMARY KEY,
  profile_id       bigint NOT NULL,
  knowledge_epoch  bigint NOT NULL,
  config_version   int NOT NULL,
  response         jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_hit_at      timestamptz,
  hits             int NOT NULL DEFAULT 0
);

-- Daily/hourly usage counters for per-user rate limits (D-28).
CREATE TABLE usage_counters (
  profile_id    bigint NOT NULL,
  user_hash     bytea NOT NULL,
  bucket        text NOT NULL,                    -- 'query' | 'llm'
  window_start  timestamptz NOT NULL,
  window_kind   text NOT NULL CHECK (window_kind IN ('hour','day')),
  count         int NOT NULL DEFAULT 0,
  PRIMARY KEY (profile_id, user_hash, bucket, window_kind, window_start)
);
CREATE INDEX usage_counters_gc ON usage_counters (window_start);
