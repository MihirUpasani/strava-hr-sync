"""Strava API client for listing, reading, deleting, and uploading activities."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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
        and not a.name.startswith("[DELETE ME]")
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
    external_id: str | None = None,
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
    if external_id:
        data["external_id"] = external_id

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


PENDING_DIR = Path.home() / ".config" / "strava-hr-sync" / "pending"


def _save_pending(original_id: int, detail: dict[str, Any], tcx_content: str) -> _Path:
    """Save a TCX file and metadata for later upload after the user deletes the original."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    tcx_path = PENDING_DIR / f"{original_id}.tcx"
    meta_path = PENDING_DIR / f"{original_id}.json"
    tcx_path.write_text(tcx_content)
    meta_path.write_text(json.dumps(detail, default=str, indent=2))
    return tcx_path


def load_pending() -> list[tuple[int, str, dict[str, Any]]]:
    """Load all pending TCX uploads. Returns list of (original_id, tcx, metadata)."""
    if not PENDING_DIR.exists():
        return []
    pending = []
    for tcx_path in sorted(PENDING_DIR.glob("*.tcx")):
        original_id = int(tcx_path.stem)
        meta_path = PENDING_DIR / f"{original_id}.json"
        if meta_path.exists():
            tcx_content = tcx_path.read_text()
            detail = json.loads(meta_path.read_text())
            pending.append((original_id, tcx_content, detail))
    return pending


def clear_pending(original_id: int) -> None:
    """Remove pending files for a successfully uploaded activity."""
    for ext in (".tcx", ".json"):
        path = PENDING_DIR / f"{original_id}{ext}"
        path.unlink(missing_ok=True)


def _upload_and_restore(
    client: httpx.Client,
    tcx_content: str,
    detail: dict[str, Any],
) -> int:
    """Upload a TCX file and restore the original activity's metadata."""
    new_id = upload_tcx(
        client,
        tcx_content,
        activity_type="run",
        name=detail.get("name", ""),
        description=detail.get("description", ""),
        trainer=detail.get("trainer", True),
    )

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


def seamless_replace(
    client: httpx.Client,
    original: StravaActivity,
    tcx_content: str,
) -> int | None:
    """Replace an activity with an HR-enriched version.

    Tries to delete the original and upload the new version. If deletion
    fails (common for non-approved Strava apps), saves the TCX locally
    and marks the original for manual deletion.

    Returns the new activity ID, or None if the upload is pending.
    """
    detail = get_activity_detail(client, original.id)

    # Try to delete the original first
    try:
        delete_activity(client, original.id)
    except Exception:
        # Can't delete — save for later and mark the original
        _save_pending(original.id, detail, tcx_content)
        old_name = detail.get("name", original.name)
        update_activity_metadata(
            client,
            original.id,
            name=f"[DELETE ME] {old_name}",
            description="Replaced by HR-enriched version. Delete this, then re-run sync.",
            hide_from_home=True,
        )
        return None

    return _upload_and_restore(client, tcx_content, detail)


def upload_pending_tcx(
    client: httpx.Client,
    original_id: int,
    tcx_content: str,
    detail: dict[str, Any],
) -> int:
    """Upload a previously saved pending TCX file and restore metadata."""
    new_id = _upload_and_restore(client, tcx_content, detail)
    clear_pending(original_id)
    return new_id
