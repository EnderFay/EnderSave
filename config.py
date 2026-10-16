import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name}: требуется целое число") from None
    if value <= 0:
        raise ValueError(f"{name}: число должно быть больше нуля")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_base: str
    download_dir: Path
    max_file_bytes: int
    concurrency: int
    max_users: int
    job_timeout: int
    upload_timeout: int
    mp3_bitrate: int
    cookies_file: Path | None
    proxy: str | None
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433
    postgres_db: str = "ytttbot"
    postgres_user: str = "ytttbot"
    postgres_password: str = field(default="", repr=False)
    retry_attempts: int = 3
    progress_interval: int = 3

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Добавьте BOT_TOKEN в .env")
        api_base = os.getenv("BOT_API_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
        address = urlsplit(api_base)
        if address.scheme not in {"http", "https"} or not address.hostname:
            raise ValueError("BOT_API_BASE_URL: некорректный адрес")
        if address.hostname.lower() == "api.telegram.org":
            raise ValueError("Для файлов до 1 ГБ нужен собственный Bot API Server")
        max_bytes = positive_int("MAX_FILE_BYTES", 1_000_000_000)
        if max_bytes > 2_000_000_000:
            raise ValueError("MAX_FILE_BYTES превышает лимит локального Bot API: 2000 МБ")
        bitrate = positive_int("MP3_BITRATE", 192)
        if bitrate not in {64, 96, 128, 160, 192, 256, 320}:
            raise ValueError("MP3_BITRATE: выберите 64, 96, 128, 160, 192, 256 или 320")
        cookies = os.getenv("COOKIES_FILE", "").strip()
        cookies_path = (ROOT / cookies).resolve() if cookies else None
        if cookies_path and not cookies_path.is_file():
            raise ValueError("COOKIES_FILE: файл не найден")
        return cls(
            bot_token=token,
            api_base=api_base,
            download_dir=(ROOT / os.getenv("DOWNLOAD_DIR", "downloads")).resolve(),
            max_file_bytes=max_bytes,
            concurrency=positive_int("MAX_CONCURRENT_DOWNLOADS", 2),
            max_users=positive_int("MAX_ACTIVE_USERS", 20),
            job_timeout=positive_int("JOB_TIMEOUT_SECONDS", 3600),
            upload_timeout=positive_int("UPLOAD_TIMEOUT_SECONDS", 3600),
            mp3_bitrate=bitrate,
            cookies_file=cookies_path,
            proxy=os.getenv("DOWNLOAD_PROXY") or None,
            postgres_host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=positive_int("POSTGRES_PORT", 5433),
            postgres_db=os.getenv("POSTGRES_DB", "ytttbot"),
            postgres_user=os.getenv("POSTGRES_USER", "ytttbot"),
            postgres_password=os.getenv("POSTGRES_PASSWORD", ""),
            retry_attempts=positive_int("RETRY_ATTEMPTS", 3),
            progress_interval=positive_int("PROGRESS_INTERVAL_SECONDS", 3),
        )
