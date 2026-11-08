-- Usage reporting (Grafana, docs/plans/usage-reporting.md): who, where and which session each model call was for.
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS agent           text;
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS conversation_id text;
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS session_id      text;
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS channel         text;
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS user_id         text;
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS user_name       text;
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS trigger         text;
CREATE INDEX IF NOT EXISTS model_usage_user_idx ON model_usage (user_id, at);
CREATE INDEX IF NOT EXISTS model_usage_session_idx ON model_usage (session_id);

-- Costs that aren't model calls: voice minutes, phone minutes and SMS, speech services (phase 3).
CREATE TABLE IF NOT EXISTS service_usage (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    agent           text,
    service         text NOT NULL,      -- elevenlabs | telnyx_voice | telnyx_sms | transcribe | polly | ...
    unit            text NOT NULL,      -- minute | message | second | character | credit
    quantity        numeric(14, 4) NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
    conversation_id text,
    session_id      text,
    channel         text,
    user_id         text,
    user_name       text
);
CREATE INDEX IF NOT EXISTS service_usage_at_idx ON service_usage (at);
