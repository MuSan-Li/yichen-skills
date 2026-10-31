#!/usr/bin/env python3
"""Run AnySearch safely and emit unified-search candidate envelopes.

The upstream AnySearch CLI intentionally returns Markdown.  This adapter keeps
that CLI as the source of truth, reads its configured command from
``runtime.conf``, and normalizes search results without treating parser
failures as successful empty searches.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

try:
    import idna as _idna_uts46
except ImportError:  # Fail closed for non-ASCII hosts when the helper is absent.
    _idna_uts46 = None


def _load_public_urls() -> Any:
    """Load the sibling host rule by path; this module itself is often loaded by path."""

    module_name = "_yichen_unified_search_public_urls"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).resolve().with_name("public_urls.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError("public_urls.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[module_name] = module
    return module


_PUBLIC_URLS = _load_public_urls()
PUBLIC_HOST_PROBLEM_MESSAGES = {
    "malformed": "URL host is malformed",
    "non_public": "URL does not identify a public host",
    "not_fully_qualified": "URL host is not a fully qualified public name",
    "ambiguous_numeric": "URL contains an ambiguous numeric host",
}


BACKEND = "anysearch"
SKILL_DIR = Path(__file__).resolve().parents[1]
SKILLS_ROOT = Path(
    os.environ.get("YICHEN_SKILLS_ROOT", str(SKILL_DIR.parent))
).expanduser()
DEFAULT_RUNTIME_CONF = Path(
    os.environ.get(
        "YICHEN_ANYSEARCH_RUNTIME_CONF",
        str(SKILLS_ROOT / "anysearch" / "runtime.conf"),
    )
).expanduser()
RECEIPT_KEY_ENV = "YICHEN_UNIFIED_SEARCH_RECEIPT_KEY"
RECEIPT_KEY_FILE_ENV = "YICHEN_UNIFIED_SEARCH_RECEIPT_KEY_FILE"
DEFAULT_RECEIPT_KEY_FILE = (
    Path.home() / ".config" / "agent-secrets" / "yichen-unified-search-receipt-key"
)
RECEIPT_TTL_SECONDS = 2 * 60 * 60
MIN_RECEIPT_KEY_BYTES = 32
SEARCH_SNIPPET_LIMITATION = (
    "AnySearch result snippets are discovery text, not verified page content."
)
VERIFY_LIMITATION = (
    "Opening the original URL does not by itself verify a factual claim; "
    "verification status remains candidate."
)
# Live 2026-10-31: a `site:xiaoyuzhoufm.com` query returned a google.com/goto link.
# Its destination is opaque, so it may lie outside any site: scope of the query.
SEARCH_REDIRECT_WRAPPERS = {
    "google.com": ("/url", "/goto"),
    "www.google.com": ("/url", "/goto"),
    "bing.com": ("/ck/a",),
    "www.bing.com": ("/ck/a",),
    "baidu.com": ("/link",),
    "www.baidu.com": ("/link",),
}
REDIRECT_WRAPPER_LIMITATION = (
    "This URL is a search-engine redirect wrapper; its destination, and whether it "
    "matches any site: scope in the query, is unknown until the page is opened."
)


def is_search_redirect_wrapper(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.path.rstrip("/") in SEARCH_REDIRECT_WRAPPERS.get(host, ())

# An extracted original counts as opened only when it carries substantive text.
# Navigation stubs and failed extractions (observed at 26-54 visible characters)
# must not be reported as opened originals.
MIN_ORIGINAL_CONTENT_CHARS = 200
EXTRACT_FAILURE_MARKERS = ("extract_failed", "unable to extract content from the url")
WHITESPACE_RE = re.compile(r"\s+")
# Anti-bot, verification and login gates can pass the length gate. Observed 2026-10-13:
# BOSS直聘 {"code":37,"message":"您的环境存在异常."}, 百度安全验证 slider pages, jobui
# 访问异常 captcha pages, and a redirect to zhipin.com/web/passport/zp/security.html.
# Observed 2026-10-23: Reddit served "Reddit - Prove your humanity" with link boilerplate
# long enough to pass the length gate, and its body uses a typographic apostrophe (you’re).
# A title marker must make up most of one title segment (百度安全验证, 安全验证 - 知乎,
# Just a moment...), and text phrases count only near the top or on a short page, so an
# article titled or navigated with 人机验证 or 验证码 stays readable.
BLOCK_PAGE_TITLE_MARKERS = (
    "安全验证", "访问验证", "人机验证", "请输入验证码", "验证码", "请稍候", "环境异常", "访问异常",
    "访问受限", "just a moment", "attention required", "access denied", "security check", "captcha",
    "prove your humanity", "are you a robot",
)
TITLE_SEGMENT_RE = re.compile(r"[-|_–—·:：｜]+")
BLOCK_PAGE_TEXT_MARKERS = (
    "环境存在异常", "系统检测到访问异常", "检测到您的访问异常", "访问过于频繁",
    "请完成下方验证", "请完成安全验证", "完成验证后继续", "拖动左侧滑块", "拖动下方滑块",
    "请输入下方验证码", "verify you are human", "verifying you are human",
    "checking your browser", "enable javascript and cookies to continue",
    "unusual traffic from your computer network", "prove your humanity",
    "let us know you're a real person", "make sure you're not a robot",
    "your request has been blocked due to a network policy",
)
# Typographic apostrophes compare as ASCII so "you’re" matches a "you're" marker.
APOSTROPHE_TRANSLATION = str.maketrans({"‘": "'", "’": "'", "ʼ": "'"})
BLOCK_PAGE_REDIRECT_MARKERS = (
    "/passport", "/login", "/signin", "/captcha", "security.html", "/account/unhuman",
    "wappass.baidu.com",
)
BLOCK_SHORT_PAGE_CHARS = 2000
BLOCK_PAGE_HEAD_CHARS = 600
# Link targets are not visible text: a stub of [Home](https://...long-slug) links must
# not pass the length gate on its URLs. Images keep their alt text, links their label.
_MARKDOWN_TARGET = (
    r"\((?:<[^<>\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
MARKDOWN_IMAGE_TARGET_RE = re.compile(r"!\[([^\[\]]*)\]" + _MARKDOWN_TARGET)
MARKDOWN_LINK_TARGET_RE = re.compile(r"\[([^\[\]]*)\]" + _MARKDOWN_TARGET)
MARKDOWN_LINK_DEFINITION_RE = re.compile(r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*\S+.*$")


def visible_text(text: str) -> str:
    """Page text as a reader sees it: Markdown link and image targets removed."""

    text = MARKDOWN_LINK_DEFINITION_RE.sub("", text)
    text = MARKDOWN_IMAGE_TARGET_RE.sub(r"\1", text)
    return MARKDOWN_LINK_TARGET_RE.sub(r"\1", text)


def visible_char_count(text: str) -> int:
    """Non-whitespace characters of the visible text; every original-content reader uses this."""

    return len(WHITESPACE_RE.sub("", visible_text(text)))


def _compact(text: str) -> str:
    return WHITESPACE_RE.sub("", text).lower().translate(APOSTROPHE_TRANSLATION)


def blocked_page_marker(
    content: str, *, title: str = "", requested_url: str = "", final_url: str = ""
) -> str:
    """Return the gate marker when a read original is an anti-bot, verification or login page."""
    for segment in TITLE_SEGMENT_RE.split(title):
        compact_segment = _compact(segment)
        for marker in BLOCK_PAGE_TITLE_MARKERS:
            compact_marker = _compact(marker)
            if compact_marker in compact_segment and 2 * len(compact_marker) > len(compact_segment):
                return marker
    body = _compact(content)
    scope = body if len(body) <= BLOCK_SHORT_PAGE_CHARS else body[:BLOCK_PAGE_HEAD_CHARS]
    for marker in BLOCK_PAGE_TEXT_MARKERS:
        if _compact(marker) in scope:
            return marker
    if final_url and requested_url and final_url != requested_url:
        parsed = urlsplit(final_url.lower())
        target = (parsed.hostname or "") + parsed.path
        for marker in BLOCK_PAGE_REDIRECT_MARKERS:
            if marker in target:
                return marker
    return ""


def split_extract_output(output: str) -> tuple[str, str, str]:
    """AnySearch extract prints {"url","title","content"} JSON; anything else is page text."""
    try:
        data = json.loads(output)
    except ValueError:
        return "", output, ""
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return "", output, ""
    title = data.get("title") if isinstance(data.get("title"), str) else ""
    url = data.get("url") if isinstance(data.get("url"), str) else ""
    return title, data["content"], url
ALLOWED_BATCH_KEYS = {
    "query",
    "domain",
    "sub_domain",
    "sub_domain_params",
    "max_results",
}
SEARCH_ROUTE_REASONS = {
    "public_web_default",
    "anysearch_vertical_domain",
    "public_web_batch",
    "anysearch_vertical_domain_batch",
}
SAFE_CHILD_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
        "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "PATHEXT", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)

SEARCH_HEADER_RE = re.compile(
    r"^##[ \t]+Search Results[ \t]*\([ \t]*(?P<count>\d+)[ \t]+"
    r"results?(?:[ \t]*,[ \t]*(?P<meta>[^)]*))?\)[ \t]*$",
    flags=re.MULTILINE | re.IGNORECASE,
)
RESULT_START_RE = re.compile(
    r"^###[ \t]+(?P<rank>\d+)\.[ \t]+(?P<title>[^\n]+)\n"
    r"(?:[ \t]*\n)*-[ \t]*\*\*URL\*\*:[ \t]*(?P<url>[^\n]+)[ \t]*$",
    flags=re.MULTILINE | re.IGNORECASE,
)
QUERY_SECTION_RE = re.compile(
    r"(?:\A|^---[ \t]*\n(?:[ \t]*\n)*)"
    r"##[ \t]+Query[ \t]+(?P<index>\d+):[ \t]*(?P<query>[^\n]*)$",
    flags=re.MULTILINE | re.IGNORECASE,
)
MARKDOWN_LINK_RE = re.compile(
    r"^\[[^\]]*\]\((?P<url>https?://[^\s)]+)(?:[ \t]+[\"'][^\"']*[\"'])?\)$",
    flags=re.IGNORECASE,
)


class AdapterError(RuntimeError):
    """A public, already-redacted adapter failure."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class ParsedRow:
    rank: int
    title: str
    url: str
    snippet: str | None


