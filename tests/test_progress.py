import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from downloader import Media, normalize_url, parse_progress, process_failure, run_process
from progress import Progress, ProgressInputFile
from test_downloader import video, sound


class MetadataCacheTests(unittest.TestCase):
    def test_youtube_aliases_have_same_key(self):
        for url in ("https://youtu.be/abcd?si=tracking", "https://youtube.com/shorts/abcd?si=other",
                    "https://www.youtube.com/watch?v=abcd&list=123"):
            self.assertEqual(normalize_url(url), "https://www.youtube.com/watch?v=abcd")

    def test_snapshots_exclude_credentials_and_cdn_urls(self):
        fmt = video(audio="aac") | {"url": "https://signed.example/?secret=x", "http_headers": {"Cookie": "SECRET"}}
        media = Media("https://youtu.be/abcd?si=tracking", "title", 60, [fmt], "abcd", "Youtube")
        snapshot = media.snapshot()
        self.assertNotIn("secret", str(snapshot).lower())
        self.assertEqual(Media(**snapshot).key, media.key)

    def test_mp3_size_estimate(self):
        self.assertEqual(Media("url", "title", 60, [sound()]).estimate("mp3", 192), 1_440_000)

    def test_video_estimate_sums_separate_tracks(self):
        media = Media("url", "title", 10, [video() | {"filesize": 1000}, sound() | {"filesize": 200}])
        self.assertEqual(media.estimate("1080"), 1200)

    def test_retryable_codes(self):
        for error in (b"HTTP Error 503", b"HTTP Error 429", b"Connection refused"):
            self.assertTrue(process_failure(error).retryable)
        for error in (b"HTTP Error 403", b"private video", b"HTTP Error 404"):
            self.assertFalse(process_failure(error).retryable)

    def test_progress_parses_unknown_total(self):
        p = parse_progress("__YT_PROGRESS__100|NA|NA|50|NA")
        self.assertEqual(p["downloaded_bytes"], 100)
        self.assertIsNone(p["total_bytes"])
        self.assertEqual(p["speed"], 50)


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_progress_does_not_pollute_filepath(self):
        import sys
        seen = []
        code = "import sys; print('__YT_PROGRESS__10|20|NA|5|2',file=sys.stderr); print('/tmp/source.mp4')"
        output = await run_process([sys.executable, "-c", code], timeout=5, on_progress=seen.append)
        self.assertEqual(output, "/tmp/source.mp4")
        self.assertEqual(seen[0]["total_bytes"], 20)

    async def test_upload_progress_and_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "file.mp4"
            content = b"0123456789" * 10_000
            path.write_bytes(content)
            progress = Progress(AsyncMock(), {"id": 1})
            upload = ProgressInputFile(path, "file.mp4", progress)
            result = b"".join([chunk async for chunk in upload.read(None)])
            self.assertEqual(result, content)
            self.assertIn("Ожидаю подтверждения", progress.text)

    async def test_progress_updates_are_throttled(self):
        bot = AsyncMock()
        p = Progress(bot, {"id": 1, "chat_id": 42, "message_id": 9}, interval=3)
        async with p:
            for i in range(6):
                p.download({"downloaded_bytes": i, "total_bytes": 10})
                await asyncio.sleep(0.1)
        self.assertLessEqual(bot.edit_message_text.await_count, 1)


if __name__ == "__main__":
    unittest.main()
