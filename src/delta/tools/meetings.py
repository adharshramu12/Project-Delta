"""Meetings add-on: instant and scheduled Google Meet / Zoom / Teams meetings, with the link shared.

Built like play_youtube: the work is done in code in the user's own signed-in browser, and the AI
only decides what to call. An instant Google Meet costs 0 AI tokens (the link is read from the
browser's window title and checked against Meet's code format); scheduling fills Google
Calendar's own "new event" page and presses Save by the button's accessible name.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from delta.tools.base import Risk, Tool, ToolResult

MEET_CODE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b")
NEW_MEETING = {
    "google_meet": "https://meet.new",
    "zoom": "https://zoom.us/start/videomeeting",
    "teams": "https://teams.live.com/v2/?meetnow=true",
}


def window_titles() -> list[str]:
    import win32gui

    titles: list[str] = []
    win32gui.EnumWindows(lambda h, _: titles.append(win32gui.GetWindowText(h))
                         if win32gui.IsWindowVisible(h) else None, None)
    return [t for t in titles if t]


def find_meet_code(titles: list[str], known: set[str] | None = None) -> str | None:
    """A Google Meet tab is titled 'Meet - abc-defg-hij': the code is the meeting link."""
    for t in titles:
        if "meet" in t.lower():
            m = MEET_CODE.search(t)
            if m and m.group(1) not in (known or set()):
                return m.group(1)
    return None


def copy_to_clipboard(text: str) -> bool:
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def gmail_draft_url(to: list[str], subject: str, body: str) -> str:
    q = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": ",".join(to), "su": subject, "body": body})
    return f"https://mail.google.com/mail/?{q}"


def calendar_url(title: str, start: datetime, minutes: int, guests: list[str], details: str, link: str) -> str:
    fmt = "%Y%m%dT%H%M%S"  # no 'Z': Google Calendar reads it in the user's own time zone
    q = {"action": "TEMPLATE", "text": title, "dates": f"{start:{fmt}}/{start + timedelta(minutes=minutes):{fmt}}",
         "details": f"{details}\n\nJoin: {link}".strip(), "location": link}
    if guests:
        q["add"] = ",".join(guests)
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(q)


async def _new_meet_link(timeout_s: float = 25) -> str | None:
    known = {c for t in await asyncio.to_thread(window_titles) for c in MEET_CODE.findall(t)}
    await asyncio.to_thread(os.startfile, NEW_MEETING["google_meet"])
    end = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(1)
        code = find_meet_code(await asyncio.to_thread(window_titles), known)
        if code:
            return f"https://meet.google.com/{code}"
    return None


def close_meet_tab(code: str) -> bool:
    """Close the browser tab of Meet's join screen (its camera preview is live) once the link is
    known. Uses the tab's own Close button by accessibility name: no focus change, no mouse."""
    try:
        import pythoncom
        from pywinauto import Desktop

        pythoncom.CoInitialize()
        for w in Desktop(backend="uia").windows():
            for tab in w.descendants(control_type="TabItem"):
                if code in (tab.element_info.name or ""):
                    for b in tab.descendants(control_type="Button"):
                        if (b.element_info.name or "").lower().startswith("close"):
                            b.invoke()
                            return True
    except Exception:
        return False
    return False


def find_event(title: str, code: str, timeout_s: float = 15) -> str | None:
    """Look in the open Google Calendar page for the saved event (its accessible name holds the
    time, title, organiser and location, e.g. '5pm to 5:30pm, Standup, …, Location: …meet…/abc-defg-hij,
    3 November 2026'). Returns that text, or None if it is not there."""
    import time

    import pythoncom
    from pywinauto import Desktop

    pythoncom.CoInitialize()
    end = time.monotonic() + timeout_s
    want = title.lower()
    while time.monotonic() < end:
        for w in Desktop(backend="uia").windows():
            if "calendar" not in w.window_text().lower():
                continue
            for el in w.descendants():
                name = el.element_info.name or ""
                low = name.lower()
                if want in low and (code in low or "meet.google.com" in low):
                    return name
        time.sleep(1.5)
    return None


