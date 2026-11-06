"""Eval task specs: YAML files in evals/tasks (one file per suite, a list of tasks in each).

    - id: files-create-note            unique, short
      suite: files                     files | documents | browser | apps | memory (default: the file name)
      request: Create notes.txt in {sandbox} saying hello
      needs: [browser]                 optional: browser (a real Chrome/Edge), windows (Windows only),
                                       ocr (Windows OCR with a language installed)
      approve: approve                 what the eval answers when Delta asks: approve (default) | deny
      setup:                           optional, before the task runs
        files: {"in.txt": "text"}      written into the sandbox folder
        memory: ["fact", ...]          added to Delta's memory
      script:                          scripted model replies (the CI mode); see delta.evals.scripted
        plan: [{...planner JSON...}]
        chat: [{call: {tool: write_file, args: {...}}}, {text: "DONE: ..."}]
        verify: [{passed: true, reason: "..."}]
      inject: [{tool, times, output}]  optional: make tool calls fail (delta.selfheal.inject; evals only)
      fake_browser: {moved: true}      optional: the fake browser (delta.evals.fakebrowser) with failure switches
      perception: {fixture: page}      optional: perception tools (delta.perception) on a drawn picture:
                                       fixture page (the fake browser's page) | text (lines in `text`);
                                       other keys are PerceptionConfig settings (ocr, cache, max_marks...)
      helpers: {max_helpers: 2}        optional: parallel read-only helpers on (HelpersConfig settings)
      check:                           all must pass; see delta.evals.checkers
        - file_contains: {path: notes.txt, text: hello}
      timeout_s: 120

Placeholders in request, setup, script and check: {sandbox} (the task's own folder), {site} (the local
fixture web site), {python} (this Python), {marker} (a unique word for this run).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUITES = ("files", "documents", "browser", "apps", "memory", "selfheal", "perception", "helpers")


@dataclass
class EvalTask:
    id: str
    suite: str
    request: str
    check: list[dict[str, Any]]
    script: dict[str, list[Any]] = field(default_factory=dict)
    setup: dict[str, Any] = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)
    approve: str = "approve"
    timeout_s: float = 120
    description: str = ""
    inject: list[dict[str, Any]] = field(default_factory=list)
    fake_browser: dict[str, Any] | None = None
    perception: dict[str, Any] | None = None
    helpers: dict[str, Any] | None = None


class SpecError(ValueError):
    pass


def _task(raw: Any, suite: str, where: str) -> EvalTask:
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: each task must be a mapping")
    for key in ("id", "request", "check"):
        if key not in raw:
            raise SpecError(f"{where}: task without '{key}'")
    check = raw["check"]
    if not isinstance(check, list) or not check or not all(isinstance(c, dict) and len(c) == 1 for c in check):
        raise SpecError(f"{where} {raw['id']}: check must be a list of one-key mappings")
    approve = str(raw.get("approve", "approve"))
    if approve not in ("approve", "deny"):
        raise SpecError(f"{where} {raw['id']}: approve must be approve or deny")
    script = raw.get("script") or {}
    if not isinstance(script, dict):
        raise SpecError(f"{where} {raw['id']}: script must be a mapping of job -> replies")
    inject = raw.get("inject") or []
    if not isinstance(inject, list) or not all(isinstance(r, dict) and "tool" in r for r in inject):
        raise SpecError(f"{where} {raw['id']}: inject must be a list of mappings with a tool")
    fake = raw.get("fake_browser")
    if fake is not None and not isinstance(fake, dict):
        raise SpecError(f"{where} {raw['id']}: fake_browser must be a mapping of switches")
    extra: dict[str, dict[str, Any] | None] = {}
    for key in ("perception", "helpers"):
        value = raw.get(key)
        if value is not None and not isinstance(value, dict):
            raise SpecError(f"{where} {raw['id']}: {key} must be a mapping of settings")
        extra[key] = dict(value) if value is not None else None
    return EvalTask(id=str(raw["id"]), suite=str(raw.get("suite", suite)), request=str(raw["request"]),
                    check=list(check), script={str(k): list(v or []) for k, v in script.items()},
                    setup=dict(raw.get("setup") or {}), needs=[str(n) for n in raw.get("needs") or []],
                    approve=approve, timeout_s=float(raw.get("timeout_s", 120)),
                    description=str(raw.get("description", "")), inject=list(inject),
                    fake_browser=dict(fake) if fake is not None else None, **extra)


def load_specs(folder: Path, suites: list[str] | None = None, ids: list[str] | None = None) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for path in sorted(folder.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            raise SpecError(f"{path.name}: expected a list of tasks")
        for raw in data:
            task = _task(raw, path.stem, path.name)
            if task.id in seen:
                raise SpecError(f"{path.name}: duplicate task id {task.id}")
            seen.add(task.id)
            tasks.append(task)
    if suites:
        tasks = [t for t in tasks if t.suite in suites]
    if ids:
        tasks = [t for t in tasks if t.id in ids]
    return tasks


def fill(value: Any, values: dict[str, str]) -> Any:
    """Put placeholders into strings, lists and mappings (keys too)."""
    if isinstance(value, str):
        for k, v in values.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [fill(v, values) for v in value]
    if isinstance(value, dict):
        return {fill(k, values): fill(v, values) for k, v in value.items()}
    return value
