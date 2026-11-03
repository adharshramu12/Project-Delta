# Contributing to Delta

Delta is created and maintained by **Adharsh Kumar Bachu**. Contributions are welcome under the
Apache 2.0 license; by sending a pull request you agree your work is licensed the same way.
Forks and derivatives must keep the [NOTICE](NOTICE) file and its credit.

## Before you open a pull request
These are the same checks CI runs:

```powershell
pip install uv
uv sync --extra all
uv run pytest
uv run ruff check src tests
uv run mypy
```

## Adding a tool
1. Subclass `delta.tools.base.Tool` with a pydantic `Args` model.
2. Declare `risk` honestly; override `risk_for`, `paths`, `domains`, `app` so the policy engine can see what the call touches.
3. Return `ToolResult(ok, output, evidence)` — put checkable facts in `evidence` for the verifier.
4. Register it in `bootstrap.build` and add a test.

Tools never bypass `ToolExecutor`. PRs that weaken the hard safety floor or the Remote Guard will not be merged.

## Keep Delta light and cheap
- No new background loops that wake more than every 15 s; no GPU use unless a local model needs it.
- Every token in a prompt that rides on each call must earn its place (the planner prompt has a size test).
- Optional features must import lazily, and their tests must skip cleanly without the extra installed.

## Style
Small modules, typed, docstring explaining *why* at the top of each file. Tests use fakes from `tests/fakes.py` — no network or GPU in CI.
