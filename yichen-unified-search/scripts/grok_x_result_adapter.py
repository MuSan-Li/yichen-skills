#!/usr/bin/env python3
"""Normalize verified ``search_x_with_grok`` text into an X envelope.

This adapter is intentionally offline and fail-closed.  It reads only the
machine-verification blocks that grok-consult appends after the untrusted tool
output, so tags echoed inside Grok prose or post text are never parsed.  Grok's
prose is never used to infer post text, language, engagement, or post type.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO


SCHEMA_VERSION = "1.0"
BACKEND = "grok-consult"
NATIVE_TAG = "native_search_verification"
LOCAL_TAG = "local_reader_verification"
TIME_TAG = "x_post_time_verification"
ROUTE_SELECTION_TAG = "grok_consult_route"
CANDIDATE_URLS_TAG_PREFIX = "candidate_urls_from_"
GROK_OUTPUT_TAG = "grok_output"
FXTWITTER_ROUTE = "fxtwitter-public"
TRAILING_BLOCK_TAGS = {NATIVE_TAG, LOCAL_TAG, TIME_TAG, ROUTE_SELECTION_TAG}
TRAILING_CLOSE_TAG_RE = re.compile(r"</([A-Za-z][A-Za-z0-9_-]{0,80})>\s*\Z")
OUTPUT_TRUNCATED_MARKER = "[Output truncated by grok-consult]"
PARTIAL_OUTPUT_NOTICE = (
    "Grok returned usable partial output before the CLI stopped"
)
ALLOWED_LOCAL_ROUTES = {"fxtwitter-public", "opencli", "xreach"}
TOOL_ERROR_CODE_RE = re.compile(r"\A\s*\[([A-Z][A-Z0-9_]{0,99})\]\s")
TOOL_ERROR_CATEGORIES = {
    "OFFICIAL_QUOTA_EXHAUSTED": "quota_exhausted",
    "NATIVE_AUTH_FAILED": "authentication_error",
    "NATIVE_CLI_TIMEOUT": "timeout",
    "AUTH_PREFLIGHT_REFRESH_TIMEOUT": "timeout",
    "NATIVE_CLI_FAILED": "service_error",
    "NATIVE_SANDBOX_FAILED": "service_error",
    "AUTH_PREFLIGHT_INVALID_CONFIGURATION": "service_error",
    "NATIVE_TOOL_POLICY_VIOLATION": "unverified_native_search",
    "NATIVE_SEARCH_VERIFICATION_FAILED": "unverified_native_search",
    "NATIVE_TURN_INCOMPLETE": "unverified_native_search",
    "NATIVE_FINAL_ANSWER_INCOMPLETE": "unverified_native_search",
    "NATIVE_PERMISSION_CANCELLED": "cancelled",
    "NATIVE_TURN_CANCELLED": "cancelled",
    "AUTH_PREFLIGHT_CANCELLED": "cancelled",
}
# Ordered (pattern, category) checks for grok-consult errors without a code.
TOOL_ERROR_MESSAGE_CATEGORIES = (
    (
        re.compile(r"authenticated_fallback_requires_current_task_authorization"),
        "authenticated_fallback_not_authorized",
    ),
    (re.compile(r"All eligible grok-consult routes failed"), "all_routes_failed"),
    (
        re.compile(
            r"not authenticated|(?:authentication|authorization)\s+(?:failed|required|denied)"
            r"|not (?:logged|signed) in",
            re.IGNORECASE,
        ),
        "authentication_error",
    ),
    (re.compile(r"\btimed out\b", re.IGNORECASE), "timeout"),
    (re.compile(r"\bcancell?ed\b", re.IGNORECASE), "cancelled"),
    (re.compile(r"\AOfficial Grok CLI\b"), "service_error"),
    (
        re.compile(r"\AUnknown tool:|\bmust be\b|\bexceeds the\b", re.IGNORECASE),
        "invalid_request",
    ),
)
TOOL_ERROR_TEXT_RE = re.compile(
    r"\A\s*(?:\[[A-Z][A-Z0-9_]{0,99}\]\s|All eligible grok-consult routes failed"
    r"|authenticated_fallback_requires_current_task_authorization"
    r"|Official Grok CLI failed without explicit quota-exhaustion evidence)"
)
STATUS_URL_RE = re.compile(
    r"^https://(?:www\.)?(?:x\.com|twitter\.com)/"
    r"([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{10,25})(?:[/?#].*)?$",
    re.IGNORECASE,
)

UNKNOWN_FIELDS_LIMITATION = (
    "The Grok time-verification block does not provide post text, language, "
    "engagement metrics, or Reply/Repost type; those fields remain unknown."
)
FXTWITTER_FIELDS_LIMITATION = (
    "Post text, language, engagement, and content-type fields are preserved only "
    "when supplied by the third-party FxTwitter public index; absent fields remain unknown."
)
SNOWFLAKE_LIMITATION = (
    "The publication time is decoded from the X status Snowflake ID; this does "
    "not prove that X issued or currently serves the ID."
)
MATCHED_ONLY_LIMITATION = (
    "Only x_post_time_verification.matched records were normalized; "
    "excluded_outside_window records were counted for coverage but omitted."
)
URL_AUTHOR_LIMITATION = (
    "The author handle is URL-derived and was not independently verified."
)
PLACEHOLDER_AUTHOR_LIMITATION = (
    "The status URL uses the /i/ placeholder, so the author is unknown."
)
PARTIAL_OUTPUT_LIMITATION = (
    "grok-consult reported that the Grok CLI stopped before completing; "
    "the matched set may be incomplete."
)
GROK_PROSE_TRUNCATED_LIMITATION = (
    "grok-consult truncated the Grok prose; candidates come only from the "
    "verification blocks, which grok-consult computes before truncation."
)


class AdapterError(RuntimeError):
    """A safe parse or contract failure suitable for a public envelope."""

    def __init__(
        self, category: str, message: str, error_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.error_code = error_code


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(
            "contract_error", f"{field} must be a non-negative integer."
        )
    return value


def _positive_cli_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _unwrap_mcp_text(raw: str) -> tuple[str, bool, str | None]:
    """Return tool text, the MCP isError flag, and any structured error code."""

    stripped = raw.strip()
    if not stripped:
        raise AdapterError("invalid_input", "The Grok result input is empty.")

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return raw, False, None

    if isinstance(payload, str):
        if not payload.strip():
            raise AdapterError("invalid_input", "The MCP text content is empty.")
        return payload, False, None

    if not isinstance(payload, dict):
        raise AdapterError(
            "invalid_input", "MCP JSON input must be an object containing content."
        )

    if "content" not in payload and isinstance(payload.get("error"), dict):
        raise AdapterError(
            "tool_error",
            "grok-consult returned a JSON-RPC error; no candidates were produced.",
        )
    container: Any = payload
    if "content" not in container and isinstance(container.get("result"), dict):
        container = container["result"]
    content = container.get("content") if isinstance(container, dict) else None
    if not isinstance(content, list):
        raise AdapterError(
            "invalid_input", "MCP JSON input is missing a content array."
        )

    text_blocks: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise AdapterError(
                "invalid_input", "An MCP text content block has a non-string text field."
            )
        text_blocks.append(text)
    is_error = container.get("isError") is True
    structured = container.get("structuredContent")
    error_code = (
        _nonempty_text(structured.get("error_code"))
        if isinstance(structured, dict)
        else None
    )
    if not text_blocks or not any(block.strip() for block in text_blocks):
        if is_error:
            return "", True, error_code
        raise AdapterError(
            "invalid_input", "MCP JSON input contains no non-empty text content."
        )
    return "\n".join(text_blocks), is_error, error_code


def _tool_error(text: str, error_code: str | None) -> AdapterError:
    """Map a grok-consult tool error to its real failure category."""

    if error_code is None:
        match = TOOL_ERROR_CODE_RE.match(text)
        error_code = match.group(1) if match else None
    if error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", error_code):
        error_code = None
    category = TOOL_ERROR_CATEGORIES.get(error_code or "")
    if category is None and error_code and error_code.startswith("AUTH_PREFLIGHT_"):
        category = "authentication_error"
    if category is None:
        message_text = TOOL_ERROR_CODE_RE.sub("", text, count=1).strip()
        category = next(
            (
                mapped
                for pattern, mapped in TOOL_ERROR_MESSAGE_CATEGORIES
                if pattern.search(message_text)
            ),
            "tool_error",
        )
    suffix = f" ({error_code})" if error_code else ""
    # The tool's own diagnostic text is not copied: it may quote CLI output.
    return AdapterError(
        category,
        f"grok-consult returned a tool error{suffix}; no candidates were produced.",
        error_code=error_code,
    )


def _split_trailing_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Peel grok-consult's machine blocks off the end of the tool text.

    grok-consult appends these blocks after the untrusted ``<grok_output>`` or
    ``<route_output>`` body, so tags echoed inside that body can never reach the
    tail.  Each block's opening tag is searched backwards from its own closing
    tag.  Returns the remaining head (header plus untrusted body) and the
    trailing block bodies keyed by tag.
    """

    head = text.rstrip()
    blocks: dict[str, str] = {}
    while True:
        match = TRAILING_CLOSE_TAG_RE.search(head)
        if match is None:
            break
        tag = match.group(1)
        key = (
            CANDIDATE_URLS_TAG_PREFIX
            if tag.startswith(CANDIDATE_URLS_TAG_PREFIX)
            else tag
        )
        if key not in TRAILING_BLOCK_TAGS and key != CANDIDATE_URLS_TAG_PREFIX:
            break
        open_tag = f"<{tag}>"
        start = head.rfind(open_tag, 0, match.start())
        if start < 0:
            raise AdapterError(
                "parse_error", f"The trailing </{tag}> block has no opening tag."
            )
        if key in blocks:
            raise AdapterError(
                "parse_error",
                f"Expected at most one trailing <{tag}> block; found a duplicate.",
            )
        blocks[key] = head[start + len(open_tag) : match.start()].strip()
        head = head[:start].rstrip()
    return head, blocks


