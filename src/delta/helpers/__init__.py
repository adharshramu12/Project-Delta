"""Parallel read-only helpers (opt-in, `helpers.enabled`).

    readonly.py    ReadOnlyExecutor: the tool allowlist, enforced in code (helpers never click, type, write,
                   send, ask the user, or use the user's own browser)
    supervisor.py  HelperSupervisor: runs up to N helpers at once with their own token budget and time limit,
                   merges their answers with provenance, records them as child tasks
    tools.py       parallel_research (for the agent) and helper_read_page (for helpers)
"""
