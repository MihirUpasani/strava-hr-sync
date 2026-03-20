"""Merge Strava activity streams with Fitbit HR data into a TCX file."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from .fitbit_client import HeartRateSample


TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"


def _iso_format(dt: datetime) -> str:
    """Format datetime as ISO 8601 with Z suffix for UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _interpolate_hr(
    hr_samples: list[HeartRateSample],
    target_time: datetime,
) -> int | None:
    """Find the closest HR sample to the target time.

    Uses nearest-neighbor interpolation within a 5-second window.
    """
    if not hr_samples:
        return None

    best_sample = None
    best_delta = float("inf")

    for sample in hr_samples:
        # Make both timezone-naive for comparison
        sample_time = sample.time.replace(tzinfo=None) if sample.time.tzinfo else sample.time
        target_naive = target_time.replace(tzinfo=None) if target_time.tzinfo else target_time
        delta = abs((sample_time - target_naive).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best_sample = sample

    if best_sample is not None and best_delta <= 5.0:
        return best_sample.value
    return None


def build_tcx(
    start_time: datetime,
    streams: dict[str, list],
    hr_samples: list[HeartRateSample],
    sport: str = "Running",
) -> str:
    """Build a TCX XML document merging Strava streams with Fitbit HR data.

    Args:
        start_time: Activity start time (UTC)
        streams: Strava activity streams (keys: time, distance, altitude, cadence, etc.)
            - "time": list of seconds from start
            - "distance": list of meters from start
            - "altitude": list of meters elevation
            - "cadence": list of steps per minute
        hr_samples: Fitbit intraday HR samples
        sport: TCX sport type ("Running", "Biking", etc.)

    Returns:
        TCX XML as a string
    """
    # Register namespace to avoid ns0: prefixes
    ET.register_namespace("", TCX_NS)

    root = ET.Element(f"{{{TCX_NS}}}TrainingCenterDatabase")
    activities_elem = ET.SubElement(root, f"{{{TCX_NS}}}Activities")
    activity_elem = ET.SubElement(activities_elem, f"{{{TCX_NS}}}Activity")
    activity_elem.set("Sport", sport)

    id_elem = ET.SubElement(activity_elem, f"{{{TCX_NS}}}Id")
    id_elem.text = _iso_format(start_time)

    lap_elem = ET.SubElement(activity_elem, f"{{{TCX_NS}}}Lap")
    lap_elem.set("StartTime", _iso_format(start_time))

    time_stream = streams.get("time", [])
    distance_stream = streams.get("distance", [])
    altitude_stream = streams.get("altitude", [])
    cadence_stream = streams.get("cadence", [])

    # Lap total time
    if time_stream:
        total_time = ET.SubElement(lap_elem, f"{{{TCX_NS}}}TotalTimeSeconds")
        total_time.text = str(time_stream[-1])

    # Lap total distance
    if distance_stream:
        total_dist = ET.SubElement(lap_elem, f"{{{TCX_NS}}}DistanceMeters")
        total_dist.text = f"{distance_stream[-1]:.2f}"

    # Intensity
    intensity = ET.SubElement(lap_elem, f"{{{TCX_NS}}}Intensity")
    intensity.text = "Active"

    # Trigger method
    trigger = ET.SubElement(lap_elem, f"{{{TCX_NS}}}TriggerMethod")
    trigger.text = "Manual"

    # Track with trackpoints
    track_elem = ET.SubElement(lap_elem, f"{{{TCX_NS}}}Track")

    num_points = len(time_stream) if time_stream else 0
    hr_found = 0

    for i in range(num_points):
        tp = ET.SubElement(track_elem, f"{{{TCX_NS}}}Trackpoint")

        # Time
        point_time = start_time + timedelta(seconds=time_stream[i])
        time_elem = ET.SubElement(tp, f"{{{TCX_NS}}}Time")
        time_elem.text = _iso_format(point_time)

        # Distance
        if i < len(distance_stream):
            dist_elem = ET.SubElement(tp, f"{{{TCX_NS}}}DistanceMeters")
            dist_elem.text = f"{distance_stream[i]:.2f}"

        # Altitude
        if i < len(altitude_stream):
            alt_elem = ET.SubElement(tp, f"{{{TCX_NS}}}AltitudeMeters")
            alt_elem.text = f"{altitude_stream[i]:.1f}"

        # Heart Rate from Fitbit
        hr_value = _interpolate_hr(hr_samples, point_time)
        if hr_value is not None:
            hr_elem = ET.SubElement(tp, f"{{{TCX_NS}}}HeartRateBpm")
            value_elem = ET.SubElement(hr_elem, f"{{{TCX_NS}}}Value")
            value_elem.text = str(hr_value)
            hr_found += 1

        # Cadence
        if i < len(cadence_stream) and cadence_stream[i] is not None:
            cad_elem = ET.SubElement(tp, f"{{{TCX_NS}}}Cadence")
            # Strava reports full steps/min, TCX expects half (left foot only)
            cad_elem.text = str(cadence_stream[i] // 2)

    # Add average HR to lap if we found any
    if hr_found > 0:
        hr_values = [s.value for s in hr_samples if s.value > 0]
        if hr_values:
            avg_hr_elem = ET.SubElement(lap_elem, f"{{{TCX_NS}}}AverageHeartRateBpm")
            avg_val = ET.SubElement(avg_hr_elem, f"{{{TCX_NS}}}Value")
            avg_val.text = str(round(sum(hr_values) / len(hr_values)))

            max_hr_elem = ET.SubElement(lap_elem, f"{{{TCX_NS}}}MaximumHeartRateBpm")
            max_val = ET.SubElement(max_hr_elem, f"{{{TCX_NS}}}Value")
            max_val.text = str(max(hr_values))

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    # Serialize to string
    import io

    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue().decode("UTF-8")


def build_tcx_minimal(
    start_time: datetime,
    elapsed_seconds: int,
    distance_meters: float,
    hr_samples: list[HeartRateSample],
    sport: str = "Running",
) -> str:
    """Build a minimal TCX when Strava streams are unavailable.

    Creates trackpoints from HR samples only, with linearly interpolated distance.
    """
    if not hr_samples:
        raise ValueError("No HR samples provided")

    # Build synthetic streams from HR data
    time_stream = []
    distance_stream = []

    activity_start = start_time
    total_hr_seconds = len(hr_samples)

    # Use HR sample timestamps to create time offsets
    for i, sample in enumerate(hr_samples):
        sample_time = sample.time.replace(tzinfo=None) if sample.time.tzinfo else sample.time
        start_naive = activity_start.replace(tzinfo=None) if activity_start.tzinfo else activity_start
        offset = (sample_time - start_naive).total_seconds()
        if 0 <= offset <= elapsed_seconds + 60:
            time_stream.append(int(offset))
            # Linear interpolation of distance
            if elapsed_seconds > 0:
                fraction = offset / elapsed_seconds
                distance_stream.append(distance_meters * min(fraction, 1.0))
            else:
                distance_stream.append(0.0)

    streams = {"time": time_stream, "distance": distance_stream}
    return build_tcx(start_time, streams, hr_samples, sport)
