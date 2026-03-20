"""Strava API client for listing, reading, deleting, and uploading activities."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from dateutil.parser import isoparse

from .auth import get_strava_client, refresh_strava_token
from .rate_limiter import RateLimiter

STRAVA_RATE_LIMITER = RateLimiter(
    short_limit=200, short_window=900,  # 200 per 15 min
    long_limit=2000, long_window=86400,  # 2000 per day
)


@dataclass
class StravaActivity:
    id: int
    name: str
    sport_type: str
    start_date: datetime
    elapsed_time: int  # seconds
    distance: float  # meters
    has_heartrate: bool
    trainer: bool
    description: str = ""
    gear_id: str | None = None
    commute: bool = False
    hide_from_home: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _parse_activity(data: dict[str, Any]) -> StravaActivity:
    return StravaActivity(
        id=data["id"],
        name=data["name"],
        sport_type=data.get("sport_type", data.get("type", "Run")),
        start_date=isoparse(data["start_date"]),
        elapsed_time=data["elapsed_time"],
        distance=data.get("distance", 0.0),
        has_heartrate=data.get("has_heartrate", False),
        trainer=data.get("trainer", False),
        description=data.get("description", ""),
        gear_id=data.get("gear_id"),
        commute=data.get("commute", False),
        hide_from_home=data.get("hide_from_home", False),
        raw=data,
    )


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """Make an API request with rate limiting and token refresh."""
    STRAVA_RATE_LIMITER.wait()
    resp = client.request(method, url, **kwargs)

    if resp.status_code == 401:
        # Try refreshing the token
        tokens = client._tokens  # type: ignore[attr-defined]
        new_tokens = refresh_strava_token(tokens)
        client.headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
        client._tokens = new_tokens  # type: ignore[attr-defined]
        STRAVA_RATE_LIMITER.wait()
        resp = client.request(method, url, **kwargs)

    resp.raise_for_status()
    return resp


def list_activities(
    client: httpx.Client,
    after: datetime | None = None,
    before: datetime | None = None,
    per_page: int = 100,
) -> list[StravaActivity]:
    """Fetch all activities, paginating automatically."""
    activities: list[StravaActivity] = []
    page = 1

    while True:
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if after:
            params["after"] = int(after.timestamp())
        if before:
            params["before"] = int(before.timestamp())

        resp = _request(client, "GET", "/athlete/activities", params=params)
        batch = resp.json()
        if not batch:
            break
        activities.extend(_parse_activity(a) for a in batch)
        if len(batch) < per_page:
            break
        page += 1

    return activities


def get_treadmill_runs_without_hr(
    client: httpx.Client,
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[StravaActivity]:
    """Get treadmill runs (trainer=true, type=Run) that lack heart rate data."""
    all_activities = list_activities(client, after=after, before=before)
    return [
        a
        for a in all_activities
        if a.trainer
        and a.sport_type in ("Run", "VirtualRun")
        and not a.has_heartrate
    ]


def get_activity_streams(
    client: httpx.Client,
    activity_id: int,
    keys: tuple[str, ...] = ("time", "distance", "altitude", "cadence", "velocity_smooth"),
) -> dict[str, list]:
    """Get data streams for an activity."""
    resp = _request(
        client,
        "GET",
        f"/activities/{activity_id}/streams",
        params={"keys": ",".join(keys), "key_type": "time"},
    )
    data = resp.json()
    return {stream["type"]: stream["data"] for stream in data}


def get_activity_detail(client: httpx.Client, activity_id: int) -> dict[str, Any]:
    """Get full activity details (used for capturing metadata before delete)."""
    resp = _request(client, "GET", f"/activities/{activity_id}")
    return resp.json()


def delete_activity(client: httpx.Client, activity_id: int) -> None:
    """Delete an activity."""
    _request(client, "DELETE", f"/activities/{activity_id}")


def upload_tcx(
    client: httpx.Client,
    tcx_content: str,
    activity_type: str = "run",
    name: str | None = None,
    description: str | None = None,
    trainer: bool = True,
    data_type: str = "tcx",
) -> int:
    """Upload a TCX file and return the new activity ID.

    Polls the upload status until processing is complete.
    """
    data: dict[str, Any] = {
        "data_type": data_type,
        "activity_type": activity_type,
        "trainer": "1" if trainer else "0",
    }
    if name:
        data["name"] = name
    if description:
        data["description"] = description

    resp = _request(
        client,
        "POST",
        "/uploads",
        files={"file": ("activity.tcx", tcx_content.encode(), "application/xml")},
        data=data,
    )
    upload = resp.json()
    upload_id = upload["id"]

    # Poll for completion
    for _ in range(60):
        time.sleep(2)
        status_resp = _request(client, "GET", f"/uploads/{upload_id}")
        status = status_resp.json()

        if status.get("error"):
            raise RuntimeError(f"Strava upload error: {status['error']}")
        if status.get("activity_id"):
            return status["activity_id"]
        # Still processing
        if status.get("status") == "Your activity is still being processed.":
            continue

    raise RuntimeError("Upload processing timed out after 2 minutes")


def update_activity_metadata(
    client: httpx.Client,
    activity_id: int,
    name: str | None = None,
    description: str | None = None,
    sport_type: str | None = None,
    gear_id: str | None = None,
    trainer: bool | None = None,
    commute: bool | None = None,
    hide_from_home: bool | None = None,
) -> dict[str, Any]:
    """Update metadata on an existing activity."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if sport_type is not None:
        body["sport_type"] = sport_type
    if gear_id is not None:
        body["gear_id"] = gear_id
    if trainer is not None:
        body["trainer"] = trainer
    if commute is not None:
        body["commute"] = commute
    if hide_from_home is not None:
        body["hide_from_home"] = hide_from_home

    resp = _request(client, "PUT", f"/activities/{activity_id}", json=body)
    return resp.json()


def seamless_replace(
    client: httpx.Client,
    original: StravaActivity,
    tcx_content: str,
) -> int:
    """Delete original activity and upload enriched TCX, restoring metadata.

    Returns the new activity ID.
    """
    # Capture full metadata before deletion
    detail = get_activity_detail(client, original.id)

    # Delete the old activity
    delete_activity(client, original.id)

    # Upload the HR-enriched version
    new_id = upload_tcx(
        client,
        tcx_content,
        activity_type="run",
        name=detail.get("name", original.name),
        description=detail.get("description", ""),
        trainer=detail.get("trainer", True),
    )

    # Restore all metadata
    update_activity_metadata(
        client,
        new_id,
        name=detail.get("name"),
        description=detail.get("description"),
        sport_type=detail.get("sport_type", "Run"),
        gear_id=detail.get("gear_id"),
        trainer=detail.get("trainer", True),
        commute=detail.get("commute", False),
        hide_from_home=detail.get("hide_from_home", False),
    )

    return new_id
