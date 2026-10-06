-- 0006: FAQ = projection of canonical claims (DESIGN §9); Discord posts are presentation only.

CREATE TABLE faq_entries (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id     bigint NOT NULL REFERENCES profiles,
  category_id    bigint REFERENCES categories,
  question       text NOT NULL,
  answer         text NOT NULL,
  search_text    text NOT NULL,
  lang           text NOT NULL,
  status         text NOT NULL CHECK (status IN ('draft','pending_approval','published','needs_review',
                                                 'deprecated','superseded','rejected')),
  rev            int NOT NULL DEFAULT 1,
  superseded_by  bigint REFERENCES faq_entries,
  origin         text NOT NULL CHECK (origin IN ('verified_claim','popular_query','admin')),
  banner         text CHECK (banner IN ('disputed','review','deprecated')),
  created_by     bigint REFERENCES actors,
  approved_by    bigint REFERENCES actors,
  approved_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  ts_config      regconfig NOT NULL DEFAULT 'guru_simple',
  tsv            tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, search_text)) STORED
);
CREATE INDEX faq_tsv ON faq_entries USING gin (tsv) WHERE status = 'published';
CREATE INDEX faq_question_trgm ON faq_entries USING gin (search_text gin_trgm_ops) WHERE status = 'published';
CREATE INDEX faq_profile_status ON faq_entries (profile_id, status);

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

-- Desired vs published state for the reconciler (P6).
CREATE TABLE faq_publications (
  faq_id           bigint PRIMARY KEY REFERENCES faq_entries,
  channel_id       bigint NOT NULL,
  thread_id        bigint,
  message_id       bigint,
  desired_state    text NOT NULL CHECK (desired_state IN ('published','deprecated','removed')),
  desired_rev      int NOT NULL,
  published_state  text CHECK (published_state IN ('published','deprecated','removed')),
  published_rev    int,
  sync_status      text NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending','in_sync','error')),
  last_synced_at   timestamptz,
  last_error       text,
  attempts         int NOT NULL DEFAULT 0
);
CREATE INDEX faq_publications_dirty ON faq_publications (faq_id) WHERE sync_status <> 'in_sync';

ALTER TABLE review_tasks DROP CONSTRAINT review_tasks_kind_check;
ALTER TABLE review_tasks ADD CONSTRAINT review_tasks_kind_check
  CHECK (kind IN ('claim_keep','answer_rating','faq_approval','conflict','claim_report','claim_review','source_review'));

ALTER TABLE query_log DROP CONSTRAINT query_log_answered_by_check;
ALTER TABLE query_log ADD CONSTRAINT query_log_answered_by_check
  CHECK (answered_by IN ('cache','faq','structured','extractive','llm','no_answer','rejected','limited','error'));
