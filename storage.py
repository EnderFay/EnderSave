import json
from pathlib import Path

import asyncpg

from config import Settings
from downloader import Media, normalize_url


class BusyError(Exception):
    pass


class Store:
    def __init__(self, pool: asyncpg.Pool, bot_id: int):
        self.pool = pool
        self.bot_id = bot_id
        self.lock_connection = None

    @classmethod
    async def open(cls, settings: Settings, bot_id: int) -> "Store":
        if not settings.postgres_password:
            raise ValueError("Добавьте POSTGRES_PASSWORD в .env")
        pool = await asyncpg.create_pool(
            host=settings.postgres_host, port=settings.postgres_port,
            database=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password, min_size=1,
            max_size=max(5, settings.concurrency + 4), command_timeout=15,
        )
        store = cls(pool, bot_id)
        try:
            async with pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(719245019)")
                await connection.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            store.lock_connection = await pool.acquire()
            if not await store.lock_connection.fetchval("SELECT pg_try_advisory_lock($1)", bot_id):
                raise ValueError("Этот бот уже запущен с той же базой данных")
            return store
        except BaseException:
            await store.close()
            raise

    async def close(self):
        if self.lock_connection is not None:
            await self.lock_connection.execute("SELECT pg_advisory_unlock($1)", self.bot_id)
            await self.pool.release(self.lock_connection)
            self.lock_connection = None
        await self.pool.close()

    async def recover(self) -> list[dict]:
        async with self.pool.acquire() as c, c.transaction():
            uncertain = await c.fetch("""UPDATE jobs SET state='unknown', error_code='interrupted_send',
                updated_at=now() WHERE bot_id=$1 AND state='sending' RETURNING *""", self.bot_id)
            resumed = await c.fetch("""UPDATE jobs SET state=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
                updated_at=now() WHERE bot_id=$1 AND state='running' RETURNING *""", self.bot_id)
            await c.execute("""UPDATE jobs SET state='expired', updated_at=now()
                WHERE bot_id=$1 AND state='choosing' AND updated_at < now()-interval '15 minutes'""", self.bot_id)
            return [dict(row) for row in uncertain] + [dict(row) for row in resumed if row["state"] == "cancelled"]

    async def create(self, owner: int, chat_id: int, message_id: int, url: str, max_users: int) -> dict:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", -self.bot_id)
            active = await c.fetchval("""SELECT count(*) FROM jobs WHERE bot_id=$1
                AND state IN ('queued','running','sending')""", self.bot_id)
            busy = await c.fetchval("""SELECT EXISTS(SELECT 1 FROM jobs WHERE bot_id=$1 AND owner=$2
                AND state IN ('queued','running','sending'))""", self.bot_id, owner)
            if busy:
                raise BusyError("Дождись завершения предыдущего запроса или отмени его.")
            if active >= max_users:
                raise BusyError("Очередь заполнена. Попробуй немного позже.")
            await c.execute("UPDATE jobs SET state='expired', updated_at=now() WHERE bot_id=$1 AND owner=$2 AND state='choosing'",
                            self.bot_id, owner)
            row = await c.fetchrow("""INSERT INTO jobs(bot_id,owner,chat_id,message_id,url)
                VALUES($1,$2,$3,$4,$5) RETURNING *""", self.bot_id, owner, chat_id, message_id, normalize_url(url))
            await c.execute("INSERT INTO job_events(job_id,event) VALUES($1,'queued')", row["id"])
            return dict(row)

    async def get(self, job_id: int) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM jobs WHERE bot_id=$1 AND id=$2", self.bot_id, job_id)
        return dict(row) if row else None

    async def claim(self) -> dict | None:
        row = await self.pool.fetchrow("""WITH next AS (
            SELECT id,extract(epoch FROM clock_timestamp()-updated_at)::double precision AS queue_seconds
            FROM jobs WHERE bot_id=$1 AND state='queued' AND NOT cancel_requested
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE jobs SET state='running', attempts=attempts+1, updated_at=now()
            FROM next WHERE jobs.id=next.id RETURNING jobs.*,next.queue_seconds""", self.bot_id)
        return dict(row) if row else None

    async def stage(self, job_id: int, state: str, **fields) -> bool:
        allowed = {"media_key", "temp_path", "inspection_seconds", "error_code", "result_message_id", "timings"}
        if not set(fields) <= allowed:
            raise ValueError("Unknown job field")
        values = [self.bot_id, job_id, state]
        assignments = ["state=$3", "updated_at=now()"]
        for name, value in fields.items():
            values.append(json.dumps(value) if name == "timings" else value)
            assignments.append(f"{name}=${len(values)}")
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(f"""UPDATE jobs SET {','.join(assignments)}
                WHERE bot_id=$1 AND id=$2 AND state IN ('running','sending')
                AND (NOT cancel_requested OR $3 IN ('cancelled','unknown')) RETURNING id""", *values)
            if row:
                await c.execute("INSERT INTO job_events(job_id,event) VALUES($1,$2)", job_id, state)
            return row is not None

    async def enqueue(self, job_id: int, owner: int, chat_id: int, choice: str) -> bool:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", -self.bot_id)
            if await c.fetchval("""SELECT EXISTS(SELECT 1 FROM jobs WHERE bot_id=$1 AND owner=$2
                AND state IN ('queued','running','sending'))""", self.bot_id, owner):
                return False
            row = await c.fetchrow("""UPDATE jobs SET kind='download',state='queued',choice=$4,updated_at=now()
                WHERE bot_id=$1 AND id=$2 AND owner=$3 AND chat_id=$5 AND state='choosing'
                AND NOT cancel_requested AND updated_at >= now()-interval '15 minutes' RETURNING id""",
                self.bot_id, job_id, owner, choice, chat_id)
            if row:
                await c.execute("INSERT INTO job_events(job_id,event) VALUES($1,'queued_download')", job_id)
            return row is not None

    async def cancel(self, job_id: int, owner: int, chat_id: int) -> str | None:
        row = await self.pool.fetchrow("""UPDATE jobs SET cancel_requested=true,
            state=CASE WHEN state IN ('queued','choosing') THEN 'cancelled' ELSE state END, updated_at=now()
            WHERE bot_id=$1 AND id=$2 AND owner=$3 AND chat_id=$4
            AND state IN ('queued','choosing','running','sending') RETURNING state""", self.bot_id, job_id, owner, chat_id)
        return row["state"] if row else None

    async def cancelled(self, job_id: int) -> bool:
        return bool(await self.pool.fetchval("SELECT cancel_requested FROM jobs WHERE bot_id=$1 AND id=$2", self.bot_id, job_id))

    async def save_media(self, media: Media, alias: str) -> str:
        key = media.key
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("""INSERT INTO media(media_key,snapshot) VALUES($1,$2)
                ON CONFLICT(media_key) DO UPDATE SET snapshot=excluded.snapshot,updated_at=now()""", key, json.dumps(media.snapshot()))
            for url in {normalize_url(alias), normalize_url(media.url)}:
                await c.execute("""INSERT INTO media_aliases(url,media_key) VALUES($1,$2)
                    ON CONFLICT(url) DO UPDATE SET media_key=excluded.media_key""", url, key)
        return key

    async def known_media(self, alias: str) -> Media | None:
        raw = await self.pool.fetchval("""SELECT m.snapshot FROM media m JOIN media_aliases a USING(media_key)
            WHERE a.url=$1 AND (m.updated_at > now()-interval '15 minutes' OR EXISTS(
                SELECT 1 FROM telegram_files f WHERE f.media_key=m.media_key AND f.bot_id=$2))""", normalize_url(alias), self.bot_id)
        return Media(**json.loads(raw)) if raw else None

    async def media(self, key: str) -> Media:
        raw = await self.pool.fetchval("SELECT snapshot FROM media WHERE media_key=$1", key)
        return Media(**json.loads(raw))

    async def cached(self, key: str, choice: str, bitrate: int) -> dict | None:
        row = await self.pool.fetchrow("""SELECT * FROM telegram_files
            WHERE bot_id=$1 AND media_key=$2 AND choice=$3 AND bitrate=$4""", self.bot_id, key, choice, bitrate)
        return dict(row) if row else None

    async def invalidate(self, key: str, choice: str, bitrate: int):
        await self.pool.execute("DELETE FROM telegram_files WHERE bot_id=$1 AND media_key=$2 AND choice=$3 AND bitrate=$4",
                                self.bot_id, key, choice, bitrate)

    async def complete(self, job: dict, result, media: Media, bitrate: int, timings: dict):
        attachment = result.audio if job["choice"] == "mp3" else result.video
        if attachment is None:
            raise ValueError("Telegram did not return an attachment")
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("""INSERT INTO telegram_files(bot_id,media_key,choice,bitrate,file_id,file_size,width,height,duration)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(bot_id,media_key,choice,bitrate)
                DO UPDATE SET file_id=excluded.file_id,file_size=excluded.file_size,width=excluded.width,
                    height=excluded.height,duration=excluded.duration,created_at=now()""",
                self.bot_id, media.key, job["choice"], bitrate, attachment.file_id, attachment.file_size or 0,
                getattr(attachment, "width", 0), getattr(attachment, "height", 0), attachment.duration or 0)
            await c.execute("""UPDATE jobs SET state='done',result_message_id=$3,timings=$4,updated_at=now()
                WHERE bot_id=$1 AND id=$2""", self.bot_id, job["id"], result.message_id, json.dumps(timings))
            await c.execute("INSERT INTO job_events(job_id,event) VALUES($1,'done')", job["id"])

    async def history(self, owner: int) -> list[dict]:
        rows = await self.pool.fetch("""SELECT j.id,j.state,j.choice,j.created_at,m.snapshot->>'title' AS title
            FROM jobs j LEFT JOIN media m USING(media_key)
            WHERE j.bot_id=$1 AND j.owner=$2 ORDER BY j.id DESC LIMIT 10""", self.bot_id, owner)
        return [dict(row) for row in rows]
