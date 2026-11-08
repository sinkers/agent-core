-- One person across channels (agent.py "elsewhere"): find their recent messages in other conversations.
CREATE INDEX IF NOT EXISTS messages_user_at_idx ON messages (user_id, at DESC) WHERE role = 'user';
-- Background work started from a call (scheduler.start_background_task): how the result reaches the person.
ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS deliver_via text NOT NULL DEFAULT 'teams';  -- teams | email | call | call_mobile
ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS background boolean NOT NULL DEFAULT false;
ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS origin text;  -- the conversation it was asked for in
