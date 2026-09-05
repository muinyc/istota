"""Calendar operations via CalDAV.

Also provides a CLI for calendar operations from Claude Code:
    python -m istota.skills.calendar list [--calendar URL] [--date today|tomorrow|DATE | --week] [--tz TZ]
    python -m istota.skills.calendar create --calendar URL --summary TEXT --start DATETIME [--end DATETIME]
    python -m istota.skills.calendar update --calendar URL --uid UID [--summary TEXT] [--start DT] [--end DT] [--all-day] ...
    python -m istota.skills.calendar delete --calendar URL --uid UID
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from istota.skills._cli import run_skill_cli

try:
    import caldav
except ImportError:
    caldav = None

try:
    from icalendar import Calendar, Event
except ImportError:
    Calendar = None
    Event = None


@dataclass
class CalendarEvent:
    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str | None = None
    description: str | None = None
    all_day: bool = False
    timezone: str | None = None  # Original TZID, None = floating


def _require_caldav():
    if caldav is None:
        raise ImportError("caldav not installed. Install with: uv sync --extra calendar")


def get_caldav_client(url: str, username: str, password: str):
    """Create a CalDAV client."""
    _require_caldav()
    return caldav.DAVClient(url=url, username=username, password=password)


def list_calendars(client: caldav.DAVClient) -> list[tuple[str, str]]:
    """List available calendars. Returns list of (name, url) tuples."""
    principal = client.principal()
    calendars = principal.calendars()
    return [(cal.name, str(cal.url)) for cal in calendars]


def get_calendar_owner(calendar: caldav.Calendar) -> str | None:
    """Get the owner username from a calendar's DAV properties."""
    try:
        props = calendar.get_properties([caldav.dav.Owner()])
        owner_path = props.get("{DAV:}owner", "")
        # Parse username from path like /remote.php/dav/principals/users/alice/
        if "/principals/users/" in owner_path:
            parts = owner_path.rstrip("/").split("/")
            return parts[-1] if parts else None
    except Exception:
        pass
    return None


def get_calendars_for_user(
    client: caldav.DAVClient,
    username: str,
) -> list[tuple[str, str, bool]]:
    """
    Get calendars owned by a specific user.

    Returns list of (name, url, writable) tuples for calendars where the
    DAV owner matches the given username.
    """
    principal = client.principal()
    calendars = principal.calendars()

    result = []
    for cal in calendars:
        owner = get_calendar_owner(cal)
        if owner == username:
            # Check if writable by looking at current-user-privilege-set
            # For now, assume shared calendars may be read-only
            # We'll determine this by trying to check ACL or just mark as unknown
            writable = True  # Default assumption; will fail gracefully on write
            result.append((cal.name, str(cal.url), writable))

    return result


