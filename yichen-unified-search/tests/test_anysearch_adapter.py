import contextlib
import importlib.util
import io
import json
import os
import pathlib
import argparse
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "anysearch_adapter", ROOT / "scripts" / "anysearch_adapter.py"
)
ADAPTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = ADAPTER
SPEC.loader.exec_module(ADAPTER)

TEST_RECEIPT_SECRET = b"unit-test-receipt-secret-32-bytes-minimum"
TEST_NOW = datetime(2026, 8, 9, 1, 30, tzinfo=timezone.utc)
ORIGINAL_PAGE_MARKDOWN = "# Original page\n\n" + " ".join(
    ["Body text describing the original article in enough detail."] * 6
)
# Reddit gate page as AnySearch extracted it on 2026-10-23, typographic apostrophes included.
REDDIT_GATE_MARKDOWN = (
    "[](https://www.reddit.com)\n\n# Prove your humanity\n\n"
    "We’re committed to safety and security. But not for bots. Complete the challenge below "
    "and let us know you’re a real person.\n\n"
    "[Reddit, Inc. © \"2026\". All rights reserved.](https://www.redditinc.com/)\n\n"
    "[User Agreement](https://www.reddit.com/help/useragreement) "
    "[Privacy Policy](https://www.reddit.com/help/privacypolicy) "
    "[Content Policy](https://www.reddit.com/help/contentpolicy) "
    "[Help](https://support.reddithelp.com/hc/en-us)"
)


SEARCH_MARKDOWN = """## Search Results (2 results, 31ms)

### 1. First result
- **URL**: https://example.com/first?x=1
- First discovery snippet.

### 2. Second result
- **URL**: <https://example.org/second>
- Second discovery snippet.
"""


def candidate_fixture():
    url = "https://example.com/original"
    candidate = {
        "candidate_id": ADAPTER._candidate_id(url),
        "query": "original query",
        "platform": "web",
        "backend": "anysearch",
        "rank": 2,
        "title": "Original title",
        "url": url,
        "canonical_url": url,
        "snippet": "Original snippet",
        "author": None,
        "published_at": None,
        "content_type": "web_page",
        "language": None,
        "metrics": {
            "likes": None,
            "comments": None,
            "collects": None,
            "shares": None,
            "views": None,
        },
        "access": {"visibility": "public", "login_state_used": False},
        "verification": {
            "status": "candidate",
            "opened_original": False,
            "checked_at": None,
        },
        "provenance": {
            "source_id": None,
            "retrieved_at": "2026-08-09T01:00:00Z",
            "route_reason": "public_web_default",
        },
        "limitations": ["existing limitation"],
    }
    ADAPTER.issue_candidate_receipt(
        candidate,
        secret=TEST_RECEIPT_SECRET,
        run_id="0123456789abcdef0123456789abcdef",
    )
    return candidate


class SearchMarkdownTests(unittest.TestCase):
    def test_search_results_map_to_candidate_envelope(self):
        result = ADAPTER.normalize_search_markdown(
            SEARCH_MARKDOWN,
            query="test query",
            requested_limit=10,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["coverage"][0]["backend_reported_count"], 2)
        self.assertEqual(result["coverage"][0]["elapsed_ms"], 31)
        first = result["candidates"][0]
        self.assertEqual(first["query"], "test query")
        self.assertEqual(first["backend"], "anysearch")
        self.assertEqual(first["rank"], 1)
        self.assertEqual(first["title"], "First result")
        self.assertEqual(first["url"], "https://example.com/first?x=1")
        self.assertEqual(first["snippet"], "First discovery snippet.")
        self.assertTrue(first["candidate_id"].startswith("web:"))
        self.assertEqual(first["verification"]["status"], "candidate")
        self.assertFalse(first["verification"]["opened_original"])
        self.assertEqual(
            first["provenance"]["route_reason"], "public_web_default"
        )
        receipt = first["provenance"]["anysearch_receipt"]
        self.assertEqual(receipt["version"], 1)
        self.assertRegex(receipt["run_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(receipt["signature"], r"^[A-Za-z0-9_-]{43}$")

    def test_only_explicit_zero_header_is_successful_empty_search(self):
        result = ADAPTER.normalize_search_markdown(
            "## Search Results (0 results, 8ms)\n",
            query="nothing",
            requested_limit=3,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["coverage"][0]["backend_reported_count"], 0)

    def test_zero_header_with_unexpected_text_is_failed_parse(self):
        result = ADAPTER.normalize_search_markdown(
            "## Search Results (0 results, 8ms)\n\nUPSTREAM FORMAT ERROR\n",
            query="nothing",
            requested_limit=3,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["errors"][0]["category"], "parse_error")

    def test_nonempty_unrecognized_output_is_failed_parse_not_zero_results(self):
        result = ADAPTER.normalize_search_markdown(
            "The service changed its response format.",
            query="query",
            requested_limit=10,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["errors"][0]["category"], "parse_error")

    def test_missing_header_error_reports_shape_without_raw_output(self):
        marker = "RAW_UPSTREAM_TEXT_93"
        cases = {
            "The service changed its response format.": "text",
            f'{{"error": "{marker}"}}': "json",
            f"<html><body>{marker}</body></html>": "html_or_xml",
            f"## 搜索结果（8 条）\n\n### 1. {marker}": "markdown_heading",
        }
        for output, shape in cases.items():
            with self.subTest(shape=shape):
                result = ADAPTER.normalize_search_markdown(
                    output,
                    query="query",
                    requested_limit=10,
                    retrieved_at="2026-08-09T01:02:03Z",
                    receipt_secret=TEST_RECEIPT_SECRET,
                )
                error = result["errors"][0]
                self.assertEqual(error["category"], "parse_error")
                self.assertEqual(error["output_shape"], shape)
                self.assertEqual(error["output_chars"], len(output))
                self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))

    def test_reported_result_without_url_is_failed_parse_not_zero_results(self):
        result = ADAPTER.normalize_search_markdown(
            "## Search Results (1 results, 9ms)\n\n### 1. Missing URL\n- text",
            query="query",
            requested_limit=10,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(
            all(error["category"] == "parse_error" for error in result["errors"])
        )

    def test_count_mismatch_with_one_valid_result_is_partial(self):
        result = ADAPTER.normalize_search_markdown(
            "## Search Results (2 results, 9ms)\n\n"
            "### 1. Valid\n- **URL**: https://example.com/valid\n- snippet\n",
            query="query",
            requested_limit=10,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "partial")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["errors"][0]["category"], "parse_error")

    def test_numbered_heading_inside_snippet_is_not_a_result_boundary(self):
        markdown = """## Search Results (2 results, 15ms)

### 1. Documentation
- **URL**: https://example.com/docs
- Intro

### 2. Installation
This is a heading inside the page snippet, not a search card.

### 2. Actual second card
- **URL**: https://example.com/second
- second
"""
        section = ADAPTER.parse_search_section(markdown)
        self.assertEqual(section.status, "completed")
        self.assertEqual(len(section.rows), 2)
        self.assertIn("### 2. Installation", section.rows[0].snippet)

    def test_search_parser_rejects_nonpublic_http_hosts(self):
        urls = (
            "http://localhost/private",
            "https://service.local/private",
            "http://127.0.0.1/private",
            "http://10.0.0.1/private",
            "http://169.254.1.2/private",
            "http://192.168.1.2/private",
            "http://[::1]/private",
            "http://[fe80::1]/private",
            "http://[fc00::1]/private",
        )
        for url in urls:
            with self.subTest(url=url):
                section = ADAPTER.parse_search_section(
                    "## Search Results (1 result, 1ms)\n\n"
                    f"### 1. Internal\n- **URL**: {url}\n- private\n"
                )
                self.assertEqual(section.status, "failed")
                self.assertEqual(section.rows, ())
                self.assertTrue(section.errors)

    def test_vertical_search_records_distinct_route_reason(self):
        args = ADAPTER.parse_args(
            [
                "search",
                "AAPL",
                "--domain",
                "finance",
                "--sub-domain",
                "finance.us_stock",
                "--sub-domain-params",
                '{"ticker":"AAPL"}',
                "--limit",
                "1",
            ]
        )
        seen = {}

        def runner(runtime_conf, argv, timeout):
            seen["argv"] = argv
            return (
                "## Search Results (1 results, 1ms)\n\n"
                "### 1. Apple\n- **URL**: https://example.com/apple\n- result\n"
            )

        result = ADAPTER.execute(
            args, runner=runner, receipt_secret=TEST_RECEIPT_SECRET
        )
        self.assertIn("--sub_domain_params", seen["argv"])
        self.assertEqual(
            result["candidates"][0]["provenance"]["route_reason"],
            "anysearch_vertical_domain",
        )


