-- 0002: materialized profile config (rebuilt on apply) + reference data (entities, aliases)

CREATE TABLE categories (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  key          text NOT NULL,
  parent_id    bigint REFERENCES categories,
  name         text NOT NULL,
  description  text,
  settings     jsonb NOT NULL DEFAULT '{}',
  enabled      boolean NOT NULL DEFAULT true,
  UNIQUE (profile_id, key)
);

CREATE TABLE dimension_values (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id  bigint NOT NULL REFERENCES profiles,
  dimension   text NOT NULL,
  value       text NOT NULL,
  ordinal     int,
  valid_from  timestamptz,
  scope       jsonb NOT NULL DEFAULT '{}',
  UNIQUE (profile_id, dimension, value, scope)
);

CREATE TABLE entity_types (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id           bigint NOT NULL REFERENCES profiles,
  key                  text NOT NULL,
  name                 text NOT NULL,
  default_category_id  bigint REFERENCES categories,
  attribute_schema     jsonb NOT NULL DEFAULT '{}',
  UNIQUE (profile_id, key)
);

CREATE TABLE intents (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id  bigint NOT NULL REFERENCES profiles,
  key         text NOT NULL,
  description text,
  target      jsonb NOT NULL,
  patterns    text[] NOT NULL DEFAULT '{}',
  examples    text[] NOT NULL DEFAULT '{}',
  UNIQUE (profile_id, key)
);

CREATE TABLE channel_bindings (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id           bigint NOT NULL REFERENCES profiles,
  guild_id             bigint NOT NULL,
  channel_id           bigint NOT NULL,
  role                 text NOT NULL CHECK (role IN ('home','watch','ask','faq_publish','faq_review','mod_review','admin_log')),
  default_category_id  bigint REFERENCES categories,
  audience             text NOT NULL DEFAULT 'public' CHECK (audience IN ('public','restricted')),
  settings             jsonb NOT NULL DEFAULT '{}',
  enabled              boolean NOT NULL DEFAULT true,
  UNIQUE (profile_id, channel_id, role)
);
-- A channel answers for exactly one profile.
CREATE UNIQUE INDEX channel_bindings_one_answering_profile
  ON channel_bindings (channel_id) WHERE role IN ('home','ask') AND enabled;
CREATE INDEX channel_bindings_channel ON channel_bindings (channel_id) WHERE enabled;

CREATE TABLE sources (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id            bigint NOT NULL REFERENCES profiles,
  key                   text NOT NULL,
  kind                  text NOT NULL CHECK (kind IN ('discord_channel','web_page','web_feed','web_sitemap','web_search','manual','import')),
  name                  text NOT NULL,
  locator               text,
  domain                text,
  independence_group    text NOT NULL,
  trust_tier            smallint NOT NULL CHECK (trust_tier BETWEEN 0 AND 4),
  trust_pinned          boolean NOT NULL DEFAULT false,   -- reputation may not change it
  origin                text NOT NULL DEFAULT 'config' CHECK (origin IN ('config','dynamic','system')),
  fetch_config          jsonb NOT NULL DEFAULT '{}',
  schedule_seconds      int,
  enabled               boolean NOT NULL DEFAULT true,
  etag                  text,
  last_modified         text,
  last_fetched_at       timestamptz,
  next_fetch_at         timestamptz,
  consecutive_failures  int NOT NULL DEFAULT 0,
  circuit_open_until    timestamptz,
  UNIQUE (profile_id, key)
);
CREATE INDEX sources_due ON sources (next_fetch_at) WHERE enabled;

CREATE TABLE permission_grants (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  subject_type text NOT NULL CHECK (subject_type IN ('role','user','everyone')),
  subject_id   bigint NOT NULL,                            -- 0 for 'everyone'
  capability   text NOT NULL,
  UNIQUE (profile_id, subject_type, subject_id, capability)
);
CREATE INDEX permission_grants_lookup ON permission_grants (profile_id, capability);

CREATE TABLE trust_assignments (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  subject_type text NOT NULL CHECK (subject_type IN ('role','user','webhook')),
  subject_id   bigint NOT NULL,
  trust_tier   smallint NOT NULL CHECK (trust_tier BETWEEN 0 AND 4),
  UNIQUE (profile_id, subject_type, subject_id)
);

-- ---------------------------------------------------------------- reference data
CREATE TABLE entities (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id      bigint NOT NULL REFERENCES profiles,
  entity_type_key text NOT NULL,                  -- by key: survives config re-materialization
  canonical_name  text NOT NULL,
  category_key    text,
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','retired')),
  merged_into     bigint REFERENCES entities,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id, entity_type_key, canonical_name)
);

CREATE TABLE aliases (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id   bigint NOT NULL REFERENCES profiles,
  target_type  text NOT NULL CHECK (target_type IN ('entity','category','intent','profile')),
  target_key   text NOT NULL,                     -- entity id as text | category key | intent key | '' (profile)
  alias        text NOT NULL,
  alias_norm   text NOT NULL,
  weight       real NOT NULL DEFAULT 1.0,
  origin       text NOT NULL CHECK (origin IN ('config','import','admin','learned')),
  UNIQUE (profile_id, target_type, target_key, alias_norm)
);
CREATE INDEX aliases_trgm ON aliases USING gin (alias_norm gin_trgm_ops);
CREATE INDEX aliases_profile ON aliases (profile_id);
