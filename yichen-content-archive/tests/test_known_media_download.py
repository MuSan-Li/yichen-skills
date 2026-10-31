#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import email.message
import http.client
import importlib.util
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "known_media_download", ROOT / "scripts" / "known_media_download.py"
)
MEDIA = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MEDIA
SPEC.loader.exec_module(MEDIA)

_NO_REAL_DOH = mock.patch.object(
    MEDIA, "doh_open", side_effect=AssertionError("unexpected real DNS-over-HTTPS request")
)


def setUpModule():
    # Any test that reaches DoH without its own fake fails loudly instead of using the network.
    _NO_REAL_DOH.start()


def tearDownModule():
    _NO_REAL_DOH.stop()


def doh_unavailable(_request, _timeout):
    raise urllib.error.URLError(socket.timeout("timed out"))


class KnownMediaDownloadTests(unittest.TestCase):
    def test_public_https_gate_rejects_local_and_credentials(self):
        self.assertEqual(
            MEDIA.validate_public_https_url(
                "https://8.8.8.8/video.mp4", resolve_dns=False
            ),
            "https://8.8.8.8/video.mp4",
        )
        with self.assertRaises(MEDIA.KnownMediaError):
            MEDIA.validate_public_https_url(
                "https://127.0.0.1/video.mp4", resolve_dns=False
            )
        with self.assertRaises(MEDIA.KnownMediaError):
            MEDIA.validate_public_https_url(
                "https://user:password@example.com/video", resolve_dns=False
            )

    def test_x_selector_uses_highest_bitrate_mp4(self):
        content = {
            "media": {
                "videos": [
                    {
                        "id": "123",
                        "url": "https://video.twimg.com/fallback.mp4",
                        "width": 1280,
                        "height": 720,
                        "duration": 10.5,
                        "formats": [
                            {
                                "url": "https://video.twimg.com/low.mp4",
                                "container": "mp4",
                                "bitrate": 256000,
                            },
                            {
                                "url": "https://video.twimg.com/high.mp4",
                                "container": "mp4",
                                "bitrate": 2176000,
                            },
                            {
                                "url": "https://video.twimg.com/master.m3u8",
                                "container": "m3u8",
                            },
                        ],
                    }
                ]
            }
        }
        selected = MEDIA.select_x_videos(content)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["url"], "https://video.twimg.com/high.mp4")
        self.assertEqual(selected[0]["bitrate"], 2176000)

    def test_x_selector_rejects_unapproved_media_host(self):
        content = {
            "media": {
                "videos": [
                    {
                        "formats": [
                            {
                                "url": "https://example.com/video.mp4",
                                "container": "mp4",
                                "bitrate": 9999999,
                            }
                        ]
                    }
                ]
            }
        }
        with self.assertRaises(MEDIA.KnownMediaError):
            MEDIA.select_x_videos(content)

    def test_existing_output_directory_gets_new_run(self):
        with tempfile.TemporaryDirectory(prefix="known-media-download-test-") as root:
            requested = pathlib.Path(root) / "media-example"
            requested.mkdir()
            selected = MEDIA.choose_unique_output_dir(requested)
            self.assertEqual(selected.name, "media-example-run-2")
            self.assertTrue(requested.is_dir())
            self.assertTrue(selected.is_dir())

    def test_run_directory_race_moves_on_to_the_next_number(self):
        with tempfile.TemporaryDirectory() as root:
            requested = pathlib.Path(root) / "media-example"
            requested.mkdir()
            original_mkdir = pathlib.Path.mkdir

            def racing_mkdir(path, *args, **kwargs):
                # A concurrent run creates -run-2 between our check and our mkdir.
                if path.name == "media-example-run-2" and not path.exists():
                    original_mkdir(path)
                return original_mkdir(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "mkdir", racing_mkdir):
                selected = MEDIA.choose_unique_output_dir(requested)
            self.assertEqual(selected.name, "media-example-run-3")

    def test_generic_backend_is_single_url_anonymous(self):
        with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"):
            command = MEDIA.yt_dlp_common(60)
        self.assertIn("--ignore-config", command)
        self.assertIn("--no-playlist", command)
        self.assertIn("--no-cookies-from-browser", command)
        # The generic extractor follows arbitrary pages and redirects; never allow it.
        self.assertEqual(
            command[command.index("--use-extractors") + 1], "default,-generic"
        )
        for unsafe in ("--prefer-insecure", "--no-check-certificates", "--legacy-server-connect"):
            self.assertNotIn(unsafe, command)

    def test_x_page_urls_skip_local_dns_but_keep_url_checks(self):
        # Observed 2026-10-23: a fake-IP proxy DNS answered 198.18.0.11 for x.com.
        fake_ip = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.11", 443))]
        no_video = {
            "status": "success",
            "input": {"id": "2000000000000000001"},
            "content": {"id": "2000000000000000001", "media": None},
        }
        reader = MEDIA.load_x_reader()
        with mock.patch.object(
            MEDIA.socket, "getaddrinfo", return_value=fake_ip
        ) as resolve, mock.patch.object(
            MEDIA, "load_x_reader", return_value=reader
        ), mock.patch.object(reader, "read_known_url", return_value=no_video):
            for url in (
                "https://x.com/example/status/2000000000000000001",
                "https://www.x.com/example/status/2000000000000000001",
                "https://twitter.com/i/status/2000000000000000001",
                "https://mobile.twitter.com/example/status/2000000000000000001",
            ):
                with self.subTest(url=url):
                    with self.assertRaises(MEDIA.KnownMediaError) as raised:
                        MEDIA.run(url, output_dir=None, timeout=10)
                    self.assertEqual(raised.exception.category, "no_public_video")
            for url, category in (
                ("http://x.com/example/status/1", "unsupported_url"),
                ("https://user:secret@x.com/example/status/1", "unsupported_url"),
                ("https://x.com:8443/example/status/1", "unsupported_port"),
            ):
                with self.subTest(url=url):
                    with self.assertRaises(MEDIA.KnownMediaError) as raised:
                        MEDIA.run(url, output_dir=None, timeout=10)
                    self.assertEqual(raised.exception.category, category)
        resolve.assert_not_called()

    def test_other_hosts_still_reject_fake_ip_answers_when_doh_is_unavailable(self):
        fake_ip = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.104", 443))]
        with mock.patch.object(
            MEDIA.socket, "getaddrinfo", return_value=fake_ip
        ) as resolve, mock.patch.object(MEDIA, "preflight_generic") as preflight, mock.patch.object(
            MEDIA, "doh_open", side_effect=doh_unavailable
        ):
            for url in (
                "https://www.bilibili.com/video/BV1xx411c7mD",
                "https://x.com.evil.example/example/status/1",
            ):
                with self.subTest(url=url):
                    with self.assertRaises(MEDIA.KnownMediaError) as raised:
                        MEDIA.run(url, output_dir=None, timeout=10)
                    self.assertEqual(raised.exception.category, "non_public_host")
        self.assertEqual(resolve.call_count, 2)
        preflight.assert_not_called()

    def test_fake_ip_only_answers_name_the_proxy_cause(self):
        # Live 2026-10-31: macOS returned the IPv4 answer plus its IPv4-translated
        # IPv6 form (::ffff:0:c612:3bc), which Python 3.9 even reports as global.
        live_answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.3.188", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:0:c612:3bc", 443, 0, 0)),
        ]
        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.3.188", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ]
        for answers, fake_ip_message in ((live_answers, True), (mixed, False)):
            with self.subTest(answers=answers), mock.patch.object(
                MEDIA.socket, "getaddrinfo", return_value=answers
            ), mock.patch.object(MEDIA, "preflight_generic") as preflight, mock.patch.object(
                MEDIA, "doh_open", side_effect=doh_unavailable
            ):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.run("https://www.bilibili.com/video/BV1Ab411c7Xy/", output_dir=None, timeout=10)
                self.assertEqual(raised.exception.category, "non_public_host")
                self.assertEqual("fake-IP" in raised.exception.message, fake_ip_message)
                preflight.assert_not_called()

    def test_saved_source_url_drops_query_and_fragment(self):
        self.assertEqual(
            MEDIA.safe_source_url("https://example.com/watch/1?token=secret#part"),
            "https://example.com/watch/1",
        )


