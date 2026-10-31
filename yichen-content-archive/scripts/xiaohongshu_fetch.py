#!/usr/bin/env python3
"""
小红书笔记抓取器。

默认先匿名请求网页，从 window.__INITIAL_STATE__ 中提取结构化元数据；失败时回退网页 meta。
不写入飞书；只有显式传入 --use-cookie 时才允许从 XHS_COOKIE 读取登录态，而且仍先匿名尝试。
Cookie 只附在发往 https://*.xiaohongshu.com 的请求上，媒体 CDN 请求永远匿名。
只接受笔记详情链接与 xhslink 短链；短链先跟随跳转，再从最终详情 URL 提取 note_id。
媒体先写入 .part 并校验长度后才改名；任何媒体失败都以退出码 2 结束并列出失败项。
stdout 始终只输出一个 JSON 结果（含失败分类 reason），进度说明写到 stderr。
页面取不到笔记数据（跳到 /404/sec_*、风控、令牌过期等）时不创建输出目录；
保存的 HTML 快照会把 xsec_token 等令牌参数值替换为 REDACTED。
"""

import argparse
import html as html_lib
import json
import os
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:
    print(
        json.dumps(
            {
                "ok": False,
                "status": "failed",
                "platform": "xiaohongshu",
                "reason": "missing_dependency",
                "error": "缺少 requests；运行: pip install requests",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(1)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
NOTE_DETAIL_PATTERNS = (
    re.compile(r"/explore/(?P<id>[0-9a-zA-Z]+)/?"),
    re.compile(r"/discovery/item/(?P<id>[0-9a-zA-Z]+)/?"),
    # 从作者主页打开的笔记详情；裸 /user/profile/<uid> 仍是主页，不匹配。
    re.compile(r"/user/profile/[0-9a-zA-Z]+/(?P<id>[0-9a-zA-Z]+)/?"),
    # 网页搜索结果里打开的单篇笔记（统一搜索交接的就是这种链接，带 xsec_token）；
    # 只认 24 位十六进制笔记 ID，裸 /search_result?keyword= 搜索页仍拒绝。
    re.compile(r"/search_result/(?P<id>[0-9a-fA-F]{24})/?"),
)
NOTE_ID_QUERY_PATHS = {"/explore", "/discovery/item"}
NOTE_ID_RE = re.compile(r"[0-9a-zA-Z]+")
URL_IN_TEXT_RE = re.compile(r"(?i)\b(https?)://([^/\s?#'\"<>]+)[^\s'\"<>]*")
DETAIL_URL_HINT = (
    "只接受小红书笔记详情链接（/explore/<id>、/discovery/item/<id>、"
    "/user/profile/<uid>/<id>、/search_result/<24位笔记ID>）或 xhslink 短链；"
    "不支持搜索页、用户主页或其他页面"
)
ROUTE_BACKEND = "xiaohongshu-known-url"
# 快照里的令牌类参数值一律替换；名称含 token/session/ticket/signature 或为已知签名名。
TOKEN_NAME = (
    r"(?:[A-Za-z0-9_-]*(?:token|session|ticket|signature)[A-Za-z0-9_-]*"
    r"|sign|a1|webid|web_id|websectiga|sec_poison_id|x-s|x-t|x-s-common)"
)
QUERY_TOKEN_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_-])({TOKEN_NAME})(=|%3D)([^&\"'<>\s#\\]+?)(?=%26|&|[\"'<>\s#\\]|$)"
)
JSON_TOKEN_PATTERNS = (
    re.compile(rf'(?i)("{TOKEN_NAME}"\s*:\s*")((?:[^"\\]|\\.)*)(")'),
    re.compile(rf'(?i)(\\"{TOKEN_NAME}\\"\s*:\s*\\")((?:(?!\\").){{0,4000}})(\\")'),
    re.compile(rf"(?i)(&quot;{TOKEN_NAME}&quot;\s*:\s*&quot;)(.{{0,4000}}?)(&quot;)"),
)
REDACTED = "REDACTED"
# sns-webpic-qc.xhscdn.com/<时间戳>/<32 位签名>/... 这类路径签名地址会过期。
PATH_SIGNED_MEDIA_RE = re.compile(r"^/\d{10,14}/[0-9a-fA-F]{32}/")
MEDIA_URLS_EXPIRE_NOTE = (
    "image_urls/video_url 是带时间戳与签名路径的 CDN 地址，会过期失效；"
    "以本地下载的媒体文件为准"
)
REASON_HINTS = {
    "blocked_or_token_expired": "跳到了小红书安全拦截页（/404/sec_*）：可能是风控、xsec_token 失效/过期或笔记不可见",
    "login_or_captcha_required": "跳到了登录或验证码页面",
    "note_not_found": "跳到了 404 页面，笔记可能已删除或不可见",
    "no_note_data": "页面里没有可用笔记数据，可能被风控或需要有效 xsec_token",
}


class FetchFailure(ValueError):
    """带失败分类的抓取错误；仍是 ValueError，以兼容既有调用方。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class MediaDownloadError(RuntimeError):
    """一项媒体在所有候选地址上都没有完整下载。"""


def log(message: str) -> None:
    """人读进度只写 stderr，stdout 留给最终 JSON。"""
    print(message, file=sys.stderr)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_xiaohongshu_host(host: str) -> bool:
    return host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")


def is_xhslink_host(host: str) -> bool:
    return host == "xhslink.com" or host.endswith(".xhslink.com")


def _plain_https(url: str) -> bool:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and port in (None, 443)
    )


def validate_xiaohongshu_url(url: str) -> str:
    """只接受小红书 HTTPS 链接，不进行开放式站点访问。"""
    host = _host(url)
    if not _plain_https(url) or not (is_xiaohongshu_host(host) or is_xhslink_host(host)):
        raise FetchFailure("invalid_url", "只接受 https://*.xiaohongshu.com 或 https://*.xhslink.com 链接")
    return url


def cookie_allowed_for(url: Optional[str]) -> bool:
    """登录 Cookie 只能发往 https://*.xiaohongshu.com，绝不发给媒体 CDN 或其他主机。"""
    return bool(url) and _plain_https(url) and is_xiaohongshu_host(_host(url))


def request_headers(
    referer: Optional[str] = None,
    include_cookie: bool = False,
    target_url: Optional[str] = None,
) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    if include_cookie and cookie_allowed_for(target_url):
        cookie = os.environ.get("XHS_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
    return headers


def sanitize_error_text(text: str) -> str:
    """错误输出只保留 URL 的协议与主机，去掉可能带签名或 xsec_token 的路径和查询。"""
    return URL_IN_TEXT_RE.sub(lambda match: f"{match.group(1)}://{match.group(2)}/…", text or "")


def strip_url_query(url: str) -> str:
    """保存用的 URL：只保留协议、主机与路径，去掉签名、xsec_token 等查询参数。"""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    return urlunparse((scheme, host, parsed.path, "", "", ""))


def canonical_note_url(note_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def note_id_from_detail_url(url: str) -> Optional[str]:
    """只从笔记详情形态的 https 小红书 URL 中取 note_id；其他页面返回 None。"""
    if not _plain_https(url) or not is_xiaohongshu_host(_host(url)):
        return None
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path or "/")
    for pattern in NOTE_DETAIL_PATTERNS:
        match = pattern.fullmatch(path)
        if match:
            return match.group("id")
    if path.rstrip("/") in NOTE_ID_QUERY_PATHS:
        values = parse_qs(parsed.query).get("note_id", [])
        if len(values) == 1 and NOTE_ID_RE.fullmatch(values[0]):
            return values[0]
    return None


def extract_note_id(url: str) -> str:
    validate_xiaohongshu_url(url)
    if is_xhslink_host(_host(url)):
        raise FetchFailure("invalid_url", "xhslink 短链必须先跟随跳转，再从最终详情链接识别 note_id")
    note_id = note_id_from_detail_url(url)
    if not note_id:
        raise FetchFailure("invalid_url", DETAIL_URL_HINT)
    return note_id


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _next_hop(current: str, location: str) -> str:
    """解析跳转地址；小红书/xhslink 的 http 跳转升级为 https，其余一律拒绝。"""
    target = urljoin(current, location)
    parsed = urlparse(target)
    host = _host(target)
    if not (is_xiaohongshu_host(host) or is_xhslink_host(host)):
        raise FetchFailure("unexpected_redirect", f"跳转到小红书之外的站点，已停止: {host or '?'}")
    if parsed.scheme == "http":
        target = urlunparse(parsed._replace(scheme="https"))
    if not _plain_https(target):
        raise FetchFailure("unexpected_redirect", f"跳转地址不是普通 HTTPS 链接，已停止: {host}")
    return target


def redirect_failure_reason(target: str) -> str:
    """把非目标详情页的跳转落点归类；/404/sec_* 是小红书的安全拦截页。"""
    path = re.sub(r"/+", "/", urlparse(target).path or "/").lower()
    if path.startswith("/404/sec_") or path.startswith("/404/sec-"):
        return "blocked_or_token_expired"
    if path.startswith("/404"):
        return "note_not_found"
    if "login" in path or "captcha" in path or "verify" in path:
        return "login_or_captcha_required"
    if note_id_from_detail_url(target):
        return "redirected_to_other_note"
    return "redirected_to_non_note"


def resolve_short_link(url: str) -> str:
    """匿名跟随 xhslink 跳转，直到到达小红书详情页；不读取任何页面正文。"""
    current = url
    for _ in range(MAX_REDIRECTS):
        response = requests.get(
            current,
            headers=request_headers(),
            allow_redirects=False,
            stream=True,
            timeout=30,
        )
        try:
            status = response.status_code
            location = response.headers.get("Location") or ""
        finally:
            _close(response)
        if status not in REDIRECT_STATUSES or not location:
            raise FetchFailure(
                "short_link_unresolved", f"xhslink 短链没有返回跳转（HTTP {status}），无法确认目标笔记"
            )
        current = _next_hop(current, location)
        if is_xiaohongshu_host(_host(current)):
            if not note_id_from_detail_url(current):
                parsed = urlparse(current)
                raise FetchFailure(
                    redirect_failure_reason(current),
                    f"短链跳转目标不是笔记详情页: {_host(current)}{parsed.path}；{DETAIL_URL_HINT}",
                )
            return current
    raise FetchFailure("too_many_redirects", "xhslink 短链跳转次数过多，已停止")


def resolve_note_target(url: str) -> Tuple[str, str]:
    """返回 (实际请求的详情 URL, note_id)。短链先跳转再校验。"""
    validate_xiaohongshu_url(url)
    target = resolve_short_link(url) if is_xhslink_host(_host(url)) else url
    return target, extract_note_id(target)


def fetch_html(url: str, include_cookie: bool = False) -> str:
    """手动跟随跳转：每一跳都必须仍是同一篇笔记的 https 详情页，Cookie 按主机重新判定。"""
    expected_note_id = extract_note_id(url)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response = requests.get(
            current,
            headers=request_headers(include_cookie=include_cookie, target_url=current),
            timeout=60,
            allow_redirects=False,
        )
        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("Location") or ""
            _close(response)
            current = _next_hop(current, location)
            if note_id_from_detail_url(current) != expected_note_id:
                parsed = urlparse(current)
                raise FetchFailure(
                    redirect_failure_reason(current),
                    "笔记页面跳转到非目标详情页（可能需要登录、验证或笔记不可见）: "
                    f"{_host(current)}{parsed.path}",
                )
            continue
        response.raise_for_status()
        return response.text
    raise FetchFailure("too_many_redirects", "笔记页面跳转次数过多，已停止")


class MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "meta":
            return
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        key = (attributes.get("property") or attributes.get("name") or "").lower()
        content = attributes.get("content", "").strip()
        if key and content:
            self.values.setdefault(key, html_lib.unescape(content))


def extract_meta(page_html: str) -> Dict[str, str]:
    parser = MetaParser()
    parser.feed(page_html)
    return parser.values


def extract_initial_state(page_html: str) -> Dict[str, Any]:
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", page_html)
    if not match:
        raise ValueError("页面里没有找到 window.__INITIAL_STATE__，可能被风控或需要有效 xsec_token")

    start = match.end()
    end = page_html.find("</script>", start)
    if end == -1:
        raise ValueError("找到了 INITIAL_STATE 开头，但没有找到 script 结束位置")

    raw = page_html[start:end].strip().rstrip(";")
    raw = html_lib.unescape(raw)
    raw = re.sub(r"\bundefined\b", "null", raw)
    return json.loads(raw)


def collect_note_maps(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        note_map = obj.get("noteDetailMap")
        if isinstance(note_map, dict):
            yield note_map
        for value in obj.values():
            yield from collect_note_maps(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from collect_note_maps(value)


def extract_note(state: Dict[str, Any], note_id: str) -> Dict[str, Any]:
    for note_map in collect_note_maps(state):
        detail = note_map.get(note_id)
        if isinstance(detail, dict) and isinstance(detail.get("note"), dict):
            return detail["note"]
        for detail in note_map.values():
            if not isinstance(detail, dict):
                continue
            note = detail.get("note")
            if isinstance(note, dict) and (note.get("noteId") == note_id or note.get("id") == note_id):
                return note

    raise ValueError("INITIAL_STATE 中没有找到目标笔记数据")


def extract_note_from_html(page_html: str, note_id: str) -> Dict[str, Any]:
    return extract_note(extract_initial_state(page_html), note_id)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    return cleaned.strip("_") or "item"


def first_value(data: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_media_v2(note: Dict[str, Any]) -> Dict[str, Any]:
    video = note.get("video", {}) or {}
    raw = video.get("mediaV2") or video.get("consumer", {}).get("mediaV2")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def stream_items_from(stream: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for codec in ("h264", "h265", "h266", "av1"):
        items = stream.get(codec) or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield item


def collect_video_urls(note: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    media_v2 = parse_media_v2(note)
    streams = [
        media_v2.get("video", {}).get("stream", {}),
        note.get("video", {}).get("media", {}).get("stream", {}),
    ]

    for stream in streams:
        for item in stream_items_from(stream):
            master_url = first_value(item, ("masterUrl", "master_url"))
            backup_urls = first_value(item, ("backupUrls", "backup_urls")) or []
            for url in [master_url] + list(backup_urls):
                if url and url not in urls:
                    urls.append(url)
    return urls


def collect_subtitles(note: Dict[str, Any]) -> List[Dict[str, str]]:
    subtitles: List[Dict[str, str]] = []
    media_v2 = parse_media_v2(note)
    subtitle_map = media_v2.get("video", {}).get("subtitles", {}) or {}

    for label, entries in subtitle_map.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if url:
                subtitles.append({
                    "label": str(label),
                    "language": str(entry.get("language") or label),
                    "url": url,
                })

    def subtitle_priority(subtitle: Dict[str, str]) -> int:
        label = subtitle.get("label", "")
        language = subtitle.get("language", "")
        if label == "source":
            return 0
        if language.startswith("zh"):
            return 1
        return 2

    return sorted(subtitles, key=subtitle_priority)


def collect_images(note: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for image in note.get("imageList") or []:
        if not isinstance(image, dict):
            continue
        candidates = [
            image.get("urlDefault"),
            image.get("url"),
            image.get("urlPre"),
        ]
        for info in image.get("infoList") or []:
            if isinstance(info, dict):
                candidates.append(info.get("url"))
        for url in candidates:
            if url and url not in urls:
                urls.append(url)
                break
    return urls


def get_duration_ms(note: Dict[str, Any]) -> Optional[int]:
    media_v2 = parse_media_v2(note)
    for stream in [
        media_v2.get("video", {}).get("stream", {}),
        note.get("video", {}).get("media", {}).get("stream", {}),
    ]:
        for item in stream_items_from(stream):
            duration = first_value(item, ("duration", "videoDuration", "video_duration"))
            if duration:
                return int(duration)

    seconds = note.get("video", {}).get("consumer", {}).get("capa", {}).get("duration")
    if seconds:
        return int(float(seconds) * 1000)
    return None


def build_metadata(note: Dict[str, Any], source_url: str, note_id: str) -> Dict[str, Any]:
    """元数据只保存去掉查询参数的 URL；带签名的媒体直链只在内存中用于下载。"""
    user = note.get("user", {}) or {}
    tags = [item.get("name") for item in note.get("tagList") or [] if isinstance(item, dict) and item.get("name")]
    chapters = note.get("video", {}).get("consumer", {}).get("chapters") or []
    video_urls = collect_video_urls(note)
    subtitles = collect_subtitles(note)
    images = collect_images(note)

    return {
        "source_url": strip_url_query(source_url),
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extraction_source": "initial_state",
        "note_id": note_id,
        "title": note.get("title", ""),
        "desc": note.get("desc", ""),
        "type": note.get("type", ""),
        "author": user.get("nickname", ""),
        "user_id": user.get("userId", ""),
        "time": note.get("time"),
        "last_update_time": note.get("lastUpdateTime"),
        "ip_location": note.get("ipLocation", ""),
        "interact_info": note.get("interactInfo", {}) or {},
        "tags": tags,
        "duration_ms": get_duration_ms(note),
        "media_url_query_removed": True,
        "video_url": strip_url_query(video_urls[0]) if video_urls else "",
        "backup_urls": [strip_url_query(url) for url in video_urls[1:]],
        "subtitle_urls": [
            {**subtitle, "url": strip_url_query(subtitle["url"])} for subtitle in subtitles
        ],
        "chapters": chapters,
        "image_count": len(images),
        "image_urls": [strip_url_query(url) for url in images],
        **media_expiry_fields([*images, *video_urls]),
    }


def media_expiry_fields(urls: Iterable[Optional[str]]) -> Dict[str, Any]:
    """路径签名的 CDN 地址会过期：明确标注，避免把 metadata 里的链接当作长期可用。"""
    expire = any(
        PATH_SIGNED_MEDIA_RE.match(urlparse(url).path or "") for url in urls if isinstance(url, str) and url
    )
    fields: Dict[str, Any] = {"media_urls_expire": expire}
    if expire:
        fields["media_urls_note"] = MEDIA_URLS_EXPIRE_NOTE
    return fields


def redact_tokens(text: str) -> str:
    """把快照里的 xsec_token 等令牌参数值替换为 REDACTED，结构与其他内容保持原样。"""
    redacted = QUERY_TOKEN_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text or "")
    for pattern in JSON_TOKEN_PATTERNS:
        redacted = pattern.sub(
            lambda match: match.group(1) + (REDACTED if match.group(2) else "") + match.group(3),
            redacted,
        )
    return redacted


def meta_media_urls(meta: Dict[str, str]) -> Tuple[str, str]:
    """返回网页 meta 中的 (视频直链, 封面图直链)，仅供内存中下载使用。"""
    video_url = first_value(meta, ("og:video", "og:video:url", "twitter:player:stream")) or ""
    image_url = first_value(meta, ("og:image", "twitter:image")) or ""
    return video_url, image_url


def build_metadata_from_meta(meta: Dict[str, str], source_url: str, note_id: str) -> Dict[str, Any]:
    title = first_value(meta, ("og:title", "twitter:title")) or ""
    description = first_value(meta, ("description", "og:description", "twitter:description")) or ""
    video_url, image_url = meta_media_urls(meta)
    images = [strip_url_query(image_url)] if image_url else []
    note_type = "video" if video_url else ("normal" if image_url else "")
    return {
        "source_url": strip_url_query(source_url),
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extraction_source": "meta_fallback",
        "note_id": note_id,
        "title": title,
        "desc": description,
        "type": note_type,
        "author": "",
        "user_id": "",
        "time": None,
        "last_update_time": None,
        "ip_location": "",
        "interact_info": {},
        "tags": [],
        "duration_ms": None,
        "media_url_query_removed": True,
        "video_url": strip_url_query(video_url),
        "backup_urls": [],
        "subtitle_urls": [],
        "chapters": [],
        "image_count": len(images),
        "image_urls": images,
        **media_expiry_fields([video_url, image_url]),
    }


def normalize_media_url(url: Any) -> Optional[str]:
    """媒体地址只允许 https；http 与协议相对地址升级为 https，其他协议拒绝。"""
    if not isinstance(url, str) or not url.strip():
        return None
    value = url.strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.scheme == "http":
        value = urlunparse(parsed._replace(scheme="https"))
    if not _plain_https(value) or not _host(value):
        return None
    return value


def _declared_length(response: Any) -> int:
    """无压缩编码时返回 Content-Length；缺失、非法或有编码时返回 0（只做非空校验）。"""
    headers = getattr(response, "headers", None) or {}
    encoding = str(headers.get("Content-Encoding") or "").strip().lower()
    raw = str(headers.get("Content-Length") or "").strip()
    if encoding not in {"", "identity"} or not raw.isdigit():
        return 0
    return int(raw)


def download_file(
    urls: List[str],
    output_path: Path,
    label: str,
    referer: str,
    allow_cookie_fallback: bool = False,
    usage: Optional[Dict[str, bool]] = None,
) -> Path:
    """先写 <name>.part，长度校验通过后才改成正式文件名；全部地址失败时抛出 MediaDownloadError。

    usage 若给出，用登录态成功下载时记 usage["cookie"] = True。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.stat().st_size > 0:
        log(f"{label} 已存在，跳过下载: {output_path}")
        return output_path

    partial = output_path.with_name(output_path.name + ".part")
    last_error: Optional[Exception] = None
    cookie_available = bool(os.environ.get("XHS_COOKIE", "").strip())

    for index, raw_url in enumerate(urls, start=1):
        url = normalize_media_url(raw_url)
        if url is None:
            last_error = ValueError("媒体地址不是可用的 http(s) 链接，已拒绝")
            continue
        cookie_attempts = (
            [False, True]
            if allow_cookie_fallback and cookie_available and cookie_allowed_for(url)
            else [False]
        )
        for include_cookie in cookie_attempts:
            try:
                if include_cookie:
                    log(f"{label} 匿名下载失败，尝试使用已明确授权的登录态")
                response = requests.get(
                    url,
                    headers=request_headers(referer, include_cookie=include_cookie, target_url=url),
                    stream=True,
                    timeout=90,
                )
                downloaded = 0
                try:
                    response.raise_for_status()
                    total = _declared_length(response)
                    next_percent = 0
                    next_mb_report = 5
                    with open(partial, "wb") as file:
                        for chunk in response.iter_content(chunk_size=1024 * 256):
                            if not chunk:
                                continue
                            file.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                percent = min(100, downloaded * 100 // total)
                                if percent >= next_percent or downloaded == total:
                                    log(f"{label} 下载进度: {percent}% ({downloaded//1024//1024}MB / {total//1024//1024}MB)")
                                    next_percent += 10
                            else:
                                downloaded_mb = downloaded // 1024 // 1024
                                if downloaded_mb >= next_mb_report:
                                    log(f"{label} 已下载: {downloaded_mb}MB")
                                    next_mb_report += 5
                finally:
                    _close(response)
                if downloaded <= 0:
                    raise ValueError("下载结果为空")
                if total and downloaded != total:
                    raise ValueError(f"下载不完整（声明 {total} 字节，实际 {downloaded} 字节）")
                if output_path.exists():
                    raise FileExistsError(f"拒绝覆盖既有文件: {output_path}")
                partial.rename(output_path)
                if include_cookie and usage is not None:
                    usage["cookie"] = True
                return output_path
            except Exception as exc:
                last_error = exc
        if index < len(urls):
            log(f"{label} 当前地址失败，尝试备用地址...")

    reason = sanitize_error_text(str(last_error)) if last_error else "没有可用的下载地址"
    log(f"{label} 下载失败: {reason}")
    if partial.exists():
        log(f"{label} 未完成的临时文件已保留（未改名为正式文件）: {partial}")
    raise MediaDownloadError(reason)


def srt_to_transcript(srt_text: str) -> str:
    lines: List[str] = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        block_lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line_index = next((i for i, line in enumerate(block_lines) if "-->" in line), None)
        if time_line_index is None:
            continue
        start_time = block_lines[time_line_index].split("-->", 1)[0].strip()
        text = " ".join(block_lines[time_line_index + 1:]).strip()
        if text:
            lines.append(f"[{start_time}] {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        file.write(text)


def choose_unique_output_dir(output_dir: Path) -> Path:
    """目标目录存在时使用同级 run 目录（与其他执行器一致，首个重跑为 -run-2），绝不覆盖旧归档。"""
    requested = output_dir.expanduser()
    candidate = requested
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = requested.with_name(f"{requested.name}-run-{counter}")
        counter += 1
    return candidate


def create_output_dir(requested: Path) -> Path:
    """排他创建输出目录；并发运行抢先占用同名目录时换下一个 -run-N。"""
    for _ in range(1000):
        candidate = choose_unique_output_dir(requested)
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FetchFailure("output_conflict", "找不到不冲突的输出目录")


def failure_reason(exc: BaseException) -> str:
    if isinstance(exc, FetchFailure):
        return exc.reason
    # requests 的异常是 OSError 子类，必须先于本地写盘错误判断。
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.HTTPError):
        return "http_error"
    if isinstance(exc, requests.exceptions.RequestException):
        return "network_error"
    if isinstance(exc, OSError):
        return "write_failed"
    return "unexpected_error"


def http_status_of(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


class _JsonArgumentParser(argparse.ArgumentParser):
    """参数错误也以 JSON 结果返回，而不是只在 stderr 打印用法。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise FetchFailure("invalid_arguments", sanitize_error_text(message))


def emit(result: Dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> None:
    parser = _JsonArgumentParser(description="小红书笔记抓取器")
    parser.add_argument("url", help="小红书笔记详情链接或 xhslink 短链")
    parser.add_argument("output_dir", nargs="?", help="输出目录，默认 ~/Downloads/xhs_<note_id>")
    parser.add_argument("--skip-media", action="store_true", help="只保存 HTML 和元数据，不下载视频/字幕/图片")
    parser.add_argument(
        "--use-cookie",
        action="store_true",
        help="匿名解析失败后，允许使用 XHS_COOKIE 重试；必须由用户当轮明确授权",
    )
    result: Dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "platform": "xiaohongshu",
        "route_backend": ROUTE_BACKEND,
        "operation": None,
        "note_id": None,
        "source_url": None,
        "authentication": "anonymous",
        "discovery_performed": False,
        "output_dir": None,
        "html_path": None,
        "metadata_path": None,
        "media_paths": [],
        "media_failures": [],
    }
    try:
        args = parser.parse_args(argv)
    except FetchFailure as exc:
        result.update(status="rejected", reason=exc.reason, error=str(exc), side_effects_performed=False)
        emit(result)
        sys.exit(1)
    result["operation"] = "read" if args.skip_media else "download"

    try:
        target_url, note_id = resolve_note_target(args.url)
    except Exception as exc:
        reason = failure_reason(exc)
        message = sanitize_error_text(str(exc))
        log(f"抓取失败: {message}")
        result.update(
            status="rejected" if reason == "invalid_url" else "failed",
            reason=reason,
            error=message,
            side_effects_performed=False,
        )
        emit(result)
        sys.exit(1)
    # 元数据与 Referer 都只用不带 xsec_token 的规范详情链接；带 token 的地址只用于请求页面。
    source_url = canonical_note_url(note_id)
    result.update(note_id=note_id, source_url=source_url)
    cookie_ready = args.use_cookie and bool(os.environ.get("XHS_COOKIE", "").strip())
    usage: Dict[str, bool] = {"cookie": False}

    requested_output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path.home() / "Downloads" / f"xhs_{note_id}"
    )

    log("=" * 50)
    log("小红书笔记抓取器")
    log("=" * 50)
    log(f"笔记ID: {note_id}")

    media_failures: List[Dict[str, str]] = result["media_failures"]
    media_paths: List[str] = result["media_paths"]

    def fetch_media(urls: List[str], path: Path, label: str) -> Optional[Path]:
        try:
            saved = download_file(
                urls,
                path,
                label,
                source_url,
                allow_cookie_fallback=args.use_cookie,
                usage=usage,
            )
        except MediaDownloadError as exc:
            media_failures.append({"item": label, "reason": str(exc)})
            return None
        media_paths.append(str(saved))
        return saved

    output_dir: Optional[Path] = None
    try:
        try:
            page_html = fetch_html(target_url, include_cookie=False)
        except Exception as anonymous_fetch_error:
            if cookie_ready:
                log(
                    "匿名页面请求失败，尝试使用已明确授权的登录态："
                    f"{sanitize_error_text(str(anonymous_fetch_error))}"
                )
                usage["cookie"] = True
                page_html = fetch_html(target_url, include_cookie=True)
            else:
                raise
        note: Optional[Dict[str, Any]] = None
        state_error: Optional[Exception] = None

        try:
            note = extract_note_from_html(page_html, note_id)
        except Exception as exc:
            state_error = exc

        if note is None and cookie_ready:
            log("匿名页面没有完整结构化笔记，尝试使用已明确授权的登录态")
            usage["cookie"] = True
            page_html = fetch_html(target_url, include_cookie=True)
            try:
                note = extract_note_from_html(page_html, note_id)
            except Exception as exc:
                state_error = exc

        # 带签名的媒体直链只留在内存里用于下载，不写进 metadata。
        if note is not None:
            metadata = build_metadata(note, source_url, note_id)
            video_urls = collect_video_urls(note)
            subtitles = collect_subtitles(note)
            image_urls = collect_images(note)
        else:
            meta = extract_meta(page_html)
            metadata = build_metadata_from_meta(meta, source_url, note_id)
            has_useful_meta = any(
                [metadata["title"], metadata["desc"], metadata["video_url"], metadata["image_urls"]]
            )
            if not has_useful_meta:
                cookie_hint = "；可在用户当轮明确授权后配置 XHS_COOKIE 并加 --use-cookie" if not args.use_cookie else ""
                # 在创建任何目录之前失败：风控/令牌过期的页面不留下空目录或快照。
                raise FetchFailure(
                    "no_note_data", f"INITIAL_STATE 与 meta 都没有可用笔记数据：{state_error}{cookie_hint}"
                )
            log(f"结构化状态解析失败，已回退网页 meta：{state_error}")
            fallback_video, fallback_image = meta_media_urls(meta)
            video_urls = [fallback_video] if fallback_video else []
            subtitles = []
            image_urls = [fallback_image] if fallback_image else []

        output_dir = create_output_dir(requested_output_dir)
        result["output_dir"] = str(output_dir)
        if output_dir != requested_output_dir:
            log(f"目标已存在，改用不冲突目录: {output_dir}")
        log(f"输出目录: {output_dir}")

        html_path = output_dir / f"xhs_{note_id}.html"
        write_text(html_path, redact_tokens(page_html))
        result["html_path"] = str(html_path)
        log(f"网页快照（令牌已替换为 {REDACTED}）: {html_path}")

        metadata_path = output_dir / f"xhs_{note_id}.metadata.json"
        write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))
        result.update(
            metadata_path=str(metadata_path),
            title=metadata.get("title") or None,
            note_type=metadata.get("type") or None,
            extraction_source=metadata.get("extraction_source"),
            media_urls_expire=metadata.get("media_urls_expire", False),
        )
        log(f"元数据: {metadata_path}")
        log(f"标题: {metadata.get('title') or '(无标题)'}")
        log(f"作者: {metadata.get('author') or '(未知)'}")
        log(f"类型: {metadata.get('type') or '(未知)'}")

        if args.skip_media:
            log("已按 --skip-media 跳过媒体下载")
        elif metadata["type"] == "video":
            video_urls = [url for url in video_urls if url]
            if video_urls:
                video_path = output_dir / f"xhs_{note_id}.mp4"
                saved_video = fetch_media(video_urls, video_path, "视频")
                if saved_video:
                    size_mb = saved_video.stat().st_size / 1024 / 1024
                    log(f"视频: {saved_video} ({size_mb:.2f} MB)")
            else:
                log("未找到视频直链")
                media_failures.append({"item": "视频", "reason": "未找到视频直链"})

            source_transcript_written = False
            fallback_srt: Optional[Path] = None
            for subtitle in subtitles:
                label = safe_name(f"{subtitle['label']}_{subtitle['language']}")
                srt_path = output_dir / f"xhs_{note_id}.{label}.srt"
                saved_srt = fetch_media([subtitle["url"]], srt_path, f"字幕 {label}")
                if saved_srt:
                    log(f"字幕 {label}: {saved_srt}")
                    fallback_srt = fallback_srt or saved_srt

                is_preferred_transcript = (
                    subtitle.get("label") == "source"
                    or subtitle.get("language", "").startswith("zh")
                )
                if saved_srt and is_preferred_transcript and not source_transcript_written:
                    transcript = srt_to_transcript(saved_srt.read_text(encoding="utf-8", errors="ignore"))
                    transcript_path = output_dir / f"xhs_{note_id}.transcript.txt"
                    write_text(transcript_path, transcript)
                    media_paths.append(str(transcript_path))
                    log(f"口播文本: {transcript_path}")
                    source_transcript_written = True
            if fallback_srt and not source_transcript_written:
                transcript = srt_to_transcript(fallback_srt.read_text(encoding="utf-8", errors="ignore"))
                transcript_path = output_dir / f"xhs_{note_id}.transcript.txt"
                write_text(transcript_path, transcript)
                media_paths.append(str(transcript_path))
                log(f"口播文本: {transcript_path}")
        else:
            if not image_urls:
                log("未找到图片地址")
                media_failures.append({"item": "图片", "reason": "未找到图片地址"})
            images_dir = output_dir / "images"
            for index, image_url in enumerate(image_urls, start=1):
                suffix = Path(urlparse(image_url).path).suffix or ".jpg"
                image_path = images_dir / f"xhs_{note_id}_{index:02d}{suffix}"
                saved_image = fetch_media([image_url], image_path, f"图片 {index}")
                if saved_image:
                    log(f"图片 {index}: {saved_image}")
    except Exception as exc:
        reason = failure_reason(exc)
        message = sanitize_error_text(str(exc))
        log(f"抓取失败: {message}")
        result.update(reason=reason, error=message, side_effects_performed=output_dir is not None)
        hint = REASON_HINTS.get(reason)
        if hint:
            result["hint"] = hint
        status = http_status_of(exc)
        if status is not None:
            result["http_status"] = status
        if usage["cookie"]:
            result["authentication"] = "cookie_authorized_this_turn"
        emit(result)
        sys.exit(1)

    if usage["cookie"]:
        result["authentication"] = "cookie_authorized_this_turn"
    result["side_effects_performed"] = True
    if media_failures:
        log(f"抓取未完成：{len(media_failures)} 项媒体失败（网页快照与元数据已保存）")
        for failure in media_failures:
            log(f"- {failure['item']}: {failure['reason']}")
        result.update(status="partial", reason="media_download_failed")
        emit(result)
        sys.exit(2)
    log("抓取完成")
    result.update(ok=True, status="success")
    emit(result)


if __name__ == "__main__":
    main()