@dataclass(frozen=True)
class ParsedSection:
    rows: tuple[ParsedRow, ...]
    reported_count: int | None
    elapsed_ms: int | None
    status: str
    errors: tuple[dict[str, Any], ...]


Runner = Callable[[Path, list[str], int], str]


def safe_child_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Keep only process/runtime settings; credentials require explicit runtime overrides."""

    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name.upper() in SAFE_CHILD_ENV_NAMES
    }


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError("invalid_candidate", f"{label} is required.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise AdapterError("invalid_candidate", f"{label} is invalid.") from None
    if parsed.tzinfo is None:
        raise AdapterError(
            "invalid_candidate", f"{label} needs a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _require_receipt_key(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or len(secret) < MIN_RECEIPT_KEY_BYTES:
        raise AdapterError(
            "runtime_unavailable",
            "Unified-search receipt key must contain at least 32 bytes.",
        )
    return secret


def _read_secure_receipt_key(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AdapterError(
            "runtime_unavailable", "Unified-search receipt key could not be read."
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AdapterError(
                "runtime_unavailable", "Unified-search receipt key is not a regular file."
            )
        if metadata.st_mode & 0o077:
            raise AdapterError(
                "runtime_unavailable",
                "Unified-search receipt key permissions must be 0600 or stricter.",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise AdapterError(
                "runtime_unavailable",
                "Unified-search receipt key must be owned by the current user.",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            secret = handle.read(4097)
        if len(secret) > 4096:
            raise AdapterError(
                "runtime_unavailable", "Unified-search receipt key file is too large."
            )
        return _require_receipt_key(secret)
    finally:
        os.close(descriptor)


def _ensure_private_key_parent(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise AdapterError(
                "runtime_unavailable",
                "Unified-search receipt key directory is not private.",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise AdapterError(
                "runtime_unavailable",
                "Unified-search receipt key directory must be owned by the current user.",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise AdapterError(
                "runtime_unavailable",
                "Unified-search receipt key directory permissions must be 0700.",
            )
    except AdapterError:
        raise
    except OSError:
        raise AdapterError(
            "runtime_unavailable",
            "Unified-search receipt key directory could not be secured.",
        ) from None


def _write_new_private_file(path: Path, secret: bytes) -> None:
    """Create ``path`` exclusively (0600) and write ``secret`` to it."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(descriptor)
        os.chmod(path, 0o600)
    finally:
        os.close(descriptor)


def _create_receipt_key(path: Path) -> bytes:
    """Publish a new key atomically; a concurrent first run keeps the winner's key.

    The key is written to a private temporary file and hard-linked into place, so
    the key path never exists half-written (a reader racing the creator used to
    see an empty file) and an existing key is never replaced.
    """

    _ensure_private_key_parent(path.parent)
    secret = secrets.token_bytes(MIN_RECEIPT_KEY_BYTES)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        _write_new_private_file(temporary, secret)
    except OSError:
        raise AdapterError(
            "runtime_unavailable", "Unified-search receipt key could not be written."
        ) from None
    try:
        os.link(temporary, path)
    except FileExistsError:
        return _read_secure_receipt_key(path)
    except OSError:
        # No hard links on this filesystem: fall back to a direct exclusive create.
        try:
            _write_new_private_file(path, secret)
        except FileExistsError:
            return _read_secure_receipt_key(path)
        except OSError:
            raise AdapterError(
                "runtime_unavailable", "Unified-search receipt key could not be created."
            ) from None
    finally:
        try:
            os.unlink(temporary)  # Only the private temporary this call created.
        except OSError:
            pass
    return secret


def load_receipt_secret(
    *,
    key_file: Path | None = None,
    environ: dict[str, str] | None = None,
) -> bytes:
    """Load the receipt HMAC key without exposing it in argv or output."""

    environment = os.environ if environ is None else environ
    inline = environment.get(RECEIPT_KEY_ENV)
    if inline is not None:
        return _require_receipt_key(inline.encode("utf-8"))

    if key_file is None:
        configured = environment.get(RECEIPT_KEY_FILE_ENV)
        if configured:
            key_file = Path(configured).expanduser()
            if not key_file.is_absolute():
                raise AdapterError(
                    "runtime_unavailable",
                    "Unified-search receipt key file path must be absolute.",
                )
        else:
            key_file = DEFAULT_RECEIPT_KEY_FILE
    path = key_file.expanduser()
    try:
        return _read_secure_receipt_key(path)
    except AdapterError as exc:
        if path.is_symlink():
            raise exc
        if path.exists():
            # Another process may have published the key between our read and this
            # check; its file is complete (hard-link publish), so read it once more.
            return _read_secure_receipt_key(path)
    return _create_receipt_key(path)


def _normalize_host_uts46(raw_host: str) -> str:
    """Normalize one URL host without falling back to legacy IDNA2003."""

    host_input = raw_host.rstrip(".")
    if not host_input or "%" in host_input:
        raise ValueError("URL host is malformed")
    try:
        literal = ipaddress.ip_address(host_input)
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal).lower()

    if _idna_uts46 is None:
        if any(ord(character) > 0x7F for character in host_input):
            raise ValueError("URL host is malformed")
        try:
            return host_input.encode("ascii").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("URL host is malformed") from None
    try:
        return _idna_uts46.encode(
            host_input,
            uts46=True,
            transitional=False,
            std3_rules=True,
        ).decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise ValueError("URL host is malformed") from None


