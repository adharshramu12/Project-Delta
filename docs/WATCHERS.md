# Portal watchers

A watcher checks a site you have a saved login for (see [LOGINS.md](LOGINS.md)) every few hours and tells you only
what is new or changed: a new exam on the ERP, a changed timetable slot, a fresh notice on the LMS.

## Quickstart

1. Save a login for the site (Settings > Logins and watchers, or `delta logins add`).
2. Ask Delta: "watch my college ERP for exams, timetable and notifications". Or in Settings press **Start watching**
   with the login, what to check and how often.
3. The first check runs about a minute later and only says what is now being watched. After that you hear from
   Delta only when something is new.

## How often

- Default every 6 hours. The shortest gap is 1 hour; the longest is a week.
- "Check now" (dashboard, `delta watch run`, or asking Delta) works at most once every 10 minutes per watcher, to go
  easy on the site.
- Watchers run on Delta's scheduler as schedules of kind `watch`, so quiet hours, missed runs after sleep and the run
  history work as for any scheduled task. They run only while Delta is on.

## How one check works

1. Delta opens one tab in its private browser (one heavy task at a time) and closes it afterwards. If the browser was
   not open before, it is closed again.
2. It opens each page in the watcher's recipe. A sign-in form or a "session expired" page means the site signed Delta
   out: it signs in again with the saved login (asking first unless that login is "Always allow"), then reads on.
3. Each page is cut into items with the recipe, **with no AI at all**. Nothing written on the portal can steer Delta
   during a run: an instruction inside a page is just text in an item.
4. The items are compared with the last snapshot (sealed with DPAPI). Only new or changed items are reported, at once
   or in one daily digest (default 18:00).
5. New exams, assignments, deadlines and events are **offered** as reminders. A reminder is made only when you press
   **Add reminder** or ask Delta for it.

If a page no longer looks like the recipe says (the portal was redesigned), the message says "Couldn't read" for that
part; nothing is invented and its last snapshot is kept. A site that is down, or a sign-in that needs you (one-time
code, captcha), ends the run with a plain message. You are told once when a watcher starts failing, not on every run.

## Recipes

A recipe lists the pages to read and how to cut each into items:

```json
{"sections": [
  {"name": "Upcoming exams", "url": "/exams", "rows": "table#exams tbody tr", "key": ["course", "date"]},
  {"name": "Timetable", "url": "/timetable"},
  {"name": "Notices", "url": "/notices", "rows": "ul.notices li"}
]}
```

- `url` is a path on the login's own site; any other origin is refused.
- Without `rows`, Delta picks the best matching table or list on the page.
- `key` names the fields that identify an item, so a changed date shows as "changed", not as a new item.

When you ask Delta to watch a site, the AI looks at the pages once (`inspect_portal`) and saves a recipe
(`save_watch_recipe`), which is checked against the live pages first. Recipes are kept as skills per site, so you can
see and retire them like any skill. Until a recipe is saved, a simple default built from your words is used.

## Commands

| Where | Command |
|---|---|
| Terminal | `delta watch list`, `delta watch run NAME`, `delta watch remove NAME` |
| Dashboard | Settings > Logins and watchers: start, check now, remove, add or dismiss reminder offers |
| Telegram | `/watchers`: what is watched and how the last check went (read-only) |
| The AI | `watch_portal`, `list_watchers`, `check_watcher`, `inspect_portal`, `save_watch_recipe` |

## Privacy

What a watcher read is sealed with DPAPI in `watchers.db` in Delta's data folder. Its settings (which login, what to
read, how often) are plain. Snapshots are bounded (at most 200 items per section) and removed with the watcher.
