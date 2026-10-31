"""Offline public URL syntax checks; no DNS resolution or fetching."""
from __future__ import annotations

import ipaddress
import re
try:
    import idna as _idna_uts46
except ImportError:
    _idna_uts46 = None

# The one host rule every public-URL validator in this Skill applies after its own
# URL parsing and UTS46 normalization (AnySearch, Firecrawl and the route checks).
NON_PUBLIC_HOSTNAMES = frozenset({"localhost"})
NON_PUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".intranet")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_LABEL_RE = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)")


def public_host_problem(host: str) -> str | None:
    """Classify an already normalized host without DNS; ``None`` means public.

    Problems: ``malformed``, ``non_public``, ``not_fully_qualified`` and
    ``ambiguous_numeric``.
    """

    if not isinstance(host, str) or not host or len(host) > 253:
        return "malformed"
    if host in NON_PUBLIC_HOSTNAMES or host.endswith(NON_PUBLIC_HOST_SUFFIXES):
        return "non_public"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return None if address.is_global else "non_public"
    labels = host.split(".")
    if len(labels) < 2:
        return "not_fully_qualified"
    if all(_NUMERIC_LABEL_RE.fullmatch(label) for label in labels):
        return "ambiguous_numeric"
    if any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        return "malformed"
    return None


def _normalize_host_uts46(raw_host: str) -> str | None:
    """Normalize one route host without legacy IDNA2003 target changes."""

    host_input = raw_host.rstrip(".")
    if not host_input or "%" in host_input:
        return None
    try:
        literal = ipaddress.ip_address(host_input)
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal).lower()

    if _idna_uts46 is None:
        if any(ord(character) > 0x7F for character in host_input):
            return None
        try:
            return host_input.encode("ascii").decode("ascii").lower()
        except UnicodeError:
            return None
    try:
        return _idna_uts46.encode(
            host_input,
            uts46=True,
            transitional=False,
            std3_rules=True,
        ).decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None


def is_public_http_url(value: str) -> bool:
    """Strict offline check for a literal public HTTP(S) site-map seed."""

    if not isinstance(value, str) or not value.strip():
        return False
    cleaned = value.strip()
    if "\\" in cleaned:
        return False
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in cleaned):
        return False
    parsed = re.fullmatch(
        r"https?://([^/?#]+)(?:[/?#].*)?", cleaned, flags=re.IGNORECASE
    )
    if parsed is None:
        return False
    authority = parsed.group(1)
    if "@" in authority:
        return False
    port: int | None = None
    if authority.startswith("["):
        closing = authority.find("]")
        if closing <= 1:
            return False
        raw_host = authority[1:closing]
        remainder = authority[closing + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                return False
            port = int(remainder[1:])
    else:
        if "[" in authority or "]" in authority or authority.count(":") > 1:
            return False
        if ":" in authority:
            raw_host, port_text = authority.rsplit(":", 1)
            if not port_text.isdigit():
                return False
            port = int(port_text)
        else:
            raw_host = authority
    if port is not None and not 1 <= port <= 65535:
        return False
    host = _normalize_host_uts46(raw_host)
    if host is None:
        return False
    return public_host_problem(host) is None