def validate_public_http_url(value: Any) -> str:
    """Return a public HTTP(S) URL, without DNS resolution."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL is missing")
    cleaned = value.strip()
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in cleaned):
        raise ValueError("URL contains unsafe whitespace or control characters")
    try:
        parsed = urlsplit(cleaned)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("URL is malformed") from None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("URL is not HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL contains user information")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is invalid")

    try:
        host = _normalize_host_uts46(hostname)
    except ValueError:
        raise ValueError("URL host is malformed") from None
    # Shared with Firecrawl and the route checks: localhost, .local, .internal, .lan,
    # .home, .intranet, non-global IPs and numeric-only names are never public.
    problem = _PUBLIC_URLS.public_host_problem(host)
    if problem is not None:
        raise ValueError(
            PUBLIC_HOST_PROBLEM_MESSAGES.get(problem, "URL does not identify a public host")
        )
    return cleaned


def _parse_elapsed_ms(meta: str | None) -> int | None:
    if not meta:
        return None
    match = re.search(r"(?<!\d)(\d+)[ \t]*ms\b", meta, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _public_error(category: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"category": category, "message": message}
    for key, value in details.items():
        if value is not None:
            result[key] = value
    return result


def _output_shape(text: str) -> str:
    """Name the coarse form of unparsable output without exposing any of it.

    Telling an upstream error page (json/html/short text) from a changed result
    format (markdown_heading) is enough to decide what to fix.
    """

    first = text.lstrip()[:1]
    if not first:
        return "empty"
    if first in "{[":
        return "json"
    if first == "<":
        return "html_or_xml"
    if first == "#":
        return "markdown_heading"
    return "text"


def _clean_snippet(raw: str) -> str | None:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and (not lines[-1].strip() or lines[-1].strip() == "---"):
        lines.pop()
    if lines and lines[0].startswith("- "):
        lines[0] = lines[0][2:]
    value = "\n".join(lines).strip()
    return value or None


def _clean_url(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    else:
        link = MARKDOWN_LINK_RE.fullmatch(value)
        if link:
            value = link.group("url")
    return validate_public_http_url(value)


def _candidate_id(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return f"web:{digest}"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _receipt_payload(
    candidate: dict[str, Any], *, run_id: str, expires_at: str
) -> bytes:
    provenance = candidate.get("provenance")
    retrieved_at = provenance.get("retrieved_at") if isinstance(provenance, dict) else None
    payload = {
        "version": 1,
        "run_id": run_id,
        "candidate_id": candidate.get("candidate_id"),
        "url": candidate.get("url"),
        "query": candidate.get("query"),
        "retrieved_at": retrieved_at,
        "expires_at": expires_at,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _receipt_signature(
    candidate: dict[str, Any], *, run_id: str, expires_at: str, secret: bytes
) -> str:
    digest = hmac.new(
        _require_receipt_key(secret),
        _receipt_payload(candidate, run_id=run_id, expires_at=expires_at),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_candidate_receipt(
    candidate: dict[str, Any],
    *,
    secret: bytes,
    run_id: str,
) -> None:
    """Attach one signed, short-lived receipt to a search-produced candidate."""

    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise AdapterError("internal_error", "Search receipt run ID is invalid.")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, dict):
        raise AdapterError("internal_error", "Candidate provenance is missing.")
    retrieved_at = _parse_timestamp(
        provenance.get("retrieved_at"), label="Candidate retrieval timestamp"
    )
    expires_at = _format_timestamp(
        retrieved_at + timedelta(seconds=RECEIPT_TTL_SECONDS)
    )
    provenance["anysearch_receipt"] = {
        "version": 1,
        "run_id": run_id,
        "expires_at": expires_at,
        "signature": _receipt_signature(
            candidate, run_id=run_id, expires_at=expires_at, secret=secret
        ),
    }


def _issue_envelope_receipts(
    envelope: dict[str, Any], *, receipt_secret: bytes | None
) -> dict[str, Any]:
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return envelope
    secret = load_receipt_secret() if receipt_secret is None else receipt_secret
    _require_receipt_key(secret)
    run_id = secrets.token_hex(16)
    for candidate in candidates:
        issue_candidate_receipt(candidate, secret=secret, run_id=run_id)
    return envelope


def parse_search_section(markdown: str) -> ParsedSection:
    """Parse one documented AnySearch ``Search Results`` Markdown section.

    This function is deliberately pure.  A documented, explicit zero-result
    header is the only route to a successful empty section.  Missing headers,
    count mismatches, or invalid result blocks always produce parse errors.
    """

    text = (
        markdown.replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
        .lstrip("\ufeff")
    )
    if not text:
        return ParsedSection(
            rows=(),
            reported_count=None,
            elapsed_ms=None,
            status="failed",
            errors=(
                _public_error(
                    "parse_error",
                    "AnySearch returned an empty response instead of search-result Markdown.",
                ),
            ),
        )

    header = SEARCH_HEADER_RE.match(text)
    if header is None:
        return ParsedSection(
            rows=(),
            reported_count=None,
            elapsed_ms=None,
            status="failed",
            errors=(
                _public_error(
                    "parse_error",
                    "AnySearch output did not begin with the documented search-results header.",
                    output_chars=len(text),
                    output_shape=_output_shape(text),
                ),
            ),
        )

    reported_count = int(header.group("count"))
    elapsed_ms = _parse_elapsed_ms(header.group("meta"))
    starts = list(RESULT_START_RE.finditer(text, header.end()))
    rows: list[ParsedRow] = []
    errors: list[dict[str, Any]] = []

    if reported_count == 0:
        remainder = text[header.end() :].strip()
        if remainder not in {"", "---"}:
            errors.append(
                _public_error(
                    "parse_error",
                    "AnySearch returned unexpected content after a zero-results header.",
                )
            )

    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        try:
            url = _clean_url(match.group("url"))
        except ValueError:
            errors.append(
                _public_error(
                    "parse_error",
                    "An AnySearch result block contained an invalid URL.",
                    result_rank=int(match.group("rank")),
                )
            )
            continue
        title = match.group("title").strip()
        if not title:
            errors.append(
                _public_error(
                    "parse_error",
                    "An AnySearch result block was missing its title.",
                    result_rank=int(match.group("rank")),
                )
            )
            continue
        rows.append(
            ParsedRow(
                rank=int(match.group("rank")),
                title=title,
                url=url,
                snippet=_clean_snippet(text[match.end() : end]),
            )
        )

    if reported_count != len(starts):
        errors.append(
            _public_error(
                "parse_error",
                "AnySearch's reported result count did not match its parseable result blocks.",
                backend_reported_count=reported_count,
                parseable_block_count=len(starts),
            )
        )
    if len(rows) != len(starts):
        errors.append(
            _public_error(
                "parse_error",
                "One or more AnySearch result blocks could not be normalized.",
                parseable_block_count=len(starts),
                normalized_count=len(rows),
            )
        )

    if errors:
        status = "partial" if rows else "failed"
    else:
        status = "completed"
    return ParsedSection(
        rows=tuple(rows),
        reported_count=reported_count,
        elapsed_ms=elapsed_ms,
        status=status,
        errors=tuple(errors),
    )


def make_candidate(
    row: ParsedRow,
    *,
    query: str,
    retrieved_at: str,
    route_reason: str,
) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(row.url),
        "query": query,
        "platform": "web",
        "backend": BACKEND,
        "rank": row.rank,
        "title": row.title,
        "url": row.url,
        "canonical_url": row.url,
        "snippet": row.snippet,
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
            "retrieved_at": retrieved_at,
            "route_reason": route_reason,
        },
        "limitations": [
            SEARCH_SNIPPET_LIMITATION,
            *([REDIRECT_WRAPPER_LIMITATION] if is_search_redirect_wrapper(row.url) else []),
        ],
    }


def _route(
    *, mode: str, status: str, reason: str, limitations: Iterable[str]
) -> dict[str, Any]:
    return {
        "platform": "web",
        "backend": BACKEND,
        "mode": mode,
        "reason": reason,
        "login_state_used": False,
        "status": status,
        "limitations": list(limitations),
    }


def normalize_search_markdown(
    markdown: str,
    *,
    query: str,
    requested_limit: int,
    retrieved_at: str,
    route_reason: str = "public_web_default",
    receipt_secret: bytes | None = None,
) -> dict[str, Any]:
    """Normalize one AnySearch search response into the candidate envelope."""

    section = parse_search_section(markdown)
    all_candidates = [
        make_candidate(
            row,
            query=query,
            retrieved_at=retrieved_at,
            route_reason=route_reason,
        )
        for row in section.rows
    ]
    candidates = all_candidates[:requested_limit]
    truncated = len(all_candidates) > len(candidates) or (
        section.reported_count is not None
        and section.reported_count > requested_limit
    )
    limitations = [SEARCH_SNIPPET_LIMITATION]
    coverage = {
        "backend": BACKEND,
        "mode": "search",
        "query_count": 1,
        "returned_count": len(candidates),
        "backend_reported_count": section.reported_count,
        "truncated": truncated,
        "login_state_used": False,
        "parse_status": section.status,
        "limitations": limitations,
    }
    if section.elapsed_ms is not None:
        coverage["elapsed_ms"] = section.elapsed_ms
    envelope = {
        "schema_version": "1.0",
        "request": {
            "queries": [query],
            "platforms": ["web"],
            "time_range": None,
            "requested_limit": requested_limit,
        },
        "routes": [
            _route(
                mode="search",
                status=section.status,
                reason=route_reason,
                limitations=limitations,
            )
        ],
        "candidates": candidates,
        "coverage": [coverage],
        "errors": list(section.errors),
    }
    return _issue_envelope_receipts(envelope, receipt_secret=receipt_secret)


def _query_requested_limit(query_item: dict[str, Any]) -> int:
    value = query_item.get("max_results", 10)
    return value if isinstance(value, int) and not isinstance(value, bool) else 10


def normalize_batch_markdown(
    markdown: str,
    *,
    query_items: list[dict[str, Any]],
    retrieved_at: str,
    receipt_secret: bytes | None = None,
) -> dict[str, Any]:
    """Normalize AnySearch's numbered batch Markdown using submitted queries.

    Query headings are used only to associate a numbered response section with
    its submitted item.  The original submitted query remains authoritative in
    each candidate and in coverage.
    """

    text = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    headings = list(QUERY_SECTION_RE.finditer(text)) if text else []
    section_by_index: dict[int, tuple[str, str]] = {}
    global_errors: list[dict[str, Any]] = []
    observed_indices = [int(heading.group("index")) for heading in headings]
    expected_indices = list(range(1, len(query_items) + 1))
    index_counts: dict[int, int] = {}
    for index in observed_indices:
        index_counts[index] = index_counts.get(index, 0) + 1
    duplicated_indices = {
        index for index, count in index_counts.items() if count > 1
    }

    for index in sorted(duplicated_indices):
        global_errors.append(
            _public_error(
                "parse_error",
                "AnySearch batch output repeated a query section number; all sections with that number were discarded.",
                query_index=index,
            )
        )

    if headings and observed_indices != expected_indices:
        global_errors.append(
            _public_error(
                "parse_error",
                "AnySearch batch output must contain exactly one ordered section for each submitted query.",
                expected_query_count=len(query_items),
                observed_section_count=len(headings),
            )
        )

    for position, heading in enumerate(headings, start=1):
        index = int(heading.group("index"))
        offset = position - 1
        end = headings[offset + 1].start() if offset + 1 < len(headings) else len(text)
        if (
            index in duplicated_indices
            or index not in expected_indices
            or position != index
        ):
            continue
        if index in section_by_index:
            global_errors.append(
                _public_error(
                    "parse_error",
                    "AnySearch batch output repeated a query section number; all sections with that number were discarded.",
                    query_index=index,
                )
            )
            section_by_index.pop(index, None)
            continue
        section_by_index[index] = (
            heading.group("query").strip(),
            text[heading.end() : end],
        )

    if not headings:
        global_errors.append(
            _public_error(
                "parse_error",
                "AnySearch batch output did not contain documented numbered query sections.",
            )
        )

    candidates: list[dict[str, Any]] = []
    per_query: list[dict[str, Any]] = []
    section_statuses: list[str] = []
    errors = list(global_errors)
    total_reported = 0
    all_reported_known = True
    any_truncated = False

    for index, query_item in enumerate(query_items, start=1):
        query = query_item["query"]
        response_section = section_by_index.get(index)
        requested_limit = _query_requested_limit(query_item)
        route_reason = (
            "anysearch_vertical_domain_batch"
            if query_item.get("domain")
            else "public_web_batch"
        )
        if response_section is None:
            section = ParsedSection(
                rows=(),
                reported_count=None,
                elapsed_ms=None,
                status="failed",
                errors=(
                    _public_error(
                        "parse_error",
                        "AnySearch batch output was missing a submitted query section.",
                        query_index=index,
                    ),
                ),
            )
            response_query = None
        else:
            response_query, raw_section = response_section
            section = parse_search_section(raw_section)

        query_candidates = [
            make_candidate(
                row,
                query=query,
                retrieved_at=retrieved_at,
                route_reason=route_reason,
            )
            for row in section.rows
        ]
        emitted = query_candidates[:requested_limit]
        candidates.extend(emitted)
        truncated = len(query_candidates) > len(emitted) or (
            section.reported_count is not None
            and section.reported_count > requested_limit
        )
        any_truncated = any_truncated or truncated
        if section.reported_count is None:
            all_reported_known = False
        else:
            total_reported += section.reported_count

        for error in section.errors:
            enriched = dict(error)
            enriched.setdefault("query_index", index)
            errors.append(enriched)
        section_statuses.append(section.status)
        query_coverage: dict[str, Any] = {
            "query": query,
            "query_index": index,
            "response_query": response_query,
            "returned_count": len(emitted),
            "backend_reported_count": section.reported_count,
            "requested_limit": requested_limit,
            "truncated": truncated,
            "parse_status": section.status,
        }
        if section.elapsed_ms is not None:
            query_coverage["elapsed_ms"] = section.elapsed_ms
        per_query.append(query_coverage)

    extra_indices = sorted(set(observed_indices) - set(expected_indices))
    for index in extra_indices:
        errors.append(
            _public_error(
                "parse_error",
                "AnySearch batch output contained an unexpected query section.",
                query_index=index,
            )
        )

    if not section_statuses or all(status == "failed" for status in section_statuses):
        route_status = "failed"
    elif (
        all(status == "completed" for status in section_statuses)
        and not global_errors
        and not extra_indices
    ):
        route_status = "completed"
    else:
        route_status = "partial"

    requested_limits = [_query_requested_limit(item) for item in query_items]
    common_limit = (
        requested_limits[0]
        if requested_limits and len(set(requested_limits)) == 1
        else None
    )
    limitations = [SEARCH_SNIPPET_LIMITATION]
    coverage = {
        "backend": BACKEND,
        "mode": "batch",
        "query_count": len(query_items),
        "returned_count": len(candidates),
        "backend_reported_count": total_reported if all_reported_known else None,
        "truncated": any_truncated,
        "login_state_used": False,
        "parse_status": route_status,
        "per_query": per_query,
        "limitations": limitations,
    }
    envelope = {
        "schema_version": "1.0",
        "request": {
            "queries": [item["query"] for item in query_items],
            "platforms": ["web"],
            "time_range": None,
            "requested_limit": common_limit,
        },
        "routes": [
            _route(
                mode="batch",
                status=route_status,
                reason="anysearch_batch",
                limitations=limitations,
            )
        ],
        "candidates": candidates,
        "coverage": [coverage],
        "errors": errors,
    }
    return _issue_envelope_receipts(envelope, receipt_secret=receipt_secret)


def _validate_candidate_url(url: Any) -> str:
    try:
        return validate_public_http_url(url)
    except ValueError:
        raise AdapterError(
            "invalid_candidate", "Candidate URL must use public HTTP(S)."
        ) from None


def _validate_search_receipt(
    candidate: dict[str, Any],
    *,
    secret: bytes,
    retrieved_at: datetime,
    now: datetime | None,
) -> None:
    provenance = candidate.get("provenance")
    receipt = provenance.get("anysearch_receipt") if isinstance(provenance, dict) else None
    required_keys = {"version", "run_id", "expires_at", "signature"}
    if not isinstance(receipt, dict) or set(receipt) != required_keys:
        raise AdapterError(
            "invalid_candidate",
            "Candidate is missing a valid current-search receipt.",
        )
    if receipt.get("version") != 1:
        raise AdapterError(
            "invalid_candidate", "Candidate search receipt version is invalid."
        )
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise AdapterError(
            "invalid_candidate", "Candidate search receipt run ID is invalid."
        )
    expires_at_raw = receipt.get("expires_at")
    expires_at = _parse_timestamp(
        expires_at_raw, label="Candidate search receipt expiration"
    )
    if expires_at <= retrieved_at or expires_at > retrieved_at + timedelta(
        seconds=RECEIPT_TTL_SECONDS
    ):
        raise AdapterError(
            "invalid_candidate", "Candidate search receipt lifetime is invalid."
        )
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise AdapterError(
            "invalid_candidate", "Candidate verification time needs a timezone."
        )
    if current.astimezone(timezone.utc) >= expires_at:
        raise AdapterError(
            "invalid_candidate", "Candidate search receipt has expired."
        )
    signature = receipt.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", signature
    ):
        raise AdapterError(
            "invalid_candidate", "Candidate search receipt signature is invalid."
        )
    expected = _receipt_signature(
        candidate,
        run_id=run_id,
        expires_at=expires_at_raw,
        secret=secret,
    )
    if not hmac.compare_digest(signature, expected):
        raise AdapterError(
            "invalid_candidate", "Candidate search receipt signature is invalid."
        )


def validate_candidate_from_search(
    candidate: Any,
    *,
    receipt_secret: bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise AdapterError(
            "invalid_candidate", "--candidate-from-search must contain one candidate object."
        )
    if candidate.get("backend") != BACKEND:
        raise AdapterError(
            "invalid_candidate", "Candidate backend must be anysearch."
        )
    for field in ("candidate_id", "query"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise AdapterError(
                "invalid_candidate", f"Candidate field {field} is required."
            )
    if candidate.get("platform") != "web":
        raise AdapterError(
            "invalid_candidate", "AnySearch candidate platform must be web."
        )
    if (
        isinstance(candidate.get("rank"), bool)
        or not isinstance(candidate.get("rank"), int)
        or candidate["rank"] < 1
    ):
        raise AdapterError(
            "invalid_candidate", "Candidate rank must be a positive integer."
        )
    if not isinstance(candidate.get("title"), str) or not candidate["title"].strip():
        raise AdapterError("invalid_candidate", "Candidate title is required.")
    verification = candidate.get("verification")
    if not isinstance(verification, dict) or verification.get("status") != "candidate":
        raise AdapterError(
            "invalid_candidate", "Candidate verification status must be candidate."
        )
    if verification.get("opened_original") is not False or verification.get(
        "checked_at"
    ) is not None:
        raise AdapterError(
            "invalid_candidate",
            "Candidate must be an unopened current-search result.",
        )
    url = _validate_candidate_url(candidate.get("url"))
    if candidate.get("canonical_url") != url:
        raise AdapterError(
            "invalid_candidate", "Candidate canonical URL must match its search URL."
        )
    if candidate["candidate_id"] != _candidate_id(url):
        raise AdapterError(
            "invalid_candidate", "Candidate ID does not match its search URL."
        )
    access = candidate.get("access")
    if not isinstance(access, dict) or access.get("visibility") != "public":
        raise AdapterError(
            "invalid_candidate", "AnySearch candidate access must be public."
        )
    if access.get("login_state_used") is not False:
        raise AdapterError(
            "invalid_candidate", "AnySearch candidate must not use login state."
        )
    provenance = candidate.get("provenance")
    if not isinstance(provenance, dict) or provenance.get(
        "route_reason"
    ) not in SEARCH_ROUTE_REASONS:
        raise AdapterError(
            "invalid_candidate", "Candidate search provenance is missing or invalid."
        )
    parsed_retrieved_at = _parse_timestamp(
        provenance.get("retrieved_at"), label="Candidate retrieval timestamp"
    )
    secret = load_receipt_secret() if receipt_secret is None else receipt_secret
    _validate_search_receipt(
        candidate,
        secret=_require_receipt_key(secret),
        retrieved_at=parsed_retrieved_at,
        now=now,
    )
    return candidate


def normalize_verified_candidate(
    candidate: dict[str, Any],
    *,
    extract_markdown: str,
    checked_at: str,
    receipt_secret: bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach extract Markdown while deliberately retaining candidate status."""

    validate_candidate_from_search(
        candidate, receipt_secret=receipt_secret, now=now
    )
    markdown = extract_markdown.strip()
    if not markdown:
        raise AdapterError(
            "invalid_response", "AnySearch extract returned no Markdown content."
        )
    # Gates judge the page itself, not the JSON wrapper's URL and escaped newlines.
    page_title, page_text, page_url = split_extract_output(markdown)
    # The sentinel counts only where AnySearch prints it: at the start of the whole
    # output or of the JSON content. A page that quotes it in its body stays readable.
    if any(
        text.lstrip().lower().startswith(EXTRACT_FAILURE_MARKERS)
        for text in (markdown, page_text)
    ):
        raise AdapterError(
            "extract_failed",
            "AnySearch could not extract the original page; treat the original as unread.",
        )
    marker = blocked_page_marker(
        page_text, title=page_title, requested_url=candidate["url"], final_url=page_url
    )
    if marker:
        raise AdapterError(
            "blocked_or_verification",
            f"AnySearch extract returned an anti-bot, verification, or login page "
            f"(matched {marker!r}); treat the original as unread.",
        )
    visible_chars = visible_char_count(page_text)
    if visible_chars < MIN_ORIGINAL_CONTENT_CHARS:
        raise AdapterError(
            "content_too_short",
            f"AnySearch extract returned only {visible_chars} visible characters "
            f"(minimum {MIN_ORIGINAL_CONTENT_CHARS}); treat the original as unread.",
        )

    normalized = copy.deepcopy(candidate)
    normalized["verification"] = dict(normalized.get("verification") or {})
    normalized["verification"].update(
        {
            "status": "candidate",
            "opened_original": True,
            "checked_at": checked_at,
        }
    )

    platform_fields = normalized.get("platform_fields")
    if not isinstance(platform_fields, dict):
        platform_fields = {}
    else:
        platform_fields = copy.deepcopy(platform_fields)
    prior_anysearch = platform_fields.get("anysearch")
    if isinstance(prior_anysearch, dict):
        anysearch_fields = copy.deepcopy(prior_anysearch)
    elif prior_anysearch is None:
        anysearch_fields = {}
    else:
        anysearch_fields = {"prior_value": prior_anysearch}
    # A {"url","title","content"} wrapper is unpacked: the page Markdown is its content,
    # and the page title and the URL extract reports are kept beside it. Other output
    # is already page text and is stored as-is.
    anysearch_fields.update(
        {
            "extract_markdown": page_text.strip(),
            "extract_retrieved_at": checked_at,
        }
    )
    for field, value in (("extract_title", page_title), ("extract_url", page_url)):
        anysearch_fields.pop(field, None)
        if value.strip():
            anysearch_fields[field] = value.strip()
    platform_fields["anysearch"] = anysearch_fields
    normalized["platform_fields"] = platform_fields

    provenance = normalized.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    else:
        provenance = copy.deepcopy(provenance)
    provenance["verification"] = {
        "backend": BACKEND,
        "route_reason": "verify_candidate_from_current_search",
        "source_query": normalized["query"],
        "candidate_id": normalized["candidate_id"],
        "checked_at": checked_at,
    }
    normalized["provenance"] = provenance

    limitations = normalized.get("limitations")
    limitations = list(limitations) if isinstance(limitations, list) else []
    if VERIFY_LIMITATION not in limitations:
        limitations.append(VERIFY_LIMITATION)
    normalized["limitations"] = limitations
    return normalized