def _tool_output(head: str, tag: str) -> tuple[str, str | None]:
    """Split the head into the server header and the untrusted output body.

    The first opening tag belongs to grok-consult because the header precedes
    the body; the closing tag must end the head because the machine blocks
    follow it.  Returns ``(header, body)``; ``body`` is None when absent.
    """

    open_tag = f"<{tag}>"
    close_tag = f"</{tag}>"
    start = head.find(open_tag)
    if start < 0:
        return head, None
    header = head[:start]
    body_end = len(head) - len(close_tag)
    if not head.endswith(close_tag) or body_end < start + len(open_tag):
        return header, None
    return header, head[start + len(open_tag) : body_end]


def _parse_json_object(body: str, tag: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "parse_error", f"The <{tag}> block does not contain valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AdapterError(
            "contract_error", f"The <{tag}> JSON value must be an object."
        )
    return payload


def _route_verification(blocks: dict[str, str]) -> dict[str, Any]:
    if (NATIVE_TAG in blocks) == (LOCAL_TAG in blocks):
        raise AdapterError(
            "parse_error",
            "Expected exactly one mutually exclusive trailing route-verification "
            "block: native_search_verification or local_reader_verification.",
        )

    if NATIVE_TAG in blocks:
        native = _parse_json_object(blocks[NATIVE_TAG], NATIVE_TAG)
        count = _native_search_count(native)
        return {
            "backend": BACKEND,
            "login_state_used": True,
            "visibility": "public",
            "native_search_count": count,
            "grok_native_x_search_verified": True,
            "local_reader_verified": False,
            "local_reader_route": None,
        }

    local = _parse_json_object(blocks[LOCAL_TAG], LOCAL_TAG)
    if local.get("read_only") is not True:
        raise AdapterError(
            "contract_error", "Local X reader verification read_only must be true."
        )
    route = _nonempty_text(local.get("route"))
    if route not in ALLOWED_LOCAL_ROUTES:
        raise AdapterError(
            "contract_error",
            "Local X reader route must be one of fxtwitter-public, opencli, or xreach.",
        )
    login_state_used = route in {"opencli", "xreach"}
    return {
        "backend": route,
        "login_state_used": login_state_used,
        "visibility": "authenticated_public" if login_state_used else "public",
        "native_search_count": None,
        "grok_native_x_search_verified": False,
        "local_reader_verified": True,
        "local_reader_route": route,
    }


