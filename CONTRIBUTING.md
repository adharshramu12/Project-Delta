# Contributing to Delta

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

Tools never bypass `ToolExecutor`. PRs that weaken the hard safety floor will not be merged.

## Style
Small modules, typed, docstring explaining *why* at the top of each file. Tests use fakes from `tests/fakes.py` — no network or GPU in CI.