def verification_envelope(
    candidate: dict[str, Any],
    *,
    extract_markdown: str,
    checked_at: str,
    receipt_secret: bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized = normalize_verified_candidate(
        candidate,
        extract_markdown=extract_markdown,
        checked_at=checked_at,
        receipt_secret=receipt_secret,
        now=now,
    )
    query = normalized["query"]
    return {
        "schema_version": "1.0",
        "request": {
            "queries": [query],
            "platforms": [normalized.get("platform") or "web"],
            "time_range": None,
            "requested_limit": 1,
        },
        "routes": [
            _route(
                mode="verify",
                status="completed",
                reason="verify_candidate_from_current_search",
                limitations=[VERIFY_LIMITATION],
            )
        ],
        "candidates": [normalized],
        "coverage": [
            {
                "backend": BACKEND,
                "mode": "verify",
                "query_count": 1,
                "returned_count": 1,
                "opened_count": 1,
                "truncated": False,
                "login_state_used": False,
                "limitations": [VERIFY_LIMITATION],
            }
        ],
        "errors": [],
    }


SUB_DOMAIN_ROUTE_REASON = "anysearch_sub_domain_discovery"
SUB_DOMAIN_LIMITATION = (
    "AnySearch's live sub-domain directory. A vertical search must pass every required "
    "parameter of the chosen sub-domain in --sub-domain-params, using \"\" when one does not apply."
)
DOMAIN_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")
SUB_DOMAIN_KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}")
PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}")
# The upstream contract marks each parameter "(required)" or not; full-width
# parentheses and Chinese labels are accepted defensively.
PARAM_MARKER_RE = re.compile(
    r"`?(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,63})`?\**[ \t]*[(（][ \t]*"
    r"(?P<marker>required|optional|必填|可选)[ \t]*[)）]",
    flags=re.IGNORECASE,
)
EMPTY_PARAM_TEXTS = frozenset({"", "-", "—", "–", "none", "n/a", "na", "null", "无", "{}", "[]"})
TABLE_SEPARATOR_RE = re.compile(r"\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?")
TABLE_COLUMNS = {
    "sub_domain": {"sub_domain", "subdomain"},
    "domain": {"domain"},
    "description": {"description", "desc"},
    "query_format": {"query_format", "format"},
    "params": {"params_schema", "params", "parameters", "param", "parameter"},
    "zone": {"zone"},
}
MAX_DIRECTORY_TEXT_CHARS = 60000
MAX_PARAM_TEXT_CHARS = 2000
MAX_NOTICE_CHARS = 1000