def _native_search_count(native: dict[str, Any]) -> int:
    if native.get("verified") is not True:
        raise AdapterError(
            "contract_error", "Native Grok X search verification is not true."
        )
    count = _nonnegative_int(
        native.get("x_search_completed_call_count"),
        "native_search_verification.x_search_completed_call_count",
    )
    if count < 1:
        raise AdapterError(
            "contract_error",
            "At least one completed native Grok XSearch call is required.",
        )
    return count


def _status_identity(item: dict[str, Any], index: int) -> tuple[str, str, str]:
    tweet_id = item.get("tweet_id")
    if not isinstance(tweet_id, str) or not re.fullmatch(r"\d{10,25}", tweet_id):
        raise AdapterError(
            "contract_error",
            f"x_post_time_verification.matched[{index}].tweet_id is invalid.",
        )
    url = _nonempty_text(item.get("url"))
    match = STATUS_URL_RE.fullmatch(url or "")
    if match is None:
        raise AdapterError(
            "contract_error",
            f"x_post_time_verification.matched[{index}].url is not a supported X status URL.",
        )
    url_tweet_id = match.group(2)
    if url_tweet_id != tweet_id:
        raise AdapterError(
            "contract_error",
            f"x_post_time_verification.matched[{index}] URL status ID does not match tweet_id.",
        )
    return tweet_id, url, match.group(1)


