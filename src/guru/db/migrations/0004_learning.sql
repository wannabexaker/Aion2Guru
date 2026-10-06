-- 0004: learned review gates (D-27) and ingestion bookkeeping

CREATE TABLE learned_gates (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id     bigint NOT NULL REFERENCES profiles,
  decision_kind  text NOT NULL,
  model          jsonb,                           -- NULL when not enough labels yet
  n_labels       int NOT NULL,
  auto_enabled   boolean NOT NULL DEFAULT false,
  metrics        jsonb NOT NULL DEFAULT '{}',
  trained_at     timestamptz NOT NULL DEFAULT now(),
  active         boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX learned_gates_active ON learned_gates (profile_id, decision_kind) WHERE active;

-- Fast lookup of pending passive candidates per channel for windowing.
CREATE INDEX observations_pending_channel ON observations (channel_id, published_at)
  WHERE processing_state = 'pending';
