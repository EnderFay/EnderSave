import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendVideo
from aiogram.types import CallbackQuery, Message

from bot import Application
from downloader import DownloadError, Media, VideoMetadata
from progress import Progress
from service import JobService
from test_downloader import settings, video, sound


def media():
    return Media("https://youtu.be/test", "title", 10, [video(audio="aac"), sound()], "test", "Youtube")


def job():
    return dict(id=1, owner=42, chat_id=42, message_id=9, state="running", kind="download",
                choice="1080", media_key="youtube:test", url="https://youtu.be/test", temp_path=None)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.bot = AsyncMock(), AsyncMock()
        self.store.stage.return_value = True
        self.store.cancelled.return_value = False
        self.store.media.return_value = media()
        self.store.cached.return_value = None
        self.service = JobService(settings(Path(self.temp.name)), self.store, self.bot)
        self.service.downloader.inspect = AsyncMock(return_value=media())

    async def test_cached_file_never_downloaded(self):
        self.store.cached.return_value = dict(file_id="stored_id", file_size=100, width=1080, height=1920, duration=10)
        self.service.downloader.download = AsyncMock()
        text = await self.service.deliver(job(), Progress(self.bot, job()))
        self.service.downloader.inspect.assert_not_awaited()
        self.service.downloader.download.assert_not_awaited()
        self.bot.send_video.assert_awaited_once()
        self.assertEqual(self.bot.send_video.call_args.kwargs["video"], "stored_id")
        self.assertEqual(self.bot.send_video.call_args.kwargs["height"], 1920)
        self.store.complete.assert_awaited_once()
        self.assertIn("без скачивания", text)

    async def test_oversized_cache_not_sent(self):
        self.store.cached.return_value = dict(file_id="stored_id", file_size=2_000_000_000)
        self.service.downloader.inspect.side_effect = DownloadError("stop")
        with self.assertRaises(DownloadError):
            await self.service.deliver(job(), Progress(self.bot, job()))
        self.bot.send_video.assert_not_awaited()

    async def test_network_send_failure_not_retried(self):
        self.store.cached.return_value = dict(file_id="stored_id", file_size=100, width=1080, height=1920, duration=10)
        self.bot.send_video.side_effect = TelegramNetworkError(SendVideo(chat_id=42, video="id"), "network")
        current = job()
        current["state"] = "sending"
        self.store.get.return_value = current
        await self.service.execute(current)
        self.assertEqual(self.bot.send_video.await_count, 1)
        self.assertTrue(any(c.args[1] == "unknown" for c in self.store.stage.call_args_list))
        self.store.complete.assert_not_awaited()

    async def test_temporary_download_errors_are_retried(self):
        action = AsyncMock(side_effect=[DownloadError("network", retryable=True), "ok"])
        with patch("service.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(await self.service.retry(job(), action, Progress(self.bot, job())), "ok")
        self.assertEqual(action.await_count, 2)

    async def test_permanent_errors_not_retried(self):
        action = AsyncMock(side_effect=DownloadError("private"))
        with self.assertRaises(DownloadError):
            await self.service.retry(job(), action, Progress(self.bot, job()))
        self.assertEqual(action.await_count, 1)

    async def test_bad_cached_file_id_falls_back_to_download(self):
        self.store.cached.return_value = dict(file_id="expired", file_size=100, width=1080, height=1920, duration=10)
        self.bot.send_video.side_effect = TelegramBadRequest(SendVideo(chat_id=42, video="expired"), "wrong file identifier/HTTP URL specified")
        self.service.downloader.inspect.side_effect = DownloadError("stop before download")
        with self.assertRaises(DownloadError):
            await self.service.deliver(job(), Progress(self.bot, job()))
        self.store.invalidate.assert_awaited_once_with("youtube:test", "1080", 0)
        self.service.downloader.inspect.assert_awaited_once()

    async def test_cleanup_when_preparation_fails(self):
        current = job()
        async def download(_media, _choice, directory, **_):
            current["temp_path"] = str(directory)
            (directory / "source.part").write_bytes(b"partial")
            raise DownloadError("error")
        self.store.get.return_value = current
        self.service.downloader.download = download
        await self.service.execute(current)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.bot.send_video.assert_not_awaited()

    async def test_cancel_during_download_cleans_files(self):
        current, entered = job(), asyncio.Event()
        async def download(_media, _choice, directory, **_):
            current["temp_path"] = str(directory)
            (directory / "source.part").write_bytes(b"partial")
            entered.set()
            await asyncio.sleep(60)
        self.store.get.return_value = current
        self.service.downloader.download = download
        task = asyncio.create_task(self.service.execute(current))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.bot.send_video.assert_not_awaited()


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_user_cannot_select_or_cancel(self):
        store, bot = AsyncMock(), AsyncMock()
        stored = job()
        stored["state"] = "choosing"
        store.get.return_value = stored
        app = Application(settings(Path(".")), store, bot)
        callback = AsyncMock(spec=CallbackQuery)
        callback.answer = AsyncMock()
        callback.data = "download:1:1080"
        callback.from_user = SimpleNamespace(id=99)
        callback.message = AsyncMock(spec=Message)
        callback.message.chat = SimpleNamespace(id=42)
        await app.choose(callback)
        store.enqueue.assert_not_awaited()
        callback.answer.assert_awaited_once()

    async def test_selection_is_read_from_postgres_not_memory(self):
        store, bot = AsyncMock(), AsyncMock()
        stored = job()
        stored["state"] = "choosing"
        store.get.return_value, store.media.return_value = stored, media()
        store.enqueue.return_value = True
        app = Application(settings(Path(".")), store, bot)
        callback = AsyncMock(spec=CallbackQuery)
        callback.answer = AsyncMock()
        callback.data = "download:1:1080"
        callback.from_user = SimpleNamespace(id=42)
        callback.message = AsyncMock(spec=Message)
        callback.message.chat = SimpleNamespace(id=42)
        await app.choose(callback)
        store.enqueue.assert_awaited_once_with(1, 42, 42, "1080")


if __name__ == "__main__":
    unittest.main()
