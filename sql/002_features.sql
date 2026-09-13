ALTER TABLE logs ADD COLUMN IF NOT EXISTS stream text NOT NULL DEFAULT 'runtime' CHECK(stream IN ('runtime','build'));
CREATE INDEX IF NOT EXISTS logs_stream_id ON logs(bot_id,stream,id DESC);
ALTER TABLE bots ADD COLUMN IF NOT EXISTS build_mode text NOT NULL DEFAULT 'system' CHECK(build_mode IN ('system','dockerfile'));
ALTER TABLE bots ADD COLUMN IF NOT EXISTS dockerfile_path text NOT NULL DEFAULT 'Dockerfile';
ALTER TABLE worker_health ADD COLUMN IF NOT EXISTS supports_docker boolean NOT NULL DEFAULT false;
