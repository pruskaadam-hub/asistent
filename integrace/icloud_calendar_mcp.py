# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "icloud-mcp==0.1.1",
#   "mcp<2",
# ]
# ///
"""Calendar-only MCP gateway for M.A.R.V.I.N. and Apple iCloud CalDAV.

Credentials are resolved by icloud-mcp from macOS Keychain or the explicit
ICLOUD_APP_PASSWORD fallback. This wrapper
intentionally exposes no mail or contacts tools. Calendar writes remain gated
by ICLOUD_ENABLE_WRITES=1 and an interactive confirmation for every action.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import unquote, urlparse

import icalendar
from icloud_mcp.clients import CalendarClient
from icloud_mcp.config import Settings
from icloud_mcp.models import Event
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="marvin-icloud")
_CALL_TIMEOUT = 90


async def _run_blocking(fn, *args):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, fn, *args), timeout=_CALL_TIMEOUT
    )


@dataclass
class AppContext:
    settings: Settings


class CalendarInfo(BaseModel):
    name: str
    identifier: str
    url: str


@asynccontextmanager
async def lifespan(_server: FastMCP):
    settings = Settings.from_env()
    yield AppContext(settings=settings)


mcp = FastMCP("marvin-icloud-calendar", lifespan=lifespan)


def _ctx(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _with_calendar(settings: Settings, operation, *args):
    """Use a fresh CalDAV client for each call to avoid stale HTTP sessions."""
    calendar = CalendarClient(settings)
    try:
        return operation(calendar, *args)
    finally:
        calendar.close()


async def _run_calendar(app: AppContext, operation, *args):
    return await _run_blocking(_with_calendar, app.settings, operation, *args)


def _calendar_identifier(raw_url: str) -> str:
    path = unquote(urlparse(raw_url).path).rstrip("/")
    return path.rsplit("/", 1)[-1] if path else raw_url


def _list_calendar_info(calendar: CalendarClient) -> list[CalendarInfo]:
    result: list[CalendarInfo] = []
    for item in calendar.principal().calendars():
        raw_url = str(getattr(item, "url", ""))
        result.append(
            CalendarInfo(
                name=item.get_display_name(),
                identifier=_calendar_identifier(raw_url),
                url=raw_url,
            )
        )
    return result


def _calendar_policy() -> tuple[set[str], set[str]]:
    allowed = {
        value.strip()
        for value in os.getenv("ICLOUD_ALLOWED_CALENDAR_IDS", "").split(",")
        if value.strip()
    }
    if not allowed:
        raise RuntimeError(
            "ICLOUD_ALLOWED_CALENDAR_IDS is empty. Configure explicitly allowed "
            "calendar identifiers before reading events."
        )
    busy_only = {
        value.strip()
        for value in os.getenv("ICLOUD_BUSY_ONLY_CALENDAR_IDS", "").split(",")
        if value.strip()
    }
    unknown = busy_only - allowed
    if unknown:
        raise RuntimeError(
            "Busy-only calendar identifiers must also be present in "
            "ICLOUD_ALLOWED_CALENDAR_IDS."
        )
    return allowed, busy_only


def _write_policy() -> set[str]:
    allowed, busy_only = _calendar_policy()
    writable = {
        value.strip()
        for value in os.getenv("ICLOUD_WRITE_CALENDAR_IDS", "").split(",")
        if value.strip()
    }
    if not writable:
        raise RuntimeError(
            "ICLOUD_WRITE_CALENDAR_IDS is empty. Configure an explicit writable "
            "calendar identifier before enabling writes."
        )
    forbidden = writable - (allowed - busy_only)
    if forbidden:
        raise RuntimeError(
            "Writable calendar identifiers must be allowed and cannot be busy-only."
        )
    return writable


def _calendar_by_id(calendar: CalendarClient, calendar_id: str):
    for item in calendar.principal().calendars():
        raw_url = str(getattr(item, "url", ""))
        if _calendar_identifier(raw_url) == calendar_id:
            return item
    raise RuntimeError(f"iCloud calendar {calendar_id} was not found.")


def _ical_value(value: str):
    return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)


def _create_event_in_calendar(
    calendar: CalendarClient,
    calendar_id: str,
    title: str,
    start: str,
    end: str,
    location: str,
) -> str:
    target = _calendar_by_id(calendar, calendar_id)
    document = icalendar.Calendar()
    document.add("version", "2.0")
    document.add("prodid", "-//M.A.R.V.I.N.//iCloud Calendar//CS")
    event = icalendar.Event()
    event.add("uid", f"{uuid.uuid4()}@marvin")
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("dtstart", _ical_value(start))
    event.add("dtend", _ical_value(end))
    event.add("summary", title)
    if location:
        event.add("location", location)
    document.add_component(event)
    target.save_event(document.to_ical().decode())
    return f"Created “{title}” on {target.get_display_name()}."


def _find_event_in_calendar(calendar: CalendarClient, calendar_id: str, uid: str):
    target = _calendar_by_id(calendar, calendar_id)
    for event in target.events():
        component = event.icalendar_component
        if str(component.get("uid", "")) == uid:
            return event
    raise RuntimeError(f"Event {uid} was not found in calendar {calendar_id}.")


def _update_event_in_calendar(
    calendar: CalendarClient,
    calendar_id: str,
    uid: str,
    title: str,
    start: str,
    end: str,
    location: str,
) -> str:
    event = _find_event_in_calendar(calendar, calendar_id, uid)
    document = icalendar.Calendar.from_ical(event.data)
    component = next((item for item in document.walk() if item.name == "VEVENT"), None)
    if component is None:
        raise RuntimeError(f"Event {uid} has no VEVENT component.")

    def replace(key: str, value) -> None:
        if key in component:
            del component[key]
        component.add(key, value)

    if title:
        replace("summary", title)
    if start:
        replace("dtstart", _ical_value(start))
    if end:
        replace("dtend", _ical_value(end))
    if location:
        replace("location", location)
    replace("dtstamp", datetime.now(timezone.utc))
    event.data = document.to_ical().decode()
    event.save()
    return f"Updated event {uid}."


def _delete_event_in_calendar(
    calendar: CalendarClient, calendar_id: str, uid: str
) -> str:
    event = _find_event_in_calendar(calendar, calendar_id, uid)
    event.delete()
    return f"Deleted event {uid}."


def _sort_key(value, tz):
    if isinstance(value, datetime):
        return value.astimezone(tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def _to_iso(value, tz) -> tuple[str, bool]:
    if isinstance(value, datetime):
        return value.astimezone(tz).isoformat(), False
    if isinstance(value, date):
        return value.isoformat(), True
    return str(value), False


def _list_events_filtered(
    calendar: CalendarClient,
    start: datetime,
    end: datetime,
    tz,
    allowed: set[str],
    busy_only: set[str],
) -> list[Event]:
    events: list[tuple[object, Event]] = []
    for item in calendar.principal().calendars():
        raw_url = str(getattr(item, "url", ""))
        calendar_id = _calendar_identifier(raw_url)
        if calendar_id not in allowed:
            continue
        try:
            found = item.search(start=start, end=end, event=True, expand=True)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read allowed iCloud calendar {calendar_id}: "
                f"{type(exc).__name__}"
            ) from exc
        calendar_name = item.get_display_name()
        for source_event in found:
            component = source_event.icalendar_component
            start_value = component.get("dtstart").dt
            end_field = component.get("dtend")
            start_iso, all_day = _to_iso(start_value, tz)
            end_iso = _to_iso(end_field.dt, tz)[0] if end_field is not None else ""
            is_busy_only = calendar_id in busy_only
            events.append(
                (
                    _sort_key(start_value, tz),
                    Event(
                        uid="" if is_busy_only else str(component.get("uid", "")),
                        summary="Obsazeno" if is_busy_only else str(
                            component.get("summary", "(bez názvu)")
                        ),
                        start=start_iso,
                        end=end_iso,
                        all_day=all_day,
                        calendar=calendar_name,
                    ),
                )
            )
    events.sort(key=lambda pair: pair[0])
    return [event for _, event in events]


def _search_events_filtered(
    calendar: CalendarClient,
    text: str,
    start: datetime,
    end: datetime,
    tz,
    allowed: set[str],
    busy_only: set[str],
) -> list[Event]:
    searchable = allowed - busy_only
    needle = text.casefold()
    return [
        event
        for event in _list_events_filtered(
            calendar, start, end, tz, searchable, set()
        )
        if needle in event.summary.casefold()
    ]


class Confirm(BaseModel):
    confirm: bool = True


def _window(app: AppContext, days_ahead: int) -> tuple[datetime, datetime]:
    days = max(0, min(days_ahead, 31))
    local_today = datetime.now(app.settings.tz).date()
    start = datetime.combine(local_today, time.min, tzinfo=app.settings.tz)
    end = datetime.combine(
        local_today + timedelta(days=days), time.max, tzinfo=app.settings.tz
    )
    return start, end


def _require_writes(app: AppContext) -> None:
    if not app.settings.enable_writes:
        raise ValueError(
            "iCloud calendar writes are disabled. Keep ICLOUD_ENABLE_WRITES=0 "
            "until the read-only connection and allowed calendars are verified."
        )


async def _confirmed(ctx: Context, app: AppContext, message: str) -> bool:
    _require_writes(app)
    result = await ctx.elicit(message=message, schema=Confirm)
    return result.action == "accept"


@mcp.tool()
async def list_calendars(ctx: Context) -> list[CalendarInfo]:
    """List iCloud calendar names, stable CalDAV identifiers, and URLs."""
    app = _ctx(ctx)
    return await _run_calendar(app, _list_calendar_info)


@mcp.tool()
async def list_events(ctx: Context, days_ahead: int = 7) -> list[Event]:
    """List events only from explicitly allowed calendars; redact busy-only titles."""
    app = _ctx(ctx)
    start, end = _window(app, days_ahead)
    allowed, busy_only = _calendar_policy()
    return await _run_calendar(
        app,
        _list_events_filtered,
        start,
        end,
        app.settings.tz,
        allowed,
        busy_only,
    )


@mcp.tool()
async def search_events(ctx: Context, text: str, days_ahead: int = 30) -> list[Event]:
    """Search titles in allowed calendars, excluding every busy-only calendar."""
    app = _ctx(ctx)
    start, end = _window(app, days_ahead)
    allowed, busy_only = _calendar_policy()
    return await _run_calendar(
        app,
        _search_events_filtered,
        text,
        start,
        end,
        app.settings.tz,
        allowed,
        busy_only,
    )


@mcp.tool()
async def create_event(
    ctx: Context,
    title: str,
    start: str,
    end: str,
    calendar_id: str,
    location: str = "",
) -> str:
    """Create an event in an explicitly writable calendar after confirmation."""
    app = _ctx(ctx)
    writable = _write_policy()
    if calendar_id not in writable:
        raise ValueError(f"Calendar {calendar_id} is not configured for writes.")
    if not await _confirmed(
        ctx,
        app,
        f"Create iCloud event '{title}' in calendar {calendar_id} from {start} to {end}?",
    ):
        return "Cancelled — event not created."
    return await _run_calendar(
        app,
        _create_event_in_calendar,
        calendar_id,
        title,
        start,
        end,
        location,
    )


@mcp.tool()
async def update_event(
    ctx: Context,
    calendar_id: str,
    uid: str,
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
) -> str:
    """Update one event in an explicitly writable calendar after confirmation."""
    app = _ctx(ctx)
    writable = _write_policy()
    if calendar_id not in writable:
        raise ValueError(f"Calendar {calendar_id} is not configured for writes.")
    changes = ", ".join(
        f"{key}={value}"
        for key, value in (
            ("title", title),
            ("start", start),
            ("end", end),
            ("location", location),
        )
        if value
    )
    if not changes:
        raise ValueError("At least one changed field is required.")
    if not await _confirmed(
        ctx, app, f"Update iCloud event {uid} in calendar {calendar_id}: {changes}?"
    ):
        return "Cancelled — event not updated."
    return await _run_calendar(
        app,
        _update_event_in_calendar,
        calendar_id,
        uid,
        title,
        start,
        end,
        location,
    )


@mcp.tool()
async def delete_event(ctx: Context, calendar_id: str, uid: str) -> str:
    """Delete one event in an explicitly writable calendar after confirmation."""
    app = _ctx(ctx)
    writable = _write_policy()
    if calendar_id not in writable:
        raise ValueError(f"Calendar {calendar_id} is not configured for writes.")
    if not await _confirmed(
        ctx, app, f"Permanently delete iCloud event {uid} in calendar {calendar_id}?"
    ):
        return "Cancelled — event not deleted."
    return await _run_calendar(
        app, _delete_event_in_calendar, calendar_id, uid
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
