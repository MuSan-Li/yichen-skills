#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "weibo_known_url.py"
    spec = importlib.util.spec_from_file_location("content_archive_weibo", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


weibo = load_module()


class WeiboKnownUrlTests(unittest.TestCase):
    def test_overlay_is_normalized_to_exact_mobile_detail(self) -> None:
        target = weibo.normalize_known_url(
            "https://weibo.com/u/5700099573?layerid=4930755200029647"
        )
        self.assertEqual(
            target.canonical_url,
            "https://m.weibo.cn/detail/4930755200029647",
        )
        self.assertEqual(target.expected_numeric_id, "4930755200029647")
        self.assertEqual(target.user_id, "5700099573")

    def test_mobile_status_and_desktop_status_are_single_items(self) -> None:
        mobile = weibo.normalize_known_url(
            "https://m.weibo.cn/status/4930755200029647"
        )
        self.assertEqual(
            mobile.canonical_url,
            "https://m.weibo.cn/detail/4930755200029647",
        )
        desktop = weibo.normalize_known_url(
            "https://weibo.com/5700099573/4930755200029647"
        )
        self.assertEqual(
            desktop.canonical_url,
            "https://weibo.com/5700099573/4930755200029647",
        )

    def test_account_search_hot_comments_external_and_duplicate_layerid_reject(self) -> None:
        rejected = (
            "https://weibo.com/u/5700099573",
            "https://weibo.com/search?q=test",
            "https://weibo.com/hot/search",
            "https://weibo.com/comments/4930755200029647",
            "https://example.com/u/5700099573?layerid=4930755200029647",
            "https://weibo.com/u/5700099573?layerid=4930755200029647&layerid=4930755200029648",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(weibo.ArchiveError):
                    weibo.normalize_known_url(url)

    def test_ytdlp_command_is_anonymous_closed_and_non_overwriting(self) -> None:
        command = weibo.ytdlp_base_command("/usr/bin/yt-dlp", 20)
        for flag in (
            "--ignore-config",
            "--no-config-locations",
            "--no-plugin-dirs",
            "--no-cookies",
            "--no-cookies-from-browser",
            "--no-cache-dir",
            "--no-playlist",
            "--no-overwrites",
            "--no-post-overwrites",
        ):
            self.assertIn(flag, command)
        self.assertNotIn("--cookies", command)
        self.assertNotIn("--cookies-from-browser", command)

    def test_best_progressive_mp4_is_selected(self) -> None:
        selected = weibo.select_progressive_mp4(
            {
                "formats": [
                    {
                        "format_id": "audio-only",
                        "ext": "m4a",
                        "protocol": "https",
                        "vcodec": "none",
                        "acodec": "mp4a",
                    },
                    {
                        "format_id": "mp4_hd",
                        "ext": "mp4",
                        "protocol": "https",
                        "width": 1620,
                        "height": 720,
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                    },
                    {
                        "format_id": "mp4_1080p",
                        "ext": "mp4",
                        "protocol": "https",
                        "width": 2304,
                        "height": 1024,
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                    },
                ]
            }
        )
        self.assertEqual(selected["format_id"], "mp4_1080p")

    def test_signed_urls_and_cookie_values_are_redacted(self) -> None:
        message = (
            "failed https://cdn.example/video.mp4?token=secret "
            "Cookie=session-secret Authorization=Bearer-secret"
        )
        sanitized = weibo.sanitized_error(message)
        self.assertNotIn("cdn.example", sanitized)
        self.assertNotIn("session-secret", sanitized)
        self.assertNotIn("Bearer-secret", sanitized)

    def test_full_auth_cookie_headers_and_scheme_relative_urls_are_redacted(self) -> None:
        message = (
            "Authorization: Bearer REALTOKEN\n"
            "Cookie: a=1; SUB=SECONDSECRET; csrftoken=THIRDSECRET\n"
            "failed //cdn.example/video.mp4?token=SIGNEDSECRET"
        )
        sanitized = weibo.sanitized_error(message)
        for secret in (
            "REALTOKEN",
            "SECONDSECRET",
            "THIRDSECRET",
            "SIGNEDSECRET",
            "cdn.example",
        ):
            self.assertNotIn(secret, sanitized)

    def test_existing_output_gets_new_run_without_touching_sentinel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yichen-content-archive-weibo-") as directory:
            requested = Path(directory) / "weibo-test"
            requested.mkdir()
            sentinel = requested / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            selected = weibo.choose_unique_run_dir(requested)
            self.assertEqual(selected.name, "weibo-test-run-2")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


POST_ID = "4930755200029647"


def valid_info(**overrides):
    info = {
        "id": POST_ID,
        "extractor": "Weibo",
        "title": "post",
        "formats": [
            {
                "format_id": "mp4_hd",
                "ext": "mp4",
                "protocol": "https",
                "height": 720,
                "vcodec": "avc1",
                "acodec": "mp4a",
                "url": "https://f.video.weibocdn.com/o0/x.mp4?Expires=1&ssig=SIGNED",
            }
        ],
    }
    info.update(overrides)
    return info


class WeiboFetchInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = weibo.normalize_known_url(f"https://m.weibo.cn/detail/{POST_ID}")
        patcher = mock.patch.object(weibo, "ytdlp_binary", return_value="/usr/bin/yt-dlp")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, stdout: str):
        with mock.patch.object(weibo, "run_process", return_value=stdout) as runner:
            result = weibo.fetch_info(self.target, 20)
        command = runner.call_args.args[0]
        self.assertIn("--no-plugin-dirs", command)
        self.assertEqual(command[-1], f"https://m.weibo.cn/detail/{POST_ID}")
        return result

    def test_each_rejection_branch_has_its_own_reason(self) -> None:
        cases = {
            "metadata_parse_failed": "not json",
            "unexpected_collection": json.dumps(valid_info(_type="playlist")),
            "unexpected_extractor": json.dumps(valid_info(extractor="generic")),
            "invalid_resolved_id": json.dumps(valid_info(id="../etc")),
            "target_mismatch": json.dumps(valid_info(id="4930755200029648")),
        }
        for reason, stdout in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(weibo.ArchiveError) as raised:
                    self.fetch(stdout)
                self.assertEqual(raised.exception.reason, reason)

    def test_matching_single_post_is_returned(self) -> None:
        self.assertEqual(self.fetch(json.dumps(valid_info()))["id"], POST_ID)

    def test_backend_failure_is_a_fixed_classification(self) -> None:
        with mock.patch.object(
            weibo.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="Cookie: SECRET https://cdn/x?sig=1"),
        ):
            with self.assertRaises(weibo.ArchiveError) as raised:
                weibo.fetch_info(self.target, 20)
        self.assertEqual(raised.exception.reason, "metadata_fetch_failed")
        self.assertNotIn("SECRET", str(raised.exception))


class WeiboDownloadPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.output_dir = self.root / "run"
        self.output_dir.mkdir()
        self.target = weibo.normalize_known_url(f"https://m.weibo.cn/detail/{POST_ID}")
        patcher = mock.patch.object(weibo, "ytdlp_binary", return_value="/usr/bin/yt-dlp")
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, printed: str):
        info = valid_info()
        with mock.patch.object(weibo, "run_process", return_value=printed):
            return weibo.download_media(self.target, info, info["formats"][0], self.output_dir, 20)

    def test_printed_path_outside_output_dir_is_rejected(self) -> None:
        outside = self.root / f"weibo_{POST_ID}.mp4"
        outside.write_bytes(b"video")
        with self.assertRaises(weibo.ArchiveError) as raised:
            self.download(f"{outside}\n")
        self.assertEqual(raised.exception.reason, "unsafe_output_path")

    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"video")
        link = self.output_dir / f"weibo_{POST_ID}.mp4"
        os.symlink(outside, link)
        with self.assertRaises(weibo.ArchiveError) as raised:
            self.download(f"{link}\n")
        self.assertEqual(raised.exception.reason, "unsafe_output_path")

    def test_missing_empty_or_non_mp4_results_are_rejected(self) -> None:
        with self.assertRaises(weibo.ArchiveError) as raised:
            self.download("")
        self.assertEqual(raised.exception.reason, "download_missing")
        empty = self.output_dir / f"weibo_{POST_ID}.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(weibo.ArchiveError) as raised:
            self.download(f"{empty}\n")
        self.assertEqual(raised.exception.reason, "invalid_media")
        webm = self.output_dir / f"weibo_{POST_ID}.webm"
        webm.write_bytes(b"video")
        with self.assertRaises(weibo.ArchiveError) as raised:
            self.download(f"{webm}\n")
        self.assertEqual(raised.exception.reason, "invalid_media")

    def test_valid_file_inside_output_dir_is_returned(self) -> None:
        media = self.output_dir / f"weibo_{POST_ID}.mp4"
        media.write_bytes(b"video")
        self.assertEqual(self.download(f"{media}\n"), media)