def get_events(
    client: caldav.DAVClient,
    calendar_url: str,
    start: datetime,
    end: datetime,
) -> list[CalendarEvent]:
    """Get events from a calendar within a date range.

    When the query boundaries are timezone-aware, all-day events are
    post-filtered against the local date range to prevent UTC offset leakage
    (e.g. a Sunday all-day event appearing in a Saturday query because the
    UTC end boundary extends into the next day).
    """
    calendar = client.calendar(url=calendar_url)
    events = calendar.search(start=start, end=end, expand=True)

    # Derive local date boundaries for all-day event filtering.
    # All-day events use DATE (no time component), so we compare dates.
    # The query range [start, end) in local time defines which dates are valid.
    # For a single-day query like "tomorrow" (Feb 14 00:00 → Feb 15 00:00),
    # the valid all-day date range is [Feb 14, Feb 15) — i.e. only Feb 14.
    local_start_date = start.date() if isinstance(start, datetime) else start
    local_end_date = end.date() if isinstance(end, datetime) else end

    result = []
    for event in events:
        ical = Calendar.from_ical(event.data)
        for component in ical.walk():
            if component.name == "VEVENT":
                dtstart = component.get("dtstart")
                dtend = component.get("dtend")

                # Handle all-day events (date vs datetime)
                start_dt = dtstart.dt if dtstart else start
                end_dt = dtend.dt if dtend else start_dt

                all_day = not isinstance(start_dt, datetime)

                # Extract original timezone from DTSTART
                event_tz = None
                if not all_day and dtstart:
                    if hasattr(start_dt, "tzinfo") and start_dt.tzinfo is not None:
                        # Try TZID parameter first, then tzinfo name
                        tzid = dtstart.params.get("TZID") if hasattr(dtstart, "params") else None
                        if tzid:
                            event_tz = str(tzid)
                        elif hasattr(start_dt.tzinfo, "key"):
                            event_tz = start_dt.tzinfo.key
                        elif str(start_dt.tzinfo) == "UTC" or str(start_dt.tzinfo) == "utc":
                            event_tz = "UTC"
                        else:
                            event_tz = str(start_dt.tzinfo)

                if all_day:
                    # Filter out all-day events that leaked in due to
                    # UTC offset extending the CalDAV query window.
                    # An all-day event spans [DTSTART, DTEND) as dates.
                    # It overlaps our range if event_start < local_end_date
                    # AND event_end > local_start_date.
                    event_start_date = start_dt if isinstance(start_dt, date) else start_dt.date()
                    event_end_date = end_dt if isinstance(end_dt, date) else end_dt.date()
                    if event_start_date >= local_end_date or event_end_date <= local_start_date:
                        continue

                    start_dt = datetime.combine(start_dt, datetime.min.time())
                    end_dt = datetime.combine(end_dt, datetime.min.time())
                else:
                    # Strip tzinfo for consistent sorting with all-day events
                    if start_dt.tzinfo is not None:
                        start_dt = start_dt.replace(tzinfo=None)
                    if end_dt.tzinfo is not None:
                        end_dt = end_dt.replace(tzinfo=None)

                result.append(
                    CalendarEvent(
                        uid=str(component.get("uid", "")),
                        summary=str(component.get("summary", "Untitled")),
                        start=start_dt,
                        end=end_dt,
                        location=str(component.get("location", "")) or None,
                        description=str(component.get("description", "")) or None,
                        all_day=all_day,
                        timezone=event_tz,
                    )
                )

    # Sort by start time
    result.sort(key=lambda e: e.start)
    return result


def _local_midnight(tz: str | None = None) -> datetime:
    """Get midnight today in the given timezone, as a timezone-aware datetime."""
    if tz:
        zone = ZoneInfo(tz)
        now = datetime.now(zone)
        return datetime(now.year, now.month, now.day, tzinfo=zone)
    else:
        now = datetime.now()
        return datetime(now.year, now.month, now.day)


def get_today_events(
    client: caldav.DAVClient, calendar_url: str, tz: str | None = None
) -> list[CalendarEvent]:
    """Get today's events. Pass tz (e.g., 'America/Los_Angeles') for correct local date."""
    today = _local_midnight(tz)
    tomorrow = today + timedelta(days=1)
    return get_events(client, calendar_url, today, tomorrow)


def get_tomorrow_events(
    client: caldav.DAVClient, calendar_url: str, tz: str | None = None
) -> list[CalendarEvent]:
    """Get tomorrow's events. Pass tz (e.g., 'America/Los_Angeles') for correct local date."""
    tomorrow = _local_midnight(tz) + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    return get_events(client, calendar_url, tomorrow, day_after)


def get_week_events(
    client: caldav.DAVClient, calendar_url: str, tz: str | None = None
) -> list[CalendarEvent]:
    """Get this week's events. Pass tz (e.g., 'America/Los_Angeles') for correct local date."""
    today = _local_midnight(tz)
    week_end = today + timedelta(days=7)
    return get_events(client, calendar_url, today, week_end)


def create_event(
    client: caldav.DAVClient,
    calendar_url: str,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    location: str | None = None,
    description: str | None = None,
) -> str:
    """Create a new calendar event. Returns the event UID."""
    import uuid

    calendar = client.calendar(url=calendar_url)

    event = Event()
    event.add("uid", str(uuid.uuid4()))
    event.add("summary", summary)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("dtstamp", datetime.now())

    if location:
        event.add("location", location)
    if description:
        event.add("description", description)

    cal = Calendar()
    cal.add("prodid", "-//Istota Bot//istota//EN")
    cal.add("version", "2.0")
    cal.add_component(event)

    calendar.save_event(cal.to_ical().decode("utf-8"))
    return str(event["uid"])


def delete_event(client: caldav.DAVClient, calendar_url: str, uid: str) -> bool:
    """Delete an event by UID. Returns True if deleted."""
    calendar = client.calendar(url=calendar_url)

    try:
        event = calendar.event_by_uid(uid)
        event.delete()
        return True
    except caldav.error.NotFoundError:
        return False


