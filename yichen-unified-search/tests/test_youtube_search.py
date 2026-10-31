import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "youtube_search", ROOT / "scripts" / "youtube_search.py"
)
YOUTUBE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = YOUTUBE
SPEC.loader.exec_module(YOUTUBE)


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        self.read_limit = limit
        return self.body


class FakeOpener:
    def __init__(self, body=None, error=None):
        self.response = FakeResponse(body) if body is not None else None
        self.error = error

    def open(self, request, timeout):
        if self.error is not None:
            raise self.error
        return self.response


def flat_entry(index, **overrides):
    value = {
        "id": f"abcdefghij{index}",
        "title": f"AI agent {index}",
        "channel": "Example Channel",
        "duration": 300,
        "view_count": 10,
    }
    value.update(overrides)
    return value


class YouTubeSearchTests(unittest.TestCase):
    def run_main_with_flat(self, argv, entries):
        """Run the CLI on the anonymous yt-dlp route with a fake yt-dlp process."""

        runner = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps({"entries": entries}), stderr=""
            )
        )
        original = YOUTUBE.run_ytdlp

        def fake_run(target, *, limit, timeout):
            return original(target, limit=limit, timeout=timeout, runner=runner)

        stdout = io.StringIO()
        with (
            mock.patch.object(YOUTUBE, "api_key", return_value=None),
            mock.patch.object(YOUTUBE.shutil, "which", return_value="/opt/yt-dlp"),
            mock.patch.object(YOUTUBE, "run_ytdlp", side_effect=fake_run),
            contextlib.redirect_stdout(stdout),
        ):
            code = YOUTUBE.main(argv)
        return code, json.loads(stdout.getvalue()), runner

    def test_api_get_reads_one_bounded_response(self):
        opener = FakeOpener(b'{"items": []}')
        with mock.patch.object(YOUTUBE, "API_OPENER", opener):
            payload = YOUTUBE.api_get("search", {"part": "id"}, key="test", timeout=5)
        self.assertEqual(payload, {"items": []})
        self.assertEqual(
            opener.response.read_limit,
            YOUTUBE.MAX_API_RESPONSE_BYTES + 1,
        )

    def test_api_get_rejects_oversized_response(self):
        opener = FakeOpener(b"x" * (YOUTUBE.MAX_API_RESPONSE_BYTES + 1))
        with (
            mock.patch.object(YOUTUBE, "API_OPENER", opener),
            self.assertRaises(YOUTUBE.SearchError) as raised,
        ):
            YOUTUBE.api_get("search", {"part": "id"}, key="test", timeout=5)
        self.assertEqual(raised.exception.category, "response_too_large")

    def test_api_get_refuses_redirects(self):
        redirected = YOUTUBE.urllib.error.HTTPError(
            "https://www.googleapis.com/youtube/v3/search",
            302,
            "Found",
            {"Location": "https://example.net/"},
            None,
        )
        opener = FakeOpener(error=redirected)
        with (
            mock.patch.object(YOUTUBE, "API_OPENER", opener),
            self.assertRaises(YOUTUBE.SearchError) as raised,
        ):
            YOUTUBE.api_get("search", {"part": "id"}, key="test", timeout=5)
        self.assertEqual(raised.exception.category, "redirect_refused")

    def test_api_get_classifies_timeouts_and_transport_errors(self):
        cases = (
            (socket.timeout("timed out"), "timeout"),
            (TimeoutError("timed out"), "timeout"),
            (urllib.error.URLError(socket.timeout("timed out")), "timeout"),
            (urllib.error.URLError(ConnectionRefusedError()), "network_error"),
            (http.client.RemoteDisconnected("closed"), "network_error"),
            (http.client.IncompleteRead(b"partial"), "network_error"),
            (ConnectionResetError(), "network_error"),
        )
        for error, category in cases:
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(YOUTUBE, "API_OPENER", FakeOpener(error=error)),
                self.assertRaises(YOUTUBE.SearchError) as raised,
            ):
                YOUTUBE.api_get("search", {"part": "id"}, key="test", timeout=5)
            self.assertEqual(raised.exception.category, category)
            self.assertNotIn("test", raised.exception.message)

    def test_api_get_times_out_against_silent_local_server(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        base = f"http://127.0.0.1:{server.getsockname()[1]}/youtube/v3"
        with (
            mock.patch.object(YOUTUBE, "API_BASE", base),
            mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}),
            self.assertRaises(YOUTUBE.SearchError) as raised,
        ):
            YOUTUBE.api_get("search", {"part": "id"}, key="test", timeout=1)
        self.assertEqual(raised.exception.category, "timeout")

    def test_api_mode_timeout_prints_failure_envelope(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(YOUTUBE, "api_key", return_value="test"),
            mock.patch.object(
                YOUTUBE, "API_OPENER", FakeOpener(error=socket.timeout("timed out"))
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = YOUTUBE.main(["search", "example", "--backend", "api"])
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(envelope["routes"][0]["status"], "failed")
        self.assertEqual(envelope["errors"][0]["category"], "timeout")

    def test_ytdlp_requests_and_labels_approximate_flat_dates(self):
        recent = int(time.time()) - 2 * 86400
        code, envelope, runner = self.run_main_with_flat(
            ["search", "AI agent", "--limit", "5", "--days", "7"],
            [flat_entry(1, timestamp=recent), flat_entry(2, timestamp=recent)],
        )
        command = runner.call_args.args[0]
        option = command.index("--extractor-args")
        self.assertEqual(command[option + 1], "youtubetab:approximate_date")
        self.assertEqual(code, 0)
        self.assertEqual(envelope["routes"][0]["status"], "completed")
        self.assertEqual(len(envelope["candidates"]), 2)
        candidate = envelope["candidates"][0]
        self.assertIsNotNone(candidate["published_at"])
        self.assertEqual(
            candidate["provenance"]["published_at_precision"],
            "approximate_relative_time",
        )
        self.assertIn(YOUTUBE.APPROXIMATE_TIME_LIMITATION, candidate["limitations"])
        coverage = envelope["coverage"][0]
        self.assertEqual(coverage["published_at_precision"], "approximate_relative_time")
        self.assertEqual(coverage["time_filter"]["time_unknown_dropped"], 0)

    def test_days_window_with_unknown_flat_times_is_partial_not_zero_success(self):
        code, envelope, _ = self.run_main_with_flat(
            ["search", "AI agent", "--limit", "5", "--days", "7"],
            [flat_entry(index) for index in range(5)],
        )
        self.assertEqual(code, 0)
        self.assertEqual(envelope["candidates"], [])
        self.assertEqual(envelope["routes"][0]["status"], "partial")
        self.assertEqual(envelope["coverage"][0]["time_filter"]["time_unknown_dropped"], 5)
        self.assertTrue(
            any("without a usable publication time" in item
                for item in envelope["routes"][0]["limitations"])
        )

    def test_days_window_counts_unknown_and_outside_rows_separately(self):
        now = int(time.time())
        code, envelope, _ = self.run_main_with_flat(
            ["search", "AI agent", "--limit", "5", "--days", "7"],
            [
                flat_entry(1, timestamp=now - 86400),
                flat_entry(2, timestamp=now - 30 * 86400),
                flat_entry(3),
            ],
        )
        self.assertEqual(code, 0)
        self.assertEqual(envelope["routes"][0]["status"], "partial")
        self.assertEqual([item["candidate_id"] for item in envelope["candidates"]],
                         ["youtube:abcdefghij1"])
        time_filter = envelope["coverage"][0]["time_filter"]
        self.assertEqual(time_filter["time_unknown_dropped"], 1)
        self.assertEqual(time_filter["outside_time_window"], 1)

    def test_without_time_window_unknown_times_stay_completed(self):
        code, envelope, _ = self.run_main_with_flat(
            ["search", "AI agent", "--limit", "5"],
            [flat_entry(index) for index in range(3)],
        )
        self.assertEqual(code, 0)
        self.assertEqual(envelope["routes"][0]["status"], "completed")
        self.assertEqual(len(envelope["candidates"]), 3)
        self.assertIsNone(envelope["coverage"][0]["time_filter"])
        self.assertNotIn("published_at_precision", envelope["candidates"][0]["provenance"])

    def test_duration_parsers(self):
        self.assertEqual(YOUTUBE.parse_duration("PT1H30M5S"), 5405)
        self.assertEqual(YOUTUBE.parse_duration_input("1h30m"), 5400)
        self.assertEqual(YOUTUBE.parse_duration_input("45"), 2700)

    def test_exact_channel_target_never_guesses_a_name(self):
        self.assertEqual(
            YOUTUBE.channel_target("@example"),
            "https://www.youtube.com/@example/videos",
        )
        with self.assertRaises(YOUTUBE.SearchError):
            YOUTUBE.channel_target("Example Creator")

    def test_flat_results_map_to_candidate_schema(self):
        rows = [
            {
                "id": "abcdefghijk",
                "title": "Example",
                "url": "https://www.youtube.com/watch?v=abcdefghijk",
                "description": "Public result",
                "channel": "Example Channel",
                "channel_id": "UCabcdefghijklmnopqrstuv",
                "published_at": "2026-07-31T00:00:00Z",
                "duration_seconds": 90,
                "views": 123,
                "likes": None,
            }
        ]
        payload = YOUTUBE.envelope(
            query="example",
            mode="search",
            backend=YOUTUBE.BACKEND_YTDLP,
            limit=10,
            rows=rows,
            time_range=None,
        )
        item = payload["candidates"][0]
        self.assertEqual(item["candidate_id"], "youtube:abcdefghijk")
        self.assertEqual(item["platform"], "youtube")
        self.assertFalse(item["access"]["login_state_used"])
        self.assertEqual(item["metrics"]["views"], 123)

    def test_auto_backend_without_key_uses_anonymous_ytdlp(self):
        args = YOUTUBE.parse_args(["search", "example", "--limit", "2"])
        rows = [
            {
                "id": "abcdefghijk",
                "title": "Example",
                "url": "https://www.youtube.com/watch?v=abcdefghijk",
                "description": None,
                "channel": None,
                "channel_id": None,
                "published_at": None,
                "duration_seconds": None,
                "views": None,
                "likes": None,
            }
        ]
        with (
            mock.patch.object(YOUTUBE, "api_key", return_value=None),
            mock.patch.object(YOUTUBE, "run_ytdlp", return_value=rows) as runner,
        ):
            payload = YOUTUBE.execute(args)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.kwargs["limit"], 6)
        self.assertTrue(runner.call_args.args[0].startswith("ytsearch6:"))
        self.assertEqual(payload["routes"][0]["backend"], YOUTUBE.BACKEND_YTDLP)

    def test_ytdlp_fallback_ignores_user_config_plugins_cache_and_downloads(self):
        runner = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"entries": []}),
                stderr="",
            )
        )
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": "/private/home",
            "YOUTUBE_API_KEY": "youtube-secret",  # pragma: allowlist secret
            "FIRECRAWL_API_KEY": "firecrawl-secret",  # pragma: allowlist secret
            "ANYSEARCH_API_KEY": "anysearch-secret",  # pragma: allowlist secret
            "BROWSER_COOKIE": "cookie-secret",  # pragma: allowlist secret
        }
        with (
            mock.patch.object(YOUTUBE.shutil, "which", return_value="/opt/yt-dlp"),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            self.assertEqual(
                YOUTUBE.run_ytdlp("ytsearch5:example", limit=5, timeout=20, runner=runner),
                [],
            )

        command = runner.call_args.args[0]
        for option in (
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-cache-dir",
            "--skip-download",
            "--flat-playlist",
        ):
            self.assertIn(option, command)
        child_env = runner.call_args.kwargs["env"]
        self.assertEqual(child_env, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertNotIn("HOME", child_env)

    def test_script_contains_no_media_download_or_cookie_command(self):
        source = (ROOT / "scripts" / "youtube_search.py").read_text(encoding="utf-8")
        self.assertNotIn("--audio-format", source)
        self.assertNotIn("--write-subs", source)
        self.assertNotIn("--cookies-from-browser", source)
        self.assertIn('"--ignore-config"', source)
        self.assertIn('"--skip-download"', source)


if __name__ == "__main__":
    unittest.main()
