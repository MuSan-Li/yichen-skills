#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import html
import http.client
import importlib.util
import io
import json
import re
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "wechat_known_url.py"
    spec = importlib.util.spec_from_file_location("content_archive_wechat", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wechat = load_module()

BIZ = "MzA3MDM3NjE5NQ=="
MID = "2650000001"
IDX = "1"
SN = "0123456789abcdef0123456789abcdef"
CHKSM = "84f4c5d1b3834cc7ad5a"
SHORT_ID = "AbCdEfGhIjKlMnOpQrSt"
SHORT_CANONICAL = f"https://mp.weixin.qq.com/s/{SHORT_ID}"
LONG_CANONICAL = (
    f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&idx={IDX}&sn={SN}&chksm={CHKSM}"
)
SECRETS = (
    "SECRETKEY123",
    "SECRETUIN456",
    "SECRETTICKET789",
    "SECRETEXPORT000",
    "SECRETDEVICE111",
)
LONG_WITH_SECRETS = (
    f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&idx={IDX}&sn={SN}"
    f"&chksm={CHKSM}&key=SECRETKEY123&uin=SECRETUIN456&pass_ticket=SECRETTICKET789"
    "&exportkey=SECRETEXPORT000&devicetype=SECRETDEVICE111&scene=1#rd"
)
FILLER = "这是一段足够长的正文内容，用来通过最短正文门槛的检查，避免被当作摘要。"

DEFAULT_BODY = """
<section><p>第一段<strong>加粗</strong>文字，正文足够长，用于通过最短正文门槛的检查。</p></section>
<h2>小标题</h2>
<p>请看<a href="https://example.com/a_b?x=1">示例链接</a>。</p>
<p><img data-src="https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png" src="data:image/gif;base64,AAAA" alt="图一"></p>
<ul><li>甲</li><li>乙<ol><li>子一</li></ol></li></ul>
<section class="code-snippet__fix"><ul class="code-snippet__line-index"><li></li><li></li></ul><pre class="code-snippet__js" data-lang="python"><code><span>print("hi")</span></code><code><span>x = 1</span></code></pre></section>
<script>var leaked = "SCRIPT_TEXT";</script>
<p style="display: none">HIDDEN_TEXT</p>
<p>结尾段落<br>第二行</p>
"""

VERIFY_HTML = (
    "<html><head><title>环境异常</title></head><body><div class='weui-msg'>"
    "<h2 class='weui-msg__title'>环境异常</h2>"
    "<p>当前环境异常，完成验证后即可继续访问。</p><a href='#'>去验证</a>"
    "</div></body></html>"
)
DELETED_HTML = (
    "<html><head><title>微信公众平台</title></head><body><div class='weui-msg'>"
    "<h2 class='weui-msg__title warn'>该内容已被发布者删除</h2></div></body></html>"
)
# Shapes of real pages seen on 2026-10-31: a migrated account's interstitial
# and the static notice the shut-down relay now returns for every request.
MIGRATED_TARGET = (
    "https://mp.weixin.qq.com/s?__biz=MzAwMDAwMDAwMQ==&mid=2247480001&idx=1"
    "&sn=fedcba9876543210fedcba9876543210"
)
MIGRATED_HTML = (
    "<html><head><title>账号已迁移</title></head><body><div class='weui-msg'>"
    "<h2 class='weui-msg__title'>该公众号已迁移</h2>"
    "<p>该公众号已迁移至新的账号，原账号已回收。若需访问原文章链接，请点击下方按钮。</p>"
    "<a href='' id='js_access_msg'>访问文章</a></div>"
    "<script>var transferTargetLink = 'http://mp.weixin.qq.com/s?__biz=MzAwMDAwMDAwMQ==&amp;"
    "mid=2247480001&amp;idx=1&amp;sn=fedcba9876543210fedcba9876543210#rd';</script>"
    "</body></html>"
)
RELAY_OFFLINE_HTML = (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />'
    "<title>公众号文章导出工具已下线</title>"
    '<link rel="canonical" href="https://down.mptext.top/" /></head>'
    "<body><main><h1>公众号文章导出工具已下线</h1>"
    "<p>微信已关闭这个工具依赖的接口，同步功能无法恢复，在线网站因此停止服务。</p>"
    "</main></body></html>"
)


def article_html(*, mid: str = MID, body: str = DEFAULT_BODY, title: str = "测试文章标题", with_meta: bool = True) -> str:
    meta = ""
    script = ""
    if with_meta:
        meta = (
            f'<meta property="og:title" content="{title}" />'
            f'<meta property="og:url" content="http://mp.weixin.qq.com/s?__biz={BIZ}'
            f'&amp;mid={mid}&amp;idx={IDX}&amp;sn={SN}&amp;chksm={CHKSM}#rd" />'
            '<meta property="og:article:author" content="张三" />'
        )
        script = '<script>var nickname = htmlDecode("测试公众号"); var ct = "1700000000";</script>'
    header = (
        f'<h1 class="rich_media_title" id="activity-name">  {title} </h1>'
        '<a id="js_name"> 测试公众号 </a><em id="publish_time"></em>'
        if with_meta
        else ""
    )
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title if with_meta else ""}</title>'
        f"{meta}{script}<style>.rich_media{{color:red}}</style></head>"
        f'<body><div id="js_article">{header}'
        f'<div class="rich_media_content js_underline_content" id="js_content" style="visibility: hidden;">{body}</div>'
        '<div id="js_pc_qr_code">微信扫一扫 关注该公众号</div></div></body></html>'
    )


class FakeResponse:
    def __init__(self, body, *, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = http.client.HTTPMessage()
        self.headers["Content-Type"] = content_type

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
IMAGE_HOST = "mmbiz.qpic.cn"


def image_response(body: bytes = PNG_BYTES, *, content_type: str = "image/png", status: int = 200):
    response = FakeResponse(body, status=status, content_type=content_type)
    response.headers["Content-Length"] = str(len(body))
    return response


class FakeNetwork:
    """Queue-driven stand-in for wechat.open_request; any extra request fails.

    Image CDN requests get a small PNG unless ``images`` queues something else.
    """

    def __init__(self, direct=(), relay=(), images=()) -> None:
        self.direct = list(direct)
        self.relay = list(relay)
        self.images = list(images)
        self.requests = []

    def __call__(self, opener, request, timeout):
        self.requests.append(request)
        host = urllib.parse.urlsplit(request.full_url).hostname
        if host == IMAGE_HOST:
            item = self.images.pop(0) if self.images else image_response()
        else:
            queue = self.direct if host == "mp.weixin.qq.com" else self.relay
            if host not in {"mp.weixin.qq.com", "down.mptext.top"} or not queue:
                raise AssertionError(f"unexpected request to {host}")
            item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return FakeResponse(item)
        return item

    def hosts(self):
        return [urllib.parse.urlsplit(request.full_url).hostname for request in self.requests]

    def page_hosts(self):
        return [host for host in self.hosts() if host != IMAGE_HOST]

    def image_requests(self):
        return [
            request
            for request in self.requests
            if urllib.parse.urlsplit(request.full_url).hostname == IMAGE_HOST
        ]


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://mp.weixin.qq.com/s/x", code, "error", http.client.HTTPMessage(), None
    )


class NetworkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps = []
        patcher = mock.patch.object(wechat, "_sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory(prefix="wechat-known-url-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()

    def use_network(self, network: FakeNetwork) -> FakeNetwork:
        patcher = mock.patch.object(wechat, "open_request", network)
        patcher.start()
        self.addCleanup(patcher.stop)
        return network

    def run_main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = wechat.main(argv)
        text = buffer.getvalue()
        return code, json.loads(text), text

    def fetch(self, url: str, *, relay: bool = True):
        return wechat.fetch_article(
            wechat.normalize_article_url(url),
            timeout=10,
            throttle=wechat.Throttle(1.0),
            relay_enabled=relay,
        )


class UrlNormalizationTests(unittest.TestCase):
    def test_short_form_drops_query_and_fragment(self) -> None:
        article = wechat.normalize_article_url(
            f"https://MP.weixin.qq.com/s/{SHORT_ID}?scene=1&key=SECRETKEY123#rd"
        )
        self.assertEqual(article.canonical_url, SHORT_CANONICAL)
        self.assertEqual(article.form, "short")
        self.assertEqual(article.dropped_param_count, 2)

    def test_long_form_keeps_only_locators_and_drops_sensitive_values(self) -> None:
        article = wechat.normalize_article_url(LONG_WITH_SECRETS)
        self.assertEqual(article.canonical_url, LONG_CANONICAL)
        self.assertEqual(article.identity, (BIZ, MID, IDX))
        self.assertEqual(article.dropped_param_count, 6)
        for secret in SECRETS:
            self.assertNotIn(secret, article.canonical_url)

    def test_html_escaped_separators_from_page_source_are_accepted(self) -> None:
        article = wechat.normalize_article_url(
            f"https://mp.weixin.qq.com/s?__biz={BIZ}&amp;mid={MID}&amp;idx={IDX}&amp;sn={SN}"
        )
        self.assertEqual(
            article.canonical_url,
            f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&idx={IDX}&sn={SN}",
        )

    def test_non_https_other_hosts_credentials_and_ports_are_invalid(self) -> None:
        rejected = (
            f"http://mp.weixin.qq.com/s/{SHORT_ID}",
            f"https://example.com/s/{SHORT_ID}",
            f"https://mp.weixin.qq.com.evil.example/s/{SHORT_ID}",
            f"https://user:pass@mp.weixin.qq.com/s/{SHORT_ID}",
            f"https://evil.example\\@mp.weixin.qq.com/s/{SHORT_ID}",
            f"https://mp.weixin.qq.com:8443/s/{SHORT_ID}",
            f"https://mp.weixin.qq.com/s/{SHORT_ID} extra",
            f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=abc&idx={IDX}&sn={SN}",
            f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&mid=2650000002&idx={IDX}&sn={SN}",
            "file:///etc/passwd",
            "",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(wechat.ArchiveError) as context:
                    wechat.normalize_article_url(url)
                self.assertEqual(context.exception.reason, "invalid_url")

    def test_container_and_account_pages_are_unsupported(self) -> None:
        rejected = (
            f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={BIZ}&scene=124#wechat_redirect",
            f"https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz={BIZ}&offset=10",
            f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={BIZ}&action=getalbum&album_id=123",
            f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={BIZ}&mid={MID}&idx={IDX}&sn={SN}&album_id=1",
            f"https://mp.weixin.qq.com/mp/homepage?__biz={BIZ}&hid=1",
            f"https://mp.weixin.qq.com/mp/getmasssendmsg?__biz={BIZ}",
            "https://mp.weixin.qq.com/cgi-bin/appmsg?action=list_ex&begin=0",
            f"https://mp.weixin.qq.com/mp/appmsg/show?__biz={BIZ}&appmsgid={MID}&itemidx=1",
            f"https://mp.weixin.qq.com/s?__biz={BIZ}",
            "https://mp.weixin.qq.com/s/short",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(wechat.ArchiveError) as context:
                    wechat.normalize_article_url(url)
                self.assertEqual(context.exception.reason, "unsupported_url")


class ExtractionTests(unittest.TestCase):
    def test_markdown_keeps_structure_and_drops_scripts_and_hidden_nodes(self) -> None:
        page = wechat.extract_page(article_html())
        markdown = page.markdown
        self.assertIn("第一段**加粗**文字", markdown)
        self.assertIn("## 小标题", markdown)
        self.assertIn("请看[示例链接](https://example.com/a_b?x=1)。", markdown)
        self.assertIn("![图一](https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png)", markdown)
        self.assertIn("- 甲\n- 乙\n  1. 子一", markdown)
        self.assertIn('```python\nprint("hi")\nx = 1\n```', markdown)
        self.assertIn("结尾段落  \n第二行", markdown)
        for leaked in ("SCRIPT_TEXT", "HIDDEN_TEXT", "微信扫一扫", "data:image", "color:red"):
            self.assertNotIn(leaked, markdown)
        self.assertEqual(page.images, ["https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png"])

    def test_metadata_comes_from_meta_tags_and_page_variables(self) -> None:
        page = wechat.extract_page(article_html())
        self.assertEqual(page.title, "测试文章标题")
        self.assertEqual(page.account_name, "测试公众号")
        self.assertEqual(page.author, "张三")
        self.assertEqual(page.published_at, "2023-11-14T22:13:20Z")
        self.assertEqual(page.page_canonical_url, LONG_CANONICAL)

    def test_missing_metadata_stays_null(self) -> None:
        page = wechat.extract_page(article_html(with_meta=False, body=f"<p>{FILLER}</p>"))
        self.assertIsNone(page.title)
        self.assertIsNone(page.account_name)
        self.assertIsNone(page.author)
        self.assertIsNone(page.published_at)
        self.assertIsNone(page.page_canonical_url)
        self.assertIsNone(wechat.page_failure(page, ""))

    def test_accidental_markdown_syntax_in_text_is_escaped(self) -> None:
        body = f"<p>1. 不是列表</p><p># 不是标题</p><p>*星号* 和 _下划线_</p><p>{FILLER}</p>"
        markdown = wechat.extract_page(article_html(body=body)).markdown
        self.assertIn("1\\. 不是列表", markdown)
        self.assertIn("\\# 不是标题", markdown)
        self.assertIn("\\*星号\\* 和 \\_下划线\\_", markdown)

    def test_blockquote_and_bold_spanning_blocks_stay_valid(self) -> None:
        body = (
            "<blockquote><p>引用一</p><p>引用<em>二</em></p></blockquote>"
            f"<strong><p>整段一</p><p>整段二</p></strong><p><strong> </strong>{FILLER}</p>"
        )
        markdown = wechat.extract_page(article_html(body=body)).markdown
        self.assertIn("> 引用一\n>\n> 引用*二*", markdown)
        self.assertIn("**整段一**\n\n**整段二**", markdown)
        self.assertNotIn("****", markdown)

    def test_table_becomes_gfm_table(self) -> None:
        body = (
            f"<p>{FILLER}</p><table><tr><th>名称</th><th>值</th></tr>"
            "<tr><td>a|b</td><td><p>1</p></td></tr></table>"
        )
        markdown = wechat.extract_page(article_html(body=body)).markdown
        self.assertIn("| 名称 | 值 |\n| --- | --- |\n| a\\|b | 1 |", markdown)

    def test_body_quoting_page_state_phrases_is_still_an_article(self) -> None:
        body = f"<p>当前环境异常，完成验证后即可继续访问。该内容已被发布者删除。请登录。{FILLER}</p>"
        page = wechat.extract_page(article_html(body=body))
        self.assertIsNone(wechat.page_failure(page, ""))

    def test_short_body_fails_but_image_only_article_passes(self) -> None:
        short = wechat.extract_page(article_html(body="<p>太短</p>"))
        self.assertEqual(wechat.page_failure(short, ""), "content_too_short")
        image_only = wechat.extract_page(
            article_html(body='<p><img data-src="https://mmbiz.qpic.cn/a/640"></p>')
        )
        self.assertIsNone(wechat.page_failure(image_only, ""))

    def test_verification_deleted_and_missing_pages_are_classified(self) -> None:
        cases = (
            (VERIFY_HTML, "blocked_or_verification"),
            (DELETED_HTML, "deleted_or_unavailable"),
            ("<html><body><p>参数错误</p></body></html>", "link_invalid_or_expired"),
            ("<html><body><p>请在微信客户端打开链接</p></body></html>", "login_required"),
            ("<html><body><script>location.replace('/mp/wappoc_appmsgcaptcha?x=1')</script></body></html>", "blocked_or_verification"),
            ("<html><body><p>别的页面</p></body></html>", "missing_content"),
        )
        for html_text, reason in cases:
            with self.subTest(reason=reason):
                page = wechat.extract_page(html_text)
                self.assertEqual(wechat.page_failure(page, html_text), reason)

    def test_migrated_account_page_is_its_own_reason_with_normalized_target(self) -> None:
        page = wechat.extract_page(MIGRATED_HTML)
        self.assertEqual(wechat.page_failure(page, MIGRATED_HTML), "account_migrated")
        self.assertEqual(wechat.migration_target(MIGRATED_HTML), MIGRATED_TARGET)
        # A target that is not an exact article link is never reported.
        bogus = MIGRATED_HTML.replace(
            "http://mp.weixin.qq.com/s?__biz", "https://evil.example/s?__biz"
        )
        self.assertIsNone(wechat.migration_target(bogus))
        self.assertIsNone(wechat.migration_target(DELETED_HTML))

    def test_safe_title_strips_separators_controls_and_limits_length(self) -> None:
        self.assertEqual(wechat.safe_title(None), "article")
        self.assertEqual(wechat.safe_title("..."), "article")
        traversal = wechat.safe_title("../../etc/passwd")
        self.assertNotIn("/", traversal)
        self.assertFalse(traversal.startswith("."))
        tricky = wechat.safe_title("\x00\u202e-evil:name*?\"<>|\\#[x]")
        self.assertFalse(tricky.startswith("-"))
        self.assertIsNone(re.search(r'[\x00-\x1f\u202e/\\:*?"<>|#\[\]]', tricky))
        long_title = wechat.safe_title("标题" * 200)
        self.assertLessEqual(len(long_title), wechat.TITLE_MAX_CHARS)
        self.assertLessEqual(len(long_title.encode("utf-8")), wechat.TITLE_MAX_BYTES)


class FetchTests(NetworkTestCase):
    def test_direct_success_sends_no_cookie_and_skips_relay(self) -> None:
        network = self.use_network(FakeNetwork(direct=[article_html()]))
        outcome = self.fetch(LONG_WITH_SECRETS)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.backend_used, "wechat-direct")
        self.assertEqual(network.hosts(), ["mp.weixin.qq.com"])
        request = network.requests[0]
        self.assertEqual(request.full_url, LONG_CANONICAL)
        self.assertFalse(request.has_header("Cookie"))
        self.assertTrue(request.get_header("User-agent").startswith("Mozilla/5.0"))

    def test_verification_page_falls_back_to_relay_html_with_only_public_url(self) -> None:
        network = self.use_network(FakeNetwork(direct=[VERIFY_HTML], relay=[article_html()]))
        outcome = self.fetch(LONG_WITH_SECRETS)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.backend_used, "mptext-html")
        self.assertEqual(
            [(item["backend"], item["reason"]) for item in outcome.attempts],
            [("wechat-direct", "blocked_or_verification"), ("mptext-html", None)],
        )
        relay = urllib.parse.urlsplit(network.requests[1].full_url)
        self.assertEqual((relay.hostname, relay.path), ("down.mptext.top", "/api/public/v1/download"))
        self.assertEqual(
            urllib.parse.parse_qs(relay.query),
            {"url": [LONG_CANONICAL], "format": ["html"]},
        )
        self.assertFalse(network.requests[1].has_header("Cookie"))
        for secret in SECRETS:
            self.assertNotIn(secret, network.requests[1].full_url)
        self.assertEqual(len(self.sleeps), 1)
        self.assertGreaterEqual(self.sleeps[0], 0.5)

    def test_relay_membership_or_error_json_is_never_success(self) -> None:
        cases = (
            ('{"code": 401, "msg": "该格式需要会员认证"}', "relay_membership_required"),
            ('{"code": 403, "msg": "\\u9700\\u8981\\u4f1a\\u5458"}', "relay_membership_required"),
            ('{"code": 500, "msg": "server busy"}', "relay_error_json"),
        )
        for body, relay_reason in cases:
            with self.subTest(relay_reason=relay_reason, body=body):
                self.use_network(
                    FakeNetwork(
                        direct=[VERIFY_HTML],
                        relay=[FakeResponse(body, content_type="application/json")],
                    )
                )
                outcome = self.fetch(SHORT_CANONICAL)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.reason, "blocked_or_verification")
                self.assertEqual(outcome.attempts[1]["reason"], relay_reason)

    def test_relay_json_sent_as_html_is_still_rejected(self) -> None:
        self.use_network(FakeNetwork(direct=[http_error(503)], relay=['{"error": "invalid url"}']))
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "http_error")
        self.assertEqual(outcome.attempts[1]["reason"], "relay_error_json")

    def test_deleted_article_never_uses_relay(self) -> None:
        network = self.use_network(FakeNetwork(direct=[DELETED_HTML]))
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "deleted_or_unavailable")
        self.assertEqual(network.hosts(), ["mp.weixin.qq.com"])

    def test_relay_deleted_page_is_authoritative(self) -> None:
        self.use_network(FakeNetwork(direct=[VERIFY_HTML], relay=[DELETED_HTML]))
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertEqual(outcome.reason, "deleted_or_unavailable")

    def test_migrated_account_never_uses_relay_and_reports_target(self) -> None:
        network = self.use_network(FakeNetwork(direct=[MIGRATED_HTML]))
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "account_migrated")
        self.assertEqual(outcome.details, {"migrated_to": MIGRATED_TARGET})
        # The new URL is only reported, never fetched.
        self.assertEqual(network.hosts(), ["mp.weixin.qq.com"])

    def test_relay_shutdown_notice_is_relay_offline_not_missing_content(self) -> None:
        self.use_network(FakeNetwork(direct=[VERIFY_HTML], relay=[RELAY_OFFLINE_HTML]))
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "blocked_or_verification")
        self.assertEqual(outcome.attempts[1]["reason"], "relay_offline")
        self.assertTrue(wechat.relay_reported_offline(outcome.attempts))

    def test_python39_socket_timeout_is_classified_on_both_backends(self) -> None:
        self.use_network(
            FakeNetwork(
                direct=[socket.timeout("timed out")],
                relay=[urllib.error.URLError(socket.timeout("timed out"))],
            )
        )
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "timeout")
        self.assertEqual(
            [item["reason"] for item in outcome.attempts], ["timeout", "relay_timeout"]
        )

    def test_http_exception_and_os_error_are_network_errors(self) -> None:
        for error in (http.client.RemoteDisconnected("closed"), ConnectionResetError("reset")):
            with self.subTest(error=type(error).__name__):
                self.use_network(FakeNetwork(direct=[error]))
                outcome = self.fetch(SHORT_CANONICAL, relay=False)
                self.assertEqual(outcome.reason, "network_error")

    def test_http_404_falls_back_and_429_is_rate_limited(self) -> None:
        self.use_network(FakeNetwork(direct=[http_error(404)], relay=[article_html()]))
        outcome = self.fetch(SHORT_CANONICAL)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.attempts[0]["http_status"], 404)
        self.use_network(FakeNetwork(direct=[http_error(429)]))
        self.assertEqual(self.fetch(SHORT_CANONICAL, relay=False).reason, "rate_limited")

    def test_oversized_response_is_rejected(self) -> None:
        self.use_network(FakeNetwork(direct=[article_html()]))
        with mock.patch.object(wechat, "MAX_HTML_BYTES", 64):
            outcome = self.fetch(SHORT_CANONICAL, relay=False)
        self.assertEqual(outcome.reason, "response_too_large")

    def test_page_for_another_article_is_a_mismatch_without_relay(self) -> None:
        network = self.use_network(FakeNetwork(direct=[article_html(mid="2650009999")]))
        outcome = self.fetch(LONG_CANONICAL)
        self.assertEqual(outcome.reason, "target_mismatch")
        self.assertEqual(len(network.requests), 1)

    def test_redirects_only_follow_normalized_article_urls(self) -> None:
        handler = wechat._ArticleRedirectHandler()
        request = urllib.request.Request(SHORT_CANONICAL, headers={"User-Agent": "test"})
        followed = handler.redirect_request(
            request, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), LONG_WITH_SECRETS
        )
        self.assertEqual(followed.full_url, LONG_CANONICAL)
        cases = (
            ("https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=abc", "blocked_or_verification"),
            (f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={BIZ}", "unexpected_redirect"),
            ("https://evil.example/s/AbCdEfGhIjKl", "unexpected_redirect"),
        )
        for target, reason in cases:
            with self.subTest(target=target):
                with self.assertRaises(wechat.FetchError) as context:
                    handler.redirect_request(
                        request, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), target
                    )
                self.assertEqual(context.exception.reason, reason)

    def test_throttle_keeps_minimum_interval(self) -> None:
        clock = iter([100.0, 100.25])
        slept = []
        throttle = wechat.Throttle(1.0, clock=lambda: next(clock), sleep=slept.append)
        throttle.wait()
        throttle.mark()
        throttle.wait()
        self.assertEqual(slept, [0.75])


