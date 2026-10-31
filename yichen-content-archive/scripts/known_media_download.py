#!/usr/bin/env python3
"""Download media from one user-supplied public HTTPS URL without login state."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_MEDIA_BYTES = 4 * 1024 * 1024 * 1024
USER_AGENT = "yichen-content-archive/1.0 (+known public media downloader)"
X_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
X_MEDIA_HOSTS = {"video.twimg.com"}
X_STATUS_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
GENERIC_FORMAT = "bv*+ba/b"
# --socket-timeout only bounds each socket wait; these bound the whole yt-dlp run.
GENERIC_PREFLIGHT_SECONDS = 300
GENERIC_DOWNLOAD_SECONDS = 3 * 60 * 60
YT_DLP_POLL_SECONDS = 2.0


class KnownMediaError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        output_dir: Path | None = None,
        metadata_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.output_dir = output_dir
        self.metadata_path = metadata_path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_x_reader() -> Any:
    path = Path(__file__).with_name("x_known_url.py")
    spec = importlib.util.spec_from_file_location("known_media_x_reader", path)
    if spec is None or spec.loader is None:
        raise KnownMediaError("backend_unavailable", "The public X reader is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def safe_source_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    authority = host if port in {None, 443} else f"{host}:{port}"
    return urllib.parse.urlunsplit(("https", authority, parsed.path or "/", "", ""))


FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
# IPv6 forms that carry an IPv4 address in their last 32 bits. Python 3.9's
# ipaddress reports the translated and NAT64 forms as global even when the
# embedded IPv4 is private, so the embedded address is judged instead.
IPV4_TRANSLATED_NETWORK = ipaddress.ip_network("::ffff:0:0:0/96")
NAT64_WELL_KNOWN_NETWORK = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL_NETWORK = ipaddress.ip_network("64:ff9b:1::/48")
NAT64_LOCAL_96_NETWORK = ipaddress.ip_network("64:ff9b:1::/96")

# Used only when every local DNS answer is a fake-IP proxy address. The lookup
# itself still travels through the existing proxy; only the answer is checked.
DOH_RESOLVERS = (
    "https://dns.alidns.com/resolve",
    "https://cloudflare-dns.com/dns-query",
)
DOH_RECORD_TYPES = ((1, 4), (28, 6))  # (DNS type, IP version): A, AAAA
DOH_TIMEOUT_SECONDS = 5
DOH_MAX_BYTES = 64 * 1024
DOH_CONTENT_TYPES = {"application/json", "application/dns-json"}
DNS_CHECK_DOH = "doh_verified_behind_fake_ip"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        return None


DOH_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _effective_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address whose reachability matters; None when it cannot be determined."""
    if address.version == 4:
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if (
        address in IPV4_TRANSLATED_NETWORK
        or address in NAT64_WELL_KNOWN_NETWORK
        or address in NAT64_LOCAL_96_NETWORK
    ):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address in NAT64_LOCAL_NETWORK:
        # Local-use NAT64 with a non-/96 prefix: the IPv4 position is not knowable here.
        return None
    if address.sixtofour is not None:
        return address.sixtofour
    return address


