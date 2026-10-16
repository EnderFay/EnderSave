import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config import Settings
from downloader import DownloadError, Downloader, DownloadTimings, FileTooLarge, Media, VideoMetadata, extract_url, process_failure, run_process, video_encoding

COMPATIBLE_STREAMS = [
    {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
    {"codec_type": "audio", "codec_name": "aac"},
]


def settings(root: Path) -> Settings:
    return Settings("123456:dummy", "http://localhost:8081", root, 1_000_000_000,
                    2, 20, 60, 60, 192, None, None)


def video(identifier="v", width=1920, height=1080, audio="none", codec="avc1.123"):
    return dict(format_id=identifier, width=width, height=height, vcodec=codec,
                acodec=audio, ext="mp4", tbr=1000)


def sound(identifier="a"):
    return dict(format_id=identifier, vcodec="none", acodec="aac", ext="m4a", abr=192)


class LinkTests(unittest.TestCase):
    def test_normal_link_in_message(self):
        self.assertEqual(extract_url("Вот https://youtu.be/abc."), "https://youtu.be/abc")

    def test_short_tiktok(self):
        self.assertEqual(extract_url("https://vm.tiktok.com/abc/"), "https://vm.tiktok.com/abc/")

    def test_untrusted_hosts_and_credentials(self):
        for url in ["https://youtube.com.evil.test/a", "https://evil.test/a",
                    "https://user:pass@youtube.com/a", "https://youtube.com:123/a",
                    "https://youtube.com:invalid/a", "https://[broken", "file:///tmp/a"]:
            with self.subTest(url=url), self.assertRaises(DownloadError):
                extract_url(url)

    def test_multiple_links_rejected(self):
        with self.assertRaises(DownloadError):
            extract_url("https://youtu.be/a https://youtu.be/b")


class FormatTests(unittest.TestCase):
    def test_portrait_uses_short_dimension(self):
        media = Media("url", "title", 10, [video(width=1080, height=1920), sound()])
        self.assertEqual(media.qualities, [1080])
        self.assertEqual(media.select("1080"), "v+a")

    def test_audio_only_does_not_advertise_video(self):
        media = Media("url", "title", 10, [sound()])
        self.assertEqual(media.qualities, [])
        self.assertTrue(media.audio_available)
        self.assertEqual(media.select("mp3"), "a")

    def test_combined_video_does_not_duplicate_audio(self):
        media = Media("url", "title", 10, [video(audio="aac"), sound()])
        self.assertEqual(media.select("1080"), "v")

    def test_missing_quality_is_not_upscaled(self):
        media = Media("url", "title", 10, [video(), sound()])
        with self.assertRaises(DownloadError):
            media.select("480")

    def test_h264_preferred_to_vp9(self):
        media = Media("url", "title", 10, [video("vp9", codec="vp9"), video("h264"), sound()])
        self.assertEqual(media.select("1080"), "h264+a")

    def test_video_only_has_no_mp3(self):
        media = Media("url", "title", 10, [video()])
        self.assertFalse(media.audio_available)
        with self.assertRaises(DownloadError):
            media.select("mp3")


class VideoMetadataTests(unittest.TestCase):
    def test_portrait_dimensions_preserved(self):
        info = {"streams": [{"width": 720, "height": 1280, "sample_aspect_ratio": "1:1"}],
                "format": {"duration": "149.653"}}
        self.assertEqual(VideoMetadata.from_probe(info), VideoMetadata(720, 1280, 150))

    def test_landscape_dimensions_preserved(self):
        self.assertEqual(VideoMetadata.from_probe({"streams": [{"width": 1920, "height": 1080}]}),
                         VideoMetadata(1920, 1080, 0))

    def test_rotation_swaps_dimensions(self):
        for rotation in (-90, 90, 270):
            with self.subTest(rotation=rotation):
                info = {"streams": [{"width": 1920, "height": 1080, "side_data_list": [{"rotation": rotation}]}]}
                self.assertEqual(VideoMetadata.from_probe(info), VideoMetadata(1080, 1920, 0))

    def test_sample_aspect_ratio_used_for_display_dimensions(self):
        info = {"streams": [{"width": 720, "height": 576, "sample_aspect_ratio": "16:15"}]}
        self.assertEqual(VideoMetadata.from_probe(info), VideoMetadata(768, 576, 0))

    def test_unknown_aspect_ratio_defaults_to_square_pixels(self):
        info = {"streams": [{"width": 720, "height": 1280, "sample_aspect_ratio": "N/A"}]}
        self.assertEqual(VideoMetadata.from_probe(info), VideoMetadata(720, 1280, 0))

    def test_invalid_probe_rejected(self):
        for info in ({}, {"streams": []}, {"streams": [{"width": 0, "height": 1280}]},
                     {"streams": [{"width": 720, "height": 1280}], "format": {"duration": "nan"}}):
            with self.subTest(info=info), self.assertRaises(DownloadError):
                VideoMetadata.from_probe(info)


class EncodingTests(unittest.TestCase):
    def test_h264_and_aac_are_copied(self):
        args, compatible, mode = video_encoding(COMPATIBLE_STREAMS)
        self.assertTrue(compatible)
        self.assertEqual(mode, "copy")
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertNotIn("-b:a", args)

    def test_only_opus_audio_is_converted(self):
        args, compatible, mode = video_encoding([COMPATIBLE_STREAMS[0], {"codec_type": "audio", "codec_name": "opus"}])
        self.assertFalse(compatible)
        self.assertEqual(mode, "audio_encode")
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")

    def test_incompatible_video_is_converted_but_aac_is_copied(self):
        for codec, pixel_format in (("vp9", "yuv420p"), ("h264", "yuv420p10le")):
            with self.subTest(codec=codec, pixel_format=pixel_format):
                args, compatible, mode = video_encoding([
                    {"codec_type": "video", "codec_name": codec, "pix_fmt": pixel_format}, COMPATIBLE_STREAMS[1]])
                self.assertFalse(compatible)
                self.assertEqual(mode, "video_encode")
                self.assertEqual(args[args.index("-c:v") + 1], "libx264")
                self.assertEqual(args[args.index("-c:a") + 1], "copy")

    def test_silent_video_supported(self):
        _, compatible, _ = video_encoding([COMPATIBLE_STREAMS[0]])
        self.assertTrue(compatible)


class DiagnosticTests(unittest.TestCase):
    def test_tiktok_response_distinguished_from_authorization(self):
        error = process_failure(b"ERROR: [TikTok] 123: Unexpected response from webpage request")
        self.assertIn("TikTok", str(error))
        self.assertIn("curl_cffi", str(error))

    def test_http_codes_are_distinct(self):
        for status, expected in ((403, "403"), (404, "удалено"), (429, "число запросов")):
            with self.subTest(status=status):
                self.assertIn(expected, str(process_failure(f"HTTP Error {status}".encode())))

    def test_ffmpeg_error_is_conversion_error(self):
        self.assertIn("преобразовать", str(process_failure(b"Invalid data found", ffmpeg=True)))

    def test_stderr_never_exposed_in_message_or_logs(self):
        secret = "https://user:SECRET@example.test/private?token=HIDDEN"
        for details in (secret, "HTTP Error 403 " + secret, "[TikTok] Unexpected response " + secret):
            with self.subTest(details=details), self.assertLogs("ytttbot.downloader", level="WARNING") as captured:
                message = str(process_failure(details.encode()))
            output = message + " ".join(captured.output)
            self.assertNotIn("SECRET", output)
            self.assertNotIn("HIDDEN", output)
            self.assertNotIn("example.test", output)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_subprocess_output(self):
        output = await run_process([sys.executable, "-c", "print('ok')"], timeout=5)
        self.assertEqual(output, "ok")

    async def test_failure_does_not_leak_stderr(self):
        with self.assertRaises(DownloadError) as result:
            await run_process([sys.executable, "-c", "import sys; sys.stderr.write('SECRET'); sys.exit(1)"], timeout=5)
        self.assertNotIn("SECRET", str(result.exception))

    async def test_timeout_stops_subprocess(self):
        with self.assertRaises(DownloadError):
            await asyncio.wait_for(run_process(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=1), timeout=10)

    async def test_cancellation_stops_subprocess(self):
        task = asyncio.create_task(run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=60))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_mp3_estimated_size_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = Downloader(replace(settings(Path(root)), max_file_bytes=10))
            with self.assertRaises(FileTooLarge), patch("downloader.run_process", new_callable=AsyncMock) as run:
                await downloader.download(Media("url", "title", 10, [sound()]), "mp3", Path(root))
            run.assert_not_called()

    async def test_live_and_playlists_rejected(self):
        import json
        for info in [{"is_live": True}, {"_type": "playlist", "entries": []}]:
            with tempfile.TemporaryDirectory() as root:
                downloader = Downloader(settings(Path(root)))
                with patch("downloader.run_process", new_callable=AsyncMock, return_value=json.dumps(info)):
                    with self.assertRaises(DownloadError):
                        await downloader.inspect("https://youtu.be/a")

    async def test_final_size_checked_after_conversion(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            downloader = Downloader(replace(settings(directory), max_file_bytes=100))

            async def fake_run(args, **kwargs):
                if args[0] == "ffprobe":
                    return json.dumps({"streams": COMPATIBLE_STREAMS})
                if args[0] == "ffmpeg":
                    Path(args[-1]).write_bytes(b"x" * 101)
                    return ""
                source = directory / "source.mp4"
                source.write_bytes(b"x" * 10)
                return str(source)

            with patch("downloader.run_process", side_effect=fake_run):
                with self.assertRaises(FileTooLarge):
                    await downloader.download(Media("url", "title", 1, [video(audio="aac")]), "1080", directory)

    async def test_success_returns_complete_file(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            downloader = Downloader(replace(settings(directory), max_file_bytes=100))

            async def fake_run(args, **kwargs):
                if args[0] == "ffprobe":
                    return json.dumps({"streams": COMPATIBLE_STREAMS})
                if args[0] == "ffmpeg":
                    self.assertNotIn("-fs", args)  # Не выдаём обрезанное видео за готовое.
                    Path(args[-1]).write_bytes(b"result")
                    return ""
                source = directory / "source.mp4"
                source.write_bytes(b"source")
                return str(source)

            with patch("downloader.run_process", side_effect=fake_run):
                result = await downloader.download(Media("url", "title", 1, [video(audio="aac")]), "1080", directory)
            self.assertEqual(result.read_bytes(), b"result")
            self.assertFalse((directory / "source.mp4").exists())

    async def test_ready_mp4_skips_ffmpeg_and_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            downloader = Downloader(replace(settings(directory), max_file_bytes=100))
            # moov перед mdat: файл уже готов к потоковому воспроизведению.
            original = (8).to_bytes(4, "big") + b"moov" + (12).to_bytes(4, "big") + b"mdatDATA"
            timings = DownloadTimings()
            callback = AsyncMock()

            async def fake_run(args, **kwargs):
                if args[0] == "ffprobe":
                    return json.dumps({"streams": COMPATIBLE_STREAMS})
                self.assertNotEqual(args[0], "ffmpeg")
                source = directory / "source.mp4"
                source.write_bytes(original)
                return str(source)

            with patch("downloader.run_process", side_effect=fake_run):
                result = await downloader.download(Media("url", "title", 1, [video(audio="aac")]),
                                                   "1080", directory, timings=timings, on_processing=callback)
            self.assertEqual(result.read_bytes(), original)
            self.assertEqual(timings.mode, "ready_mp4")
            callback.assert_awaited_once()

    async def test_mp4_without_faststart_is_remuxed_without_encoding(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            downloader = Downloader(replace(settings(directory), max_file_bytes=100))
            original = (12).to_bytes(4, "big") + b"mdatDATA" + (8).to_bytes(4, "big") + b"moov"
            timings = DownloadTimings()

            async def fake_run(args, **kwargs):
                if args[0] == "ffprobe":
                    return json.dumps({"streams": COMPATIBLE_STREAMS})
                if args[0] == "ffmpeg":
                    self.assertEqual(args[args.index("-c:v") + 1], "copy")
                    self.assertEqual(args[args.index("-c:a") + 1], "copy")
                    self.assertIn("+faststart", args)
                    Path(args[-1]).write_bytes(b"remuxed")
                    return ""
                source = directory / "source.mp4"
                source.write_bytes(original)
                return str(source)

            with patch("downloader.run_process", side_effect=fake_run):
                result = await downloader.download(Media("url", "title", 1, [video(audio="aac")]),
                                                   "1080", directory, timings=timings)
            self.assertEqual(result.read_bytes(), b"remuxed")
            self.assertEqual(timings.mode, "copy")


if __name__ == "__main__":
    unittest.main()
