import importlib.util
import pathlib
import sys
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "aihot_search", ROOT / "scripts" / "aihot_search.py"
)
ADAPTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = ADAPTER
SPEC.loader.exec_module(ADAPTER)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.payload[:limit]


class FakeOpener:
    def __init__(self, payload: bytes) -> None:
        self.response = FakeResponse(payload)
        self.requests = []

    def open(self, request, timeout: int):
        self.requests.append((request, timeout))
        return self.response


class AihotAdapterTests(unittest.TestCase):
    def test_fetch_json_rejects_redirects_before_following_them(self):
        handler = ADAPTER.RejectRedirects()
        redirected = handler.redirect_request(
            Request(f"{ADAPTER.BASE_URL}/api/public/items"),
            None,
            302,
            "Found",
            {},
            "http://127.0.0.1/private",
        )
        self.assertIsNone(redirected)

    def test_fetch_json_reads_one_bounded_response(self):
        opener = FakeOpener(b'{"items": []}')
        payload = ADAPTER.fetch_json(
            f"{ADAPTER.BASE_URL}/api/public/items",
            7,
            opener=opener,
        )
        self.assertEqual(payload, {"items": []})
        self.assertEqual(
            opener.response.read_limit,
            ADAPTER.MAX_RESPONSE_BYTES + 1,
        )
        self.assertEqual(opener.requests[0][1], 7)

    def test_fetch_json_rejects_oversized_response(self):
        opener = FakeOpener(b"x" * (ADAPTER.MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "maximum allowed size"):
            ADAPTER.fetch_json(
                f"{ADAPTER.BASE_URL}/api/public/items",
                7,
                opener=opener,
            )

    def test_items_are_mapped_to_unified_candidates(self):
        payload = {
            "count": 1,
            "hasNext": False,
            "items": [
                {
                    "id": "cm9abc456def789ghi012jkl3",
                    "title": "模型更新",
                    "title_en": "Model update",
                    "url": "https://example.com/model",
                    "source": "Example Lab",
                    "publishedAt": "2026-07-31T00:00:00.000Z",
                    "summary": "AI 生成摘要",
                    "category": "ai-models",
                }
            ],
        }
        result = ADAPTER.normalize_items(
            payload,
            query="今天 AI 圈有什么",
            limit=10,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        item = result["candidates"][0]
        self.assertEqual(item["candidate_id"], "aihot:cm9abc456def789ghi012jkl3")
        self.assertEqual(item["platform"], "web")
        self.assertEqual(item["backend"], "aihot")
        self.assertEqual(
            item["platform_fields"]["aihot"]["source_name"], "Example Lab"
        )
        self.assertEqual(item["verification"]["status"], "candidate")
        self.assertIn("AI-generated", item["limitations"][0])

    def test_paper_category_maps_content_type(self):
        payload = {
            "items": [
                {
                    "id": "paper-id",
                    "title": "New paper",
                    "url": "https://example.com/paper",
                    "source": "arXiv",
                    "category": "paper",
                }
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="最新 AI 论文",
            limit=5,
            days=7,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(result["candidates"][0]["content_type"], "ai_paper")

    def test_items_reject_non_public_or_unsafe_urls(self):
        unsafe_urls = [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://127.0.0.1/private",
            "http://10.0.0.1/private",
            "http://localhost/private",
            "https://intranet/private",
            "https://user:password@example.com/private",  # pragma: allowlist secret
        ]
        payload = {
            "items": [
                {"title": f"unsafe {index}", "url": url}
                for index, url in enumerate(unsafe_urls)
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="AI news",
            limit=20,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["coverage"][0]["rejected_count"], len(unsafe_urls))
        self.assertEqual(result["errors"][0]["category"], "invalid_candidate")

    def test_all_invalid_nonempty_items_fail_instead_of_claiming_zero_results(self):
        payload = {
            "items": [
                {"url": "https://example.com/missing-title"},
                {"title": "missing URL"},
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="AI news",
            limit=20,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["coverage"][0]["raw_result_count"], 2)
        self.assertEqual(result["coverage"][0]["rejected_count"], 2)
        self.assertEqual(result["candidates"], [])

    def test_mixed_valid_and_invalid_items_are_partial(self):
        payload = {
            "items": [
                {"title": "valid", "url": "https://example.com/valid"},
                {"title": 123, "url": "https://example.com/invalid-title"},
                {
                    "title": "invalid timestamp",
                    "url": "https://example.com/invalid-time",
                    "publishedAt": "2026-07-31T01:00:00",
                },
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="AI news",
            limit=20,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(result["routes"][0]["status"], "partial")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["coverage"][0]["rejected_count"], 2)
        self.assertEqual(result["errors"][0]["category"], "invalid_candidate")

    def test_valid_timestamp_is_normalized_and_language_is_not_guessed(self):
        payload = {
            "items": [
                {
                    "title": "Model release",
                    "url": "https://example.com/model",
                    "publishedAt": "2026-07-31T09:00:00+08:00",
                }
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="AI news",
            limit=20,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        item = result["candidates"][0]
        self.assertEqual(item["published_at"], "2026-07-31T01:00:00Z")
        self.assertIsNone(item["language"])

    def test_daily_sections_and_flashes_are_flattened_and_deduplicated(self):
        payload = {
            "date": "2026-07-30",
            "windowStart": "2026-07-29T00:00:00.000Z",
            "windowEnd": "2026-07-30T00:00:00.000Z",
            "sections": [
                {
                    "label": "论文研究",
                    "items": [
                        {
                            "title": "Paper",
                            "summary": "Summary",
                            "sourceUrl": "https://example.com/paper",
                            "sourceName": "arXiv",
                        }
                    ],
                }
            ],
            "flashes": [
                {
                    "title": "Paper duplicate",
                    "sourceUrl": "https://example.com/paper",
                    "sourceName": "arXiv",
                },
                {
                    "title": "Flash",
                    "sourceUrl": "https://example.com/flash",
                    "sourceName": "News",
                    "publishedAt": "2026-07-30T00:30:00.000Z",
                },
            ],
        }
        result = ADAPTER.normalize_daily(
            payload,
            query="AI 日报",
            limit=10,
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["candidates"][0]["content_type"], "ai_paper")
        self.assertEqual(
            result["request"]["time_range"]["date"], "2026-07-30"
        )
        self.assertEqual(result["coverage"][0]["duplicate_count"], 1)

    def test_nonempty_malformed_daily_rows_fail_closed(self):
        payload = {
            "date": "2026-07-30",
            "sections": [
                "not-an-object",
                {"label": "论文研究", "items": [{"title": "missing URL"}]},
            ],
            "flashes": [{"title": "private", "sourceUrl": "http://127.0.0.1"}],
        }
        result = ADAPTER.normalize_daily(
            payload,
            query="AI 日报",
            limit=10,
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["coverage"][0]["rejected_count"], 3)
        self.assertEqual(result["errors"][0]["category"], "invalid_candidate")

    def test_daily_rejects_invalid_top_level_shapes_and_time_metadata(self):
        with self.assertRaises(ValueError):
            ADAPTER.normalize_daily(
                {"sections": {}, "flashes": []},
                query="AI 日报",
                limit=10,
                retrieved_at="2026-07-31T01:00:00Z",
            )
        with self.assertRaises(ValueError):
            ADAPTER.normalize_daily(
                {
                    "date": "30-07-2026",
                    "sections": [],
                    "flashes": [],
                },
                query="AI 日报",
                limit=10,
                retrieved_at="2026-07-31T01:00:00Z",
            )

    def test_items_drop_rows_published_before_since(self):
        payload = {
            "items": [
                {
                    "title": "inside",
                    "url": "https://example.com/inside",
                    "publishedAt": "2026-07-31T00:00:00Z",
                },
                {
                    "title": "too old",
                    "url": "https://example.com/old",
                    "publishedAt": "2026-07-29T23:59:59Z",
                },
                {"title": "no time", "url": "https://example.com/no-time"},
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="AI news",
            limit=20,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
            since=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [item["url"] for item in result["candidates"]],
            ["https://example.com/inside", "https://example.com/no-time"],
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(result["errors"], [])
        coverage = result["coverage"][0]
        self.assertEqual(coverage["outside_window_count"], 1)
        self.assertEqual(coverage["time_unverified_count"], 1)
        self.assertEqual(coverage["rejected_count"], 0)
        self.assertEqual(result["request"]["time_range"]["since"], "2026-07-30T00:00:00Z")

    def test_run_items_checks_the_same_since_it_requests(self):
        now = datetime(2026, 7, 31, 12, 0, 0, 654321, tzinfo=timezone.utc)
        requested = []

        def fetch(url, timeout):
            requested.append(url)
            return {
                "items": [
                    {
                        "title": "boundary",
                        "url": "https://example.com/boundary",
                        "publishedAt": "2026-07-30T12:00:00Z",
                    },
                    {
                        "title": "one second early",
                        "url": "https://example.com/early",
                        "publishedAt": "2026-07-30T11:59:59Z",
                    },
                ]
            }

        args = Namespace(
            keyword=[], feed="selected", days=1, limit=10,
            category=None, timeout=20, query="AI news",
        )
        result = ADAPTER.run_items(args, now, fetch)
        since = parse_qs(urlsplit(requested[0]).query)["since"][0]
        self.assertEqual(since, "2026-07-30T12:00:00Z")
        self.assertEqual(
            [item["url"] for item in result["candidates"]],
            ["https://example.com/boundary"],
        )
        self.assertEqual(result["coverage"][0]["outside_window_count"], 1)

    def _paged_fetch(self, pages):
        requested = []

        def fetch(url, timeout):
            requested.append(url)
            return pages[len(requested) - 1]

        return fetch, requested

    def _page(self, index, has_next, cursor):
        return {
            "items": [{
                "title": f"item {index}",
                "url": f"https://example.com/item-{index}",
                "publishedAt": "2026-07-31T11:00:00Z",
            }],
            "hasNext": has_next,
            "nextCursor": cursor,
        }

    def test_max_pages_follows_opaque_cursor_until_has_next_is_false(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        fetch, requested = self._paged_fetch([
            self._page(1, True, "opaque+/=token"),
            self._page(2, True, "second"),
            self._page(3, False, None),
        ])
        args = Namespace(keyword=[], feed="selected", days=7, limit=300, category=None,
                         timeout=20, query="AI news", max_pages=5)
        result = ADAPTER.run_items(args, now, fetch)
        self.assertEqual(len(requested), 3)
        self.assertNotIn("cursor", parse_qs(urlsplit(requested[0]).query))
        self.assertEqual(parse_qs(urlsplit(requested[1]).query)["cursor"], ["opaque+/=token"])
        self.assertEqual(parse_qs(urlsplit(requested[2]).query)["cursor"], ["second"])
        self.assertEqual(parse_qs(urlsplit(requested[0]).query)["take"], ["100"])
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["coverage"][0]["pages_fetched"], 3)
        self.assertFalse(result["coverage"][0]["truncated"])

    def test_max_pages_cap_reports_truncation(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        fetch, requested = self._paged_fetch([
            self._page(1, True, "a"),
            self._page(2, True, "b"),
        ])
        args = Namespace(keyword=[], feed="selected", days=7, limit=200, category=None,
                         timeout=20, query="AI news", max_pages=2)
        result = ADAPTER.run_items(args, now, fetch)
        self.assertEqual(len(requested), 2)
        self.assertEqual(result["coverage"][0]["pages_fetched"], 2)
        self.assertTrue(result["coverage"][0]["truncated"])

    def test_single_page_default_never_requests_a_cursor(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        fetch, requested = self._paged_fetch([self._page(1, True, "ignored")])
        args = Namespace(keyword=[], feed="selected", days=1, limit=10, category=None,
                         timeout=20, query="AI news")
        result = ADAPTER.run_items(args, now, fetch)
        self.assertEqual(len(requested), 1)
        self.assertTrue(result["coverage"][0]["truncated"])
        self.assertNotIn("pages_fetched", result["coverage"][0])

    def test_cli_rejects_paging_for_daily_and_scales_limit_with_pages(self):
        import subprocess
        script = str(ROOT / "scripts" / "aihot_search.py")
        daily = subprocess.run([sys.executable, script, "--query", "q", "--feed", "daily",
                                "--max-pages", "2"], capture_output=True, text=True)
        self.assertEqual(daily.returncode, 2)
        self.assertIn("--max-pages", daily.stderr)
        too_many = subprocess.run([sys.executable, script, "--query", "q", "--limit", "201",
                                   "--max-pages", "2"], capture_output=True, text=True)
        self.assertEqual(too_many.returncode, 2)
        self.assertIn("between 1 and 200", too_many.stderr)

    def test_unknown_category_is_kept_and_counted(self):
        payload = {
            "items": [
                {
                    "title": "new section",
                    "url": "https://example.com/agents",
                    "category": "ai-agents",
                },
                {
                    "title": "numeric category",
                    "url": "https://example.com/numeric",
                    "category": 7,
                },
                {
                    "title": "known",
                    "url": "https://example.com/known",
                    "category": "paper",
                },
            ]
        }
        result = ADAPTER.normalize_items(
            payload,
            query="AI news",
            limit=20,
            days=1,
            feed="selected",
            retrieved_at="2026-07-31T01:00:00Z",
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["candidates"]), 3)
        first, second, third = result["candidates"]
        self.assertEqual(first["platform_fields"]["aihot"]["category"], "unknown")
        self.assertEqual(first["platform_fields"]["aihot"]["category_raw"], "ai-agents")
        self.assertEqual(first["content_type"], "ai_news")
        self.assertIn(ADAPTER.UNKNOWN_CATEGORY_LIMITATION, first["limitations"])
        self.assertEqual(second["platform_fields"]["aihot"]["category"], "unknown")
        self.assertIsNone(second["platform_fields"]["aihot"]["category_raw"])
        self.assertEqual(third["content_type"], "ai_paper")
        self.assertEqual(result["coverage"][0]["unknown_category_count"], 2)
        self.assertEqual(result["coverage"][0]["rejected_count"], 0)

    def test_daily_unknown_item_category_is_kept(self):
        payload = {
            "date": "2026-07-30",
            "sections": [
                {
                    "label": "论文研究",
                    "items": [
                        {
                            "title": "Agent paper",
                            "sourceUrl": "https://example.com/agent-paper",
                            "category": "agents",
                        }
                    ],
                }
            ],
            "flashes": [],
        }
        result = ADAPTER.normalize_daily(
            payload, query="AI 日报", limit=10, retrieved_at="2026-07-31T01:00:00Z"
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(
            result["candidates"][0]["platform_fields"]["aihot"]["category"], "unknown"
        )
        self.assertEqual(result["coverage"][0]["unknown_category_count"], 1)

    def test_items_url_contains_browser_api_parameters(self):
        url = ADAPTER.build_url(
            feed="selected",
            days=3,
            limit=20,
            keyword="OpenAI",
            category="ai-models",
            date=None,
            now=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        self.assertIn("/api/public/items?", url)
        self.assertIn("mode=selected", url)
        self.assertIn("q=OpenAI", url)
        self.assertIn("category=ai-models", url)

    def test_daily_url_can_target_a_date(self):
        url = ADAPTER.build_url(
            feed="daily",
            days=1,
            limit=20,
            keyword=None,
            category=None,
            date="2026-07-30",
            now=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        self.assertEqual(url, "https://aihot.virxact.com/api/public/daily/2026-07-30")


if __name__ == "__main__":
    unittest.main()