def get_event_by_uid(
    client: caldav.DAVClient,
    calendar_url: str,
    uid: str,
) -> CalendarEvent | None:
    """Get a single event by UID. Returns None if not found."""
    calendar = client.calendar(url=calendar_url)

    try:
        event = calendar.event_by_uid(uid)
        ical = Calendar.from_ical(event.data)

        for component in ical.walk():
            if component.name == "VEVENT":
                dtstart = component.get("dtstart")
                dtend = component.get("dtend")

                start_dt = dtstart.dt if dtstart else datetime.now()
                end_dt = dtend.dt if dtend else start_dt

                all_day = not isinstance(start_dt, datetime)

                # Extract original timezone
                event_tz = None
                if not all_day and dtstart:
                    if hasattr(start_dt, "tzinfo") and start_dt.tzinfo is not None:
                        tzid = dtstart.params.get("TZID") if hasattr(dtstart, "params") else None
                        if tzid:
                            event_tz = str(tzid)
                        elif hasattr(start_dt.tzinfo, "key"):
                            event_tz = start_dt.tzinfo.key
                        elif str(start_dt.tzinfo) in ("UTC", "utc"):
                            event_tz = "UTC"
                        else:
                            event_tz = str(start_dt.tzinfo)

                if all_day:
                    start_dt = datetime.combine(start_dt, datetime.min.time())
                    end_dt = datetime.combine(end_dt, datetime.min.time())

                return CalendarEvent(
                    uid=str(component.get("uid", "")),
                    summary=str(component.get("summary", "Untitled")),
                    start=start_dt,
                    end=end_dt,
                    location=str(component.get("location", "")) or None,
                    description=str(component.get("description", "")) or None,
                    all_day=all_day,
                    timezone=event_tz,
                )
        return None
    except caldav.error.NotFoundError:
        return None


def update_event(
    client: caldav.DAVClient,
    calendar_url: str,
    uid: str,
    summary: str | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    location: str | None = None,
    description: str | None = None,
) -> bool:
    """Update an existing event. Only provided fields are changed.

    Returns False if event not found.
    Raises caldav.error.AuthorizationError if calendar is read-only.
    """
    calendar = client.calendar(url=calendar_url)

    try:
        event = calendar.event_by_uid(uid)
    except caldav.error.NotFoundError:
        return False

    ical = Calendar.from_ical(event.data)

    for component in ical.walk():
        if component.name == "VEVENT":
            if summary is not None:
                component["summary"] = summary
            if start is not None:
                component.pop("dtstart", None)
                component.add("dtstart", start)
            if end is not None:
                component.pop("dtend", None)
                component.add("dtend", end)
            if location is not None:
                if location:
                    component["location"] = location
                elif "location" in component:
                    del component["location"]
            if description is not None:
                if description:
                    component["description"] = description
                elif "description" in component:
                    del component["description"]
            break

    event.data = ical.to_ical().decode("utf-8")
    event.save()
    return True


def format_event_for_display(event: CalendarEvent) -> str:
    """Format an event for human-readable display."""
    if event.all_day:
        time_str = "All day"
    else:
        time_str = f"{event.start.strftime('%H:%M')} - {event.end.strftime('%H:%M')}"

    parts = [f"{time_str}: {event.summary}"]
    if event.location:
        parts.append(f"  Location: {event.location}")

    return "\n".join(parts)


def format_day_schedule(events: list[CalendarEvent], date_label: str = "Today") -> str:
    """Format a day's events for display."""
    if not events:
        return f"{date_label}: No events scheduled"

    lines = [f"{date_label}'s schedule:"]
    for event in events:
        lines.append(f"  • {format_event_for_display(event)}")

    return "\n".join(lines)


# =============================================================================
# CLI Interface
# =============================================================================


def _get_client_from_env() -> caldav.DAVClient:
    """Create CalDAV client from environment variables."""
    url = os.environ.get("CALDAV_URL")
    username = os.environ.get("CALDAV_USERNAME")
    password = os.environ.get("CALDAV_PASSWORD")

    if not all([url, username, password]):
        raise ValueError(
            "Missing CalDAV credentials. Required env vars: "
            "CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD"
        )

    return get_caldav_client(url, username, password)


