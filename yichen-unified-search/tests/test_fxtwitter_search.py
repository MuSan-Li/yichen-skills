import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fxtwitter_search", ROOT / "scripts" / "fxtwitter_search.py"
)
FX = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = FX
SPEC.loader.exec_module(FX)


def base_status(**overrides):
    value = {
        "type": "status",
        "id": "2000000000000000001",
        "text": "A public X post",
        "created_timestamp": 1785369600,
        "created_at": "2026-07-30T00:00:00Z",
        "likes": 12,
        "reposts": 3,
        "quotes": 2,
        "replies": 4,
        "views": 120,
        "bookmarks": 5,
        "lang": "en",
        "author": {
            "name": "Example Author",
            "screen_name": "example",
        },
    }
    value.update(overrides)
    return value


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.body[:limit]


def http_error(code):
    return urllib.error.HTTPError(FX.API_URL, code, "error", {}, None)


def silent_local_server(test_case):
    """Return a URL whose TCP handshake completes but never sends a response."""

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test_case.addCleanup(server.close)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    return f"http://127.0.0.1:{server.getsockname()[1]}"


class FxTwitterSearchTests(unittest.TestCase):
    def setUp(self):
        self.retrieved_at = "2026-07-30T12:00:00Z"

    def normalize(self, status):
        return FX.normalize_status(
            status,
            query="GPT 5.6",
            rank=1,
            retrieved_at=self.retrieved_at,
        )

    def test_plain_post_maps_to_candidate_schema(self):
        candidate = self.normalize(base_status())
        self.assertEqual(candidate["content_type"], "x_post")
        self.assertEqual(candidate["candidate_id"], "x:2000000000000000001")
        self.assertEqual(
            candidate["canonical_url"],
            "https://x.com/example/status/2000000000000000001",
        )
        self.assertFalse(candidate["access"]["login_state_used"])
        self.assertEqual(candidate["metrics"]["comments"], 4)
        self.assertEqual(candidate["metrics"]["shares"], 3)

    def test_quote_keeps_nested_public_post_metadata(self):
        quote = base_status(
            id="1999999999999999999",
            text="Quoted body",
            author={"name": "Quoted", "screen_name": "quoted"},
        )
        candidate = self.normalize(base_status(text="My comment", quote=quote))
        self.assertEqual(candidate["content_type"], "x_quote_post")
        nested = candidate["platform_fields"]["quoted_post"]
        self.assertEqual(nested["id"], "1999999999999999999")
        self.assertEqual(nested["author"]["screen_name"], "quoted")
        self.assertEqual(nested["text"], "Quoted body")

    def test_article_returns_preview_but_never_full_blocks(self):
        article = {
            "id": "2032093606551707648",
            "title": "A Long X Article",
            "preview_text": "Public preview",
            "cover_media": {
                "media_info": {
                    "__typename": "ApiImage",
                    "original_img_url": "https://pbs.twimg.com/media/cover.jpg",
                }
            },
            "content": {
                "blocks": [{"text": "FULL BODY MUST NOT LEAK INTO SEARCH"}],
                "entityMap": [],
            },
        }
        candidate = self.normalize(base_status(article=article))
        self.assertEqual(candidate["content_type"], "x_article")
        self.assertEqual(candidate["title"], "A Long X Article")
        projected = candidate["platform_fields"]["article"]
        self.assertEqual(projected["id"], "2032093606551707648")
        self.assertEqual(projected["preview_text"], "Public preview")
        self.assertNotIn("content", projected)
        self.assertNotIn("FULL BODY", json.dumps(candidate))

    def test_quote_can_contain_article_metadata(self):
        quote = base_status(
            id="1999999999999999999",
            article={
                "id": "1888888888888888888",
                "title": "Quoted article",
                "preview_text": "Quoted preview",
            },
        )
        candidate = self.normalize(base_status(quote=quote))
        nested_article = candidate["platform_fields"]["quoted_post"]["article"]
        self.assertEqual(nested_article["title"], "Quoted article")

    def test_days_filter_uses_public_timestamp_and_drops_unknown_time(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        statuses = [
            base_status(id="recent", created_timestamp=now.timestamp() - 3600),
            base_status(id="old", created_timestamp=now.timestamp() - 172800),
            base_status(id="unknown", created_timestamp=None),
        ]
        filtered = FX.filter_by_days(statuses, 1, now=now)
        self.assertEqual([item["id"] for item in filtered], ["recent"])

    def test_envelope_marks_anonymous_third_party_coverage(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        envelope = FX.make_envelope(
            query="GPT 5.6",
            limit=5,
            days=None,
            feed="latest",
            payload={
                "code": 200,
                "results": [base_status()],
                "cursor": {"top": "top", "bottom": "next"},
            },
            error=None,
            now=now,
        )
        self.assertEqual(envelope["routes"][0]["backend"], "fxtwitter-public")
        self.assertFalse(envelope["routes"][0]["login_state_used"])
        self.assertTrue(envelope["coverage"][0]["truncated"])
        self.assertEqual(len(envelope["candidates"]), 1)

    def test_error_envelope_is_safe_and_requests_fallback(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        error = FX.SearchError(
            "rate_limited",
            "FxTwitter returned HTTP 429; continue to the configured X fallback chain.",
        )
        envelope = FX.make_envelope(
            query="GPT 5.6",
            limit=5,
            days=None,
            feed="latest",
            payload=None,
            error=error,
            now=now,
        )
        self.assertEqual(envelope["routes"][0]["status"], "failed")
        self.assertEqual(envelope["errors"][0]["category"], "rate_limited")
        self.assertEqual(envelope["candidates"], [])

    def fetch_with(self, side_effect):
        with mock.patch.object(FX.urllib.request, "urlopen", side_effect=side_effect):
            return FX.fetch_search("GPT 5.6", limit=5, feed="latest", lang=None, timeout=5)

    def test_fetch_search_maps_http_status_errors(self):
        for code, category in (
            (400, "invalid_query"),
            (429, "rate_limited"),
            (500, "upstream_http_error"),
            (503, "upstream_http_error"),
        ):
            with self.subTest(code=code), self.assertRaises(FX.SearchError) as raised:
                self.fetch_with(http_error(code))
            self.assertEqual(raised.exception.category, category)

    def test_http_404_stays_empty_but_is_distinguishable(self):
        payload = self.fetch_with(http_error(404))
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["empty_result_signal"], "http_404_treated_as_empty")

        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        envelope = FX.make_envelope(
            query="GPT 5.6", limit=5, days=1, feed="latest",
            payload=payload, error=None, now=now,
        )
        # Same fallback semantics as before: completed, zero candidates.
        self.assertEqual(envelope["routes"][0]["status"], "completed")
        self.assertEqual(envelope["candidates"], [])
        coverage = envelope["coverage"][0]
        self.assertEqual(coverage["empty_result_signal"], "http_404_treated_as_empty")
        self.assertTrue(
            any("http_404_treated_as_empty" in item for item in coverage["limitations"])
        )

    def test_api_code_404_is_marked_and_upstream_marker_is_not_trusted(self):
        body = json.dumps(
            {"code": 404, "results": [], "empty_result_signal": "spoofed"}
        ).encode()
        payload = self.fetch_with(lambda *args, **kwargs: FakeResponse(body))
        self.assertEqual(payload["empty_result_signal"], "api_code_404_treated_as_empty")

        ok_body = json.dumps(
            {"code": 200, "results": [base_status()], "empty_result_signal": "spoofed"}
        ).encode()
        ok_payload = self.fetch_with(lambda *args, **kwargs: FakeResponse(ok_body))
        self.assertNotIn("empty_result_signal", ok_payload)
        envelope = FX.make_envelope(
            query="GPT 5.6", limit=5, days=None, feed="latest",
            payload=ok_payload, error=None,
            now=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(envelope["coverage"][0]["empty_result_signal"])
        self.assertEqual(len(envelope["candidates"]), 1)

    def test_fetch_search_classifies_timeouts_and_transport_errors(self):
        cases = (
            (socket.timeout("timed out"), "timeout"),
            (TimeoutError("timed out"), "timeout"),
            (urllib.error.URLError(socket.timeout("timed out")), "timeout"),
            (urllib.error.URLError(ConnectionRefusedError()), "network_error"),
            (http.client.RemoteDisconnected("closed"), "network_error"),
            (http.client.IncompleteRead(b"partial"), "network_error"),
            (http.client.BadStatusLine("junk"), "network_error"),
            (ConnectionResetError(), "network_error"),
        )
        for error, category in cases:
            with self.subTest(error=type(error).__name__), self.assertRaises(
                FX.SearchError
            ) as raised:
                self.fetch_with(error)
            self.assertEqual(raised.exception.category, category)

    def test_fetch_search_times_out_against_silent_local_server(self):
        base = silent_local_server(self)
        with mock.patch.object(FX, "API_URL", f"{base}/2/search"), mock.patch.dict(
            os.environ, {"no_proxy": "*", "NO_PROXY": "*"}
        ), self.assertRaises(FX.SearchError) as raised:
            FX.fetch_search("GPT 5.6", limit=5, feed="latest", lang=None, timeout=1)
        self.assertEqual(raised.exception.category, "timeout")

    def test_main_prints_failure_envelope_instead_of_crashing_on_socket_timeout(self):
        stdout = io.StringIO()
        with mock.patch.object(
            sys, "argv", ["fxtwitter_search.py", "--query", "GPT 5.6", "--timeout", "5"]
        ), mock.patch.object(
            FX.urllib.request, "urlopen", side_effect=socket.timeout("timed out")
        ), contextlib.redirect_stdout(stdout):
            code = FX.main()
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(envelope["routes"][0]["status"], "failed")
        self.assertEqual(envelope["errors"][0]["category"], "timeout")
        self.assertEqual(envelope["candidates"], [])


if __name__ == "__main__":
    unittest.main()