class RedirectWrapperTests(unittest.TestCase):
    def test_search_engine_redirect_links_are_flagged_not_dropped(self):
        markdown = (
            "## Search Results (2 results, 9ms)\n\n"
            "### 1. Episode\n- **URL**: https://www.xiaoyuzhoufm.com/episode/abc\n- Snippet one.\n\n"
            "### 2. Podcast\n- **URL**: https://www.google.com/goto?url=CAESeAHrOzAV\n- Snippet two.\n"
        )
        result = ADAPTER.normalize_search_markdown(
            markdown, query="site:xiaoyuzhoufm.com AI", requested_limit=10,
            retrieved_at="2026-10-31T07:50:38Z", receipt_secret=TEST_RECEIPT_SECRET,
        )
        first, second = result["candidates"]
        self.assertNotIn(ADAPTER.REDIRECT_WRAPPER_LIMITATION, first["limitations"])
        self.assertIn(ADAPTER.REDIRECT_WRAPPER_LIMITATION, second["limitations"])
        self.assertEqual(second["url"], "https://www.google.com/goto?url=CAESeAHrOzAV")
        for url, expected in (
            ("https://google.com/url?q=x", True),
            ("https://www.bing.com/ck/a?u=x", True),
            ("https://www.baidu.com/link?url=x", True),
            ("https://www.google.com/search?q=x", False),
            ("https://example.com/goto?url=x", False),
        ):
            with self.subTest(url=url):
                self.assertIs(ADAPTER.is_search_redirect_wrapper(url), expected)


