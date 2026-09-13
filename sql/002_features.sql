ALTER TABLE logs ADD COLUMN IF NOT EXISTS stream text NOT NULL DEFAULT 'runtime' CHECK(stream IN ('runtime','build'));
CREATE INDEX IF NOT EXISTS logs_stream_id ON logs(bot_id,stream,id DESC);
ALTER TABLE bots ADD COLUMN IF NOT EXISTS build_mode text NOT NULL DEFAULT 'system' CHECK(build_mode IN ('system','dockerfile'));
ALTER TABLE bots ADD COLUMN IF NOT EXISTS dockerfile_path text NOT NULL DEFAULT 'Dockerfile';
ALTER TABLE bots ADD COLUMN IF NOT EXISTS auto_update boolean NOT NULL DEFAULT false;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS github_last_sha text;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS github_last_attempt_sha text;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS github_last_check timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_token text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_login text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_user_id bigint;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_scope text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_connected_at timestamptz;
CREATE TABLE IF NOT EXISTS sqlite_requests(
 id uuid PRIMARY KEY,
 bot_id uuid NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
 user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 operation text NOT NULL CHECK(operation IN ('files','tables','schema','rows')),
 file_path text,
 table_name text,
 row_offset integer NOT NULL DEFAULT 0,
 row_limit integer NOT NULL DEFAULT 100,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','done','failed')),
 result jsonb,
 error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,
 finished_at timestamptz,
 expires_at timestamptz NOT NULL DEFAULT now()+interval '2 minutes'
);
CREATE INDEX IF NOT EXISTS sqlite_requests_queue ON sqlite_requests(state,created_at) WHERE state='pending';
CREATE INDEX IF NOT EXISTS sqlite_requests_owner ON sqlite_requests(user_id,created_at DESC);
ALTER TABLE worker_health ADD COLUMN IF NOT EXISTS supports_docker boolean NOT NULL DEFAULT false;
