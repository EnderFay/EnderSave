CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    installed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS media (
    media_key text PRIMARY KEY,
    snapshot jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS media_aliases (
    url text PRIMARY KEY,
    media_key text NOT NULL REFERENCES media(media_key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS telegram_files (
    bot_id bigint NOT NULL,
    media_key text NOT NULL REFERENCES media(media_key) ON DELETE CASCADE,
    choice text NOT NULL,
    bitrate integer NOT NULL,
    file_id text NOT NULL,
    file_size bigint NOT NULL,
    width integer NOT NULL DEFAULT 0,
    height integer NOT NULL DEFAULT 0,
    duration integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, media_key, choice, bitrate)
);
CREATE TABLE IF NOT EXISTS jobs (
    id bigserial PRIMARY KEY,
    bot_id bigint NOT NULL,
    owner bigint NOT NULL,
    chat_id bigint NOT NULL,
    message_id bigint NOT NULL,
    url text NOT NULL,
    kind text NOT NULL DEFAULT 'inspect' CHECK (kind IN ('inspect', 'download')),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'running', 'choosing', 'sending', 'done', 'cancelled', 'failed', 'unknown', 'expired'
    )),
    choice text,
    media_key text REFERENCES media(media_key),
    cancel_requested boolean NOT NULL DEFAULT false,
    temp_path text,
    attempts integer NOT NULL DEFAULT 0,
    inspection_seconds double precision NOT NULL DEFAULT 0,
    timings jsonb NOT NULL DEFAULT '{}',
    error_code text,
    result_message_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs(bot_id, id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_history_idx ON jobs(bot_id, owner, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_owner ON jobs(bot_id, owner)
    WHERE state IN ('queued', 'running', 'sending');
CREATE TABLE IF NOT EXISTS job_events (
    id bigserial PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
