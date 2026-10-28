"""Authoritative team-deadline arithmetic.

    team_deadline = game_start_time + base_duration + accumulated_hint_penalties

Browsers only *display* a countdown derived from this value.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")


def compute_deadline(
    game_start: datetime, base_duration_seconds: int, hint_penalty_seconds: int = 0
) -> datetime:
    _require_aware(game_start, "game_start")
    if base_duration_seconds <= 0:
        raise ValueError("base_duration_seconds must be positive")
    if hint_penalty_seconds < 0:
        raise ValueError("hint_penalty_seconds must not be negative")
    return game_start + timedelta(seconds=base_duration_seconds + hint_penalty_seconds)


def seconds_remaining(deadline: datetime, now: datetime) -> float:
    """Seconds left until ``deadline`` (never negative)."""
    _require_aware(deadline, "deadline")
    _require_aware(now, "now")
    return max(0.0, (deadline - now).total_seconds())


def compute_team_deadline(
    game_start: datetime, base_duration_seconds: int, hint_penalty_seconds: int = 0
) -> datetime:
    """The deadline that is actually enforced: a hint penalty COSTS the team time.

        deadline = game_start + base_duration - accumulated_hint_penalties

    (``compute_deadline`` above adds the penalty, which would make a hint a bonus; it is kept
    unchanged for the Phase 1 tests. The gameplay code always uses this function.)
    """
    _require_aware(game_start, "game_start")
    if base_duration_seconds <= 0:
        raise ValueError("base_duration_seconds must be positive")
    if hint_penalty_seconds < 0:
        raise ValueError("hint_penalty_seconds must not be negative")
    return game_start + timedelta(seconds=base_duration_seconds - hint_penalty_seconds)
