import asyncio
import time
from contextlib import suppress

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup


def cancel_keyboard(job_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{job_id}")]])


def size_text(size: float | None) -> str:
    return f"{size / 1_000_000:.1f} МБ" if size is not None else "размер неизвестен"


class Progress:
    def __init__(self, bot, job: dict, interval: int = 3):
        self.bot, self.job, self.interval = bot, job, interval
        self.text = "Запрос в очереди…"
        self.last_text = None
        self.next_edit = 0.0
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.tick())
        return self

    async def __aexit__(self, *_):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    def stage(self, text: str):
        self.text = text

    async def processing(self):
        self.stage("Подготавливаю файл для отправки…")

    def download(self, data: dict):
        done, total, speed = data.get("downloaded_bytes", 0), data.get("total_bytes"), data.get("speed")
        label = f"{min(100, done / total * 100):.0f}% · " if total else ""
        self.text = f"Скачиваю дорожку: {label}{size_text(done)}"
        if total:
            self.text += f" / {size_text(total)}"
        if speed:
            self.text += f"\nСкорость: {size_text(speed)}/с"
        if data.get("eta") is not None:
            self.text += f" · осталось ≈{int(data['eta'])} с"

    def upload(self, done: int, total: int, elapsed: float):
        if done >= total:
            self.text = "Файл передан. Ожидаю подтверждения отправки от Telegram…"
        else:
            self.text = f"Передаю файл: {done / total * 100:.0f}% · {size_text(done)} / {size_text(total)}"
            if elapsed > 0:
                self.text += f"\nСкорость передачи: {size_text(done / elapsed)}/с"

    async def tick(self):
        while True:
            if self.text != self.last_text and time.monotonic() >= self.next_edit:
                try:
                    await self.bot.edit_message_text(chat_id=self.job["chat_id"], message_id=self.job["message_id"],
                                                     text=self.text, reply_markup=cancel_keyboard(self.job["id"]),
                                                     request_timeout=15)
                    self.last_text = self.text
                    self.next_edit = time.monotonic() + self.interval
                except TelegramRetryAfter as error:
                    self.next_edit = time.monotonic() + error.retry_after
                except TelegramAPIError:
                    self.next_edit = time.monotonic() + self.interval
            await asyncio.sleep(0.5)


class ProgressInputFile(FSInputFile):
    def __init__(self, path, filename: str, progress: Progress):
        super().__init__(path, filename=filename)
        self.progress = progress

    async def read(self, bot):
        done, total, started = 0, self.path.stat().st_size, time.monotonic()
        async for chunk in super().read(bot):
            yield chunk
            done += len(chunk)
            self.progress.upload(done, total, time.monotonic() - started)
