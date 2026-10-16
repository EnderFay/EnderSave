from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.types import Message, MessageEntity, Update, User

from bot import AddressedMessage, Application
from test_downloader import settings

USERNAME = "EnderYtTtDownloaderBot"
LINK = "https://vt.tiktok.com/ZSgF21D2P/"


def message(text, chat_type="supergroup", entity_type="mention", target=None, prefix=""):
    entities = []
    if target:
        offset = len(prefix.encode("utf-16-le")) // 2
        entities.append(MessageEntity(type=entity_type, offset=offset, length=len(target)))
    return Message(message_id=1, date=datetime.now(timezone.utc),
        chat={"id": 42 if chat_type == "private" else -100123, "type": chat_type},
        from_user={"id": 42, "is_bot": False, "first_name": "User"}, text=text, entities=entities)


class AddressingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456:dummy")
        self.bot.me = AsyncMock(return_value=User(id=123456, is_bot=True, first_name="EnderSave", username=USERNAME))
        self.addAsyncCleanup(self.bot.session.close)
        self.filter = AddressedMessage()

    async def test_private_link_needs_no_mention(self):
        self.assertTrue(await self.filter(message(LINK, "private"), self.bot))
        self.bot.me.assert_not_awaited()

    async def test_group_chatter_and_plain_links_are_ignored(self):
        for text in ("Всем привет", LINK, "@" + USERNAME):
            self.assertFalse(await self.filter(message(text), self.bot))
        self.bot.me.assert_not_awaited()

    async def test_group_explicit_mention_is_accepted(self):
        target = "@" + USERNAME
        for chat_type in ("group", "supergroup"):
            self.assertTrue(await self.filter(message(f"{target} {LINK}", chat_type, target=target), self.bot))

    async def test_other_bot_and_similar_username_are_ignored(self):
        for target in ("@OtherBot", "@" + USERNAME + "Extra"):
            self.assertFalse(await self.filter(message(f"{target} {LINK}", target=target), self.bot))

    async def test_mention_is_case_insensitive_and_unicode_offsets_work(self):
        target, prefix = "@" + USERNAME.lower(), "🎬 Привет! "
        self.assertTrue(await self.filter(message(f"{prefix}{target} {LINK}", target=target, prefix=prefix), self.bot))

    async def test_username_in_link_is_not_a_mention(self):
        text = f"https://example.test/@{USERNAME} {LINK}"
        self.assertFalse(await self.filter(message(text, entity_type="url", target=text.split()[0]), self.bot))

    async def test_only_explicit_group_commands_are_accepted(self):
        for command in ("start", "help", "history", "download"):
            target = f"/{command}@{USERNAME}"
            self.assertTrue(await self.filter(message(target, entity_type="bot_command", target=target), self.bot))
            self.assertFalse(await self.filter(message(f"/{command}", entity_type="bot_command", target=f"/{command}"), self.bot))

    async def test_dispatcher_does_not_touch_database_or_reply_to_group_chatter(self):
        store = AsyncMock()
        app = Application(settings(Path(".")), store, self.bot)
        dp = Dispatcher()
        dp.include_router(app.router)
        for number, text in enumerate(("Всем привет", LINK, "/history", "/start"), 1):
            msg = message(text, entity_type="bot_command", target=text if text.startswith("/") else None)
            await dp.feed_update(self.bot, Update(update_id=number, message=msg))
        store.create.assert_not_awaited()
        store.history.assert_not_awaited()
        self.bot.me.assert_awaited()  # только для явно оформленных команд

    async def test_dispatcher_routes_mentioned_link_to_existing_queue(self):
        store = AsyncMock()
        store.create.return_value = {"id": 1, "chat_id": -100123, "message_id": 9}
        app = Application(settings(Path(".")), store, self.bot)
        app.service.edit = AsyncMock()
        self.bot.session = AsyncMock(return_value=Message(message_id=9, date=datetime.now(timezone.utc),
            chat={"id": -100123, "type": "supergroup"}))
        dp = Dispatcher()
        dp.include_router(app.router)
        target = "@" + USERNAME
        await dp.feed_update(self.bot, Update(update_id=1, message=message(f"{target} {LINK}", target=target)))
        store.create.assert_awaited_once_with(42, -100123, 9, LINK, 20)