def _press(window_hint: str, names: tuple[str, ...], timeout_s: float = 20) -> str | None:
    """Press the first visible button whose accessible name is one of `names`, in a window whose
    title contains `window_hint`. Returns the name pressed, or None."""
    import time

    import pythoncom
    from pywinauto import Desktop

    pythoncom.CoInitialize()
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        for w in Desktop(backend="uia").windows():
            if window_hint not in w.window_text().lower():
                continue
            for el in w.descendants(control_type="Button"):
                name = (el.element_info.name or "").strip()
                if name.lower() in names and el.is_visible():
                    try:
                        el.invoke()
                    except Exception:
                        el.click_input()
                    return name
        time.sleep(1)
    return None


class StartArgs(BaseModel):
    platform: str = Field(default="google_meet", description="google_meet, zoom or teams")
    invite: list[str] = Field(default_factory=list, description="Emails to share the link with (a Gmail "
                              "draft is opened for the user to send)",
                              validation_alias=AliasChoices("invite", "guests", "attendees", "emails", "share_with"))
    title: str = Field(default="Meeting", description="Used in the invite email subject")


class StartMeeting(Tool[StartArgs]):
    name = "start_meeting"
    description = ("Start an instant meeting now (Google Meet, Zoom or Teams) in the user's own browser and "
                   "return the join link, copied to the clipboard. With invite=[emails] it also opens a Gmail "
                   "draft with the link for the user to send. Google Meet needs no further steps.")
    risk = Risk.ACT
    Args = StartArgs

    def domains(self, args: StartArgs) -> list[str]:
        return [NEW_MEETING.get(args.platform, NEW_MEETING["google_meet"])]

    def summary(self, args: StartArgs) -> str:
        who = f" and draft an invite to {', '.join(args.invite)}" if args.invite else ""
        return f"start a {args.platform.replace('_', ' ')} meeting{who}"

    async def run(self, args: StartArgs) -> ToolResult:
        platform = args.platform if args.platform in NEW_MEETING else "google_meet"
        if platform != "google_meet":
            await asyncio.to_thread(os.startfile, NEW_MEETING[platform])
            return ToolResult(False, f"Opened {platform}'s new-meeting page. Its link is not readable "
                                     "automatically: look at the window (read_window / look_at_screen) to copy "
                                     "the invite link.", {"platform": platform})
        link = await _new_meet_link()
        if link is None:
            return ToolResult(False, "Opened meet.new but no meeting code appeared (not signed in to Google, "
                                     "or the page is asking something). Check the browser window.")
        copied = await asyncio.to_thread(copy_to_clipboard, link)
        evidence: dict[str, Any] = {"link": link, "platform": platform, "copied": copied, "verified": True}
        extra = ""
        if args.invite:
            body = f"Join the meeting: {link}"
            await asyncio.to_thread(os.startfile, gmail_draft_url(args.invite, args.title, body))
            evidence["draft_to"] = args.invite
            extra = f" A Gmail draft to {', '.join(args.invite)} is open with the link: press Send to share it."
        return ToolResult(True, f"Google Meet is ready: {link} (copied to clipboard{'' if copied else ' failed'})."
                                f" Its join screen is open in your browser (camera preview on).{extra}", evidence)


class ScheduleArgs(BaseModel):
    title: str
    start: str = Field(description="Local start time, ISO format, e.g. 2026-11-03T17:00")
    # Models name these differently ("attendees", "duration_minutes"): accept the common names, since
    # a silently dropped guest list would schedule the meeting without inviting anyone.
    minutes: int = Field(default=30, ge=5, le=600,
                         validation_alias=AliasChoices("minutes", "duration_minutes", "duration", "length"))
    guests: list[str] = Field(default_factory=list, description="Guest emails: they get the calendar invite",
                              validation_alias=AliasChoices("guests", "attendees", "emails", "invitees",
                                                            "participants"))
    details: str = ""