class WeiboInfoWithoutProgressiveMp4Tests(unittest.TestCase):
    def run_execute(self, operation: str, info):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                operation=operation,
                url=f"https://m.weibo.cn/detail/{POST_ID}",
                output_dir=str(Path(directory) / "run"),
                timeout=20,
            )
            with mock.patch.object(weibo, "fetch_info", return_value=info), mock.patch.object(
                weibo, "download_media"
            ) as downloader:
                result = weibo.execute(args)
                metadata = None
                if result.get("metadata_path"):
                    metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
            return result, metadata, downloader

    def test_info_succeeds_without_single_file_mp4(self) -> None:
        info = valid_info(formats=[{"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native"}])
        result, metadata, downloader = self.run_execute("info", info)
        self.assertTrue(result["ok"])
        self.assertIsNone(metadata["selected_format"])
        self.assertFalse(metadata["progressive_mp4_available"])
        self.assertNotIn("SIGNED", json.dumps(metadata))
        downloader.assert_not_called()

    def test_download_still_requires_single_file_mp4(self) -> None:
        info = valid_info(formats=[{"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native"}])
        result, _metadata, downloader = self.run_execute("download", info)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported_media_format")
        downloader.assert_not_called()

    def test_info_with_mp4_records_selected_format_without_url(self) -> None:
        result, metadata, _ = self.run_execute("info", valid_info())
        self.assertTrue(result["ok"])
        self.assertEqual(metadata["selected_format"]["format_id"], "mp4_hd")
        self.assertTrue(metadata["progressive_mp4_available"])
        self.assertEqual(metadata["audio_track"], "present")
        self.assertNotIn("SIGNED", json.dumps(metadata))


# Format list of a real 8 s silent post (m.weibo.cn/detail/<id>,
# yt-dlp 2026.07.04, 2026-10-31); URLs replaced, other fields as returned.
SILENT_POST_FORMATS = [
    {"format_id": "scrubber_hd", "ext": "jpg", "protocol": "https", "vcodec": None, "acodec": None,
     "width": 180, "height": 320, "url": "https://wx1.sinaimg.cn/x.jpg?SIGNED"},
    {"format_id": "mp4_hd", "ext": "mp4", "protocol": "https", "vcodec": "avc1.64001f", "acodec": None,
     "width": 540, "height": 720, "fps": 30, "tbr": 84989, "filesize": 84989,
     "url": "https://f.video.weibocdn.com/o0/hd.mp4?Expires=1&ssig=SIGNED"},
    {"format_id": "mp4_720p", "ext": "mp4", "protocol": "https", "vcodec": "avc1.64001f", "acodec": None,
     "width": 720, "height": 960, "fps": 30, "tbr": 124489, "filesize": 124489,
     "url": "https://f.video.weibocdn.com/o0/720.mp4?Expires=1&ssig=SIGNED"},
    {"format_id": "mp4_1080p", "ext": "mp4", "protocol": "https", "vcodec": "avc1.640028", "acodec": None,
     "width": 1080, "height": 1440, "fps": 30, "tbr": 227981, "filesize": 227981,
     "url": "https://f.video.weibocdn.com/o0/1080.mp4?Expires=1&ssig=SIGNED"},
    {"format_id": "mp4_1440p", "ext": "mp4", "protocol": "https", "vcodec": "avc1.640032", "acodec": None,
     "width": 1440, "height": 1920, "fps": 30, "tbr": 315397, "filesize": 315397,
     "url": "https://f.video.weibocdn.com/o0/1440.mp4?Expires=1&ssig=SIGNED"},
]


