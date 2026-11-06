"""Self-heal: when a step fails, work out why (taxonomy), try a fixed and safe fix first (playbooks), step
down to another way of doing a UI action when one keeps failing (strategy ladder), and keep a copy of every
file Delta changes so the user can undo it (undo).

Nothing here weakens a safety rule: every retry and every recovery step goes through the same ToolExecutor
gate (policy, approvals, Remote Guard, ExfilGuard, the resume repeat check) as the first try, steps that
change something are never retried unless the failure proves they did not happen, and recovery attempts are
capped per step and per task. See SECURITY.md, "What self-heal may do on its own".
"""
