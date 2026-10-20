# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "icloud-mcp==0.1.1",
# ]
# ///
"""Synchronize approved iCloud calendars into M.A.R.V.I.N. planovani.json.

Secrets are read from macOS Keychain or environment variables. The script
only reads iCloud and only writes planovani.json in the configured private
GitHub repository. Existing non-iCloud planning data and unknown fields are
preserved.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

import keyring
from icloud_mcp.clients import CalendarClient
from icloud_mcp.config import Settings


SYNC_VERSION = "2026.10.21-v1"
DEFAULT_REPOSITORY = "pruskaadam-hub/asistent-data"
DEFAULT_BRANCH = "main"
PLAN_PATH = "planovani.json"
SOURCES_PATH = "zdroje.json"
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class RepoConfig:
    repository: str
    branch: str
    token: str


def _csv(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}


def _calendar_identifier(raw_url: str) -> str:
    path = unquote(urlparse(raw_url).path).rstrip("/")
    return path.rsplit("/", 1)[-1] if path else raw_url


def _github_token() -> str:
    token = os.getenv("MARVIN_GITHUB_TOKEN", "").strip()
    if token:
        return token
    service = os.getenv("MARVIN_GITHUB_KEYRING_SERVICE", "marvin-github-api")
    account = os.getenv("MARVIN_GITHUB_KEYRING_ACCOUNT", "pruskaadam-hub")
    token = keyring.get_password(service, account) or ""
    if not token:
        raise RuntimeError(
            "GitHub token not found. Store it in macOS Keychain under service "
            f"{service!r}, account {account!r}, or set MARVIN_GITHUB_TOKEN."
        )
    return token


def _repo_config() -> RepoConfig:
    repository = os.getenv("MARVIN_GITHUB_REPOSITORY", DEFAULT_REPOSITORY).strip()
    if repository != DEFAULT_REPOSITORY:
        raise RuntimeError(
            f"This synchronizer is restricted to {DEFAULT_REPOSITORY}; got {repository}."
        )
    return RepoConfig(
        repository=repository,
        branch=os.getenv("MARVIN_GITHUB_BRANCH", DEFAULT_BRANCH).strip(),
        token=_github_token(),
    )


def _github_request(config: RepoConfig, method: str, path: str, body=None):
    url = f"{GITHUB_API}/repos/{config.repository}/contents/{quote(path)}"
    if method == "GET":
        url += f"?ref={quote(config.branch)}"
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "marvin-icloud-calendar-sync",
        },
    )
    with urlopen(request, timeout=45) as response:
        return json.load(response)


def _read_json(config: RepoConfig, path: str) -> tuple[object, str]:
    result = _github_request(config, "GET", path)
    raw = base64.b64decode(result["content"].replace("\n", ""))
    return json.loads(raw.decode("utf-8")), result["sha"]


def _write_json(config: RepoConfig, path: str, value: object, sha: str) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    _github_request(
        config,
        "PUT",
        path,
        {
            "message": "Asistent: synchronizace iCloud kalendáře",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": config.branch,
            "sha": sha,
        },
    )


def _to_iso(value, tz) -> tuple[str, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz).isoformat(), False
    if isinstance(value, date):
        local = datetime.combine(value, time.min, tzinfo=tz)
        return local.isoformat(), True
    raise ValueError(f"Unsupported calendar time {value!r}")


def _source_registry(sources: object) -> tuple[dict[str, dict], set[str]]:
    if not isinstance(sources, list):
        raise RuntimeError("zdroje.json must contain an array.")
    source = next((item for item in sources if item.get("id") == "icloud-cal"), None)
    if not source or source.get("stav") != "Aktivní":
        raise RuntimeError("iCloud source is not Active in zdroje.json.")
    metadata = {
        item["calendar_id"]: item
        for item in source.get("kalendare", [])
        if item.get("calendar_id")
    }
    registered = set(source.get("calendar_ids", []))
    return metadata, registered


def _read_icloud_events(
    settings: Settings,
    allowed: set[str],
    metadata: dict[str, dict],
    start: datetime,
    end: datetime,
) -> tuple[list[dict], list[dict], list[str]]:
    client = CalendarClient(settings)
    events: list[dict] = []
    busy: list[dict] = []
    errors: list[str] = []
    try:
        calendars = client.principal().calendars()
        seen: set[str] = set()
        for calendar in calendars:
            calendar_id = _calendar_identifier(str(getattr(calendar, "url", "")))
            if calendar_id not in allowed:
                continue
            seen.add(calendar_id)
            info = metadata.get(calendar_id, {})
            calendar_name = info.get("nazev") or calendar.get_display_name()
            area = info.get("oblast") or "Osobní"
            try:
                found = calendar.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                errors.append(
                    f"iCloud:{calendar_id}: čtení selhalo ({type(exc).__name__})."
                )
                continue
            for source_event in found:
                component = source_event.icalendar_component
                start_field = component.get("dtstart")
                if start_field is None:
                    continue
                start_iso, all_day = _to_iso(start_field.dt, settings.tz)
                end_field = component.get("dtend")
                if end_field is not None:
                    end_iso, _ = _to_iso(end_field.dt, settings.tz)
                elif all_day:
                    end_iso, _ = _to_iso(start_field.dt + timedelta(days=1), settings.tz)
                else:
                    end_iso = start_iso
                uid = str(component.get("uid", ""))
                title = str(component.get("summary", "(bez názvu)"))
                description = str(component.get("description", ""))
                event_id = f"icloud:{calendar_id}:{uid}:{start_iso}"
                events.append(
                    {
                        "id": event_id,
                        "calendar_id": calendar_id,
                        "source": "icloud-cal",
                        "title": title,
                        "start": start_iso,
                        "end": end_iso,
                        "description": description,
                        "oblast": area,
                        "url": "",
                        "all_day": all_day,
                        "icloud_calendar": calendar_name,
                        "icloud_uid": uid,
                    }
                )
                busy.append(
                    {
                        "start": start_iso,
                        "end": end_iso,
                        "source": f"icloud-cal:{calendar_id}",
                    }
                )
        for missing in sorted(allowed - seen):
            errors.append(f"iCloud:{missing}: povolený kalendář nebyl nalezen.")
    finally:
        client.close()
    events.sort(key=lambda item: (item["start"], item["title"], item["id"]))
    busy.sort(key=lambda item: (item["start"], item["end"], item["source"]))
    return events, busy, errors


def _same_event(a: dict, b: dict) -> bool:
    return (
        str(a.get("title", "")).strip().casefold(),
        a.get("start"),
        a.get("end"),
    ) == (
        str(b.get("title", "")).strip().casefold(),
        b.get("start"),
        b.get("end"),
    )


def merge_plan(
    current: object,
    events: list[dict],
    busy: list[dict],
    errors: list[str],
    start_date: str,
    end_date: str,
    synced_at: str,
) -> dict:
    plan = deepcopy(current) if isinstance(current, dict) else {}
    existing_events = [
        item for item in plan.get("udalosti", []) if item.get("source") != "icloud-cal"
    ]
    # Shared Google subscriptions may contain the same event. Avoid a second card
    # when title and exact interval already match.
    filtered_events = [
        item for item in events if not any(_same_event(item, old) for old in existing_events)
    ]
    existing_busy = [
        item
        for item in plan.get("busy", [])
        if not str(item.get("source", "")).startswith("icloud-cal:")
    ]
    existing_errors = [
        item for item in plan.get("chyby", []) if not str(item).startswith("iCloud:")
    ]
    plan["udalosti"] = existing_events + filtered_events
    plan["busy"] = existing_busy + busy
    plan["chyby"] = existing_errors + errors
    plan["od"] = start_date
    plan["do"] = end_date
    plan["timezone"] = "Europe/Prague"
    plan["icloud_aktualizovano"] = synced_at
    plan["icloud_sync_version"] = SYNC_VERSION
    plan["icloud_stav"] = "částečný" if errors else "úspěšný"
    return plan


def _sync_once() -> str:
    repo = _repo_config()
    settings = Settings.from_env()
    allowed = _csv("ICLOUD_ALLOWED_CALENDAR_IDS")
    if not allowed:
        raise RuntimeError("ICLOUD_ALLOWED_CALENDAR_IDS is empty.")

    sources, _ = _read_json(repo, SOURCES_PATH)
    metadata, registered = _source_registry(sources)
    forbidden = allowed - registered
    if forbidden:
        raise RuntimeError(
            "Allowed iCloud IDs are not registered in zdroje.json: "
            + ", ".join(sorted(forbidden))
        )

    local_today = datetime.now(settings.tz).date()
    through = local_today + timedelta(days=7)
    start = datetime.combine(local_today, time.min, tzinfo=settings.tz)
    end = datetime.combine(through + timedelta(days=1), time.min, tzinfo=settings.tz)
    events, busy, errors = _read_icloud_events(
        settings, allowed, metadata, start, end
    )
    synced_at = datetime.now(settings.tz).isoformat(timespec="seconds")

    for attempt in range(2):
        current, sha = _read_json(repo, PLAN_PATH)
        merged = merge_plan(
            current,
            events,
            busy,
            errors,
            local_today.isoformat(),
            through.isoformat(),
            synced_at,
        )
        try:
            _write_json(repo, PLAN_PATH, merged, sha)
            return (
                f"iCloud sync complete: {len(events)} read, "
                f"{len(merged.get('udalosti', []))} total events, "
                f"{len(errors)} errors."
            )
        except HTTPError as exc:
            if exc.code in (409, 422) and attempt == 0:
                continue
            raise
    raise RuntimeError("GitHub update conflict could not be resolved.")


def main() -> None:
    try:
        print(_sync_once())
    except Exception as exc:
        print(f"iCloud sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