class WeiboSilentVideoTests(unittest.TestCase):
    def test_silent_post_selects_best_video_only_mp4(self) -> None:
        selected, audio_track = weibo.select_single_file_mp4({"formats": SILENT_POST_FORMATS})
        self.assertEqual((selected["format_id"], audio_track), ("mp4_1440p", "none"))
        # The audio-required selector alone still refuses this post.
        with self.assertRaises(weibo.ArchiveError):
            weibo.select_progressive_mp4({"formats": SILENT_POST_FORMATS})

    def test_any_audio_format_keeps_the_muxed_only_rule(self) -> None:
        with_audio_only_stream = SILENT_POST_FORMATS + [
            {"format_id": "audio", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a"}
        ]
        with self.assertRaises(weibo.ArchiveError) as raised:
            weibo.select_single_file_mp4({"formats": with_audio_only_stream})
        self.assertEqual(raised.exception.reason, "unsupported_media_format")
        muxed = SILENT_POST_FORMATS + [
            {"format_id": "mp4_ld", "ext": "mp4", "protocol": "https", "height": 360,
             "vcodec": "avc1", "acodec": "mp4a"}
        ]
        selected, audio_track = weibo.select_single_file_mp4({"formats": muxed})
        self.assertEqual((selected["format_id"], audio_track), ("mp4_ld", "present"))

    def test_no_single_file_mp4_at_all_is_still_unsupported(self) -> None:
        for formats in ([], [{"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native", "vcodec": "avc1"}]):
            with self.subTest(formats=formats):
                with self.assertRaises(weibo.ArchiveError) as raised:
                    weibo.select_single_file_mp4({"formats": formats})
                self.assertEqual(raised.exception.reason, "unsupported_media_format")

    def test_download_of_silent_post_marks_audio_track_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                operation="download",
                url=f"https://m.weibo.cn/detail/{POST_ID}",
                output_dir=str(Path(directory) / "run"),
                timeout=20,
            )
            info = valid_info(formats=SILENT_POST_FORMATS)

            def fake_download(_target, _info, selected, output_dir, _timeout):
                self.assertEqual(selected["format_id"], "mp4_1440p")
                media = output_dir / f"weibo_{POST_ID}.mp4"
                media.write_bytes(b"video-only")
                return media

            probe = {"streams": [{"codec_type": "video", "codec_name": "h264"}]}
            with mock.patch.object(weibo, "fetch_info", return_value=info), mock.patch.object(
                weibo, "download_media", side_effect=fake_download
            ), mock.patch.object(weibo, "probe_media", return_value=probe):
                result = weibo.execute(args)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["audio_track"], "none")
            metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
            self.assertEqual(metadata["audio_track"], "none")
            self.assertEqual(metadata["selected_format"]["acodec"], None)
            self.assertNotIn("SIGNED", json.dumps(metadata))
            summary = json.loads((Path(result["output_dir"]) / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["verification"]["audio_track"], "none")
            self.assertEqual(summary["verification"]["ffprobe_audio_streams"], 0)


if __name__ == "__main__":
    unittest.main()
