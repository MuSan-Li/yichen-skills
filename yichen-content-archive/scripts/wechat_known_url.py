#!/usr/bin/env python3
"""Read or download known public WeChat Official Account article URLs.

Only exact article URLs are accepted: ``/s/<id>`` or
``/s?__biz=&mid=&idx=&sn=``. Account pages, history, albums and homepages are
rejected; nothing is searched or enumerated, and the WeChat client is never
touched.

Each article is fetched anonymously (browser User-Agent, no Cookie) from the
public WeChat page only. The down.mptext.top ``format=html`` relay was shut
down by its operator (2026-10-31), so falling back to it is off by default and
only happens with an explicit ``--relay-fallback``. Every response must contain
a real ``#js_content`` body; error JSON, verification, login and deleted pages
never count as success. The one exception is an image post ("图片消息",
``item_show_type`` 8): it has no ``#js_content`` and is read from the page's
``window.cgiDataNew`` data instead (description text plus the ordered picture
list), recorded with ``content_type`` ``image_post``.

``download`` also stores body images under each article's ``images/`` folder
(WeChat image CDN only, sent with the ``Referer`` the CDN requires) and keeps a
visible placeholder for embedded videos, which are never downloaded.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import re
import secrets
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


ROUTE_BACKEND = "wechat-known-url"
HANDOFF_VERSION = "yichen-content-handoff/v1"
ARTICLE_HOST = "mp.weixin.qq.com"
DIRECT_BACKEND = "wechat-direct"
RELAY_BACKEND = "mptext-html"
RELAY_HOST = "down.mptext.top"
RELAY_ENDPOINT = f"https://{RELAY_HOST}/api/public/v1/download"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
RELAY_USER_AGENT = "yichen-content-archive/1.0 (+known public WeChat article reader)"
DIRECT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}
RELAY_HEADERS = {"User-Agent": RELAY_USER_AGENT, "Accept": "text/html"}
# Only hosts actually seen serving article body images (live run 2026-10-31).
# Without this Referer the CDN answers with a 140x140 placeholder instead.
IMAGE_HOSTS = frozenset({"mmbiz.qpic.cn"})
IMAGE_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": f"https://{ARTICLE_HOST}/",
}
# SVG is left out on purpose: a local SVG can carry script.
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES_PER_ARTICLE = 200
# Query parameter names that could carry a credential or signature; anything
# else (wx_fmt, from, tp, wxfrom, ...) is kept when an image link is recorded.
SENSITIVE_QUERY_NAME_RE = re.compile(
    r"(?i)^(?:key|uin|sid|sig|sign|signature|token|ticket|secret|password|cookie|"
    r"exportkey|session|credential|auth|.*(?:_token|token_|_ticket|_key|_sign|_secret|session|auth).*)$"
)

MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_URL_FILE_BYTES = 1024 * 1024
MAX_URL_LENGTH = 4096
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_INTERVAL = 1.5
MIN_INTERVAL = 1.0
MAX_INTERVAL = 30.0
DEFAULT_TIMEOUT = 20
MIN_TIMEOUT = 5
MAX_TIMEOUT = 60
MIN_BODY_TEXT_CHARS = 30
BLOCK_STOP_THRESHOLD = 3
TITLE_MAX_CHARS = 60
TITLE_MAX_BYTES = 150
MAX_SIGNAL_CHARS = 50000

SHORT_PATH_RE = re.compile(r"/s/(?P<id>[A-Za-z0-9_-]{8,64})/?")
LOCATOR_VALIDATORS = {
    "__biz": re.compile(r"[A-Za-z0-9+/]{4,64}={0,2}"),
    "mid": re.compile(r"\d{1,20}"),
    "idx": re.compile(r"\d{1,3}"),
    "sn": re.compile(r"[0-9A-Fa-f]{8,64}"),
}
REQUIRED_LOCATOR_PARAMS = ("__biz", "mid", "idx", "sn")
CHKSM_RE = re.compile(r"[0-9A-Fa-f]{8,256}")

URL_RE = re.compile(r"(?:(?:https?):)?//[^\s\"']+", re.IGNORECASE)
QUERY_TOKEN_RE = re.compile(r"\S*[?&=]\S*")
MD_ESCAPE_RE = re.compile(r"[\\`*_\[\]<>~|]")
SAFE_LANG_RE = re.compile(r"[A-Za-z0-9_+#.-]{1,20}")
# Zero-width spaces, BOM and bidi overrides; ZWJ/ZWNJ stay so emoji survive.
INVISIBLE_CHARS = dict.fromkeys(
    map(ord, "\u200b\ufeff\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"),
    None,
)

# Page-state markers are only consulted after extraction failed, and only
# against text outside #js_content, so article bodies quoting these phrases
# can never turn a real article into a failure.
PAGE_STATE_MARKERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # A migrated account serves an interstitial pointing at the article's new
    # URL; it is not a deleted article, so it gets its own final reason.
    ("account_migrated", ("该公众号已迁移", "账号已迁移")),
    (
        "deleted_or_unavailable",
        (
            "该内容已被发布者删除",
            "此内容因违规无法查看",
            "此内容被多人投诉",
            "因涉嫌违反相关法律法规",
            "该公众号已被屏蔽",
            "此帐号已被屏蔽",
            "此账号已被屏蔽",
            "该内容已被删除",
            "此内容已被删除",
            "此内容发送失败无法查看",
            "此内容无法查看",
            "已停止访问该网页",
        ),
    ),
    ("link_invalid_or_expired", ("参数错误", "链接已过期", "页面不存在")),
    (
        "blocked_or_verification",
        (
            "环境异常",
            "完成验证",
            "去验证",
            "访问过于频繁",
            "请求过于频繁",
            "操作过于频繁",
            "访问频繁",
            "安全验证",
            "人机验证",
        ),
    ),
    (
        "login_required",
        ("请在微信客户端打开", "扫码登录", "请登录", "登录后查看", "微信登录"),
    ),
)
RELAY_MEMBERSHIP_RE = re.compile(
    r"会员|认证|授权|登录|权限|付费|vip|member|\bauth\b|authoriz|authenticat|"
    r"unauthor|forbidden|permission|login|api[ _-]?key",
    re.IGNORECASE,
)
# A deleted/blocked article or a mismatched page is a final state; the relay
# is never used to get around it.
AUTHORITATIVE_REASONS = frozenset(
    {"deleted_or_unavailable", "account_migrated", "link_invalid_or_expired", "target_mismatch"}
)
# The migration interstitial names the new article URL in a page variable.
TRANSFER_TARGET_PATTERNS = (
    re.compile(r"var\s+transferTargetLink\s*=\s*'([^'\\\n]{1,2000})'"),
    re.compile(r'var\s+transferTargetLink\s*=\s*"([^"\\\n]{1,2000})"'),
)
# down.mptext.top was shut down by its operator (seen 2026-10-31): every request
# gets a static "tool is offline" notice instead of article HTML.
RELAY_OFFLINE_TITLE_RE = re.compile(
    r"<title[^>]*>[^<]{0,120}(?:已下线|停止服务|停服)[^<]{0,120}</title>", re.IGNORECASE
)
RELAY_ROOT_CANONICAL_RE = re.compile(
    r"<link\s+rel=[\"']canonical[\"']\s+href=[\"']https://down\.mptext\.top/?[\"']",
    re.IGNORECASE,
)
BLOCKING_REASONS = frozenset({"blocked_or_verification", "rate_limited"})
RELAY_TRANSPORT_REASONS = frozenset(
    {"timeout", "network_error", "http_error", "rate_limited", "response_too_large"}
)
REASON_MESSAGES = {
    "invalid_url": "只接受公开 HTTPS mp.weixin.qq.com 文章链接",
    "unsupported_url": "只支持单篇文章链接；账号主页、历史消息、合集或主页等容器不支持",
    "http_error": "微信公开页返回了非 200 状态",
    "rate_limited": "微信公开页限流（HTTP 429）",
    "timeout": "请求超时",
    "network_error": "网络连接失败",
    "response_too_large": "响应超过大小上限",
    "unexpected_content_type": "响应不是 HTML",
    "unexpected_redirect": "跳转到了非文章地址，已停止",
    "parse_failed": "页面 HTML 无法解析",
    "blocked_or_verification": "遇到环境异常、验证或访问频繁页面，未取得正文",
    "login_required": "页面要求登录或在微信客户端打开，未取得正文",
    "deleted_or_unavailable": "文章已删除、因违规不可查看或账号已屏蔽",
    "account_migrated": "公众号已迁移到新账号，原链接只剩迁移说明页；新文章链接见 migrated_to，未自动跟随，需用户确认后作为新的已知链接处理",
    "link_invalid_or_expired": "链接参数错误或已过期",
    "missing_content": "页面没有 #js_content 正文，也没有可识别的图文页文字或图片",
    "content_too_short": "#js_content 正文过短，不视为成功",
    "target_mismatch": "页面文章标识与输入链接不一致",
    "relay_timeout": "中转请求超时",
    "relay_network_error": "中转网络连接失败",
    "relay_http_error": "中转返回了非 200 状态",
    "relay_rate_limited": "中转限流（HTTP 429）",
    "relay_response_too_large": "中转响应超过大小上限",
    "relay_membership_required": "中转返回会员/认证限制 JSON，未取得正文",
    "relay_error_json": "中转返回错误 JSON 而不是正文 HTML",
    "relay_unexpected_content": "中转响应不是 HTML",
    "relay_offline": "中转 down.mptext.top 已下线（返回停服说明页），未取得正文；本次运行后续条目不再请求中转",
    "duplicate_input": "与前面的输入是同一篇文章，已跳过",
    "stopped_after_repeated_blocking": "连续遇到验证或限流，已停止后续请求",
    "write_failed": "正文文件写入失败",
    "image_download_partial": "正文已保存，但部分图片没有存到本地（见 failed_images，Markdown 对这些图片保留原链接）",
    "image_host_not_allowed": "图片不在微信图片 CDN 白名单主机上，未下载",
    "image_limit_exceeded": f"超过单篇 {MAX_IMAGES_PER_ARTICLE} 张图片上限，未下载",
    "image_redirect_rejected": "图片跳转到了白名单之外的地址，已停止",
    "image_http_error": "图片 CDN 返回了非 200 状态",
    "image_timeout": "图片请求超时",
    "image_network_error": "图片网络连接失败",
    "image_too_large": f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB 上限",
    "image_empty": "图片响应为空",
    "image_incomplete": "图片字节数与 Content-Length 不一致",
    "image_unexpected_type": "响应不是支持的位图类型（jpg/png/gif/webp/bmp/avif）",
    "image_write_failed": "图片文件写入失败",
}

VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }
)
CONTENT_SKIP_TAGS = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "iframe", "nav",
        "button", "select", "textarea", "form", "object", "canvas", "audio",
        "video", "mpvoice", "mpvideo", "qqmusic", "mp-common-profile",
        "mp-miniprogram", "mp-common-videosnap",
    }
)
# Embedded videos are still not downloaded, but each one leaves a visible
# placeholder and an identifier instead of vanishing from the article.
# WeChat's editor only allows iframes for video players.
VIDEO_TAGS = frozenset({"iframe", "video", "mpvideo", "mp-common-videosnap"})
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_.:=+/-]{1,160}")
OUTSIDE_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "aside", "main",
        "figure", "figcaption", "center", "address", "dl", "dt", "dd",
        "tbody", "thead", "tfoot", "caption",
    }
)
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
CAPTURE_IDS = frozenset({"activity-name", "js_name", "publish_time"})
GENERIC_PAGE_TITLES = frozenset({"微信公众平台", "Weixin Official Accounts Platform"})

JS_STRING_PATTERNS = {
    "nickname": (
        re.compile(r'var\s+nickname\s*=\s*htmlDecode\(\s*"((?:[^"\\\n]|\\.){1,400})"'),
        re.compile(r'var\s+nickname\s*=\s*"((?:[^"\\\n]|\\.){1,400})"'),
    ),
    "msg_title": (
        re.compile(r"var\s+msg_title\s*=\s*'((?:[^'\\\n]|\\.){1,600})'"),
        re.compile(r'var\s+msg_title\s*=\s*"((?:[^"\\\n]|\\.){1,600})"'),
    ),
    "msg_link": (re.compile(r'var\s+msg_link\s*=\s*"((?:[^"\\\n]|\\.){1,2000})"'),),
}
TIMESTAMP_PATTERNS = (
    re.compile(r'var\s+ct\s*=\s*"(\d{9,11})"'),
    re.compile(r'var\s+create_time\s*=\s*"(\d{9,11})"'),
)
PUBLISH_DATE_PATTERN = re.compile(r'var\s+publish_time\s*=\s*"(\d{4}-\d{2}-\d{2})"')
DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EARLIEST_TIMESTAMP = 1325376000  # 2012-01-01, before the first public articles.

# Image posts ("图片消息"; the page's ITEM_SHOW_TYPE_MAP calls 8 IMG_SHARE_PAGE)
# are rendered client-side from the ``window.cgiDataNew`` object literal, so
# they carry no #js_content. Other share types (5 video, 6 music, 7 audio,
# 10 text, 17 short content) have not been sampled and stay missing_content.
IMAGE_POST_SHOW_TYPE = 8
ITEM_SHOW_TYPE_PATTERNS = (
    re.compile(r'var\s+item_show_type\s*=\s*"(\d{1,3})"'),
    re.compile(r"window\.item_show_type\s*=\s*'(\d{1,3})'"),
)
CGI_DATA_RE = re.compile(r"window\.cgiDataNew\s*=\s*\{")
MAX_CGI_DATA_CHARS = 2 * 1024 * 1024
MAX_JS_LITERAL_DEPTH = 32
# Read one by one only when the whole object literal could not be parsed.
IMAGE_POST_FIELDS = (
    "item_show_type",
    "picture_page_info_list",
    "content_noencode",
    "desc",
    "nick_name",
    "author",
    "ori_create_time",
    "ori_send_time",
)
IMAGE_POST_TEXT_ROOT = "image-post-text"
WINDOW_CT_PATTERN = re.compile(r"window\.ct\s*=\s*'(\d{9,11})'")


class ArchiveError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class FetchError(ArchiveError):
    def __init__(
        self,
        reason: str,
        message: Optional[str] = None,
        *,
        http_status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(reason, message or REASON_MESSAGES.get(reason, reason))
        self.http_status = http_status
        self.details: Dict[str, Any] = dict(details or {})


@dataclass(frozen=True)
class KnownArticle:
    canonical_url: str
    form: str
    identity: Tuple[str, ...]
    dropped_param_count: int


@dataclass
class HttpResponse:
    status: int
    content_type: str
    charset: Optional[str]
    body: bytes


@dataclass
class PageExtraction:
    content_found: bool
    markdown: str
    images: List[str]
    text_chars: int
    title: Optional[str]
    account_name: Optional[str]
    author: Optional[str]
    published_at: Optional[str]
    page_canonical_url: Optional[str]
    signal_text: str
    videos: List[Dict[str, Any]] = field(default_factory=list)
    content_type: str = "article"


@dataclass
class ImagePost:
    markdown: str
    images: List[str]
    text_chars: int
    videos: List[Dict[str, Any]]
    account_name: Optional[str]
    author: Optional[str]
    published_at: Optional[str]


@dataclass
class FetchOutcome:
    ok: bool
    backend_used: Optional[str]
    page: Optional[PageExtraction]
    html_text: Optional[str]
    reason: Optional[str]
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InputItem:
    ref: str
    article: Optional[KnownArticle]
    error: Optional[ArchiveError]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sanitized_message(value: str) -> str:
    """Drop URLs and query-like tokens so discarded parameters never surface."""
    text = URL_RE.sub("[URL]", value or "")
    text = QUERY_TOKEN_RE.sub("[REDACTED]", text)
    return " ".join(text.split())[:300] or "invalid_arguments"


# ---------------------------------------------------------------------------
# URL validation


def _parse_query(query: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    # Links copied from page source often keep HTML-escaped separators.
    for chunk in query.replace("&amp;", "&").split("&"):
        if not chunk:
            continue
        name, _, raw_value = chunk.partition("=")
        pairs.append((urllib.parse.unquote(name), urllib.parse.unquote(raw_value)))
    return pairs


def normalize_article_url(raw: str) -> KnownArticle:
    """Normalize one exact public article URL, keeping only locator params."""
    value = (raw or "").strip()
    if (
        not value
        or len(value) > MAX_URL_LENGTH
        or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value)
    ):
        raise ArchiveError("invalid_url", "只接受单个公开 HTTPS 公众号文章链接")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ArchiveError("invalid_url", "公众号文章链接无法解析") from exc
    if parsed.scheme.lower() != "https":
        raise ArchiveError("invalid_url", "只接受 HTTPS 公众号文章链接")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ArchiveError("invalid_url", "公众号文章链接不得包含内嵌凭证")
    if port is not None:
        raise ArchiveError("invalid_url", "公众号文章链接不得指定端口")
    if parsed.netloc.lower().rstrip(".") != ARTICLE_HOST:
        raise ArchiveError("invalid_url", "只接受 mp.weixin.qq.com 的文章链接")

    pairs = _parse_query(parsed.query)
    short = SHORT_PATH_RE.fullmatch(parsed.path)
    if short:
        short_id = short.group("id")
        return KnownArticle(
            canonical_url=f"https://{ARTICLE_HOST}/s/{short_id}",
            form="short",
            identity=(short_id,),
            dropped_param_count=len(pairs),
        )

    if parsed.path not in {"/s", "/s/"}:
        raise ArchiveError("unsupported_url", REASON_MESSAGES["unsupported_url"])

    values: Dict[str, str] = {}
    chksm: Optional[str] = None
    dropped = 0
    for name, param_value in pairs:
        if name in LOCATOR_VALIDATORS:
            if name in values and values[name] != param_value:
                raise ArchiveError("invalid_url", f"文章定位参数 {name} 出现多个不同值")
            values[name] = param_value
        elif name == "chksm" and chksm is None and CHKSM_RE.fullmatch(param_value):
            chksm = param_value
        else:
            dropped += 1
    if any(name not in values for name in REQUIRED_LOCATOR_PARAMS):
        raise ArchiveError(
            "unsupported_url",
            "长链接必须同时包含 __biz、mid、idx、sn；账号或容器链接不支持",
        )
    for name in REQUIRED_LOCATOR_PARAMS:
        if not LOCATOR_VALIDATORS[name].fullmatch(values[name]):
            raise ArchiveError("invalid_url", f"文章定位参数 {name} 格式不正确")
    query = "&".join(
        f"{name}={urllib.parse.quote(values[name], safe='=')}"
        for name in REQUIRED_LOCATOR_PARAMS
    )
    if chksm:
        query += f"&chksm={chksm}"
    return KnownArticle(
        canonical_url=f"https://{ARTICLE_HOST}/s?{query}",
        form="long",
        identity=(values["__biz"], values["mid"], values["idx"]),
        dropped_param_count=dropped,
    )


# ---------------------------------------------------------------------------
# Network


class _ArticleRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to exact article URLs, re-normalized first."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urljoin(req.full_url, newurl)
        parsed = urllib.parse.urlsplit(target)
        same_host = (parsed.hostname or "").lower().rstrip(".") == ARTICLE_HOST
        if same_host and "captcha" in parsed.path.lower():
            fp.close()
            raise FetchError("blocked_or_verification", http_status=code)
        if same_host and parsed.scheme.lower() == "http":
            target = "https" + target[len(parsed.scheme):]
        try:
            article = normalize_article_url(target)
        except ArchiveError:
            fp.close()
            raise FetchError("unexpected_redirect", http_status=code) from None
        return super().redirect_request(req, fp, code, msg, headers, article.canonical_url)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def image_download_url(src: Optional[str]) -> Optional[str]:
    """The https URL to fetch for a body image, or None when its host is not allowlisted."""
    url = safe_http_url(src)
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in IMAGE_HOSTS or port not in {None, 443} or "@" in parsed.netloc:
        return None
    return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))


def recorded_image_url(src: str) -> str:
    """Image link as recorded in metadata: sensitive-looking query values removed."""
    try:
        parsed = urllib.parse.urlsplit(src)
    except ValueError:
        return src
    if not parsed.query:
        return src
    kept: List[str] = []
    removed = False
    for chunk in parsed.query.split("&"):
        name = urllib.parse.unquote(chunk.partition("=")[0])
        if name and SENSITIVE_QUERY_NAME_RE.fullmatch(name):
            removed = True
            continue
        kept.append(chunk)
    if not removed:
        return src
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "&".join(kept), parsed.fragment)
    )


class _ImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow image redirects only to allowlisted https CDN hosts."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = image_download_url(urllib.parse.urljoin(req.full_url, newurl))
        if target is None:
            fp.close()
            raise FetchError("image_redirect_rejected", http_status=code)
        return super().redirect_request(req, fp, code, msg, headers, target)


# No opener has a cookie processor, so no Cookie header is ever sent.
DIRECT_OPENER = urllib.request.build_opener(_ArticleRedirectHandler())
RELAY_OPENER = urllib.request.build_opener(_NoRedirectHandler())
IMAGE_OPENER = urllib.request.build_opener(_ImageRedirectHandler())


def open_request(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: int):
    """Network seam; tests replace this function."""
    return opener.open(request, timeout=timeout)


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class Throttle:
    """Keep at least ``interval`` seconds between the end of one request and the next."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.interval = interval
        self._clock = clock or _monotonic
        self._sleep = sleep or _sleep
        self._last: Optional[float] = None

    def wait(self) -> None:
        if self._last is None:
            return
        remaining = self.interval - (self._clock() - self._last)
        if remaining > 0:
            self._sleep(remaining)

    def mark(self) -> None:
        self._last = self._clock()