def validate_sub_domain_name(value: Any) -> str:
    if not isinstance(value, str) or DOMAIN_NAME_RE.fullmatch(value) is None:
        raise AdapterError(
            "invalid_input", "--domain must match ^[a-z][a-z0-9_]{0,40}$."
        )
    return value


def _interpret_params(value: Any) -> tuple[list[str], list[str], bool]:
    """Return (required, optional, known) for one sub-domain's parameter description."""

    if value is None:
        return [], [], True
    if isinstance(value, str):
        text = value.strip().strip("`").strip()
        if text.casefold() in EMPTY_PARAM_TEXTS:
            return [], [], True
        if text[:1] in "{[":
            try:
                return _interpret_params(json.loads(text))
            except ValueError:
                pass
        required: list[str] = []
        optional: list[str] = []
        for match in PARAM_MARKER_RE.finditer(text):
            target = required if match.group("marker").lower() in {"required", "必填"} else optional
            if match.group("name") not in target:
                target.append(match.group("name"))
        if required or optional:
            return required, [name for name in optional if name not in required], True
        return [], [], False
    if isinstance(value, dict):
        if not value:
            return [], [], True
        if isinstance(value.get("properties"), dict) or isinstance(value.get("required"), list):
            names = list((value.get("properties") or {}).keys())
            listed = value.get("required", [])
            if not isinstance(listed, list) or not all(
                isinstance(name, str) and PARAM_NAME_RE.fullmatch(name) for name in [*names, *listed]
            ):
                return [], [], False
            required = list(dict.fromkeys(listed))
            return required, [name for name in names if name not in required], True
        pairs = list(value.items())
    elif isinstance(value, list):
        if not value:
            return [], [], True
        pairs = [
            (item.get("name"), item) if isinstance(item, dict) else (None, None)
            for item in value
        ]
    else:
        return [], [], False
    required, optional = [], []
    for name, spec in pairs:
        if (
            not isinstance(name, str)
            or PARAM_NAME_RE.fullmatch(name) is None
            or not isinstance(spec, dict)
            or not isinstance(spec.get("required"), bool)
        ):
            return [], [], False
        (required if spec["required"] else optional).append(name)
    return required, optional, True


_PARAMS_MISSING = object()


def _sub_domain_entry(
    *,
    domain: str,
    sub_domain: str,
    description: Any = None,
    query_format: Any = None,
    zone: Any = None,
    params: Any = _PARAMS_MISSING,
) -> dict[str, Any]:
    if params is _PARAMS_MISSING:
        required, optional, known = [], [], False
    else:
        required, optional, known = _interpret_params(params)
    if isinstance(params, str):
        params_text = params.strip() or None
    elif params is _PARAMS_MISSING or params is None:
        params_text = None
    else:
        params_text = json.dumps(params, ensure_ascii=False, separators=(",", ":"))

    def text(value: Any) -> str | None:
        return (value.strip() or None) if isinstance(value, str) else None

    return {
        "domain": domain,
        "sub_domain": sub_domain,
        "description": text(description),
        "query_format": text(query_format),
        "zone": text(zone),
        "required_params": required,
        "optional_params": optional,
        "params_known": known,
        "sub_domain_params_template": {name: "" for name in required} if known else None,
        "params_text": params_text[:MAX_PARAM_TEXT_CHARS] if params_text else None,
    }


def _table_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [
        re.sub(r"<br\s*/?>", "\n", cell.replace("\\|", "|"), flags=re.IGNORECASE).strip()
        for cell in re.split(r"(?<!\\)\|", body)
    ]


def _column_key(header: str) -> str | None:
    name = re.sub(r"[`*_\s-]+", "_", header.strip().lower()).strip("_")
    for key, aliases in TABLE_COLUMNS.items():
        if name in aliases:
            return key
    return None


