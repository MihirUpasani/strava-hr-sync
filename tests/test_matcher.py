"""Tests for the activity matcher."""

from datetime import datetime, timezone

from strava_hr_sync.fitbit_client import FitbitActivity
from strava_hr_sync.matcher import ActivityMatch, match_activities
from strava_hr_sync.strava_client import StravaActivity


def _strava(name: str, start: str, elapsed: int, **kw) -> StravaActivity:
    return StravaActivity(
        id=kw.get("id", 1),
        name=name,
        sport_type="Run",
        start_date=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        elapsed_time=elapsed,
        distance=kw.get("distance", 5000.0),
        has_heartrate=False,
        trainer=True,
    )


def _fitbit(name: str, start: str, duration_ms: int, **kw) -> FitbitActivity:
    return FitbitActivity(
        log_id=kw.get("log_id", 100),
        activity_name=name,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        duration_ms=duration_ms,
        calories=kw.get("calories", 300),
        distance=kw.get("distance", 5.0),
    )


def test_exact_overlap():
    """Activities with identical start times should match."""
    strava_acts = [_strava("Morning Run", "2024-06-01T07:00:00", 1800)]
    fitbit_acts = [_fitbit("Treadmill", "2024-06-01T07:00:00", 1800_000)]

    matches = match_activities(strava_acts, fitbit_acts)
    assert len(matches) == 1
    assert matches[0].overlap_ratio == 1.0


def test_offset_within_tolerance():
    """Activities offset by less than tolerance should still match."""
    strava_acts = [_strava("Morning Run", "2024-06-01T07:00:00", 1800)]
    fitbit_acts = [_fitbit("Treadmill", "2024-06-01T07:03:00", 1800_000)]

    matches = match_activities(strava_acts, fitbit_acts, tolerance_minutes=5)
    assert len(matches) == 1
    assert matches[0].overlap_seconds > 0


def test_no_overlap():
    """Activities hours apart should not match."""
    strava_acts = [_strava("Morning Run", "2024-06-01T07:00:00", 1800)]
    fitbit_acts = [_fitbit("Afternoon Walk", "2024-06-01T15:00:00", 1800_000)]

    matches = match_activities(strava_acts, fitbit_acts)
    assert len(matches) == 0


def test_best_match_selected():
    """When multiple Fitbit activities overlap, the best match wins."""
    strava_acts = [_strava("Morning Run", "2024-06-01T07:00:00", 1800)]
    fitbit_acts = [
        _fitbit("Warmup", "2024-06-01T06:50:00", 600_000, log_id=101),  # partial overlap
        _fitbit("Treadmill", "2024-06-01T07:00:00", 1800_000, log_id=102),  # exact overlap
    ]

    matches = match_activities(strava_acts, fitbit_acts)
    assert len(matches) == 1
    assert matches[0].fitbit.log_id == 102


def test_insufficient_overlap():
    """Activities with overlap below min_overlap_ratio should not match."""
    strava_acts = [_strava("Long Run", "2024-06-01T07:00:00", 3600)]
    # Only 2 min overlap out of 10 min Fitbit activity = 20%
    fitbit_acts = [_fitbit("Short Walk", "2024-06-01T07:58:00", 600_000)]

    matches = match_activities(strava_acts, fitbit_acts, min_overlap_ratio=0.5)
    assert len(matches) == 0


def test_multiple_matches():
    """Multiple Strava activities can each match a Fitbit activity."""
    strava_acts = [
        _strava("Run 1", "2024-06-01T07:00:00", 1800, id=1),
        _strava("Run 2", "2024-06-01T18:00:00", 1800, id=2),
    ]
    fitbit_acts = [
        _fitbit("Morning", "2024-06-01T07:00:00", 1800_000, log_id=101),
        _fitbit("Evening", "2024-06-01T18:00:00", 1800_000, log_id=102),
    ]

    matches = match_activities(strava_acts, fitbit_acts)
    assert len(matches) == 2
    assert matches[0].strava.id == 1
    assert matches[1].strava.id == 2


def test_empty_inputs():
    """Empty activity lists produce no matches."""
    assert match_activities([], []) == []
    assert match_activities([_strava("Run", "2024-06-01T07:00:00", 1800)], []) == []
    assert match_activities([], [_fitbit("Run", "2024-06-01T07:00:00", 1800_000)]) == []
