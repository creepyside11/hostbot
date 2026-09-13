BEGIN;
CREATE TABLE IF NOT EXISTS users (
 id uuid PRIMARY KEY, email text UNIQUE NOT NULL, password_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash text PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS rate_limits (
 key text PRIMARY KEY, hits integer NOT NULL, expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS bots (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name text NOT NULL, location text NOT NULL DEFAULT 'nl' CHECK(location='nl'),
 source text NOT NULL CHECK(source IN ('github','zip')), repo text, branch text NOT NULL DEFAULT 'main',
 runtime text NOT NULL CHECK(runtime IN ('python','node')), entrypoint text NOT NULL,
 secrets text NOT NULL, archive bytea,
 status text NOT NULL DEFAULT 'deploying' CHECK(status IN ('deploying','running','error','stopped')),
 desired text NOT NULL DEFAULT 'running' CHECK(desired IN ('running','stopped','deleted')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bots_owner ON bots(user_id);
CREATE TABLE IF NOT EXISTS jobs (
 id bigserial PRIMARY KEY, bot_id uuid NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
 action text NOT NULL CHECK(action IN ('deploy','update','restart','start','stop','delete')),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','done','failed')),
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs(bot_id) WHERE state IN ('pending','active');
CREATE TABLE IF NOT EXISTS logs (
 id bigserial PRIMARY KEY, bot_id uuid NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
 message text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS logs_bot_id ON logs(bot_id,id DESC);
CREATE TABLE IF NOT EXISTS worker_health (
 id text PRIMARY KEY, heartbeat timestamptz NOT NULL, version text NOT NULL
);
COMMIT;
