## What and why

<!-- What this changes, and the problem it solves. Link the issue if there is one (Fixes #123). -->

## How it was tested

<!-- Tests added or changed, and anything you checked by hand on Windows. -->

## Checklist

- [ ] `uv run ruff check src tests`, `uv run mypy` and `uv run pytest -q` pass
- [ ] Safety is not weakened: policy, approvals, Autopilot, Remote Guard, ExfilGuard, captcha refusals and secret denies behave as before (or the change is explained above)
- [ ] No new runtime dependency (or it is explained above)
- [ ] Old settings files and data still load
- [ ] Docs updated (README, SECURITY.md, docs/) and a line added to CHANGELOG.md under Unreleased
- [ ] No API keys, tokens, personal data or real names in code, tests or screenshots
