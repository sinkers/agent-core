-- Scheduled work requested of the agent: one-off or repeating (cron, in the task's time zone).
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              bigserial PRIMARY KEY,
    instruction     text        NOT NULL,
    created_by      text,                    -- requester aad id
    created_by_name text,
    deliver_to      text,                    -- aad id of who receives the result
    deliver_to_name text,
    run_at          timestamptz NOT NULL,    -- next run
    cron            text,                    -- null = one-off
    tz              text        NOT NULL DEFAULT 'UTC',
    status          text        NOT NULL DEFAULT 'active',  -- active | done | cancelled
    run_count       integer     NOT NULL DEFAULT 0,
    last_run_at     timestamptz,
    last_result     text,
    locked_until    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scheduled_tasks_due_idx ON scheduled_tasks (status, run_at);
