import http.client
import importlib.util
import pathlib
import socket
import sys
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "x_known_url", ROOT / "scripts" / "x_known_url.py"
)
XURL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = XURL
SPEC.loader.exec_module(XURL)


def status(**overrides):
    value = {
        "type": "status",
        "id": "2000000000000000001",
        "text": "outer post",
        "created_at": "2026-07-30T12:00:00Z",
        "likes": 3,
        "reposts": 2,
        "quotes": 1,
        "replies": 4,
        "bookmarks": 5,
        "views": 100,
        "lang": "en",
        "author": {
            "id": "42",
            "name": "Example",
            "screen_name": "example",
            "followers": 99,
            "verification": {"verified": True},
        },
    }
    value.update(overrides)
    return value


class KnownXUrlTests(unittest.TestCase):
    def test_status_url_is_normalized(self):
        parsed = XURL.parse_known_url(
            "https://twitter.com/example/status/2000000000000000001?s=20"
        )
        self.assertEqual(parsed["input_kind"], "x_status_url")
        self.assertEqual(parsed["id"], "2000000000000000001")
        self.assertEqual(
            parsed["canonical_url"],
            "https://x.com/example/status/2000000000000000001",
        )

    def test_article_url_is_recognized(self):
        parsed = XURL.parse_known_url(
            "https://x.com/i/article/2032093606551707648"
        )
        self.assertEqual(parsed["input_kind"], "x_article_url")
        self.assertEqual(parsed["id"], "2032093606551707648")

    def test_non_x_host_is_rejected(self):
        with self.assertRaises(XURL.KnownUrlError):
            XURL.parse_known_url("https://example.com/user/status/123")

    def test_plain_post_normalizes_without_login(self):
        normalized = XURL.normalize_status(status())
        self.assertEqual(normalized["content_type"], "x_post")
        self.assertEqual(normalized["author"]["screen_name"], "example")
        self.assertEqual(normalized["metrics"]["bookmarks"], 5)

    def test_quote_keeps_nested_post(self):
        normalized = XURL.normalize_status(
            status(
                quote=status(
                    id="1999999999999999999",
                    text="quoted post",
                    author={
                        "id": "43",
                        "name": "Quoted",
                        "screen_name": "quoted",
                    },
                )
            )
        )
        self.assertEqual(normalized["content_type"], "x_quote_post")
        self.assertEqual(normalized["quote"]["text"], "quoted post")
        self.assertEqual(normalized["quote"]["author"]["screen_name"], "quoted")

    def test_exact_article_parent_ignores_noise_and_accepts_nested_quote(self):
        noise = status(article={"id": "111"})
        exact_quote = status(
            id="1888888888888888888",
            article={"id": "2032093606551707648"},
        )
        outer = status(quote=exact_quote)
        parent = XURL.find_article_parent(
            [noise, outer],
            "2032093606551707648",
        )
        self.assertEqual(parent["id"], "1888888888888888888")

    def test_resolve_article_rechecks_fetched_parent_and_article_identity(self):
        article_id = "2032093606551707648"
        parent_id = "1888888888888888888"
        search_payload = {
            "results": [status(id=parent_id, article={"id": article_id})]
        }
        for fetched in (
            status(id="1777777777777777777", article={"id": article_id}),
            status(id=parent_id, article={"id": "1111111111111111111"}),
        ):
            with self.subTest(fetched_id=fetched["id"]), mock.patch.object(
                XURL, "fetch_json", return_value=search_payload
            ), mock.patch.object(XURL, "fetch_status", return_value=fetched):
                with self.assertRaises(XURL.KnownUrlError) as raised:
                    XURL.resolve_article(article_id, timeout=30)
            self.assertEqual(raised.exception.category, "source_identity_mismatch")

    def test_article_markdown_renders_structure_links_and_media(self):
        article = {
            "id": "2032093606551707648",
            "title": "Article title",
            "preview_text": "Preview",
            "content": {
                "blocks": [
                    {
                        "type": "header-two",
                        "text": "Heading",
                        "entityRanges": [],
                    },
                    {
                        "type": "unstyled",
                        "text": "Read this site",
                        "entityRanges": [{"key": 1, "offset": 10, "length": 4}],
                    },
                    {
                        "type": "unordered-list-item",
                        "text": "A list item",
                        "entityRanges": [],
                    },
                    {
                        "type": "blockquote",
                        "text": "A quote",
                        "entityRanges": [],
                    },
                    {
                        "type": "atomic",
                        "text": " ",
                        "entityRanges": [{"key": 0, "offset": 0, "length": 1}],
                    },
                ],
                "entityMap": [
                    {
                        "key": "0",
                        "value": {
                            "type": "MEDIA",
                            "data": {
                                "caption": "Chart",
                                "mediaItems": [{"mediaId": "1234567890"}],
                            },
                        },
                    },
                    {
                        "key": "1",
                        "value": {
                            "type": "LINK",
                            "data": {"url": "https://example.com"},
                        },
                    },
                ],
            },
            "media_entities": [
                {
                    "media_id": "1234567890",
                    "media_info": {
                        "original_img_url": "https://pbs.twimg.com/media/chart.jpg"
                    },
                }
            ],
        }
        markdown = XURL.render_article_markdown(article)
        self.assertIn("## Heading", markdown)
        self.assertIn("[site](https://example.com)", markdown)
        self.assertIn("- A list item", markdown)
        self.assertIn("> A quote", markdown)
        self.assertIn(
            "![Chart](https://pbs.twimg.com/media/chart.jpg)",
            markdown,
        )
        self.assertEqual(XURL.article_media(article)[0]["kind"], "photo")

    def test_article_video_projection_keeps_signal_and_never_renders_poster_as_photo(self):
        poster_url = "https://pbs.twimg.com/media/video-poster.jpg"
        article = {
            "id": "2032093606551707648",
            "title": "Video article",
            "content": {
                "blocks": [
                    {"type": "unstyled", "text": "Body", "entityRanges": []},
                    {
                        "type": "atomic",
                        "text": " ",
                        "entityRanges": [{"key": 0, "offset": 0, "length": 1}],
                    },
                ],
                "entityMap": [
                    {
                        "key": "0",
                        "value": {
                            "type": "MEDIA",
                            "data": {
                                "caption": "Video",
                                "mediaItems": [{"mediaId": "video-1234567890"}],
                            },
                        },
                    }
                ],
            },
            "media_entities": [
                {
                    "media_id": "video-1234567890",
                    "media_info": {
                        "__typename": "ApiVideo",
                        "thumbnail_url": poster_url,
                        "duration_millis": 1234,
                        "variants": [
                            {
                                "type": "video/mp4",
                                "bitrate": 832000,
                                "url": "https://video.twimg.com/ext_tw_video/123/pu/vid/video.mp4?token=redacted",
                            }
                        ],
                    },
                }
            ],
        }
        projected = XURL.article_projection(article)
        media = projected["media"][0]
        self.assertEqual(media["kind"], "video")
        self.assertEqual(media["url"], poster_url)
        self.assertEqual(media["video_info"]["variant_count"], 1)
        self.assertEqual(
            media["video_info"]["variants"][0]["url"],
            "https://video.twimg.com/ext_tw_video/123/pu/vid/video.mp4",
        )
        self.assertNotIn(poster_url, projected["body_markdown"])

    def test_article_thumbnail_without_type_is_projected_unknown(self):
        article = {
            "media_entities": [
                {
                    "media_id": "unknown-1",
                    "media_info": {
                        "thumbnail_url": "https://pbs.twimg.com/media/ambiguous.jpg"
                    },
                }
            ]
        }
        media = XURL.article_media(article)[0]
        self.assertEqual(media["kind"], "unknown")
        self.assertEqual(XURL.media_lookup(article), {})

    def test_article_projection_contains_body_but_not_raw_draftjs(self):
        article = {
            "id": "2032093606551707648",
            "title": "Article title",
            "content": {
                "blocks": [
                    {"type": "unstyled", "text": "Full body", "entityRanges": []}
                ],
                "entityMap": [],
            },
            "media_entities": [],
        }
        projected = XURL.article_projection(article)
        self.assertEqual(projected["body_markdown"], "Full body")
        self.assertTrue(projected["render_complete"])
        self.assertEqual(projected["render_errors"], [])
        self.assertNotIn("content", projected)

    def test_article_render_audit_rejects_malformed_block_atomic_and_link(self):
        article = {
            "id": "2032093606551707648",
            "title": "Malformed article",
            "content": {
                "blocks": [
                    {"type": "unstyled", "text": "Visible body", "entityRanges": []},
                    None,
                    {
                        "type": "atomic",
                        "text": " ",
                        "entityRanges": [{"key": 0, "offset": 0, "length": 1}],
                    },
                    {
                        "type": "unstyled",
                        "text": "bad link",
                        "entityRanges": [
                            {"key": 1, "offset": "zero", "length": 3}
                        ],
                    },
                    {"type": "unstyled", "text": "", "entityRanges": []},
                ],
                "entityMap": [
                    {
                        "key": "0",
                        "value": {
                            "type": "MEDIA",
                            "data": {"mediaItems": [{"mediaId": "missing-media"}]},
                        },
                    },
                    {
                        "key": "1",
                        "value": {
                            "type": "LINK",
                            "data": {"url": "https://example.com"},
                        },
                    },
                ],
            },
            "media_entities": [],
        }
        projected = XURL.article_projection(article)
        self.assertIn("Visible body", projected["body_markdown"])
        self.assertFalse(projected["render_complete"])
        categories = {error["category"] for error in projected["render_errors"]}
        self.assertIn("block_not_object", categories)
        self.assertIn("atomic_media_unrenderable", categories)
        self.assertIn("link_range_invalid", categories)
        self.assertEqual(projected["render_audit"]["status"], "incomplete")

    def test_authenticated_fallbacks_are_authorization_gated(self):
        parsed = XURL.parse_known_url(
            "https://x.com/example/status/2000000000000000001"
        )
        fallbacks = XURL.authenticated_fallbacks(
            parsed,
            parsed["canonical_url"],
        )
        self.assertEqual(
            [item["backend"] for item in fallbacks],
            ["opencli-twitter", "xreach"],
        )
        self.assertTrue(
            all(item["requires_current_turn_authorization"] for item in fallbacks)
        )
        self.assertTrue(all(item["login_state_used"] for item in fallbacks))

    def test_status_article_uses_opencli_article_fallback(self):
        parsed = XURL.parse_known_url(
            "https://x.com/example/status/2000000000000000001"
        )
        fallbacks = XURL.authenticated_fallbacks(
            parsed,
            parsed["canonical_url"],
            "x_article",
        )
        self.assertEqual(
            fallbacks[0]["argv"],
            [
                "opencli",
                "twitter",
                "article",
                parsed["canonical_url"],
                "-f",
                "md",
            ],
        )

    def test_status_article_with_incomplete_body_and_jina_text_stays_partial(self):
        # Jina text cannot be audited against the DraftJS body, so it never makes
        # an incomplete Article "success" or drops the authorized fallback plan.
        status = {
            "type": "status",
            "id": "2000000000000000001",
            "url": "https://x.com/example/status/2000000000000000001",
            "author": {"screen_name": "example"},
            "article": {
                "id": "2000000000000000002",
                "title": "Incomplete article",
            },
        }
        with mock.patch.object(
            XURL,
            "fetch_status",
            return_value=status,
        ), mock.patch.object(
            XURL,
            "fetch_jina",
            return_value="# Public article fallback",
        ) as jina:
            result = XURL.read_known_url(
                status["url"],
                timeout=30,
                allow_jina_fallback=True,
            )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["content"]["content_type"], "x_article")
        self.assertEqual(result["route"]["backend_used"], "jina-reader")
        self.assertEqual(result["jina_markdown"], "# Public article fallback")
        jina.assert_called_once_with(status["url"], timeout=30)
        self.assertEqual(
            [error["category"] for error in result["errors"]],
            ["article_body_incomplete", "article_body_unverified"],
        )
        fallbacks = result["authenticated_fallbacks"]
        self.assertEqual(
            [item["backend"] for item in fallbacks], ["opencli-twitter", "xreach"]
        )
        self.assertEqual(
            fallbacks[0]["argv"],
            ["opencli", "twitter", "article", status["url"], "-f", "md"],
        )
        self.assertTrue(
            all(item["requires_current_turn_authorization"] for item in fallbacks)
        )

    def test_article_url_resolves_parent_with_one_exact_search(self):
        article_id = "2032093606551707648"
        parent_id = "1888888888888888888"
        article = {
            "id": article_id,
            "title": "Full article",
            "content": {
                "blocks": [{"type": "unstyled", "text": "Body", "entityRanges": []}],
                "entityMap": [],
            },
            "media_entities": [],
        }
        calls = []

        def fake_fetch_json(path, *, query, timeout):
            calls.append((path, query))
            if path == "/search":
                return {"code": 200, "results": [status(id=parent_id, article={"id": article_id})]}
            return {"code": 200, "status": status(id=parent_id, article=article)}

        with mock.patch.object(
            XURL, "fetch_json", side_effect=fake_fetch_json
        ), mock.patch.object(XURL, "fetch_jina") as jina:
            result = XURL.read_known_url(
                f"https://x.com/i/article/{article_id}",
                timeout=30,
                allow_jina_fallback=True,
            )
        self.assertEqual(
            calls,
            [
                ("/search", {"q": article_id, "feed": "latest", "count": 10}),
                (f"/status/{parent_id}", None),
            ],
        )
        jina.assert_not_called()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["content"]["content_type"], "x_article")
        self.assertEqual(result["content"]["article"]["body_markdown"], "Body")
        self.assertEqual(result["authenticated_fallbacks"], [])

    def test_article_parent_id_from_search_is_validated_before_any_path_use(self):
        article_id = "2032093606551707648"
        for parent_id in ("1/../../escaped", "0", "-1", "１２３", "1" * 21, "12a"):
            with self.subTest(parent_id=parent_id), mock.patch.object(
                XURL,
                "fetch_json",
                return_value={
                    "code": 200,
                    "results": [{"id": parent_id, "article": {"id": article_id}}],
                },
            ) as search, mock.patch.object(XURL, "fetch_status") as fetch_status:
                with self.assertRaises(XURL.KnownUrlError) as raised:
                    XURL.resolve_article(article_id, timeout=30)
            self.assertEqual(raised.exception.category, "invalid_response")
            search.assert_called_once()
            fetch_status.assert_not_called()

    def test_failed_article_url_returns_only_article_fallback(self):
        with mock.patch.object(
            XURL,
            "fetch_json",
            side_effect=XURL.KnownUrlError("rate_limited", "FxTwitter returned HTTP 429."),
        ), mock.patch.object(XURL, "fetch_jina") as jina:
            result = XURL.read_known_url(
                "https://x.com/i/article/2032093606551707648",
                timeout=30,
                allow_jina_fallback=True,
            )
        jina.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["category"], "rate_limited")
        self.assertEqual(
            [item["argv"][:3] for item in result["authenticated_fallbacks"]],
            [["opencli", "twitter", "article"]],
        )

    def test_article_search_404_is_index_unavailable_not_not_found(self):
        # Live 2026-10-31: /2/search answered HTTP 404 for every query, even "test".
        with mock.patch.object(
            XURL,
            "fetch_json",
            side_effect=XURL.KnownUrlError("not_found", "FxTwitter returned HTTP 404."),
        ) as search, mock.patch.object(XURL, "fetch_status") as fetch_status, mock.patch.object(
            XURL, "fetch_jina"
        ) as jina:
            result = XURL.read_known_url(
                "https://x.com/i/article/2032093606551707648",
                timeout=30,
                allow_jina_fallback=True,
            )
        search.assert_called_once()
        fetch_status.assert_not_called()
        jina.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["category"], "article_index_unavailable")
        self.assertIn("/status/", result["errors"][0]["message"])
        self.assertEqual(
            [item["argv"][:3] for item in result["authenticated_fallbacks"]],
            [["opencli", "twitter", "article"]],
        )

    def test_status_404_stays_not_found(self):
        with mock.patch.object(
            XURL,
            "fetch_json",
            side_effect=XURL.KnownUrlError("not_found", "FxTwitter returned HTTP 404."),
        ), mock.patch.object(XURL, "fetch_jina", side_effect=XURL.KnownUrlError("not_found", "x")):
            result = XURL.read_known_url(
                "https://x.com/someone/status/2032093606551707648",
                timeout=30,
                allow_jina_fallback=True,
            )
        self.assertEqual(result["errors"][0]["category"], "not_found")

    def test_non_ascii_digit_ids_are_rejected_before_any_request(self):
        for url in (
            "https://x.com/a/status/١٢٣",
            "https://x.com/i/status/１２３",
            "https://x.com/i/article/١٢٣",
        ):
            with self.subTest(url=url):
                with self.assertRaises(XURL.KnownUrlError) as raised:
                    XURL.parse_known_url(url)
                self.assertEqual(raised.exception.category, "unsupported_url")

    def test_public_jina_fallback_does_not_use_login_state(self):
        with mock.patch.object(
            XURL,
            "fetch_status",
            side_effect=XURL.KnownUrlError("not_found", "missing"),
        ), mock.patch.object(
            XURL,
            "fetch_jina",
            return_value="# Public fallback",
        ):
            result = XURL.read_known_url(
                "https://x.com/example/status/2000000000000000001",
                timeout=30,
                allow_jina_fallback=True,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["route"]["backend_used"], "jina-reader")
        self.assertFalse(result["route"]["login_state_used"])
        self.assertEqual(result["authenticated_fallbacks"], [])

    def test_failed_public_chain_only_returns_authorized_fallback_plan(self):
        with mock.patch.object(
            XURL,
            "fetch_status",
            side_effect=XURL.KnownUrlError("not_found", "missing"),
        ), mock.patch.object(
            XURL,
            "fetch_jina",
            side_effect=XURL.KnownUrlError("login_wall", "login"),
        ):
            result = XURL.read_known_url(
                "https://x.com/example/status/2000000000000000001",
                timeout=30,
                allow_jina_fallback=True,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            [item["backend"] for item in result["authenticated_fallbacks"]],
            ["opencli-twitter", "xreach"],
        )
        self.assertTrue(
            all(
                item["requires_current_turn_authorization"]
                for item in result["authenticated_fallbacks"]
            )
        )


