# strava-hr-sync

Sync Fitbit heart rate data into Strava treadmill activities.

## The Problem

When you do structured treadmill runs and track them with both Runna and Fitbit,
the heart rate data captured by your Fitbit doesn't make it to Strava. This means
your indoor runs show up on Strava without HR data, which breaks your fitness
score, relative effort, and HR zone distribution.

## The Solution

`strava-hr-sync` pulls your intraday heart rate data from Fitbit's API, merges it
with the existing activity data from Strava, and seamlessly replaces the activity
with an HR-enriched version. Same name, same date, same gear — just now with
heart rate data.

## How It Works

1. **Discover** — Finds Strava treadmill runs missing heart rate data
2. **Match** — Correlates them with Fitbit activity logs by time overlap
3. **Merge** — Pulls 1-second resolution HR from Fitbit's Intraday API, merges
   with Strava's existing activity streams into a TCX file
4. **Replace** — Deletes the old activity, uploads the HR-enriched version, and
   restores all metadata (name, description, gear, etc.)

## Installation

```bash
pip install -e ".[dev]"
```

## Setup

Register OAuth applications with both services:

### Strava
1. Go to https://www.strava.com/settings/api
2. Create an application
3. Set Authorization Callback Domain to `localhost`
4. Note your Client ID and Client Secret

### Fitbit
1. Go to https://dev.fitbit.com/apps/new
2. Create a **Personal** application (gives free access to intraday HR data)
3. Set OAuth 2.0 Application Type to "Personal"
4. Set Callback URL to `http://localhost:8089/callback`
5. Note your Client ID and Client Secret

### Authenticate

```bash
# Set your credentials as environment variables
export STRAVA_CLIENT_ID=your_id
export STRAVA_CLIENT_SECRET=your_secret
export FITBIT_CLIENT_ID=your_id
export FITBIT_CLIENT_SECRET=your_secret

# Authenticate with each service (opens browser)
strava-hr-sync auth strava
strava-hr-sync auth fitbit
```

## Usage

```bash
# Preview what would be synced (dry run)
strava-hr-sync sync --dry-run

# Sync a single recent activity
strava-hr-sync sync

# Backfill all historical treadmill runs missing HR
strava-hr-sync backfill --dry-run   # preview first
strava-hr-sync backfill             # do it

# Auto-confirm without prompts
strava-hr-sync sync --yes
```

## Rate Limits

The tool respects both APIs' rate limits:
- **Strava**: 200 requests per 15 minutes, 2000 per day
- **Fitbit**: 150 requests per hour

## License

MIT
