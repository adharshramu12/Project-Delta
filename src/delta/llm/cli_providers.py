"""Providers that run the user's own signed-in coding CLIs (Claude Code, OpenAI Codex).

Many users already pay for a Claude or ChatGPT plan. Calling `claude -p` / `codex exec` lets Delta
think with those plans instead of needing API credits. The CLI is only used as a text model: its own
tools are switched off (Claude Code) or sandboxed read-only (Codex), and every action still goes
through Delta's JSON tool protocol, so Delta's policy and approvals apply as usual.

The prompt is passed on stdin, never on the command line: Windows limits command lines to ~32k
characters and `.cmd` shims mangle quotes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from delta.config import ProviderConfig
from delta.llm.providers import flatten_messages, inject_tool_prompt, parse_prompt_tools
from delta.llm.schemas import LLMError, LLMResponse, Message

# Replaces Claude Code's own (large, coding-focused) system prompt, which also saves plan quota.
CLI_SYSTEM_PROMPT = ("You are the reasoning engine of a desktop assistant called Delta. You have no tools "
                     "of your own. Follow the instructions in the conversation below exactly and reply "
                     "only in the format it asks for.")

SIGN_IN_HINTS = ("not logged in", "login", "log in", "authenticat", "unauthorized", "401", "api key")
LIMIT_HINTS = ("usage limit", "rate limit", "limit reached", "quota", "too many requests", "429")


def render_transcript(messages: list[Message]) -> str:
    """One text prompt from a chat history: CLIs take a single prompt, not a message list."""
    parts = []
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):  # multimodal parts: keep only the text
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        label = {"system": "INSTRUCTIONS", "user": "USER", "assistant": "ASSISTANT"}.get(m["role"], m["role"].upper())
        parts.append(f"### {label}\n{content}")
    parts.append("### ASSISTANT (your reply)")
    return "\n\n".join(parts)


def take_images(messages: list[Message], folder: Path) -> list[Path]:
    """Save the screenshots in a chat (data: URLs) as files, for CLIs that take image files."""
    files: list[Path] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            url = (part.get("image_url") or {}).get("url", "") if isinstance(part, dict) else ""
            if url.startswith("data:image/") and ";base64," in url:
                head, data = url.split(";base64,", 1)
                f = folder / f"image{len(files)}.{head.removeprefix('data:image/').replace('jpeg', 'jpg')}"
                f.write_bytes(base64.b64decode(data))
                files.append(f)
    return files


def find_cli(names: tuple[str, ...], configured: str | None = None) -> str | None:
    """The CLI's path: configured, on PATH, or in the usual per-user install folders."""
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    for name in names:
        if found := shutil.which(name):
            return found
    home = Path.home()
    # The last folder is where the Codex desktop app keeps its CLI; it shares the app's sign-in.
    extra = [home / ".local" / "bin", home / ".claude" / "local", home / ".codex" / "bin",
             home / ".codex" / "plugins" / ".plugin-appserver"]
    if appdata := os.environ.get("APPDATA"):
        extra.append(Path(appdata) / "npm")
    for folder in extra:
        for name in names:
            for suffix in ((".exe", ".cmd", "") if sys.platform == "win32" else ("",)):
                if (p := folder / f"{name}{suffix}").is_file():
                    return str(p)
    if sys.platform == "win32" and "claude" in names and (local := os.environ.get("LOCALAPPDATA")):
        # The Claude desktop app ships its own Claude Code; use the newest copy when nothing else is installed.
        bundled = sorted(Path(local).glob("Claude-Data/*/claude-code/*/*/claude.exe"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if bundled:
            return str(bundled[0])
    return None


class CLIProvider:
    """Base for CLI-backed providers. Subclasses build the command and read the reply."""

    names: tuple[str, ...] = ()
    label = ""
    preamble = ""  # for CLIs without a system-prompt flag

    def __init__(self, name: str, cfg: ProviderConfig) -> None:
        self.name = name
        self.cfg = cfg
        self.local = False  # the model runs in the vendor's cloud; nothing loads on this PC
        self._path: str | None = None
        self._checked_at = -1e9

    def executable(self) -> str | None:
        """Looked up lazily and re-checked every minute, so installing the CLI needs no restart."""
        now = time.monotonic()
        if self._path is None and now - self._checked_at > 60:
            self._checked_at = now
            self._path = find_cli(self.names, self.cfg.command)
        return self._path

    def available(self) -> bool:
        return self.executable() is not None

    def refresh(self) -> None:
        self._path = None
        self._checked_at = -1e9

    def supports(self, task: str) -> bool:
        # Both CLIs accept screenshots, so they can also be Delta's eyes (task "vision").
        return (task in self.cfg.models or "chat" in self.cfg.models) and self.available()

    def model_for(self, task: str) -> str:
        return self.cfg.models.get(task) or self.cfg.models.get("chat") or ""

    def effort_for(self, task: str) -> str:
        return self.cfg.efforts.get(task) or self.cfg.efforts.get("chat") or ""

    def model_options(self) -> list[dict]:
        """Models the user can pick in Settings: [{"id", "label", "efforts"}]."""
        return []

    def command(self, exe: str, model: str, out_file: Path, effort: str = "",
                images: list[Path] | None = None) -> list[str]:
        raise NotImplementedError

    def stdin(self, prompt: str, images: list[Path]) -> str:
        return prompt

    def reply(self, stdout: str, out_file: Path) -> str:
        raise NotImplementedError

    async def complete(
        self, task: str, messages: list[Message], tools: list[dict] | None, json_mode: bool = False
    ) -> LLMResponse:
        exe = self.executable()
        if exe is None:
            raise LLMError(f"{self.name}: {self.label} is not installed", kind="unreachable")
        model = self.model_for(task)
        msgs = inject_tool_prompt(messages, tools) if tools else list(messages)
        prompt = self.preamble + render_transcript(msgs)
        if json_mode and not tools:
            prompt += "\n\nReply with ONLY a JSON object."
        # An empty scratch folder as working dir: no project files, CLAUDE.md or AGENTS.md leak in.
        with tempfile.TemporaryDirectory(prefix="delta-cli-") as work:
            out_file = Path(work) / "reply.txt"
            images = take_images(msgs, Path(work))
            cmd = self.command(exe, model, out_file, self.effort_for(task), images)
            stdout, stderr, code = await self._run(cmd, self.stdin(prompt, images), work)
            if code != 0:
                raise self._error(stderr or stdout, code)
            content = self.reply(stdout, out_file)
            usage = self.usage(stdout, stderr)
        if tools:  # json_mode alone: the caller parses its own JSON (e.g. the planner)
            resp = parse_prompt_tools(content, self.name, model or "default")
            resp.usage = usage
            return resp
        return LLMResponse(content=content, provider=self.name, model=model or "default", usage=usage)

    def usage(self, stdout: str, stderr: str) -> dict[str, int]:
        """Token counts the CLI printed, for the per-task meter ({} if it printed none)."""
        return {}

    async def _run(self, cmd: list[str], prompt: str, cwd: str) -> tuple[str, str, int]:
        env = {**os.environ, "NO_COLOR": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self._path = None  # moved or uninstalled: look again next time
            raise LLMError(f"{self.name}: could not start {self.label}: {e}", kind="unreachable") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), self.cfg.timeout_s)
        except TimeoutError as e:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise LLMError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from e
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0

    def _error(self, text: str, code: int) -> LLMError:
        low = text.lower()
        if any(h in low for h in LIMIT_HINTS):
            return LLMError(f"{self.name}: your {self.label} plan's usage limit is reached", kind="rate_limit")
        if any(h in low for h in SIGN_IN_HINTS):
            return LLMError(f"{self.name}: {self.label} is not signed in. Open a terminal and run "
                            f"`{self.names[0]}` once to sign in with your plan.")
        return LLMError(f"{self.name}: {self.label} exited with code {code}: {text.strip()[:300]}")

    async def unload(self) -> None:
        return None

    # --- sign-in (Settings > Connected apps) ----------------------------------------------
    def login_args(self) -> list[str]:
        raise NotImplementedError

    def status_args(self) -> list[str]:
        raise NotImplementedError

    def signed_in_from(self, stdout: str, stderr: str, code: int) -> bool:
        raise NotImplementedError

    async def signed_in(self) -> bool | None:
        """None = not installed or the status check itself failed."""
        exe = self.executable()
        if exe is None:
            return None
        with tempfile.TemporaryDirectory(prefix="delta-cli-") as work:
            try:
                stdout, stderr, code = await self._run([exe, *self.status_args()], "", work)
            except LLMError:
                return None
        return self.signed_in_from(stdout, stderr, code)

    def open_login(self) -> None:
        """The CLI's own sign-in in a console window the user can see; it opens the browser to
        the vendor's login page. Delta never sees the password or the token."""
        import subprocess

        exe = self.executable()
        if exe is None:
            raise LLMError(f"{self.label} is not installed", kind="unreachable")
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen([exe, *self.login_args()], creationflags=flags, cwd=tempfile.gettempdir())


class ClaudeCodeProvider(CLIProvider):
    """`claude -p` with the user's Claude Pro/Max login. Built-in tools and MCP servers are off."""

    names = ("claude",)
    label = "Claude Code"

    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def model_options(self) -> list[dict]:
        # Aliases always mean the newest model of that family, so they never go stale.
        return [{"id": "fable", "label": "Fable (newest, most capable)", "efforts": list(self.EFFORTS)},
                {"id": "opus", "label": "Opus (deep reasoning)", "efforts": list(self.EFFORTS)},
                {"id": "sonnet", "label": "Sonnet (balanced)", "efforts": list(self.EFFORTS)},
                {"id": "haiku", "label": "Haiku (fastest, fewest tokens)", "efforts": list(self.EFFORTS)}]

    def command(self, exe: str, model: str, out_file: Path, effort: str = "",
                images: list[Path] | None = None) -> list[str]:
        # Images only go in through the streaming JSON input, which needs streaming output too.
        io = (["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"] if images
              else ["--output-format", "json"])
        cmd = [exe, "-p", *io, "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--system-prompt", CLI_SYSTEM_PROMPT]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def stdin(self, prompt: str, images: list[Path]) -> str:
        if not images:
            return prompt
        blocks: list[dict] = [{"type": "text", "text": prompt}]
        for f in images:
            kind = "jpeg" if f.suffix == ".jpg" else f.suffix.lstrip(".")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": f"image/{kind}",
                                                       "data": base64.b64encode(f.read_bytes()).decode()}})
        return json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"

    def reply(self, stdout: str, out_file: Path) -> str:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # Streaming output: one JSON event per line, the answer is in the "result" event.
            data = next((e for e in map(_json_or_none, reversed(stdout.splitlines()))
                         if isinstance(e, dict) and e.get("type") == "result"), None)
            if data is None:
                return stdout.strip()
        if data.get("is_error"):
            raise self._error(str(data.get("result", "")), 1)
        return str(data.get("result", ""))

    def login_args(self) -> list[str]:
        return ["auth", "login"]

    def status_args(self) -> list[str]:
        return ["auth", "status", "--json"]

    def signed_in_from(self, stdout: str, stderr: str, code: int) -> bool:
        try:
            return bool(json.loads(stdout).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            return False

    # --- live sessions ---------------------------------------------------------------------
    # Starting `claude` costs ~6 s per call, and a fresh process re-reads the whole history with no
    # cache. One process per conversation, fed only the new messages, answers in ~3-4 s and reads
    # the earlier turns from the prompt cache (about a tenth of the token price).
    def __init__(self, name: str, cfg: ProviderConfig) -> None:
        super().__init__(name, cfg)
        self._live: dict[str, _Session] = {}   # conversation (system prompt) -> session
        self._spare: dict[str, _Session] = {}  # model/effort -> started, unused session

    def session_args(self, exe: str, model: str, effort: str) -> list[str]:
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--system-prompt", CLI_SYSTEM_PROMPT]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    async def _spawn(self, model: str, effort: str) -> _Session:
        exe = self.executable()
        if exe is None:
            raise LLMError(f"{self.name}: {self.label} is not installed", kind="unreachable")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.session_args(exe, model, effort), cwd=tempfile.gettempdir(),
                env={**os.environ, "NO_COLOR": "1"}, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2**24)
        except OSError as e:
            self._path = None
            raise LLMError(f"{self.name}: could not start {self.label}: {e}", kind="unreachable") from e
        return _Session(proc, f"{model}/{effort}")

    async def prewarm(self, task: str) -> None:
        """Start the process for `task` now (e.g. while the plan is being made), so the first
        step does not wait for Claude Code to boot."""
        key = f"{self.model_for(task)}/{self.effort_for(task)}"
        if self.available() and key not in self._spare:
            with contextlib.suppress(LLMError):
                self._spare[key] = await self._spawn(self.model_for(task), self.effort_for(task))

    async def complete(
        self, task: str, messages: list[Message], tools: list[dict] | None, json_mode: bool = False
    ) -> LLMResponse:
        model, effort = self.model_for(task), self.effort_for(task)
        full = inject_tool_prompt(messages, tools) if tools else list(messages)
        system = str(full[0].get("content", "")) if full else ""
        convo = f"{model}/{effort}/" + hashlib.sha1(system[:20000].encode()).hexdigest()
        sess = self._live.get(convo)
        if sess is not None and sess.continues(messages):
            new = flatten_messages(messages[sess.sent + 1:])  # +1: skip our own last reply
        else:
            if sess is not None:
                sess.close()
            sess = self._spare.pop(f"{model}/{effort}", None)
            if sess is None or not sess.alive():
                sess = await self._spawn(model, effort)
            new = full
            self._live[convo] = sess
            self._trim()
        prompt = render_transcript(new)
        if json_mode and not tools:
            prompt += "\n\nReply with ONLY a JSON object."
        with tempfile.TemporaryDirectory(prefix="delta-cli-") as work:
            line = self.stdin(prompt, take_images(new, Path(work))) if _has_images(new) else json.dumps(
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n"
        try:
            event = await sess.ask(line, self.cfg.timeout_s)
        except LLMError:
            sess.close()
            self._live.pop(convo, None)
            raise
        if event.get("is_error"):
            sess.close()
            self._live.pop(convo, None)
            raise self._error(str(event.get("result", "")), 1)
        sess.sent = len(messages)
        sess.system = messages[0].get("content") if messages else None
        content = str(event.get("result", ""))
        usage = _usage(event.get("usage") or {})
        if tools:
            resp = parse_prompt_tools(content, self.name, model or "default")
            resp.usage = usage
            return resp
        return LLMResponse(content=content, provider=self.name, model=model or "default", usage=usage)

    def _trim(self, keep: int = 2) -> None:
        """At most `keep` live conversations (each process holds ~250 MB of RAM)."""
        while len(self._live) > keep:
            oldest = min(self._live, key=lambda k: self._live[k].last_used)
            self._live.pop(oldest).close()

    async def unload(self) -> None:
        """End of a task or shutdown: free the processes."""
        for s in [*self._live.values(), *self._spare.values()]:
            s.close()
        self._live.clear()
        self._spare.clear()


class _Session:
    """One running `claude -p --input-format stream-json` process: a conversation that stays open."""

    def __init__(self, proc: asyncio.subprocess.Process, key: str) -> None:
        self.proc = proc
        self.key = key
        self.sent = 0           # how many of Delta's messages this conversation has seen
        self.system: object = None
        self.last_used = time.monotonic()
        self._lock = asyncio.Lock()

    def alive(self) -> bool:
        return self.proc.returncode is None

    def continues(self, messages: list[Message]) -> bool:
        """Same conversation, grown by our last reply plus new messages."""
        return (self.alive() and self.sent > 0 and len(messages) > self.sent + 1
                and messages[0].get("content") == self.system and messages[self.sent].get("role") == "assistant")

    async def ask(self, line: str, limit_s: float) -> dict:
        async with self._lock:
            self.last_used = time.monotonic()
            if not self.alive() or self.proc.stdin is None or self.proc.stdout is None:
                raise LLMError("claude_code: session ended", kind="unreachable")
            try:
                self.proc.stdin.write(line.encode("utf-8"))
                await self.proc.stdin.drain()
                return await asyncio.wait_for(self._read_result(), limit_s)
            except TimeoutError as e:
                raise LLMError(f"claude_code: timed out after {limit_s}s") from e
            except (ConnectionError, BrokenPipeError) as e:
                raise LLMError("claude_code: session ended", kind="unreachable") from e

    async def _read_result(self) -> dict:
        assert self.proc.stdout is not None
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise LLMError("claude_code: session ended unexpectedly", kind="unreachable")
            event = _json_or_none(raw.decode("utf-8", "replace"))
            if isinstance(event, dict) and event.get("type") == "result":
                return event

    def close(self) -> None:
        if self.alive():
            with contextlib.suppress(ProcessLookupError, OSError):
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
                self.proc.kill()


def _has_images(messages: list[Message]) -> bool:
    return any(isinstance(m.get("content"), list) and any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"]) for m in messages)


def _usage(u: dict) -> dict[str, int]:
    """Claude's usage block -> Delta's counters. Cache reads are counted apart: they cost ~10%."""
    return {"input": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "cached": int(u.get("cache_read_input_tokens") or 0), "output": int(u.get("output_tokens") or 0)}


class CodexProvider(CLIProvider):
    """`codex exec` with the user's ChatGPT plan login, sandboxed read-only."""

    names = ("codex",)
    label = "Codex"
    preamble = CLI_SYSTEM_PROMPT + " Do not run commands or read files.\n\n"

    def model_options(self) -> list[dict]:
        """The models this user's ChatGPT plan offers, from the list Codex itself keeps."""
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            data = json.loads((Path.home() / ".codex" / "models_cache.json").read_text(encoding="utf-8"))
            return [{"id": m["slug"], "label": m.get("display_name") or m["slug"],
                     "efforts": [e["effort"] for e in m.get("supported_reasoning_levels", [])]}
                    for m in data["models"] if m.get("visibility") != "hide"]
        return []

    def command(self, exe: str, model: str, out_file: Path, effort: str = "",
                images: list[Path] | None = None) -> list[str]:
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--output-last-message", str(out_file)]
        for f in images or []:
            cmd += ["--image", str(f)]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        return [*cmd, "--", "-"]  # "-": read the prompt from stdin ("--": not another image)

    def usage(self, stdout: str, stderr: str) -> dict[str, int]:
        """`codex exec` ends with 'tokens used' and the total (e.g. '2,658'): count it as input+output,
        since this mode does not split them."""
        m = re.search(r"tokens used\s*[:\n\r ]\s*([\d,]+)", f"{stderr}\n{stdout}", re.I)
        return {"input": int(m.group(1).replace(",", ""))} if m else {}

    def reply(self, stdout: str, out_file: Path) -> str:
        if out_file.is_file():
            return out_file.read_text(encoding="utf-8", errors="replace").strip()
        return stdout.strip()

    def login_args(self) -> list[str]:
        return ["login"]

    def status_args(self) -> list[str]:
        return ["login", "status"]

    def signed_in_from(self, stdout: str, stderr: str, code: int) -> bool:
        text = (stdout + stderr).lower()  # Codex prints the status on stderr
        return code == 0 and "logged in" in text and "not logged in" not in text


def _json_or_none(line: str) -> object:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