class FakeResponse:
    def __init__(self, *, body=b"{}", read_error=None):
        self.body = body
        self.read_error = read_error

    def read(self, _amount):
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class NetworkErrorTests(unittest.TestCase):
    def test_stalled_local_server_is_a_timeout_not_a_crash(self):
        # Python 3.9 raises a bare socket.timeout while waiting for headers.
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with mock.patch.object(XURL, "FX_API", f"http://127.0.0.1:{port}/2"):
            result = XURL.read_known_url(
                "https://x.com/example/status/2000000000000000001",
                timeout=1,
                allow_jina_fallback=False,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["category"], "timeout")
        self.assertEqual(result["errors"][0]["backend"], "fxtwitter-public")

    def test_fetch_json_classifies_transport_failures(self):
        cases = (
            ({"side_effect": socket.timeout("timed out")}, "timeout"),
            ({"side_effect": urllib.error.URLError(socket.timeout("timed out"))}, "timeout"),
            ({"side_effect": urllib.error.URLError(ConnectionRefusedError())}, "network_error"),
            ({"side_effect": http.client.RemoteDisconnected("closed")}, "network_error"),
            ({"side_effect": http.client.BadStatusLine("junk")}, "network_error"),
            ({"side_effect": ConnectionResetError()}, "network_error"),
            (
                {"return_value": FakeResponse(read_error=socket.timeout("timed out"))},
                "timeout",
            ),
            (
                {"return_value": FakeResponse(read_error=http.client.IncompleteRead(b"{"))},
                "network_error",
            ),
        )
        for behavior, category in cases:
            with self.subTest(behavior=behavior), mock.patch.object(
                XURL.urllib.request, "urlopen", **behavior
            ):
                with self.assertRaises(XURL.KnownUrlError) as raised:
                    XURL.fetch_json("/status/1", query=None, timeout=5)
            self.assertEqual(raised.exception.category, category)

    def test_fetch_json_keeps_http_status_categories(self):
        for code, category in ((404, "not_found"), (429, "rate_limited"), (503, "upstream_http_error")):
            error = urllib.error.HTTPError("https://api.fxtwitter.com/2", code, "x", {}, None)
            with self.subTest(code=code), mock.patch.object(
                XURL.urllib.request, "urlopen", side_effect=error
            ):
                with self.assertRaises(XURL.KnownUrlError) as raised:
                    XURL.fetch_json("/status/1", query=None, timeout=5)
            self.assertEqual(raised.exception.category, category)

    def test_fetch_jina_classifies_transport_failures(self):
        cases = (
            (socket.timeout("timed out"), "timeout"),
            (http.client.BadStatusLine("junk"), "network_error"),
            (urllib.error.URLError(ConnectionRefusedError()), "network_error"),
            (
                urllib.error.HTTPError("https://r.jina.ai/", 451, "x", {}, None),
                "jina_unavailable",
            ),
        )
        for error, category in cases:
            with self.subTest(error=type(error).__name__), mock.patch.object(
                XURL.urllib.request, "urlopen", side_effect=error
            ):
                with self.assertRaises(XURL.KnownUrlError) as raised:
                    XURL.fetch_jina(
                        "https://x.com/example/status/2000000000000000001", timeout=5
                    )
            self.assertEqual(raised.exception.category, category)


if __name__ == "__main__":
    unittest.main()