def _parse_directory_json(payload: Any, domain: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [payload]
    while stack and len(found) < 500:
        item = stack.pop(0)
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            name = item.get("sub_domain")
            if isinstance(name, str):
                found.append(item)
            else:
                stack.extend(item.values())
    entries = []
    for item in found:
        if SUB_DOMAIN_KEY_RE.fullmatch(item["sub_domain"]) is None:
            continue
        params: Any = _PARAMS_MISSING
        for key in ("params", "params_schema", "parameters"):
            if key in item:
                params = item[key]
                break
        entries.append(
            _sub_domain_entry(
                domain=item["domain"] if isinstance(item.get("domain"), str) else domain,
                sub_domain=item["sub_domain"],
                description=item.get("description"),
                query_format=item.get("query_format"),
                zone=item.get("zone"),
                params=params,
            )
        )
    return entries


def _parse_directory_tables(
    text: str, domain: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    found_table = False
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if not (
            "|" in line
            and index + 1 < len(lines)
            and TABLE_SEPARATOR_RE.fullmatch(lines[index + 1].strip())
        ):
            index += 1
            continue
        columns: dict[str, int] = {}
        for position, header in enumerate(_table_cells(line)):
            key = _column_key(header)
            if key is not None and key not in columns:
                columns[key] = position
        index += 2
        rows = []
        while index < len(lines) and lines[index].strip() and "|" in lines[index]:
            rows.append(lines[index])
            index += 1
        if "sub_domain" not in columns:
            continue
        found_table = True
        for row_number, row in enumerate(rows, start=1):
            cells = _table_cells(row)

            def cell(key: str) -> str:
                position = columns.get(key)
                return cells[position] if position is not None and position < len(cells) else ""

            name = cell("sub_domain").strip("`* ")
            if SUB_DOMAIN_KEY_RE.fullmatch(name) is None:
                errors.append(
                    _public_error(
                        "parse_error",
                        "A sub-domain table row had no valid sub_domain value.",
                        table_row=row_number,
                    )
                )
                continue
            entries.append(
                _sub_domain_entry(
                    domain=cell("domain").strip("`* ") or domain,
                    sub_domain=name,
                    description=cell("description"),
                    query_format=cell("query_format"),
                    zone=cell("zone"),
                    params=cell("params") if "params" in columns else _PARAMS_MISSING,
                )
            )
    return entries, errors, found_table


# Layout returned by the live service (observed 2026-10-31 for legal and finance):
#   ## <domain> Domain Capabilities (N available)
#   ### <domain>.<sub>
#   <description>
#   **Parameters:**
#   - `name` (required): ...
#   - `name`: ...
# Only a "(required)" marker directly after a bullet's backticked name counts;
# markers inside a description (e.g. "type=crypto: e.g., `BTCUSD` (required)")
# describe value rules, not parameters.
SECTION_PARAMS_HEADER_RE = re.compile(
    r"[ \t]*(?:\*\*|__)?[ \t]*(?:parameters|params|参数)[ \t]*[:：]?[ \t]*(?:\*\*|__)?[ \t]*[:：]?[ \t]*",
    flags=re.IGNORECASE,
)
SECTION_PARAM_LINE_RE = re.compile(
    r"[ \t]{0,3}[-*+][ \t]+`(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,63})`"
    r"(?:[ \t]*[(（][ \t]*(?P<marker>required|optional|必填|可选)[ \t]*[)）])?[ \t]*[:：]",
    flags=re.IGNORECASE,
)
SECTION_TOP_BULLET_RE = re.compile(r"[ \t]{0,3}[-*+][ \t]+\S")


def _parse_directory_sections(
    text: str, domain: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse the heading + ``**Parameters:**`` bullet layout of ``get_sub_domains``."""

    heading = re.compile(
        r"(?m)^[ \t]*#{1,6}[ \t]+`?(?P<name>"
        + re.escape(domain)
        + r"\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)`?[ \t]*$"
    )
    any_heading = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+\S")
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in heading.finditer(text):
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        following = any_heading.search(text, match.end())
        block = text[match.end() : following.start() if following else len(text)]
        lines = block.split("\n")
        header_index = next(
            (i for i, line in enumerate(lines) if SECTION_PARAMS_HEADER_RE.fullmatch(line)), None
        )
        description_lines = lines if header_index is None else lines[:header_index]
        description = " ".join(line.strip() for line in description_lines if line.strip()) or None
        if header_index is None:
            entries.append(
                _sub_domain_entry(domain=domain, sub_domain=name, description=description)
            )
            continue
        params_lines = lines[header_index + 1 :]
        params: list[dict[str, Any]] = []
        unparsed_bullets = 0
        for line in params_lines:
            param = SECTION_PARAM_LINE_RE.match(line)
            if param is not None:
                marker = (param.group("marker") or "").lower()
                if param.group("name") not in {item["name"] for item in params}:
                    params.append(
                        {"name": param.group("name"), "required": marker in {"required", "必填"}}
                    )
            elif SECTION_TOP_BULLET_RE.match(line):
                unparsed_bullets += 1
        params_text = "\n".join(params_lines).strip()
        if unparsed_bullets or (not params and params_text.strip("`").strip().casefold() not in EMPTY_PARAM_TEXTS):
            entry = _sub_domain_entry(domain=domain, sub_domain=name, description=description)
        else:
            entry = _sub_domain_entry(
                domain=domain, sub_domain=name, description=description, params=params
            )
        entry["params_text"] = params_text[:MAX_PARAM_TEXT_CHARS] or None
        entries.append(entry)
    announced = re.search(
        r"(?im)^[ \t]*#{1,6}[ \t]+"
        + re.escape(domain)
        + r"[ \t]+domain[ \t]+capabilities[ \t]*\([ \t]*(?P<count>\d+)[ \t]+available[ \t]*\)",
        text,
    )
    if entries and announced is not None and int(announced.group("count")) != len(entries):
        errors.append(
            _public_error(
                "parse_error",
                "The sub-domain directory announced a different number of sub-domains than "
                "were parsed; check directory_text.",
                announced_count=int(announced.group("count")),
                parsed_count=len(entries),
            )
        )
    return entries, errors


def _parse_directory_listing(text: str, domain: str) -> list[dict[str, Any]]:
    """Last resort for undocumented layouts: headings or list items naming ``<domain>.x``."""

    starts = list(
        re.finditer(
            r"(?m)^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+\.[ \t]+)?[`*]*(?P<name>"
            + re.escape(domain)
            + r"\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)\b",
            text,
        )
    )
    entries = []
    seen: set[str] = set()
    for position, match in enumerate(starts):
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        end = starts[position + 1].start() if position + 1 < len(starts) else len(text)
        block = text[match.end() : end]
        has_markers = PARAM_MARKER_RE.search(block) is not None
        entries.append(
            _sub_domain_entry(
                domain=domain,
                sub_domain=name,
                params=block if has_markers else _PARAMS_MISSING,
            )
        )
    return entries


def sub_domains_envelope(
    *,
    domain: str | None,
    status: str,
    sub_domains: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    directory_text: str | None = None,
) -> dict[str, Any]:
    limitations = [SUB_DOMAIN_LIMITATION]
    truncated = directory_text is not None and len(directory_text) > MAX_DIRECTORY_TEXT_CHARS
    return {
        "schema_version": "1.0",
        "request": {
            "queries": [],
            "platforms": ["web"],
            "time_range": None,
            "requested_limit": None,
            "domain": domain,
        },
        "status": status,
        "domain": domain,
        "routes": [
            _route(
                mode="sub-domains",
                status=status,
                reason=SUB_DOMAIN_ROUTE_REASON,
                limitations=limitations,
            )
        ],
        "sub_domains": sub_domains,
        "candidates": [],
        "coverage": [
            {
                "backend": BACKEND,
                "mode": "sub-domains",
                "domain": domain,
                "query_count": 0,
                "returned_count": 0,
                "sub_domain_count": len(sub_domains),
                "truncated": truncated,
                "login_state_used": False,
                "parse_status": status,
                "limitations": limitations,
            }
        ],
        "errors": list(errors),
        "directory_text": (
            directory_text[:MAX_DIRECTORY_TEXT_CHARS] if directory_text is not None else None
        ),
        "directory_text_truncated": truncated,
    }


def normalize_sub_domain_directory(text: str, *, domain: str) -> dict[str, Any]:
    """Turn ``get_sub_domains`` output into sub-domains plus their required parameters.

    Two layouts are understood: a Markdown table (domain, sub_domain, description,
    query_format, params_schema, zone) whose parameters are marked ``(required)``,
    and the live service's ``### <domain>.<sub>`` sections with a ``**Parameters:**``
    bullet list where a parameter is required only when ``(required)`` follows its
    backticked name. Anything less certain is reported as ``partial`` or ``failed``
    with the directory text attached, never as a complete directory.
    """

    domain = validate_sub_domain_name(domain)
    raw = text.replace("\r\n", "\n").replace("\r", "\n").strip().lstrip("﻿")
    if not raw:
        return sub_domains_envelope(
            domain=domain,
            status="failed",
            sub_domains=[],
            errors=[
                _public_error("parse_error", "AnySearch returned an empty sub-domain directory.")
            ],
        )
    errors: list[dict[str, Any]] = []
    documented = True
    entries: list[dict[str, Any]] = []
    if raw[:1] in "{[":
        try:
            entries = _parse_directory_json(json.loads(raw), domain)
        except ValueError:
            entries = []
    if not entries:
        entries, errors, documented = _parse_directory_tables(raw, domain)
    if not entries:
        entries, errors = _parse_directory_sections(raw, domain)
        documented = True
    if not entries:
        entries = _parse_directory_listing(raw, domain)
        documented = False
        errors = []
    unique: dict[str, dict[str, Any]] = {}
    for entry in entries:
        unique.setdefault(entry["sub_domain"], entry)
    entries = list(unique.values())

    if not entries:
        if len(raw) <= MAX_NOTICE_CHARS:
            category, message = _failure_category(raw, "")
            if category in {"quota_exhausted", "rate_limited", "authentication_error"}:
                raise AdapterError(category, message)
        errors.append(
            _public_error(
                "parse_error",
                "AnySearch's sub-domain directory had no parseable sub-domain entries; "
                "read directory_text before any vertical search.",
                output_chars=len(raw),
                output_shape=_output_shape(raw),
            )
        )
        status = "failed"
    else:
        if not documented:
            errors.append(
                _public_error(
                    "parse_error",
                    "AnySearch's sub-domain directory was not in the documented table format; "
                    "sub-domains were read from headings or list items, so check directory_text.",
                )
            )
        unknown = [entry["sub_domain"] for entry in entries if not entry["params_known"]]
        if unknown:
            errors.append(
                _public_error(
                    "parse_error",
                    "Required parameters of some sub-domains could not be determined; "
                    "read their params_text or directory_text.",
                    sub_domains=unknown,
                )
            )
        status = "partial" if errors else "completed"
    return sub_domains_envelope(
        domain=domain,
        status=status,
        sub_domains=entries,
        errors=errors,
        directory_text=raw,
    )


def read_runtime_command(runtime_conf: Path) -> list[str]:
    """Read and split the configured AnySearch command without evaluating it."""

    try:
        text = runtime_conf.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise AdapterError(
            "runtime_unavailable", "AnySearch runtime configuration could not be read."
        ) from exc
    command_values = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.split(":", 1)[0].strip().lower() == "command" and ":" in line
    ]
    command_values = [value for value in command_values if value]
    if len(command_values) != 1:
        raise AdapterError(
            "runtime_unavailable",
            "AnySearch runtime configuration must contain exactly one Command value.",
        )
    try:
        command = shlex.split(command_values[0], posix=True)
    except ValueError as exc:
        raise AdapterError(
            "runtime_unavailable", "AnySearch Command could not be parsed safely."
        ) from exc
    if not command or any("\x00" in token for token in command):
        raise AdapterError(
            "runtime_unavailable", "AnySearch Command is empty or invalid."
        )
    return command


def prepare_runtime_invocation(
    command: list[str],
) -> tuple[list[str], dict[str, str] | None]:
    """Move ``env KEY=value`` prefixes out of process argv.

    ``runtime.conf`` is trusted configuration, but its values must not become
    visible in a child command line or in ``TimeoutExpired.cmd``.  The standard
    ``env`` prefix used by the AnySearch skill is therefore represented through
    ``subprocess``'s environment mapping instead.
    """

    argv = list(command)
    child_env = safe_child_environment()
    if Path(argv[0]).name == "env":
        overrides: dict[str, str] = {}
        index = 1
        while index < len(argv):
            token = argv[index]
            if token.startswith("-"):
                raise AdapterError(
                    "runtime_unavailable",
                    "AnySearch Command uses unsupported env options.",
                )
            name, separator, value = token.partition("=")
            if not separator:
                break
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise AdapterError(
                    "runtime_unavailable",
                    "AnySearch Command contains an invalid environment assignment.",
                )
            overrides[name] = value
            index += 1
        argv = argv[index:]
        if not argv:
            raise AdapterError(
                "runtime_unavailable", "AnySearch Command has no executable."
            )
        if overrides:
            child_env.update(overrides)

    lowered = [token.casefold() for token in argv]
    for index, token in enumerate(lowered):
        if token in {"--api_key", "--api-key"} or token.startswith(
            ("--api_key=", "--api-key=")
        ):
            raise AdapterError(
                "runtime_unavailable",
                "AnySearch credentials must be provided through the environment, not Command argv.",
            )
        if index == 0 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
            raise AdapterError(
                "runtime_unavailable",
                "AnySearch environment assignments require an env prefix.",
            )
    return argv, child_env


# Status codes are read from the CLI's own wording ("HTTP Error: 429 Client Error: ...",
# "HTTP 401") with word boundaries, so request IDs and other digits are never codes.
HTTP_STATUS_RES = (
    re.compile(r"\bhttp(?:/\d(?:\.\d)?)?(?:[ \t]+error)?[ \t]*:?[ \t]*(?P<code>[1-5]\d\d)\b"),
    re.compile(r"\b(?P<code>[45]\d\d)[ \t]+(?:client|server)[ \t]+error\b"),
    re.compile(r"\bstatus(?:[ _]code)?[ \t]*[:=]?[ \t]*(?P<code>[1-5]\d\d)\b"),
)
# Explicit exhaustion of the account's quota or balance: the only wording reported
# as quota_exhausted.
QUOTA_EXHAUSTED_SIGNALS = (
    "you've hit your usage limit",
    "you have hit your usage limit",
    "usage limit exceeded",
    "usage limit reached",
    "quota exhausted",
    "quota has been exhausted",
    "insufficient quota",
    "insufficient_quota",
    "daily limit reached",
    "monthly limit reached",
    "credits exhausted",
    "out of credits",
    "insufficient credits",
    "credit balance is too low",
    "insufficient balance",
)
# Weaker quota wording counts as exhaustion only without an HTTP 429.
QUOTA_EXCEEDED_SIGNALS = ("quota exceeded", "quota limit reached", "exceeded your quota")
# A short request window ("per-minute quota exceeded") is a rate limit, never exhaustion.
SHORT_WINDOW_RE = re.compile(
    r"\bper[ \t-]?(?:second|minute)\b|\b(?:requests?|calls?)[ \t]+per[ \t]+(?:second|minute)\b"
    r"|\b(?:rpm|rps|qps)\b|\bconcurren(?:cy|t requests?)\b"
)
ARGPARSE_ERROR_RE = re.compile(r"(?m)^usage: [^\n]*$[\s\S]*^[^\n]*: error: ")
QUOTA_NOTICE_COMMANDS = frozenset({"search", "batch_search"})


def _http_status(signal: str) -> int | None:
    for pattern in HTTP_STATUS_RES:
        match = pattern.search(signal)
        if match:
            return int(match.group("code"))
    return None


def _failure_category(stdout: str, stderr: str) -> tuple[str, str]:
    """Classify captured output while returning only fixed, redacted messages.

    A parsed HTTP status decides first. 429 stays ``rate_limited`` unless the text
    explicitly says the quota or balance is exhausted; a bare 402 is
    ``access_unavailable`` (it can also mean a plan lacks the feature), and only
    402 with explicit balance/credit exhaustion becomes ``quota_exhausted``.
    """

    signal = f"{stdout}\n{stderr}".casefold()
    quota = ("quota_exhausted", "AnySearch usage quota is exhausted.")
    rate = ("rate_limited", "AnySearch request was rate limited.")
    if ARGPARSE_ERROR_RE.search(signal):
        return (
            "invalid_input",
            "AnySearch rejected the adapter's arguments (for example an unsupported domain).",
        )
    status = _http_status(signal)
    short_window = SHORT_WINDOW_RE.search(signal) is not None
    exhausted = not short_window and any(token in signal for token in QUOTA_EXHAUSTED_SIGNALS)
    exceeded = not short_window and any(token in signal for token in QUOTA_EXCEEDED_SIGNALS)
    if status == 429:
        return quota if exhausted else rate
    if status == 402:
        if exhausted:
            return quota
        return (
            "access_unavailable",
            "AnySearch requires payment or a plan change for this request.",
        )
    if status == 401:
        return "authentication_error", "AnySearch authentication failed."
    if status in {408, 504}:
        return "timeout", "AnySearch did not respond before the timeout."
    if exhausted or exceeded:
        return quota
    if status is not None and status >= 500:
        return "upstream_error", "AnySearch returned an unsuccessful response."
    if short_window or any(
        token in signal for token in ("rate limit", "rate-limit", "too many requests")
    ):
        return rate
    if any(token in signal for token in ("unauthorized", "authentication", "invalid api key")):
        return "authentication_error", "AnySearch authentication failed."
    if any(
        token in signal
        for token in ("connection error", "unable to reach", "name resolution", "network")
    ):
        return "network_error", "AnySearch could not be reached."
    if "timeout" in signal or "timed out" in signal:
        return "timeout", "AnySearch did not respond before the timeout."
    return "upstream_error", "AnySearch returned an unsuccessful response."


def run_anysearch(runtime_conf: Path, argv: list[str], timeout: int) -> str:
    """Execute the configured CLI as argv, capturing all potentially sensitive text."""

    command, child_env = prepare_runtime_invocation(
        read_runtime_command(runtime_conf)
    )
    child_env.pop(RECEIPT_KEY_ENV, None)
    child_env.pop(RECEIPT_KEY_FILE_ENV, None)
    failure: tuple[str, str] | None = None
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            [*command, *argv],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        failure = ("timeout", "AnySearch did not respond before the timeout.")
    except (OSError, ValueError):
        failure = (
            "runtime_unavailable",
            "AnySearch runtime could not be started.",
        )
    if failure is not None:
        raise AdapterError(*failure)
    if result is None:
        raise AdapterError(
            "runtime_unavailable", "AnySearch runtime did not return a result."
        )
    if result.returncode != 0:
        category, message = _failure_category(result.stdout, result.stderr)
        raise AdapterError(category, message)
    # Only search commands answer a spent quota with a bare notice on stdout. Extract
    # and directory output is page or catalogue text: an article that mentions
    # "quota exceeded" must not be reported as AnySearch's own quota.
    if (
        argv
        and argv[0] in QUOTA_NOTICE_COMMANDS
        and SEARCH_HEADER_RE.search(result.stdout) is None
    ):
        category, message = _failure_category(result.stdout, "")
        if category == "quota_exhausted":
            raise AdapterError(category, message)
    return result.stdout


def _read_json_argument(value: str, *, label: str) -> Any:
    raw = value
    if value.startswith("@"):
        path_text = value[1:]
        if not path_text:
            raise AdapterError("invalid_input", f"{label} file path is missing.")
        path = Path(path_text).expanduser()
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise AdapterError("invalid_input", f"{label} file is too large.")
            raw = path.read_text(encoding="utf-8-sig")
        except AdapterError:
            raise
        except OSError as exc:
            raise AdapterError("invalid_input", f"{label} file could not be read.") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError("invalid_input", f"{label} must be valid JSON.") from exc


def parse_batch_queries(value: str) -> list[dict[str, Any]]:
    payload = _read_json_argument(value, label="Batch queries")
    if not isinstance(payload, list) or not 1 <= len(payload) <= 5:
        raise AdapterError(
            "invalid_input", "Batch queries must be a JSON list containing 1 to 5 items."
        )
    queries: list[dict[str, Any]] = []
    for index, raw_item in enumerate(payload, start=1):
        if isinstance(raw_item, str):
            item: dict[str, Any] = {"query": raw_item}
        elif isinstance(raw_item, dict):
            item = copy.deepcopy(raw_item)
        else:
            raise AdapterError(
                "invalid_input", f"Batch query item {index} must be an object or string."
            )
        unknown = set(item) - ALLOWED_BATCH_KEYS
        if unknown:
            raise AdapterError(
                "invalid_input", f"Batch query item {index} contains unsupported fields."
            )
        if not isinstance(item.get("query"), str) or not item["query"].strip():
            raise AdapterError(
                "invalid_input", f"Batch query item {index} requires a query string."
            )
        if "max_results" in item and (
            isinstance(item["max_results"], bool)
            or not isinstance(item["max_results"], int)
            or not 1 <= item["max_results"] <= 10
        ):
            raise AdapterError(
                "invalid_input", f"Batch query item {index} max_results must be 1 to 10."
            )
        for field in ("domain", "sub_domain"):
            if field in item and (
                not isinstance(item[field], str) or not item[field].strip()
            ):
                raise AdapterError(
                    "invalid_input", f"Batch query item {index} {field} must be a string."
                )
        if "sub_domain_params" in item and not isinstance(
            item["sub_domain_params"], dict
        ):
            raise AdapterError(
                "invalid_input",
                f"Batch query item {index} sub_domain_params must be an object.",
            )
        queries.append(item)
    return queries


def load_candidate_argument(
    value: str,
    *,
    receipt_secret: bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    candidate = _read_json_argument(value, label="Candidate")
    return validate_candidate_from_search(
        candidate, receipt_secret=receipt_secret, now=now
    )


def _validate_sub_domain_params(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "invalid_input", "--sub-domain-params must be valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AdapterError(
            "invalid_input", "--sub-domain-params must contain a JSON object."
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def error_envelope(
    *,
    queries: list[str],
    mode: str,
    category: str,
    message: str,
    requested_limit: int | None,
    route_reason: str | None = None,
) -> dict[str, Any]:
    reason = route_reason or {
        "search": "public_web_default",
        "batch": "anysearch_batch",
        "verify": "verify_candidate_from_current_search",
    }.get(mode, "anysearch_adapter")
    limitations = [SEARCH_SNIPPET_LIMITATION] if mode != "verify" else [VERIFY_LIMITATION]
    return {
        "schema_version": "1.0",
        "request": {
            "queries": queries,
            "platforms": ["web"],
            "time_range": None,
            "requested_limit": requested_limit,
        },
        "routes": [
            _route(
                mode=mode,
                status="failed",
                reason=reason,
                limitations=limitations,
            )
        ],
        "candidates": [],
        "coverage": [
            {
                "backend": BACKEND,
                "mode": mode,
                "query_count": len(queries),
                "returned_count": 0,
                "truncated": False,
                "login_state_used": False,
                "parse_status": "failed",
                "limitations": limitations,
            }
        ],
        "errors": [_public_error(category, message)],
    }


def execute(
    args: argparse.Namespace,
    runner: Runner = run_anysearch,
    *,
    receipt_secret: bytes | None = None,
) -> dict[str, Any]:
    runtime_conf = Path(args.runtime_conf).expanduser()
    if not runtime_conf.is_absolute():
        raise AdapterError(
            "configuration_error",
            "AnySearch runtime configuration path must be absolute.",
        )
    if args.command == "search":
        route_reason = (
            "anysearch_vertical_domain" if args.domain else "public_web_default"
        )
        argv = ["search", "--max_results", str(args.limit)]
        if args.domain:
            argv.extend(["--domain", args.domain])
        if args.sub_domain:
            argv.extend(["--sub_domain", args.sub_domain])
        params = _validate_sub_domain_params(args.sub_domain_params)
        if params is not None:
            argv.extend(["--sub_domain_params", params])
        # Options first, then "--": a query such as "-Wall" or "--domain=x" stays the query.
        argv.extend(["--", args.query])
        markdown = runner(runtime_conf, argv, args.timeout)
        return normalize_search_markdown(
            markdown,
            query=args.query,
            requested_limit=args.limit,
            retrieved_at=utc_now_iso(),
            route_reason=route_reason,
            receipt_secret=receipt_secret,
        )

    if args.command == "batch":
        query_items = parse_batch_queries(args.queries)
        payload = json.dumps(query_items, ensure_ascii=False, separators=(",", ":"))
        markdown = runner(
            runtime_conf, ["batch_search", "--queries", payload], args.timeout
        )
        return normalize_batch_markdown(
            markdown,
            query_items=query_items,
            retrieved_at=utc_now_iso(),
            receipt_secret=receipt_secret,
        )

    if args.command == "verify":
        secret = load_receipt_secret() if receipt_secret is None else receipt_secret
        candidate = load_candidate_argument(
            args.candidate_from_search, receipt_secret=secret
        )
        markdown = runner(runtime_conf, ["extract", candidate["url"]], args.timeout)
        return verification_envelope(
            candidate,
            extract_markdown=markdown,
            checked_at=utc_now_iso(),
            receipt_secret=secret,
        )

    if args.command == "sub-domains":
        domain = validate_sub_domain_name(args.domain)
        directory = runner(runtime_conf, ["get_sub_domains", "--domain", domain], args.timeout)
        return normalize_sub_domain_directory(directory, domain=domain)

    raise AdapterError("invalid_input", "Unsupported adapter command.")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--runtime-conf",
        default=str(DEFAULT_RUNTIME_CONF),
        help="AnySearch runtime.conf path.",
    )
    common.add_argument("--timeout", type=int, default=45)

    parser = argparse.ArgumentParser(
        description="Run AnySearch and emit unified-search candidate JSON."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", parents=[common])
    search.add_argument("query")
    search.add_argument(
        "--limit", "--max-results", "--max_results", dest="limit", type=int, default=10
    )
    search.add_argument("--domain")
    search.add_argument("--sub-domain", "--sub_domain", dest="sub_domain")
    search.add_argument(
        "--sub-domain-params",
        "--sub_domain_params",
        dest="sub_domain_params",
    )

    batch = subparsers.add_parser("batch", parents=[common])
    batch.add_argument("--queries", required=True)

    verify = subparsers.add_parser("verify", parents=[common])
    verify.add_argument(
        "--candidate-from-search",
        required=True,
        help="One current-search AnySearch candidate as JSON, or @file.",
    )

    sub_domains = subparsers.add_parser(
        "sub-domains",
        parents=[common],
        help="Discover a vertical domain's sub-domains and their required parameters.",
    )
    sub_domains.add_argument(
        "--domain", required=True, help="Vertical domain, matching ^[a-z][a-z0-9_]{0,40}$."
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120")
    if args.command == "search":
        if not args.query.strip():
            parser.error("query must not be empty")
        if not 1 <= args.limit <= 10:
            parser.error("--limit must be between 1 and 10")
        if (args.sub_domain or args.sub_domain_params) and not args.domain:
            parser.error("--sub-domain and --sub-domain-params require --domain")
    return args


def _request_context(args: argparse.Namespace) -> tuple[list[str], int | None]:
    if args.command == "search":
        return [args.query], args.limit
    if args.command == "batch":
        try:
            items = parse_batch_queries(args.queries)
        except AdapterError:
            return [], None
        limits = [_query_requested_limit(item) for item in items]
        common = limits[0] if limits and len(set(limits)) == 1 else None
        return [item["query"] for item in items], common
    if args.command == "verify":
        try:
            candidate = load_candidate_argument(args.candidate_from_search)
        except AdapterError:
            return [], 1
        return [candidate["query"]], 1
    return [], None


def _route_reason_for_args(args: argparse.Namespace) -> str:
    if args.command == "search":
        return "anysearch_vertical_domain" if args.domain else "public_web_default"
    if args.command == "batch":
        return "anysearch_batch"
    if args.command == "verify":
        return "verify_candidate_from_current_search"
    if args.command == "sub-domains":
        return SUB_DOMAIN_ROUTE_REASON
    return "anysearch_adapter"


def _failure_result(args: argparse.Namespace, category: str, message: str) -> dict[str, Any]:
    if args.command == "sub-domains":
        valid = isinstance(args.domain, str) and DOMAIN_NAME_RE.fullmatch(args.domain)
        domain = args.domain if valid else None
        return sub_domains_envelope(
            domain=domain,
            status="failed",
            sub_domains=[],
            errors=[_public_error(category, message)],
        )
    queries, requested_limit = _request_context(args)
    return error_envelope(
        queries=queries,
        mode=args.command,
        category=category,
        message=message,
        requested_limit=requested_limit,
        route_reason=_route_reason_for_args(args),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = execute(args)
    except AdapterError as exc:
        result = _failure_result(args, exc.category, exc.message)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    except Exception:
        result = _failure_result(
            args,
            "internal_error",
            "The AnySearch adapter failed without exposing captured runtime output.",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["routes"][0]["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
