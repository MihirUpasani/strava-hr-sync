"""Fitbit API client for listing activities and fetching intraday heart rate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from dateutil.parser import isoparse

from .auth import load_tokens, refresh_fitbit_token
from .rate_limiter import RateLimiter

FITBIT_RATE_LIMITER = RateLimiter(
    short_limit=150, short_window=3600,  # 150 per hour
)


@dataclass
class FitbitActivity:
    log_id: int
    activity_name: str
    start_time: datetime  # UTC
    duration_ms: int
    calories: int
    distance: float  # km
    heart_rate_zones: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def duration_seconds(self) -> int:
        return self.duration_ms // 1000

    @property
    def end_time(self) -> datetime:
        return self.start_time + timedelta(milliseconds=self.duration_ms)


@dataclass
class HeartRateSample:
    time: datetime
    value: int  # bpm


def _parse_activity(data: dict[str, Any]) -> FitbitActivity:
    # startTime can be in different formats
    start = isoparse(data["startTime"])
    return FitbitActivity(
        log_id=data["logId"],
        activity_name=data.get("activityName", ""),
        start_time=start,
        duration_ms=data.get("activeDuration", data.get("duration", 0)),
        calories=data.get("calories", 0),
        distance=data.get("distance", 0.0),
        heart_rate_zones=data.get("heartRateZones", []),
        raw=data,
    )


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """Make an API request with rate limiting and token refresh on 401."""
    FITBIT_RATE_LIMITER.wait()
    resp = client.request(method, url, **kwargs)

    if resp.status_code == 401:
        tokens = client._tokens  # type: ignore[attr-defined]
        new_tokens = refresh_fitbit_token(tokens)
        client.headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
        client._tokens = new_tokens  # type: ignore[attr-defined]
        FITBIT_RATE_LIMITER.wait()
        resp = client.request(method, url, **kwargs)

    resp.raise_for_status()
    return resp


def list_activities(
    client: httpx.Client,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 100,
) -> list[FitbitActivity]:
    """List Fitbit activity logs, paginating via 'next' links."""
    activities: list[FitbitActivity] = []
    params: dict[str, Any] = {"sort": "asc", "limit": min(limit, 100), "offset": 0}

    if after:
        params["afterDate"] = after.strftime("%Y-%m-%d")
    else:
        params["afterDate"] = "2020-01-01"

    while True:
        resp = _request(client, "GET", "/1/user/-/activities/list.json", params=params)
        data = resp.json()
        batch = data.get("activities", [])
        if not batch:
            break

        for a in batch:
            activity = _parse_activity(a)
            if before and activity.start_time >= before:
                return activities
            activities.append(activity)

        # Fitbit uses pagination with 'next' URL
        pagination = data.get("pagination", {})
        next_url = pagination.get("next")
        if not next_url:
            break

        # Extract offset from next URL
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(next_url)
        next_params = parse_qs(parsed.query)
        params["offset"] = int(next_params.get("offset", [str(params["offset"] + len(batch))])[0])

    return activities


def get_intraday_hr(
    client: httpx.Client,
    date: datetime,
    start_time: str,
    end_time: str,
    detail_level: str = "1sec",
) -> list[HeartRateSample]:
    """Fetch intraday heart rate data for a time range on a given date.

    Args:
        date: The date to query
        start_time: HH:mm format (e.g. "06:30")
        end_time: HH:mm format (e.g. "07:15")
        detail_level: "1sec" or "1min"

    Returns:
        List of HeartRateSample with wall-clock times
    """
    date_str = date.strftime("%Y-%m-%d")
    url = f"/1/user/-/activities/heart/date/{date_str}/1d/{detail_level}/time/{start_time}/{end_time}.json"

    resp = _request(client, "GET", url)
    data = resp.json()

    # Navigate the nested response structure
    intraday = data.get("activities-heart-intraday", {})
    dataset = intraday.get("dataset", [])

    samples = []
    for point in dataset:
        # point = {"time": "06:30:00", "value": 142}
        t = datetime.strptime(f"{date_str} {point['time']}", "%Y-%m-%d %H:%M:%S")
        # Preserve the original date's timezone info if available
        if date.tzinfo:
            t = t.replace(tzinfo=date.tzinfo)
        samples.append(HeartRateSample(time=t, value=point["value"]))

    return samples


def get_hr_for_activity(
    client: httpx.Client,
    activity: FitbitActivity,
) -> list[HeartRateSample]:
    """Fetch intraday HR data covering a Fitbit activity's time window.

    Adds a 1-minute buffer on each side to ensure full coverage.
    """
    # Use the activity's local start/end times
    start = activity.start_time - timedelta(minutes=1)
    end = activity.end_time + timedelta(minutes=1)

    start_time_str = start.strftime("%H:%M")
    end_time_str = end.strftime("%H:%M")

    # Handle midnight crossing
    if end.date() > start.date():
        # Split into two requests
        samples_day1 = get_intraday_hr(
            client, start, start_time_str, "23:59", detail_level="1sec"
        )
        samples_day2 = get_intraday_hr(
            client, end, "00:00", end_time_str, detail_level="1sec"
        )
        return samples_day1 + samples_day2

    return get_intraday_hr(
        client, start, start_time_str, end_time_str, detail_level="1sec"
    )
