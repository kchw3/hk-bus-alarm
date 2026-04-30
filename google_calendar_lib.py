"""
Library for creating Google Calendar events via the Google Calendar API v3.

Authentication uses OAuth 2.0 with a credentials file (client_secrets.json)
downloaded from Google Cloud Console and a local token cache (token.json).

Setup (one-time):
  1. Go to https://console.cloud.google.com/
  2. Create a project, enable the Google Calendar API.
  3. Create OAuth 2.0 credentials (Desktop app), download as credentials.json.
  4. On first run, the script prints an authorisation URL to the terminal.
     Visit it in any browser, approve access. The browser will then try to
     redirect to http://localhost:8080/ and fail — that is expected. Copy
     the full URL from the address bar and paste it into the terminal prompt.
     The granted token is saved to token_file for all subsequent runs.

Requires:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""

import json
import os
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Read-write access to a user's calendar events (does not allow calendar
# list/settings changes; use 'calendar' scope if those are also needed).
_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _run_console_flow(credentials_file: str):
    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, _SCOPES)
    flow.redirect_uri = "http://localhost:8080/"
    auth_url, _ = flow.authorization_url(prompt="consent")
    print(f"\nOpen this URL in a browser on any device to authorise:\n\n  {auth_url}\n")
    print("After approving, your browser will be redirected to http://localhost:8080/")
    print("The page will fail to load — that is expected.")
    print("Copy the full URL from the browser address bar and paste it below.\n")
    redirect_url = input("Paste the full redirect URL: ").strip()
    flow.fetch_token(authorization_response=redirect_url)
    return flow.credentials


def get_calendar_service(
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
):
    """
    Authenticate with Google and return a Calendar API service object.

    credentials_file : path to the OAuth 2.0 client-secrets JSON downloaded
                       from Google Cloud Console.
    token_file       : path where the granted OAuth token is cached between
                       runs. Created automatically on first authorisation.

    On the very first call an authorisation URL is printed to the terminal.
    Visit it in any browser and approve access. The browser will then try to
    redirect to http://localhost:8080/ and fail — that is expected. Copy the
    full URL from the address bar and paste it into the terminal prompt. All
    subsequent calls load the cached token (refreshing it silently when it
    expires).
    """
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = _run_console_flow(credentials_file)
        else:
            creds = _run_console_flow(credentials_file)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------

def create_calendar_event(
    service,
    calendar_id: str,
    summary: str,
    start_dt: datetime,
    *,
    end_dt: datetime | None = None,
    duration_minutes: int = 30,
    description: str = "",
    location: str = "",
) -> dict:
    """
    Create a Google Calendar event and return the created event resource dict.

    service          : Calendar API service object from get_calendar_service().
    calendar_id      : ID of the target calendar. Use 'primary' for the user's
                       default calendar, or the calendar's full email-style ID
                       (e.g. 'abc123@group.calendar.google.com') for any other.
    summary          : Event title shown in Google Calendar.
    start_dt         : Event start as a timezone-aware datetime. If naive, UTC
                       is assumed.
    end_dt           : Event end datetime. If omitted, defaults to
                       start_dt + duration_minutes.
    duration_minutes : Duration used when end_dt is not supplied (default 30).
    description      : Optional body text for the event.
    location         : Optional location string for the event.

    Raises googleapiclient.errors.HttpError on API failures.
    """
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    if end_dt is None:
        end_dt = start_dt + timedelta(minutes=duration_minutes)
    elif end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    event_body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat()},
        "end":   {"dateTime": end_dt.isoformat()},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    created = (
        service.events()
        .insert(calendarId=calendar_id, body=event_body)
        .execute()
    )
    return created


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def add_bus_schedule_event(
    schedule_dt: datetime,
    summary: str = "Bus schedule",
    *,
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
    calendar_id: str = "primary",
    duration_minutes: int = 30,
    description: str = "",
    location: str = "",
) -> dict:
    """
    Authenticate, then create a calendar event timed to schedule_dt.

    Returns the created event resource dict (contains 'id', 'htmlLink', etc.).

    schedule_dt      : timezone-aware datetime of the bus arrival (the event
                       start time).
    summary          : Event title (default: 'Bus schedule').
    credentials_file : Path to OAuth 2.0 client-secrets JSON.
    token_file       : Path to the cached OAuth token.
    calendar_id      : Target calendar ID ('primary' or a specific calendar's
                       email-style ID).
    duration_minutes : Event duration in minutes (default: 30).
    description      : Optional event body text.
    location         : Optional event location string.
    """
    service = get_calendar_service(credentials_file, token_file)
    event = create_calendar_event(
        service,
        calendar_id=calendar_id,
        summary=summary,
        start_dt=schedule_dt,
        duration_minutes=duration_minutes,
        description=description,
        location=location,
    )
    print(f"Calendar event created: {event.get('htmlLink')}")
    return event