def http_get(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: Dict[str, str],
    timeout: int,
) -> HttpResponse:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with open_request(opener, request, timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            content_type = response.headers.get("Content-Type", "") or ""
            charset = response.headers.get_content_charset()
            body = response.read(MAX_HTML_BYTES + 1)
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        reason = "rate_limited" if exc.code == 429 else "http_error"
        raise FetchError(reason, f"HTTP {exc.code}", http_status=exc.code) from None
    except (socket.timeout, TimeoutError):
        # Python 3.9: socket.timeout is not a TimeoutError subclass and is not
        # wrapped in URLError while waiting for headers or reading the body.
        raise FetchError("timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise FetchError("timeout") from None
        raise FetchError("network_error") from None
    except (http.client.HTTPException, OSError):
        raise FetchError("network_error") from None
    if len(body) > MAX_HTML_BYTES:
        raise FetchError("response_too_large", http_status=status)
    return HttpResponse(status=status, content_type=content_type, charset=charset, body=body)


def throttled_get(
    throttle: Throttle,
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: Dict[str, str],
    timeout: int,
) -> HttpResponse:
    throttle.wait()
    try:
        return http_get(opener, url, headers, timeout)
    finally:
        throttle.mark()


def image_extension(content_type: str, body: bytes) -> Optional[str]:
    """Safe file extension from Content-Type, sniffing only untyped responses."""
    if content_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[content_type]
    if content_type in {"", "application/octet-stream", "binary/octet-stream"}:
        for magic, extension in IMAGE_MAGIC:
            if body.startswith(magic):
                return extension
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            return ".webp"
    return None


def fetch_image(url: str, timeout: int) -> Tuple[str, bytes]:
    """Fetch one allowlisted body image fully into memory under a size cap."""
    request = urllib.request.Request(url, headers=dict(IMAGE_HEADERS), method="GET")
    try:
        with open_request(IMAGE_OPENER, request, timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            headers = response.headers
            raw_length = (headers.get("Content-Length") or "").strip()
            encoding = (headers.get("Content-Encoding") or "").strip().lower()
            declared = int(raw_length) if raw_length.isdigit() and encoding in {"", "identity"} else None
            if declared is not None and declared > MAX_IMAGE_BYTES:
                raise FetchError("image_too_large", http_status=status)
            body = response.read(MAX_IMAGE_BYTES + 1)
            content_type = headers.get_content_type() if headers.get("Content-Type") else ""
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        raise FetchError("image_http_error", f"HTTP {exc.code}", http_status=exc.code) from None
    except (socket.timeout, TimeoutError):
        raise FetchError("image_timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise FetchError("image_timeout") from None
        raise FetchError("image_network_error") from None
    except (http.client.HTTPException, OSError):
        raise FetchError("image_network_error") from None
    if status != 200:
        raise FetchError("image_http_error", f"HTTP {status}", http_status=status)
    if len(body) > MAX_IMAGE_BYTES:
        raise FetchError("image_too_large", http_status=status)
    if not body:
        raise FetchError("image_empty", http_status=status)
    if declared is not None and len(body) != declared:
        raise FetchError("image_incomplete", http_status=status)
    return content_type.lower(), body


def decode_body(response: HttpResponse) -> str:
    try:
        return response.body.decode(response.charset or "utf-8", errors="replace")
    except LookupError:
        return response.body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Markdown conversion


def escape_md(text: str) -> str:
    return MD_ESCAPE_RE.sub(r"\\\g<0>", text)


def _escape_line_start(line: str) -> str:
    ordered = re.match(r"(\d{1,9})([.)])(\s|$)", line)
    if ordered:
        return ordered.group(1) + "\\" + line[len(ordered.group(1)):]
    if re.match(r"(#{1,6}(\s|$)|[-+](\s|$)|>|=+\s*$|-+\s*$)", line):
        return "\\" + line
    return line


def _visible_len(text: str) -> int:
    return len("".join(text.split()))


def safe_http_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    url = value.strip()
    if url.startswith("//"):
        url = "https:" + url
    if len(url) > MAX_URL_LENGTH or any(
        ch.isspace() or unicodedata.category(ch) == "Cc" for ch in url
    ):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return url


def _video_id(*values: Optional[str]) -> Optional[str]:
    for value in values:
        text = (value or "").strip()
        if text and VIDEO_ID_RE.fullmatch(text):
            return text
    return None


def video_info(tag: str, attrs: Dict[str, str]) -> Dict[str, Any]:
    """Identify an embedded video by public identifiers only; never its media URL."""
    src = attrs.get("data-src") or attrs.get("src") or ""
    try:
        parsed = urllib.parse.urlsplit(html.unescape(src.strip()))
        host = (parsed.hostname or "").lower()
        params = dict(_parse_query(parsed.query))
    except ValueError:
        host, params = "", {}
    vid = _video_id(
        attrs.get("data-mpvid"),
        attrs.get("data-vid"),
        attrs.get("vid"),
        params.get("vid"),
        params.get("mpvid"),
        attrs.get("data-feedid"),
    )
    if tag == "mp-common-videosnap":
        kind = "channels_video"
    elif host == "v.qq.com" or host.endswith(".v.qq.com"):
        kind = "tencent_video"
    elif attrs.get("data-mpvid") or (vid or "").startswith("wxv_") or host == ARTICLE_HOST:
        kind = "wechat_video"
    elif tag == "video":
        kind = "html5_video"
    else:
        kind = "embedded_video"
    info: Dict[str, Any] = {
        "kind": kind,
        "vid": vid,
        "title": clean_meta_text(
            attrs.get("data-title") or attrs.get("data-desc") or attrs.get("title"), 120
        ),
    }
    account = clean_meta_text(attrs.get("data-nickname"), 80)
    if account:
        info["account"] = account
    return info


def _md_url(url: str) -> str:
    return (
        url.replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
    )


def _hidden(attrs: Dict[str, str]) -> bool:
    style = re.sub(r"\s+", "", attrs.get("style", "").lower())
    return "display:none" in style


class _Frame:
    __slots__ = ("tag", "kind", "skip", "marker", "href")

    def __init__(self, tag: str, *, kind: str = "", skip: bool = False) -> None:
        self.tag = tag
        self.kind = kind
        self.skip = skip
        self.marker: Optional[int] = None
        self.href: Optional[str] = None


class MarkdownBuilder:
    """Convert the #js_content subtree into Markdown with only the stdlib."""

    def __init__(self, root_tag: str) -> None:
        self.stack: List[_Frame] = [_Frame(root_tag, kind="block")]
        self.closed = False
        self.blocks: List[Tuple[str, bool, int]] = []
        self.inline: List[str] = []
        self.images: List[str] = []
        self.videos: List[Dict[str, Any]] = []
        self.text_chars = 0
        self.skip_depth = 0
        self.heading: Optional[int] = None
        self.lists: List[Dict[str, Any]] = []
        self.quote_depth = 0
        self.pre_depth = 0
        self.pre_buffer: List[str] = []
        self.pre_lang = ""
        self.table_depth = 0
        self.table_rows: List[List[str]] = []
        self.table_row: Optional[List[str]] = None
        self.bold_depth = 0
        self.italic_depth = 0
        self.strike_depth = 0
        self.code_depth = 0
        self.link_depth = 0

    # -- parser callbacks -------------------------------------------------

    def start(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        if tag in VOID_TAGS:
            self._void(tag, attr_map)
            return
        if tag in VIDEO_TAGS and not self.skip_depth and not self.pre_depth:
            # Recorded even when styled hidden: players are often revealed by script.
            self._video(tag, attr_map)
        skip = (
            self.skip_depth > 0
            or tag in CONTENT_SKIP_TAGS
            or _hidden(attr_map)
            or (tag == "ul" and "code-snippet__line-index" in attr_map.get("class", ""))
        )
        frame = _Frame(tag, skip=skip)
        self.stack.append(frame)
        if skip:
            self.skip_depth += 1
            return
        self._open(frame, attr_map)

    def end(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                break
        else:
            return
        while len(self.stack) > index:
            frame = self.stack.pop()
            if frame.skip:
                self.skip_depth -= 1
                continue
            self._close(frame)
        if not self.stack:
            self.closed = True

    def text(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.pre_depth:
            self.pre_buffer.append(data)
            self.text_chars += _visible_len(data)
            return
        collapsed = re.sub(r"\s+", " ", data.translate(INVISIBLE_CHARS))
        if not collapsed:
            return
        self.text_chars += _visible_len(collapsed)
        self.inline.append(collapsed if self.code_depth else escape_md(collapsed))

    def finish(self) -> None:
        while self.stack:
            frame = self.stack.pop()
            if frame.skip:
                self.skip_depth -= 1
                continue
            self._close(frame)
        self.closed = True

    def markdown(self) -> str:
        parts: List[str] = []
        previous_list = False
        previous_quote = 0
        for text, is_list_item, quote_depth in self.blocks:
            if parts:
                if is_list_item and previous_list:
                    parts.append("\n")
                elif quote_depth and previous_quote:
                    # Keep consecutive quoted paragraphs inside one blockquote.
                    parts.append("\n" + " ".join([">"] * min(quote_depth, previous_quote)) + "\n")
                else:
                    parts.append("\n\n")
            parts.append(text)
            previous_list = is_list_item
            previous_quote = quote_depth
        body = "".join(parts).strip()
        return body + "\n" if body else ""

    # -- element handling -------------------------------------------------

    def _open(self, frame: _Frame, attrs: Dict[str, str]) -> None:
        tag = frame.tag
        if self.pre_depth:
            if tag == "pre":
                self.pre_depth += 1
                frame.kind = "pre"
            return
        if self.table_depth:
            if tag == "table":
                self.table_depth += 1
                frame.kind = "table"
                return
            if tag == "tr" and self.table_depth == 1:
                self.table_row = []
                frame.kind = "tr"
                return
            if tag in {"td", "th"} and self.table_depth == 1:
                frame.kind = "cell"
                frame.marker = len(self.inline)
                return
            if (
                tag in BLOCK_TAGS
                or tag in HEADING_TAGS
                or tag in {"ul", "ol", "li", "blockquote", "pre", "tr", "td", "th"}
            ):
                self.inline.append(" ")
                frame.kind = "soft"
                return
        if tag in HEADING_TAGS:
            self.flush()
            self.heading = HEADING_TAGS[tag]
            frame.kind = "heading"
        elif tag in {"ul", "ol"}:
            self.flush()
            start = attrs.get("start", "")
            counter = int(start) - 1 if start.isdigit() and int(start) < 10 ** 6 else 0
            self.lists.append(
                {"ordered": tag == "ol", "counter": counter, "prefix": "", "cont": "", "pending": False}
            )
            frame.kind = "list"
        elif tag == "li" and self.lists:
            self.flush()
            current = self.lists[-1]
            current["counter"] += 1
            marker = f"{current['counter']}. " if current["ordered"] else "- "
            indent = self.lists[-2]["cont"] if len(self.lists) > 1 else ""
            current["prefix"] = indent + marker
            current["cont"] = indent + " " * len(marker)
            current["pending"] = True
            frame.kind = "li"
        elif tag == "blockquote":
            self.flush()
            self.quote_depth += 1
            frame.kind = "quote"
        elif tag == "pre":
            self.flush()
            self.pre_depth = 1
            self.pre_buffer = []
            lang = attrs.get("data-lang", "").strip()
            self.pre_lang = lang if SAFE_LANG_RE.fullmatch(lang) else ""
            frame.kind = "pre"
        elif tag == "table":
            self.flush()
            self.table_depth = 1
            self.table_rows = []
            self.table_row = None
            frame.kind = "table"
        elif tag in BLOCK_TAGS or tag == "li":
            self.flush()
            frame.kind = "block"
        elif tag in {"strong", "b"}:
            frame.kind = "bold"
            if self.bold_depth == 0:
                frame.marker = len(self.inline)
            self.bold_depth += 1
        elif tag in {"em", "i"}:
            frame.kind = "italic"
            if self.italic_depth == 0:
                frame.marker = len(self.inline)
            self.italic_depth += 1
        elif tag in {"del", "s", "strike"}:
            frame.kind = "strike"
            if self.strike_depth == 0:
                frame.marker = len(self.inline)
            self.strike_depth += 1
        elif tag == "code":
            frame.kind = "code"
            if self.code_depth == 0:
                frame.marker = len(self.inline)
            self.code_depth += 1
        elif tag == "a":
            frame.kind = "link"
            href = safe_http_url(attrs.get("href"))
            if href and self.link_depth == 0:
                frame.marker = len(self.inline)
                frame.href = href
            self.link_depth += 1

    def _close(self, frame: _Frame) -> None:
        kind = frame.kind
        if kind == "pre":
            self.pre_depth -= 1
            if self.pre_depth == 0:
                self._emit_code()
            return
        if self.pre_depth:
            if frame.tag == "code" or frame.tag in BLOCK_TAGS or frame.tag in {"p", "li"}:
                if self.pre_buffer and not "".join(self.pre_buffer).endswith("\n"):
                    self.pre_buffer.append("\n")
            return
        if kind == "heading":
            self.flush()
            self.heading = None
        elif kind == "list":
            self.flush()
            if self.lists:
                self.lists.pop()
        elif kind == "li":
            self.flush()
            if self.lists:
                self.lists[-1]["pending"] = False
        elif kind == "quote":
            self.flush()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif kind == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self._emit_table()
        elif kind == "tr":
            if self.table_row is not None:
                self.table_rows.append(self.table_row)
            self.table_row = None
        elif kind == "cell":
            marker = frame.marker if frame.marker is not None else len(self.inline)
            cell = " ".join("".join(self.inline[marker:]).split())
            del self.inline[marker:]
            if self.table_row is None:
                self.table_row = []
            self.table_row.append(cell)
        elif kind == "soft":
            self.inline.append(" ")
        elif kind == "block":
            self.flush()
        elif kind == "bold":
            self.bold_depth -= 1
            self._wrap(frame, "**", "**")
        elif kind == "italic":
            self.italic_depth -= 1
            self._wrap(frame, "*", "*")
        elif kind == "strike":
            self.strike_depth -= 1
            self._wrap(frame, "~~", "~~")
        elif kind == "code":
            self.code_depth -= 1
            if frame.marker is not None:
                content = "".join(self.inline[frame.marker:])
                fence = "``" if "`" in content else "`"
                pad = " " if fence == "``" else ""
                self._wrap(frame, fence + pad, pad + fence)
        elif kind == "link":
            self.link_depth -= 1
            if frame.marker is not None and frame.href:
                self._wrap(frame, "[", f"]({_md_url(frame.href)})")

    def _void(self, tag: str, attrs: Dict[str, str]) -> None:
        if self.skip_depth:
            return
        if tag == "br":
            if self.pre_depth:
                self.pre_buffer.append("\n")
            elif self.table_depth:
                self.inline.append(" ")
            else:
                self.inline.append("\n")
        elif tag == "img":
            if self.pre_depth or _hidden(attrs):
                return
            src = safe_http_url(attrs.get("data-src")) or safe_http_url(attrs.get("src"))
            if not src:
                return
            if src not in self.images:
                self.images.append(src)
            alt = escape_md(" ".join(attrs.get("alt", "").split()))[:200]
            self.inline.append(f"![{alt}]({_md_url(src)})")
        elif tag == "hr":
            if self.pre_depth or self.table_depth:
                return
            self.flush()
            self._emit(["---"])

    def _video(self, tag: str, attrs: Dict[str, str]) -> None:
        info = video_info(tag, attrs)
        self.videos.append(info)
        label = info.get("title") or info.get("vid")
        note = f"[视频：{escape_md(label)}，未下载]" if label else "[视频，未下载]"
        # On its own line so following "(...)" text can never turn it into a link.
        self.inline.append(f"\n{note}\n")

    def _wrap(self, frame: _Frame, opener: str, closer: str) -> None:
        if frame.marker is None:
            return
        marker = min(frame.marker, len(self.inline))
        content = "".join(self.inline[marker:])
        del self.inline[marker:]
        stripped = content.strip()
        if not stripped:
            self.inline.append(content)
            return
        lead = content[: len(content) - len(content.lstrip())]
        trail = content[len(content.rstrip()):]
        self.inline.append(f"{lead}{opener}{stripped}{closer}{trail}")

    def _open_mark_frames(self) -> List[_Frame]:
        return [
            frame
            for frame in self.stack
            if not frame.skip
            and frame.marker is not None
            and frame.kind in {"bold", "italic", "strike", "code", "link"}
        ]

    def flush(self) -> None:
        if self.table_depth:
            return
        # Inline marks that span a block boundary are closed in this block and
        # reopened (marker reset) for the next one.
        open_marks = self._open_mark_frames()
        for frame in reversed(open_marks):
            if frame.kind == "link":
                self._wrap(frame, "[", f"]({_md_url(frame.href or '')})")
            elif frame.kind == "bold":
                self._wrap(frame, "**", "**")
            elif frame.kind == "italic":
                self._wrap(frame, "*", "*")
            elif frame.kind == "strike":
                self._wrap(frame, "~~", "~~")
            elif frame.kind == "code":
                self._wrap(frame, "`", "`")
        raw = "".join(self.inline)
        self.inline = []
        for frame in open_marks:
            frame.marker = 0
        lines = [" ".join(part.split()) for part in raw.split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            return
        if self.heading:
            self._emit(["#" * self.heading + " " + " ".join(lines)])
            return
        lines = [_escape_line_start(line) for line in lines]
        lines = [line + "  " for line in lines[:-1]] + lines[-1:]
        self._emit(lines)

    def _emit(self, lines: List[str]) -> None:
        list_item = False
        if self.lists and self.lists[-1]["prefix"]:
            current = self.lists[-1]
            if current["pending"]:
                lines = [current["prefix"] + lines[0]] + [current["cont"] + line for line in lines[1:]]
                current["pending"] = False
                list_item = True
            else:
                lines = [current["cont"] + line for line in lines]
        if self.quote_depth:
            quote = "> " * self.quote_depth
            lines = [(quote + line).rstrip() if not line.strip() else quote + line for line in lines]
        self.blocks.append(("\n".join(lines), list_item, self.quote_depth))

    def _emit_code(self) -> None:
        code = "".join(self.pre_buffer)
        self.pre_buffer = []
        code = code.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").strip("\n")
        if not code.strip():
            return
        longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest + 1)
        self._emit([fence + self.pre_lang, *code.split("\n"), fence])

    def _emit_table(self) -> None:
        rows = [row for row in self.table_rows if any(cell for cell in row)]
        self.table_rows = []
        self.table_row = None
        leftover = " ".join("".join(self.inline).split())
        self.inline = []
        if rows:
            width = max(len(row) for row in rows)
            padded = [row + [""] * (width - len(row)) for row in rows]
            lines = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
            lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
            self._emit(lines)
        if leftover:
            self._emit([_escape_line_start(leftover)])


# ---------------------------------------------------------------------------
# Page parsing


class WechatPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.builder: Optional[MarkdownBuilder] = None
        self.meta: Dict[str, str] = {}
        self.captured: Dict[str, str] = {}
        self._capture: Optional[List[Any]] = None
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: List[str] = []
        self._signal_parts: List[str] = []
        self._signal_len = 0

    @property
    def _in_content(self) -> bool:
        return self.builder is not None and not self.builder.closed

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._in_content:
            self.builder.start(tag, attrs)  # type: ignore[union-attr]
            return
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or "").lower()
            if key and key not in self.meta and "content" in attr_map:
                self.meta[key] = attr_map["content"]
            return
        if (
            self.builder is None
            and tag not in VOID_TAGS
            and attr_map.get("id") == "js_content"
        ):
            self.builder = MarkdownBuilder(tag)
            return
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in OUTSIDE_SKIP_TAGS:
            self._skip_depth = 1
            return
        if tag == "title":
            self._in_title = True
        if tag in VOID_TAGS:
            return
        if self._capture is None:
            element_id = attr_map.get("id", "")
            key = None
            if element_id in CAPTURE_IDS:
                key = element_id
            elif "profile_nickname" in attr_map.get("class", "").split():
                key = "profile_nickname"
            if key and key not in self.captured:
                self._capture = [key, tag, 1, []]
        elif tag == self._capture[1]:
            self._capture[2] += 1

    def handle_endtag(self, tag: str) -> None:
        if self._in_content:
            self.builder.end(tag)  # type: ignore[union-attr]
            return
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if self._capture is not None and tag == self._capture[1]:
            self._capture[2] -= 1
            if self._capture[2] == 0:
                key, _, _, parts = self._capture
                self.captured[key] = " ".join("".join(parts).split())
                self._capture = None

    def handle_data(self, data: str) -> None:
        if self._in_content:
            self.builder.text(data)  # type: ignore[union-attr]
            return
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._capture is not None:
            self._capture[3].append(data)
        if self._signal_len < MAX_SIGNAL_CHARS:
            self._signal_parts.append(data)
            self._signal_len += len(data)

    def close(self) -> None:
        super().close()
        if self.builder is not None and not self.builder.closed:
            self.builder.finish()

    @property
    def title_text(self) -> str:
        return " ".join("".join(self._title_parts).split())

    @property
    def signal_text(self) -> str:
        return " ".join("".join(self._signal_parts).split())


def decode_js_string(value: str) -> str:
    def replace(match: re.Match) -> str:
        escape = match.group(1)
        if escape[0] in {"x", "u"} and len(escape) > 1:
            return chr(int(escape[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(escape, escape)

    return re.sub(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|.)", replace, value)


def clean_meta_text(value: Optional[str], limit: int = 300) -> Optional[str]:
    if value is None:
        return None
    text = str(value).translate(INVISIBLE_CHARS)
    text = "".join(
        " " if unicodedata.category(ch) in {"Cc", "Zl", "Zp"} else ch for ch in text
    )
    text = " ".join(text.split())
    return text[:limit] or None


def first_text(*values: Optional[str]) -> Optional[str]:
    for value in values:
        cleaned = clean_meta_text(value)
        if cleaned:
            return cleaned
    return None


def js_string_var(html_text: str, name: str) -> Optional[str]:
    for pattern in JS_STRING_PATTERNS[name]:
        match = pattern.search(html_text)
        if match:
            return html.unescape(decode_js_string(match.group(1)))
    return None


def timestamp_iso(value: Any) -> Optional[str]:
    """UTC ISO time for a plausible Unix publish timestamp, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"\d{9,11}", value):
            return None
        value = int(value)
    if not EARLIEST_TIMESTAMP <= value <= time.time() + 2 * 86400:
        return None
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def extract_published_at(html_text: str, element_text: Optional[str]) -> Optional[str]:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(html_text)
        if match:
            published = timestamp_iso(match.group(1))
            if published:
                return published
    match = PUBLISH_DATE_PATTERN.search(html_text)
    if match:
        return match.group(1)
    candidate = (element_text or "").strip()
    if DATE_ONLY_RE.fullmatch(candidate):
        return candidate
    return None


def page_article_url(*candidates: Optional[str]) -> Optional[str]:
    for raw in candidates:
        if not raw:
            continue
        value = html.unescape(raw.strip())
        if value.lower().startswith("http://"):
            value = "https://" + value[len("http://"):]
        try:
            return normalize_article_url(value).canonical_url
        except ArchiveError:
            continue
    return None


# -- image posts ------------------------------------------------------------

_JS_SPACE_RE = re.compile(r"\s*")
_JS_KEY_RE = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:")
_JS_STRING_RES = {
    "'": re.compile(r"'((?:[^'\\\n]|\\.)*)'"),
    '"': re.compile(r'"((?:[^"\\\n]|\\.)*)"'),
}
_JS_NUMBER_RE = re.compile(r"-?\d{1,20}(?:\.\d{1,20})?")
_JS_WORD_RE = re.compile(r"(true|false|null|undefined)(?![A-Za-z0-9_$])")
_JS_TIMES_ONE_RE = re.compile(r"\s*\*\s*1(?![0-9.])")
_JS_WORDS = {"true": True, "false": False, "null": None, "undefined": None}


def _join_surrogates(text: str) -> str:
    """Combine \\uD83D\\uDE00-style escape pairs; lone halves become U+FFFD."""
    if not any("\ud800" <= ch <= "\udfff" for ch in text):
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _js_number(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def read_js_literal(text: str, pos: int, depth: int = 0) -> Tuple[Any, int]:
    """Read one value of the object literal WeChat's templates print.

    Only that subset is accepted: objects with bare or quoted keys, arrays,
    quoted strings with JS escapes, numbers, ``'123' * 1`` and
    true/false/null/undefined, trailing commas allowed. Anything else raises
    ValueError; nothing is ever evaluated.
    """
    if depth > MAX_JS_LITERAL_DEPTH:
        raise ValueError("literal nested too deeply")
    pos = _JS_SPACE_RE.match(text, pos).end()
    char = text[pos:pos + 1]
    if char in {"{", "["}:
        closing = "}" if char == "{" else "]"
        container: Any = {} if char == "{" else []
        pos += 1
        while True:
            pos = _JS_SPACE_RE.match(text, pos).end()
            if text[pos:pos + 1] == closing:
                return container, pos + 1
            key = None
            if char == "{":
                bare = _JS_KEY_RE.match(text, pos)
                quoted = _JS_STRING_RES.get(text[pos:pos + 1])
                quoted_match = quoted.match(text, pos) if quoted else None
                if bare:
                    key, pos = bare.group(1), bare.end()
                elif quoted_match:
                    key = decode_js_string(quoted_match.group(1))
                    pos = _JS_SPACE_RE.match(text, quoted_match.end()).end()
                    if text[pos:pos + 1] != ":":
                        raise ValueError("expected ':'")
                    pos += 1
                else:
                    raise ValueError("unexpected object key")
            value, pos = read_js_literal(text, pos, depth + 1)
            if isinstance(container, dict):
                container.setdefault(key, value)
            else:
                container.append(value)
            pos = _JS_SPACE_RE.match(text, pos).end()
            separator = text[pos:pos + 1]
            if separator == ",":
                pos += 1
            elif separator != closing:
                raise ValueError("unexpected separator")
    string_re = _JS_STRING_RES.get(char)
    if string_re:
        match = string_re.match(text, pos)
        if not match:
            raise ValueError("unterminated string")
        value = _join_surrogates(decode_js_string(match.group(1)))
        times_one = _JS_TIMES_ONE_RE.match(text, match.end())
        if times_one:
            return _js_number(value.strip()), times_one.end()
        return value, match.end()
    match = _JS_NUMBER_RE.match(text, pos)
    if match:
        end = match.end()
        times_one = _JS_TIMES_ONE_RE.match(text, end)
        return _js_number(match.group(0)), times_one.end() if times_one else end
    match = _JS_WORD_RE.match(text, pos)
    if match:
        return _JS_WORDS[match.group(1)], match.end()
    raise ValueError("unsupported literal")


def cgi_data_fields(html_text: str) -> Dict[str, Any]:
    """Top-level fields of ``window.cgiDataNew``; {} when absent or unreadable."""
    match = CGI_DATA_RE.search(html_text)
    if not match:
        return {}
    block = html_text[match.end() - 1:match.end() - 1 + MAX_CGI_DATA_CHARS]
    try:
        value, _ = read_js_literal(block, 0)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    # One unexpected token elsewhere in the object must not lose the post:
    # read just the needed fields, each at its first occurrence.
    fields: Dict[str, Any] = {}
    for name in IMAGE_POST_FIELDS:
        found = re.search(rf"(?<![A-Za-z0-9_$]){name}\s*:\s*", block)
        if not found:
            continue
        try:
            fields[name], _ = read_js_literal(block, found.end())
        except ValueError:
            continue
    return fields


def page_item_show_type(html_text: str, fields: Dict[str, Any]) -> Optional[int]:
    value = fields.get("item_show_type")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    for pattern in ITEM_SHOW_TYPE_PATTERNS:
        match = pattern.search(html_text)
        if match:
            return int(match.group(1))
    return None


def image_post_picture(item: Any) -> Optional[str]:
    """Best image link of one picture_page_info_list entry.

    The entry's own ``cdn_url`` is the full-size (``/0``) picture without the
    watermark; the watermarked copy is only a fallback. ``original_info`` sits
    on a signed non-CDN host and is never used.
    """
    if not isinstance(item, dict):
        return None
    watermark = item.get("watermark_info")
    candidates = [item.get("cdn_url"), watermark.get("cdn_url") if isinstance(watermark, dict) else None]
    urls = [
        url
        for url in (safe_http_url(html.unescape(value)) for value in candidates if isinstance(value, str))
        if url
    ]
    for url in urls:
        if image_download_url(url):
            return url
    return urls[0] if urls else None


class _FragmentParser(HTMLParser):
    """Feed an HTML fragment (an image post's description) into a MarkdownBuilder."""

    def __init__(self, builder: MarkdownBuilder) -> None:
        super().__init__(convert_charrefs=True)
        self.builder = builder

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if not self.builder.closed:
            self.builder.start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self.builder.closed:
            self.builder.end(tag)

    def handle_data(self, data: str) -> None:
        if not self.builder.closed:
            self.builder.text(data)


def image_post_text(text: str) -> MarkdownBuilder:
    """Description text (plain lines plus inline <a> tags) as Markdown paragraphs.

    Blank lines separate paragraphs; a single newline is a line break.
    """
    builder = MarkdownBuilder(IMAGE_POST_TEXT_ROOT)
    parser = _FragmentParser(builder)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        for paragraph in re.split(r"\n\s*\n", normalized):
            if paragraph.strip():
                parser.feed("<p>" + paragraph.replace("\n", "<br>") + "</p>")
        parser.close()
    except Exception:  # html.parser is lenient; anything else is an unusable page.
        raise FetchError("parse_failed") from None
    builder.finish()
    return builder


def extract_image_post(html_text: str, og_description: Optional[str]) -> Optional[ImagePost]:
    """Text and pictures of an image post page, or None when it is not one."""
    fields = cgi_data_fields(html_text)
    show_type = page_item_show_type(html_text, fields)
    raw_items = fields.get("picture_page_info_list")
    pictures = [
        url
        for url in (image_post_picture(item) for item in (raw_items if isinstance(raw_items, list) else []))
        if url
    ]
    if show_type != IMAGE_POST_SHOW_TYPE and not (show_type is None and pictures):
        return None
    texts = [fields.get("content_noencode")]
    # ``desc`` and og:description hold the same text HTML-escaped once more;
    # og:description also keeps the JS escapes (\x0a, \x26quot;) verbatim.
    if isinstance(fields.get("desc"), str):
        texts.append(html.unescape(fields["desc"]))
    if og_description:
        texts.append(html.unescape(_join_surrogates(decode_js_string(og_description))))
    text = next((value for value in texts if isinstance(value, str) and value.strip()), "")
    builder = image_post_text(text)
    images: List[str] = []
    for src in pictures + builder.images:
        if src not in images:
            images.append(src)
    blocks = [f"![图 {index}]({_md_url(src)})" for index, src in enumerate(pictures, 1)]
    description = builder.markdown().strip()
    if description:
        blocks.append(description)
    markdown = "\n\n".join(blocks)
    published_at = None
    for value in (fields.get("ori_create_time"), fields.get("ori_send_time")):
        published_at = timestamp_iso(value)
        if published_at:
            break
    if not published_at:
        match = WINDOW_CT_PATTERN.search(html_text)
        published_at = timestamp_iso(match.group(1)) if match else None
    nick_name, author = fields.get("nick_name"), fields.get("author")
    return ImagePost(
        markdown=markdown + "\n" if markdown else "",
        images=images,
        text_chars=builder.text_chars,
        videos=list(builder.videos),
        account_name=first_text(nick_name if isinstance(nick_name, str) else None),
        author=first_text(author if isinstance(author, str) else None),
        published_at=published_at,
    )


def extract_page(html_text: str) -> PageExtraction:
    parser = WechatPageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # html.parser is lenient; anything else is an unusable page.
        raise FetchError("parse_failed") from None
    builder = parser.builder
    meta = parser.meta
    html_title = parser.title_text
    title = first_text(
        meta.get("og:title"),
        meta.get("twitter:title"),
        parser.captured.get("activity-name"),
        js_string_var(html_text, "msg_title"),
        None if html_title in GENERIC_PAGE_TITLES else html_title,
    )
    page = PageExtraction(
        content_found=builder is not None,
        markdown=builder.markdown() if builder else "",
        images=list(builder.images) if builder else [],
        text_chars=builder.text_chars if builder else 0,
        title=title,
        account_name=first_text(
            parser.captured.get("js_name"),
            parser.captured.get("profile_nickname"),
            js_string_var(html_text, "nickname"),
        ),
        author=first_text(meta.get("og:article:author"), meta.get("author")),
        published_at=extract_published_at(html_text, parser.captured.get("publish_time")),
        page_canonical_url=page_article_url(
            meta.get("og:url"), js_string_var(html_text, "msg_link")
        ),
        signal_text=f"{html_title} {parser.signal_text}",
        videos=list(builder.videos) if builder else [],
    )
    if builder is not None:
        return page
    # Only a page without #js_content is ever read as an image post, so
    # regular articles keep exactly the extraction above.
    post = extract_image_post(html_text, meta.get("og:description"))
    if post is None:
        return page
    page.content_type = "image_post"
    page.markdown = post.markdown
    page.images = post.images
    page.text_chars = post.text_chars
    page.videos = post.videos
    page.account_name = page.account_name or post.account_name
    page.author = page.author or post.author
    page.published_at = page.published_at or post.published_at
    return page


def page_failure(page: PageExtraction, html_text: str) -> Optional[str]:
    """Return a failure reason, or None when the page holds a real article body."""
    if page.content_found and (
        page.text_chars >= MIN_BODY_TEXT_CHARS or page.images or page.videos
    ):
        return None
    # An image post is mostly pictures with a short caption: any text or
    # picture counts; only a post with neither falls through to missing_content.
    if page.content_type == "image_post" and (page.text_chars or page.images or page.videos):
        return None
    for reason, markers in PAGE_STATE_MARKERS:
        if any(marker in page.signal_text for marker in markers):
            return reason
    if "appmsgcaptcha" in html_text:
        return "blocked_or_verification"
    return "content_too_short" if page.content_found else "missing_content"


def migration_target(html_text: str) -> Optional[str]:
    """Normalized new article URL named by a migration interstitial, if any.

    Only a URL that normalizes to an exact article link is reported; it is
    never fetched here.
    """
    for pattern in TRANSFER_TARGET_PATTERNS:
        match = pattern.search(html_text)
        if not match:
            continue
        raw = html.unescape(match.group(1)).strip()
        if raw.lower().startswith("http://"):
            raw = "https://" + raw[len("http://"):]
        try:
            return normalize_article_url(raw).canonical_url
        except ArchiveError:
            return None
    return None


def failure_error(reason: str, html_text: str, http_status: Optional[int]) -> FetchError:
    details: Dict[str, Any] = {}
    if reason == "account_migrated":
        details["migrated_to"] = migration_target(html_text)
    return FetchError(reason, http_status=http_status, details=details)


def relay_offline_page(text: str, page: PageExtraction) -> bool:
    if page.content_found:
        return False
    head = text[:20000]
    return bool(RELAY_OFFLINE_TITLE_RE.search(head) or RELAY_ROOT_CANONICAL_RE.search(head))


def verify_identity(article: KnownArticle, page: PageExtraction) -> None:
    if not page.page_canonical_url:
        return
    try:
        reported = normalize_article_url(page.page_canonical_url)
    except ArchiveError:
        return
    if reported.form == article.form and reported.identity != article.identity:
        raise FetchError("target_mismatch")


# ---------------------------------------------------------------------------
# Fetch orchestration


def fetch_direct(
    article: KnownArticle, *, timeout: int, throttle: Throttle
) -> Tuple[str, PageExtraction]:
    response = throttled_get(throttle, DIRECT_OPENER, article.canonical_url, DIRECT_HEADERS, timeout)
    if response.status != 200:
        raise FetchError("http_error", f"HTTP {response.status}", http_status=response.status)
    content_type = response.content_type.lower()
    if content_type and "html" not in content_type:
        raise FetchError("unexpected_content_type", http_status=response.status)
    html_text = decode_body(response)
    page = extract_page(html_text)
    reason = page_failure(page, html_text)
    if reason:
        raise failure_error(reason, html_text, response.status)
    verify_identity(article, page)
    return html_text, page


def _json_strings(value: Any, depth: int = 0) -> List[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in list(value.items())[:50] for text in [str(key), *_json_strings(item, depth + 1)]]
    if isinstance(value, list):
        return [text for item in value[:50] for text in _json_strings(item, depth + 1)]
    return []


def classify_relay_json(text: str) -> str:
    # Decoded strings also catch Chinese messages sent as JSON unicode escapes.
    haystack = text[:8192]
    try:
        haystack += " " + " ".join(_json_strings(json.loads(text)))[:8192]
    except ValueError:
        pass
    return (
        "relay_membership_required"
        if RELAY_MEMBERSHIP_RE.search(haystack)
        else "relay_error_json"
    )


def fetch_relay(
    article: KnownArticle, *, timeout: int, throttle: Throttle
) -> Tuple[str, PageExtraction]:
    # Only the normalized public article URL is sent to the relay.
    query = urllib.parse.urlencode({"url": article.canonical_url, "format": "html"})
    try:
        response = throttled_get(
            throttle, RELAY_OPENER, f"{RELAY_ENDPOINT}?{query}", RELAY_HEADERS, timeout
        )
    except FetchError as exc:
        if exc.reason in RELAY_TRANSPORT_REASONS:
            raise FetchError(f"relay_{exc.reason}", http_status=exc.http_status) from None
        raise
    if response.status != 200:
        raise FetchError("relay_http_error", http_status=response.status)
    text = decode_body(response)
    head = text.lstrip("\ufeff \t\r\n")
    if "json" in response.content_type.lower() or head[:1] in {"{", "["}:
        # The JSON body is classified in memory and never persisted.
        raise FetchError(classify_relay_json(head), http_status=response.status)
    if "<" not in head[:4096]:
        raise FetchError("relay_unexpected_content", http_status=response.status)
    page = extract_page(text)
    if relay_offline_page(text, page):
        raise FetchError("relay_offline", http_status=response.status)
    reason = page_failure(page, text)
    if reason:
        raise failure_error(reason, text, response.status)
    verify_identity(article, page)
    return text, page


def _attempt(backend: str, error: Optional[FetchError] = None) -> Dict[str, Any]:
    if error is None:
        return {"backend": backend, "status": "success", "reason": None, "http_status": None}
    return {
        "backend": backend,
        "status": "failed",
        "reason": error.reason,
        "http_status": error.http_status,
    }


def fetch_article(
    article: KnownArticle, *, timeout: int, throttle: Throttle, relay_enabled: bool
) -> FetchOutcome:
    attempts: List[Dict[str, Any]] = []
    try:
        html_text, page = fetch_direct(article, timeout=timeout, throttle=throttle)
        attempts.append(_attempt(DIRECT_BACKEND))
        return FetchOutcome(True, DIRECT_BACKEND, page, html_text, None, attempts)
    except FetchError as exc:
        direct_reason = exc.reason
        direct_details = exc.details
        attempts.append(_attempt(DIRECT_BACKEND, exc))
    if direct_reason in AUTHORITATIVE_REASONS or not relay_enabled:
        return FetchOutcome(False, None, None, None, direct_reason, attempts, direct_details)
    try:
        html_text, page = fetch_relay(article, timeout=timeout, throttle=throttle)
        attempts.append(_attempt(RELAY_BACKEND))
        return FetchOutcome(True, RELAY_BACKEND, page, html_text, None, attempts)
    except FetchError as exc:
        attempts.append(_attempt(RELAY_BACKEND, exc))
        if exc.reason in AUTHORITATIVE_REASONS:
            return FetchOutcome(False, None, None, None, exc.reason, attempts, exc.details)
        return FetchOutcome(False, None, None, None, direct_reason, attempts, direct_details)


def relay_contacted(attempts: Sequence[Dict[str, Any]]) -> bool:
    return any(item.get("backend") == RELAY_BACKEND for item in attempts)


def relay_reported_offline(attempts: Sequence[Dict[str, Any]]) -> bool:
    return any(
        item.get("backend") == RELAY_BACKEND and item.get("reason") == "relay_offline"
        for item in attempts
    )


# ---------------------------------------------------------------------------
# Output


def safe_title(title: Optional[str], fallback: str = "article") -> str:
    text = unicodedata.normalize("NFC", title or "").translate(INVISIBLE_CHARS)
    text = "".join(
        " " if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} else ch for ch in text
    )
    text = re.sub(r'[\\/:*?"<>|#^\[\]]', "_", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"_{2,}", "_", text)
    text = text.strip(" ._-")
    text = text[:TITLE_MAX_CHARS]
    encoded = text.encode("utf-8")[:TITLE_MAX_BYTES]
    text = encoded.decode("utf-8", errors="ignore").strip(" ._-")
    if not text or set(text) <= {"."}:
        return fallback
    return text


def default_output_root() -> Path:
    return Path.home() / "Downloads" / "wechat-mp-known"


def allocate_run_dir(requested: Optional[str]) -> Path:
    """Exclusively create a new run directory; never reuse an existing one."""
    if requested:
        base = Path(requested).expanduser()
    else:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        base = default_output_root() / f"{stamp}-{secrets.token_hex(3)}"
    base = base.absolute()
    try:
        base.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError("output_dir_unavailable", "无法创建输出目录的上级目录") from exc
    candidate = base
    for counter in range(1, 1001):
        try:
            candidate.mkdir(mode=0o700)
            return candidate.resolve()
        except FileExistsError:
            # Same numbering as the other executors: the first rerun is -run-2.
            candidate = base.with_name(f"{base.name}-run-{counter + 1}")
        except OSError as exc:
            raise ArchiveError("output_dir_unavailable", "无法创建新的 run 目录") from exc
    raise ArchiveError("output_dir_unavailable", "找不到不冲突的 run 目录名")


def write_text_exclusive(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def write_json_exclusive(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_bytes_via_part(path: Path, data: bytes) -> None:
    """Write ``<name>.part`` exclusively, then rename; an existing file is never replaced."""
    partial = path.with_name(f"{path.name}.part")
    with partial.open("xb") as handle:
        handle.write(data)
    if path.exists():
        raise FileExistsError(str(path))
    partial.rename(path)


def localize_images(
    article_dir: Path, page: PageExtraction, *, timeout: int
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Download body images into ``article_dir/images``; one failure never stops the rest.

    Returns {original src: relative local path} and one record per image.
    """
    mapping: Dict[str, str] = {}
    records: List[Dict[str, Any]] = []
    images_dir = article_dir / "images"
    for index, src in enumerate(page.images, 1):
        record: Dict[str, Any] = {
            "index": index,
            "source_url": recorded_image_url(src),
            "path": None,
            "status": "failed",
            "reason": None,
        }
        records.append(record)
        if index > MAX_IMAGES_PER_ARTICLE:
            record.update(status="skipped", reason="image_limit_exceeded")
            continue
        url = image_download_url(src)
        if url is None:
            record["reason"] = "image_host_not_allowed"
            continue
        try:
            content_type, body = fetch_image(url, timeout)
        except FetchError as exc:
            record["reason"] = exc.reason
            if exc.http_status is not None:
                record["http_status"] = exc.http_status
            continue
        extension = image_extension(content_type, body)
        if extension is None:
            record["reason"] = "image_unexpected_type"
            continue
        relative = f"images/{index:03d}{extension}"
        try:
            images_dir.mkdir(mode=0o700, exist_ok=True)
            write_bytes_via_part(article_dir / relative, body)
        except OSError:
            record["reason"] = "image_write_failed"
            continue
        record.update(
            status="success",
            path=relative,
            content_type=content_type or None,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        mapping[src] = relative
    return mapping, records


def image_status(records: Sequence[Dict[str, Any]]) -> str:
    if not records:
        return "none"
    return "complete" if all(item["status"] == "success" for item in records) else "partial"


def failed_images(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"index": item["index"], "reason": item["reason"], "source_url": item["source_url"]}
        for item in records
        if item["status"] != "success"
    ]


def localized_markdown(page: PageExtraction, mapping: Dict[str, str]) -> str:
    """Point downloaded images at their local copy; others keep a sanitized remote link."""
    markdown = page.markdown
    for src in page.images:
        target = mapping.get(src) or recorded_image_url(src)
        if target != src:
            markdown = markdown.replace(f"]({_md_url(src)})", f"]({_md_url(target)})")
    return markdown


def render_article_document(
    page: PageExtraction,
    article: KnownArticle,
    backend_used: str,
    fetched_at: str,
    *,
    markdown: Optional[str] = None,
    local_image_count: Optional[int] = None,
) -> str:
    front_matter: List[Tuple[str, Any]] = [
        ("title", page.title),
        ("account_name", page.account_name),
        ("author", page.author),
        ("published_at", page.published_at),
        ("canonical_url", article.canonical_url),
        ("route_backend", ROUTE_BACKEND),
        ("backend_used", backend_used),
        ("fetched_at", fetched_at),
        ("content_type", page.content_type),
        ("image_count", len(page.images)),
    ]
    if local_image_count is not None:
        front_matter.append(("image_local_count", local_image_count))
    front_matter.append(("video_count", len(page.videos)))
    lines = ["---"]
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front_matter)
    lines.extend(["---", ""])
    if page.title:
        lines.extend([f"# {escape_md(page.title)}", ""])
    lines.append((page.markdown if markdown is None else markdown).strip())
    return "\n".join(lines).rstrip() + "\n"


def article_metadata(
    article: KnownArticle,
    page: PageExtraction,
    backend_used: str,
    fetched_at: str,
    image_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema_version": "wechat-known-url-article/v1",
        "title": page.title,
        "account_name": page.account_name,
        "author": page.author,
        "published_at": page.published_at,
        "canonical_url": article.canonical_url,
        "page_canonical_url": page.page_canonical_url,
        "route_backend": ROUTE_BACKEND,
        "backend_used": backend_used,
        "fetched_at": fetched_at,
        "content_type": page.content_type,
        "text_chars": page.text_chars,
        "image_count": len(page.images),
        "image_status": image_status(image_records),
        "images": image_records,
        "video_count": len(page.videos),
        "videos": list(page.videos),
        "videos_downloaded": False,
    }


def write_article_files(
    articles_dir: Path,
    position: int,
    article: KnownArticle,
    page: PageExtraction,
    backend_used: str,
    html_text: Optional[str],
    fetched_at: str,
    save_html: bool,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Write one article into its own folder: Markdown, metadata.json, images/ and optional HTML."""
    stem = f"{position:03d}-{safe_title(page.title)}"
    article_dir = articles_dir / stem
    article_dir.mkdir(mode=0o700)
    mapping, image_records = localize_images(article_dir, page, timeout=timeout)
    markdown_path = article_dir / f"{stem}.md"
    write_text_exclusive(
        markdown_path,
        render_article_document(
            page,
            article,
            backend_used,
            fetched_at,
            markdown=localized_markdown(page, mapping),
            local_image_count=len(mapping),
        ),
    )
    metadata_path = article_dir / "metadata.json"
    write_json_exclusive(
        metadata_path, article_metadata(article, page, backend_used, fetched_at, image_records)
    )
    paths = [str(markdown_path), str(metadata_path)]
    if save_html and html_text is not None:
        html_path = article_dir / f"{stem}.html"
        write_text_exclusive(html_path, html_text)
        paths.append(str(html_path))
    return paths, image_records


# ---------------------------------------------------------------------------
# Commands


def article_payload(article: KnownArticle, page: PageExtraction) -> Dict[str, Any]:
    return {
        "title": page.title,
        "account_name": page.account_name,
        "author": page.author,
        "published_at": page.published_at,
        "canonical_url": article.canonical_url,
        "page_canonical_url": page.page_canonical_url,
        "content_type": page.content_type,
        "markdown": page.markdown,
        "images": [recorded_image_url(src) for src in page.images],
        "image_count": len(page.images),
        "images_downloaded": False,
        "video_count": len(page.videos),
        "videos": list(page.videos),
        "text_chars": page.text_chars,
    }


def run_read(args: argparse.Namespace) -> Dict[str, Any]:
    article = normalize_article_url(args.url)
    outcome = fetch_article(
        article,
        timeout=args.timeout,
        throttle=Throttle(MIN_INTERVAL),
        relay_enabled=args.relay_fallback,
    )
    result: Dict[str, Any] = {
        "ok": outcome.ok,
        "status": "success" if outcome.ok else "failed",
        "platform": "wechat",
        "operation": "read",
        "route_backend": ROUTE_BACKEND,
        "backend_used": outcome.backend_used,
        "authentication": "anonymous",
        "discovery_performed": False,
        "files_written": False,
        "relay_contacted": relay_contacted(outcome.attempts),
        "canonical_url": article.canonical_url,
        "sanitization": {"dropped_query_param_count": article.dropped_param_count},
        "attempts": outcome.attempts,
        "fetched_at": now_iso(),
    }
    if outcome.ok and outcome.page is not None:
        result["article"] = article_payload(article, outcome.page)
    else:
        result["reason"] = outcome.reason
        result["error"] = REASON_MESSAGES.get(outcome.reason or "", outcome.reason)
        if outcome.reason == "account_migrated":
            result["migrated_to"] = outcome.details.get("migrated_to")
        if relay_reported_offline(outcome.attempts):
            result["relay_offline_detected"] = True
    return result


def collect_inputs(args: argparse.Namespace) -> Tuple[str, List[InputItem]]:
    if not args.url_file:
        # A single positional URL must be valid before any side effect.
        return "known_urls", [InputItem("input:1", normalize_article_url(args.url), None)]

    path = Path(args.url_file).expanduser()
    try:
        if not path.is_file():
            raise ArchiveError("url_file_unreadable", "URL 文件不存在或不是普通文件")
        if path.stat().st_size > MAX_URL_FILE_BYTES:
            raise ArchiveError("url_file_too_large", "URL 文件超过 1 MiB")
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArchiveError("url_file_unreadable", "URL 文件不是 UTF-8 文本") from exc
    except OSError as exc:
        raise ArchiveError("url_file_unreadable", "URL 文件无法读取") from exc

    lines = [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), 1)
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise ArchiveError("no_urls", "URL 文件里没有任何链接行")
    if len(lines) > args.limit:
        raise ArchiveError(
            "limit_exceeded",
            f"URL 文件有 {len(lines)} 条链接，超过 --limit {args.limit}（上限 {MAX_LIMIT}）；请拆分文件或调整 --limit",
        )
    items: List[InputItem] = []
    for number, value in lines:
        ref = f"{path.name}:{number}"
        try:
            items.append(InputItem(ref, normalize_article_url(value), None))
        except ArchiveError as exc:
            items.append(InputItem(ref, None, exc))
    return "url_file", items


def _row(
    *,
    item_ref: str,
    source_ref: str,
    status: str,
    fetched_at: str,
    article: Optional[KnownArticle] = None,
    outcome: Optional[FetchOutcome] = None,
    artifact_paths: Optional[List[str]] = None,
    reason: Optional[str] = None,
    image_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    page = outcome.page if outcome and outcome.ok else None
    row: Dict[str, Any] = {
        "item_ref": item_ref,
        "platform": "wechat",
        "source_url_ref": source_ref,
        "requested_actions": ["download"],
        "status": status,
        "artifact_paths": artifact_paths or [],
        "route_backend": ROUTE_BACKEND,
        "backend_used": outcome.backend_used if outcome else None,
        "canonical_url": article.canonical_url if article else None,
        "title": page.title if page else None,
        "account_name": page.account_name if page else None,
        "author": page.author if page else None,
        "published_at": page.published_at if page else None,
        "image_urls": [recorded_image_url(src) for src in page.images] if page else [],
        "reason": reason,
        "attempts": outcome.attempts if outcome else [],
        "fetched_at": fetched_at,
    }
    if image_records is not None:
        row["image_status"] = image_status(image_records)
        row["image_local_count"] = sum(item["status"] == "success" for item in image_records)
        row["failed_images"] = failed_images(image_records)
    if page is not None:
        row["content_type"] = page.content_type
        row["video_count"] = len(page.videos)
        row["videos"] = list(page.videos)
    if reason == "account_migrated" and outcome is not None:
        row["migrated_to"] = outcome.details.get("migrated_to")
    return row


def run_download(args: argparse.Namespace) -> Dict[str, Any]:
    input_kind, items = collect_inputs(args)
    started_at = now_iso()
    run_dir = allocate_run_dir(args.output_dir)
    articles_dir = run_dir / "wechat"
    articles_dir.mkdir(mode=0o700)

    throttle = Throttle(args.interval)
    relay_enabled = bool(args.relay_fallback)
    rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    consecutive_blocks = 0
    stopped_early = False
    any_relay = False
    relay_offline = False

    for position, item in enumerate(items, 1):
        item_ref = f"wechat:{position:03d}"
        fetched_at = now_iso()
        if item.error is not None or item.article is None:
            reason = item.error.reason if item.error else "invalid_url"
            status = "unsupported" if reason == "unsupported_url" else "failed"
            rows.append(
                _row(item_ref=item_ref, source_ref=item.ref, status=status, fetched_at=fetched_at, reason=reason)
            )
            failures.append(
                {"item_ref": item_ref, "source_url_ref": item.ref, "stage": "validate", "reason": reason}
            )
            continue
        article = item.article
        if article.canonical_url in seen:
            rows.append(
                _row(
                    item_ref=item_ref,
                    source_ref=item.ref,
                    status="skipped",
                    fetched_at=fetched_at,
                    article=article,
                    reason="duplicate_input",
                )
            )
            skipped.append(
                {
                    "item_ref": item_ref,
                    "source_url_ref": item.ref,
                    "reason": "duplicate_input",
                    "duplicate_of": seen[article.canonical_url],
                }
            )
            continue
        seen[article.canonical_url] = item_ref
        if stopped_early:
            reason = "stopped_after_repeated_blocking"
            rows.append(
                _row(
                    item_ref=item_ref,
                    source_ref=item.ref,
                    status="skipped",
                    fetched_at=fetched_at,
                    article=article,
                    reason=reason,
                )
            )
            skipped.append({"item_ref": item_ref, "source_url_ref": item.ref, "reason": reason})
            continue

        outcome = fetch_article(
            article, timeout=args.timeout, throttle=throttle, relay_enabled=relay_enabled
        )
        any_relay = any_relay or relay_contacted(outcome.attempts)
        if relay_reported_offline(outcome.attempts):
            # A shut-down relay answers every URL with the same notice; stop sending it URLs.
            relay_offline = True
            relay_enabled = False
        fetched_at = now_iso()
        if outcome.ok and outcome.page is not None and outcome.backend_used:
            consecutive_blocks = 0
            try:
                paths, image_records = write_article_files(
                    articles_dir,
                    position,
                    article,
                    outcome.page,
                    outcome.backend_used,
                    outcome.html_text,
                    fetched_at,
                    args.save_html,
                    timeout=args.timeout,
                )
            except OSError:
                rows.append(
                    _row(
                        item_ref=item_ref,
                        source_ref=item.ref,
                        status="failed",
                        fetched_at=fetched_at,
                        article=article,
                        outcome=outcome,
                        reason="write_failed",
                    )
                )
                failures.append(
                    {"item_ref": item_ref, "source_url_ref": item.ref, "stage": "write", "reason": "write_failed"}
                )
                continue
            rows.append(
                _row(
                    item_ref=item_ref,
                    source_ref=item.ref,
                    status="success",
                    fetched_at=fetched_at,
                    article=article,
                    outcome=outcome,
                    artifact_paths=paths,
                    image_records=image_records,
                )
            )
            missing = failed_images(image_records)
            if missing:
                # The article itself succeeded; the run is partial and lists the images.
                failures.append(
                    {
                        "item_ref": item_ref,
                        "source_url_ref": item.ref,
                        "stage": "images",
                        "reason": "image_download_partial",
                        "failed_images": missing,
                    }
                )
            continue

        rows.append(
            _row(
                item_ref=item_ref,
                source_ref=item.ref,
                status="failed",
                fetched_at=fetched_at,
                article=article,
                outcome=outcome,
                reason=outcome.reason,
            )
        )
        failure: Dict[str, Any] = {
            "item_ref": item_ref,
            "source_url_ref": item.ref,
            "stage": "fetch",
            "reason": outcome.reason,
            "attempts": outcome.attempts,
        }
        if outcome.reason == "account_migrated":
            failure["migrated_to"] = outcome.details.get("migrated_to")
        failures.append(failure)
        consecutive_blocks = consecutive_blocks + 1 if outcome.reason in BLOCKING_REASONS else 0
        if consecutive_blocks >= BLOCK_STOP_THRESHOLD:
            stopped_early = True

    return write_run_artifacts(
        run_dir=run_dir,
        articles_dir=articles_dir,
        input_kind=input_kind,
        rows=rows,
        failures=failures,
        skipped=skipped,
        args=args,
        relay_was_contacted=any_relay,
        stopped_early=stopped_early,
        started_at=started_at,
        relay_offline_detected=relay_offline,
    )


def write_run_artifacts(
    *,
    run_dir: Path,
    articles_dir: Path,
    input_kind: str,
    rows: List[Dict[str, Any]],
    failures: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    args: argparse.Namespace,
    relay_was_contacted: bool,
    stopped_early: bool,
    started_at: str,
    relay_offline_detected: bool = False,
) -> Dict[str, Any]:
    counts = {
        "input": len(rows),
        "success": sum(1 for row in rows if row["status"] == "success"),
        "failed": sum(1 for row in rows if row["status"] == "failed"),
        "skipped": sum(1 for row in rows if row["status"] == "skipped"),
        "unsupported": sum(1 for row in rows if row["status"] == "unsupported"),
    }
    image_rows = [row for row in rows if "image_status" in row]
    image_counts = {
        "total": sum(row["image_local_count"] + len(row["failed_images"]) for row in image_rows),
        "downloaded": sum(row["image_local_count"] for row in image_rows),
        "failed": sum(len(row["failed_images"]) for row in image_rows),
    }
    if counts["success"] == counts["input"] and not image_counts["failed"]:
        status = "completed"
    elif counts["success"]:
        status = "partial"
    else:
        status = "failed"
    artifact_paths = [path for row in rows for path in row["artifact_paths"]]
    scope = {"platforms": ["wechat"], "input_kind": input_kind, "discovery_performed": False}
    authorization = {
        "private_read_authorized_this_turn": False,
        "download_authorized_this_turn": True,
        "authorization_not_transferable": True,
    }

    manifest_path = run_dir / "archive-manifest.jsonl"
    with manifest_path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    failures_path = run_dir / "failures.json"
    write_json_exclusive(failures_path, failures)
    summary_path = run_dir / "run-summary.json"
    write_json_exclusive(
        summary_path,
        {
            "schema_version": "wechat-known-url-run/v1",
            "operation": "download",
            "status": status,
            "scope": scope,
            "route_backend": ROUTE_BACKEND,
            "authorization": authorization,
            "settings": {
                "limit": args.limit,
                "interval_seconds": args.interval,
                "timeout_seconds": args.timeout,
                "relay_fallback_enabled": bool(args.relay_fallback),
                "relay_host": RELAY_HOST,
                "relay_format": "html",
                "save_html": bool(args.save_html),
                "cookies_sent": False,
                "image_hosts": sorted(IMAGE_HOSTS),
                "image_referer": IMAGE_HEADERS["Referer"],
                "max_image_bytes": MAX_IMAGE_BYTES,
                "max_images_per_article": MAX_IMAGES_PER_ARTICLE,
                "videos_downloaded": False,
            },
            "counts": counts,
            "image_counts": image_counts,
            "relay_contacted": relay_was_contacted,
            "relay_offline_detected": relay_offline_detected,
            "stopped_early": stopped_early,
            "artifacts": artifact_paths,
            "failures": failures,
            "skipped": skipped,
            "started_at": started_at,
            "finished_at": now_iso(),
        },
    )
    handoff_path = run_dir / "handoff.json"
    write_json_exclusive(
        handoff_path,
        {
            "handoff_version": HANDOFF_VERSION,
            "producer_skill": "yichen-content-archive",
            "operation": "content_archive",
            "scope": scope,
            "authorization": authorization,
            "artifacts": [
                {"kind": "archive_manifest", "path": str(manifest_path), "count": len(rows)},
                {"kind": "run_summary", "path": str(summary_path), "count": 1},
                {"kind": "failures", "path": str(failures_path), "count": len(failures)},
                {"kind": "article_markdown", "path": str(articles_dir), "count": counts["success"]},
            ],
            "counts": counts,
            "failures": [
                {"item_ref": item["item_ref"], "stage": item["stage"], "reason": item["reason"]}
                for item in failures
            ],
            "next_step": {"action": "none", "requires_explicit_user_request": True},
        },
    )
    return {
        "ok": status == "completed",
        "status": status,
        "platform": "wechat",
        "operation": "download",
        "route_backend": ROUTE_BACKEND,
        "discovery_performed": False,
        "counts": counts,
        "image_counts": image_counts,
        "relay_contacted": relay_was_contacted,
        "relay_offline_detected": relay_offline_detected,
        "stopped_early": stopped_early,
        "output_dir": str(run_dir),
        "articles_dir": str(articles_dir),
        "manifest_path": str(manifest_path),
        "summary_path": str(summary_path),
        "failures_path": str(failures_path),
        "handoff_path": str(handoff_path),
    }


class _JsonArgumentParser(argparse.ArgumentParser):
    """Turn argparse failures into a JSON rejection instead of bare stderr."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ArchiveError("invalid_arguments", sanitized_message(message))


def _add_fetch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数（{MIN_TIMEOUT}-{MAX_TIMEOUT}）",
    )
    relay = parser.add_mutually_exclusive_group()
    relay.add_argument(
        "--relay-fallback",
        action="store_true",
        help=(
            f"直读失败时显式回退 {RELAY_HOST}；该中转 2026-10-31 已停服，"
            "开启通常只会得到 relay_offline"
        ),
    )
    relay.add_argument(
        "--no-relay-fallback",
        action="store_true",
        help="兼容旧参数：不回退中转（现为默认行为）",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = _JsonArgumentParser(
        prog="wechat_known_url.py",
        description="匿名读取或下载用户已给出的公开公众号文章链接；不搜索、不枚举账号历史",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    read_parser = subparsers.add_parser("read", help="只读一篇文章，stdout 输出 JSON，不写盘")
    read_parser.add_argument("url")
    _add_fetch_arguments(read_parser)

    download_parser = subparsers.add_parser("download", help="下载已知文章到新的 run 目录")
    source = download_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("url", nargs="?")
    source.add_argument("--url-file", help="每行一个文章 URL；空行和 # 注释行忽略")
    download_parser.add_argument(
        "--output-dir", help="run 目录；已存在时自动改用不冲突的 -run-N"
    )
    download_parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"本次最多处理条数（1-{MAX_LIMIT}）"
    )
    download_parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"相邻请求最小间隔秒数（{MIN_INTERVAL:g}-{MAX_INTERVAL:g}）",
    )
    download_parser.add_argument(
        "--save-html", action="store_true", help="同时在文章目录保存原始 HTML"
    )
    _add_fetch_arguments(download_parser)

    args = parser.parse_args(argv)
    if not MIN_TIMEOUT <= args.timeout <= MAX_TIMEOUT:
        raise ArchiveError("invalid_arguments", f"--timeout 必须在 {MIN_TIMEOUT}-{MAX_TIMEOUT} 秒之间")
    if args.command == "download":
        if not 1 <= args.limit <= MAX_LIMIT:
            raise ArchiveError("limit_out_of_range", f"--limit 必须在 1-{MAX_LIMIT} 之间")
        if not MIN_INTERVAL <= args.interval <= MAX_INTERVAL:
            raise ArchiveError(
                "invalid_arguments",
                f"--interval 必须在 {MIN_INTERVAL:g}-{MAX_INTERVAL:g} 秒之间",
            )
    return args


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    command = None
    try:
        args = parse_args(argv)
        command = args.command
        result = run_read(args) if command == "read" else run_download(args)
    except ArchiveError as exc:
        emit(
            {
                "ok": False,
                "status": "rejected",
                "platform": "wechat",
                "operation": command,
                "reason": exc.reason,
                "error": sanitized_message(str(exc)),
                "side_effects_performed": False,
            }
        )
        return 2
    except Exception as exc:
        emit(
            {
                "ok": False,
                "status": "failed",
                "platform": "wechat",
                "operation": command,
                "reason": "unexpected_local_error",
                "error": type(exc).__name__,
            }
        )
        return 1
    emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
