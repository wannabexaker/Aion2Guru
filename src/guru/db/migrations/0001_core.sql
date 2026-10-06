-- 0001: extensions, actors, tenancy, config versions, audit (hash chain), job queue

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- No stemming; text is normalized by the application (guru.core.text.normalize).
CREATE TEXT SEARCH CONFIGURATION guru_simple (COPY = simple);

-- ---------------------------------------------------------------- actors
CREATE TABLE actors (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind             text NOT NULL CHECK (kind IN ('discord_user','system','cli','api')),
  discord_user_id  bigint UNIQUE,
  label            text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (kind <> 'discord_user' OR discord_user_id IS NOT NULL)
);
CREATE UNIQUE INDEX actors_system_label ON actors (kind, label) WHERE discord_user_id IS NULL;

-- ---------------------------------------------------------------- tenancy & config
CREATE TABLE guilds (
  guild_id            bigint PRIMARY KEY,
  name                text NOT NULL,
  default_profile_id  bigint,
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE profiles (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug              text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{1,40}$'),
  name              text NOT NULL,
  guild_id          bigint NOT NULL REFERENCES guilds,
  status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
  active_config_id  bigint,
  knowledge_epoch   bigint NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE guilds ADD FOREIGN KEY (default_profile_id) REFERENCES profiles;

CREATE TABLE profile_config_versions (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id      bigint NOT NULL REFERENCES profiles,
  version         int NOT NULL,
  schema_version  int NOT NULL,
  config          jsonb NOT NULL,
  config_hash     bytea NOT NULL,
  created_by      bigint NOT NULL REFERENCES actors,
  comment         text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id, version)
);
ALTER TABLE profiles ADD FOREIGN KEY (active_config_id) REFERENCES profile_config_versions;

-- ---------------------------------------------------------------- audit (append-only, hash chain)
CREATE TABLE audit_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  chain_seq       bigint UNIQUE,                  -- assigned under lock → total order of the chain
  ts              timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor_id        bigint NOT NULL REFERENCES actors,
  action          text NOT NULL,
  profile_id      bigint,
  target_type     text,
  target_id       text,
  before          jsonb,
  after           jsonb,
  reason          text,
  correlation_id  text,
  prev_hash       bytea,
  row_hash        bytea NOT NULL
);
CREATE INDEX audit_target ON audit_log (target_type, target_id);
CREATE INDEX audit_actor  ON audit_log (actor_id, ts);
CREATE INDEX audit_action ON audit_log (action, ts);

-- Digest is computed from a timezone-independent text form so verification is reproducible.
CREATE FUNCTION audit_row_digest(prev bytea, a audit_log) RETURNS bytea
LANGUAGE sql IMMUTABLE AS $$
  SELECT sha256(coalesce(prev, '\x'::bytea) || convert_to(concat_ws(E'\x1f',
           a.chain_seq::text,
           to_char(a.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
           a.actor_id::text, a.action, a.profile_id::text, a.target_type, a.target_id,
           a.before::text, a.after::text, a.reason, a.correlation_id), 'UTF8'))
$$;

CREATE FUNCTION audit_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  last_seq  bigint;
  last_hash bytea;
BEGIN
  PERFORM pg_advisory_xact_lock(7412001);
  SELECT chain_seq, row_hash INTO last_seq, last_hash FROM audit_log ORDER BY chain_seq DESC NULLS LAST LIMIT 1;
  NEW.chain_seq := coalesce(last_seq, 0) + 1;
  NEW.prev_hash := last_hash;
  NEW.row_hash  := audit_row_digest(last_hash, NEW);
  RETURN NEW;
END $$;

CREATE TRIGGER audit_chain BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_chain();

CREATE FUNCTION audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END $$;

CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_immutable();

-- Returns the first chain_seq whose hash does not verify (NULL = chain intact).
CREATE FUNCTION audit_verify_chain() RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE
  r    audit_log;
  prev bytea := NULL;
BEGIN
  FOR r IN SELECT * FROM audit_log ORDER BY chain_seq LOOP
    IF r.prev_hash IS DISTINCT FROM prev OR r.row_hash <> audit_row_digest(prev, r) THEN
      RETURN r.chain_seq;
    END IF;
    prev := r.row_hash;
  END LOOP;
  RETURN NULL;
END $$;

-- ---------------------------------------------------------------- job queue
CREATE TABLE jobs (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind             text NOT NULL,
  payload          jsonb NOT NULL DEFAULT '{}',
  priority         smallint NOT NULL DEFAULT 100,
  status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','dead')),
  idempotency_key  text UNIQUE,
  attempts         int NOT NULL DEFAULT 0,
  max_attempts     int NOT NULL DEFAULT 5,
  run_after        timestamptz NOT NULL DEFAULT now(),
  locked_by        text,
  locked_until     timestamptz,
  last_error       text,
  correlation_id   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz
);
CREATE INDEX jobs_ready  ON jobs (priority, run_after) WHERE status = 'queued';
CREATE INDEX jobs_leases ON jobs (locked_until) WHERE status = 'running';
CREATE INDEX jobs_dead   ON jobs (kind) WHERE status = 'dead';

CREATE TABLE schedules (
  key               text PRIMARY KEY,
  kind              text NOT NULL,
  payload           jsonb NOT NULL DEFAULT '{}',
  interval_seconds  int NOT NULL CHECK (interval_seconds > 0),
  next_run_at       timestamptz NOT NULL DEFAULT now(),
  enabled           boolean NOT NULL DEFAULT true
);