def _time_range(time_verification: dict[str, Any]) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    requested_date = _nonempty_text(time_verification.get("requested_date"))
    requested_hours = time_verification.get("requested_hours")
    timezone = _nonempty_text(time_verification.get("timezone"))
    as_of_utc = _nonempty_text(time_verification.get("as_of_utc"))
    if requested_date is not None:
        result["date"] = requested_date
    if isinstance(requested_hours, int) and not isinstance(requested_hours, bool):
        result["hours"] = requested_hours
    if timezone is not None:
        result["timezone"] = timezone
    if as_of_utc is not None:
        result["as_of_utc"] = as_of_utc
    return result or None


def _candidate_tweet_id(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return None
    provenance = candidate.get("provenance")
    if isinstance(provenance, dict):
        source_id = provenance.get("source_id")
        if isinstance(source_id, str) and re.fullmatch(r"\d{10,25}", source_id):
            return source_id
    candidate_id = candidate.get("candidate_id")
    if isinstance(candidate_id, str):
        match = re.fullmatch(r"x:(\d{10,25})", candidate_id, re.IGNORECASE)
        if match:
            return match.group(1)
    for field in ("canonical_url", "url"):
        value = _nonempty_text(candidate.get(field))
        match = STATUS_URL_RE.fullmatch(value or "")
        if match:
            return match.group(2)
    return None


def _skip_json_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _truncated_envelope_candidates(body: str) -> list[Any]:
    """Decode the complete candidate objects from a truncated envelope prefix.

    The FxTwitter envelope is parsed structurally (key by key, then element by
    element) so strings inside post text cannot be mistaken for structure; the
    cut-off trailing element is discarded.
    """

    decoder = json.JSONDecoder()
    index = _skip_json_whitespace(body, 0)
    if not body.startswith("{", index):
        return []
    index += 1
    while True:
        index = _skip_json_whitespace(body, index)
        try:
            key, index = decoder.raw_decode(body, index)
        except json.JSONDecodeError:
            return []
        index = _skip_json_whitespace(body, index)
        if not isinstance(key, str) or not body.startswith(":", index):
            return []
        index = _skip_json_whitespace(body, index + 1)
        if key == "candidates":
            break
        try:
            _, index = decoder.raw_decode(body, index)
        except json.JSONDecodeError:
            return []
        index = _skip_json_whitespace(body, index)
        if not body.startswith(",", index):
            return []
        index += 1

    if not body.startswith("[", index):
        return []
    index += 1
    items: list[Any] = []
    while True:
        index = _skip_json_whitespace(body, index)
        if index >= len(body) or body[index] == "]":
            return items
        try:
            item, index = decoder.raw_decode(body, index)
        except json.JSONDecodeError:
            return items
        items.append(item)
        index = _skip_json_whitespace(body, index)
        if not body.startswith(",", index):
            return items
        index += 1


def _fxtwitter_candidates(
    head: str, route: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return structured FxTwitter candidates by tweet ID and a truncation flag."""

    if route.get("local_reader_route") != FXTWITTER_ROUTE:
        return {}, False
    tag = f"{FXTWITTER_ROUTE}_output"
    if f"<{tag}>" not in head:
        return {}, False
    _, body = _tool_output(head, tag)
    if body is None:
        raise AdapterError(
            "parse_error",
            f"The <{tag}> block is not closed immediately before the verification blocks.",
        )
    body = body.strip()
    truncated = body.endswith(OUTPUT_TRUNCATED_MARKER)
    if truncated:
        raw_candidates = _truncated_envelope_candidates(
            body[: -len(OUTPUT_TRUNCATED_MARKER)]
        )
    else:
        payload = _parse_json_object(body, tag)
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            raise AdapterError(
                "contract_error",
                "The fxtwitter-public output envelope is missing a candidates array.",
            )
    result: dict[str, dict[str, Any]] = {}
    for raw_candidate in raw_candidates:
        tweet_id = _candidate_tweet_id(raw_candidate)
        if tweet_id is not None and tweet_id not in result:
            result[tweet_id] = raw_candidate
    return result, truncated


def _candidate(
    item: dict[str, Any],
    *,
    index: int,
    query: str,
    call_index: int | None,
    phase: str,
    route: dict[str, Any],
    retrieved_at: str | None,
    structured_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    if item.get("window_match") is not True:
        raise AdapterError(
            "contract_error",
            f"x_post_time_verification.matched[{index}].window_match must be true.",
        )
    tweet_id, url, url_handle = _status_identity(item, index)
    canonical_url = f"https://x.com/{url_handle}/status/{tweet_id}"
    author = item.get("author")
    if author is not None and not isinstance(author, str):
        raise AdapterError(
            "contract_error",
            f"x_post_time_verification.matched[{index}].author must be a string or null.",
        )
    supplied_author = _nonempty_text(author)
    # x.com/i/status/<id> is X's handle-free placeholder: it carries no author.
    placeholder_url = url_handle.casefold() == "i"
    if (
        not placeholder_url
        and supplied_author is not None
        and supplied_author.removeprefix("@").casefold() != url_handle.casefold()
    ):
        raise AdapterError(
            "contract_error",
            f"x_post_time_verification.matched[{index}].author does not match the status URL handle.",
        )
    author_handle = None if placeholder_url else f"@{url_handle}"

    backend = route["backend"]
    provenance = {
        "source_id": tweet_id,
        "retrieved_at": retrieved_at,
        "route_reason": (
            "official_grok_cli_native_x_search"
            if route["grok_native_x_search_verified"]
            else f"grok_consult_read_only_fallback:{backend}"
        ),
        "grok_time_verification_bucket": "matched",
        "call_index": call_index,
        "phase": phase,
    }
    if route["grok_native_x_search_verified"]:
        provenance.update(
            {
                "native_x_search_completed_call_count": route[
                    "native_search_count"
                ],
                "grok_native_x_search_verified": True,
            }
        )
    else:
        provenance.update(
            {
                "local_reader_verified": True,
                "local_reader_route": backend,
            }
        )

    candidate = {
        "candidate_id": f"x:{tweet_id}",
        "query": query,
        "platform": "x",
        "backend": backend,
        "rank": index + 1,
        "title": None,
        "url": url,
        "canonical_url": canonical_url,
        "snippet": None,
        "author": author_handle,
        "published_at": _nonempty_text(item.get("created_at_utc")),
        "content_type": None,
        "language": None,
        "metrics": {
            "likes": None,
            "comments": None,
            "collects": None,
            "shares": None,
            "views": None,
        },
        "access": {
            "visibility": route["visibility"],
            "login_state_used": route["login_state_used"],
        },
        "verification": {
            "status": "candidate",
            "opened_original": False,
            "checked_at": None,
        },
        "provenance": provenance,
        "platform_fields": {
            "x": {
                "tweet_id": tweet_id,
                "author_handle": author_handle,
                "created_at_local": _nonempty_text(item.get("created_at_local")),
                "timezone": _nonempty_text(item.get("timezone")),
                "date_match": item.get("date_match")
                if isinstance(item.get("date_match"), bool)
                else None,
                "window_match": True,
                "url_provenance": _nonempty_text(item.get("url_provenance")),
                "reply_status": "unknown",
                "repost_status": "unknown",
            }
        },
        "limitations": [
            UNKNOWN_FIELDS_LIMITATION,
            SNOWFLAKE_LIMITATION,
            PLACEHOLDER_AUTHOR_LIMITATION if placeholder_url else URL_AUTHOR_LIMITATION,
        ],
    }
    if backend == "fxtwitter-public" and structured_candidate is not None:
        candidate["limitations"].remove(UNKNOWN_FIELDS_LIMITATION)
        candidate["limitations"].insert(0, FXTWITTER_FIELDS_LIMITATION)
        for field in ("title", "snippet", "author", "content_type", "language"):
            value = structured_candidate.get(field)
            if value is None or isinstance(value, str):
                candidate[field] = value
        source_metrics = structured_candidate.get("metrics")
        if isinstance(source_metrics, dict):
            candidate["metrics"] = {
                key: source_metrics.get(key)
                for key in ("likes", "comments", "collects", "shares", "views", "quotes")
                if key in source_metrics or key != "quotes"
            }
        source_fields = structured_candidate.get("platform_fields")
        if isinstance(source_fields, dict):
            candidate["platform_fields"].update(source_fields)
        source_limitations = structured_candidate.get("limitations")
        if isinstance(source_limitations, list):
            for limitation in source_limitations:
                if (
                    isinstance(limitation, str)
                    and limitation not in candidate["limitations"]
                ):
                    candidate["limitations"].append(limitation)
        if placeholder_url and candidate["author"] is not None:
            candidate["limitations"].remove(PLACEHOLDER_AUTHOR_LIMITATION)
    return candidate


def normalize_grok_result(
    raw: str,
    *,
    query: str,
    call_index: int | None = None,
    phase: str = "initial",
) -> dict[str, Any]:
    """Normalize one Grok tool result or raise :class:`AdapterError`."""

    if phase not in {"initial", "supplementary"}:
        raise AdapterError(
            "invalid_input", "phase must be either initial or supplementary."
        )
    if call_index is not None and (
        isinstance(call_index, bool) or not isinstance(call_index, int) or call_index < 1
    ):
        raise AdapterError("invalid_input", "call_index must be at least 1.")
    if not isinstance(query, str) or not query.strip():
        raise AdapterError("invalid_input", "query must be non-empty text.")

    text, is_error, error_code = _unwrap_mcp_text(raw)
    if is_error:
        raise _tool_error(text, error_code)
    head, blocks = _split_trailing_blocks(text)
    if TIME_TAG not in blocks:
        if TOOL_ERROR_TEXT_RE.match(text):
            raise _tool_error(text, None)
        raise AdapterError(
            "parse_error",
            f"Expected exactly one trailing <{TIME_TAG}> JSON block; found 0.",
        )
    time_verification = _parse_json_object(blocks[TIME_TAG], TIME_TAG)
    route = _route_verification(blocks)
    structured_fxtwitter, structured_truncated = _fxtwitter_candidates(head, route)
    output_tag = (
        GROK_OUTPUT_TAG
        if route["grok_native_x_search_verified"]
        else f"{route['local_reader_route']}_output"
    )
    header, output_body = _tool_output(head, output_tag)
    partial_notice = any(
        line.strip().startswith(PARTIAL_OUTPUT_NOTICE) for line in header.splitlines()
    )
    output_truncated = output_body is not None and output_body.rstrip().endswith(
        OUTPUT_TRUNCATED_MARKER
    )

    matched = time_verification.get("matched")
    excluded = time_verification.get("excluded_outside_window")
    if not isinstance(matched, list):
        raise AdapterError(
            "contract_error", "x_post_time_verification.matched must be an array."
        )
    if not isinstance(excluded, list):
        raise AdapterError(
            "contract_error",
            "x_post_time_verification.excluded_outside_window must be an array.",
        )
    matched_count = _nonnegative_int(
        time_verification.get("matched_count"),
        "x_post_time_verification.matched_count",
    )
    if matched_count != len(matched):
        raise AdapterError(
            "contract_error",
            "x_post_time_verification.matched_count does not match the matched array length.",
        )

    retrieved_at = _nonempty_text(time_verification.get("as_of_utc"))
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(matched):
        if not isinstance(item, dict):
            raise AdapterError(
                "contract_error",
                f"x_post_time_verification.matched[{index}] must be an object.",
            )
        candidates.append(
            _candidate(
                item,
                index=index,
                query=query.strip(),
                call_index=call_index,
                phase=phase,
                route=route,
                retrieved_at=retrieved_at,
                structured_candidate=structured_fxtwitter.get(item.get("tweet_id")),
            )
        )

    route_limitations = [MATCHED_ONLY_LIMITATION]
    route_limitations.append(
        FXTWITTER_FIELDS_LIMITATION
        if route["backend"] == "fxtwitter-public"
        else UNKNOWN_FIELDS_LIMITATION
    )
    route_limitations.append(SNOWFLAKE_LIMITATION)
    route_status = "completed"
    if partial_notice:
        route_status = "partial"
        route_limitations.append(PARTIAL_OUTPUT_LIMITATION)
    structured_matched_count = sum(
        candidate["candidate_id"].removeprefix("x:") in structured_fxtwitter
        for candidate in candidates
    )
    if structured_truncated:
        route_status = "partial"
        route_limitations.append(
            "grok-consult truncated the FxTwitter structured output; "
            f"{len(candidates) - structured_matched_count} matched post(s) keep only "
            "URL-derived fields."
        )
    elif output_truncated:
        route_limitations.append(GROK_PROSE_TRUNCATED_LIMITATION)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "queries": [query.strip()],
            "platforms": ["x"],
            "time_range": _time_range(time_verification),
            "requested_limit": None,
        },
        "routes": [
            {
                "platform": "x",
                "backend": route["backend"],
                "mode": "search_x_with_grok",
                "login_state_used": route["login_state_used"],
                "status": route_status,
                "query": query.strip(),
                "call_index": call_index,
                "phase": phase,
                "limitations": route_limitations,
            }
        ],
        "candidates": candidates,
        "coverage": [
            {
                "platform": "x",
                "backend": route["backend"],
                "query_count": 1,
                "matched_count": matched_count,
                "excluded_outside_window_count": len(excluded),
                "returned_count": len(candidates),
                "call_index": call_index,
                "phase": phase,
                "truncated": None,
                "tool_output_truncated": output_truncated,
                "partial_output_notice": partial_notice,
                "login_state_used": route["login_state_used"],
                "limitations": route_limitations,
            }
        ],
        "errors": [],
    }

    coverage = envelope["coverage"][0]
    if route["grok_native_x_search_verified"]:
        coverage["native_x_search_completed_call_count"] = route[
            "native_search_count"
        ]
        coverage["grok_native_x_search_verified"] = True
    else:
        coverage["local_reader_verified"] = True
        coverage["local_reader_route"] = route["local_reader_route"]
    if route["local_reader_route"] == FXTWITTER_ROUTE:
        coverage["structured_matched_count"] = structured_matched_count
        coverage["structured_missing_count"] = len(candidates) - structured_matched_count
    return envelope


def error_envelope(
    *,
    query: str,
    call_index: int | None,
    phase: str,
    error: AdapterError,
) -> dict[str, Any]:
    limitation = "No network fallback was attempted after the local adapter error."
    detail = {"backend": BACKEND, "category": error.category, "message": error.message}
    if error.error_code:
        detail["error_code"] = error.error_code
    return {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "queries": [query],
            "platforms": ["x"],
            "time_range": None,
            "requested_limit": None,
        },
        "routes": [
            {
                "platform": "x",
                "backend": BACKEND,
                "mode": "search_x_with_grok",
                "login_state_used": True,
                "status": "failed",
                "query": query,
                "call_index": call_index,
                "phase": phase,
                "limitations": [limitation],
            }
        ],
        "candidates": [],
        "coverage": [],
        "errors": [detail],
    }


def read_input(path: str, *, stdin: TextIO) -> str:
    if path == "-":
        return stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdapterError("invalid_input", f"Could not read Grok result input: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline normalizer for verified search_x_with_grok results."
    )
    parser.add_argument("--input", required=True, help="Input file path or '-' for stdin")
    parser.add_argument("--query", required=True, help="The focused query for this call")
    parser.add_argument("--call-index", type=_positive_cli_int)
    parser.add_argument(
        "--phase", choices=("initial", "supplementary"), default="initial"
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = read_input(args.input, stdin=stdin)
        result = normalize_grok_result(
            raw,
            query=args.query,
            call_index=args.call_index,
            phase=args.phase,
        )
    except AdapterError as exc:
        result = error_envelope(
            query=args.query,
            call_index=args.call_index,
            phase=args.phase,
            error=exc,
        )
        json.dump(result, stdout, ensure_ascii=False, indent=2)
        stdout.write("\n")
        return 2

    json.dump(result, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
