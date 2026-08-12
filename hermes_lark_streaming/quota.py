"""Shared GPT quota presentation thresholds."""

from __future__ import annotations

GPT_QUOTA_WARNING_REMAINING_PERCENT = 20
GPT_QUOTA_HEALTHY_REMAINING_PERCENT = 50


def _quota_color(remaining: float) -> str:
    """Map GPT quota remaining percentage to its shared semantic color."""
    if remaining >= GPT_QUOTA_HEALTHY_REMAINING_PERCENT:
        return "green"
    if remaining >= GPT_QUOTA_WARNING_REMAINING_PERCENT:
        return "orange"
    return "red"
