"""Интеграционные тесты запускаются только в отдельной тестовой базе."""

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from downloader import Media, VideoMetadata
from progress import Progress
from service import JobService
from storage import BusyError, Store
from test_downloader import settings, video, sound


@unittest.skipUnless(os.getenv("TEST_POSTGRES_PORT"), "Use tests/run_postgres_tests.py for isolated PostgreSQL tests")
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(settings(Path(self.temp.name)), postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
            postgres_db="ytttbot_test", postgres_user="postgres", postgres_password=os.environ["TEST_POSTGRES_PASSWORD"])
        self.store = await Store.open(self.settings, 123456)
        self.addAsyncCleanup(self.store.close)
        await self.store.pool.execute("TRUNCATE job_events,jobs,telegram_files,media_aliases,media RESTART IDENTITY CASCADE")
        self.media = Media("https://youtube.com/shorts/abcd", "Title", 10, [video(audio="aac"), sound()], "abcd", "Youtube")

    async def create(self, owner=42):
        return await self.store.create(owner, owner, 9, self.media.url, 20)

    async def choosing(self, job):
        await self.store.claim()
        await self.store.save_media(self.media, job["url"])
        self.assertTrue(await self.store.stage(job["id"], "choosing", media_key=self.media.key))

    async def test_atomic_claim_with_multiple_workers(self):
        for owner in range(5):
            await self.create(owner)
        claimed = await asyncio.gather(*(self.store.claim() for _ in range(8)))
        ids = [j["id"] for j in claimed if j]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)

    async def test_double_click_and_foreign_owner_rejected(self):
        job = await self.create()
        await self.choosing(job)
        self.assertFalse(await self.store.enqueue(job["id"], 99, 42, "1080"))
        results = await asyncio.gather(*(self.store.enqueue(job["id"], 42, 42, "1080") for _ in range(2)))
        self.assertEqual(sorted(results), [False, True])

    async def test_history_is_private(self):
        await self.create(42)
        await self.create(99)
        rows = await self.store.history(42)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)

    async def test_cancel_queued_and_running(self):
        job = await self.create()
        self.assertIsNone(await self.store.cancel(job["id"], 99, 42))
        self.assertEqual(await self.store.cancel(job["id"], 42, 42), "cancelled")
        self.assertIsNone(await self.store.claim())
        running = await self.create()
        await self.store.claim()
        self.assertEqual(await self.store.cancel(running["id"], 42, 42), "running")
        self.assertTrue(await self.store.cancelled(running["id"]))

    async def test_restart_requeues_running_but_not_uncertain_sends(self):
        a, b = await self.create(1), await self.create(2)
        await self.store.claim()
        await self.store.claim()
        await self.store.stage(b["id"], "sending")
        uncertain = await self.store.recover()
        self.assertEqual([row["id"] for row in uncertain], [b["id"]])
        self.assertEqual((await self.store.get(a["id"]))["state"], "queued")
        self.assertEqual((await self.store.get(b["id"]))["state"], "unknown")

    async def test_single_instance_lock(self):
        with self.assertRaises(ValueError):
            await Store.open(self.settings, 123456)

    async def test_expired_menu_not_enqueued(self):
        job = await self.create()
        await self.choosing(job)
        await self.store.pool.execute("UPDATE jobs SET updated_at=now()-interval '16 minutes' WHERE id=$1", job["id"])
        self.assertFalse(await self.store.enqueue(job["id"], 42, 42, "1080"))

    async def test_end_to_end_cache_persists_across_service_restart(self):
        bot = AsyncMock()
        attachment = SimpleNamespace(file_id="telegram_id", file_size=100, width=1080, height=1920, duration=10)
        bot.send_video.return_value = SimpleNamespace(video=attachment, message_id=100)
        service = JobService(self.settings, self.store, bot)
        service.downloader.inspect = AsyncMock(return_value=self.media)
        async def download(media, choice, directory, **kwargs):
            path = directory / "result.mp4"
            path.write_bytes(b"file")
            return path
        service.downloader.download = AsyncMock(side_effect=download)
        job = await self.create()
        await service.execute(await self.store.claim())
        self.assertEqual((await self.store.get(job["id"]))["state"], "choosing")
        await self.store.enqueue(job["id"], 42, 42, "1080")
        with patch("service.probe_video", new_callable=AsyncMock, return_value=VideoMetadata(1080, 1920, 10)):
            await service.execute(await self.store.claim())
        self.assertEqual((await self.store.get(job["id"]))["state"], "done")
        self.assertEqual(list(self.settings.download_dir.iterdir()), [])
        self.assertEqual((await self.store.cached(self.media.key, "1080", 0))["file_id"], "telegram_id")
        self.assertIsNone(await self.store.cached(self.media.key, "720", 0))
        self.assertIsNone(await self.store.cached(self.media.key, "mp3", 192))
        await self.store.close()
        self.store = await Store.open(self.settings, 123456)
        self.addAsyncCleanup(self.store.close)
        service2 = JobService(self.settings, self.store, bot)
        service2.downloader.inspect = AsyncMock(side_effect=AssertionError("Cache should avoid yt-dlp"))
        service2.downloader.download = AsyncMock(side_effect=AssertionError("Cache should avoid download"))
        second = await self.store.create(42, 42, 10, "https://youtu.be/abcd?si=tracking", 20)
        await service2.execute(await self.store.claim())
        self.assertEqual((await self.store.get(second["id"]))["state"], "choosing")
        await self.store.enqueue(second["id"], 42, 42, "1080")
        await service2.execute(await self.store.claim())
        self.assertEqual((await self.store.get(second["id"]))["state"], "done")
        self.assertEqual(bot.send_video.call_args.kwargs["video"], "telegram_id")

    async def test_database_cancel_stops_worker_and_removes_partial_file(self):
        job = await self.create()
        await self.choosing(job)
        await self.store.enqueue(job["id"], 42, 42, "1080")
        bot, entered = AsyncMock(), asyncio.Event()
        service = JobService(self.settings, self.store, bot)
        service.downloader.inspect = AsyncMock(return_value=self.media)

        async def download(media, choice, directory, **kwargs):
            (directory / "source.part").write_bytes(b"partial")
            entered.set()
            await asyncio.sleep(60)

        service.downloader.download = download
        await service.start()
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await self.store.cancel(job["id"], 42, 42)
            async with asyncio.timeout(5):
                while (await self.store.get(job["id"]))["state"] != "cancelled":
                    await asyncio.sleep(0.1)
            self.assertEqual(list(self.settings.download_dir.iterdir()), [])
            bot.send_video.assert_not_awaited()
        finally:
            await service.stop()


if __name__ == "__main__":
    unittest.main()