def _event_to_dict(event: CalendarEvent) -> dict:
    """Convert CalendarEvent to JSON-serializable dict."""
    d = {
        "uid": event.uid,
        "summary": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "location": event.location,
        "description": event.description,
        "all_day": event.all_day,
    }
    if event.timezone:
        d["timezone"] = event.timezone
    else:
        d["timezone"] = "floating"
    return d


def _parse_date(date_str: str, tz: str | None = None) -> datetime:
    """Parse a date string into a datetime at midnight, timezone-aware if tz provided."""
    zone = ZoneInfo(tz) if tz else None

    if date_str.lower() == "today":
        if zone:
            now = datetime.now(zone)
            return datetime(now.year, now.month, now.day, tzinfo=zone)
        else:
            now = datetime.now()
            return datetime(now.year, now.month, now.day)
    elif date_str.lower() == "tomorrow":
        if zone:
            now = datetime.now(zone)
            tomorrow = now + timedelta(days=1)
            return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone)
        else:
            now = datetime.now()
            tomorrow = now + timedelta(days=1)
            return datetime(tomorrow.year, tomorrow.month, tomorrow.day)
    else:
        # Parse YYYY-MM-DD format
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if zone:
            return dt.replace(tzinfo=zone)
        return dt


def _parse_datetime(dt_str: str) -> datetime:
    """Parse datetime string in various formats."""
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {dt_str}. Use format: YYYY-MM-DD HH:MM")