class ScheduleMeeting(Tool[ScheduleArgs]):
    name = "schedule_meeting"
    description = ("Schedule a Google Meet meeting in the user's Google Calendar: creates a Meet link, fills "
                   "the event (title, time, guests, link) and saves it, so guests get an invite with the link. "
                   "start is local time in ISO format.")
    risk = Risk.EXTERNAL  # guests are emailed: always asks the user first
    Args = ScheduleArgs

    async def _via_api(self, args: ScheduleArgs, start: datetime) -> ToolResult:
        return await _api_schedule(args, start)

    def domains(self, args: ScheduleArgs) -> list[str]:
        return ["https://calendar.google.com", "https://meet.google.com"]

    def summary(self, args: ScheduleArgs) -> str:
        who = f" with {', '.join(args.guests)} (emails them an invite)" if args.guests else " (no guests)"
        return f"schedule '{args.title}' at {args.start} for {args.minutes} min{who}"

    async def run(self, args: ScheduleArgs) -> ToolResult:
        try:
            start = datetime.fromisoformat(args.start)
        except ValueError:
            return ToolResult(False, f"start '{args.start}' is not a date-time like 2026-11-03T17:00")
        if start < datetime.now() - timedelta(minutes=5):
            return ToolResult(False, f"{start:%d %b %Y %H:%M} is in the past: ask the user for the right time.")
        if await asyncio.to_thread(_google_connected):
            return await self._via_api(args, start)
        link = await _new_meet_link()
        if link is None:
            return ToolResult(False, "Could not create a Google Meet link (not signed in to Google?).")
        # Only the link is needed now: close Meet's join screen so its camera preview stops.
        await asyncio.to_thread(close_meet_tab, link.rsplit("/", 1)[-1])
        url = calendar_url(args.title, start, args.minutes, args.guests, args.details, link)
        await asyncio.to_thread(os.startfile, url)
        saved = await asyncio.to_thread(_press, "calendar", ("save",))
        sent = None
        if saved and args.guests:  # Google asks whether to email the guests
            sent = await asyncio.to_thread(_press, "calendar", ("send",), 10)
        copied = await asyncio.to_thread(copy_to_clipboard, link)
        when = f"{start:%a %d %b %Y, %H:%M} ({args.minutes} min)"
        evidence: dict[str, Any] = {"link": link, "start": args.start, "guests": args.guests, "saved": bool(saved),
                                    "invites_sent": bool(sent), "copied": copied}
        if not saved:
            return ToolResult(False, f"Meet link {link} is ready and the calendar event is filled in, but I could "
                                     "not press Save: check the Google Calendar window.", evidence)
        # Pressing Save is not proof. Look for the event in the calendar itself: Calendar shows the
        # event's week after saving; if it is not on screen, open that day and look again.
        code = link.rsplit("/", 1)[-1]
        seen = await asyncio.to_thread(find_event, args.title, code)
        if seen is None:
            await asyncio.to_thread(os.startfile, f"https://calendar.google.com/calendar/r/day/"
                                                  f"{start.year}/{start.month}/{start.day}")
            seen = await asyncio.to_thread(find_event, args.title, code, 20)
        evidence.update(event_seen=seen, verified=bool(seen) and (not args.guests or bool(sent)))
        if seen is None:
            return ToolResult(False, f"I pressed Save for '{args.title}' ({when}) but could not find the event in "
                                     f"Google Calendar afterwards, so it may not be saved. Meet link: {link}.",
                              evidence)
        guests = f" Invites with the link were sent to {', '.join(args.guests)}." if sent else (
            " Guests were NOT emailed yet: check the Google Calendar window." if args.guests else "")
        return ToolResult(bool(sent) or not args.guests,
                          f"Scheduled '{args.title}' on {when}, and checked: it is in your Google Calendar "
                          f"(\"{seen[:140]}\"). Meet link: {link}{' (copied)' if copied else ''}.{guests}", evidence)


# --- edit / reschedule / cancel ----------------------------------------------------------------
# Google Calendar's search lists the event as a button named like "10am to 10:30am, Standup, Owner,
# Location: …, 3 November 2026"; its edit page has fields named Title, Start date, Start time and
# End time. Keys are typed into them like a person would, so Calendar's own logic runs (e.g. it
# keeps the length when the start moves). The result is then checked in Calendar.

def time_label(t: datetime) -> str:
    """Calendar's short time label: 12pm, 5:30pm, 10am."""
    h = t.hour % 12 or 12
    return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'}{'am' if t.hour < 12 else 'pm'}"


def field_time(t: datetime) -> str:
    """What to type into a Calendar time field: 12:00pm, 5:30pm."""
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def field_date(d: datetime) -> str:
    return f"{d.day} {d:%b %Y}"


def long_date(d: datetime) -> str:
    return f"{d.day} {d:%B %Y}"


def _calendar_window() -> Any:
    from pywinauto import Desktop

    wins = [w for w in Desktop(backend="uia").windows() if "calendar" in w.window_text().lower()]
    if not wins:
        raise LookupError("Google Calendar is not open")
    return wins[0]


