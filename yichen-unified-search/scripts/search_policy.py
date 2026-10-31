"""Planner capabilities that prevent silent query loss and silently ignored options.

Native multi-query search is currently implemented only by X, AI HOT and web.
Other native backends either require one query or expose explicit batch semantics.
Each planner route reads its query, result-limit, time-window and vertical-domain
bounds from PLATFORM_CAPABILITIES so the CLI, the planner and the emitted steps agree.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformCapability:
    single_query: bool  # native --mode search accepts exactly one --query
    max_results: int | None = None  # per-call result cap the planner applies
    days_range: tuple[int, int] | None = None  # inclusive native --days range; None = no native window
    days_values: frozenset[int] | None = None  # discrete native windows inside days_range
    default_days: int | None = None  # window the planner forwards when --days is absent
    rejects_days: bool = False  # --days is an error instead of a reported no-op
    domain_routing: bool = False  # may combine with AnySearch --domain / --hybrid


PLATFORM_CAPABILITIES = {
    # AnySearch does not translate --days; the planner reports it as a limitation.
    "web": PlatformCapability(single_query=False, max_results=10, domain_routing=True),
    "aihot": PlatformCapability(single_query=False, max_results=100, days_range=(1, 7)),
    "github": PlatformCapability(single_query=True),
    "zhihu": PlatformCapability(single_query=False, max_results=10, rejects_days=True),
    "wechat": PlatformCapability(single_query=True, max_results=10),
    # The OpenCLI Xiaohongshu and Douyin search filters only know these windows.
    "xiaohongshu": PlatformCapability(
        single_query=True, max_results=20, days_range=(0, 180),
        days_values=frozenset({0, 1, 7, 180}), default_days=1,
    ),
    "douyin": PlatformCapability(
        single_query=True, max_results=30, days_range=(0, 180),
        days_values=frozenset({0, 1, 7, 180}), default_days=1,
    ),
    "toutiao": PlatformCapability(
        single_query=True, max_results=50, days_range=(1, 30), default_days=1
    ),
    # Grok hours is limited to 168.
    "x": PlatformCapability(single_query=False, max_results=20, days_range=(1, 7)),
    "bilibili": PlatformCapability(single_query=True),
    # 0 means no window; the adapter accepts 1-3650.
    "youtube": PlatformCapability(single_query=True, days_range=(0, 3650)),
    "xiaoyuzhou": PlatformCapability(single_query=True, max_results=10),
    "weibo": PlatformCapability(single_query=False, max_results=20, days_range=(1, 180)),
}

SINGLE_QUERY_PLATFORMS = frozenset(
    name for name, capability in PLATFORM_CAPABILITIES.items() if capability.single_query
)
# The CLI accepts the widest platform window; each platform route narrows it.
MAX_DAYS = max(
    capability.days_range[1]
    for capability in PLATFORM_CAPABILITIES.values()
    if capability.days_range is not None
)


def clamp_results(platform: str, limit: int) -> int:
    maximum = PLATFORM_CAPABILITIES[platform].max_results
    return limit if maximum is None else min(limit, maximum)


def days_error(platform: str, days: int) -> str | None:
    """Return the rejection reason when a native route cannot honor --days."""

    capability = PLATFORM_CAPABILITIES[platform]
    if capability.days_values is not None:
        if days in capability.days_values:
            return None
        values = [str(value) for value in sorted(capability.days_values)]
        return f"{platform} --days must be one of {', '.join(values[:-1])}, or {values[-1]}"
    if capability.days_range is None:
        return None
    low, high = capability.days_range
    if low <= days <= high:
        return None
    if low == 0:
        return f"{platform} --days must be an integer from 0 to {high} (0 means no time limit)"
    return f"{platform} --days must be between {low} and {high}"
