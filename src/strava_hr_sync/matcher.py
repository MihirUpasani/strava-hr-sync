"""Match Strava treadmill activities with Fitbit activity logs by time overlap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .fitbit_client import FitbitActivity
from .strava_client import StravaActivity


@dataclass
class ActivityMatch:
    strava: StravaActivity
    fitbit: FitbitActivity
    overlap_seconds: int
    overlap_ratio: float  # overlap / min(strava_dur, fitbit_dur)


def _time_overlap(
    start_a: datetime, end_a: datetime,
    start_b: datetime, end_b: datetime,
) -> int:
    """Calculate overlap in seconds between two time ranges."""
    latest_start = max(start_a, start_b)
    earliest_end = min(end_a, end_b)
    delta = (earliest_end - latest_start).total_seconds()
    return max(0, int(delta))


def match_activities(
    strava_activities: list[StravaActivity],
    fitbit_activities: list[FitbitActivity],
    tolerance_minutes: int = 5,
    min_overlap_ratio: float = 0.5,
) -> list[ActivityMatch]:
    """Match Strava activities with Fitbit activities by time overlap.

    Args:
        strava_activities: Strava activities to match (typically treadmill runs w/o HR)
        fitbit_activities: Fitbit activity logs to match against
        tolerance_minutes: Extra buffer around activity times for matching
        min_overlap_ratio: Minimum overlap fraction required (overlap / shorter duration)

    Returns:
        List of matched activity pairs, sorted by Strava start time
    """
    tolerance = timedelta(minutes=tolerance_minutes)
    matches: list[ActivityMatch] = []

    for strava in strava_activities:
        strava_start = strava.start_date
        strava_end = strava_start + timedelta(seconds=strava.elapsed_time)

        best_match: ActivityMatch | None = None

        for fitbit in fitbit_activities:
            fitbit_start = fitbit.start_time
            fitbit_end = fitbit.end_time

            # Check overlap with tolerance buffer
            overlap = _time_overlap(
                strava_start - tolerance, strava_end + tolerance,
                fitbit_start, fitbit_end,
            )

            if overlap <= 0:
                continue

            # Calculate actual overlap (without tolerance) for ratio
            actual_overlap = _time_overlap(
                strava_start, strava_end,
                fitbit_start, fitbit_end,
            )

            shorter_duration = min(strava.elapsed_time, fitbit.duration_seconds)
            if shorter_duration == 0:
                continue

            ratio = actual_overlap / shorter_duration

            if ratio < min_overlap_ratio:
                continue

            candidate = ActivityMatch(
                strava=strava,
                fitbit=fitbit,
                overlap_seconds=actual_overlap,
                overlap_ratio=ratio,
            )

            if best_match is None or candidate.overlap_ratio > best_match.overlap_ratio:
                best_match = candidate

        if best_match is not None:
            matches.append(best_match)

    matches.sort(key=lambda m: m.strava.start_date)
    return matches
