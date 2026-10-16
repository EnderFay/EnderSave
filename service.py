import asyncio
import logging
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import asyncpg
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from downloader import DownloadError, Downloader, DownloadTimings, VideoMetadata, probe_video
from progress import Progress, ProgressInputFile, size_text

logger = logging.getLogger("ytttbot.queue")


class JobService:
    def __init__(self, settings, store, bot):
        self.settings, self.store, self.bot = settings, store, bot
        self.downloader = Downloader(settings)
        self.workers = []
        self.active = {}
        self.stopping = False
        self.accept_lock = asyncio.Lock()

    async def start(self):
        for job in await self.store.recover():
            await self.cleanup(job.get("temp_path"), job["id"])
            await self.edit(job, "Запрос отменён. Временные файлы удалены." if job["state"] == "cancelled" else
                            "Отправка была прервана. Проверь чат: файл мог уже прийти. Если его нет, пришли ссылку заново.")
        self.workers = [asyncio.create_task(self.worker()) for _ in range(self.settings.concurrency)]

    async def stop(self):
        self.stopping = True
        for task in self.workers:
            task.cancel()
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*self.workers, *list(self.active.values()), return_exceptions=True)

    async def edit(self, job, text, keyboard=None):
        try:
            await self.bot.edit_message_text(chat_id=job["chat_id"], message_id=job["message_id"],
                                             text=text, reply_markup=keyboard, request_timeout=15)
        except TelegramAPIError:
            logger.warning("Не удалось обновить сообщение задания %d", job["id"])

    async def cleanup(self, raw_path, job_id):
        if not raw_path:
            return
        path = Path(raw_path)
        root = self.settings.download_dir.resolve()
        if path.is_symlink() or path.resolve().parent != root or not path.name.startswith(f"job-{job_id}-"):
            logger.warning("Очистка отклонена: каталог не принадлежит заданию %d", job_id)
            return
        if path.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, path)
            except OSError:
                logger.warning("Не удалось очистить каталог задания %d", job_id)

    async def watch_cancel(self, job_id, task):
        while not task.done():
            if await self.store.cancelled(job_id):
                task.cancel()
                return
            await asyncio.sleep(0.7)

    async def worker(self):
        while not self.stopping:
            job = None
            try:
                async with self.accept_lock:
                    job = await self.store.claim()
                if not job:
                    await asyncio.sleep(0.8)
                    continue
                task = asyncio.create_task(self.execute(job))
                self.active[job["id"]] = task
                watcher = asyncio.create_task(self.watch_cancel(job["id"], task))
                try:
                    await task
                except asyncio.CancelledError:
                    if not self.stopping:
                        current = await self.store.get(job["id"])
                        if current["state"] == "done":
                            continue
                        uncertain = current["state"] == "sending"
                        await self.store.stage(job["id"], "unknown" if uncertain else "cancelled", error_code="cancelled")
                        await self.edit(job, "Передача остановлена. Telegram мог уже получить файл — проверь чат."
                                        if uncertain else "Запрос отменён. Временные файлы удалены.")
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                    self.active.pop(job["id"], None)
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.error("Сбой worker: %s", type(error).__name__)
                if job:
                    await self.recover_failed_worker(job)
                await asyncio.sleep(2)

    async def recover_failed_worker(self, job):
        while not self.stopping:
            try:
                current = await self.store.get(job["id"])
                if current["state"] == "sending":
                    await self.store.stage(job["id"], "unknown", error_code="worker_interrupted")
                elif current["state"] == "running":
                    state = "cancelled" if current["cancel_requested"] else "queued"
                    await self.store.stage(job["id"], state)
                return
            except Exception:
                await asyncio.sleep(2)

    async def guard(self, job_id):
        if await self.store.cancelled(job_id):
            raise asyncio.CancelledError

    async def retry(self, job, action, progress):
        for attempt in range(1, self.settings.retry_attempts + 1):
            await self.guard(job["id"])
            try:
                return await action()
            except DownloadError as error:
                if not error.retryable or attempt == self.settings.retry_attempts:
                    raise
                delay = 2 ** attempt
                progress.stage(f"Временный сбой сети. Повтор {attempt + 1}/{self.settings.retry_attempts} через {delay} с…")
                await self.store.pool.execute("INSERT INTO job_events(job_id,event) VALUES($1,$2)",
                                              job["id"], f"network_retry_{attempt + 1}")
                await asyncio.sleep(delay)

    async def execute(self, job):
        try:
            await self.cleanup(job.get("temp_path"), job["id"])
            async with Progress(self.bot, job, self.settings.progress_interval) as progress:
                if job["kind"] == "inspect":
                    text, keyboard = await self.inspect(job, progress)
                else:
                    text, keyboard = await self.deliver(job, progress), None
            await self.edit(job, text, keyboard)
        except DownloadError as error:
            await self.guard(job["id"])
            if await self.store.stage(job["id"], "failed", error_code="download_error"):
                await self.edit(job, str(error) + "\nПришли ссылку заново, чтобы повторить запрос.")
        except TelegramNetworkError:
            await self.store.stage(job["id"], "unknown", error_code="network_during_send")
            await self.edit(job, "Связь с Telegram прервалась при отправке. Проверь чат перед повторным запросом: файл мог уже прийти.")
        except Exception as error:
            logger.error("Задание %d: %s", job["id"], type(error).__name__)
            with suppress(Exception):
                current = await self.store.get(job["id"])
                state = "unknown" if current["state"] == "sending" else "failed"
                await self.store.stage(job["id"], state, error_code=type(error).__name__)
                await self.edit(job, "Не удалось завершить запрос. Проверь чат перед повторной отправкой ссылки."
                                if state == "unknown" else "Не удалось обработать видео. Попробуй позже.")
        finally:
            current = await self.store.get(job["id"])
            if current:
                await self.cleanup(current.get("temp_path"), job["id"])

    async def inspect(self, job, progress):
        started = time.perf_counter()
        progress.stage("Проверяю ссылку…")
        media = await self.store.known_media(job["url"])
        if media is None:
            media = await self.retry(job, lambda: self.downloader.inspect(job["url"]), progress)
        key = await self.store.save_media(media, job["url"])
        seconds = time.perf_counter() - started
        buttons = []
        for choice in [str(q) for q in media.qualities] + (["mp3"] if media.audio_available else []):
            bitrate = self.settings.mp3_bitrate if choice == "mp3" else 0
            cached = await self.store.cached(key, choice, bitrate)
            label = "🎵 MP3" if choice == "mp3" else f"{choice}p"
            if cached and cached["file_size"]:
                label += f" · {size_text(cached['file_size'])}"
            else:
                estimate = media.estimate(choice, self.settings.mp3_bitrate)
                if estimate:
                    label += f" · ≈{size_text(estimate)}"
            buttons.append(InlineKeyboardButton(text=label, callback_data=f"download:{job['id']}:{choice}"))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{job['id']}")])
        await self.guard(job["id"])
        if not await self.store.stage(job["id"], "choosing", media_key=key, inspection_seconds=seconds):
            raise asyncio.CancelledError
        return (f"{media.title}\n\nВыбери качество или MP3. Размер с ≈ указан приблизительно. Меню действует 15 минут.",
                InlineKeyboardMarkup(inline_keyboard=rows))

    async def send(self, job, attachment, media, metadata=None):
        for attempt in range(self.settings.retry_attempts):
            await self.guard(job["id"])
            try:
                options = dict(chat_id=job["chat_id"], caption=media.title,
                               request_timeout=self.settings.upload_timeout)
                if job["choice"] == "mp3":
                    return await self.bot.send_audio(audio=attachment, title=media.title, **options)
                return await self.bot.send_video(video=attachment, supports_streaming=True,
                    width=metadata.width, height=metadata.height, duration=metadata.duration, **options)
            except TelegramRetryAfter as error:
                if attempt + 1 == self.settings.retry_attempts:
                    raise
                await asyncio.sleep(error.retry_after)

    async def commit(self, job, result, media, bitrate, timings):
        async def commit_with_retry():
            for attempt in range(3):
                try:
                    return await self.store.complete(job, result, media, bitrate, timings)
                except (asyncpg.PostgresConnectionError, OSError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** attempt)
        task = asyncio.create_task(commit_with_retry())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task

    async def deliver(self, job, progress):
        started = time.perf_counter()
        media = await self.store.media(job["media_key"])
        bitrate = self.settings.mp3_bitrate if job["choice"] == "mp3" else 0
        cached = await self.store.cached(media.key, job["choice"], bitrate)
        if cached and 0 < cached["file_size"] <= self.settings.max_file_bytes:
            progress.stage("Файл уже загружен. Отправляю из Telegram…")
            if not await self.store.stage(job["id"], "sending"):
                raise asyncio.CancelledError
            metadata = VideoMetadata(cached["width"], cached["height"], cached["duration"])
            try:
                result = await self.send(job, cached["file_id"], media, metadata)
            except TelegramBadRequest as error:
                if not any(text in error.message.lower() for text in (
                    "wrong file identifier", "file_reference_expired", "invalid file id", "wrong remote file")):
                    raise
                await self.store.invalidate(media.key, job["choice"], bitrate)
                if not await self.store.stage(job["id"], "running"):
                    raise asyncio.CancelledError
            else:
                await self.commit(job, result, media, bitrate, {"cache_hit": True, "seconds": time.perf_counter() - started})
                return f"Готово! Отправлено из Telegram без скачивания на сервер.\nОбщее время: {time.perf_counter() - started:.1f} с."
        progress.stage("Проверяю доступность выбранного формата…")
        media = await self.retry(job, lambda: self.downloader.inspect(media.url), progress)
        await self.store.save_media(media, job["url"])
        media.select(job["choice"])
        directory = Path(tempfile.mkdtemp(prefix=f"job-{job['id']}-", dir=self.settings.download_dir))
        if not await self.store.stage(job["id"], "running", temp_path=str(directory), media_key=media.key):
            await self.cleanup(str(directory), job["id"])
            raise asyncio.CancelledError
        timing = DownloadTimings()

        async def download():
            progress.stage("Скачиваю…")
            return await self.downloader.download(media, job["choice"], directory, timings=timing,
                on_processing=progress.processing, on_progress=progress.download)

        path = await self.retry(job, download, progress)
        progress.stage("Проверяю готовый файл…")
        metadata = await probe_video(path) if job["choice"] != "mp3" else None
        filename = media.title[:80].translate(str.maketrans('', '', '/\\:*?"<>|'))
        attachment = ProgressInputFile(path, f"{filename}{path.suffix}", progress)
        await self.guard(job["id"])
        if not await self.store.stage(job["id"], "sending"):
            raise asyncio.CancelledError
        progress.stage("Отправляю файл в Telegram…")
        upload_started = time.perf_counter()
        result = await self.send(job, attachment, media, metadata)
        upload_seconds = time.perf_counter() - upload_started
        timings = {"download": timing.download, "processing": timing.processing, "upload": upload_seconds,
                   "total": time.perf_counter() - started, "mode": timing.mode, "cache_hit": False}
        await self.commit(job, result, media, bitrate, timings)
        logger.info("Задание %d: download=%.2fs processing=%.2fs upload=%.2fs mode=%s", job["id"],
                    timing.download, timing.processing, upload_seconds, timing.mode)
        return (f"Готово!\nОбщее время: {timings['total']:.1f} с.\n"
                "Пришли следующую ссылку.")
