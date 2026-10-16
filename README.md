# YouTube / TikTok Telegram Bot

Telegram-бот для скачивания видео из YouTube, Shorts и TikTok.

**Стек:** Python 3.12, aiogram 3, yt-dlp, FFmpeg, Deno, PostgreSQL 17, Docker Compose.

## Возможности

- Выбор доступного качества: 360p, 480p, 720p, 1080p или MP3.
- Файлы до 1 ГБ через собственный Telegram Bot API Server.
- Повторная отправка по `file_id` без скачивания.
- Прогресс, отмена запроса и повторы временных сетевых ошибок.
- Постоянная очередь и история запросов в PostgreSQL.

Видео хранятся в `downloads/` только на время обработки. В базе сохраняются
метаданные, задания и идентификаторы файлов Telegram. Плейлисты и прямые эфиры
не поддерживаются.

## Запуск

Нужен Docker с поддержкой Linux containers и Docker Compose.

При первом запуске скопируйте `.env.example` в `.env` и заполните:

```dotenv
BOT_TOKEN=...
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
POSTGRES_PASSWORD=...
```

Токен выдаёт [BotFather](https://t.me/BotFather), `api_id` и `api_hash` доступны
на [my.telegram.org](https://my.telegram.org). Для базы задайте случайный пароль.
Остальные настройки и значения по умолчанию — в `.env.example`.

```bash
docker compose up --build -d
docker compose logs -f bot
```

Compose запускает бота, Bot API Server и PostgreSQL. Таблицы создаются автоматически.
Первая сборка может занять время: Bot API Server собирается из исходного кода.
После запуска отправьте боту `/start` и ссылку на видео.

Приветственный баннер находится в `assets/endersave-welcome.png`, текст описания
для BotFather — в `assets/bot-description.txt`. По `/start` бот отправляет баннер
с описанием возможностей; картинка карточки Welcome message задаётся в BotFather отдельно.

Если бот ранее работал через `api.telegram.org`, остановите старую копию и перед
переходом на собственный Bot API один раз выполните:

```bash
docker compose run --rm --no-deps bot python bot.py --logout-cloud
```

## Команды

В Telegram: `/start`, `/help`, `/history`.

В личных сообщениях достаточно ссылки. В группе обращайтесь к боту явно:
`@EnderYtTtDownloaderBot https://vt.tiktok.com/ZSgF21D2P/` (используйте username
своего бота). Обычная переписка и ссылки без упоминания игнорируются.
Команды в группе также адресуются явно: `/help@EnderYtTtDownloaderBot`.

Для получения сообщений вида `@бот ссылка` отключите Group Privacy в BotFather
(`/setprivacy` → ваш бот → Disable), затем удалите и заново добавьте бота в группу.
Telegram будет передавать сообщения приложению, но фильтр пропустит в обработку
только обращения к этому боту. При включённой приватности можно использовать
`/download@EnderYtTtDownloaderBot ссылка`. Права администратора не требуются,
но бот должен иметь разрешение отправлять сообщения и видео.

```bash
docker compose ps                    # состояние сервисов
docker compose stop bot              # остановить только бота
docker compose start bot             # запустить снова
docker compose down                 # остановить все сервисы, сохранив данные
docker compose up --build -d         # пересобрать и применить обновление
```

PostgreSQL доступен на `127.0.0.1:5433`, Bot API — на `127.0.0.1:8081`.
Данные PostgreSQL хранятся в volume `postgres-data`. Команда `down -v` удалит
volumes вместе с историей и кэшем.

## Структура

```text
bot.py          — команды и кнопки
service.py      — выполнение заданий, отправка, отмена
storage.py      — очередь, история и кэш PostgreSQL
downloader.py   — yt-dlp, FFmpeg и FFprobe
progress.py     — отображение прогресса
config.py       — настройки
schema.sql      — схема базы
tests/          — модульные и интеграционные тесты
```

При перезапуске проверка и скачивание восстанавливаются из очереди. Если соединение
оборвалось во время отправки, бот попросит проверить получение файла: такой запрос
автоматически не повторяется, чтобы избежать дублей.

## Тесты

В локальном окружении Python 3.12 с установленными зависимостями:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Полный набор, включая PostgreSQL в отдельном временном контейнере:

```bash
python tests/run_postgres_tests.py
```

Интеграционные тесты требуют работающий Docker. Рабочая база и Telegram не используются.
