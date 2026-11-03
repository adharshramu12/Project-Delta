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
import contextvars
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from delta.config import ProviderConfig
from delta.llm.providers import flatten_messages, inject_tool_prompt, parse_prompt_tools
from delta.llm.schemas import LLMError, LLMResponse, Message
from delta.system.logging import log

# Replaces Claude Code's own (large, coding-focused) system prompt, which also saves plan quota.
CLI_SYSTEM_PROMPT = ("You are the reasoning engine of a desktop assistant called Delta. Delta's tools (listed "
                     "in the conversation when a step needs them) are how you act on the user's PC; you call them "
                     "by replying in the format the conversation asks for. Follow its instructions exactly.")

# Codex is only Delta's text model, but by default it brings its whole agent: shell, multi-agent,
# browser and computer use, image generation, skills, the user's desktop-app plugins and MCP servers.
# Their instructions and tool definitions ride along on EVERY call (measured: a one-word reply cost
# ~11.7k tokens, ~4.8k with these off). Switched off for Delta's sessions only; the user's own Codex
# keeps them. Unknown feature names are ignored by Codex, so older versions are fine.
CODEX_FEATURES_OFF = ("apps", "browser_use", "browser_use_external", "computer_use", "goals", "image_generation",
                      "in_app_browser", "multi_agent", "plugins", "shell_tool", "skill_search", "sleep_tool",
                      "tool_suggest", "unified_exec", "view_image", "hooks", "code_mode_host", "remote_plugin",
                      "plugin_discovery_prefetch", "mentions_v2", "workspace_dependencies")


def lean_codex_args() -> list[str]:
    """`-c` overrides that strip Codex down to a plain model for Delta (see CODEX_FEATURES_OFF)."""
    sets = [f"features.{f}=false" for f in CODEX_FEATURES_OFF]
    sets += ['web_search="disabled"', "include_permissions_instructions=false", "include_apps_instructions=false",
             "include_environment_context=false", "project_doc_max_bytes=0"]
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    with contextlib.suppress(OSError, ValueError):
        import tomllib

        cfg = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        # Codex splits -c keys on dots and keeps quotes as part of the name, so keys go unquoted
        # (a quoted one names a new, empty MCP server and Codex refuses to start); dotted names are skipped.
        sets += [f"plugins.{p}.enabled=false" for p in cfg.get("plugins", {}) if "." not in p]
        sets += [f"mcp_servers.{m}.enabled=false" for m in cfg.get("mcp_servers", {}) if "." not in m]
    return [arg for s in sets for arg in ("-c", s)]


# Said on every turn that offers tools. Without it a CLI model judges by its own sandbox and may
# answer "this environment doesn't provide desktop tools" while Delta's tools are right there.
TOOLS_ONLY_REMINDER = ("\n\nYou act on the user's PC ONLY through the tools listed above, and they all work: "
                       "use them, never say tools are missing. Do not run commands yourself. Reply with the "
                       'JSON {"tool_calls": [...]} or {"final": "..."} described above.')

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
        if tools:
            prompt += TOOLS_ONLY_REMINDER
        if json_mode and not tools:
            prompt += "\n\nReply with ONLY a JSON object."
        # An empty scratch folder as working dir: no project files, CLAUDE.md or AGENTS.md leak in.
        with tempfile.TemporaryDirectory(prefix="delta-cli-", ignore_cleanup_errors=True) as work:
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
        with tempfile.TemporaryDirectory(prefix="delta-cli-", ignore_cleanup_errors=True) as work:
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
        with tempfile.TemporaryDirectory(prefix="delta-cli-", ignore_cleanup_errors=True) as work:
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
    return bool(_image_urls(messages))