class FakeMediaResponse:
    def __init__(
        self,
        body=b"\x00\x00\x00\x18ftypmp42",
        *,
        content_type="video/mp4",
        content_length="auto",
        final_url="https://video.twimg.com/ext_tw_video/1/pu/vid/a.mp4",
        read_error=None,
    ):
        self.body = io.BytesIO(body)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        if content_length == "auto":
            content_length = str(len(body))
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.final_url = final_url
        self.read_error = read_error

    def geturl(self):
        return self.final_url

    def read(self, amount):
        if self.read_error is not None:
            raise self.read_error
        return self.body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


X_VIDEO_URL = "https://video.twimg.com/ext_tw_video/1/pu/vid/a.mp4"


class XVideoDownloadTests(unittest.TestCase):
    def download(self, opener, directory):
        destination = pathlib.Path(directory) / "x-1-video-1.mp4"
        with mock.patch.object(MEDIA.urllib.request, "build_opener", return_value=opener):
            MEDIA.download_x_file(X_VIDEO_URL, destination, timeout=5)
        return destination

    def assert_download_error(self, opener, category):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                self.download(opener, directory)
            self.assertEqual(raised.exception.category, category)
            self.assertFalse((pathlib.Path(directory) / "x-1-video-1.mp4").exists())
        return raised.exception

    def test_complete_body_is_renamed_from_part_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = self.download(FakeOpener(FakeMediaResponse()), directory)
            self.assertEqual(destination.read_bytes(), b"\x00\x00\x00\x18ftypmp42")
            self.assertFalse(MEDIA.part_path(destination).exists())

    def test_truncated_or_empty_body_is_never_renamed(self):
        for response in (
            FakeMediaResponse(b"1234", content_length="10"),
            FakeMediaResponse(b"", content_length=None),
        ):
            with self.subTest(length=response.headers["Content-Length"]):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(MEDIA.KnownMediaError) as raised:
                        self.download(FakeOpener(response), directory)
                    self.assertEqual(raised.exception.category, "incomplete_download")
                    destination = pathlib.Path(directory) / "x-1-video-1.mp4"
                    self.assertFalse(destination.exists())
                    self.assertTrue(MEDIA.part_path(destination).exists())

    def test_content_type_and_length_gates(self):
        self.assert_download_error(
            FakeOpener(FakeMediaResponse(content_type="text/html")), "unexpected_media_type"
        )
        self.assert_download_error(
            FakeOpener(FakeMediaResponse(content_length=str(4 * 1024**3 + 1))),
            "media_too_large",
        )
        for malformed in ("abc", "-1", "1_000", "1e3"):
            with self.subTest(content_length=malformed):
                self.assert_download_error(
                    FakeOpener(FakeMediaResponse(content_length=malformed)),
                    "invalid_response",
                )
        with mock.patch.object(MEDIA, "MAX_MEDIA_BYTES", 8):
            # No Content-Length: the streamed byte count is still capped.
            self.assert_download_error(
                FakeOpener(FakeMediaResponse(b"x" * 9, content_length=None)),
                "media_too_large",
            )

    def test_redirects_and_final_url_stay_on_the_video_cdn(self):
        handler = MEDIA.XMediaRedirectHandler()
        with self.assertRaises(MEDIA.KnownMediaError) as raised:
            handler.redirect_request(
                None, None, 302, "Found", {}, "https://evil.example/a.mp4"
            )
        self.assertEqual(raised.exception.category, "unsafe_media_url")
        self.assert_download_error(
            FakeOpener(FakeMediaResponse(final_url="http://video.twimg.com/a.mp4")),
            "unsafe_media_url",
        )
        with self.assertRaises(MEDIA.KnownMediaError):
            MEDIA.download_x_file("https://pbs.twimg.com/a.mp4", pathlib.Path("unused"), timeout=5)

    def test_transport_failures_are_classified(self):
        cases = (
            (FakeOpener(error=socket.timeout("timed out")), "timeout"),
            (FakeOpener(error=urllib.error.URLError(socket.timeout("timed out"))), "timeout"),
            (FakeOpener(error=http.client.BadStatusLine("junk")), "download_failed"),
            (FakeOpener(error=ConnectionResetError()), "download_failed"),
            (
                FakeOpener(FakeMediaResponse(read_error=http.client.IncompleteRead(b"x"))),
                "download_failed",
            ),
            (FakeOpener(FakeMediaResponse(read_error=socket.timeout("timed out"))), "timeout"),
        )
        for opener, category in cases:
            with self.subTest(category=category, opener=opener.error or opener.response.read_error):
                self.assert_download_error(opener, category)


