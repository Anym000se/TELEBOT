"""Reads and writes your Google Calendar through Google's REST API."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

try:
    from google.auth.exceptions import RefreshError
except ImportError:  # Google packages not installed; nothing can raise it then

    class RefreshError(Exception):
        pass

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
API = "https://www.googleapis.com/calendar/v3"
# Events the bot creates are tagged, so it only ever changes or removes its own.
TAG = "telebot"


class CalendarError(Exception):
    """Google said no. The message says why, in Google's words."""


class SignedOut(CalendarError):
    """The Google sign-in expired or was revoked, so it has to be done again."""


def service_account_email(credentials_path: Path) -> str | None:
    """The robot account's address if the file is a service-account key, else None."""
    try:
        info = json.loads(credentials_path.read_text())
    except (OSError, ValueError):
        return None
    return info.get("client_email") if isinstance(info, dict) and info.get("type") == "service_account" else None


def login(credentials_path: Path, token_path: Path):
    """An authorized session for the Calendar API.

    With a service-account key there's nothing to sign in to: you share your calendar with the
    robot account instead. With a Desktop app client, a browser opens to sign in the first time.
    """
    from google.auth.transport.requests import AuthorizedSession, Request

    if service_account_email(credentials_path):
        from google.oauth2 import service_account

        return AuthorizedSession(service_account.Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES))

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None  # revoked or expired for good: sign in again
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)
        granted = creds.granted_scopes or SCOPES
        if SCOPES[0] not in (granted.split() if isinstance(granted, str) else granted):
            raise CalendarError(
                "Google sign-in didn't include calendar access. Run it again, and on Google's screen tick "
                "the box that lets TELEBOT view and edit events on your calendars."
            )
    token_path.write_text(creds.to_json())
    return AuthorizedSession(creds)


@dataclass
class Event:
    id: str
    title: str
    start: str  # "2026-10-03T19:00", or "2026-10-03" for all-day
    end: str  # same format; for all-day events, the last day ("" if it's one day)
    ours: bool  # created by the bot


def _parse(value: str) -> datetime | date | None:
    try:
        if "T" in value:
            return datetime.fromisoformat(value).replace(tzinfo=None, second=0, microsecond=0)
        return date.fromisoformat(value)
    except ValueError:
        return None


def event_body(title: str, start: str, end: str, location: str, quote: str, tz: ZoneInfo, now: datetime) -> dict:
    """The Calendar API body for an event. Raises ValueError if it can't be put on the calendar."""
    begin, finish = _parse(start), _parse(end)
    if not title.strip():
        raise ValueError("no title")
    if begin is None:
        raise ValueError(f"couldn't read the start time {start!r}")
    if isinstance(begin, datetime):
        if begin.replace(tzinfo=tz) < now:
            raise ValueError(f"{start} is in the past")
        if not isinstance(finish, datetime) or finish <= begin:
            finish = begin + timedelta(hours=1)
        times = {
            "start": {"dateTime": begin.isoformat(), "timeZone": tz.key},
            "end": {"dateTime": finish.isoformat(), "timeZone": tz.key},
        }
    else:
        if begin < now.astimezone(tz).date():
            raise ValueError(f"{start} is in the past")
        last = finish if type(finish) is date and finish >= begin else begin
        # Google's all-day end date is exclusive, so the day after the last day.
        times = {"start": {"date": begin.isoformat()}, "end": {"date": (last + timedelta(days=1)).isoformat()}}
    return {
        "summary": title.strip(),
        "location": location.strip(),
        "description": f'From your chat: "{quote.strip()}"\n\nAdded by TELEBOT',
        **times,
        "extendedProperties": {"private": {TAG: "1"}},
    }


def describe(body: dict) -> str:
    """'Dinner at Luigi's, Sat Oct 3, 7:00pm' for notifications."""
    start = body["start"]
    if "dateTime" in start:
        when = datetime.fromisoformat(start["dateTime"])
        clock = f"{when.hour % 12 or 12}:{when:%M}{'am' if when.hour < 12 else 'pm'}"
        return f"{body['summary']}, {when:%a %b} {when.day}, {clock}"
    day = date.fromisoformat(start["date"])
    last = date.fromisoformat(body["end"]["date"]) - timedelta(days=1)
    span = f"{day:%a %b} {day.day}" + (f" to {last:%a %b} {last.day}" if last > day else "")
    return f"{body['summary']}, {span}"


class GoogleCalendar:
    def __init__(self, session, calendar_id: str, tz: ZoneInfo, robot: str | None = None):
        self.session = session
        self.tz = tz
        self.robot = robot  # the service account's address, if that's how it signs in
        self.url = f"{API}/calendars/{urlquote(calendar_id, safe='')}/events"

    def _call(self, method: str, url: str, **kwargs):
        try:
            response = self.session.request(method, url, timeout=30, **kwargs)
        except RefreshError as e:
            if self.robot:
                raise SignedOut(
                    f"Google stopped accepting the service-account key ({e}). In Google Cloud, make a new "
                    f"key for {self.robot}, save it as google-credentials.json, and restart the bot."
                ) from None
            raise SignedOut(
                "Google signed the bot out of your calendar. While the Google app is in testing mode this "
                "happens every 7 days. Stop the bot (Ctrl+C) and start it again to sign back in."
            ) from None
        if response.status_code >= 400:
            try:
                reason = response.json()["error"]["message"]
            except Exception:
                reason = response.text[:300]
            raise CalendarError(f"{reason} (HTTP {response.status_code})")
        return response.json() if response.content else None

    def upcoming(self, days: int = 60) -> list[Event]:
        now = datetime.now(timezone.utc)
        params = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=days)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "100",
        }
        events = []
        for item in self._call("GET", self.url, params=params).get("items", []):
            start, end = item.get("start", {}), item.get("end", {})
            if "dateTime" in start:
                begin = self._local(start["dateTime"])
                finish = self._local(end["dateTime"]) if "dateTime" in end else ""
            else:
                begin = start.get("date", "")
                last = date.fromisoformat(end["date"]) - timedelta(days=1) if "date" in end else None
                finish = last.isoformat() if last and last.isoformat() != begin else ""
            ours = item.get("extendedProperties", {}).get("private", {}).get(TAG) == "1"
            events.append(Event(item["id"], item.get("summary", "(no title)"), begin, finish, ours))
        return events

    def _local(self, value: str) -> str:
        return datetime.fromisoformat(value).astimezone(self.tz).strftime("%Y-%m-%dT%H:%M")

    def add(self, body: dict) -> str:
        return self._call("POST", self.url, json=body)["id"]

    def update(self, event_id: str, body: dict):
        self._call("PATCH", f"{self.url}/{urlquote(event_id, safe='')}", json=body)

    def cancel(self, event_id: str):
        self._call("DELETE", f"{self.url}/{urlquote(event_id, safe='')}")