def _search_events(title: str, wait_s: float = 12) -> list[str]:
    """Open Calendar's search for `title` and return the matching events' names."""
    import time

    import pythoncom

    pythoncom.CoInitialize()
    os.startfile("https://calendar.google.com/calendar/r/search?q=" + urllib.parse.quote(title))
    end, want = time.monotonic() + wait_s, title.lower()
    while time.monotonic() < end:
        time.sleep(1.5)
        try:
            w = _calendar_window()
        except LookupError:
            continue
        names = [e.element_info.name or "" for e in w.descendants(control_type="Button")]
        hits = [n for n in names if want in n.lower() and " to " in n]
        if hits or ("search" in w.window_text().lower() and time.monotonic() > end - wait_s + 6):
            return list(dict.fromkeys(hits))
    return []


def _open_event_and_press(event_name: str, button: str) -> None:
    import time

    w = _calendar_window()
    event = next((e for e in w.descendants(control_type="Button") if (e.element_info.name or "") == event_name), None)
    if event is None:
        raise LookupError("the meeting disappeared from the search results")
    event.invoke()
    time.sleep(1.5)
    w = _calendar_window()
    btns = [e for e in w.descendants(control_type="Button") if (e.element_info.name or "") == button]
    if not btns:
        raise LookupError(f"no '{button}' button on the event (it may belong to someone else)")
    btns[0].invoke()
    time.sleep(3)


def _type_fields(values: dict[str, str]) -> None:
    """Type into named fields of the open edit page (select all, type, Enter)."""
    import time

    from pywinauto.keyboard import send_keys

    w = _calendar_window()
    w.set_focus()
    for name, text in values.items():
        fields = [e for e in w.descendants() if e.element_info.control_type in ("Edit", "ComboBox")
                  and (e.element_info.name or "") == name]
        if not fields:
            raise LookupError(f"no '{name}' field on Calendar's edit page")
        fields[0].set_focus()
        time.sleep(0.2)
        safe = text.replace("{", "{{}").replace("}", "{}}").replace("+", "{+}").replace("^", "{^}") \
            .replace("%", "{%}").replace("~", "{~}").replace("(", "{(}").replace(")", "{)}")
        send_keys("^a" + safe + "{ENTER}", pause=0.02, with_spaces=True)
        time.sleep(0.6)


def _press_named(names: tuple[str, ...], wait_s: float = 6) -> str | None:
    import time

    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        for e in _calendar_window().descendants(control_type="Button"):
            n = (e.element_info.name or "").strip()
            if n.lower() in names and e.is_visible():
                e.invoke()
                return n
        time.sleep(0.5)
    return None


def _google_connected() -> bool:
    from delta.integrations import google  # loaded on use: keeps httpx out of Delta's startup

    return google.connected()


async def _api_schedule(args: ScheduleArgs, start: datetime) -> ToolResult:
    """Google Calendar API: create with a Meet link, then read it back from Google as proof."""
    from delta.integrations import google

    when = f"{start:%a %d %b %Y, %H:%M} ({args.minutes} min)"
    try:
        cal = google.GoogleCalendar()
        made = await cal.create(args.title, start, start + timedelta(minutes=args.minutes), args.guests, args.details)
        check = await cal.get(made.id)
    except google.GoogleError as e:
        return ToolResult(False, f"Google Calendar did not schedule it: {e}")
    ok = check.title == args.title and check.start == start.replace(second=0, microsecond=0)
    copied = await asyncio.to_thread(copy_to_clipboard, check.meet_link) if check.meet_link else False
    guests = f" Google emailed the invite to {', '.join(check.guests)}." if check.guests else ""
    return ToolResult(ok, f"Scheduled '{check.title}' on {when}, confirmed by Google Calendar. Meet link: "
                          f"{check.meet_link or '(none)'}{' (copied)' if copied else ''}.{guests}",
                      {"event_id": check.id, "link": check.meet_link, "guests": check.guests, "via": "api",
                       "verified": ok and bool(check.meet_link)})


