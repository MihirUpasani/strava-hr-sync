"""Tests for the TCX merger."""

from datetime import datetime, timezone

from strava_hr_sync.fitbit_client import HeartRateSample
from strava_hr_sync.merger import _interpolate_hr, build_tcx, build_tcx_minimal


def _sample(time_str: str, bpm: int) -> HeartRateSample:
    """Create a HeartRateSample from time string and BPM."""
    t = datetime.fromisoformat(f"2024-06-01T{time_str}").replace(tzinfo=timezone.utc)
    return HeartRateSample(time=t, value=bpm)


def test_interpolate_hr_exact_match():
    """HR interpolation returns exact value when timestamp matches."""
    samples = [_sample("07:00:00", 120), _sample("07:00:01", 122)]
    target = datetime(2024, 6, 1, 7, 0, 0, tzinfo=timezone.utc)
    assert _interpolate_hr(samples, target) == 120


def test_interpolate_hr_nearest():
    """HR interpolation picks the nearest sample within 5s window."""
    samples = [_sample("07:00:00", 120), _sample("07:00:04", 130)]
    target = datetime(2024, 6, 1, 7, 0, 3, tzinfo=timezone.utc)
    assert _interpolate_hr(samples, target) == 130  # 1s away vs 3s away


def test_interpolate_hr_out_of_range():
    """HR interpolation returns None when no sample within 5 seconds."""
    samples = [_sample("07:00:00", 120)]
    target = datetime(2024, 6, 1, 7, 0, 10, tzinfo=timezone.utc)
    assert _interpolate_hr(samples, target) is None


def test_interpolate_hr_empty():
    """HR interpolation returns None for empty samples."""
    assert _interpolate_hr([], datetime(2024, 6, 1, 7, 0, 0, tzinfo=timezone.utc)) is None


def test_build_tcx_basic():
    """Build TCX produces valid XML with HR data."""
    start = datetime(2024, 6, 1, 7, 0, 0, tzinfo=timezone.utc)
    streams = {
        "time": [0, 1, 2, 3, 4],
        "distance": [0.0, 2.5, 5.0, 7.5, 10.0],
    }
    hr_samples = [
        _sample("07:00:00", 100),
        _sample("07:00:01", 110),
        _sample("07:00:02", 120),
        _sample("07:00:03", 130),
        _sample("07:00:04", 140),
    ]

    tcx = build_tcx(start, streams, hr_samples)

    assert '<?xml version' in tcx
    assert "TrainingCenterDatabase" in tcx
    assert 'Sport="Running"' in tcx
    assert "<Value>100</Value>" in tcx
    assert "<Value>140</Value>" in tcx
    # Average HR should be 120
    assert "<Value>120</Value>" in tcx


def test_build_tcx_with_altitude_and_cadence():
    """Build TCX includes altitude and cadence when present."""
    start = datetime(2024, 6, 1, 7, 0, 0, tzinfo=timezone.utc)
    streams = {
        "time": [0, 1],
        "distance": [0.0, 3.0],
        "altitude": [100.0, 100.5],
        "cadence": [180, 182],
    }
    hr_samples = [_sample("07:00:00", 120), _sample("07:00:01", 125)]

    tcx = build_tcx(start, streams, hr_samples)

    assert "AltitudeMeters" in tcx
    assert "100.0" in tcx
    assert "Cadence" in tcx
    # Cadence in TCX is half-steps
    assert "<Cadence>90</Cadence>" in tcx


def test_build_tcx_no_hr_data():
    """Build TCX without HR data still produces valid XML."""
    start = datetime(2024, 6, 1, 7, 0, 0, tzinfo=timezone.utc)
    streams = {"time": [0, 1, 2], "distance": [0.0, 3.0, 6.0]}
    hr_samples = []  # No HR data

    tcx = build_tcx(start, streams, hr_samples)

    assert "TrainingCenterDatabase" in tcx
    assert "HeartRateBpm" not in tcx


def test_build_tcx_minimal():
    """Minimal TCX builds from just HR samples and basic info."""
    start = datetime(2024, 6, 1, 7, 0, 0, tzinfo=timezone.utc)
    hr_samples = [_sample(f"07:00:{i:02d}", 100 + i) for i in range(10)]

    tcx = build_tcx_minimal(start, 60, 200.0, hr_samples)

    assert "TrainingCenterDatabase" in tcx
    assert "HeartRateBpm" in tcx
    assert "DistanceMeters" in tcx