def x_reader_with(result):
    reader = MEDIA.load_x_reader()
    reader.read_known_url = mock.Mock(return_value=result)
    return reader


def x_video_result(*videos, status_id="2000000000000000001", status="success"):
    return {
        "status": status,
        "input": {"id": status_id},
        "content": {
            "id": status_id,
            "text": "post",
            "media": {"videos": [{"url": url} for url in videos]},
        },
        "errors": [],
    }


class XRunTests(unittest.TestCase):
    def test_reader_failure_keeps_its_category(self):
        failed = {
            "status": "failed",
            "content": None,
            "errors": [{"backend": "fxtwitter-public", "category": "rate_limited", "message": "HTTP 429"}],
        }
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            MEDIA, "load_x_reader", return_value=x_reader_with(failed)
        ), mock.patch.object(MEDIA.Path, "home", return_value=pathlib.Path(home)):
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                MEDIA.run("https://x.com/a/status/2000000000000000001", output_dir=None, timeout=10)
            self.assertEqual(raised.exception.category, "rate_limited")
            self.assertEqual(list(pathlib.Path(home).iterdir()), [])

    def test_invalid_source_id_never_reaches_a_local_path(self):
        for bad_id in ("1/../../escaped", "0", "１２３", "1" * 21):
            result = x_video_result(X_VIDEO_URL, status_id=bad_id)
            with self.subTest(bad_id=bad_id), tempfile.TemporaryDirectory() as home, mock.patch.object(
                MEDIA, "load_x_reader", return_value=x_reader_with(result)
            ), mock.patch.object(
                MEDIA.Path, "home", return_value=pathlib.Path(home)
            ), mock.patch.object(MEDIA, "download_x_file") as download:
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.run("https://x.com/i/article/2032093606551707648", output_dir=None, timeout=10)
                self.assertEqual(raised.exception.category, "invalid_response")
                self.assertEqual(list(pathlib.Path(home).iterdir()), [])
                download.assert_not_called()

    def test_partial_multi_video_run_records_each_item(self):
        calls = []

        def flaky(url, destination, *, timeout):
            calls.append(destination)
            if len(calls) == 2:
                MEDIA.part_path(destination).write_bytes(b"half")
                raise MEDIA.KnownMediaError("incomplete_download", "short body")
            destination.write_bytes(b"video")

        result = x_video_result(X_VIDEO_URL, "https://video.twimg.com/2.mp4")
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MEDIA, "load_x_reader", return_value=x_reader_with(result)
        ), mock.patch.object(MEDIA, "download_x_file", side_effect=flaky):
            output = pathlib.Path(root) / "out"
            run = MEDIA.run("https://x.com/a/status/2000000000000000001", output_dir=str(output), timeout=10)
            self.assertEqual(run["status"], "partial")
            self.assertEqual(run["output_dir"], str(output))
            metadata = json.loads((output / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "partial")
            self.assertEqual(metadata["counts"], {"requested": 2, "success": 1, "failed": 1})
            first, second = metadata["artifacts"]
            self.assertEqual(first["status"], "success")
            self.assertEqual(first["size"], 5)
            self.assertEqual(second["status"], "failed")
            self.assertIsNone(second["path"])
            self.assertEqual(second["error"]["category"], "incomplete_download")
            self.assertEqual(second["incomplete_path"], str(MEDIA.part_path(calls[1])))

    def test_all_failed_run_reports_output_dir_and_metadata(self):
        def failing(url, destination, *, timeout):
            raise MEDIA.KnownMediaError("timeout", "slow")

        result = x_video_result(X_VIDEO_URL)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MEDIA, "load_x_reader", return_value=x_reader_with(result)
        ), mock.patch.object(MEDIA, "download_x_file", side_effect=failing):
            output = pathlib.Path(root) / "out"
            stdout = io.StringIO()
            with mock.patch.object(
                sys, "argv", ["known_media_download.py", "https://x.com/a/status/2000000000000000001", "--output-dir", str(output)]
            ), contextlib.redirect_stdout(stdout):
                exit_code = MEDIA.main()
            self.assertEqual(exit_code, 2)
            printed = json.loads(stdout.getvalue())
            self.assertEqual(printed["status"], "failed")
            self.assertEqual(printed["error"]["category"], "timeout")
            self.assertEqual(printed["output_dir"], str(output))
            metadata = json.loads(pathlib.Path(printed["metadata_path"]).read_text())
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(metadata["artifacts"][0]["status"], "failed")


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def generic_info(**overrides):
    info = {
        "id": "BV1xx411c7mD",
        "title": "clip",
        "extractor": "BiliBili",
        "webpage_url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "format_id": "30080+30280",
        "formats": [
            {"format_id": "30016", "url": "http://cdn.example.com/low.m4s"},
            {"format_id": "30080", "url": "https://upos.example.com:8082/v.m4s", "filesize": 1000},
            {"format_id": "30280", "url": "https://upos.example.com/a.m4s", "filesize": 100},
        ],
        "requested_formats": [
            {"format_id": "30080", "url": "https://upos.example.com:8082/v.m4s", "filesize": 1000},
            {"format_id": "30280", "url": "https://upos.example.com/a.m4s", "filesize": 100},
        ],
    }
    info.update(overrides)
    return info