class EditArgs(BaseModel):
    title: str = Field(description="Current title of the meeting to change")
    on: str | None = Field(default=None, description="Its current date (ISO), if several share the title")
    new_start: str | None = Field(default=None, description="New local start: 2026-11-04T12:00, or just "
                                  "12:00 to keep the meeting's own day",
                                  validation_alias=AliasChoices("new_start", "start", "new_time", "to"))
    minutes: int | None = Field(default=None, ge=5, le=600, description="New length in minutes",
                                validation_alias=AliasChoices("minutes", "duration_minutes", "duration"))
    new_title: str | None = None
    cancel: bool = Field(default=False, description="Delete the meeting instead")


class EditMeeting(Tool[EditArgs]):
    name = "edit_meeting"
    description = ("Change or cancel an existing Google Calendar meeting: move it (new_start), change its "
                   "length (minutes), rename it (new_title) or cancel=true. Finds it by its title (and 'on' "
                   "date if several match), saves, and checks the change in Calendar. Keeps its Meet link.")
    risk = Risk.WRITE
    Args = EditArgs

    def risk_for(self, args: EditArgs) -> Risk:
        return Risk.DESTRUCTIVE if args.cancel else Risk.WRITE

    def domains(self, args: EditArgs) -> list[str]:
        return ["https://calendar.google.com"]

    def summary(self, args: EditArgs) -> str:
        if args.cancel:
            return f"cancel '{args.title}' in Google Calendar"
        changes = [f"move to {args.new_start}" if args.new_start else "",
                   f"make it {args.minutes} min" if args.minutes else "",
                   f"rename to '{args.new_title}'" if args.new_title else ""]
        return f"edit '{args.title}': " + ", ".join(c for c in changes if c)

    async def run(self, args: EditArgs) -> ToolResult:
        new: datetime | None = None
        time_only = bool(args.new_start and re.fullmatch(r"\d{1,2}:\d{2}", args.new_start.strip()))
        if args.new_start and not time_only:
            try:
                new = datetime.fromisoformat(args.new_start)
            except ValueError:
                return ToolResult(False, f"new_start '{args.new_start}' is not a date-time like 2026-11-03T12:00 "
                                         "or a time like 12:00")
            if new < datetime.now() - timedelta(minutes=5):
                return ToolResult(False, f"{new:%d %b %H:%M} is in the past: ask the user for the right time.")
        if not (args.cancel or args.new_start or args.minutes or args.new_title):
            return ToolResult(False, "Nothing to change: give new_start, minutes, new_title or cancel.")
        if await asyncio.to_thread(_google_connected):
            return await _api_edit(args, new)
        return await asyncio.to_thread(self._run_sync, args, new)

    def _run_sync(self, args: EditArgs, new: datetime | None) -> ToolResult:
        try:
            hits = _search_events(args.title)
            if args.on:
                day = long_date(datetime.fromisoformat(args.on))
                hits = [h for h in hits if day in h]
            if not hits:
                return ToolResult(False, f"No meeting called '{args.title}' found in Google Calendar.")
            if len(hits) > 1:
                listing = "\n".join(f"- {h.split(', Location')[0]}" for h in hits[:5])
                return ToolResult(False, f"Several meetings match '{args.title}':\n{listing}\nAsk the user which one "
                                         "(then pass its date as 'on').", {"matches": hits})
            event = hits[0]
            if args.new_start and new is None:  # a time only: same day as the meeting
                current = _current_start(event)
                if current is None:
                    return ToolResult(False, "Could not read the meeting's current day: give new_start as a full "
                                             "date-time.")
                hh, mm = (int(x) for x in args.new_start.strip().split(":"))
                new = current.replace(hour=hh, minute=mm)
                if new < datetime.now() - timedelta(minutes=5):
                    return ToolResult(False, f"{new:%d %b %H:%M} is in the past: ask the user for the right time.")
            if args.cancel:
                _open_event_and_press(event, "Delete event")
                _press_named(("delete", "send", "don't send"), 4)  # guests: Calendar asks about emails
                left = [h for h in _search_events(args.title) if h == event]
                ok = not left
                return ToolResult(ok, (f"Cancelled '{args.title}' ({event.split(', ')[0]}), and checked: it is gone "
                                       "from Google Calendar." if ok else
                                       f"Pressed delete but '{args.title}' is still in Google Calendar."),
                                  {"cancelled": event, "verified": ok})
            _open_event_and_press(event, "Edit event")
            values: dict[str, str] = {}
            if args.new_title:
                values["Title"] = args.new_title
            if new:
                values["Start date"] = field_date(new)
                values["Start time"] = field_time(new)
            if args.minutes:
                start = new or _current_start(event)
                if start:
                    values["End time"] = field_time(start + timedelta(minutes=args.minutes))
            _type_fields(values)
            if not _press_named(("save",)):
                return ToolResult(False, "Changes are typed in but I could not press Save on Calendar's edit page.")
            _press_named(("send", "send updates"), 4)  # guests: Calendar offers to email the change
            title = args.new_title or args.title
            after = _search_events(title)
            want = [title.lower()]
            if new:
                want += [f"{time_label(new)} to".lower(), long_date(new).lower()]
            begins = new or _current_start(event)
            if args.minutes and begins is not None:
                want.append(f"to {time_label(begins + timedelta(minutes=args.minutes))}")
            seen = next((h for h in after if all(x in h.lower() for x in want)), None)
            if seen is None:
                return ToolResult(False, f"Saved, but the check did not find '{title}' with the new details in "
                                         f"Google Calendar. Now listed: {after[:3]}", {"after": after})
            return ToolResult(True, f"Updated, and checked in Google Calendar: \"{seen.split(', Location')[0]}\". "
                                    "The Meet link is unchanged.", {"event": seen, "verified": True})
        except LookupError as e:
            return ToolResult(False, f"Could not edit the meeting: {e}")
        except Exception as e:  # Calendar changed its page, window closed mid-edit
            return ToolResult(False, f"Editing failed ({type(e).__name__}: {str(e)[:160]})")