def _image_urls(messages: list[Message]) -> list[str]:
    """The data: URLs of the screenshots in these messages, in order."""
    return [str(p["image_url"]["url"]) for m in messages if isinstance(m.get("content"), list)
            for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url"]


def _usage(u: dict) -> dict[str, int]:
    """Claude's usage block -> Delta's counters. Cache reads are counted apart: they cost ~10%."""
    return {"input": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "cached": int(u.get("cache_read_input_tokens") or 0), "output": int(u.get("output_tokens") or 0)}


class _CodexSession:
    """One `codex app-server` (JSON-RPC over stdio) holding one conversation ("thread").

    `codex exec` starts a process per call (~16 s each). The app server stays up: after the first
    turn, each turn takes ~1.5 s and earlier turns come from the prompt cache. Read-only sandbox and
    approval policy "never": Codex runs nothing on the PC; Delta's own tools do all the work."""

    def __init__(self, proc: asyncio.subprocess.Process, thread_id: str, key: str) -> None:
        self.proc, self.thread_id, self.key = proc, thread_id, key
        self.sent = 0
        self.system: object = None
        self.last_used = time.monotonic()
        self._lock = asyncio.Lock()
        self._next = 100

    @classmethod
    async def start(cls, exe: str, model: str, key: str, limit_s: float = 30,
                    extra: list[str] | None = None) -> _CodexSession:
        proc = await asyncio.create_subprocess_exec(
            exe, "app-server", *(extra or []), cwd=tempfile.gettempdir(), env={**os.environ, "NO_COLOR": "1"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=2**24)
        s = cls(proc, "", key)
        try:
            await s._call("initialize", {"clientInfo": {"name": "delta", "version": "1"}}, limit_s)
            s._write({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            params: dict[str, Any] = {"sandbox": "read-only", "approvalPolicy": "never", "ephemeral": True,
                                      "baseInstructions": CLI_SYSTEM_PROMPT + " Do not run commands or read files."}
            if model:
                params["model"] = model
            result = await s._call("thread/start", params, limit_s)
            s.thread_id = str(result["thread"]["id"])
        except Exception:
            s.close()
            raise
        return s

    def alive(self) -> bool:
        return self.proc.returncode is None

    def continues(self, messages: list[Message]) -> bool:
        return _Session.continues(self, messages)  # type: ignore[arg-type]

    def _write(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))

    async def _read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise LLMError("codex: app server stopped", kind="unreachable")
            msg = _json_or_none(raw.decode("utf-8", "replace"))
            if not isinstance(msg, dict):
                continue
            if msg.get("method") and "id" in msg:  # a request from Codex (e.g. approval): always no
                self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "not allowed"}})
                continue
            return msg

    async def _call(self, method: str, params: dict[str, Any], limit_s: float) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

        async def wait() -> dict[str, Any]:
            while True:
                msg = await self._read()
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise LLMError(f"codex: {method} failed: {str(msg['error'])[:200]}")
                    return dict(msg.get("result") or {})

        return await asyncio.wait_for(wait(), limit_s)

    async def ask(self, text: str, model: str, effort: str, limit_s: float,
                  images: list[str] | None = None) -> tuple[str, dict[str, int]]:
        """One turn: send the new messages (and any new screenshots, as data: URLs), return
        (reply, token usage for this turn)."""
        async with self._lock:
            self.last_used = time.monotonic()
            params: dict[str, Any] = {"threadId": self.thread_id, "input": [
                {"type": "text", "text": text}, *({"type": "image", "url": u} for u in images or [])]}
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            self._next += 1
            rid = self._next
            self._write({"jsonrpc": "2.0", "id": rid, "method": "turn/start", "params": params})
            reply: list[str] = []
            usage: dict[str, int] = {}

            async def collect() -> None:
                while True:
                    msg = await self._read()
                    method, p = msg.get("method"), msg.get("params") or {}
                    if msg.get("id") == rid and "error" in msg:
                        raise LLMError(f"codex: {str(msg['error'])[:200]}")
                    if method == "item/completed" and (p.get("item") or {}).get("type") == "agentMessage":
                        reply.append(str(p["item"].get("text", "")))
                    elif method == "thread/tokenUsage/updated":
                        last = (p.get("tokenUsage") or {}).get("last") or {}
                        cached = int(last.get("cachedInputTokens") or 0)
                        usage.update(input=int(last.get("inputTokens") or 0) - cached, cached=cached,
                                     output=int(last.get("outputTokens") or 0))
                    elif method == "error":
                        raise LLMError(f"codex: {str(p)[:200]}", kind="rate_limit" if "limit" in str(p).lower()
                                       else "error")
                    elif method == "turn/completed":
                        return

            try:
                await asyncio.wait_for(collect(), limit_s)
            except TimeoutError as e:
                raise LLMError(f"codex: timed out after {limit_s}s") from e
            return "\n".join(reply).strip(), usage

    def close(self) -> None:
        if self.alive():
            with contextlib.suppress(ProcessLookupError, OSError):
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
                self.proc.kill()