def _parse_all_day_date(date_str: str) -> date:
    """Parse the date value used by an all-day event."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse all-day date: {date_str}. Use format: YYYY-MM-DD"
        ) from exc


def _get_date_range(args) -> tuple[datetime, datetime, str]:
    """Compute (start, end, label) from --week or --date flags."""
    if getattr(args, "week", False):
        start = _parse_date("today", args.tz)
        end = start + timedelta(days=7)
        return start, end, "week"
    start = _parse_date(args.date, args.tz)
    end = start + timedelta(days=1)
    return start, end, args.date


def cmd_list(args) -> dict:
    """List calendar events."""
    start, end, label = _get_date_range(args)

    # ISSUE-101: DAVClient owns urllib3 pools whose watchdog threads
    # leak unless close() is called.
    with _get_client_from_env() as client:
        # If no calendar specified, list from all user calendars
        if not args.calendar:
            all_events = []
            calendars = list_calendars(client)
            for name, url in calendars:
                try:
                    events = get_events(client, url, start, end)
                    for e in events:
                        all_events.append({
                            "calendar": name,
                            **_event_to_dict(e),
                        })
                except Exception:
                    continue  # Skip calendars we can't read

            all_events.sort(key=lambda e: e["start"])
            return {
                "status": "ok",
                "date": label,
                "event_count": len(all_events),
                "events": all_events,
            }

        # Single calendar specified
        events = get_events(client, args.calendar, start, end)

        return {
            "status": "ok",
            "calendar": args.calendar,
            "date": label,
            "event_count": len(events),
            "events": [_event_to_dict(e) for e in events],
        }


def cmd_create(args) -> dict:
    """Create a calendar event."""
    if args.all_day:
        start = _parse_all_day_date(args.start)
        inclusive_end = _parse_all_day_date(args.end) if args.end else start
        if inclusive_end < start:
            raise ValueError("All-day end date cannot be before start date")
        end = inclusive_end + timedelta(days=1)
    else:
        if args.end is None:
            raise ValueError("Timed events require --end")
        start = _parse_datetime(args.start)
        end = _parse_datetime(args.end)

    if args.tz and not args.all_day:
        tz = ZoneInfo(args.tz)
        start = start.replace(tzinfo=tz)
        end = end.replace(tzinfo=tz)

    # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
    with _get_client_from_env() as client:
        uid = create_event(
            client,
            args.calendar,
            summary=args.summary,
            start=start,
            end=end,
            location=args.location,
            description=args.description,
        )

    return {
        "status": "ok",
        "uid": uid,
        "summary": args.summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": args.all_day,
    }


def cmd_delete(args) -> dict:
    """Delete a calendar event."""
    # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
    with _get_client_from_env() as client:
        deleted = delete_event(client, args.calendar, args.uid)

    if deleted:
        return {"status": "ok", "uid": args.uid, "deleted": True}
    else:
        return {"status": "error", "error": f"Event not found: {args.uid}"}


def cmd_update(args) -> dict:
    """Update a calendar event."""
    kwargs: dict = {}
    if args.summary is not None:
        kwargs["summary"] = args.summary

    if args.all_day and (args.start is not None or args.end is not None):
        if args.start is None or args.end is None:
            raise ValueError("An all-day update requires both --start and --end")
        start = _parse_all_day_date(args.start)
        inclusive_end = _parse_all_day_date(args.end)
        if inclusive_end < start:
            raise ValueError("All-day end date cannot be before start date")
        kwargs["start"] = start
        kwargs["end"] = inclusive_end + timedelta(days=1)
    elif args.start is not None:
        dt = _parse_datetime(args.start)
        if args.tz:
            dt = dt.replace(tzinfo=ZoneInfo(args.tz))
        kwargs["start"] = dt
    if not args.all_day and args.end is not None:
        dt = _parse_datetime(args.end)
        if args.tz:
            dt = dt.replace(tzinfo=ZoneInfo(args.tz))
        kwargs["end"] = dt

    # --clear-location passes "" to delete the property; --location sets it
    if args.clear_location:
        kwargs["location"] = ""
    elif args.location is not None:
        kwargs["location"] = args.location

    if args.clear_description:
        kwargs["description"] = ""
    elif args.description is not None:
        kwargs["description"] = args.description

    if not kwargs:
        return {"status": "error", "error": "No fields to update. Provide at least one of --summary, --start, --end, --location, --description, --clear-location, --clear-description."}

    # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
    with _get_client_from_env() as client:
        updated = update_event(client, args.calendar, args.uid, **kwargs)

    if updated:
        return {"status": "ok", "uid": args.uid, "updated_fields": list(kwargs.keys())}
    else:
        return {"status": "error", "error": f"Event not found: {args.uid}"}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.calendar",
        description="Calendar operations CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", aliases=["agenda"], help="List calendar events")
    p_list.add_argument("--calendar", "-c", help="Calendar URL (omit to query all)")
    date_group = p_list.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date", "-d", default="today",
        help="Date to query: 'today', 'tomorrow', or YYYY-MM-DD (default: today)"
    )
    date_group.add_argument(
        "--week", "-w", action="store_true", default=False,
        help="Show events for the next 7 days"
    )
    p_list.add_argument("--tz", help="Timezone (e.g., America/Los_Angeles)")

    # create
    p_create = sub.add_parser("create", help="Create a calendar event")
    p_create.add_argument("--calendar", "-c", required=True, help="Calendar URL")
    p_create.add_argument("--summary", "-s", required=True, help="Event title")
    p_create.add_argument(
        "--start", required=True,
        help="Start time, or date with --all-day"
    )
    p_create.add_argument(
        "--end",
        help="End time, or inclusive end date with --all-day (defaults to start date)"
    )
    p_create.add_argument("--all-day", action="store_true", help="Create an all-day event using YYYY-MM-DD dates")
    p_create.add_argument("--location", "-l", help="Event location")
    p_create.add_argument("--description", help="Event description")
    p_create.add_argument("--tz", help="Timezone for start/end times (e.g., America/Los_Angeles)")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a calendar event")
    p_delete.add_argument("--calendar", "-c", required=True, help="Calendar URL")
    p_delete.add_argument("--uid", required=True, help="Event UID to delete")

    # update
    p_update = sub.add_parser("update", help="Update a calendar event")
    p_update.add_argument("--calendar", "-c", required=True, help="Calendar URL")
    p_update.add_argument("--uid", required=True, help="Event UID to update")
    p_update.add_argument("--summary", "-s", help="New event title")
    p_update.add_argument("--start", help="New start time (YYYY-MM-DD HH:MM)")
    p_update.add_argument("--end", help="New end time (YYYY-MM-DD HH:MM)")
    p_update.add_argument("--all-day", action="store_true", help="Treat start/end as YYYY-MM-DD dates; end is inclusive")
    p_update.add_argument("--location", "-l", help="New event location")
    p_update.add_argument("--description", help="New event description")
    p_update.add_argument("--tz", help="Timezone for start/end times (e.g., America/Los_Angeles)")
    p_update.add_argument("--clear-location", action="store_true", default=False, help="Remove location")
    p_update.add_argument("--clear-description", action="store_true", default=False, help="Remove description")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "list": cmd_list,
        "agenda": cmd_list,  # alias — natural name an LLM reaches for
        "create": cmd_create,
        "delete": cmd_delete,
        "update": cmd_update,
    }

    run_skill_cli(commands, args)


if __name__ == "__main__":
    main()
