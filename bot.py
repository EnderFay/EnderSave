import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import Settings
from downloader import DownloadError, extract_url
from progress import cancel_keyboard
from service import JobService
from storage import BusyError, Store

logger = logging.getLogger("ytttbot")


class AddressedMessage(Filter):
    """В группах пропускаем только явное обращение к этому боту."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if not message.from_user or message.from_user.is_bot:
            return False
        if message.chat.type == ChatType.PRIVATE:
            return True
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return False
        entities = [entity for entity in message.entities or [] if entity.type in {
            MessageEntityType.MENTION, MessageEntityType.BOT_COMMAND, MessageEntityType.TEXT_MENTION,
        }]
        if not entities:
            return False
        identity = await bot.me()
        username = (identity.username or "").casefold()
        for entity in entities:
            if entity.type == MessageEntityType.TEXT_MENTION:
                if entity.user and entity.user.id == identity.id:
                    return True
                continue
            value = entity.extract_from(message.text or "")
            if entity.type == MessageEntityType.MENTION and username and value.casefold() == f"@{username}":
                return True
            if entity.type == MessageEntityType.BOT_COMMAND and username and value.partition("@")[2].casefold() == username:
                return True
        return False


class Application:
    def __init__(self, settings, store, bot):
        self.settings, self.store, self.bot = settings, store, bot
        self.service = JobService(settings, store, bot)
        self.router = Router()
        self.router.message.filter(AddressedMessage())
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.start, Command("help"))
        self.router.message.register(self.history, Command("history"))
        self.router.callback_query.register(self.choose, F.data.startswith("download:"))
        self.router.callback_query.register(self.cancel, F.data.startswith("cancel:"))
        self.router.message.register(self.receive, F.text)

    async def start(self, message: Message):
        if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            identity = await self.bot.me()
            instruction = f"В группе отправь @{identity.username} и ссылку на ролик одним сообщением."
        else:
            instruction = "Отправь ссылку на ролик и выбери формат — остальное я сделаю сам!"
        text = (
            "👋 Привет! Я EnderSave.\n\n"
            "Сохраняю видео из YouTube, Shorts и TikTok прямо в Telegram.\n\n"
            "🎬 Выбирай доступное качество: 360p, 480p, 720p или 1080p.\n"
            "🎵 Нужен только звук? Скачай его в MP3.\n"
            f"📦 Отправляю файлы до {self.settings.max_file_bytes / 1_000_000_000:g} ГБ.\n"
            "⏳ Показываю прогресс и позволяю отменить запрос.\n"
            "📋 Твои последние запросы — /history.\n\n"
            f"{instruction}"
        )
        banner = Path(__file__).resolve().parent / "assets" / "endersave-welcome.png"
        if banner.is_file():
            try:
                await message.answer_photo(photo=FSInputFile(banner), caption=text, request_timeout=30)
                return
            except TelegramAPIError:
                logger.warning("Не удалось отправить приветственный баннер")
        await message.answer(text)

    async def receive(self, message: Message):
        if not message.from_user:
            return
        try:
            url = extract_url(message.text or "")
        except DownloadError as error:
            await message.answer(str(error))
            return
        status = await message.answer("Принимаю запрос…")
        try:
            async with self.service.accept_lock:
                job = await self.store.create(message.from_user.id, message.chat.id, status.message_id,
                                              url, self.settings.max_users)
                await self.service.edit(job, "Запрос сохранён в очереди…", cancel_keyboard(job["id"]))
        except BusyError as error:
            await status.edit_text(str(error))
            return
        except Exception as error:
            logger.error("Не удалось принять запрос: %s", type(error).__name__)
            await status.edit_text("База данных временно недоступна. Попробуй позже.")
            return

    async def choose(self, callback: CallbackQuery):
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not isinstance(callback.message, Message):
            await callback.answer("Некорректная кнопка", show_alert=True)
            return
        job = await self.store.get(int(parts[1]))
        if not job or job["owner"] != callback.from_user.id or job["chat_id"] != callback.message.chat.id or job["state"] != "choosing":
            await callback.answer("Кнопка устарела или принадлежит другому пользователю.", show_alert=True)
            return
        media = await self.store.media(job["media_key"])
        choice = parts[2]
        allowed = [str(q) for q in media.qualities] + (["mp3"] if media.audio_available else [])
        if choice not in allowed:
            await callback.answer("Формат недоступен.", show_alert=True)
            return
        async with self.service.accept_lock:
            accepted = await self.store.enqueue(job["id"], job["owner"], job["chat_id"], choice)
            if accepted:
                await self.service.edit(job, "Запрос сохранён в очереди…", cancel_keyboard(job["id"]))
        if not accepted:
            await callback.answer("Меню устарело или уже есть активный запрос. Пришли ссылку заново.", show_alert=True)
            return
        await callback.answer("Запрос принят")

    async def cancel(self, callback: CallbackQuery):
        parts = (callback.data or "").split(":")
        if len(parts) != 2 or not parts[1].isdigit() or not isinstance(callback.message, Message):
            await callback.answer("Некорректная кнопка", show_alert=True)
            return
        state = await self.store.cancel(int(parts[1]), callback.from_user.id, callback.message.chat.id)
        if state is None:
            await callback.answer("Запрос уже завершён или принадлежит другому пользователю.", show_alert=True)
        else:
            await callback.answer("Отменено" if state == "cancelled" else "Останавливаю запрос…")
            if state == "cancelled":
                job = await self.store.get(int(parts[1]))
                await self.service.cleanup(job.get("temp_path"), job["id"])
                await callback.message.edit_text("Запрос отменён.")

    async def history(self, message: Message):
        if not message.from_user:
            return
        jobs = await self.store.history(message.from_user.id)
        labels = {"queued": "в очереди", "running": "обрабатывается", "choosing": "ожидает выбора",
                  "sending": "отправляется", "done": "отправлено", "cancelled": "отменено",
                  "failed": "ошибка", "unknown": "проверь получение", "expired": "меню устарело"}
        lines = ["Последние запросы:"]
        for job in jobs:
            title = (job["title"] or "Проверка ссылки")[:100]
            lines.append(f"#{job['id']} · {title}\n{job['choice'] or '—'} · {labels[job['state']]}")
        await message.answer("\n\n".join(lines) if jobs else "История пока пуста.")


def check_dependencies():
    missing = [name for name in ("ffmpeg", "ffprobe", "deno") if not shutil.which(name)]
    if missing:
        raise ValueError("Не найдены программы: " + ", ".join(missing))


async def run(settings, logout_cloud=False):
    if logout_cloud:
        async with Bot(settings.bot_token) as cloud:
            await cloud.log_out()
        print("Выход из облачного Bot API выполнен.")
        return
    check_dependencies()
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    session = AiohttpSession(api=TelegramAPIServer.from_base(settings.api_base), timeout=settings.upload_timeout)
    async with Bot(settings.bot_token, session=session) as bot:
        identity = await bot.get_me(request_timeout=30)
        store = await Store.open(settings, identity.id)
        app = Application(settings, store, bot)
        try:
            await app.service.start()
            dp = Dispatcher()
            dp.include_router(app.router)
            logger.info("Бот запущен. Постоянная очередь PostgreSQL подключена.")
            await dp.start_polling(bot, close_bot_session=False)
        finally:
            await app.service.stop()
            await store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--logout-cloud", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
        if args.check:
            check_dependencies()
            if not settings.postgres_password:
                raise ValueError("Добавьте POSTGRES_PASSWORD в .env")
            print("Конфигурация и программы в порядке. Подключение к Telegram и PostgreSQL не проверялось.")
        else:
            asyncio.run(run(settings, args.logout_cloud))
    except ValueError as error:
        print(f"Ошибка настроек: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"Ошибка запуска ({type(error).__name__}). Проверьте PostgreSQL, Bot API Server и .env.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
