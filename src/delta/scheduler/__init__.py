"""Scheduled tasks and routines.

A schedule is a saved request ("check my attendance on the ERP") plus a rule for when to run it (once,
daily, weekly, every N minutes or hours, or a simple cron line). Schedules live in SQLite in Delta's data
folder, so they survive restarts. The Scheduler loop runs inside the background service (`delta run`)
and hands each due run to the normal Orchestrator, so every tool call still passes the same policy,
approvals and Remote Guard as a request typed by the user.

Modules:
  rules    when a schedule runs: next-run maths (time zones, DST, month ends), plain-words descriptions
  parse    natural language ("every Monday 10 am ...", "remind me at 6 pm to ...") into a rule
  store    SQLite persistence: schedules and their bounded run history
  service  the loop: due runs, missed runs, no overlap, bounded concurrency, delivery of results
  setup    one call that wires it all into bootstrap
  tools    schedule_task, list_schedules, pause/resume/delete_schedule, run_schedule_now for the agent
  cli      `delta schedule add|list|pause|resume|remove|run`
"""
