"""CLI entry point for strava-hr-sync."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import click
import httpx

from . import __version__


@click.group()
@click.version_option(version=__version__)
def cli():
    """Sync Fitbit heart rate data into Strava treadmill activities."""
    pass


@cli.group()
def auth():
    """Authenticate with Strava or Fitbit."""
    pass


@auth.command("strava")
@click.option("--client-id", envvar="STRAVA_CLIENT_ID", required=True, help="Strava Client ID")
@click.option(
    "--client-secret", envvar="STRAVA_CLIENT_SECRET", required=True, help="Strava Client Secret"
)
def auth_strava(client_id: str, client_secret: str):
    """Authenticate with Strava (opens browser)."""
    from .auth import authenticate_strava

    authenticate_strava(client_id, client_secret)


@auth.command("fitbit")
@click.option("--client-id", envvar="FITBIT_CLIENT_ID", required=True, help="Fitbit Client ID")
@click.option(
    "--client-secret", envvar="FITBIT_CLIENT_SECRET", required=True, help="Fitbit Client Secret"
)
def auth_fitbit(client_id: str, client_secret: str):
    """Authenticate with Fitbit (opens browser)."""
    from .auth import authenticate_fitbit

    authenticate_fitbit(client_id, client_secret)


def _process_pending(strava: httpx.Client) -> None:
    """Upload any pending TCX files from previous runs."""
    from .strava_client import load_pending, upload_pending_tcx

    pending = load_pending()
    if not pending:
        return

    click.echo(f"Found {len(pending)} pending upload(s) from previous runs.\n")
    uploaded = 0
    for original_id, tcx_content, detail in pending:
        name = detail.get("name", f"Activity {original_id}")
        try:
            new_id = upload_pending_tcx(strava, original_id, tcx_content, detail)
            click.echo(f"  Uploaded: {name} -> new ID: {new_id}")
            uploaded += 1
        except RuntimeError as e:
            if "duplicate" in str(e).lower():
                click.echo(f"  {name}: still a duplicate — delete the "
                           f"[DELETE ME] activity on Strava first")
            else:
                click.echo(f"  {name}: upload failed — {e}")
    if uploaded:
        click.echo(f"\n{uploaded} pending upload(s) completed!")
    click.echo()


def _process_matches(
    strava: httpx.Client,
    fitbit: httpx.Client,
    matches: list,
    use_minimal_fallback: bool = False,
) -> tuple[int, int, int]:
    """Process matched activity pairs: fetch HR, build TCX, replace.

    Returns (replaced, pending, failed) counts.
    """
    from .fitbit_client import get_hr_for_activity
    from .merger import build_tcx, build_tcx_minimal
    from .strava_client import get_activity_streams, seamless_replace

    replaced = 0
    pending_count = 0
    failed = 0

    for i, m in enumerate(matches, 1):
        label = f"[{i}/{len(matches)}] " if len(matches) > 1 else ""
        click.echo(f"\n{label}Processing: {m.strava.name}...")

        try:
            # Get Strava streams
            try:
                streams = get_activity_streams(strava, m.strava.id)
            except Exception:
                streams = {}

            # Get Fitbit HR
            hr_samples = get_hr_for_activity(fitbit, m.fitbit)
            click.echo(f"  Got {len(hr_samples)} HR samples from Fitbit")

            if not hr_samples:
                click.echo("  Skipping — no HR data available from Fitbit")
                failed += 1
                continue

            # Build TCX
            if streams and "time" in streams:
                tcx = build_tcx(m.strava.start_date, streams, hr_samples)
            elif use_minimal_fallback:
                tcx = build_tcx_minimal(
                    m.strava.start_date,
                    m.strava.elapsed_time,
                    m.strava.distance,
                    hr_samples,
                )
            else:
                tcx = build_tcx(m.strava.start_date, streams, hr_samples)

            # Seamless replace
            new_id = seamless_replace(strava, m.strava, tcx)
            if new_id:
                click.echo(f"  Done! New activity ID: {new_id}")
                replaced += 1
            else:
                click.echo(f"  Saved for later — delete '[DELETE ME] {m.strava.name}' "
                           f"on Strava, then re-run sync")
                pending_count += 1

        except Exception as e:
            click.echo(f"  Error: {e}", err=True)
            failed += 1

    return replaced, pending_count, failed


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview changes without modifying anything")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--days", default=30, help="Look back this many days (default: 30)")
def sync(dry_run: bool, yes: bool, days: int):
    """Find and sync recent treadmill activities missing HR data."""
    from .auth import get_fitbit_client, get_strava_client
    from .fitbit_client import list_activities as fitbit_list
    from .matcher import match_activities
    from .strava_client import get_treadmill_runs_without_hr

    after = datetime.now(timezone.utc) - timedelta(days=days)

    strava = get_strava_client()
    fitbit = get_fitbit_client()

    try:
        _process_pending(strava)

        click.echo(f"Looking for treadmill runs without HR in the last {days} days...\n")

        strava_activities = get_treadmill_runs_without_hr(strava, after=after)
        if not strava_activities:
            click.echo("No treadmill runs without HR data found on Strava.")
            return

        click.echo(f"Found {len(strava_activities)} Strava treadmill run(s) without HR:")
        for a in strava_activities:
            click.echo(f"  - {a.name} ({a.start_date.strftime('%Y-%m-%d %H:%M')}, "
                        f"{a.elapsed_time // 60}min, {a.distance:.0f}m)")

        click.echo(f"\nFetching Fitbit activity logs...")
        fitbit_activities = fitbit_list(fitbit, after=after)
        click.echo(f"Found {len(fitbit_activities)} Fitbit activities.")

        matches = match_activities(strava_activities, fitbit_activities)
        if not matches:
            click.echo("\nNo matching Fitbit activities found for these runs.")
            return

        click.echo(f"\nMatched {len(matches)} activity pair(s):")
        for m in matches:
            click.echo(f"  - Strava: {m.strava.name} <-> Fitbit: {m.fitbit.activity_name} "
                        f"(overlap: {m.overlap_ratio:.0%})")

        if dry_run:
            click.echo("\n[DRY RUN] No changes made.")
            return

        if not yes:
            click.confirm("\nProceed with syncing these activities?", abort=True)

        replaced, pending_count, _ = _process_matches(strava, fitbit, matches)

        click.echo(f"\nSync complete!")
        if replaced:
            click.echo(f"  {replaced} activity(ies) replaced with HR data.")
        if pending_count:
            click.echo(f"  {pending_count} activity(ies) pending — delete the "
                       f"'[DELETE ME]' activities on Strava, then re-run sync to upload.")
    finally:
        strava.close()
        fitbit.close()


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview changes without modifying anything")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option(
    "--after",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Start date for backfill (default: all time)",
)
@click.option(
    "--before",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="End date for backfill (default: now)",
)
def backfill(dry_run: bool, yes: bool, after: datetime | None, before: datetime | None):
    """Backfill HR data for all historical treadmill runs."""
    from .auth import get_fitbit_client, get_strava_client
    from .fitbit_client import list_activities as fitbit_list
    from .matcher import match_activities
    from .strava_client import get_treadmill_runs_without_hr

    click.echo("Backfilling HR data for historical treadmill runs...\n")

    if after:
        after = after.replace(tzinfo=timezone.utc)
    if before:
        before = before.replace(tzinfo=timezone.utc)

    strava = get_strava_client()
    fitbit = get_fitbit_client()

    try:
        _process_pending(strava)

        strava_activities = get_treadmill_runs_without_hr(strava, after=after, before=before)
        if not strava_activities:
            click.echo("No treadmill runs without HR data found on Strava.")
            return

        click.echo(f"Found {len(strava_activities)} Strava treadmill run(s) without HR.\n")

        fitbit_activities = fitbit_list(fitbit, after=after, before=before)
        click.echo(f"Found {len(fitbit_activities)} Fitbit activities.\n")

        matches = match_activities(strava_activities, fitbit_activities)
        if not matches:
            click.echo("No matching Fitbit activities found.")
            return

        click.echo(f"Matched {len(matches)} activity pair(s):")
        for m in matches:
            click.echo(f"  - {m.strava.start_date.strftime('%Y-%m-%d')}: "
                        f"{m.strava.name} <-> {m.fitbit.activity_name} "
                        f"(overlap: {m.overlap_ratio:.0%})")

        if dry_run:
            click.echo(f"\n[DRY RUN] Would update {len(matches)} activities. No changes made.")
            return

        if not yes:
            click.confirm(f"\nProceed with backfilling {len(matches)} activities?", abort=True)

        replaced, pending_count, failed = _process_matches(
            strava, fitbit, matches, use_minimal_fallback=True,
        )

        click.echo(f"\nBackfill complete! {replaced} succeeded, {failed} failed "
                    f"out of {len(matches)} matched.")
        if pending_count:
            click.echo(f"  {pending_count} activity(ies) pending — delete the "
                       f"'[DELETE ME]' activities on Strava, then re-run to upload.")
    finally:
        strava.close()
        fitbit.close()


@cli.command()
def status():
    """Show authentication status and token info."""
    from .auth import load_tokens

    for service in ("strava", "fitbit"):
        tokens = load_tokens(service)
        if tokens is None:
            click.echo(f"{service.capitalize()}: Not authenticated")
        else:
            click.echo(f"{service.capitalize()}: Authenticated")
            if "athlete" in tokens:
                athlete = tokens["athlete"]
                click.echo(f"  User: {athlete.get('firstname', '')} {athlete.get('lastname', '')}")
            if "expires_at" in tokens:
                exp = datetime.fromtimestamp(tokens["expires_at"], tz=timezone.utc)
                if tokens["expires_at"] < time.time():
                    click.echo(f"  Token: Expired (will auto-refresh)")
                else:
                    click.echo(f"  Token: Valid until {exp.strftime('%Y-%m-%d %H:%M UTC')}")