class CommandTests(NetworkTestCase):
    def all_written_text(self, root: Path) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix in {".md", ".json", ".jsonl", ".html"}
        )

    def test_read_outputs_json_article_without_writing(self) -> None:
        network = self.use_network(FakeNetwork(direct=[article_html()]))
        with mock.patch.object(wechat, "allocate_run_dir", side_effect=AssertionError("no writes")):
            code, payload, text = self.run_main(["read", LONG_WITH_SECRETS])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["files_written"])
        self.assertFalse(payload["relay_contacted"])
        self.assertFalse(payload["discovery_performed"])
        article = payload["article"]
        self.assertEqual(article["title"], "测试文章标题")
        self.assertEqual(article["account_name"], "测试公众号")
        self.assertEqual(article["author"], "张三")
        self.assertEqual(article["published_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(article["canonical_url"], LONG_CANONICAL)
        self.assertIn("## 小标题", article["markdown"])
        self.assertEqual(article["images"], ["https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png"])
        self.assertEqual(len(network.requests), 1)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_read_failure_reports_reason_and_exit_code(self) -> None:
        self.use_network(FakeNetwork(direct=[DELETED_HTML]))
        code, payload, _ = self.run_main(["read", SHORT_CANONICAL])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "deleted_or_unavailable")
        self.assertNotIn("article", payload)
        self.assertNotIn("migrated_to", payload)

    def test_read_and_download_of_migrated_article_record_new_url(self) -> None:
        self.use_network(FakeNetwork(direct=[MIGRATED_HTML, MIGRATED_HTML]))
        code, payload, _ = self.run_main(["read", SHORT_CANONICAL])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "account_migrated")
        self.assertEqual(payload["migrated_to"], MIGRATED_TARGET)
        self.assertFalse(payload["relay_contacted"])

        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out")]
        )
        self.assertEqual(code, 1)
        rows = [
            json.loads(line)
            for line in Path(payload["manifest_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [(row["status"], row["reason"], row["migrated_to"]) for row in rows],
            [("failed", "account_migrated", MIGRATED_TARGET)],
        )
        failures = json.loads(Path(payload["failures_path"]).read_text(encoding="utf-8"))
        self.assertEqual(failures[0]["migrated_to"], MIGRATED_TARGET)

    def test_relay_offline_once_stops_relay_for_rest_of_batch(self) -> None:
        network = self.use_network(
            FakeNetwork(direct=[VERIFY_HTML, VERIFY_HTML], relay=[RELAY_OFFLINE_HTML])
        )
        url_file = self.tmp / "urls.txt"
        url_file.write_text(
            f"https://mp.weixin.qq.com/s/{SHORT_ID}1\nhttps://mp.weixin.qq.com/s/{SHORT_ID}2\n",
            encoding="utf-8",
        )
        code, payload, _ = self.run_main(
            [
                "download",
                "--url-file",
                str(url_file),
                "--output-dir",
                str(self.tmp / "out"),
                "--relay-fallback",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(network.hosts(), ["mp.weixin.qq.com", "down.mptext.top", "mp.weixin.qq.com"])
        self.assertTrue(payload["relay_offline_detected"])
        summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
        self.assertTrue(summary["relay_offline_detected"])
        rows = [
            json.loads(line)
            for line in Path(payload["manifest_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [[(a["backend"], a["reason"]) for a in row["attempts"]] for row in rows],
            [
                [("wechat-direct", "blocked_or_verification"), ("mptext-html", "relay_offline")],
                [("wechat-direct", "blocked_or_verification")],
            ],
        )

    def test_url_file_mixed_lines_are_recorded_and_processing_continues(self) -> None:
        network = self.use_network(
            FakeNetwork(direct=[article_html(), VERIFY_HTML], relay=[article_html()])
        )
        url_file = self.tmp / "urls.txt"
        url_file.write_text(
            "\n".join(
                [
                    "# 已知文章清单",
                    f"{SHORT_CANONICAL}?scene=1&key=SECRETKEY123",
                    "not-a-url SECRETUIN456",
                    f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={BIZ}&pass_ticket=SECRETTICKET789",
                    "",
                    LONG_WITH_SECRETS,
                    f"{SHORT_CANONICAL}#rd",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.tmp / "out"
        code, payload, text = self.run_main(
            ["download", "--url-file", str(url_file), "--output-dir", str(output), "--relay-fallback"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(
            payload["counts"],
            {"input": 5, "success": 2, "failed": 1, "skipped": 1, "unsupported": 1},
        )
        self.assertTrue(payload["relay_contacted"])
        run_dir = Path(payload["output_dir"])
        self.assertEqual(run_dir, output.resolve())

        rows = [
            json.loads(line)
            for line in Path(payload["manifest_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [(row["source_url_ref"], row["status"], row["reason"]) for row in rows],
            [
                ("urls.txt:2", "success", None),
                ("urls.txt:3", "failed", "invalid_url"),
                ("urls.txt:4", "unsupported", "unsupported_url"),
                ("urls.txt:6", "success", None),
                ("urls.txt:7", "skipped", "duplicate_input"),
            ],
        )
        self.assertTrue(all(row["route_backend"] == "wechat-known-url" for row in rows))
        self.assertEqual(rows[3]["backend_used"], "mptext-html")
        self.assertEqual(rows[0]["canonical_url"], SHORT_CANONICAL)
        markdown_files = sorted((run_dir / "wechat").glob("*/*.md"))
        self.assertEqual(
            [path.relative_to(run_dir / "wechat").as_posix() for path in markdown_files],
            ["001-测试文章标题/001-测试文章标题.md", "004-测试文章标题/004-测试文章标题.md"],
        )
        self.assertEqual(rows[0]["artifact_paths"][0], str(markdown_files[0]))
        document = markdown_files[0].read_text(encoding="utf-8")
        self.assertTrue(document.startswith("---\ntitle: \"测试文章标题\"\n"))
        self.assertIn(f'canonical_url: "{SHORT_CANONICAL}"', document)
        self.assertIn("# 测试文章标题", document)

        failures = json.loads(Path(payload["failures_path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            [(item["source_url_ref"], item["stage"], item["reason"]) for item in failures],
            [("urls.txt:3", "validate", "invalid_url"), ("urls.txt:4", "validate", "unsupported_url")],
        )
        summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
        self.assertFalse(summary["scope"]["discovery_performed"])
        self.assertEqual(summary["scope"]["input_kind"], "url_file")
        self.assertFalse(summary["settings"]["cookies_sent"])
        handoff = json.loads(Path(payload["handoff_path"]).read_text(encoding="utf-8"))
        self.assertEqual(handoff["handoff_version"], "yichen-content-handoff/v1")
        self.assertFalse(handoff["scope"]["discovery_performed"])
        self.assertTrue(handoff["authorization"]["authorization_not_transferable"])

        self.assertEqual(network.page_hosts(), ["mp.weixin.qq.com", "mp.weixin.qq.com", "down.mptext.top"])
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(all(value >= 1.0 for value in self.sleeps))
        written = self.all_written_text(run_dir)
        sent = "\n".join(request.full_url for request in network.requests)
        for secret in SECRETS:
            self.assertNotIn(secret, written)
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, sent)

    def test_save_html_writes_raw_page_next_to_markdown(self) -> None:
        self.use_network(FakeNetwork(direct=[article_html()]))
        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out"), "--save-html"]
        )
        self.assertEqual(code, 0)
        article_dir = Path(payload["articles_dir"]) / "001-测试文章标题"
        names = sorted(path.name for path in article_dir.iterdir())
        self.assertEqual(
            names, ["001-测试文章标题.html", "001-测试文章标题.md", "images", "metadata.json"]
        )

    def test_existing_output_dir_gets_new_run_and_keeps_old_files(self) -> None:
        self.use_network(FakeNetwork(direct=[article_html()]))
        requested = self.tmp / "out"
        requested.mkdir()
        sentinel = requested / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(requested)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(Path(payload["output_dir"]).name, "out-run-2")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(sorted(path.name for path in requested.iterdir()), ["keep.txt"])

    def test_default_output_dir_is_timestamped_with_random_suffix(self) -> None:
        self.use_network(FakeNetwork(direct=[article_html()]))
        root = self.tmp / "wechat-mp-known"
        with mock.patch.object(wechat, "default_output_root", return_value=root):
            code, payload, _ = self.run_main(["download", SHORT_CANONICAL])
        self.assertEqual(code, 0)
        run_dir = Path(payload["output_dir"])
        self.assertEqual(run_dir.parent, root.resolve())
        self.assertRegex(run_dir.name, r"^\d{8}-\d{6}-[0-9a-f]{6}$")

    def test_limit_default_cap_and_overflow_are_enforced_before_side_effects(self) -> None:
        self.assertEqual(wechat.parse_args(["download", SHORT_CANONICAL]).limit, 50)
        network = self.use_network(FakeNetwork())
        output = self.tmp / "never"
        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--limit", "101", "--output-dir", str(output)]
        )
        self.assertEqual((code, payload["reason"]), (2, "limit_out_of_range"))
        url_file = self.tmp / "three.txt"
        url_file.write_text(
            "\n".join(f"https://mp.weixin.qq.com/s/{SHORT_ID}{index}" for index in range(3)),
            encoding="utf-8",
        )
        code, payload, _ = self.run_main(
            ["download", "--url-file", str(url_file), "--limit", "2", "--output-dir", str(output)]
        )
        self.assertEqual((code, payload["reason"]), (2, "limit_exceeded"))
        self.assertFalse(payload["side_effects_performed"])
        self.assertFalse(output.exists())
        self.assertEqual(network.requests, [])

    def test_invalid_positional_url_and_bad_arguments_reject_without_echoing_secrets(self) -> None:
        network = self.use_network(FakeNetwork())
        output = self.tmp / "never"
        code, payload, text = self.run_main(
            [
                "download",
                f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={BIZ}&key=SECRETKEY123",
                "--output-dir",
                str(output),
            ]
        )
        self.assertEqual((code, payload["reason"]), (2, "unsupported_url"))
        self.assertNotIn("SECRETKEY123", text)
        code, payload, text = self.run_main(
            ["read", SHORT_CANONICAL, f"https://mp.weixin.qq.com/s/x?pass_ticket=SECRETTICKET789"]
        )
        self.assertEqual((code, payload["reason"]), (2, "invalid_arguments"))
        self.assertNotIn("SECRETTICKET789", text)
        self.assertFalse(output.exists())
        self.assertEqual(network.requests, [])

    def test_repeated_verification_pages_stop_the_batch(self) -> None:
        network = self.use_network(FakeNetwork(direct=[VERIFY_HTML] * 3))
        url_file = self.tmp / "urls.txt"
        url_file.write_text(
            "\n".join(f"https://mp.weixin.qq.com/s/{SHORT_ID}{index}" for index in range(5)),
            encoding="utf-8",
        )
        code, payload, _ = self.run_main(
            [
                "download",
                "--url-file",
                str(url_file),
                "--output-dir",
                str(self.tmp / "out"),
                "--no-relay-fallback",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(payload["stopped_early"])
        self.assertEqual(payload["counts"]["failed"], 3)
        self.assertEqual(payload["counts"]["skipped"], 2)
        self.assertEqual(len(network.requests), 3)
        self.assertFalse(payload["relay_contacted"])


class RelayDefaultTests(NetworkTestCase):
    """down.mptext.top is shut down (2026-10-31): the relay is opt-in only."""

    def test_read_and_download_never_contact_relay_by_default(self) -> None:
        network = self.use_network(FakeNetwork(direct=[VERIFY_HTML, VERIFY_HTML]))
        code, payload, _ = self.run_main(["read", SHORT_CANONICAL])
        self.assertEqual((code, payload["reason"]), (1, "blocked_or_verification"))
        self.assertFalse(payload["relay_contacted"])
        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out")]
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["relay_contacted"])
        summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
        self.assertFalse(summary["settings"]["relay_fallback_enabled"])
        self.assertEqual(network.hosts(), ["mp.weixin.qq.com", "mp.weixin.qq.com"])

    def test_legacy_no_relay_flag_is_accepted_and_conflicting_flags_reject(self) -> None:
        self.assertFalse(wechat.parse_args(["read", SHORT_CANONICAL]).relay_fallback)
        self.assertFalse(
            wechat.parse_args(["read", SHORT_CANONICAL, "--no-relay-fallback"]).relay_fallback
        )
        self.assertTrue(
            wechat.parse_args(["download", SHORT_CANONICAL, "--relay-fallback"]).relay_fallback
        )
        network = self.use_network(FakeNetwork())
        code, payload, _ = self.run_main(
            ["read", SHORT_CANONICAL, "--relay-fallback", "--no-relay-fallback"]
        )
        self.assertEqual((code, payload["reason"]), (2, "invalid_arguments"))
        self.assertEqual(network.requests, [])


IMAGE_A = "https://mmbiz.qpic.cn/mmbiz_png/aaa/640?wx_fmt=png&from=appmsg"
IMAGE_B = "https://mmbiz.qpic.cn/sz_mmbiz_jpg/bbb/640?wx_fmt=jpeg"
IMAGE_C = "https://mmbiz.qpic.cn/mmbiz_gif/ccc/640?wx_fmt=gif&token=SECRETIMG&tp=webp"
FOREIGN_IMAGE = "https://img.example.com/x.png"


def image_body() -> str:
    return (
        f"<p>{FILLER}</p>"
        f'<p><img data-src="{IMAGE_A}" alt="甲"></p>'
        f'<p><img data-src="{IMAGE_B}" alt="乙"></p>'
        f'<p><img data-src="{IMAGE_C}" alt="丙"></p>'
        f'<p><img src="{FOREIGN_IMAGE}" alt="外站"></p>'
    )


class ImageLocalizationTests(NetworkTestCase):
    def download(self, network: FakeNetwork):
        self.use_network(network)
        code, payload, text = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out")]
        )
        article_dir = Path(payload["articles_dir"]) / "001-测试文章标题"
        rows = [
            json.loads(line)
            for line in Path(payload["manifest_path"]).read_text(encoding="utf-8").splitlines()
        ]
        return code, payload, text, article_dir, rows

    def test_images_are_stored_locally_with_referer_and_markdown_points_at_them(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x01" * 40
        network = FakeNetwork(
            direct=[article_html(body=image_body())],
            images=[
                image_response(),
                image_response(jpeg, content_type="image/jpeg"),
                image_response(b"GIF89a" + b"\x02" * 10, content_type="application/octet-stream"),
            ],
        )
        code, payload, text, article_dir, rows = self.download(network)
        # The foreign host is never contacted, so the run is partial.
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["counts"]["success"], 1)
        self.assertEqual(payload["image_counts"], {"total": 4, "downloaded": 3, "failed": 1})
        requests = network.image_requests()
        self.assertEqual(len(requests), 3)
        for request in requests:
            self.assertEqual(request.get_header("Referer"), "https://mp.weixin.qq.com/")
            self.assertFalse(request.has_header("Cookie"))
        self.assertNotIn("img.example.com", network.hosts())
        self.assertEqual(
            sorted(path.name for path in (article_dir / "images").iterdir()),
            ["001.png", "002.jpg", "003.gif"],
        )
        self.assertEqual((article_dir / "images" / "002.jpg").read_bytes(), jpeg)
        self.assertEqual(list((article_dir / "images").glob("*.part")), [])

        document = (article_dir / "001-测试文章标题.md").read_text(encoding="utf-8")
        self.assertIn("![甲](images/001.png)", document)
        self.assertIn("![乙](images/002.jpg)", document)
        self.assertIn("![丙](images/003.gif)", document)
        self.assertIn(f"![外站]({FOREIGN_IMAGE})", document)
        self.assertIn("image_local_count: 3", document)
        self.assertNotIn("mmbiz.qpic.cn", document)

        metadata = json.loads((article_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["image_status"], "partial")
        self.assertEqual(
            [(item["path"], item["source_url"], item["status"]) for item in metadata["images"]],
            [
                ("images/001.png", IMAGE_A, "success"),
                ("images/002.jpg", IMAGE_B, "success"),
                (
                    "images/003.gif",
                    "https://mmbiz.qpic.cn/mmbiz_gif/ccc/640?wx_fmt=gif&tp=webp",
                    "success",
                ),
                (None, FOREIGN_IMAGE, "failed"),
            ],
        )
        self.assertEqual(metadata["images"][3]["reason"], "image_host_not_allowed")
        self.assertEqual(metadata["images"][1]["size_bytes"], len(jpeg))

        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["image_status"], "partial")
        self.assertEqual(rows[0]["image_local_count"], 3)
        self.assertEqual(
            rows[0]["failed_images"],
            [{"index": 4, "reason": "image_host_not_allowed", "source_url": FOREIGN_IMAGE}],
        )
        failures = json.loads(Path(payload["failures_path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            [(item["stage"], item["reason"]) for item in failures],
            [("images", "image_download_partial")],
        )
        # A sensitive-looking query value never reaches any written file or stdout.
        written = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path(payload["output_dir"]).rglob("*")
            if path.suffix in {".md", ".json", ".jsonl"}
        )
        self.assertNotIn("SECRETIMG", written)
        self.assertNotIn("SECRETIMG", text)

    def test_single_image_failures_keep_the_article_and_list_each_reason(self) -> None:
        network = FakeNetwork(
            direct=[article_html(body=image_body())],
            images=[
                urllib.error.HTTPError(IMAGE_A, 404, "nf", http.client.HTTPMessage(), None),
                image_response(b"<svg onload='x'/>", content_type="image/svg+xml"),
                socket.timeout("timed out"),
            ],
        )
        code, payload, _, article_dir, rows = self.download(network)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(
            [item["reason"] for item in rows[0]["failed_images"]],
            ["image_http_error", "image_unexpected_type", "image_timeout", "image_host_not_allowed"],
        )
        self.assertFalse((article_dir / "images").exists())
        document = (article_dir / "001-测试文章标题.md").read_text(encoding="utf-8")
        self.assertIn(f"![甲]({IMAGE_A})", document)
        self.assertIn("image_local_count: 0", document)

    def test_size_and_length_gates(self) -> None:
        oversized = image_response()
        oversized.headers.replace_header("Content-Length", str(wechat.MAX_IMAGE_BYTES + 1))
        short = image_response()
        short.headers.replace_header("Content-Length", str(len(PNG_BYTES) + 5))
        network = self.use_network(FakeNetwork(images=[oversized, short, image_response(b"")]))
        for reason in ("image_too_large", "image_incomplete", "image_empty"):
            with self.subTest(reason=reason):
                with self.assertRaises(wechat.FetchError) as context:
                    wechat.fetch_image(IMAGE_A, 10)
                self.assertEqual(context.exception.reason, reason)
        self.assertEqual(len(network.requests), 3)

    def test_only_allowlisted_https_cdn_urls_are_fetched_or_followed(self) -> None:
        self.assertEqual(
            wechat.image_download_url("http://mmbiz.qpic.cn/a/640?wx_fmt=png"),
            "https://mmbiz.qpic.cn/a/640?wx_fmt=png",
        )
        self.assertEqual(
            wechat.image_download_url("//mmbiz.qpic.cn/a/640"), "https://mmbiz.qpic.cn/a/640"
        )
        for rejected in (
            "https://mmbiz.qpic.cn.evil.example/a.png",
            "https://evil.example/mmbiz.qpic.cn/a.png",
            "https://user@mmbiz.qpic.cn/a.png",
            "https://mmbiz.qpic.cn:8443/a.png",
            "ftp://mmbiz.qpic.cn/a.png",
            "data:image/png;base64,AAAA",
            None,
        ):
            with self.subTest(url=rejected):
                self.assertIsNone(wechat.image_download_url(rejected))
        handler = wechat._ImageRedirectHandler()
        request = urllib.request.Request(IMAGE_A)
        followed = handler.redirect_request(
            request, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "/other/640"
        )
        self.assertEqual(followed.full_url, "https://mmbiz.qpic.cn/other/640")
        with self.assertRaises(wechat.FetchError) as context:
            handler.redirect_request(
                request, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "https://127.0.0.1/x"
            )
        self.assertEqual(context.exception.reason, "image_redirect_rejected")

    def test_recorded_links_drop_only_sensitive_query_values(self) -> None:
        self.assertEqual(wechat.recorded_image_url(IMAGE_A), IMAGE_A)
        self.assertEqual(
            wechat.recorded_image_url(
                "https://mmbiz.qpic.cn/a/640?wx_fmt=png&pass_ticket=T&sign=S&from=appmsg&exportkey=E"
            ),
            "https://mmbiz.qpic.cn/a/640?wx_fmt=png&from=appmsg",
        )

    def test_read_mode_never_downloads_images(self) -> None:
        network = self.use_network(FakeNetwork(direct=[article_html(body=image_body())]))
        code, payload, _ = self.run_main(["read", SHORT_CANONICAL])
        self.assertEqual(code, 0)
        self.assertFalse(payload["article"]["images_downloaded"])
        self.assertEqual(payload["article"]["image_count"], 4)
        self.assertEqual(network.image_requests(), [])
        self.assertIn(f"]({IMAGE_A})", payload["article"]["markdown"])


VIDEO_BODY = (
    f"<p>{FILLER}</p>"
    '<p><iframe class="video_iframe rich_pages" data-vidtype="2" data-mpvid="wxv_3141592653589793238" '
    'data-src="https://mp.weixin.qq.com/mp/readtemplate?t=pages/video_player_tmpl&amp;vid=wxv_3141592653589793238" '
    'style="display: none;"></iframe></p>'
    '<p><iframe class="video_iframe" data-src="https://v.qq.com/iframe/preview.html?vid=x0012abcd&amp;width=500"></iframe></p>'
    '<mp-common-videosnap data-feedid="export/ExampleFeedId000000" data-nickname="某视频号" '
    'data-desc="视频号的[标题]"></mp-common-videosnap>'
    "<p>(紧跟的括号文字)</p>"
)


class VideoPlaceholderTests(NetworkTestCase):
    def test_embedded_videos_leave_placeholders_and_identifiers(self) -> None:
        page = wechat.extract_page(article_html(body=VIDEO_BODY))
        self.assertEqual(
            page.videos,
            [
                {"kind": "wechat_video", "vid": "wxv_3141592653589793238", "title": None},
                {"kind": "tencent_video", "vid": "x0012abcd", "title": None},
                {
                    "kind": "channels_video",
                    "vid": "export/ExampleFeedId000000",
                    "title": "视频号的[标题]",
                    "account": "某视频号",
                },
            ],
        )
        markdown = page.markdown
        self.assertIn("[视频：wxv\\_3141592653589793238，未下载]", markdown)
        self.assertIn("[视频：x0012abcd，未下载]", markdown)
        self.assertIn("[视频：视频号的\\[标题\\]，未下载]", markdown)
        self.assertNotIn("未下载](", markdown)
        self.assertNotIn("v.qq.com", markdown)

    def test_video_only_article_succeeds_and_download_records_video_count(self) -> None:
        body = (
            '<p><iframe class="video_iframe" data-mpvid="wxv_2718281828459045235"></iframe></p>'
            "<p>短</p>"
        )
        page = wechat.extract_page(article_html(body=body))
        self.assertIsNone(wechat.page_failure(page, ""))
        network = self.use_network(FakeNetwork(direct=[article_html(body=body)]))
        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out")]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        article_dir = Path(payload["articles_dir"]) / "001-测试文章标题"
        metadata = json.loads((article_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["video_count"], 1)
        self.assertEqual(metadata["videos"][0]["vid"], "wxv_2718281828459045235")
        self.assertFalse(metadata["videos_downloaded"])
        self.assertEqual(metadata["image_status"], "none")
        document = (article_dir / "001-测试文章标题.md").read_text(encoding="utf-8")
        self.assertIn("video_count: 1", document)
        self.assertIn("[视频：wxv\\_2718281828459045235，未下载]", document)
        self.assertEqual(network.image_requests(), [])


def js_quote(text: str) -> str:
    """Escape like WeChat's server templates do inside a JS string literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("&", "\\x26")
        .replace("<", "\\x3c")
        .replace(">", "\\x3e")
        .replace('"', "\\x22")
        .replace("'", "\\x27")
        .replace("\n", "\\x0a")
    )


IMAGE_POST_A = "https://mmbiz.qpic.cn/sz_mmbiz_png/postaaa/0?wx_fmt=png&from=appmsg"
IMAGE_POST_B = "https://mmbiz.qpic.cn/mmbiz_jpg/postbbb/0?wx_fmt=jpeg"
IMAGE_POST_WATERMARKED = "http://mmbiz.qpic.cn/sz_mmbiz_jpg/postwm/0?wx_fmt=jpeg"
IMAGE_POST_COVER = "https://mmbiz.qpic.cn/mmbiz_jpg/postcover/0?wx_fmt=jpeg"
IMAGE_POST_LINK = (
    f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=2650000002&idx=1&sn={SN}&scene=142#wechat_redirect"
)
# Description as the page holds it in content_noencode: plain lines plus the
# editor's inline <a> tags (an article link and a topic tag).
IMAGE_POST_TEXT = (
    '第一段说明文字，带 "引号" 与 *星号*。\n同一段的第二行。\n\n'
    f'详见：<a href="{IMAGE_POST_LINK.replace("&", "&amp;")}" class="normal_text_link" '
    'target="_blank" data-itemshowtype="0">上一篇</a>\n\n\n'
    '<a class="wx_topic_link" topic-id="t-1" style="color: var(--weui-LINK) !important;" '
    'data-topic="1">#话题</a>'
)
IMAGE_POST_MARKDOWN = (
    f"![图 1]({IMAGE_POST_A})\n\n![图 2]({IMAGE_POST_B})\n\n![图 3]({IMAGE_POST_WATERMARKED})\n\n"
    '第一段说明文字，带 "引号" 与 \\*星号\\*。  \n同一段的第二行。\n\n'
    f"详见：[上一篇]({IMAGE_POST_LINK})\n\n#话题\n"
)


def picture_item(cdn_url: str, *, watermark: str = IMAGE_POST_WATERMARKED, original: bool = False) -> str:
    # Trimmed from the live image posts of 2026-10-31: the entry's own cdn_url
    # is the full-size picture; watermark, share cover and the signed
    # original_info host must never be picked.
    original_info = (
        "original_info: { show_original: true, cdn_url: 'http://318.wxapp.tc.qq.com/318/20304/"
        "stodownload?m=abc\\x26amp;dis_k=SECRETORIG\\x26amp;dis_t=1793437425', file_size: '192686' * 1, },"
        if original
        else ""
    )
    return (
        f"{{ cdn_url: '{js_quote(cdn_url.replace('&', '&amp;'))}', width: '1086' * 1, height: '1448' * 1,"
        " theme_color: 'rgb(1,11,31)', is_qr_code: '0' * 1, poi_info: [ ], live_photo: { format_info: [ ], },"
        f" disable_theme_color: true, bottom_right_brightness: '0.052263573' * 1,"
        f" watermark_info: {{ cdn_url: '{js_quote(watermark)}', is_uploader: true, }},"
        f" share_cover: {{ file_id: '100000593' * 1, cdn_url: '{IMAGE_POST_COVER}',"
        " crop_info: '{\\x22x1\\x22:0.058410998}', },"
        f" {original_info} }},"
    )


DEFAULT_PICTURES = (
    picture_item(IMAGE_POST_A, original=True),
    picture_item(IMAGE_POST_B),
    # No usable own link: the watermarked copy is the fallback.
    picture_item(""),
)


def image_post_html(
    *,
    pictures=DEFAULT_PICTURES,
    text: str = IMAGE_POST_TEXT,
    show_type: str = "8",
    cgi: bool = True,
    content_field: bool = True,
    og_description=None,
    extra_cgi: str = "",
    body_text: str = "",
) -> str:
    """Sanitized shape of an image post ("图片消息") page: no #js_content."""
    og = js_quote(html.escape(text)) if og_description is None else og_description
    content = f"content_noencode: '{js_quote(text)}'," if content_field else ""
    cgi_script = (
        "<script>(function() { try { window.cgiDataNew = {"
        " base_resp: { ret: '0' * 1, errmsg: 'ok', wxtoken: '777' * 1, },"
        " title: '图文页标题',"
        f" desc: '{js_quote(html.escape(text))}',"
        f" {content}"
        " nick_name: '测试图文号', author: '', user_uin: '0',"
        f" item_show_type: '{show_type}' * 1,"
        " ori_create_time: '1700000000' * 1, ori_send_time: '1700000000' * 1,"
        f" picture_page_info_list: [ {''.join(pictures)} ],"
        " hashtags: { hashtag: [ 'AI', ], }, video_page_infos: [ ], special_biz: false,"
        f" {extra_cgi}"
        " }; } catch (e) { window.cgiError = e; } })();</script>"
        if cgi
        else ""
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>图文页标题</title>'
        '<meta property="og:title" content="图文页标题" />'
        f'<meta property="og:url" content="https://mp.weixin.qq.com/s?__biz={BIZ}'
        f'&amp;mid={MID}&amp;idx={IDX}&amp;sn={SN}&amp;chksm={CHKSM}#rd" />'
        f'<meta property="og:description" content="{og}" />'
        '<meta property="og:article:author" content="" />'
        "<script>window.ITEM_SHOW_TYPE_MAP = { APP_MSG_PAGE: '0', IMG_SHARE_PAGE: '8' };"
        f" window.item_show_type = '{show_type}' || ''; window.ct = '1700000000' || '';"
        " window.msg_title = window.title = '图文页标题' || ''; window.user_uin = ('0' || '') * 1;"
        f"</script>{cgi_script}</head><body>"
        '<div id="js_article" class="share_content_page"><div id="img_swiper_content" '
        'class="share_media_swiper_content"><div id="img_list" class="share_media"></div></div>'
        f'<div class="share_content_page_bd" id="js_base_container">{body_text}</div></div>'
        '<div class="weui-desktop-popover__desc">微信扫一扫<br>使用小程序</div></body></html>'
    )


class JsLiteralTests(unittest.TestCase):
    def test_reads_the_template_literal_subset(self) -> None:
        text = (
            "{ a: '1' * 1, b: '0.5' * 1, 'q k': \"x\\x26amp;y\", list: [ 'u', 2, ], "
            "flag: true, none: null, nested: { deep: [ { k: 'v', }, ], }, emoji: '\\ud83d\\ude80', }"
        )
        value, end = wechat.read_js_literal(text, 0)
        self.assertEqual(end, len(text))
        self.assertEqual(
            value,
            {
                "a": 1,
                "b": 0.5,
                "q k": "x&amp;y",
                "list": ["u", 2],
                "flag": True,
                "none": None,
                "nested": {"deep": [{"k": "v"}]},
                "emoji": "🚀",
            },
        )

    def test_anything_else_is_rejected_not_evaluated(self) -> None:
        for text in ("{ a: f(1) }", "{ a: 'x' + 'y' }", "{ a: 'open", "[1, 2", "{ a 1 }", "[" * 40 + "]" * 40):
            with self.subTest(text=text[:20]):
                with self.assertRaises(ValueError):
                    wechat.read_js_literal(text, 0)


class ImagePostTests(NetworkTestCase):
    def test_text_and_ordered_pictures_come_from_page_data(self) -> None:
        page = wechat.extract_page(image_post_html())
        self.assertEqual(page.content_type, "image_post")
        self.assertFalse(page.content_found)
        self.assertIsNone(wechat.page_failure(page, ""))
        self.assertEqual(page.markdown, IMAGE_POST_MARKDOWN)
        self.assertEqual(page.images, [IMAGE_POST_A, IMAGE_POST_B, IMAGE_POST_WATERMARKED])
        self.assertGreater(page.text_chars, 0)
        self.assertEqual(page.title, "图文页标题")
        self.assertEqual(page.account_name, "测试图文号")
        self.assertIsNone(page.author)
        self.assertEqual(page.published_at, "2023-11-14T22:13:20Z")
        self.assertEqual(page.page_canonical_url, LONG_CANONICAL)
        for unwanted in ("postcover", "318.wxapp.tc.qq.com", "SECRETORIG", "微信扫一扫", "&amp;"):
            self.assertNotIn(unwanted, page.markdown)

    def test_text_falls_back_to_desc_then_og_description(self) -> None:
        from_desc = wechat.extract_page(image_post_html(content_field=False))
        self.assertEqual(from_desc.markdown, IMAGE_POST_MARKDOWN)
        # No page data at all: the page variable still marks an image post and
        # og:description (JS escapes kept verbatim) still has the text.
        from_meta = wechat.extract_page(image_post_html(cgi=False))
        self.assertEqual(from_meta.content_type, "image_post")
        self.assertEqual(from_meta.images, [])
        self.assertTrue(from_meta.markdown.startswith('第一段说明文字，带 "引号"'))
        self.assertIn(f"[上一篇]({IMAGE_POST_LINK})", from_meta.markdown)
        self.assertEqual(from_meta.published_at, "2023-11-14T22:13:20Z")
        self.assertIsNone(wechat.page_failure(from_meta, ""))

    def test_one_unexpected_token_does_not_lose_the_post(self) -> None:
        page = wechat.extract_page(image_post_html(extra_cgi="report: getReport(),"))
        self.assertEqual(page.content_type, "image_post")
        self.assertEqual(page.images, [IMAGE_POST_A, IMAGE_POST_B, IMAGE_POST_WATERMARKED])
        self.assertEqual(page.markdown, IMAGE_POST_MARKDOWN)
        self.assertEqual(page.account_name, "测试图文号")

    def test_picture_only_post_succeeds(self) -> None:
        page = wechat.extract_page(image_post_html(text="", og_description=""))
        self.assertIsNone(wechat.page_failure(page, ""))
        self.assertEqual(page.text_chars, 0)
        self.assertEqual(page.markdown.count("![图 "), 3)

    def test_empty_post_and_other_share_types_stay_failures(self) -> None:
        empty = image_post_html(pictures=(), text="", og_description="")
        self.assertEqual(wechat.page_failure(wechat.extract_page(empty), empty), "missing_content")
        deleted = image_post_html(
            pictures=(), text="", og_description="", body_text="该内容已被发布者删除"
        )
        self.assertEqual(
            wechat.page_failure(wechat.extract_page(deleted), deleted), "deleted_or_unavailable"
        )
        # A video share page (item_show_type 5) is not read as an image post.
        video = image_post_html(show_type="5")
        page = wechat.extract_page(video)
        self.assertEqual(page.content_type, "article")
        self.assertEqual(wechat.page_failure(page, video), "missing_content")

    def test_regular_articles_never_use_the_image_post_path(self) -> None:
        with mock.patch.object(wechat, "extract_image_post", side_effect=AssertionError("not an image post")):
            page = wechat.extract_page(article_html())
        self.assertEqual(page.content_type, "article")
        self.assertIn("## 小标题", page.markdown)

    def test_read_reports_image_post_without_downloading(self) -> None:
        network = self.use_network(FakeNetwork(direct=[image_post_html()]))
        code, payload, text = self.run_main(["read", LONG_WITH_SECRETS])
        self.assertEqual(code, 0)
        article = payload["article"]
        self.assertEqual(article["content_type"], "image_post")
        self.assertEqual(article["image_count"], 3)
        self.assertEqual(article["images"], [IMAGE_POST_A, IMAGE_POST_B, IMAGE_POST_WATERMARKED])
        self.assertFalse(article["images_downloaded"])
        self.assertEqual(article["account_name"], "测试图文号")
        self.assertEqual(article["markdown"], IMAGE_POST_MARKDOWN)
        self.assertEqual(network.image_requests(), [])
        for secret in (*SECRETS, "SECRETORIG"):
            self.assertNotIn(secret, text)

    def test_download_stores_pictures_in_page_order(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x01" * 40
        network = self.use_network(
            FakeNetwork(
                direct=[image_post_html()],
                images=[
                    image_response(),
                    image_response(jpeg, content_type="image/jpeg"),
                    image_response(jpeg, content_type="image/jpeg"),
                ],
            )
        )
        code, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out")]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["image_counts"], {"total": 3, "downloaded": 3, "failed": 0})
        self.assertEqual(
            [request.full_url for request in network.image_requests()],
            [IMAGE_POST_A, IMAGE_POST_B, "https" + IMAGE_POST_WATERMARKED[len("http"):]],
        )
        for request in network.image_requests():
            self.assertEqual(request.get_header("Referer"), "https://mp.weixin.qq.com/")
            self.assertFalse(request.has_header("Cookie"))
        article_dir = Path(payload["articles_dir"]) / "001-图文页标题"
        self.assertEqual(
            sorted(path.name for path in (article_dir / "images").iterdir()),
            ["001.png", "002.jpg", "003.jpg"],
        )
        document = (article_dir / "001-图文页标题.md").read_text(encoding="utf-8")
        self.assertIn('content_type: "image_post"', document)
        self.assertIn("image_count: 3", document)
        self.assertIn("image_local_count: 3", document)
        self.assertIn("![图 1](images/001.png)\n\n![图 2](images/002.jpg)\n\n![图 3](images/003.jpg)", document)
        self.assertIn(f"详见：[上一篇]({IMAGE_POST_LINK})", document)
        self.assertNotIn("mmbiz.qpic.cn", document)
        metadata = json.loads((article_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["content_type"], "image_post")
        self.assertEqual(metadata["image_count"], 3)
        self.assertEqual(metadata["image_status"], "complete")
        self.assertEqual(metadata["account_name"], "测试图文号")
        rows = [
            json.loads(line)
            for line in Path(payload["manifest_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[0]["content_type"], "image_post")

    def test_regular_article_outputs_say_article(self) -> None:
        self.use_network(FakeNetwork(direct=[article_html(), article_html()]))
        _, payload, _ = self.run_main(["read", SHORT_CANONICAL])
        self.assertEqual(payload["article"]["content_type"], "article")
        _, payload, _ = self.run_main(
            ["download", SHORT_CANONICAL, "--output-dir", str(self.tmp / "out")]
        )
        article_dir = Path(payload["articles_dir"]) / "001-测试文章标题"
        metadata = json.loads((article_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["content_type"], "article")
        self.assertIn(
            'content_type: "article"', (article_dir / "001-测试文章标题.md").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