class BatchMarkdownTests(unittest.TestCase):
    def test_batch_sections_retain_each_submitted_query_and_rank(self):
        markdown = """## Query 1: response label one

## Search Results (1 results, 4ms)

### 1. Alpha
- **URL**: https://example.com/alpha
- alpha snippet

---

## Query 2: response label two

## Search Results (1 result, 5ms)

### 1. Beta
- **URL**: https://example.com/beta
- beta snippet
"""
        query_items = [
            {"query": "original alpha", "max_results": 1},
            {"query": "original beta", "max_results": 1},
        ]
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=query_items,
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(
            [candidate["query"] for candidate in result["candidates"]],
            ["original alpha", "original beta"],
        )
        self.assertEqual(
            [candidate["rank"] for candidate in result["candidates"]], [1, 1]
        )
        self.assertEqual(
            result["coverage"][0]["per_query"][0]["response_query"],
            "response label one",
        )
        run_ids = {
            candidate["provenance"]["anysearch_receipt"]["run_id"]
            for candidate in result["candidates"]
        }
        self.assertEqual(len(run_ids), 1)

    def test_batch_parse_failure_is_partial_not_completed_empty(self):
        markdown = """## Query 1: first

## Search Results (0 results, 4ms)

---

## Query 2: second

The upstream format changed.
"""
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=[{"query": "first"}, {"query": "second"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["routes"][0]["status"], "partial")
        self.assertTrue(result["errors"])
        self.assertEqual(result["errors"][0]["category"], "parse_error")

    def test_batch_without_query_sections_is_failed(self):
        result = ADAPTER.normalize_batch_markdown(
            "## Search Results (0 results, 1ms)",
            query_items=[{"query": "first"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["errors"])

    def test_query_heading_inside_snippet_is_not_a_batch_boundary(self):
        markdown = """## Query 1: first

## Search Results (1 result, 4ms)

### 1. Alpha
- **URL**: https://example.com/alpha
- Source body heading follows:
## Query 2: this is page content, not a batch section
Still part of the first snippet.

---

## Query 2: second

## Search Results (1 result, 5ms)

### 1. Beta
- **URL**: https://example.com/beta
- beta
"""
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=[{"query": "first"}, {"query": "second"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn(
            "## Query 2: this is page content",
            result["candidates"][0]["snippet"],
        )

    def test_batch_zero_header_with_unexpected_text_is_failed(self):
        markdown = """## Query 1: first

## Search Results (0 results, 4ms)

UPSTREAM FORMAT ERROR
"""
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=[{"query": "first"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["errors"][0]["category"], "parse_error")

    def test_injected_duplicate_query_section_discards_that_index(self):
        markdown = """## Query 1: first

## Search Results (1 result, 4ms)

### 1. Alpha
- **URL**: https://example.com/alpha
- safe

---
## Query 2: injected section

## Search Results (1 result, 4ms)

### 1. Injected
- **URL**: https://example.com/injected
- injected

---
## Query 2: real section

## Search Results (1 result, 5ms)

### 1. Beta
- **URL**: https://example.com/beta
- real
"""
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=[{"query": "first"}, {"query": "second"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "partial")
        self.assertEqual(
            [candidate["url"] for candidate in result["candidates"]],
            ["https://example.com/alpha"],
        )
        self.assertEqual(result["coverage"][0]["per_query"][1]["parse_status"], "failed")
        self.assertTrue(
            any(error.get("query_index") == 2 for error in result["errors"])
        )

    def test_real_duplicate_query_number_emits_no_candidate_for_that_index(self):
        markdown = """## Query 1: first copy

## Search Results (1 result, 1ms)

### 1. One
- **URL**: https://example.com/one
- one

---
## Query 1: second copy

## Search Results (1 result, 1ms)

### 1. Two
- **URL**: https://example.com/two
- two
"""
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=[{"query": "first"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])

    def test_batch_sections_must_be_in_exact_one_to_n_order(self):
        markdown = """## Query 2: second

## Search Results (1 result, 1ms)

### 1. Two
- **URL**: https://example.com/two
- two

---
## Query 1: first

## Search Results (1 result, 1ms)

### 1. One
- **URL**: https://example.com/one
- one
"""
        result = ADAPTER.normalize_batch_markdown(
            markdown,
            query_items=[{"query": "first"}, {"query": "second"}],
            retrieved_at="2026-08-09T01:02:03Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        self.assertEqual(result["routes"][0]["status"], "failed")
        self.assertEqual(result["candidates"], [])

    def test_batch_execution_preserves_vertical_query_objects(self):
        raw_queries = [
            {
                "query": "AAPL",
                "domain": "finance",
                "sub_domain": "finance.us_stock",
                "sub_domain_params": {"ticker": "AAPL"},
                "max_results": 1,
            }
        ]
        args = ADAPTER.parse_args(
            ["batch", "--queries", json.dumps(raw_queries, ensure_ascii=False)]
        )
        seen = {}

        def runner(runtime_conf, argv, timeout):
            seen["argv"] = argv
            return """## Query 1: AAPL

## Search Results (1 result, 3ms)

### 1. Apple
- **URL**: https://example.com/apple
- result
"""

        result = ADAPTER.execute(
            args, runner=runner, receipt_secret=TEST_RECEIPT_SECRET
        )
        self.assertEqual(seen["argv"][:2], ["batch_search", "--queries"])
        self.assertEqual(json.loads(seen["argv"][2]), raw_queries)
        self.assertEqual(
            result["candidates"][0]["provenance"]["route_reason"],
            "anysearch_vertical_domain_batch",
        )

    def test_batch_rejects_fields_that_could_bypass_cli_contract(self):
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.parse_batch_queries(
                '[{"query":"safe","api_key":"must-not-be-forwarded"}]'  # pragma: allowlist secret
            )


class VerificationTests(unittest.TestCase):
    def test_verify_preserves_candidate_and_attaches_extract_markdown(self):
        original = candidate_fixture()
        result = ADAPTER.verification_envelope(
            original,
            extract_markdown=ORIGINAL_PAGE_MARKDOWN,
            checked_at="2026-08-09T02:03:04Z",
            receipt_secret=TEST_RECEIPT_SECRET,
            now=TEST_NOW,
        )
        verified = result["candidates"][0]
        for field in (
            "candidate_id",
            "query",
            "backend",
            "rank",
            "title",
            "url",
            "snippet",
        ):
            self.assertEqual(verified[field], original[field])
        self.assertTrue(verified["verification"]["opened_original"])
        self.assertEqual(verified["verification"]["status"], "candidate")
        self.assertEqual(
            verified["platform_fields"]["anysearch"]["extract_markdown"],
            ORIGINAL_PAGE_MARKDOWN,
        )
        verification_provenance = verified["provenance"]["verification"]
        self.assertEqual(verification_provenance["source_query"], "original query")
        self.assertEqual(
            verification_provenance["candidate_id"], original["candidate_id"]
        )
        self.assertEqual(
            verification_provenance["route_reason"],
            "verify_candidate_from_current_search",
        )
        self.assertEqual(result["routes"][0]["mode"], "verify")
        self.assertEqual(original["verification"]["opened_original"], False)

    def test_verify_rejects_extract_failure_output(self):
        # Observed 2026-10-12: AnySearch printed this sentinel and the adapter
        # still reported opened_original=true with a completed route.
        for output in (
            "extract_failed\nUnable to extract content from the URL.",
            "Unable to extract content from the URL.",
        ):
            with self.subTest(output=output):
                with self.assertRaises(ADAPTER.AdapterError) as raised:
                    ADAPTER.verification_envelope(
                        candidate_fixture(),
                        extract_markdown=output,
                        checked_at="2026-08-09T02:03:04Z",
                        receipt_secret=TEST_RECEIPT_SECRET,
                        now=TEST_NOW,
                    )
                self.assertEqual(raised.exception.category, "extract_failed")

    def test_verify_rejects_navigation_stub_as_unread(self):
        # A GitHub README extraction returned only 54 characters.
        stub = "# example/repo\n\nSign in · Code · Issues · Pull requests"
        self.assertLess(
            ADAPTER.visible_char_count(stub), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS
        )
        with self.assertRaises(ADAPTER.AdapterError) as raised:
            ADAPTER.verification_envelope(
                candidate_fixture(),
                extract_markdown=stub,
                checked_at="2026-08-09T02:03:04Z",
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )
        self.assertEqual(raised.exception.category, "content_too_short")
        self.assertIn("treat the original as unread", raised.exception.message)

    def test_verify_rejects_block_and_verification_pages(self):
        # Observed 2026-10-13 during attendee research: these reported opened_original=true.
        url = candidate_fixture()["url"]
        boss = json.dumps({"url": url, "title": "", "content": json.dumps(
            {"code": 37, "message": "您的环境存在异常.",
             "zpData": {"seed": "FXaP5/VMocemIpFUNSdhUiyJqbLTK678+4SgaMTK5t0=", "name": "8552bbf3", "ts": 1791868526551}},
            ensure_ascii=False)}, ensure_ascii=False)
        baidu = json.dumps({"url": url, "title": "百度安全验证", "content": "\n\n".join(
            ["网络不给力，请稍后重试", "问题反馈", "百度安全验证", "请完成下方验证后继续操作", "正在验证...",
             "验证通过", "图片未转正", "网络不给力，请刷新重试", "加载中...", "图片由AI生成", "拖动左侧滑块使图片为正",
             "扫码验证|[意见反馈](https://wappass.baidu.com/static/activity/feedback.html?source=36686)|刷新"])},
            ensure_ascii=False)
        redirected = json.dumps({"url": "https://www.zhipin.com/web/passport/zp/security.html?seed=abc",
                                 "title": "BOSS直聘", "content": ORIGINAL_PAGE_MARKDOWN}, ensure_ascii=False)
        jobui = "系统检测到访问异常 为保障账户安全，请输入下方验证码完成验证： 验证码（点击图片可刷新） 提交验证"
        # Observed 2026-10-23: Reddit's gate was reported as an opened original (exit 0).
        reddit = json.dumps({"url": url, "title": "Reddit - Prove your humanity", "content": REDDIT_GATE_MARKDOWN},
                            ensure_ascii=False)
        reddit_untitled = json.dumps({"url": url, "title": "", "content": REDDIT_GATE_MARKDOWN.replace(
            "# Prove your humanity\n\n", "")}, ensure_ascii=False)
        self.assertGreaterEqual(ADAPTER.visible_char_count(boss), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS)
        self.assertGreaterEqual(
            ADAPTER.visible_char_count(REDDIT_GATE_MARKDOWN), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS
        )
        for name, output in (("boss", boss), ("baidu", baidu), ("redirect", redirected), ("jobui", jobui),
                             ("reddit", reddit), ("reddit_body_only", reddit_untitled)):
            with self.subTest(page=name):
                with self.assertRaises(ADAPTER.AdapterError) as raised:
                    ADAPTER.verification_envelope(
                        candidate_fixture(),
                        extract_markdown=output,
                        checked_at="2026-08-09T02:03:04Z",
                        receipt_secret=TEST_RECEIPT_SECRET,
                        now=TEST_NOW,
                    )
                self.assertEqual(raised.exception.category, "blocked_or_verification")
                self.assertIn("treat the original as unread", raised.exception.message)

    def test_verify_counts_page_content_not_the_json_wrapper(self):
        url = candidate_fixture()["url"] + "?with=a&long=query&string=that&pads=the&wrapper=length"
        wrapped = json.dumps({"url": url, "title": "A company profile page with a long descriptive title",
                              "content": "公司简介：" + "专注智能硬件研发与销售。" * 8}, ensure_ascii=False)
        self.assertGreaterEqual(ADAPTER.visible_char_count(wrapped), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS)
        with self.assertRaises(ADAPTER.AdapterError) as raised:
            ADAPTER.verification_envelope(
                candidate_fixture(),
                extract_markdown=wrapped,
                checked_at="2026-08-09T02:03:04Z",
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )
        self.assertEqual(raised.exception.category, "content_too_short")

    def test_articles_that_mention_gates_stay_readable(self):
        # Navigation with 验证码登录, a long title about 人机验证, and gate wording deep in a long body.
        body = ("[首页](https://example.com/) | 验证码登录 | 注册\n\n# 正文\n\n"
                + "滑块验证码的发展经历了多个阶段，本文梳理其技术原理与用户体验的权衡。" * 70
                + "\n\n早期方案要求用户拖动左侧滑块使图片为正，后来逐步被无感验证取代。")
        self.assertGreater(ADAPTER.visible_char_count(body), ADAPTER.BLOCK_SHORT_PAGE_CHARS)
        article = json.dumps({"url": candidate_fixture()["url"],
                              "title": "从滑块到无感：人机验证技术十年演进与实践复盘", "content": body},
                             ensure_ascii=False)
        result = ADAPTER.verification_envelope(
            candidate_fixture(),
            extract_markdown=article,
            checked_at="2026-08-09T02:03:04Z",
            receipt_secret=TEST_RECEIPT_SECRET,
            now=TEST_NOW,
        )
        self.assertTrue(result["candidates"][0]["verification"]["opened_original"])
        fields = result["candidates"][0]["platform_fields"]["anysearch"]
        self.assertEqual(fields["extract_markdown"], body.strip())
        self.assertEqual(fields["extract_title"], "从滑块到无感：人机验证技术十年演进与实践复盘")

    def test_live_json_wrapper_is_unpacked_into_page_fields(self):
        # Live 2026-10-31 verify: extract printed this compact wrapper (content trimmed here)
        # and the whole JSON text was stored as extract_markdown.
        url = "https://discuss.python.org/t/about-free-threading-performance/54604"
        title = "About free-threading performance - Python Help - Discussions on Python.org"
        content = (
            "# [About free-threading performance](https://discuss.python.org/t/about-free-threading-performance/54604)\n\n"
            "[Python Help](https://discuss.python.org/c/help/7)\n\n"
            "[free-threading](https://discuss.python.org/tag/free-threading)\n\n"
            "[FelixFourcolor](https://discuss.python.org/u/FelixFourcolor) (Felix Fourcolor) May 30, 2024, 4:08pm 1\n\n"
            "I was very excited to test free-threading performance when 3.13 beta came out "
            "(thank you for everyone who works on it). Here’s what I found.\n\nfib.py\n\n"
            "```python\nfrom itertools import repeat\n\ndef fib(n):\n    if n < 3:\n        return n\n"
            "    return fib(n-1) + fib(n-2)\n\n# calculate the 30th fibonacci number 16 times\n"
            "# sequentially\nfor i in repeat(30, 16):\n    print(fib(i))\n```"
        )
        output = json.dumps({"url": url, "title": title, "content": content},
                            ensure_ascii=False, separators=(",", ":"))
        self.assertGreaterEqual(ADAPTER.visible_char_count(content), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS)
        candidate = fresh_candidate(url)
        result = ADAPTER.verification_envelope(
            candidate, extract_markdown=output + "\n", checked_at="2026-10-31T07:40:48Z",
            receipt_secret=TEST_RECEIPT_SECRET,
        )
        fields = result["candidates"][0]["platform_fields"]["anysearch"]
        self.assertEqual(fields["extract_markdown"], content)
        self.assertEqual(fields["extract_title"], title)
        self.assertEqual(fields["extract_url"], url)
        self.assertEqual(fields["extract_retrieved_at"], "2026-10-31T07:40:48Z")
        self.assertTrue(result["candidates"][0]["verification"]["opened_original"])

    def test_non_json_extract_output_is_stored_as_page_text(self):
        for output in (ORIGINAL_PAGE_MARKDOWN, "[" + ORIGINAL_PAGE_MARKDOWN,
                       json.dumps({"title": "no content key", "body": ORIGINAL_PAGE_MARKDOWN})):
            with self.subTest(output=output[:30]):
                result = ADAPTER.verification_envelope(
                    candidate_fixture(), extract_markdown=output, checked_at="2026-08-09T02:03:04Z",
                    receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW,
                )
                fields = result["candidates"][0]["platform_fields"]["anysearch"]
                self.assertEqual(fields["extract_markdown"], output)
                self.assertNotIn("extract_title", fields)
                self.assertNotIn("extract_url", fields)
        untitled = json.dumps({"url": "", "title": "", "content": ORIGINAL_PAGE_MARKDOWN})
        fields = ADAPTER.verification_envelope(
            candidate_fixture(), extract_markdown=untitled, checked_at="2026-08-09T02:03:04Z",
            receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW,
        )["candidates"][0]["platform_fields"]["anysearch"]
        self.assertEqual(fields["extract_markdown"], ORIGINAL_PAGE_MARKDOWN)
        self.assertNotIn("extract_title", fields)
        self.assertNotIn("extract_url", fields)

    def test_english_articles_that_quote_humanity_checks_stay_readable(self):
        # A long title about the gate and gate wording deep in a long body are not the gate itself.
        body = ("# Design notes\n\n"
                + "Bot checks trade friction for safety, and this review compares their copy. " * 40
                + "\n\nOne interstitial asks visitors to prove your humanity and let us know you’re a real person.")
        self.assertGreater(ADAPTER.visible_char_count(body), ADAPTER.BLOCK_SHORT_PAGE_CHARS)
        article = json.dumps({"url": candidate_fixture()["url"],
                              "title": "Why sites ask you to prove your humanity before reading a thread",
                              "content": body}, ensure_ascii=False)
        result = ADAPTER.verification_envelope(
            candidate_fixture(),
            extract_markdown=article,
            checked_at="2026-08-09T02:03:04Z",
            receipt_secret=TEST_RECEIPT_SECRET,
            now=TEST_NOW,
        )
        self.assertTrue(result["candidates"][0]["verification"]["opened_original"])

    def test_verify_requires_anysearch_candidate_status(self):
        wrong_backend = candidate_fixture()
        wrong_backend["backend"] = "aihot"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                wrong_backend, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

        already_verified = candidate_fixture()
        already_verified["verification"]["status"] = "verified"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                already_verified, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

    def test_structurally_complete_known_url_without_receipt_is_rejected(self):
        forged = candidate_fixture()
        url = "https://example.net/known-url"
        forged["candidate_id"] = ADAPTER._candidate_id(url)
        forged["query"] = "known URL"
        forged["url"] = url
        forged["canonical_url"] = url
        forged["provenance"].pop("anysearch_receipt")
        with self.assertRaisesRegex(ADAPTER.AdapterError, "receipt"):
            ADAPTER.validate_candidate_from_search(
                forged, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

    def test_receipt_binds_query_url_id_and_retrieval_timestamp(self):
        mutations = (
            ("query", "different query"),
            ("url", "https://example.com/different"),
            ("candidate_id", "web:" + "0" * 20),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                tampered = candidate_fixture()
                tampered[field] = value
                if field == "url":
                    tampered["canonical_url"] = value
                    tampered["candidate_id"] = ADAPTER._candidate_id(value)
                with self.assertRaises(ADAPTER.AdapterError):
                    ADAPTER.validate_candidate_from_search(
                        tampered,
                        receipt_secret=TEST_RECEIPT_SECRET,
                        now=TEST_NOW,
                    )

        tampered_time = candidate_fixture()
        tampered_time["provenance"]["retrieved_at"] = "2026-08-09T01:01:00Z"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                tampered_time,
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )

    def test_receipt_rejects_wrong_signature_key_and_expiration(self):
        bad_signature = candidate_fixture()
        bad_signature["provenance"]["anysearch_receipt"]["signature"] = "A" * 43
        with self.assertRaisesRegex(ADAPTER.AdapterError, "signature"):
            ADAPTER.validate_candidate_from_search(
                bad_signature,
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )

        with self.assertRaisesRegex(ADAPTER.AdapterError, "signature"):
            ADAPTER.validate_candidate_from_search(
                candidate_fixture(),
                receipt_secret=b"different-unit-test-receipt-key-32-bytes",
                now=TEST_NOW,
            )

        expired_at = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ADAPTER.AdapterError, "expired"):
            ADAPTER.validate_candidate_from_search(
                candidate_fixture(),
                receipt_secret=TEST_RECEIPT_SECRET,
                now=expired_at,
            )

    def test_verify_rejects_private_or_local_url(self):
        local = candidate_fixture()
        local["url"] = "http://127.0.0.1/private"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                local, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

    def test_public_url_validation_rejects_normalized_and_legacy_loopback_hosts(self):
        unsafe_urls = (
            "https://%31%32%37.0.0.1/private",
            "https://１２７。０。０。１/private",
            "https://0x7f.1/private",
            "https://0177.0.0.1/private",
            "https://2130706433/private",
            "https://127.1/private",
            "https://intranet/private",
            "https://example.com:invalid/private",
        )
        for url in unsafe_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    ADAPTER.validate_public_http_url(url)

        self.assertEqual(
            ADAPTER.validate_public_http_url("https://8.8.8.8/public"),
            "https://8.8.8.8/public",
        )

    def test_public_host_uses_modern_uts46_without_legacy_target_changes(self):
        self.assertEqual(ADAPTER._normalize_host_uts46("faß.de"), "xn--fa-hia.de")
        self.assertEqual(ADAPTER._normalize_host_uts46("ς.gr"), "xn--3xa.gr")
        self.assertEqual(
            ADAPTER._normalize_host_uts46("Bücher.de"), "xn--bcher-kva.de"
        )
        with self.assertRaises(ValueError):
            ADAPTER._normalize_host_uts46("ab\u200ccd.com")
        with mock.patch.object(ADAPTER, "_idna_uts46", None):
            self.assertEqual(ADAPTER._normalize_host_uts46("example.com"), "example.com")
            with self.assertRaises(ValueError):
                ADAPTER._normalize_host_uts46("Bücher.de")

    def test_verify_rejects_forged_or_inconsistent_candidate(self):
        mismatched_id = candidate_fixture()
        mismatched_id["candidate_id"] = "web:forged"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                mismatched_id, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

        missing_provenance = candidate_fixture()
        missing_provenance.pop("provenance")
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                missing_provenance, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

        nonpublic = candidate_fixture()
        nonpublic["access"]["visibility"] = "authenticated_public"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                nonpublic, receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW
            )

        mismatched_canonical = candidate_fixture()
        mismatched_canonical["canonical_url"] = "https://example.net/other"
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                mismatched_canonical,
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )

        minimal_forgery = {
            "backend": "anysearch",
            "candidate_id": "web:forged",
            "query": "known URL",
            "url": "https://example.com/known",
            "verification": {"status": "candidate"},
        }
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.validate_candidate_from_search(
                minimal_forgery,
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )

    def test_candidate_from_search_can_be_loaded_from_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "candidate.json"
            path.write_text(json.dumps(candidate_fixture()), encoding="utf-8")
            loaded = ADAPTER.load_candidate_argument(
                f"@{path}",
                receipt_secret=TEST_RECEIPT_SECRET,
                now=TEST_NOW,
            )
        self.assertEqual(loaded["candidate_id"], candidate_fixture()["candidate_id"])

    def test_verify_cli_has_no_url_or_candidate_json_bypass(self):
        parser = ADAPTER.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["verify", "--url", "https://example.com"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["verify", "--candidate-json", "{}"])


class RuntimeSafetyTests(unittest.TestCase):
    def test_quota_exhaustion_is_distinct_from_temporary_rate_limit(self):
        self.assertEqual(
            ADAPTER._failure_category("You've hit your usage limit", "")[0],
            "quota_exhausted",
        )
        self.assertEqual(ADAPTER._failure_category("", "HTTP 429")[0], "rate_limited")
        self.assertEqual(ADAPTER._failure_category("", "network timeout")[0], "network_error")

    def test_zero_exit_code_with_explicit_quota_message_is_not_parse_error(self):
        completed = subprocess.CompletedProcess([], 0, "You've hit your usage limit", "")
        with mock.patch.object(ADAPTER, "read_runtime_command", return_value="/bin/true"):
            with mock.patch.object(ADAPTER, "prepare_runtime_invocation", return_value=(["/bin/true"], {})):
                with mock.patch.object(ADAPTER.subprocess, "run", return_value=completed):
                    with self.assertRaises(ADAPTER.AdapterError) as caught:
                        ADAPTER.run_anysearch(pathlib.Path("/tmp/example.conf"), ["search", "test"], 5)
        self.assertEqual(caught.exception.category, "quota_exhausted")

    def test_runtime_conf_override_must_be_absolute(self):
        args = ADAPTER.build_parser().parse_args(
            ["search", "public query", "--runtime-conf", "relative/runtime.conf"]
        )
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            ADAPTER.execute(args, runner=lambda *_: "")
        self.assertEqual(caught.exception.category, "configuration_error")

    def test_search_receipt_verifies_across_cli_processes_with_private_key_file(self):
        self._search_then_verify_across_processes(
            extract_output="# Original page\n\n" + "Body text of the original page. " * 12,
            expect_opened=True,
        )

    def test_verify_cli_reports_extract_failure_with_exit_code_two(self):
        self._search_then_verify_across_processes(
            extract_output="extract_failed\nUnable to extract content from the URL.",
            expect_opened=False,
        )

    def _search_then_verify_across_processes(self, *, extract_output, expect_opened):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake_cli = root / "fake_anysearch.py"
            search_output = (
                "## Search Results (1 result, 1ms)\n\n"
                "### 1. Cross-process result\n"
                "- **URL**: https://example.com/cross-process\n"
                "- result\n"
            )
            fake_cli.write_text(
                "import sys\n\n"
                "if sys.argv[1] == 'search':\n"
                f"    print({search_output!r})\n"
                "elif sys.argv[1] == 'extract':\n"
                f"    print({extract_output!r})\n"
                "else:\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            runtime_conf = root / "runtime.conf"
            runtime_conf.write_text(
                f'Command: {sys.executable} "{fake_cli}"\n', encoding="utf-8"
            )
            key_file = root / "receipt.key"
            environment = dict(os.environ)
            environment.pop(ADAPTER.RECEIPT_KEY_ENV, None)
            environment[ADAPTER.RECEIPT_KEY_FILE_ENV] = str(key_file)
            adapter_path = ROOT / "scripts" / "anysearch_adapter.py"

            searched = subprocess.run(
                [
                    sys.executable,
                    str(adapter_path),
                    "search",
                    "cross process",
                    "--runtime-conf",
                    str(runtime_conf),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(searched.returncode, 0, searched.stderr)
            candidate = json.loads(searched.stdout)["candidates"][0]
            self.assertTrue(key_file.exists())
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

            verified = subprocess.run(
                [
                    sys.executable,
                    str(adapter_path),
                    "verify",
                    "--candidate-from-search",
                    json.dumps(candidate, ensure_ascii=False),
                    "--runtime-conf",
                    str(runtime_conf),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            envelope = json.loads(verified.stdout)
            if expect_opened:
                self.assertEqual(verified.returncode, 0, verified.stderr)
                opened = envelope["candidates"][0]
                self.assertTrue(opened["verification"]["opened_original"])
            else:
                self.assertEqual(verified.returncode, 2, verified.stderr)
                self.assertEqual(envelope["candidates"], [])
                self.assertEqual(envelope["routes"][0]["status"], "failed")
                self.assertEqual(envelope["errors"][0]["category"], "extract_failed")

    def test_runtime_command_is_split_with_shlex(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text(
                'Runtime: Python\nCommand: env MODE=test python3 "/tmp/path with space/cli.py"\n',
                encoding="utf-8",
            )
            command = ADAPTER.read_runtime_command(conf)
        self.assertEqual(
            command,
            ["env", "MODE=test", "python3", "/tmp/path with space/cli.py"],
        )

    def test_runtime_executes_argv_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text(
                "Command: env MODE=test python3 /tmp/cli.py\n", encoding="utf-8"
            )
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr="warning"
            )
            with mock.patch.object(
                ADAPTER.subprocess, "run", return_value=completed
            ) as run:
                output = ADAPTER.run_anysearch(
                    conf, ["search", "literal;not-a-command"], 10
                )
        self.assertEqual(output, "ok")
        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:2], ["python3", "/tmp/cli.py"])
        self.assertEqual(argv[-2:], ["search", "literal;not-a-command"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["env"]["MODE"], "test")

    def test_failed_runtime_output_is_classified_without_leaking_key(self):
        secret = "supersecret-api-key"  # pragma: allowlist secret
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text(
                f"Command: env ANYSEARCH_API_KEY={secret} python3 /tmp/cli.py\n",
                encoding="utf-8",
            )
            failed = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"HTTP 401 Authorization Bearer {secret}",
            )
            with mock.patch.object(ADAPTER.subprocess, "run", return_value=failed):
                with self.assertRaises(ADAPTER.AdapterError) as caught:
                    ADAPTER.run_anysearch(conf, ["search", "query"], 10)
        self.assertEqual(caught.exception.category, "authentication_error")
        self.assertNotIn(secret, caught.exception.message)
        envelope = ADAPTER.error_envelope(
            queries=["query"],
            mode="search",
            category=caught.exception.category,
            message=caught.exception.message,
            requested_limit=10,
        )
        self.assertNotIn(secret, json.dumps(envelope))

    def test_runtime_secret_is_environment_only_not_process_argv(self):
        secret = "environment-only-secret"  # pragma: allowlist secret
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text(
                f"Command: env ANYSEARCH_API_KEY={secret} python3 /tmp/cli.py\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr=""
            )
            with mock.patch.object(
                ADAPTER.subprocess, "run", return_value=completed
            ) as run:
                ADAPTER.run_anysearch(conf, ["search", "query"], 10)
        process_argv = run.call_args.args[0]
        self.assertNotIn(secret, " ".join(process_argv))
        self.assertEqual(
            run.call_args.kwargs["env"]["ANYSEARCH_API_KEY"], secret
        )

    def test_receipt_key_environment_is_not_forwarded_to_anysearch(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text("Command: python3 /tmp/cli.py\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr=""
            )
            with mock.patch.dict(
                os.environ,
                {ADAPTER.RECEIPT_KEY_ENV: "x" * 40},  # pragma: allowlist secret
                clear=False,
            ), mock.patch.object(
                ADAPTER.subprocess, "run", return_value=completed
            ) as run:
                ADAPTER.run_anysearch(conf, ["search", "query"], 10)
        child_environment = run.call_args.kwargs["env"]
        self.assertNotIn(ADAPTER.RECEIPT_KEY_ENV, child_environment)
        self.assertNotIn(ADAPTER.RECEIPT_KEY_FILE_ENV, child_environment)

    def test_unrelated_secrets_are_not_forwarded_to_anysearch(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text(
                "Command: env ANYSEARCH_API_KEY=runtime-key python3 /tmp/cli.py\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr=""
            )
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin:/bin",
                    "OPENAI_API_KEY": "openai-secret",  # pragma: allowlist secret
                    "GITHUB_TOKEN": "github-secret",  # pragma: allowlist secret
                    "FIRECRAWL_API_KEY": "firecrawl-secret",  # pragma: allowlist secret
                    "YOUTUBE_API_KEY": "youtube-secret",  # pragma: allowlist secret
                },
                clear=True,
            ), mock.patch.object(
                ADAPTER.subprocess, "run", return_value=completed
            ) as run:
                ADAPTER.run_anysearch(conf, ["search", "query"], 10)
        child_environment = run.call_args.kwargs["env"]
        self.assertEqual(child_environment["ANYSEARCH_API_KEY"], "runtime-key")
        self.assertEqual(child_environment["PATH"], "/usr/bin:/bin")
        for name in (
            "OPENAI_API_KEY", "GITHUB_TOKEN", "FIRECRAWL_API_KEY", "YOUTUBE_API_KEY"
        ):
            self.assertNotIn(name, child_environment)

    def test_timeout_does_not_chain_raw_command_or_captured_output(self):
        secret = "must-not-appear-in-traceback"  # pragma: allowlist secret
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text(
                f"Command: env ANYSEARCH_API_KEY={secret} python3 /tmp/cli.py\n",
                encoding="utf-8",
            )
            timeout = subprocess.TimeoutExpired(
                cmd=["env", f"ANYSEARCH_API_KEY={secret}"],
                timeout=10,
                output=secret,
                stderr=secret,
            )
            with mock.patch.object(
                ADAPTER.subprocess, "run", side_effect=timeout
            ) as run:
                with self.assertRaises(ADAPTER.AdapterError) as caught:
                    ADAPTER.run_anysearch(conf, ["search", "query"], 10)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        rendered = "".join(
            traceback.format_exception(
                type(caught.exception), caught.exception, caught.exception.__traceback__
            )
        )
        self.assertNotIn(secret, rendered)
        self.assertNotIn(secret, " ".join(run.call_args.args[0]))

    def test_runtime_rejects_api_key_cli_flag(self):
        with self.assertRaises(ADAPTER.AdapterError):
            ADAPTER.prepare_runtime_invocation(
                ["python3", "/tmp/cli.py", "--api_key", "secret"]
            )

    def test_source_never_requests_shell_execution(self):
        source = (ROOT / "scripts" / "anysearch_adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("shell" + "=True", source)


def fresh_candidate(url="https://example.com/post"):
    """A candidate signed now, for paths that validate receipts against the clock."""

    markdown = f"## Search Results (1 results)\n\n### 1. Title\n- **URL**: {url}\n- snippet\n"
    return ADAPTER.normalize_search_markdown(
        markdown,
        query="q",
        requested_limit=1,
        retrieved_at=ADAPTER.utc_now_iso(),
        receipt_secret=TEST_RECEIPT_SECRET,
    )["candidates"][0]


def run_with_stdout(stdout, argv, *, returncode=0, stderr=""):
    completed = subprocess.CompletedProcess([], returncode, stdout, stderr)
    with mock.patch.object(ADAPTER, "read_runtime_command", return_value=["/bin/true"]), \
            mock.patch.object(ADAPTER, "prepare_runtime_invocation", return_value=(["/bin/true"], {})), \
            mock.patch.object(ADAPTER.subprocess, "run", return_value=completed):
        return ADAPTER.run_anysearch(pathlib.Path("/nonexistent/runtime.conf"), argv, 5)


class ExtractOutputIsPageTextTests(unittest.TestCase):
    QUOTA_ARTICLE = json.dumps({
        "url": "https://example.com/post",
        "title": "How to fix usage limits",
        "content": "When the tool says You've hit your usage limit or quota exceeded, wait for the reset. " * 6,
    })

    def test_extract_output_quoting_quota_words_is_not_an_anysearch_quota_error(self):
        output = run_with_stdout(self.QUOTA_ARTICLE, ["extract", "https://example.com/post"])
        self.assertEqual(output, self.QUOTA_ARTICLE)
        # The bare notice on a search command is still AnySearch's own quota signal.
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            run_with_stdout("You've hit your usage limit", ["search", "--max_results", "5", "--", "q"])
        self.assertEqual(caught.exception.category, "quota_exhausted")

    def test_verify_of_article_about_quotas_opens_the_original(self):
        candidate = fresh_candidate()
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text("Command: /bin/echo\n", encoding="utf-8")
            args = ADAPTER.parse_args([
                "verify", "--candidate-from-search", json.dumps(candidate), "--runtime-conf", str(conf),
            ])
            completed = subprocess.CompletedProcess([], 0, self.QUOTA_ARTICLE, "")
            with mock.patch.object(ADAPTER.subprocess, "run", return_value=completed) as run:
                result = ADAPTER.execute(args, receipt_secret=TEST_RECEIPT_SECRET)
        self.assertEqual(run.call_args.args[0][-2:], ["extract", candidate["url"]])
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertTrue(result["candidates"][0]["verification"]["opened_original"])

    def test_extract_failure_sentinel_counts_only_at_the_start(self):
        url = candidate_fixture()["url"]
        quoted = json.dumps({"url": url, "title": "Scraper FAQ", "content": "A long FAQ body. " * 30
                             + "Error text seen: Unable to extract content from the URL."})
        result = ADAPTER.verification_envelope(
            candidate_fixture(), extract_markdown=quoted, checked_at="2026-08-09T02:03:04Z",
            receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW,
        )
        self.assertTrue(result["candidates"][0]["verification"]["opened_original"])
        for output in (
            json.dumps({"url": url, "title": "", "content": "Unable to extract content from the URL."}),
            json.dumps({"url": url, "title": "", "content": "  extract_failed: upstream timeout"}),
            "\n  EXTRACT_FAILED\nUnable to extract content from the URL.",
        ):
            with self.subTest(output=output):
                with self.assertRaises(ADAPTER.AdapterError) as raised:
                    ADAPTER.verification_envelope(
                        candidate_fixture(), extract_markdown=output, checked_at="2026-08-09T02:03:04Z",
                        receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW,
                    )
                self.assertEqual(raised.exception.category, "extract_failed")


class FailureCategoryTests(unittest.TestCase):
    def test_status_codes_are_parsed_before_wording(self):
        cases = (
            # Reviewer samples (2026-10-31): request IDs, per-minute quotas and 402.
            ('HTTP Error: 500 Server Error\nResponse: {"request_id": "c4291af0"}', "upstream_error"),
            ('HTTP Error: 429 Client Error: Too Many Requests\nResponse: {"error": "per-minute quota exceeded"}',
             "rate_limited"),
            ("API Error: monthly credit balance is too low", "quota_exhausted"),
            ("HTTP Error: 402 Client Error: Payment Required", "access_unavailable"),
            ('HTTP Error: 402 Client Error: Payment Required\nResponse: {"error": "Your credit balance is too low"}',
             "quota_exhausted"),
            ('HTTP Error: 429 Client Error: Too Many Requests\nResponse: {"error": "You\'ve hit your usage limit"}',
             "quota_exhausted"),
            ("HTTP Error: 401 Client Error: Unauthorized for url: https://api.anysearch.com/mcp",
             "authentication_error"),
            ('HTTP Error: 503 Server Error: Service Unavailable\nResponse: {"trace": "x-429-y", "note": "network"}',
             "upstream_error"),
            ("HTTP Error: 504 Server Error: Gateway Timeout", "timeout"),
            ('Response: {"request_id": "req-4291"}\nConnection Error: Unable to reach the API endpoint.',
             "network_error"),
            ("Timeout: The API request timed out.", "timeout"),
            ("API Error: quota exceeded", "quota_exhausted"),
            ("API Error: per-minute quota exceeded", "rate_limited"),
            ("API Error: rate limit exceeded", "rate_limited"),
            ("usage: anysearch get_sub_domains [-h] [--domain {general,legal}]\n"
             "anysearch get_sub_domains: error: argument --domain: invalid choice: 'nope'", "invalid_input"),
        )
        for stderr, category in cases:
            with self.subTest(stderr=stderr):
                self.assertEqual(ADAPTER._failure_category("", stderr)[0], category)

    def test_only_quota_exhaustion_message_is_fixed_and_redacted(self):
        category, message = ADAPTER._failure_category(
            "", "HTTP Error: 402 Client Error: Payment Required for url: https://api.anysearch.com/mcp?k=secret"
        )
        self.assertEqual(category, "access_unavailable")
        self.assertNotIn("secret", message)


class VisibleTextTests(unittest.TestCase):
    LINK_STUB = "\n".join(
        f"[{word}](https://example.com/section/{word.lower()}/index-page-with-long-slug)"
        for word in ("Home", "Docs", "Blog", "Login")
    )

    def test_link_targets_do_not_count_toward_the_length_gate(self):
        self.assertGreaterEqual(len("".join(self.LINK_STUB.split())), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS)
        self.assertEqual(ADAPTER.visible_char_count(self.LINK_STUB), len("HomeDocsBlogLogin"))
        markup = ('![logo](https://cdn.example.com/logo.png "Logo") [![badge](https://img.example.com/b.svg)]'
                  '(https://example.com/b)\n[ref]: https://example.com/reference "Reference"\n'
                  "[Docs](<https://example.com/a path>) [Wiki](https://en.wikipedia.org/wiki/Foo_(bar))")
        self.assertEqual(ADAPTER.visible_char_count(markup), len("logobadgeDocsWiki"))
        self.assertEqual(ADAPTER.visible_char_count("plain https://example.com/x text"),
                         len("plainhttps://example.com/xtext"))

    def test_link_only_navigation_stub_is_unread(self):
        stub = json.dumps({"url": candidate_fixture()["url"], "title": "", "content": self.LINK_STUB})
        with self.assertRaises(ADAPTER.AdapterError) as raised:
            ADAPTER.verification_envelope(
                candidate_fixture(), extract_markdown=stub, checked_at="2026-08-09T02:03:04Z",
                receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW,
            )
        self.assertEqual(raised.exception.category, "content_too_short")

    def test_linked_article_with_real_text_stays_readable(self):
        body = ("[Home](https://example.com/) [Docs](https://example.com/docs)\n\n# Release notes\n\n"
                + "This release adds [incremental sync](https://example.com/sync) and fixes the importer. " * 4)
        self.assertGreaterEqual(ADAPTER.visible_char_count(body), ADAPTER.MIN_ORIGINAL_CONTENT_CHARS)
        result = ADAPTER.verification_envelope(
            candidate_fixture(), extract_markdown=body, checked_at="2026-08-09T02:03:04Z",
            receipt_secret=TEST_RECEIPT_SECRET, now=TEST_NOW,
        )
        self.assertTrue(result["candidates"][0]["verification"]["opened_original"])


class ReceiptKeyCreationTests(unittest.TestCase):
    def _key_file(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return pathlib.Path(directory.name) / "secrets" / "receipt.key"

    def test_concurrent_first_runs_share_one_complete_key(self):
        key_file = self._key_file()
        barrier = threading.Barrier(12)
        keys, failures = [], []

        def first_run():
            barrier.wait()
            try:
                keys.append(ADAPTER.load_receipt_secret(key_file=key_file, environ={}))
            except ADAPTER.AdapterError as exc:
                failures.append(exc.category)

        threads = [threading.Thread(target=first_run) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(keys), 12)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(key_file.read_bytes(), keys[0])
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in key_file.parent.iterdir()], ["receipt.key"])

    def test_losing_creator_keeps_the_winners_key(self):
        key_file = self._key_file()
        winner = b"w" * ADAPTER.MIN_RECEIPT_KEY_BYTES
        real_link = os.link

        def another_process_wins(source, destination):
            ADAPTER._write_new_private_file(pathlib.Path(destination), winner)
            return real_link(source, destination)

        with mock.patch.object(ADAPTER.os, "link", side_effect=another_process_wins):
            self.assertEqual(ADAPTER.load_receipt_secret(key_file=key_file, environ={}), winner)
        self.assertEqual(key_file.read_bytes(), winner)
        self.assertEqual([path.name for path in key_file.parent.iterdir()], ["receipt.key"])

    def test_filesystems_without_hard_links_fall_back_to_exclusive_create(self):
        key_file = self._key_file()
        with mock.patch.object(ADAPTER.os, "link", side_effect=PermissionError("no hard links")):
            secret = ADAPTER.load_receipt_secret(key_file=key_file, environ={})
        self.assertEqual(key_file.read_bytes(), secret)
        self.assertEqual(len(secret), ADAPTER.MIN_RECEIPT_KEY_BYTES)
        self.assertEqual([path.name for path in key_file.parent.iterdir()], ["receipt.key"])


class PublicHostRuleTests(unittest.TestCase):
    def test_anysearch_applies_the_shared_public_host_rule(self):
        rejected = (
            "https://a.internal/x", "https://printer.lan/", "https://nas.home/", "https://wiki.intranet/",
            "https://box.local/", "https://dev.localhost/", "https://10.0.0.1/", "https://[::1]/",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(ADAPTER._PUBLIC_URLS.is_public_http_url(url))
                with self.assertRaises(ValueError):
                    ADAPTER.validate_public_http_url(url)
        for url in ("https://example.com/", "https://docs.example.org/a?b=1", "https://8.8.8.8/"):
            with self.subTest(url=url):
                self.assertTrue(ADAPTER._PUBLIC_URLS.is_public_http_url(url))
                self.assertEqual(ADAPTER.validate_public_http_url(url), url)


def upstream_search_parser():
    """The upstream AnySearch CLI's argparse shape for ``search`` (options + positional query)."""

    parser = argparse.ArgumentParser(prog="anysearch")
    parser.add_argument("--api_key", default="")
    commands = parser.add_subparsers(dest="command")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--domain", "-d", choices=["general", "finance", "code"])
    search.add_argument("--sub_domain", "-s")
    search.add_argument("--sub_domain_params")
    search.add_argument("--max_results", "-m", type=int)
    return parser


class SearchArgvTests(unittest.TestCase):
    def test_dash_leading_queries_reach_upstream_as_the_query(self):
        seen = []

        def runner(conf, argv, timeout):
            seen.append(argv)
            return "## Search Results (0 results)\n"

        for query in ("--legacy-peer-deps", "-Wall", "--domain=code", "-sfoo", "plain words"):
            with self.subTest(query=query):
                args = ADAPTER.parse_args([
                    "search", "--limit", "5", "--domain", "finance", "--sub-domain", "finance.us_stock",
                    "--sub-domain-params", '{"ticker":""}', "--", query,
                ])
                ADAPTER.execute(args, runner=runner, receipt_secret=TEST_RECEIPT_SECRET)
                argv = seen[-1]
                self.assertEqual(argv[-2:], ["--", query])
                parsed = upstream_search_parser().parse_args(argv)
                self.assertEqual(parsed.query, query)
                self.assertEqual(parsed.domain, "finance")
                self.assertEqual(parsed.sub_domain, "finance.us_stock")
                self.assertEqual(parsed.max_results, 5)


DIRECTORY_TABLE = """## finance

| domain | sub_domain | description | query_format | params_schema | zone |
|---|---|---|---|---|---|
| finance | `finance.us_stock` | US stock quotes and filings | Ticker, e.g. AAPL | `ticker` (required): stock ticker<br>`market` (optional): exchange | global |
| finance | finance.forex | Currency pairs | Pair, e.g. USD/CNY | - | global |
| finance | finance.fund | Mutual funds | Fund code | {"type":"object","properties":{"code":{},"region":{}},"required":["code"]} | cn |
"""


def sub_domain_args(domain="finance", runtime_conf="/nonexistent/runtime.conf"):
    return ADAPTER.parse_args(["sub-domains", f"--domain={domain}", "--runtime-conf", runtime_conf])


class SubDomainDiscoveryTests(unittest.TestCase):
    def test_documented_table_yields_sub_domains_and_required_params(self):
        seen = []

        def runner(conf, argv, timeout):
            seen.append(argv)
            return DIRECTORY_TABLE

        result = ADAPTER.execute(sub_domain_args(), runner=runner)
        self.assertEqual(seen, [["get_sub_domains", "--domain", "finance"]])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["routes"][0]["status"], "completed")
        self.assertEqual(result["routes"][0]["mode"], "sub-domains")
        self.assertEqual(result["errors"], [])
        entries = {entry["sub_domain"]: entry for entry in result["sub_domains"]}
        self.assertEqual(list(entries), ["finance.us_stock", "finance.forex", "finance.fund"])
        stock = entries["finance.us_stock"]
        self.assertEqual(stock["required_params"], ["ticker"])
        self.assertEqual(stock["optional_params"], ["market"])
        self.assertEqual(stock["sub_domain_params_template"], {"ticker": ""})
        self.assertEqual(stock["query_format"], "Ticker, e.g. AAPL")
        self.assertEqual(entries["finance.forex"]["required_params"], [])
        self.assertTrue(entries["finance.forex"]["params_known"])
        self.assertEqual(entries["finance.fund"]["required_params"], ["code"])
        self.assertEqual(entries["finance.fund"]["optional_params"], ["region"])
        self.assertEqual(result["directory_text"], DIRECTORY_TABLE.strip())
        self.assertEqual(result["candidates"], [])

    def test_json_directory_is_accepted(self):
        payload = json.dumps({"domains": [{"domain": "legal", "sub_domains": [
            {"sub_domain": "legal.cn_law", "description": "Chinese statutes",
             "params": [{"name": "jurisdiction", "required": True}, {"name": "year", "required": False}]},
        ]}]})
        result = ADAPTER.normalize_sub_domain_directory(payload, domain="legal")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sub_domains"][0]["required_params"], ["jurisdiction"])
        self.assertEqual(result["sub_domains"][0]["optional_params"], ["year"])

    def test_unmarked_parameters_make_the_directory_partial(self):
        directory = DIRECTORY_TABLE.replace(
            "`ticker` (required): stock ticker<br>`market` (optional): exchange", "ticker: stock ticker"
        )
        result = ADAPTER.normalize_sub_domain_directory(directory, domain="finance")
        self.assertEqual(result["status"], "partial")
        stock = result["sub_domains"][0]
        self.assertFalse(stock["params_known"])
        self.assertIsNone(stock["sub_domain_params_template"])
        self.assertEqual(stock["params_text"], "ticker: stock ticker")
        self.assertEqual(result["errors"][0]["category"], "parse_error")
        self.assertEqual(result["errors"][0]["sub_domains"], ["finance.us_stock"])

    # Abridged from the live get_sub_domains layout observed on 2026-10-31.
    SECTION_DIRECTORY = (
        "## finance Domain Capabilities (2 available)\n\n"
        "### finance.calendar\n"
        "Earnings dates and IPO schedules\n\n"
        "**Parameters:**\n"
        "- `type` (required): Calendar type. Values: `earnings`, `ipos`\n"
        "- `period`: Forward-looking time range. Default `7d`\n\n"
        "### finance.quote\n"
        "Quotes for stocks and crypto\n\n"
        "**Parameters:**\n"
        "- `type` (required): Asset type.\n"
        "- `symbol`: Symbol. type=stock: e.g., `AAPL`; type=crypto: e.g., `BTCUSD` (required)\n"
        "- `period`: Historical range\n"
    )

    def test_live_section_layout_reads_bullet_markers_only_after_the_name(self):
        result = ADAPTER.normalize_sub_domain_directory(self.SECTION_DIRECTORY, domain="finance")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["errors"], [])
        entries = {entry["sub_domain"]: entry for entry in result["sub_domains"]}
        self.assertEqual(list(entries), ["finance.calendar", "finance.quote"])
        calendar = entries["finance.calendar"]
        self.assertEqual(calendar["description"], "Earnings dates and IPO schedules")
        self.assertEqual(calendar["required_params"], ["type"])
        self.assertEqual(calendar["optional_params"], ["period"])
        self.assertEqual(calendar["sub_domain_params_template"], {"type": ""})
        quote = entries["finance.quote"]
        # `BTCUSD` (required) inside a description is a value rule, not a parameter.
        self.assertEqual(quote["required_params"], ["type"])
        self.assertEqual(quote["optional_params"], ["symbol", "period"])
        self.assertIn("`BTCUSD` (required)", quote["params_text"])

    def test_live_section_layout_without_markers_means_no_required_params(self):
        directory = (
            "## legal Domain Capabilities (1 available)\n\n"
            "### legal.legislation\nStatutes and regulations\n\n"
            "**Parameters:**\n- `jurisdiction`: Jurisdiction code such as CN.\n"
            "- `legal_level`: Legal level.\n"
        )
        result = ADAPTER.normalize_sub_domain_directory(directory, domain="legal")
        self.assertEqual(result["status"], "completed")
        entry = result["sub_domains"][0]
        self.assertTrue(entry["params_known"])
        self.assertEqual(entry["required_params"], [])
        self.assertEqual(entry["optional_params"], ["jurisdiction", "legal_level"])
        self.assertEqual(entry["sub_domain_params_template"], {})

    def test_live_section_layout_stays_partial_when_uncertain(self):
        unbackticked = self.SECTION_DIRECTORY.replace("- `period`: Historical range", "- period: Historical range")
        result = ADAPTER.normalize_sub_domain_directory(unbackticked, domain="finance")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][-1]["sub_domains"], ["finance.quote"])
        self.assertIsNone(
            {e["sub_domain"]: e for e in result["sub_domains"]}["finance.quote"]["sub_domain_params_template"]
        )
        miscounted = self.SECTION_DIRECTORY.replace("(2 available)", "(3 available)")
        result = ADAPTER.normalize_sub_domain_directory(miscounted, domain="finance")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][0]["announced_count"], 3)
        no_params = "### finance.calendar\nEarnings dates\n"
        result = ADAPTER.normalize_sub_domain_directory(no_params, domain="finance")
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["sub_domains"][0]["params_known"])

    def test_unparseable_or_empty_directory_fails_without_claiming_zero_sub_domains(self):
        for text in ("", "Directory service changed its layout.", "<html>bad gateway</html>"):
            with self.subTest(text=text):
                result = ADAPTER.normalize_sub_domain_directory(text, domain="finance")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["sub_domains"], [])
                self.assertEqual(result["errors"][-1]["category"], "parse_error")

    def test_short_quota_notice_is_reported_as_quota(self):
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            ADAPTER.normalize_sub_domain_directory("You've hit your usage limit", domain="finance")
        self.assertEqual(caught.exception.category, "quota_exhausted")

    def test_invalid_domain_is_rejected_before_any_runtime_call(self):
        runner = mock.Mock()
        for domain in ("Legal", "-x", "legal\n", "1legal", "legal.cn", "a" * 42, ""):
            with self.subTest(domain=domain):
                with self.assertRaises(ADAPTER.AdapterError) as caught:
                    ADAPTER.execute(sub_domain_args(domain), runner=runner)
                self.assertEqual(caught.exception.category, "invalid_input")
        runner.assert_not_called()
        self.assertEqual(ADAPTER.validate_sub_domain_name("a" * 41), "a" * 41)

    def test_upstream_invalid_choice_becomes_invalid_input_envelope(self):
        failed = SimpleNamespace(returncode=2, stdout="", stderr=(
            "usage: anysearch get_sub_domains [-h] [--domain {general,legal}]\n"
            "anysearch get_sub_domains: error: argument --domain: invalid choice: 'gardening'\n"))
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "runtime.conf"
            conf.write_text("Command: python3 /tmp/cli.py\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(ADAPTER.subprocess, "run", return_value=failed), \
                    contextlib.redirect_stdout(stdout):
                code = ADAPTER.main(["sub-domains", "--domain", "gardening", "--runtime-conf", str(conf)])
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(envelope["status"], "failed")
        self.assertEqual(envelope["domain"], "gardening")
        self.assertEqual(envelope["sub_domains"], [])
        self.assertEqual(envelope["errors"][0]["category"], "invalid_input")

    def test_router_argv_runs_end_to_end_without_leaking_the_runtime_key(self):
        secret = "unit-sub-domain-secret"  # pragma: allowlist secret
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake_cli = root / "fake_anysearch.py"
            fake_cli.write_text(
                "import os, sys\n"
                f"if sys.argv[1:] == ['get_sub_domains', '--domain', 'finance'] and "
                f"os.environ.get('ANYSEARCH_API_KEY') == {secret!r}:\n"
                f"    print({DIRECTORY_TABLE!r})\n"
                "else:\n"
                "    raise SystemExit(3)\n",
                encoding="utf-8",
            )
            conf = root / "runtime.conf"
            conf.write_text(
                f'Command: env ANYSEARCH_API_KEY={secret} {sys.executable} "{fake_cli}"\n', encoding="utf-8"
            )
            environment = dict(os.environ)
            environment["YICHEN_ANYSEARCH_RUNTIME_CONF"] = str(conf)
            # The exact argv the router emits: [python3, <SKILL_DIR>/scripts/anysearch_adapter.py,
            # "sub-domains", "--domain", <domain>].
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "anysearch_adapter.py"), "sub-domains", "--domain", "finance"],
                check=False, capture_output=True, text=True, env=environment,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        envelope = json.loads(completed.stdout)
        self.assertEqual(envelope["status"], "completed")
        self.assertEqual(envelope["sub_domains"][0]["sub_domain_params_template"], {"ticker": ""})
        self.assertNotIn(secret, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
