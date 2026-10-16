import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from config import Settings

logger = logging.getLogger("ytttbot.downloader")

QUALITIES = (360, 480, 720, 1080)
HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "tiktok.com", "www.tiktok.com",
    "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}


class DownloadError(Exception):
    """Сообщение, которое можно безопасно показать пользователю."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class FileTooLarge(DownloadError):
    pass


def process_failure(stderr: bytes, *, ffmpeg: bool = False) -> DownloadError:
    """Выдаём только заранее заданные сообщения, не раскрывая stderr с секретами."""
    details = stderr.decode("utf-8", errors="replace").lower()
    if ffmpeg:
        code, message = "conversion", "Не удалось преобразовать файл в MP4 или MP3. Попробуйте другой формат."
    elif "http error 429" in details or "too many requests" in details:
        code, message = "rate_limit", "Сайт временно ограничил число запросов. Попробуйте позже."
    elif any(f"http error {code}" in details for code in (500, 502, 503, 504)):
        code, message = "server", "Сайт временно недоступен. Повторяю запрос."
    elif "http error 403" in details:
        code, message = "http_403", "Сайт отклонил запрос на скачивание (HTTP 403). Попробуйте другой ролик или повторите позже."
    elif "http error 404" in details or "video has been removed" in details:
        code, message = "not_found", "Видео не найдено или удалено. Проверьте ссылку."
    elif "impersonat" in details and ("not available" in details or "no impersonate target" in details):
        code, message = "browser_support", "Загрузчику нужна поддержка запросов браузера. Обновите образ бота с зависимостью curl_cffi."
    elif "[tiktok]" in details and any(text in details for text in (
        "unexpected response", "unable to extract", "challenge", "captcha",
    )):
        code, message = "tiktok_response", "TikTok не отдал данные ролика. Проверьте обновление загрузчика и curl_cffi; для некоторых роликов могут понадобиться cookies или прокси."
    elif any(text in details for text in ("private video", "login required", "log in", "sign in")):
        code, message = "authorization", "Для этого ролика сайт требует вход в аккаунт или доступ к закрытому видео."
    elif any(text in details for text in ("not available in your country", "geo-restricted", "not available in your region")):
        code, message = "region", "Видео недоступно в регионе сервера, где работает бот."
    elif "requested format is not available" in details:
        code, message = "format", "Выбранный формат больше недоступен. Отправьте ссылку заново и выберите другое качество."
    elif any(text in details for text in ("timed out", "unable to connect", "name resolution", "connection refused")):
        code, message = "network", "Не удалось подключиться к сайту. Попробуйте позже или проверьте сеть сервера."
    else:
        code, message = "unknown", "Загрузчик не смог обработать ролик. Попробуйте другую ссылку или обновите yt-dlp."
    logger.warning("Сбой обработки файла: %s", code)
    return DownloadError(message, retryable=code in {"network", "rate_limit", "server"})


def extract_url(text: str) -> str:
    links = re.findall(r"https?://[^\s<>]+", text, flags=re.IGNORECASE)
    if len(links) != 1:
        raise DownloadError("Отправьте одну ссылку на видео YouTube или TikTok.")
    url = links[0].rstrip(".,!;)]}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise DownloadError("Некорректная ссылка.") from None
    if (parts.hostname or "").lower() not in HOSTS or parts.username or parts.password or port:
        raise DownloadError("Поддерживаются только ссылки YouTube и TikTok.")
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def resolution(fmt: dict) -> int:
    width, height = fmt.get("width"), fmt.get("height")
    if width and height:
        return int(min(width, height))
    return int(height or 0)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        identifier = parts.path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={identifier}"
    if host.endswith("youtube.com"):
        match = re.match(r"/(?:shorts|embed)/([^/]+)", parts.path)
        identifier = match[1] if match else parse_qs(parts.query).get("v", [None])[0]
        if identifier:
            return f"https://www.youtube.com/watch?v={identifier}"
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


def has_video(fmt: dict) -> bool:
    return fmt.get("vcodec") not in {None, "none"} and resolution(fmt) > 0


def has_audio(fmt: dict) -> bool:
    return fmt.get("acodec") not in {None, "none"}


@dataclass
class Media:
    url: str
    title: str
    duration: float
    formats: list[dict]
    source_id: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.source.lower()}:{self.source_id}" if self.source and self.source_id else normalize_url(self.url)

    def snapshot(self) -> dict:
        fields = {"format_id", "width", "height", "vcodec", "acodec", "ext", "tbr", "abr", "filesize", "filesize_approx"}
        return {"url": normalize_url(self.url), "title": self.title, "duration": self.duration,
                "source_id": self.source_id, "source": self.source,
                "formats": [{k: v for k, v in fmt.items() if k in fields} for fmt in self.formats]}

    def estimate(self, choice: str, bitrate: int = 192) -> int | None:
        if choice == "mp3":
            return round(self.duration * bitrate * 125) if self.duration else None
        ids = self.select(choice).split("+")
        sizes = []
        for identifier in ids:
            fmt = next((f for f in self.formats if str(f.get("format_id")) == identifier), {})
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            if not size and self.duration and fmt.get("tbr"):
                size = self.duration * fmt["tbr"] * 125
            if not size:
                return None
            sizes.append(size)
        return round(sum(sizes))

    @property
    def qualities(self) -> list[int]:
        available = {resolution(fmt) for fmt in self.formats if has_video(fmt)}
        return [quality for quality in QUALITIES if quality in available]

    @property
    def audio_available(self) -> bool:
        return any(has_audio(fmt) for fmt in self.formats)

    def select(self, choice: str) -> str:
        audio = [fmt for fmt in self.formats if has_audio(fmt)]
        audio_only = [fmt for fmt in audio if not has_video(fmt)]
        if choice == "mp3":
            candidates = audio_only or audio
            if not candidates:
                raise DownloadError("У этого ролика нет доступной аудиодорожки.")
            return str(max(candidates, key=lambda f: f.get("abr") or f.get("tbr") or 0)["format_id"])
        if choice not in {str(q) for q in self.qualities}:
            raise DownloadError("Это качество недоступно. Отправьте ссылку ещё раз.")
        video = [f for f in self.formats if has_video(f) and resolution(f) == int(choice)]
        selected = max(video, key=lambda f: (
            str(f.get("vcodec", "")).startswith(("avc1", "h264")),
            f.get("ext") == "mp4", f.get("tbr") or 0,
        ))
        if has_audio(selected):
            return str(selected["format_id"])
        if not audio_only:
            if not audio:
                return str(selected["format_id"])
            raise DownloadError("Не удалось найти звук для выбранного качества.")
        sound = max(audio_only, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0))
        return f"{selected['format_id']}+{sound['format_id']}"


async def stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.wait()


PROGRESS_PREFIX = "__YT_PROGRESS__"


def parse_progress(line: str) -> dict | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    values = line[len(PROGRESS_PREFIX):].strip().split("|")
    if len(values) != 5:
        return None
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) and result >= 0 else None
        except ValueError:
            return None
    done, total, estimate, speed, eta = map(number, values)
    return {"downloaded_bytes": done or 0, "total_bytes": total or estimate, "speed": speed, "eta": eta}


async def run_process(args: list[str], *, timeout: int, directory: Path | None = None,
                      disk_limit: int = 0, on_progress: Callable[[dict], None] | None = None) -> str:
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
    )
    async def read_stream(stream):
        kept = bytearray()
        while line := await stream.readline():
            progress = parse_progress(line.decode("utf-8", errors="replace").strip())
            if progress is not None:
                on_progress(progress)
            else:
                kept.extend(line)
                if len(kept) > 1_000_000:
                    del kept[:-1_000_000]
        return bytes(kept)

    async def communicate_progress():
        output, errors = await asyncio.gather(read_stream(proc.stdout), read_stream(proc.stderr))
        await proc.wait()
        return output, errors

    reader = asyncio.create_task(communicate_progress() if on_progress else proc.communicate())
    try:
        async with asyncio.timeout(timeout):
            while not reader.done():
                await asyncio.wait({reader}, timeout=1)
                if directory and disk_limit:
                    size = 0
                    for path in directory.iterdir():
                        try:
                            if path.is_file():
                                file_size = path.stat().st_size
                                size += file_size
                                if path.name.startswith("result.") and file_size > disk_limit // 3:
                                    raise FileTooLarge("Готовый файл превышает лимит. Выберите меньшее качество.")
                        except FileNotFoundError:
                            continue
                    if size > disk_limit:
                        raise FileTooLarge("Для обработки требуется слишком много места. Выберите меньшее качество.")
            output, stderr = await reader
        if proc.returncode:
            raise process_failure(stderr, ffmpeg=Path(args[0]).stem.lower() == "ffmpeg")
        return output.decode("utf-8", errors="replace").strip()
    except TimeoutError:
        raise DownloadError("Превышено время обработки. Попробуйте более короткий ролик.",
                            retryable=directory is None and Path(args[0]).stem.lower() not in {"ffmpeg", "ffprobe"}) from None
    finally:
        if proc.returncode is None:
            await stop_process(proc)
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    duration: int

    @classmethod
    def from_probe(cls, info: dict) -> "VideoMetadata":
        try:
            stream = info["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            if width <= 0 or height <= 0:
                raise ValueError("invalid dimensions")
            try:
                sar = Fraction(str(stream.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
            except (ValueError, ZeroDivisionError):
                sar = Fraction(1)
            if sar > 0:
                width = max(1, round(width * sar))
            rotation = stream.get("tags", {}).get("rotate", 0)
            for side_data in stream.get("side_data_list", []):
                if "rotation" in side_data:
                    rotation = side_data["rotation"]
                    break
            if round(float(rotation) / 90) % 2:
                width, height = height, width
            duration = float(stream.get("duration") or info.get("format", {}).get("duration") or 0)
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("invalid duration")
            return cls(width, height, math.ceil(duration))
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            raise DownloadError("Не удалось прочитать размеры готового видео.") from None


async def probe_video(path: Path) -> VideoMetadata:
    try:
        output = await run_process([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height,sample_aspect_ratio,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
            "-of", "json", str(path),
        ], timeout=30)
        metadata = VideoMetadata.from_probe(json.loads(output))
    except (DownloadError, ValueError, OSError):
        raise DownloadError("Не удалось прочитать размеры готового видео. Проверьте FFprobe.") from None
    logger.info("Размеры отправляемого видео: %dx%d, длительность: %d с",
                metadata.width, metadata.height, metadata.duration)
    return metadata


@dataclass
class DownloadTimings:
    download: float = 0
    processing: float = 0
    mode: str = ""


def mp4_faststart(path: Path) -> bool:
    """Проверяем расположение moov перед mdat, не читая весь MP4 в память."""
    length = path.stat().st_size
    with path.open("rb") as file:
        for _ in range(128):
            offset = file.tell()
            header = file.read(8)
            if len(header) != 8:
                return False
            size, kind = int.from_bytes(header[:4], "big"), header[4:]
            header_size = 8
            if size == 1:
                extended = file.read(8)
                if len(extended) != 8:
                    return False
                size, header_size = int.from_bytes(extended, "big"), 16
            if size < header_size or offset + size > length:
                return False
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            file.seek(offset + size)
    return False


def video_encoding(streams: list[dict]) -> tuple[list[str], bool, str]:
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        raise DownloadError("В скачанном файле нет видеодорожки.")
    copy_video = video.get("codec_name") == "h264" and video.get("pix_fmt") in {"yuv420p", "yuvj420p"}
    copy_audio = audio is None or audio.get("codec_name") == "aac"
    args = ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy" if copy_video else "libx264"]
    if not copy_video:
        args += ["-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "copy" if copy_audio else "aac"]
    if not copy_audio:
        args += ["-b:a", "192k"]
    args += ["-movflags", "+faststart"]
    mode = "copy" if copy_video and copy_audio else ("audio_encode" if copy_video else "video_encode")
    return args, copy_video and copy_audio, mode


class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def base_args(self) -> list[str]:
        args = [
            sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
            "--no-warnings", "--no-progress", "--socket-timeout", "30",
            "--retries", "2", "--extractor-retries", "2", "--encoding", "utf-8",
        ]
        if self.settings.cookies_file:
            args += ["--cookies", str(self.settings.cookies_file)]
        if self.settings.proxy:
            args += ["--proxy", self.settings.proxy]
        return args

    async def inspect(self, url: str) -> Media:
        output = await run_process(self.base_args() + ["--dump-single-json", "--skip-download", "--", url], timeout=120)
        try:
            info = json.loads(output)
        except (ValueError, TypeError):
            raise DownloadError("Не удалось прочитать информацию о ролике.") from None
        if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
            raise DownloadError("Отправьте ссылку на один ролик, а не на плейлист или профиль.")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise DownloadError("Прямые трансляции пока не поддерживаются.")
        formats = info.get("formats") or [info]
        media = Media(info.get("webpage_url") or url, str(info.get("title") or "Видео")[:200],
                      float(info.get("duration") or 0), formats, str(info.get("id") or ""),
                      str(info.get("extractor_key") or info.get("extractor") or ""))
        if not media.qualities and not media.audio_available:
            raise DownloadError("Нет доступных форматов 360p–1080p или MP3.")
        return media

    async def download(self, media: Media, choice: str, directory: Path, *,
                       timings: DownloadTimings | None = None,
                       on_processing: Callable[[], Awaitable[None]] | None = None,
                       on_progress: Callable[[dict], None] | None = None) -> Path:
        settings = self.settings
        timings = timings if timings is not None else DownloadTimings()
        format_id = media.select(choice)
        if choice == "mp3" and media.duration * settings.mp3_bitrate * 125 > settings.max_file_bytes:
            raise FileTooLarge("Аудио превышает лимит размера файла.")
        if shutil.disk_usage(directory).free < settings.max_file_bytes * 4:
            raise DownloadError("На сервере недостаточно свободного места. Попробуйте позже.")
        args = self.base_args() + [
            "--format", format_id, "--max-filesize", str(settings.max_file_bytes),
            "--output", str(directory / "source.%(ext)s"), "--merge-output-format", "mkv",
            "--print", "after_move:filepath", "--no-simulate", "--", media.url,
        ]
        if on_progress:
            args[3:3] = ["--newline", "--progress", "--progress-template",
                "download:" + PROGRESS_PREFIX + "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"]
            args.remove("--no-progress")
        try:
            async with asyncio.timeout(settings.job_timeout):
                started = time.perf_counter()
                output = await run_process(args, timeout=settings.job_timeout, directory=directory,
                                           disk_limit=settings.max_file_bytes * 3, on_progress=on_progress)
                timings.download = time.perf_counter() - started
                lines = output.splitlines()
                if not lines:
                    raise FileTooLarge("Файл превышает лимит. Выберите меньшее качество.")
                source = Path(lines[-1]).resolve()
                if source.parent != directory.resolve() or not source.is_file():
                    raise DownloadError("Загрузчик не создал файл.")
                if source.stat().st_size > settings.max_file_bytes:
                    raise FileTooLarge("Файл превышает лимит. Выберите меньшее качество.")
                if on_processing:
                    await on_processing()
                started = time.perf_counter()
                destination = directory / ("result.mp3" if choice == "mp3" else "result.mp4")
                if choice == "mp3":
                    encoding = ["-vn", "-c:a", "libmp3lame", "-b:a", f"{settings.mp3_bitrate}k"]
                    timings.mode = "mp3_encode"
                else:
                    probe = await run_process([
                        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
                        "-of", "json", str(source),
                    ], timeout=30)
                    try:
                        encoding, compatible, timings.mode = video_encoding(json.loads(probe)["streams"])
                    except (ValueError, KeyError, TypeError):
                        raise DownloadError("Не удалось определить кодеки скачанного видео.") from None
                    if compatible and source.suffix.lower() == ".mp4" and mp4_faststart(source):
                        source.replace(destination)
                        timings.processing = time.perf_counter() - started
                        timings.mode = "ready_mp4"
                        logger.info("Подготовка: mode=%s download=%.2fs processing=%.2fs",
                                    timings.mode, timings.download, timings.processing)
                        return destination
                await run_process(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                     *encoding, str(destination)],
                    timeout=settings.job_timeout, directory=directory, disk_limit=settings.max_file_bytes * 3,
                )
                if not destination.is_file() or not destination.stat().st_size:
                    raise DownloadError("Не удалось создать итоговый файл.")
                if destination.stat().st_size > settings.max_file_bytes:
                    raise FileTooLarge("Готовый файл превышает лимит. Выберите меньшее качество.")
                source.unlink()
                timings.processing = time.perf_counter() - started
                logger.info("Подготовка: mode=%s download=%.2fs processing=%.2fs",
                            timings.mode, timings.download, timings.processing)
                return destination
        except TimeoutError:
            raise DownloadError("Превышено время обработки ролика.") from None
