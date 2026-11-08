-- Per-agent state (never on the pod filesystem). Idempotent; run on every start.

CREATE TABLE IF NOT EXISTS messages (
    id              bigserial PRIMARY KEY,
    conversation_id text        NOT NULL,
    role            text        NOT NULL,
    text            text        NOT NULL,
    user_id         text,
    user_name       text,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages (conversation_id, id DESC);

CREATE TABLE IF NOT EXISTS conversation_refs (
    conversation_id text PRIMARY KEY,
    ref             jsonb       NOT NULL,
    user_aad_id     text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_refs_user_idx ON conversation_refs (user_aad_id);

CREATE TABLE IF NOT EXISTS tasks (
    id         bigserial PRIMARY KEY,
    kind       text        NOT NULL,
    payload    jsonb       NOT NULL,
    status     text        NOT NULL DEFAULT 'queued',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- PLT-042: retained 12 months (see prune below).
CREATE TABLE IF NOT EXISTS audit_log (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    actor    text,
    action   text        NOT NULL,
    tool     text,
    args     jsonb,
    target   text,
    model_id text,
    cost_usd numeric(12, 6),
    detail   jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    text NOT NULL,
    key        text NOT NULL,
    value      jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS model_usage (
    id                 bigserial PRIMARY KEY,
    at                 timestamptz NOT NULL DEFAULT now(),
    model_id           text    NOT NULL,
    tier               text    NOT NULL,
    input_tokens       integer NOT NULL DEFAULT 0,
    output_tokens      integer NOT NULL DEFAULT 0,
    cache_read_tokens  integer NOT NULL DEFAULT 0,
    cache_write_tokens integer NOT NULL DEFAULT 0,
    cost_usd           numeric(12, 6) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS model_usage_at_idx ON model_usage (at);

CREATE TABLE IF NOT EXISTS budget_alerts (
    key     text PRIMARY KEY,
    sent_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    conversation_id text PRIMARY KEY,
    tool            text        NOT NULL,
    args            jsonb       NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

DELETE FROM audit_log WHERE at < now() - interval '12 months';