class GenericRouteTests(unittest.TestCase):
    def test_bound_info_keeps_only_validated_selected_formats(self):
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=PUBLIC_DNS) as resolve:
            bound, size_check = MEDIA.bind_generic_formats(generic_info())
        # The unselected http format is dropped instead of being trusted.
        self.assertEqual([item["format_id"] for item in bound["formats"]], ["30080", "30280"])
        self.assertNotIn("webpage_url", bound)
        self.assertNotIn("requested_formats", bound)
        self.assertEqual(size_check["declared_bytes"], 1100)
        self.assertEqual(size_check["status"], "declared_within_limit")
        self.assertEqual(resolve.call_count, 1)

    def test_selected_media_urls_must_be_public_https(self):
        private_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        cases = (
            ({"url": "http://upos.example.com/v.m4s"}, PUBLIC_DNS),
            ({"url": "https://user:pw@upos.example.com/v.m4s"}, PUBLIC_DNS),
            ({"url": "https://169.254.169.254/latest"}, PUBLIC_DNS),
            ({"url": "https://upos.example.com/v.m4s"}, private_dns),
            ({"url": "https://upos.example.com/v.m4s", "manifest_url": "http://upos.example.com/m.mpd"}, PUBLIC_DNS),
            ({"url": "https://upos.example.com/v.m4s", "fragments": [{"url": "https://127.0.0.1/f1"}]}, PUBLIC_DNS),
            ({"url": None}, PUBLIC_DNS),
        )
        for selected, answers in cases:
            info = generic_info(
                format_id="30080",
                requested_formats=None,
                formats=[{"format_id": "30080", **selected}],
                **{key: value for key, value in selected.items()},
            )
            with self.subTest(selected=selected), mock.patch.object(
                MEDIA.socket, "getaddrinfo", return_value=answers
            ):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.bind_generic_formats(info)
                self.assertEqual(raised.exception.category, "unsafe_media_url")

    def test_declared_or_undeclared_size_is_checked_or_marked(self):
        too_big = generic_info(
            requested_formats=[
                {"format_id": "30080", "url": "https://upos.example.com/v.m4s", "filesize": 3 * 1024**3},
                {"format_id": "30280", "url": "https://upos.example.com/a.m4s", "filesize_approx": 2 * 1024**3},
            ]
        )
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                MEDIA.bind_generic_formats(too_big)
            self.assertEqual(raised.exception.category, "media_too_large")
            unknown = generic_info(
                requested_formats=[
                    {"format_id": "30080", "url": "https://upos.example.com/v.m4s"},
                    {"format_id": "30280", "url": "https://upos.example.com/a.m4s", "filesize": 10},
                ]
            )
            _bound, size_check = MEDIA.bind_generic_formats(unknown)
        self.assertIsNone(size_check["declared_bytes"])
        self.assertEqual(size_check["status"], "size_undeclared")

    def test_preflight_selects_format_and_rejects_containers(self):
        seen = []

        def fake_run(command, *, total_timeout, **_kwargs):
            seen.append((command, total_timeout))
            return 0, json.dumps(payloads.pop(0))

        payloads = [generic_info(), {"_type": "playlist", "id": "x"}, {"title": "no id"}]
        with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"), mock.patch.object(
            MEDIA, "run_yt_dlp", side_effect=fake_run
        ):
            self.assertEqual(MEDIA.preflight_generic("https://www.bilibili.com/video/BV1xx411c7mD", timeout=30)["id"], "BV1xx411c7mD")
            for category in ("container_rejected", "invalid_response"):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.preflight_generic("https://www.bilibili.com/video/BV1xx411c7mD", timeout=30)
                self.assertEqual(raised.exception.category, category)
        command, total_timeout = seen[0]
        self.assertEqual(total_timeout, MEDIA.GENERIC_PREFLIGHT_SECONDS)
        self.assertEqual(command[command.index("--format") + 1], MEDIA.GENERIC_FORMAT)
        self.assertEqual(command[-2:], ["--", "https://www.bilibili.com/video/BV1xx411c7mD"])
        with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"), mock.patch.object(
            MEDIA, "run_yt_dlp", return_value=(1, "")
        ):
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                MEDIA.preflight_generic("https://www.bilibili.com/video/BV1xx411c7mD", timeout=30)
        self.assertEqual(raised.exception.category, "extractor_failed")

    def test_download_replays_bound_info_without_reextracting_the_page(self):
        with tempfile.TemporaryDirectory() as root:
            output = pathlib.Path(root)
            seen = {}

            def fake_run(command, *, total_timeout, input_text=None, watch_dir=None):
                seen.update(command=command, input=json.loads(input_text), watch_dir=watch_dir)
                media = output / "BiliBili-BV1xx411c7mD.mp4"
                media.write_bytes(b"video")
                return 0, f"{media}\n"

            with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"), mock.patch.object(
                MEDIA, "run_yt_dlp", side_effect=fake_run
            ):
                paths = MEDIA.download_generic({"id": "BV1xx411c7mD", "formats": []}, output, timeout=30)
            self.assertEqual([path.name for path in paths], ["BiliBili-BV1xx411c7mD.mp4"])
            command = seen["command"]
            self.assertEqual(command[-2:], ["--load-info-json", "-"])
            self.assertNotIn("--", command)
            self.assertEqual(seen["input"], {"id": "BV1xx411c7mD", "formats": []})
            self.assertEqual(seen["watch_dir"], output)

            with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"), mock.patch.object(
                MEDIA, "run_yt_dlp", return_value=(0, f"{output / 'missing.mp4'}\n")
            ):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.download_generic({}, output, timeout=30)
            self.assertEqual(raised.exception.category, "unexpected_output")
            with tempfile.TemporaryDirectory() as other:
                outside = pathlib.Path(other) / "escaped.mp4"
                outside.write_bytes(b"x")
                with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"), mock.patch.object(
                    MEDIA, "run_yt_dlp", return_value=(0, f"{outside}\n")
                ):
                    with self.assertRaises(MEDIA.KnownMediaError) as raised:
                        MEDIA.download_generic({}, output, timeout=30)
                self.assertEqual(raised.exception.category, "output_escape")
            with mock.patch.object(MEDIA, "find_yt_dlp", return_value="yt-dlp"), mock.patch.object(
                MEDIA, "run_yt_dlp", return_value=(1, "")
            ):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.download_generic({}, output, timeout=30)
            self.assertEqual(raised.exception.category, "download_failed")

    def test_generic_run_failure_keeps_output_dir_and_metadata(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MEDIA.socket, "getaddrinfo", return_value=PUBLIC_DNS
        ), mock.patch.object(
            MEDIA, "preflight_generic", return_value=generic_info()
        ), mock.patch.object(
            MEDIA,
            "download_generic",
            side_effect=MEDIA.KnownMediaError("timeout", "yt-dlp did not finish"),
        ):
            output = pathlib.Path(root) / "out"
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                MEDIA.run("https://www.bilibili.com/video/BV1xx411c7mD", output_dir=str(output), timeout=30)
            self.assertEqual(raised.exception.category, "timeout")
            self.assertEqual(raised.exception.output_dir, output)
            metadata = json.loads((output / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(metadata["error"]["category"], "timeout")
            self.assertEqual(metadata["size_check"]["declared_bytes"], 1100)


LIVE_FAKE_IP = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.3.188", 443)),
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:0:c612:3bc", 443, 0, 0)),
]


class FakeDohResponse:
    def __init__(self, payload, *, status=200, content_type="application/dns-json"):
        self.status = status
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = email.message.Message()
        if content_type:
            self.headers["Content-Type"] = content_type

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def doh_answer(record_type, *addresses, cname=None):
    answers = [{"name": "x.", "type": 5, "data": cname}] if cname else []
    answers += [{"name": "x.", "type": record_type, "TTL": 60, "data": address} for address in addresses]
    return {"Status": 0, "Answer": answers}


class FakeDoh:
    """Answer DoH requests by (resolver host, record type); record what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def __call__(self, request, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        key = (parsed.hostname, int(query["type"][0]))
        self.requests.append((key, query["name"][0], request.get_header("Accept"), timeout))
        reply = self.answers.get(key)
        if reply is None:
            raise urllib.error.URLError("refused")
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, FakeDohResponse) else FakeDohResponse(reply)


class FakeIpDohTests(unittest.TestCase):
    def check(self, doh, url="https://www.bilibili.com/video/BV1Ab411c7Xy/"):
        checks = {}
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=LIVE_FAKE_IP), mock.patch.object(
            MEDIA, "doh_open", side_effect=doh
        ):
            MEDIA.validate_public_https_url(url, dns_checks=checks)
        return checks

    def test_public_doh_answers_pass_behind_fake_ip(self):
        doh = FakeDoh(
            {
                ("dns.alidns.com", 1): doh_answer(1, "123.6.30.12", cname="a.w.bilicdn1.com."),
                ("dns.alidns.com", 28): doh_answer(28, "240e:974:e000:1001::1"),
            }
        )
        self.assertEqual(self.check(doh), {"www.bilibili.com": "doh_verified_behind_fake_ip"})
        self.assertEqual(
            [(key, name, accept) for key, name, accept, _ in doh.requests],
            [
                (("dns.alidns.com", 1), "www.bilibili.com", "application/dns-json"),
                (("dns.alidns.com", 28), "www.bilibili.com", "application/dns-json"),
            ],
        )
        self.assertTrue(all(timeout <= 5 for *_, timeout in doh.requests))

    def test_second_resolver_is_used_when_the_first_fails_or_lies(self):
        cloudflare = {
            ("cloudflare-dns.com", 1): doh_answer(1, "123.6.30.12"),
            ("cloudflare-dns.com", 28): {"Status": 0},
        }
        first_failures = (
            {("dns.alidns.com", 1): urllib.error.URLError(socket.timeout("timed out"))},
            {("dns.alidns.com", 1): FakeDohResponse(b"<html>blocked</html>", content_type="text/html")},
            {("dns.alidns.com", 1): FakeDohResponse(b"{" + b" " * MEDIA.DOH_MAX_BYTES + b"}")},
            {("dns.alidns.com", 1): {"Status": 2}},
            {("dns.alidns.com", 1): doh_answer(1, "not-an-ip")},
            {("dns.alidns.com", 1): FakeDohResponse({"Status": 0}, status=503)},
        )
        for first in first_failures:
            with self.subTest(first=first):
                doh = FakeDoh({**first, **cloudflare})
                self.assertEqual(
                    self.check(doh), {"www.bilibili.com": "doh_verified_behind_fake_ip"}
                )
                self.assertEqual(doh.requests[-1][0], ("cloudflare-dns.com", 28))

    def test_private_or_missing_doh_answers_still_reject(self):
        cases = (
            {
                ("dns.alidns.com", 1): doh_answer(1, "123.6.30.12", "10.0.0.8"),
                ("dns.alidns.com", 28): {"Status": 0},
            },
            {
                ("dns.alidns.com", 1): doh_answer(1, "198.18.3.188"),
                ("dns.alidns.com", 28): {"Status": 0},
            },
            {
                ("dns.alidns.com", 1): {"Status": 0},
                ("dns.alidns.com", 28): doh_answer(28, "::ffff:0:a00:8"),
            },
            {},
            {
                ("dns.alidns.com", 1): {"Status": 0, "Answer": []},
                ("dns.alidns.com", 28): {"Status": 0},
            },
        )
        for answers in cases:
            with self.subTest(answers=answers):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    self.check(FakeDoh(answers))
                self.assertEqual(raised.exception.category, "non_public_host")

    def test_preflight_media_hosts_get_the_same_doh_check(self):
        doh = FakeDoh(
            {
                ("dns.alidns.com", 1): doh_answer(1, "123.6.30.12"),
                ("dns.alidns.com", 28): {"Status": 0},
            }
        )
        checks = {}
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=LIVE_FAKE_IP), mock.patch.object(
            MEDIA, "doh_open", side_effect=doh
        ):
            MEDIA.bind_generic_formats(generic_info(), checks)
        self.assertEqual(checks, {"upos.example.com": "doh_verified_behind_fake_ip"})
        # One host, checked once: A + AAAA.
        self.assertEqual(len(doh.requests), 2)
        private = FakeDoh(
            {
                ("dns.alidns.com", 1): doh_answer(1, "192.168.1.9"),
                ("dns.alidns.com", 28): {"Status": 0},
            }
        )
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=LIVE_FAKE_IP), mock.patch.object(
            MEDIA, "doh_open", side_effect=private
        ):
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                MEDIA.bind_generic_formats(generic_info())
        self.assertEqual(raised.exception.category, "unsafe_media_url")

    def test_run_metadata_marks_doh_verified_hosts(self):
        doh = FakeDoh(
            {
                ("dns.alidns.com", 1): doh_answer(1, "123.6.30.12"),
                ("dns.alidns.com", 28): {"Status": 0},
            }
        )
        with tempfile.TemporaryDirectory() as root:
            output = pathlib.Path(root) / "out"

            def fake_download(_info, directory, *, timeout):
                media = directory / "BiliBili-BV1xx411c7mD.mp4"
                media.write_bytes(b"video")
                return [media]

            with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=LIVE_FAKE_IP), mock.patch.object(
                MEDIA, "doh_open", side_effect=doh
            ), mock.patch.object(MEDIA, "preflight_generic", return_value=generic_info()), mock.patch.object(
                MEDIA, "download_generic", side_effect=fake_download
            ):
                result = MEDIA.run(
                    "https://www.bilibili.com/video/BV1xx411c7mD", output_dir=str(output), timeout=30
                )
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["dns_check"], "doh_verified_behind_fake_ip")
            self.assertEqual(
                result["dns_check_hosts"],
                {
                    "upos.example.com": "doh_verified_behind_fake_ip",
                    "www.bilibili.com": "doh_verified_behind_fake_ip",
                },
            )
            metadata = json.loads((output / "metadata.json").read_text())
            self.assertEqual(metadata["dns_check"], "doh_verified_behind_fake_ip")

    def test_plain_public_dns_is_reported_as_local_dns_without_doh(self):
        checks = {}
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            MEDIA.validate_public_https_url("https://www.bilibili.com/video/x", dns_checks=checks)
        self.assertEqual(checks, {"www.bilibili.com": "local_dns"})
        self.assertEqual(MEDIA.summarize_dns_checks({"a": "local_dns", "b": "doh_verified_behind_fake_ip"}), "doh_verified_behind_fake_ip")


class EmbeddedIpv4Tests(unittest.TestCase):
    def test_ipv4_inside_translated_nat64_and_6to4_forms_is_judged_as_ipv4(self):
        private = (
            "::ffff:10.0.0.5",
            "::ffff:0:a00:5",  # IPv4-translated 10.0.0.5 (Python 3.9 calls this global)
            "::ffff:0:7f00:1",
            "64:ff9b::a00:5",
            "64:ff9b::a9fe:a9fe",  # 169.254.169.254
            "64:ff9b:1::c0a8:101",
            "64:ff9b:1:a00:5::",  # local-use NAT64 with an unknown prefix length
            "2002:a00:5::1",
            "::ffff:0:c612:3bc",
        )
        public = ("::ffff:8.8.8.8", "::ffff:0:808:808", "64:ff9b::808:808", "2400:3200::1", "8.8.8.8")
        for value in private:
            with self.subTest(value=value):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA._require_global_ip(value)
                self.assertEqual(raised.exception.category, "non_public_host")
                with self.assertRaises(MEDIA.KnownMediaError):
                    MEDIA.validate_public_https_url(f"https://[{value}]/v.mp4", resolve_dns=False)
        for value in public:
            with self.subTest(value=value):
                MEDIA._require_global_ip(value)

    def test_dns_answers_in_translated_form_are_rejected(self):
        answers = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::a00:5", 443, 0, 0))]
        with mock.patch.object(MEDIA.socket, "getaddrinfo", return_value=answers):
            with self.assertRaises(MEDIA.KnownMediaError) as raised:
                MEDIA.validate_public_https_url("https://cdn.example.com/v.mp4")
        self.assertEqual(raised.exception.category, "non_public_host")

    def test_fake_ip_detection_covers_every_embedded_form(self):
        for value in ("198.18.0.1", "198.19.255.254", "::ffff:198.18.3.188", "::ffff:0:c612:3bc"):
            with self.subTest(value=value):
                self.assertTrue(MEDIA._is_fake_ip_answer(value))
        for value in ("198.20.0.1", "8.8.8.8", "::1", "nonsense"):
            with self.subTest(value=value):
                self.assertFalse(MEDIA._is_fake_ip_answer(value))


class YtDlpProcessTests(unittest.TestCase):
    def test_total_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as root:
            pid_file = pathlib.Path(root) / "child.pid"
            script = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            started = time.monotonic()
            with mock.patch.object(MEDIA, "YT_DLP_POLL_SECONDS", 0.2):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.run_yt_dlp([sys.executable, "-c", script], total_timeout=1.5)
            self.assertEqual(raised.exception.category, "timeout")
            self.assertLess(time.monotonic() - started, 30)
            child_pid = int(pid_file.read_text())
            for _ in range(50):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail("the grandchild process survived the timeout")

    def test_output_file_size_is_capped_while_downloading(self):
        with tempfile.TemporaryDirectory() as root:
            script = (
                "import sys, time\n"
                "with open(sys.argv[1], 'wb') as handle:\n"
                "    for _ in range(600):\n"
                "        handle.write(b'x' * 64); handle.flush(); time.sleep(0.05)\n"
            )
            target = pathlib.Path(root) / "merged.mp4.part"
            with mock.patch.object(MEDIA, "YT_DLP_POLL_SECONDS", 0.1), mock.patch.object(
                MEDIA, "MAX_MEDIA_BYTES", 256
            ):
                with self.assertRaises(MEDIA.KnownMediaError) as raised:
                    MEDIA.run_yt_dlp(
                        [sys.executable, "-c", script, str(target)],
                        total_timeout=20,
                        watch_dir=pathlib.Path(root),
                    )
            self.assertEqual(raised.exception.category, "media_too_large")
            self.assertLess(target.stat().st_size, 600 * 64)

    def test_stdin_input_and_exit_code_are_returned(self):
        returncode, stdout = MEDIA.run_yt_dlp(
            [sys.executable, "-c", "import sys; data = sys.stdin.read(); print(len(data)); sys.exit(3)"],
            total_timeout=20,
            input_text="x" * 200000,
        )
        self.assertEqual(returncode, 3)
        self.assertEqual(stdout.strip(), "200000")


if __name__ == "__main__":
    unittest.main()
