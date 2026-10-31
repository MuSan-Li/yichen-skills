#!/usr/bin/env python3
"""Xiaohongshu / Douyin executors: main flows with mocked network and Playwright."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xhs = load_module("content_archive_xhs_flow", "xiaohongshu_fetch.py")
douyin = load_module("content_archive_douyin_flow", "douyin_download.py")
requests = xhs.requests

NOTE_ID = "n1abc"
PAGE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=SECRET_TOKEN&xsec_source=pc_share"
VIDEO_MASTER = "http://sns-video-bd.xhscdn.com/stream/v.mp4?sign=SIGNED_MASTER&t=1"
VIDEO_BACKUP = "https://sns-video-hw.xhscdn.com/stream/v.mp4?sign=SIGNED_BACKUP"
SUBTITLE_URL = "https://sns-video-bd.xhscdn.com/sub.srt?sign=SIGNED_SUB"
SRT = "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict | None = None,
        text: str = "",
        chunks: tuple = (),
        error_after: Exception | None = None,
        url: str = "",
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.chunks = chunks
        self.error_after = error_after
        self.url = url
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: {self.url}")

    def iter_content(self, chunk_size: int = 1):
        for chunk in self.chunks:
            yield chunk
        if self.error_after is not None:
            raise self.error_after

    def close(self) -> None:
        self.closed = True


def body(data: bytes, *, declared: int | None = None, error: Exception | None = None) -> FakeResponse:
    size = len(data) if declared is None else declared
    return FakeResponse(headers={"Content-Length": str(size)}, chunks=(data,), error_after=error)


class FakeWeb:
    """Route requests.get by URL without query; record (url, cookie_sent)."""

    def __init__(self, routes: dict) -> None:
        self.routes = {key: list(value) if isinstance(value, list) else [value] for key, value in routes.items()}
        self.calls: list[tuple[str, bool]] = []

    def get(self, url, headers=None, **_kwargs):
        self.calls.append((url, "Cookie" in (headers or {})))
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f"unexpected request: {key}")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        response.url = url
        return response

    def calls_to(self, host: str) -> list[tuple[str, bool]]:
        return [call for call in self.calls if urlparse(call[0]).hostname == host]


def note_page(note: dict) -> str:
    state = {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}
    return f"<html><script>window.__INITIAL_STATE__={json.dumps(state)}</script></html>"


def video_note(*, with_subtitle: bool = False) -> dict:
    note = {
        "noteId": NOTE_ID,
        "type": "video",
        "title": "视频笔记",
        "user": {"nickname": "作者"},
        "video": {"media": {"stream": {"h264": [{"masterUrl": VIDEO_MASTER, "backupUrls": [VIDEO_BACKUP]}]}}},
    }
    if with_subtitle:
        note["video"]["mediaV2"] = json.dumps(
            {"video": {"subtitles": {"source": [{"url": SUBTITLE_URL, "language": "zh-CN"}]}}}
        )
    return note


def run_xhs(argv, web: FakeWeb, *, cookie: str | None = None):
    """Return (exit code, stdout JSON, stdout + stderr text); stdout must be one JSON document."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = 0
    env = {} if cookie is None else {"XHS_COOKIE": cookie}
    with mock.patch.object(requests, "get", side_effect=web.get), mock.patch.dict(
        os.environ, env, clear=False
    ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        if cookie is None:
            os.environ.pop("XHS_COOKIE", None)
        try:
            xhs.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    payload = json.loads(stdout.getvalue())
    return code, payload, stderr.getvalue() + stdout.getvalue()


class XiaohongshuUrlScopeTests(unittest.TestCase):
    def test_only_note_detail_paths_are_accepted(self) -> None:
        accepted = {
            "https://www.xiaohongshu.com/explore/abc123": "abc123",
            "https://www.xiaohongshu.com/explore/abc123?xsec_token=t": "abc123",
            "https://www.xiaohongshu.com/discovery/item/abc123/": "abc123",
            "https://www.xiaohongshu.com/user/profile/5f00aa/abc123": "abc123",
            "https://www.xiaohongshu.com/explore?note_id=abc123": "abc123",
            # Live 2026-10-31: unified-search hands off web search-result note links.
            "https://www.xiaohongshu.com/search_result/6ae2b9d10000000000000001?xsec_token=t&xsec_source=": (
                "6ae2b9d10000000000000001"
            ),
        }
        for url, note_id in accepted.items():
            with self.subTest(url=url):
                self.assertEqual(xhs.extract_note_id(url), note_id)
        rejected = (
            "https://www.xiaohongshu.com/search_result?keyword=foo",
            "https://www.xiaohongshu.com/search_result/abc123",
            "https://www.xiaohongshu.com/search_result/",
            "https://www.xiaohongshu.com/search_result/6ae2b9d10000000000000001/comments",
            "https://www.xiaohongshu.com/user/profile/5f0000000000000000000001",
            "https://www.xiaohongshu.com/explore",
            "https://www.xiaohongshu.com/",
            "http://www.xiaohongshu.com/explore/abc123",
            "https://www.xiaohongshu.com:8443/explore/abc123",
            "https://user@www.xiaohongshu.com/explore/abc123",
            "https://xiaohongshu.com.evil.example/explore/abc123",
            "https://xhslink.com/a/AbCd123",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    xhs.extract_note_id(url)

    def test_short_link_follows_redirect_then_extracts_note_id(self) -> None:
        web = FakeWeb(
            {
                "https://xhslink.com/a/AbCd123": FakeResponse(
                    status=302,
                    headers={"Location": f"http://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=TOK"},
                )
            }
        )
        with mock.patch.object(requests, "get", side_effect=web.get):
            target, note_id = xhs.resolve_note_target("https://xhslink.com/a/AbCd123")
        self.assertEqual(note_id, NOTE_ID)
        self.assertTrue(target.startswith("https://www.xiaohongshu.com/discovery/item/"))
        self.assertEqual(web.calls, [("https://xhslink.com/a/AbCd123", False)])

    def test_short_link_to_profile_external_or_without_redirect_is_rejected(self) -> None:
        cases = {
            "profile": FakeResponse(status=302, headers={"Location": "https://www.xiaohongshu.com/user/profile/5f00"}),
            "external": FakeResponse(status=302, headers={"Location": "https://evil.example/explore/abc"}),
            "no-redirect": FakeResponse(status=200, text="<html/>"),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                web = FakeWeb({"https://xhslink.com/a/AbCd123": response})
                with mock.patch.object(requests, "get", side_effect=web.get):
                    with self.assertRaises(ValueError):
                        xhs.resolve_note_target("https://xhslink.com/a/AbCd123")

    def test_page_redirect_to_captcha_or_other_note_is_rejected(self) -> None:
        for location in (
            "/website-login/captcha?redirectPath=x",
            "https://www.xiaohongshu.com/explore/othernote",
            "https://evil.example/explore/n1abc",
        ):
            with self.subTest(location=location):
                web = FakeWeb(
                    {f"https://www.xiaohongshu.com/explore/{NOTE_ID}": FakeResponse(status=302, headers={"Location": location})}
                )
                with mock.patch.object(requests, "get", side_effect=web.get):
                    with self.assertRaises(ValueError):
                        xhs.fetch_html(PAGE_URL)
        web = FakeWeb(
            {
                f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}": FakeResponse(
                    status=301, headers={"Location": f"/explore/{NOTE_ID}?xsec_token=T"}
                ),
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}": FakeResponse(text="ok"),
            }
        )
        with mock.patch.object(requests, "get", side_effect=web.get):
            self.assertEqual(xhs.fetch_html(f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}"), "ok")

    def test_cookie_header_only_for_https_xiaohongshu_hosts(self) -> None:
        with mock.patch.dict(os.environ, {"XHS_COOKIE": "private-cookie"}):
            self.assertEqual(
                xhs.request_headers(include_cookie=True, target_url=PAGE_URL)["Cookie"], "private-cookie"
            )
            for target in (
                None,
                "https://sns-video-bd.xhscdn.com/v.mp4",
                "http://www.xiaohongshu.com/explore/abc",
                "https://xiaohongshu.com.evil.example/x",
                "https://xhslink.com/a/x",
            ):
                with self.subTest(target=target):
                    self.assertNotIn("Cookie", xhs.request_headers(include_cookie=True, target_url=target))
            self.assertNotIn("Cookie", xhs.request_headers(include_cookie=False, target_url=PAGE_URL))

    def test_error_text_and_stored_urls_drop_signatures(self) -> None:
        message = "403 Client Error: Forbidden for url: https://sns-video-bd.xhscdn.com/v.mp4?sign=SIGNED"
        sanitized = xhs.sanitize_error_text(message)
        self.assertNotIn("SIGNED", sanitized)
        self.assertIn("sns-video-bd.xhscdn.com", sanitized)
        self.assertEqual(
            xhs.strip_url_query(PAGE_URL), f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        )
        self.assertEqual(xhs.normalize_media_url(VIDEO_MASTER)[:8], "https://")
        self.assertIsNone(xhs.normalize_media_url("file:///etc/hosts"))


class XiaohongshuMainFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name) / "xhs-out"

    def test_truncated_video_is_reported_and_never_named_as_complete(self) -> None:
        reset = requests.exceptions.ChunkedEncodingError("connection reset")
        web = FakeWeb(
            {
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}": FakeResponse(text=note_page(video_note())),
                "https://sns-video-bd.xhscdn.com/stream/v.mp4": body(b"PARTIAL", declared=100, error=reset),
                "https://sns-video-hw.xhscdn.com/stream/v.mp4": FakeResponse(status=403),
            }
        )
        code, payload, output = run_xhs([PAGE_URL, str(self.out)], web)
        self.assertEqual(code, 2)
        self.assertEqual((payload["status"], payload["reason"]), ("partial", "media_download_failed"))
        self.assertEqual([item["item"] for item in payload["media_failures"]], ["视频"])
        self.assertNotIn("抓取完成", output)
        self.assertIn("抓取未完成：1 项媒体失败", output)
        self.assertIn("- 视频:", output)
        for secret in ("SIGNED_MASTER", "SIGNED_BACKUP", "SECRET_TOKEN"):
            self.assertNotIn(secret, output)
        self.assertFalse((self.out / f"xhs_{NOTE_ID}.mp4").exists())
        self.assertTrue((self.out / f"xhs_{NOTE_ID}.mp4.part").exists())
        metadata_text = (self.out / f"xhs_{NOTE_ID}.metadata.json").read_text(encoding="utf-8")
        for secret in ("sign=", "SIGNED", "xsec_token", "SECRET_TOKEN"):
            self.assertNotIn(secret, metadata_text)
        metadata = json.loads(metadata_text)
        self.assertEqual(metadata["source_url"], f"https://www.xiaohongshu.com/explore/{NOTE_ID}")
        self.assertEqual(metadata["video_url"], "http://sns-video-bd.xhscdn.com/stream/v.mp4")
        # The http master URL was upgraded before any request was made.
        self.assertNotIn("http://sns-video-bd.xhscdn.com/stream/v.mp4?sign=SIGNED_MASTER&t=1", [c[0] for c in web.calls])

    def test_cookie_goes_only_to_xiaohongshu_page_never_to_cdn(self) -> None:
        page = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        web = FakeWeb(
            {
                page: [
                    FakeResponse(text="<html><meta name='description' content='x'></html>"),
                    FakeResponse(text=note_page(video_note(with_subtitle=True))),
                ],
                "https://sns-video-bd.xhscdn.com/stream/v.mp4": body(b"VIDEO-BYTES"),
                "https://sns-video-bd.xhscdn.com/sub.srt": body(SRT.encode("utf-8")),
            }
        )
        code, payload, output = run_xhs([PAGE_URL, str(self.out), "--use-cookie"], web, cookie="dummy-cookie")
        self.assertEqual(code, 0, output)
        self.assertIn("抓取完成", output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["authentication"], "cookie_authorized_this_turn")
        self.assertEqual(web.calls_to("www.xiaohongshu.com"), [(PAGE_URL, False), (PAGE_URL, True)])
        cdn_calls = web.calls_to("sns-video-bd.xhscdn.com")
        self.assertEqual(len(cdn_calls), 2)
        self.assertTrue(all(not cookie_sent for _, cookie_sent in cdn_calls))
        self.assertTrue(all(url.startswith("https://") for url, _ in cdn_calls))
        self.assertEqual((self.out / f"xhs_{NOTE_ID}.mp4").read_bytes(), b"VIDEO-BYTES")
        self.assertIn("你好", (self.out / f"xhs_{NOTE_ID}.transcript.txt").read_text(encoding="utf-8"))
        metadata = json.loads((self.out / f"xhs_{NOTE_ID}.metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["subtitle_urls"][0]["url"], "https://sns-video-bd.xhscdn.com/sub.srt")
        self.assertNotIn("dummy-cookie", output)

    def test_image_failures_listed_and_cookie_retry_limited_to_xiaohongshu_media(self) -> None:
        note = {
            "noteId": NOTE_ID,
            "type": "normal",
            "title": "图文",
            "imageList": [
                {"urlDefault": "https://sns-webpic-qc.xhscdn.com/a/1040g.jpg?sign=IMG1"},
                {"urlDefault": "https://ci.xiaohongshu.com/b/1040h.jpg?sign=IMG2"},
            ],
        }
        web = FakeWeb(
            {
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}": FakeResponse(text=note_page(note)),
                "https://sns-webpic-qc.xhscdn.com/a/1040g.jpg": FakeResponse(status=403),
                "https://ci.xiaohongshu.com/b/1040h.jpg": [FakeResponse(status=403), body(b"IMG")],
            }
        )
        code, _payload, output = run_xhs([PAGE_URL, str(self.out), "--use-cookie"], web, cookie="dummy-cookie")
        self.assertEqual(code, 2)
        self.assertIn("- 图片 1:", output)
        self.assertNotIn("IMG1", output)
        self.assertEqual(web.calls_to("sns-webpic-qc.xhscdn.com")[0][1], False)
        self.assertEqual(len(web.calls_to("sns-webpic-qc.xhscdn.com")), 1)
        self.assertEqual([sent for _, sent in web.calls_to("ci.xiaohongshu.com")], [False, True])
        self.assertTrue((self.out / "images" / f"xhs_{NOTE_ID}_02.jpg").exists())
        self.assertFalse((self.out / "images" / f"xhs_{NOTE_ID}_01.jpg").exists())

    def test_short_link_skip_media_flow(self) -> None:
        note = {"noteId": NOTE_ID, "type": "normal", "title": "短链笔记", "imageList": []}
        web = FakeWeb(
            {
                "https://xhslink.com/a/AbCd123": FakeResponse(
                    status=302,
                    headers={"Location": f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=TOK"},
                ),
                f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}": FakeResponse(text=note_page(note)),
            }
        )
        code, payload, output = run_xhs(["https://xhslink.com/a/AbCd123", str(self.out), "--skip-media"], web)
        self.assertEqual(code, 0, output)
        self.assertIn(f"笔记ID: {NOTE_ID}", output)
        self.assertEqual((payload["status"], payload["operation"]), ("success", "read"))
        metadata = json.loads((self.out / f"xhs_{NOTE_ID}.metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["note_id"], NOTE_ID)
        self.assertNotIn("TOK", json.dumps(metadata))

    def test_search_result_note_link_is_fetched_with_token_and_saved_canonically(self) -> None:
        hex_id = "6ae2b9d10000000000000001"
        note = {"noteId": hex_id, "type": "normal", "title": "搜索结果笔记", "user": {"nickname": "作者"}}
        state = {"note": {"noteDetailMap": {hex_id: {"note": note}}}}
        page = f"<html><script>window.__INITIAL_STATE__={json.dumps(state)}</script></html>"
        url = f"https://www.xiaohongshu.com/search_result/{hex_id}?xsec_token=SEARCH_TOKEN&xsec_source="
        web = FakeWeb({f"https://www.xiaohongshu.com/search_result/{hex_id}": FakeResponse(text=page)})
        code, _payload, output = run_xhs([url, str(self.out), "--skip-media"], web)
        self.assertEqual(code, 0, output)
        # The page is requested exactly as given: the search token is what lets it load.
        self.assertEqual(web.calls, [(url, False)])
        metadata = json.loads((self.out / f"xhs_{hex_id}.metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["note_id"], hex_id)
        self.assertEqual(metadata["source_url"], f"https://www.xiaohongshu.com/explore/{hex_id}")
        self.assertNotIn("SEARCH_TOKEN", json.dumps(metadata))
        self.assertNotIn("SEARCH_TOKEN", output)

    def test_search_page_is_rejected_before_any_request_or_directory(self) -> None:
        web = FakeWeb({})
        code, payload, output = run_xhs(
            ["https://www.xiaohongshu.com/search_result?keyword=foo", str(self.out)], web
        )
        self.assertEqual(code, 1)
        self.assertIn("抓取失败", output)
        self.assertEqual((payload["status"], payload["reason"]), ("rejected", "invalid_url"))
        self.assertFalse(payload["side_effects_performed"])
        self.assertEqual(web.calls, [])
        self.assertFalse(self.out.exists())

    def test_blocked_expired_or_empty_pages_fail_as_json_without_any_directory(self) -> None:
        # Live 2026-10-31: an expired search token redirected to /404/sec_<id> and the
        # old executor left an empty output directory behind.
        page = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        cases = {
            "blocked_or_token_expired": FakeResponse(
                status=302, headers={"Location": "/404/sec_efunbjEQ?redirectPath=x&xsec_token=TOK"}
            ),
            "note_not_found": FakeResponse(status=302, headers={"Location": "/404?source=note"}),
            "login_or_captcha_required": FakeResponse(
                status=302, headers={"Location": "/website-login/captcha?redirectPath=x"}
            ),
            "redirected_to_other_note": FakeResponse(
                status=302, headers={"Location": "/explore/othernote"}
            ),
            "http_error": FakeResponse(status=461),
            "no_note_data": FakeResponse(text="<html><title>小红书</title></html>"),
        }
        for reason, response in cases.items():
            with self.subTest(reason=reason):
                code, payload, output = run_xhs([PAGE_URL, str(self.out)], FakeWeb({page: response}))
                self.assertEqual(code, 1)
                self.assertEqual((payload["status"], payload["reason"]), ("failed", reason))
                self.assertFalse(payload["ok"])
                self.assertIsNone(payload["output_dir"])
                self.assertFalse(payload["side_effects_performed"])
                self.assertEqual(payload["note_id"], NOTE_ID)
                self.assertFalse(self.out.exists())
                self.assertNotIn("TOK", output)
                self.assertNotIn("SECRET_TOKEN", output)
        _code, payload, _ = run_xhs(
            [PAGE_URL, str(self.out)], FakeWeb({page: cases["blocked_or_token_expired"]})
        )
        self.assertIn("xsec_token", payload["hint"])

    def test_network_errors_are_classified(self) -> None:
        page = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        for error, reason in (
            (requests.exceptions.ConnectTimeout("timed out"), "timeout"),
            (requests.exceptions.ConnectionError("reset"), "network_error"),
        ):
            with self.subTest(reason=reason):
                web = FakeWeb({})
                web.get = mock.Mock(side_effect=error)
                code, payload, _ = run_xhs([PAGE_URL, str(self.out)], web)
                self.assertEqual((code, payload["reason"]), (1, reason))
                self.assertFalse(self.out.exists())

    def test_html_snapshot_redacts_token_values(self) -> None:
        note = {"noteId": NOTE_ID, "type": "normal", "title": "快照", "xsecToken": "JSONTOKEN1", "imageList": []}
        state = {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}, "user": {"webId": ""}}
        page_html = (
            "<html><head><style>.c{box-shadow:var(--x-s)}</style></head><body>"
            f'<a href="/explore/{NOTE_ID}?xsec_token=QUERYTOKEN2&amp;xsec_source=pc_search">x</a>'
            f'<div data-v="{{&quot;xsecToken&quot;:&quot;QUOTTOKEN3&quot;}}"></div>'
            f'<script>var u = "https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=ESC4\\u0026xsec_source=pc";'
            'var v = "https://sns-video-bd.xhscdn.com/v.mp4?sign=SIGN5&t=1";'
            'var w = "{\\"access_token\\":\\"NESTED6\\"}";</script>'
            f"<script>window.__INITIAL_STATE__={json.dumps(state)}</script></body></html>"
        )
        web = FakeWeb({f"https://www.xiaohongshu.com/explore/{NOTE_ID}": FakeResponse(text=page_html)})
        code, payload, output = run_xhs([PAGE_URL, str(self.out), "--skip-media"], web)
        self.assertEqual(code, 0, output)
        snapshot = Path(payload["html_path"]).read_text(encoding="utf-8")
        for secret in ("JSONTOKEN1", "QUERYTOKEN2", "QUOTTOKEN3", "ESC4", "SIGN5", "NESTED6", "SECRET_TOKEN"):
            self.assertNotIn(secret, snapshot)
        self.assertIn("xsec_token=REDACTED&amp;xsec_source=pc_search", snapshot)
        self.assertIn('"xsecToken": "REDACTED"', snapshot)
        self.assertIn("&quot;xsecToken&quot;:&quot;REDACTED&quot;", snapshot)
        self.assertIn("sign=REDACTED&t=1", snapshot)
        self.assertIn('"webId": ""', snapshot)
        self.assertIn("box-shadow:var(--x-s)", snapshot)
        # The redacted snapshot still parses as the same note.
        self.assertEqual(xhs.extract_note_from_html(snapshot, NOTE_ID)["title"], "快照")

    def test_path_signed_cdn_urls_are_marked_as_expiring(self) -> None:
        signed = (
            # Shape of the live CDN links (timestamp/32-hex signature/path); values synthetic.
            "http://sns-webpic-qc.xhscdn.com/202601010000/0123456789abcdef0123456789abcdef/"
            "notes_pre_post/1040gexampleimage0000!nd_dft_wgth_jpg_3"
        )
        note = {"noteId": NOTE_ID, "type": "normal", "imageList": [{"urlDefault": signed}]}
        metadata = xhs.build_metadata(note, PAGE_URL, NOTE_ID)
        self.assertTrue(metadata["media_urls_expire"])
        self.assertIn("过期", metadata["media_urls_note"])
        self.assertEqual(metadata["image_urls"], [signed])
        plain = xhs.build_metadata(
            {"noteId": NOTE_ID, "type": "normal", "imageList": [{"urlDefault": "https://ci.xiaohongshu.com/a.jpg"}]},
            PAGE_URL,
            NOTE_ID,
        )
        self.assertFalse(plain["media_urls_expire"])
        self.assertNotIn("media_urls_note", plain)
        meta = xhs.build_metadata_from_meta({"og:title": "t", "og:image": signed}, PAGE_URL, NOTE_ID)
        self.assertTrue(meta["media_urls_expire"])


# --- Douyin -----------------------------------------------------------------

VIDEO_ID = "7611845735025364265"
SIGNED_PLAY = "https://v26-web.douyinvod.com/abc/video.mp4?sign=DYSIGNED"


class FakePlaywrightResponse:
    def __init__(self, url: str, payload: dict) -> None:
        self.url = url
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakePage:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.handlers = []
        self.visited: list[str] = []

    def on(self, _event, handler) -> None:
        self.handlers.append(handler)

    def goto(self, url, **_kwargs) -> None:
        self.visited.append(url)
        for response in self.responses:
            for handler in self.handlers:
                handler(response)

    def wait_for_timeout(self, _ms) -> None:
        return None


class FakePlaywright:
    def __init__(self, responses, *, missing_browser_times: int = 0) -> None:
        self.page = FakePage(responses)
        self.missing_browser_times = missing_browser_times
        self.launches = 0
        outer = self

        class Chromium:
            def launch(self, **_kwargs):
                outer.launches += 1
                if outer.launches <= outer.missing_browser_times:
                    raise douyin.PlaywrightError("Executable doesn't exist at /fake/chromium")
                return Browser()

        class Browser:
            def new_context(self, **_kwargs):
                return Context()

            def close(self) -> None:
                return None

        class Context:
            def new_page(self):
                return outer.page

        self.chromium = Chromium()

    def factory(self):
        @contextlib.contextmanager
        def manager():
            yield self

        return manager


def detail(aweme_id: str, play: str = SIGNED_PLAY) -> dict:
    return {
        "aweme_detail": {
            "aweme_id": aweme_id,
            "desc": f"video {aweme_id}",
            "author": {"nickname": "作者", "uid": "1"},
            "video": {"play_addr": {"url_list": [play]}, "duration": 1000},
        }
    }


DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id="


class DouyinUrlScopeTests(unittest.TestCase):
    def test_only_video_detail_forms_are_accepted(self) -> None:
        for url in (
            f"https://www.douyin.com/video/{VIDEO_ID}",
            f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}",
            f"https://www.douyin.com/user/MS4wLjAB?modal_id={VIDEO_ID}",
        ):
            with self.subTest(url=url):
                self.assertEqual(douyin.normalize_url(url), f"https://www.douyin.com/video/{VIDEO_ID}")
        for url in (
            "https://www.douyin.com/search/AI",
            "https://www.douyin.com/user/MS4wLjAB",
            "https://www.douyin.com/jingxuan",
            "https://www.douyin.com/",
            f"https://www.douyin.com/note/{VIDEO_ID}",
            f"https://www.douyin.com/search/x?q={VIDEO_ID}/video/{VIDEO_ID}",
            f"http://www.douyin.com/video/{VIDEO_ID}",
            f"https://douyin.com.evil.example/video/{VIDEO_ID}",
            "https://v.douyin.com/AbCdEf/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    douyin.normalize_url(url)

    def test_short_link_is_resolved_then_validated(self) -> None:
        cases = {
            f"https://www.iesdouyin.com/share/video/{VIDEO_ID}/?region=CN": VIDEO_ID,
            "https://www.iesdouyin.com/share/user/MS4wLjAB": None,
            f"https://evil.example/video/{VIDEO_ID}": None,
        }
        for location, expected in cases.items():
            with self.subTest(location=location):
                web = FakeWeb({"https://v.douyin.com/AbCdEf/": FakeResponse(status=302, headers={"Location": location})})
                with mock.patch.object(douyin.requests, "get", side_effect=web.get):
                    if expected is None:
                        with self.assertRaises(ValueError):
                            douyin.resolve_video_target("https://v.douyin.com/AbCdEf/")
                    else:
                        self.assertEqual(
                            douyin.resolve_video_target("https://v.douyin.com/AbCdEf/"),
                            (f"https://www.douyin.com/video/{VIDEO_ID}", VIDEO_ID),
                        )

    def test_detail_api_source_is_judged_by_hostname(self) -> None:
        self.assertTrue(douyin.is_detail_api_response(DETAIL_API + VIDEO_ID))
        for url in (
            "https://evil.example/aweme/v1/web/aweme/detail/?from=douyin.com",
            "https://douyin.com.evil.example/aweme/v1/web/aweme/detail/",
            "https://www.douyin.com/aweme/v1/web/aweme/related/?x=aweme/detail",
        ):
            with self.subTest(url=url):
                self.assertFalse(douyin.is_detail_api_response(url))


class DouyinPlaywrightFlowTests(unittest.TestCase):
    def test_only_matching_aweme_from_douyin_host_is_accepted(self) -> None:
        fake = FakePlaywright(
            [
                FakePlaywrightResponse("https://evil.example/aweme/v1/web/aweme/detail/?douyin.com", detail(VIDEO_ID, "https://evil/x.mp4")),
                FakePlaywrightResponse(DETAIL_API + "1111111111", detail("1111111111", "https://other/x.mp4")),
                FakePlaywrightResponse(DETAIL_API + VIDEO_ID, detail(VIDEO_ID)),
            ]
        )
        with mock.patch.object(douyin, "sync_playwright", fake.factory()), contextlib.redirect_stdout(io.StringIO()):
            info = douyin.fetch_video_info(f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}", timeout=0)
        self.assertEqual(info["aweme_id"], VIDEO_ID)
        self.assertEqual(info["video_url"], SIGNED_PLAY)
        self.assertEqual(fake.page.visited, [f"https://www.douyin.com/video/{VIDEO_ID}"])

    def test_mismatched_aweme_only_is_a_failure(self) -> None:
        fake = FakePlaywright([FakePlaywrightResponse(DETAIL_API + "1111111111", detail("1111111111"))])
        with mock.patch.object(douyin, "sync_playwright", fake.factory()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "aweme_id 与链接一致"):
                douyin.fetch_video_info(f"https://www.douyin.com/video/{VIDEO_ID}", timeout=0)

    def test_missing_chromium_is_never_installed_without_explicit_flag(self) -> None:
        fake = FakePlaywright([FakePlaywrightResponse(DETAIL_API + VIDEO_ID, detail(VIDEO_ID))], missing_browser_times=1)
        with mock.patch.object(douyin, "sync_playwright", fake.factory()), mock.patch.object(
            douyin.subprocess, "run"
        ) as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(douyin.ChromiumMissingError, "python3 -m playwright install chromium"):
                douyin.fetch_video_info(f"https://www.douyin.com/video/{VIDEO_ID}", timeout=0)
            run.assert_not_called()

            fake.launches = 0
            info = douyin.fetch_video_info(
                f"https://www.douyin.com/video/{VIDEO_ID}",
                timeout=0,
                install_chromium=True,
                install_timeout=7,
            )
        self.assertEqual(info["aweme_id"], VIDEO_ID)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-3:], ["playwright", "install", "chromium"])
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_install_timeout_is_reported(self) -> None:
        with mock.patch.object(
            douyin.subprocess, "run", side_effect=subprocess.TimeoutExpired(["playwright"], 7)
        ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                douyin.install_playwright_chromium(7)


class DouyinMainFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name) / "clip.mp4"
        self.info = {
            "video_url": SIGNED_PLAY,
            "title": "标题",
            "author": "作者",
            "aweme_id": VIDEO_ID,
            "aweme_data": detail(VIDEO_ID)["aweme_detail"],
            "canonical_url": f"https://www.douyin.com/video/{VIDEO_ID}",
        }

    def run_main(self, argv, response=None):
        stdout = io.StringIO()
        code = 0
        info_patch = mock.patch.object(douyin, "fetch_video_info", return_value=self.info)
        with info_patch, mock.patch.object(douyin.requests, "get", return_value=response), contextlib.redirect_stdout(stdout):
            try:
                douyin.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue()

    def test_truncated_download_fails_and_keeps_only_part_file(self) -> None:
        url = f"https://www.douyin.com/video/{VIDEO_ID}?previous_page=app_code_link"
        code, output = self.run_main([url, str(self.output)], body(b"x" * 10, declared=100))
        self.assertEqual(code, 1)
        self.assertIn("下载不完整", output)
        self.assertIn("失败项", output)
        self.assertNotIn("下载完成", output)
        self.assertNotIn("DYSIGNED", output)
        self.assertFalse(self.output.exists())
        self.assertTrue(Path(f"{self.output}.part").exists())
        metadata_text = Path(f"{self.output}.metadata.json").read_text(encoding="utf-8")
        self.assertNotIn("DYSIGNED", metadata_text)
        metadata = json.loads(metadata_text)
        self.assertEqual(metadata["source_url"], f"https://www.douyin.com/video/{VIDEO_ID}")
        self.assertEqual(metadata["download_status"], "failed")
        self.assertIsNone(metadata["sha256"])

    def test_http_error_output_hides_signed_url(self) -> None:
        code, output = self.run_main([f"https://www.douyin.com/video/{VIDEO_ID}", str(self.output)], FakeResponse(status=403, url=SIGNED_PLAY))
        self.assertEqual(code, 1)
        self.assertIn("v26-web.douyinvod.com", output)
        self.assertNotIn("DYSIGNED", output)

    def test_complete_download_is_renamed(self) -> None:
        code, output = self.run_main([f"https://www.douyin.com/video/{VIDEO_ID}", str(self.output)], body(b"VIDEO"))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.output.read_bytes(), b"VIDEO")
        self.assertFalse(Path(f"{self.output}.part").exists())
        metadata = json.loads(Path(f"{self.output}.metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["download_status"], "success")
        self.assertEqual(metadata["video_path"], str(self.output))
        self.assertEqual(metadata["size_bytes"], 5)
        self.assertEqual(metadata["sha256"], hashlib.sha256(b"VIDEO").hexdigest())
        self.assertFalse(Path(f"{self.output}.metadata.json.tmp").exists())

    def test_rerun_numbering_starts_at_run_2_like_other_executors(self) -> None:
        self.output.write_bytes(b"old")
        code, output = self.run_main([f"https://www.douyin.com/video/{VIDEO_ID}", str(self.output)], body(b"NEW"))
        self.assertEqual(code, 0, output)
        rerun = self.output.with_name("clip-run-2.mp4")
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertEqual(rerun.read_bytes(), b"NEW")
        metadata = json.loads(Path(f"{rerun}.metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["size_bytes"], 3)
        self.assertEqual(metadata["sha256"], hashlib.sha256(b"NEW").hexdigest())

    def test_metadata_only_reports_missing_chromium_without_installing(self) -> None:
        fake = FakePlaywright([], missing_browser_times=99)
        stdout = io.StringIO()
        with mock.patch.object(douyin, "sync_playwright", fake.factory()), mock.patch.object(
            douyin.subprocess, "run"
        ) as run, contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                douyin.main(
                    [f"https://www.douyin.com/video/{VIDEO_ID}", str(self.output), "--metadata-only"]
                )
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("python3 -m playwright install chromium", stdout.getvalue())
        self.assertIn("--install-chromium", stdout.getvalue())
        run.assert_not_called()
        self.assertEqual(fake.launches, 1)
        self.assertFalse(Path(f"{self.output}.metadata.json").exists())


if __name__ == "__main__":
    unittest.main()