class CodexProvider(CLIProvider):
    """Codex with the user's ChatGPT plan login. A live `codex app-server` conversation per task
    (fast, cached); turns with screenshots, or an older Codex without the server, use `codex exec`."""

    names = ("codex",)
    label = "Codex"
    preamble = CLI_SYSTEM_PROMPT + " Do not run commands or read files.\n\n"

    def __init__(self, name: str, cfg: ProviderConfig) -> None:
        super().__init__(name, cfg)
        self._live: dict[str, _CodexSession] = {}
        self._spare: dict[str, _CodexSession] = {}
        self._no_server = False  # this Codex has no app server: one-shot only
        self._plain = False  # this Codex refused lean_codex_args(): run it as configured

    async def _session(self, model: str) -> _CodexSession | None:
        exe = self.executable()
        if exe is None or self._no_server:
            return None
        for extra in ([] if self._plain else lean_codex_args(), []):
            try:
                return await _CodexSession.start(exe, model, model, extra=extra)
            except (LLMError, OSError, TimeoutError, KeyError, ValueError):
                if extra:  # this Codex rejects an override: keep working, just without the savings
                    log.warning("codex.lean_rejected")
                    self._plain = True
        self._no_server = True  # fall back to `codex exec` from now on
        return None

    async def prewarm(self, task: str) -> None:
        model = self.model_for(task)
        if model not in self._spare and (s := await self._session(model)):
            self._spare[model] = s

    async def complete(
        self, task: str, messages: list[Message], tools: list[dict] | None, json_mode: bool = False
    ) -> LLMResponse:
        # Screenshots go into the live conversation too: a one-shot `codex exec` per screenshot turn
        # re-sent the whole task uncached (a NotebookLM task: 40.9k tokens) and lost the session's framing.
        if self._no_server:
            return await super().complete(task, messages, tools, json_mode)
        model, effort = self.model_for(task), self.effort_for(task)
        full = inject_tool_prompt(messages, tools) if tools else list(messages)
        system = str(full[0].get("content", "")) if full else ""
        convo = f"{model}/" + hashlib.sha1(system[:20000].encode()).hexdigest()
        sess = self._live.get(convo)
        if sess is not None and sess.alive() and sess.continues(messages):
            images = _image_urls(messages[sess.sent + 1:])
            new = flatten_messages(messages[sess.sent + 1:])
        else:
            images = _image_urls(messages)
            if sess is not None:
                sess.close()
            sess = self._spare.pop(model, None)
            if sess is None or not sess.alive():
                sess = await self._session(model)
            if sess is None:
                return await super().complete(task, messages, tools, json_mode)
            new = full
            self._live[convo] = sess
            while len(self._live) > 2:  # each server holds RAM: keep at most two conversations
                oldest = min(self._live, key=lambda k: self._live[k].last_used)
                self._live.pop(oldest).close()
        # Codex keeps its own shell tool in server mode; it must act only through Delta's tools,
        # so the first turn says so in full and every turn with tools repeats it briefly.
        prompt = (self.preamble if new is full else "") + render_transcript(new)
        if tools:
            prompt += TOOLS_ONLY_REMINDER
        if json_mode and not tools:
            prompt += "\n\nReply with ONLY a JSON object."
        try:
            content, usage = await sess.ask(prompt, model, effort, self.cfg.timeout_s, images)
        except LLMError:
            sess.close()
            self._live.pop(convo, None)
            raise
        sess.sent = len(messages)
        sess.system = messages[0].get("content") if messages else None
        if tools:
            resp = parse_prompt_tools(content, self.name, model or "default")
            resp.usage = usage
            return resp
        return LLMResponse(content=content, provider=self.name, model=model or "default", usage=usage)

    async def unload(self) -> None:
        for s in [*self._live.values(), *self._spare.values()]:
            s.close()
        self._live.clear()
        self._spare.clear()

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
               "--output-last-message", str(out_file), *([] if self._plain else lean_codex_args())]
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


def _cline_tool_calls(lines: list[str], wanted: frozenset[str]) -> list[dict[str, Any]]:
    """Delta tool calls the model made as Cline-native tool calls ({"name", "arguments"} each)."""
    calls = []
    for line in lines:
        e = _json_or_none(line)
        ev = e.get("event") if isinstance(e, dict) else None
        if (isinstance(ev, dict) and ev.get("type") == "content_start" and ev.get("contentType") == "tool"
                and ev.get("toolName") in wanted):
            calls.append({"name": ev["toolName"], "arguments": ev.get("input") or {}})
    return calls


def _json_or_none(line: str) -> object:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