async def _api_edit(args: EditArgs, new: datetime | None) -> ToolResult:
    """Google Calendar API: find the event, change or cancel it, then read it back as proof."""
    from delta.integrations import google

    cal = google.GoogleCalendar()
    try:
        hits = await cal.find(args.title, datetime.fromisoformat(args.on) if args.on else None)
        if not hits:
            return ToolResult(False, f"No upcoming meeting called '{args.title}' in Google Calendar.")
        if len(hits) > 1:
            listing = "\n".join(f"- {e.title}: {e.start:%a %d %b %H:%M}" for e in hits[:5])
            return ToolResult(False, f"Several meetings match '{args.title}':\n{listing}\nAsk the user which one "
                                     "(then pass its date as 'on').", {"matches": [e.id for e in hits]})
        event = hits[0]
        if args.cancel:
            await cal.delete(event.id)
            gone = not [e for e in await cal.find(args.title, event.start) if e.id == event.id]
            return ToolResult(gone, f"Cancelled '{event.title}' ({event.start:%a %d %b %H:%M}), confirmed by Google "
                                    "Calendar." if gone else "Google still lists the meeting after cancelling.",
                              {"cancelled": event.id, "via": "api", "verified": gone})
        if args.new_start and new is None:  # a time only: keep the meeting's day
            hh, mm = (int(x) for x in args.new_start.strip().split(":"))
            new = event.start.replace(hour=hh, minute=mm)
            if new < datetime.now() - timedelta(minutes=5):
                return ToolResult(False, f"{new:%d %b %H:%M} is in the past: ask the user for the right time.")
        start = new or event.start
        length = timedelta(minutes=args.minutes) if args.minutes else event.end - event.start
        changed = await cal.update(event.id, title=args.new_title, start=start if new else None,
                                   end=start + length if (new or args.minutes) else None)
        check = await cal.get(changed.id)
    except google.GoogleError as e:
        return ToolResult(False, f"Google Calendar did not make the change: {e}")
    ok = (check.start == start and check.end == start + length
          and (not args.new_title or check.title == args.new_title))
    return ToolResult(ok, f"Updated, confirmed by Google Calendar: '{check.title}' {check.start:%a %d %b %H:%M}–"
                          f"{check.end:%H:%M}. The Meet link is unchanged.",
                      {"event_id": check.id, "via": "api", "verified": ok})


def _current_start(event_name: str) -> datetime | None:
    """'10am to 10:30am, Title, …, 3 November 2026' -> datetime(2026, 11, 3, 10, 0)."""
    m = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm) to .*?, (\d{1,2} \w+ \d{4})\s*$", event_name)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    day = datetime.strptime(m.group(4), "%d %B %Y")
    return day.replace(hour=hour, minute=int(m.group(2) or 0))


def meeting_tools() -> list[Tool]:
    return [StartMeeting(), ScheduleMeeting(), EditMeeting()] if sys.platform == "win32" else []
