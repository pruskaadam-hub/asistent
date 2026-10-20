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
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from urllib.parse import unquote, urlparse

from icloud_mcp import calendar as cal_ops
from icloud_mcp import writes as write_ops
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
    """List events from today through at most 31 days ahead."""
    app = _ctx(ctx)
    start, end = _window(app, days_ahead)
    return await _run_calendar(app, cal_ops.list_events, start, end, app.settings.tz)


@mcp.tool()
async def search_events(ctx: Context, text: str, days_ahead: int = 30) -> list[Event]:
    """Find events whose title matches text in a bounded future window."""
    app = _ctx(ctx)
    start, end = _window(app, days_ahead)
    return await _run_calendar(
        app, cal_ops.search_events, text, start, end, app.settings.tz
    )


@mcp.tool()
async def create_event(
    ctx: Context,
    title: str,
    start: str,
    end: str,
    calendar: str,
    location: str = "",
) -> str:
    """Create an event in a named calendar after interactive confirmation."""
    app = _ctx(ctx)
    if not calendar.strip():
        raise ValueError("A verified calendar name is required.")
    if not await _confirmed(
        ctx, app, f"Create iCloud event '{title}' in '{calendar}' from {start} to {end}?"
    ):
        return "Cancelled — event not created."
    return await _run_calendar(
        app, write_ops.create_event, title, start, end, calendar, location
    )


@mcp.tool()
async def update_event(
    ctx: Context,
    uid: str,
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
) -> str:
    """Update one event by UID after interactive confirmation."""
    app = _ctx(ctx)
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
    if not await _confirmed(ctx, app, f"Update iCloud event {uid}: {changes}?"):
        return "Cancelled — event not updated."
    return await _run_calendar(
        app, write_ops.update_event, uid, title, start, end, location
    )


@mcp.tool()
async def delete_event(ctx: Context, uid: str) -> str:
    """Delete one event by UID after interactive confirmation."""
    app = _ctx(ctx)
    if not await _confirmed(ctx, app, f"Permanently delete iCloud event {uid}?"):
        return "Cancelled — event not deleted."
    return await _run_calendar(app, write_ops.delete_event, uid)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