def _require_global_ip(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise KnownMediaError("invalid_host", "The media host resolved incorrectly.") from exc
    effective = _effective_address(address)
    if effective is None or not effective.is_global:
        raise KnownMediaError("non_public_host", "Only public internet hosts are accepted.")


class _DohFailure(Exception):
    """A DNS-over-HTTPS answer that cannot be trusted; the next resolver is tried."""


def doh_open(request: urllib.request.Request, timeout: float) -> Any:
    """Network seam for DNS-over-HTTPS lookups; tests replace this function."""
    return DOH_OPENER.open(request, timeout=timeout)


def _doh_query(endpoint: str, host: str, record_type: int, version: int) -> list[str]:
    query = urllib.parse.urlencode({"name": host, "type": record_type})
    request = urllib.request.Request(
        f"{endpoint}?{query}",
        headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with doh_open(request, DOH_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            content_type = (
                response.headers.get_content_type() if response.headers.get("Content-Type") else ""
            )
            body = response.read(DOH_MAX_BYTES + 1)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # URLError/HTTPError, socket.timeout and resets are OSError subclasses.
        raise _DohFailure("transport") from exc
    if status != 200 or content_type not in DOH_CONTENT_TYPES or len(body) > DOH_MAX_BYTES:
        raise _DohFailure("response")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _DohFailure("json") from exc
    if not isinstance(payload, dict) or payload.get("Status") != 0:
        raise _DohFailure("status")
    answers = payload.get("Answer") or []
    if not isinstance(answers, list):
        raise _DohFailure("answer")
    addresses: list[str] = []
    for item in answers:
        if not isinstance(item, dict) or item.get("type") != record_type:
            continue  # CNAME and other records in the chain
        data = item.get("data")
        try:
            parsed = ipaddress.ip_address(data.strip()) if isinstance(data, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.version != version:
            raise _DohFailure("address")
        addresses.append(str(parsed))
    return addresses


def doh_addresses(host: str) -> set[str] | None:
    """A and AAAA answers from the first DoH resolver that answers both; None if none does."""
    for endpoint in DOH_RESOLVERS:
        try:
            addresses: set[str] = set()
            for record_type, version in DOH_RECORD_TYPES:
                addresses.update(_doh_query(endpoint, host, record_type, version))
        except _DohFailure:
            continue
        if addresses:
            return addresses
    return None


def _check_behind_fake_ip(host: str) -> str:
    real = doh_addresses(host)
    if not real:
        raise KnownMediaError(
            "non_public_host",
            "Local DNS answered only from 198.18.0.0/15, which a fake-IP proxy uses for "
            "every domain, and DNS-over-HTTPS could not confirm the host's real address. "
            "Turn off the proxy's fake-IP mode (or use direct DNS) and retry.",
        )
    for address in real:
        try:
            _require_global_ip(address)
        except KnownMediaError as exc:
            raise KnownMediaError(
                "non_public_host",
                "Local DNS answered from a fake-IP range and DNS-over-HTTPS returned a "
                "non-public address for this host.",
            ) from exc
    return DNS_CHECK_DOH


def validate_public_https_url(
    value: str,
    *,
    resolve_dns: bool = True,
    allow_any_port: bool = False,
    dns_checks: dict[str, str] | None = None,
) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise KnownMediaError("invalid_url", "The supplied media URL is invalid.") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise KnownMediaError(
            "unsupported_url",
            "Only explicit public HTTPS URLs without embedded credentials are accepted.",
        )
    if port not in {None, 443} and not allow_any_port:
        raise KnownMediaError("unsupported_port", "Only the standard HTTPS port is accepted.")
    if host == "localhost" or host.endswith(".local"):
        raise KnownMediaError("non_public_host", "Local hosts are not accepted.")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise KnownMediaError("invalid_host", "The media host is invalid.") from exc
    try:
        _require_global_ip(ascii_host)
        is_ip = True
    except KnownMediaError as exc:
        if exc.category == "non_public_host":
            raise
        is_ip = False
    if is_ip:
        mode = "ip_literal"
    elif not resolve_dns:
        mode = "not_resolved"
    else:
        try:
            answers = socket.getaddrinfo(ascii_host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise KnownMediaError("dns_failed", "The public media host could not be resolved.") from exc
        addresses = {answer[4][0] for answer in answers if answer[4]}
        if not addresses:
            raise KnownMediaError("dns_failed", "The public media host returned no addresses.")
        if all(_is_fake_ip_answer(address) for address in addresses):
            # The connection still goes through the proxy; DoH only re-checks the answer.
            mode = _check_behind_fake_ip(ascii_host)
        else:
            for address in addresses:
                _require_global_ip(address)
            mode = "local_dns"
    if dns_checks is not None:
        dns_checks[ascii_host] = mode
    return value.strip()


def _is_fake_ip_answer(value: str) -> bool:
    # Covers the plain IPv4 answer and the IPv4-mapped/translated IPv6 forms;
    # macOS returns the translated form (e.g. ::ffff:0:c612:3bc) next to the IPv4 one.
    try:
        address = _effective_address(ipaddress.ip_address(value))
    except ValueError:
        return False
    return address is not None and address.version == 4 and address in FAKE_IP_NETWORK


def summarize_dns_checks(dns_checks: dict[str, str]) -> str:
    modes = set(dns_checks.values())
    for mode in (DNS_CHECK_DOH, "local_dns", "ip_literal"):
        if mode in modes:
            return mode
    return "not_resolved"


def validate_x_media_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in X_MEDIA_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise KnownMediaError(
            "unsafe_media_url",
            "The public X response did not contain an approved video CDN URL.",
        )
    return value


def choose_unique_output_dir(requested: Path) -> Path:
    base = requested.expanduser()
    base.parent.mkdir(parents=True, exist_ok=True)
    # mkdir itself is the existence check, so a concurrent run that wins the race
    # for one name only moves this run on to the next -run-N.
    for counter in range(1, 1000):
        candidate = base if counter == 1 else base.with_name(f"{base.name}-run-{counter}")
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise KnownMediaError("output_conflict", "Could not allocate a new output directory.")


def require_x_source_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not X_STATUS_ID_RE.fullmatch(text):
        raise KnownMediaError("invalid_response", "The public X reader returned an invalid post ID.")
    return text


def x_read_failure(x_result: Any) -> KnownMediaError:
    errors = x_result.get("errors") if isinstance(x_result, dict) else None
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    category = first.get("category") if isinstance(first.get("category"), str) else None
    message = first.get("message") if isinstance(first.get("message"), str) else None
    return KnownMediaError(
        category or "x_read_failed",
        message or "The public X reader could not read this post.",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_x_videos(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, dict):
        return []
    media = content.get("media")
    videos = media.get("videos") if isinstance(media, dict) else None
    if not isinstance(videos, list):
        return []
    selected: list[dict[str, Any]] = []
    for index, video in enumerate(videos, start=1):
        if not isinstance(video, dict):
            continue
        candidates: list[tuple[int, str]] = []
        formats = video.get("formats")
        if isinstance(formats, list):
            for item in formats:
                if not isinstance(item, dict) or item.get("container") != "mp4":
                    continue
                url = item.get("url")
                bitrate = item.get("bitrate")
                if isinstance(url, str):
                    candidates.append((bitrate if isinstance(bitrate, int) else 0, url))
        fallback = video.get("url")
        if isinstance(fallback, str):
            candidates.append((0, fallback))
        if not candidates:
            continue
        bitrate, media_url = max(candidates, key=lambda item: item[0])
        selected.append(
            {
                "index": index,
                "id": str(video.get("id") or index),
                "url": validate_x_media_url(media_url),
                "bitrate": bitrate or None,
                "width": video.get("width"),
                "height": video.get("height"),
                "duration_seconds": video.get("duration"),
            }
        )
    return selected


class XMediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_x_media_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def part_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.part")


def parse_content_length(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    if not re.fullmatch(r"[0-9]{1,20}", value.strip()):
        raise KnownMediaError("invalid_response", "X returned an invalid Content-Length.")
    return int(value.strip())


def is_timeout_error(exc: BaseException) -> bool:
    # Python 3.9: socket.timeout is not a TimeoutError subclass and is raised
    # unwrapped while waiting for headers or reading the body.
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (socket.timeout, TimeoutError))


def download_x_file(url: str, destination: Path, *, timeout: int) -> None:
    validate_x_media_url(url)
    request = urllib.request.Request(
        url,
        headers={"Accept": "video/mp4", "User-Agent": USER_AGENT},
        method="GET",
    )
    opener = urllib.request.build_opener(XMediaRedirectHandler())
    partial = part_path(destination)
    try:
        with opener.open(request, timeout=timeout) as response:
            validate_x_media_url(response.geturl())
            content_type = response.headers.get_content_type()
            if content_type not in {"video/mp4", "application/octet-stream"}:
                raise KnownMediaError("unexpected_media_type", "X returned a non-video response.")
            expected = parse_content_length(response.headers.get("Content-Length"))
            if expected is not None and expected > MAX_MEDIA_BYTES:
                raise KnownMediaError("media_too_large", "The video exceeds the 4 GiB safety limit.")
            written = 0
            with partial.open("xb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_MEDIA_BYTES:
                        raise KnownMediaError("media_too_large", "The video exceeds the 4 GiB safety limit.")
                    handle.write(chunk)
        # http.client returns a short body silently when the peer closes early.
        if written == 0:
            raise KnownMediaError("incomplete_download", "X returned an empty video body.")
        if expected is not None and written != expected:
            raise KnownMediaError(
                "incomplete_download",
                "The X video body did not match its declared Content-Length.",
            )
        if destination.exists():
            raise KnownMediaError("output_conflict", "The destination appeared during download.")
        partial.rename(destination)
    except KnownMediaError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        # URLError/HTTPError, socket.timeout and resets are OSError subclasses.
        if is_timeout_error(exc):
            raise KnownMediaError("timeout", "The public X video download timed out.") from exc
        raise KnownMediaError("download_failed", "The public X video download failed.") from exc


def find_yt_dlp() -> str:
    preferred = Path.home() / ".local" / "bin" / "yt-dlp"
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    binary = shutil.which("yt-dlp")
    if not binary:
        raise KnownMediaError("backend_unavailable", "yt-dlp is not installed.")
    return binary


def yt_dlp_common(timeout: int) -> list[str]:
    return [
        find_yt_dlp(),
        "--ignore-config",
        "--no-playlist",
        "--no-cookies-from-browser",
        # The generic extractor follows arbitrary pages, embeds and redirects; only
        # site-specific extractors with fixed hosts may run.
        "--use-extractors",
        "default,-generic",
        "--socket-timeout",
        str(timeout),
        "--max-filesize",
        "4G",
    ]


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _largest_file_size(directory: Path) -> int:
    largest = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                largest = max(largest, entry.stat(follow_symlinks=False).st_size)
        except OSError:
            continue
    return largest


def run_yt_dlp(
    command: list[str],
    *,
    total_timeout: float,
    input_text: str | None = None,
    watch_dir: Path | None = None,
) -> tuple[int, str]:
    """Run yt-dlp with a wall-clock limit and, when downloading, a per-file size cap.

    --max-filesize only rejects streams whose size is declared; merged output and
    undeclared (chunked/HLS) streams are capped here by watching the output files.
    """

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise KnownMediaError("backend_unavailable", "yt-dlp could not be started.") from exc
    deadline = time.monotonic() + total_timeout
    pending_input = input_text
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            process.communicate()
            raise KnownMediaError("timeout", "yt-dlp did not finish within the total time limit.")
        try:
            stdout, _stderr = process.communicate(
                input=pending_input, timeout=min(YT_DLP_POLL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            # communicate keeps buffered output and unsent input across retries.
            pending_input = None
            if watch_dir is not None and _largest_file_size(watch_dir) > MAX_MEDIA_BYTES:
                _kill_process_group(process)
                process.communicate()
                raise KnownMediaError("media_too_large", "The media exceeds the 4 GiB safety limit.")
            continue
        return process.returncode, stdout or ""


def preflight_generic(url: str, *, timeout: int) -> dict[str, Any]:
    command = [
        *yt_dlp_common(timeout),
        "--format",
        GENERIC_FORMAT,
        "--skip-download",
        "--dump-single-json",
        "--",
        url,
    ]
    returncode, stdout = run_yt_dlp(command, total_timeout=GENERIC_PREFLIGHT_SECONDS)
    if returncode != 0:
        raise KnownMediaError("extractor_failed", "The anonymous single-URL extractor could not read this link.")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise KnownMediaError("invalid_response", "yt-dlp returned invalid metadata.") from exc
    if not isinstance(payload, dict) or payload.get("_type") in {"playlist", "multi_video"}:
        raise KnownMediaError("container_rejected", "Only one explicit media item is accepted per request.")
    if not payload.get("id"):
        raise KnownMediaError("invalid_response", "yt-dlp did not return a media identifier.")
    return payload


def _positive_size(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def validate_generic_media_url(
    value: Any, checked_hosts: set[str], dns_checks: dict[str, str] | None = None
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise KnownMediaError("unsafe_media_url", "yt-dlp selected a format without a media URL.")
    try:
        host = (urllib.parse.urlsplit(value.strip()).hostname or "").lower()
    except ValueError:
        host = ""
    first_check = host not in checked_hosts
    try:
        # CDN media hosts may use non-443 ports; scheme, credentials and the
        # public-address gate (with the same fake-IP DoH re-check) apply to every URL.
        validate_public_https_url(
            value,
            resolve_dns=first_check,
            allow_any_port=True,
            dns_checks=dns_checks if first_check else None,
        )
    except KnownMediaError as exc:
        raise KnownMediaError(
            "unsafe_media_url",
            f"yt-dlp selected a media URL that failed the public HTTPS check ({exc.category}).",
        ) from exc
    checked_hosts.add(host)


def _format_urls(item: dict[str, Any]) -> list[Any]:
    urls = [item.get("url")]
    for key in ("manifest_url", "fragment_base_url"):
        if item.get(key) is not None:
            urls.append(item[key])
    fragments = item.get("fragments")
    if isinstance(fragments, list):
        for fragment in fragments:
            if isinstance(fragment, dict) and fragment.get("url") is not None:
                urls.append(fragment["url"])
    return urls


def bind_generic_formats(
    info: dict[str, Any], dns_checks: dict[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the formats yt-dlp selected and return info limited to exactly them.

    The download replays this info with --load-info-json, so it cannot pick a
    different or freshly re-extracted (unchecked) URL.
    """

    requested = info.get("requested_formats")
    if requested is None:
        selected = [info]
    elif isinstance(requested, list) and requested and all(isinstance(item, dict) for item in requested):
        selected = requested
    else:
        raise KnownMediaError("invalid_response", "yt-dlp returned invalid selected formats.")
    # Without webpage_url, yt-dlp cannot fall back to re-extracting the page.
    bound = {
        key: value
        for key, value in info.items()
        if key not in {"webpage_url", "requested_formats"}
    }
    formats = info.get("formats")
    if formats is None:
        kept = selected
    else:
        selected_ids = {
            str(item["format_id"]) for item in selected if item.get("format_id") is not None
        }
        if not isinstance(formats, list) or not selected_ids:
            raise KnownMediaError("invalid_response", "yt-dlp returned invalid format metadata.")
        kept = [
            item
            for item in formats
            if isinstance(item, dict) and str(item.get("format_id")) in selected_ids
        ]
        if not kept:
            raise KnownMediaError("invalid_response", "yt-dlp did not list its selected formats.")
        bound["formats"] = kept
    checked_hosts: set[str] = set()
    for item in [*kept, *([bound] if bound.get("url") is not None else [])]:
        for value in _format_urls(item):
            validate_generic_media_url(value, checked_hosts, dns_checks)
    declared = 0
    for item in selected:
        size = _positive_size(item.get("filesize")) or _positive_size(item.get("filesize_approx"))
        if size is None:
            declared = None
            break
        declared += size
    if declared is not None and declared > MAX_MEDIA_BYTES:
        raise KnownMediaError("media_too_large", "The selected media exceeds the 4 GiB safety limit.")
    size_check = {
        "max_bytes_per_file": MAX_MEDIA_BYTES,
        "declared_bytes": declared,
        "status": "declared_within_limit" if declared is not None else "size_undeclared",
        "enforcement": "declared size, yt-dlp --max-filesize, and output file size watch",
    }
    return bound, size_check


def download_generic(info: dict[str, Any], output_dir: Path, *, timeout: int) -> list[Path]:
    command = [
        *yt_dlp_common(timeout),
        "--no-overwrites",
        "--no-write-info-json",
        "--no-write-playlist-metafiles",
        "--no-write-comments",
        "--restrict-filenames",
        "--format",
        GENERIC_FORMAT,
        "--merge-output-format",
        "mp4",
        "--paths",
        f"home:{output_dir}",
        "--output",
        "%(extractor)s-%(id)s.%(ext)s",
        "--print",
        "after_move:filepath",
        "--no-simulate",
        "--load-info-json",
        "-",
    ]
    returncode, stdout = run_yt_dlp(
        command,
        total_timeout=GENERIC_DOWNLOAD_SECONDS,
        input_text=json.dumps(info, ensure_ascii=True),
        watch_dir=output_dir,
    )
    if returncode != 0:
        raise KnownMediaError("download_failed", "The anonymous single-URL media download failed.")
    root = output_dir.resolve()
    paths: list[Path] = []
    for line in stdout.splitlines():
        candidate = Path(line.strip()).expanduser()
        if not line.strip() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if root not in resolved.parents:
            raise KnownMediaError("output_escape", "The extractor returned a path outside the output directory.")
        paths.append(resolved)
    if len(paths) != 1:
        raise KnownMediaError("unexpected_output", "The extractor did not produce exactly one media file.")
    return paths


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def is_x_page_url(value: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(value.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in X_HOSTS


def file_artifact(path: Path) -> dict[str, Any]:
    return {
        "status": "success",
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def download_x_video(
    video: dict[str, Any], selected_dir: Path, source_id: str, *, timeout: int
) -> dict[str, Any]:
    destination = selected_dir / f"x-{source_id}-video-{video['index']}.mp4"
    details = {
        "bitrate": video["bitrate"],
        "width": video["width"],
        "height": video["height"],
        "duration_seconds": video["duration_seconds"],
    }
    try:
        download_x_file(video["url"], destination, timeout=timeout)
    except KnownMediaError as exc:
        partial = part_path(destination)
        return {
            "index": video["index"],
            "status": "failed",
            "path": None,
            "size": None,
            "sha256": None,
            **details,
            # Incomplete bytes stay on disk under a .part name; nothing is deleted.
            "incomplete_path": str(partial) if partial.exists() else None,
            "error": {"category": exc.category, "message": exc.message},
        }
    return {"index": video["index"], **file_artifact(destination), **details}


def write_run_metadata(
    selected_dir: Path,
    *,
    validated: str,
    source_id: str | None,
    title: Any,
    backend: str,
    artifacts: list[dict[str, Any]],
    requested_count: int,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    success_count = sum(item.get("status") == "success" for item in artifacts)
    if success_count == requested_count:
        status = "success"
    elif success_count:
        status = "partial"
    else:
        status = "failed"
    metadata = {
        "schema_version": "known-public-media/v1",
        "status": status,
        "source_url": safe_source_url(validated),
        "source_id": source_id,
        "title": title,
        "backend": backend,
        "login_state_used": False,
        "discovery_performed": False,
        "downloaded_at": utc_now_iso(),
        "counts": {
            "requested": requested_count,
            "success": success_count,
            "failed": requested_count - success_count,
        },
        "artifacts": artifacts,
        **(extra or {}),
    }
    metadata_path = selected_dir / "metadata.json"
    write_json_exclusive(metadata_path, metadata)
    return metadata, metadata_path


def failed_run_error(
    metadata: dict[str, Any], metadata_path: Path, selected_dir: Path
) -> KnownMediaError:
    error = metadata.get("error") or next(
        (item["error"] for item in metadata["artifacts"] if item.get("error")),
        {"category": "download_failed", "message": "The media download failed."},
    )
    return KnownMediaError(
        error["category"],
        error["message"],
        output_dir=selected_dir,
        metadata_path=metadata_path,
    )


def run(url: str, *, output_dir: str | None, timeout: int) -> dict[str, Any]:
    # The X page host is never contacted: the post is read from api.fxtwitter.com and the
    # video from allowlisted video.twimg.com. Resolving it locally only adds failures, e.g.
    # fake-IP proxy DNS answers 198.18.0.0/15 for every domain. Other hosts still resolve.
    dns_checks: dict[str, str] = {}
    validated = validate_public_https_url(
        url, resolve_dns=not is_x_page_url(url), dns_checks=dns_checks
    )
    parsed = urllib.parse.urlsplit(validated)
    host = (parsed.hostname or "").lower()
    if host in X_HOSTS:
        x_reader = load_x_reader()
        x_result = x_reader.read_known_url(
            validated,
            timeout=timeout,
            allow_jina_fallback=False,
        )
        if not isinstance(x_result, dict) or x_result.get("status") == "failed":
            # Keep the reader's own category (rate_limited, timeout, not_found, ...).
            raise x_read_failure(x_result)
        content = x_result.get("content")
        videos = select_x_videos(content)
        if not videos:
            raise KnownMediaError("no_public_video", "The supplied X post has no anonymously downloadable MP4 video.")
        input_info = x_result.get("input") if isinstance(x_result.get("input"), dict) else {}
        source_id = require_x_source_id(content.get("id") or input_info.get("id"))
        requested = Path(output_dir).expanduser() if output_dir else Path.home() / "Downloads" / f"x-{source_id}"
        selected_dir = choose_unique_output_dir(requested)
        artifacts = [
            download_x_video(video, selected_dir, source_id, timeout=timeout)
            for video in videos
        ]
        metadata, metadata_path = write_run_metadata(
            selected_dir,
            validated=validated,
            source_id=source_id,
            title=content.get("text"),
            backend="fxtwitter-public+x-video-cdn",
            artifacts=artifacts,
            requested_count=len(videos),
        )
    else:
        preflight = preflight_generic(validated, timeout=timeout)
        source_id = str(preflight.get("id"))
        download_info, size_check = bind_generic_formats(preflight, dns_checks)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{host}-{source_id}").strip("-")[:120]
        requested = Path(output_dir).expanduser() if output_dir else Path.home() / "Downloads" / f"media-{slug}"
        selected_dir = choose_unique_output_dir(requested)
        extra: dict[str, Any] = {
            "size_check": size_check,
            # doh_verified_behind_fake_ip: local DNS gave only fake-IP answers and
            # every host was confirmed public via DoH; traffic still used the proxy.
            "dns_check": summarize_dns_checks(dns_checks),
            "dns_check_hosts": dict(sorted(dns_checks.items())),
        }
        artifacts = []
        try:
            paths = download_generic(download_info, selected_dir, timeout=timeout)
        except KnownMediaError as exc:
            extra["error"] = {"category": exc.category, "message": exc.message}
            extra["incomplete_files"] = sorted(
                path.name for path in selected_dir.iterdir() if path.is_file()
            )
        else:
            for path in paths:
                artifact = file_artifact(path)
                if artifact["size"] > MAX_MEDIA_BYTES:
                    artifact["status"] = "failed"
                    artifact["error"] = {
                        "category": "media_too_large",
                        "message": "The media exceeds the 4 GiB safety limit.",
                    }
                artifacts.append(artifact)
        metadata, metadata_path = write_run_metadata(
            selected_dir,
            validated=validated,
            source_id=source_id,
            title=preflight.get("title"),
            backend="yt-dlp-anonymous-single-url",
            artifacts=artifacts,
            requested_count=1,
            extra=extra,
        )

    if metadata["status"] == "failed":
        raise failed_run_error(metadata, metadata_path, selected_dir)
    return {**metadata, "output_dir": str(selected_dir), "metadata_path": str(metadata_path)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download media from one user-supplied public HTTPS URL anonymously."
    )
    parser.add_argument("url")
    parser.add_argument("--output-dir")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if not 5 <= args.timeout <= 120:
        parser.error("--timeout must be between 5 and 120 seconds")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = run(args.url, output_dir=args.output_dir, timeout=args.timeout)
    except KnownMediaError as exc:
        result = {
            "schema_version": "known-public-media/v1",
            "status": "failed",
            "error": {"category": exc.category, "message": exc.message},
            "login_state_used": False,
            "discovery_performed": False,
        }
        # A failure after the run directory exists still points at what was written.
        if exc.output_dir is not None:
            result["output_dir"] = str(exc.output_dir)
        if exc.metadata_path is not None:
            result["metadata_path"] = str(exc.metadata_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
