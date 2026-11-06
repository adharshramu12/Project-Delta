"""Watchers: Delta signs in to a portal with a saved login on a schedule, reads what the user asked for (timetable,
exams, events, notices...), and tells them only what is new or changed. See docs/WATCHERS.md.

    recipe.py   what to read on which page, and the in-page extraction (no AI at run time)
    diff.py     new / changed / removed items between two snapshots
    store.py    watchers.db: settings, DPAPI-sealed snapshots, the digest queue and reminder offers
    service.py  one run (sign in, read, diff, notify), creating and removing watchers, reminders from items
    tools.py    watch_portal, list_watchers, check_watcher, inspect_portal, save_watch_recipe
    cli.py      `delta watch list|run|remove`
"""