class ClineProvider(CLIProvider):
    """Cline with the user's Cline account (its free credits, or any provider they set up in Cline).
    The Cline desktop app ships `cline-cli.exe`; one run per call, with Delta's system prompt in
    place of Cline's coding one and Cline's tool approvals off, so it only answers in text."""

    names = ("cline", "cline-cli")
    label = "Cline"

    def supports(self, task: str) -> bool:
        return task != "vision" and super().supports(task)  # its CLI takes no screenshots
    MAX_PROMPT_CHARS = 30000  # the prompt is a command-line argument; Windows allows ~32k characters
    # The list the Cline app shows (free, recommended, ...). Free models cost the user nothing.
    MODELS_URL = "https://api.cline.bot/api/v1/ai/cline/recommended-models"
    FREE_OFFLINE = ("reflection/beam-free", "cline-free/solar-mini4", "cline-free/nemotron-3.5-super-vl",
                    "nvidia/nemotron-3.5-lightning-free")  # Cline's free list on 2026-11-02, if offline
    EFFORTS = ("low", "medium", "high")
    CATALOG_TTL_S = 3600

    def __init__(self, name: str, cfg: ProviderConfig) -> None:
        super().__init__(name, cfg)
        self._catalog: tuple[float, dict[str, Any]] | None = None
        self._force_model: contextvars.ContextVar[str] = contextvars.ContextVar(f"{name}_model", default="")
        # Delta's tools offered on this call. Cline's model calls them as NATIVE tools (not as the JSON
        # text Delta asks for); Cline rejects those as unknown and Delta saw an empty reply. Each such
        # call is captured as a Delta tool call and Cline is stopped right there.
        self._delta_tools: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
            f"{name}_tools", default=frozenset())

    def catalog(self) -> dict[str, Any]:
        """Cline's current model list ({"free": [...], "recommended": [...], ...}), cached for an hour."""
        if self._catalog and time.monotonic() - self._catalog[0] < self.CATALOG_TTL_S:
            return self._catalog[1]
        data: dict[str, Any] = {"free": [{"id": m, "name": m.split("/")[-1]} for m in self.FREE_OFFLINE]}
        with contextlib.suppress(Exception):
            import httpx

            got = httpx.get(self.MODELS_URL, timeout=5).json()
            if isinstance(got, dict) and got.get("free"):
                data = got
        self._catalog = (time.monotonic(), data)
        return data

    def free_models(self) -> list[str]:
        return [str(m["id"]) for m in self.catalog().get("free", []) if isinstance(m, dict) and m.get("id")]

    def model_options(self) -> list[dict]:
        """Free models first (no credits needed), then the paid ones Cline recommends."""
        def label(m: dict[str, Any]) -> str:
            return str(m.get("name") or m["id"]).split("/")[-1].replace("-", " ").strip()

        cat = self.catalog()
        free = [{"id": m["id"], "label": f"{label(m)} (free)", "efforts": list(self.EFFORTS)}
                for m in cat.get("free", []) if isinstance(m, dict) and m.get("id")]
        paid = [{"id": m["id"], "label": f"{label(m)} (uses credits)", "efforts": list(self.EFFORTS)}
                for m in cat.get("recommended", []) if isinstance(m, dict) and m.get("id")]
        return free + paid

    def model_for(self, task: str) -> str:
        return self._force_model.get() or super().model_for(task)

    async def complete(
        self, task: str, messages: list[Message], tools: list[dict] | None, json_mode: bool = False
    ) -> LLMResponse:
        """Out of credits on a paid model: answer with a free Cline model instead of giving up."""
        self._delta_tools.set(frozenset(str(t["function"]["name"]) for t in tools or []))
        try:
            return await super().complete(task, messages, tools, json_mode)
        except LLMError as e:
            free = await asyncio.to_thread(self.free_models)
            if e.kind != "quota" or not free or self.model_for(task) in free:
                raise
            log.info("cline.free_fallback", task=task, model=free[0])
            token = self._force_model.set(free[0])
            try:
                return await super().complete(task, messages, tools, json_mode)
            finally:
                self._force_model.reset(token)

    def executable(self) -> str | None:
        found = super().executable()
        if found is None and (local := os.environ.get("LOCALAPPDATA")):
            app = Path(local) / "Cline" / "cline-cli.exe"  # the desktop app's own copy
            if app.is_file():
                self._path = found = str(app)
        return found

    def command(self, exe: str, model: str, out_file: Path, effort: str = "",
                images: list[Path] | None = None) -> list[str]:
        cmd = [exe, "--json", "--auto-approve", "false", "--compaction", "off", "-c", str(out_file.parent),
               "-s", CLI_SYSTEM_PROMPT]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--thinking", effort]
        return cmd

    async def _run(self, cmd: list[str], prompt: str, cwd: str) -> tuple[str, str, int]:
        # Cline reads its prompt only from the command line (piped input is not detected on Windows).
        if len(prompt) > self.MAX_PROMPT_CHARS:
            raise LLMError(f"{self.name}: prompt too long for Cline's command line", kind="too_large")
        if prompt:
            cmd = [*cmd, prompt]
        wanted = self._delta_tools.get()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env={**os.environ, "NO_COLOR": "1"}, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=2**24)
        except OSError as e:
            self._path = None
            raise LLMError(f"{self.name}: could not start Cline: {e}", kind="unreachable") from e
        lines: list[str] = []

        async def read() -> bool:
            """Collect events; True as soon as the model picked one of Delta's tools."""
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace")
                lines.append(line)
                if wanted and _cline_tool_calls([line], wanted):
                    return True
            return False

        err_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None
        stopped, finished = False, False
        try:
            stopped = await asyncio.wait_for(read(), self.cfg.timeout_s)
            if not stopped:  # output ended: let Cline exit on its own and keep its exit code
                await asyncio.wait_for(proc.wait(), 15)
                finished = True
        except TimeoutError as e:
            raise LLMError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from e
        finally:
            if not finished and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()  # got Delta's tool call: Cline's own retries would only cost time
                await proc.wait()
        err = (await err_task).decode("utf-8", "replace") if err_task else ""
        return "".join(lines), err, 0 if stopped else (proc.returncode or 0)

    def _events(self, stdout: str) -> list[dict[str, Any]]:
        return [e for e in map(_json_or_none, stdout.splitlines()) if isinstance(e, dict)]

    def _result(self, stdout: str) -> dict[str, Any]:
        return next((e for e in reversed(self._events(stdout)) if e.get("type") == "run_result"), {})

    def reply(self, stdout: str, out_file: Path) -> str:
        if calls := _cline_tool_calls(stdout.splitlines(), self._delta_tools.get()):
            return json.dumps({"tool_calls": calls})  # parsed like any JSON tool reply
        result = self._result(stdout)
        if result.get("finishReason") == "error":
            raise self._error(str(result.get("text") or "Cline reported an error"), 1)
        return str(result.get("text") or "").strip()

    def usage(self, stdout: str, stderr: str) -> dict[str, int]:
        u: dict[str, Any] = self._result(stdout).get("usage") or {}
        if not u:  # stopped early: no run_result yet, the last usage event has the totals
            last: dict[str, Any] = next((e["event"] for e in reversed(self._events(stdout))
                                         if isinstance(e.get("event"), dict) and e["event"].get("type") == "usage"), {})
            u = {"inputTokens": last.get("totalInputTokens"), "outputTokens": last.get("totalOutputTokens"),
                 "cacheReadTokens": last.get("totalCacheReadTokens")}
        return {"input": int(u.get("inputTokens") or 0) + int(u.get("cacheWriteTokens") or 0),
                "cached": int(u.get("cacheReadTokens") or 0), "output": int(u.get("outputTokens") or 0)}

    def _error(self, text: str, code: int) -> LLMError:
        if "insufficient balance" in text.lower() or "credits balance" in text.lower():
            # Not a wait-and-retry limit: move on to the next AI at once.
            return LLMError(f"{self.name}: your Cline credits are used up (add credits in the Cline app)",
                            kind="quota")
        last = next((e for e in reversed([_json_or_none(x) for x in text.splitlines()])
                     if isinstance(e, dict) and e.get("type") == "error"), None)
        return super()._error(str(last.get("message")) if last else text, code)

    def login_args(self) -> list[str]:
        return ["auth"]

    async def signed_in(self) -> bool | None:
        """Cline's status command needs a terminal, so read its sign-in file instead (never the token)."""
        if self.executable() is None:
            return None
        with contextlib.suppress(OSError, ValueError):
            data = json.loads((Path.home() / ".cline" / "data" / "settings" / "providers.json")
                              .read_text(encoding="utf-8"))
            return bool(data.get("providers", {}).get(data.get("lastUsedProvider", ""), {}).get("settings"))
        return False


# Provider kinds that run an AI app the user already has, by `kind` in config/delta.yaml.
CLI_KINDS: dict[str, type[CLIProvider]] = {"claude_code": ClaudeCodeProvider, "codex": CodexProvider,
                                           "cline": ClineProvider}
