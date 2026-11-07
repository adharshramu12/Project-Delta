"""CI only (tests/ci/fresh_user_journey.ps1): a stand-in for LM Studio's local server on 127.0.0.1:1234.

A brand-new user's Delta finds a local model the way it finds LM Studio: an OpenAI-style server on this PC. This
script answers like one, with fixed replies, so the fresh-user journey can send real chat messages and run real
tasks with no API key, no network and no cost. Delta's own config (providers, routes) is not changed: this is the
same thing Delta would see if the user started LM Studio.

Replies, by what Delta asks (the planner, the agent loop with Delta's JSON tool protocol, the checker):
  planner, request mentions "notepad"  -> one direct open_app call (the fast path)
  planner, request mentions "slowtask" -> waits a long time (the journey stops that task while it waits)
  planner, anything else               -> a plain answer containing FAKE-REPLY-OK
  agent loop                           -> {"final": "... FAKE-DONE"}
  checker                              -> every checkpoint met
Standard library only: it runs on the runner's own Python, never Delta's environment (which `delta update`
replaces and the uninstaller removes). Every request is logged to the file given as the first argument.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen2.5-7b-instruct"
LOG = sys.argv[1] if len(sys.argv) > 1 else "fake-lmstudio.log"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 1234


def log(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {line}\n")


def text_of(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):  # a picture with text
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content)


def reply_for(messages: list[dict]) -> tuple[str, str]:
    system = text_of(messages[0]) if messages and messages[0].get("role") == "system" else ""
    users = [text_of(m) for m in messages if m.get("role") == "user"]
    request = users[-1] if users else ""
    if "You are the planner of" in system:
        low = request.lower()
        if "slowtask" in low:
            time.sleep(300)  # the journey cancels the task long before this ends
            return "plan-slow", json.dumps({"needs_tools": False, "answer": "too late"})
        if "notepad" in low:
            return "plan-notepad", json.dumps({
                "needs_tools": True, "goal": "Open Notepad", "steps": ["open Notepad"], "tools": ["open_app"],
                "success_criteria": ["Notepad is open"], "skill_id": None,
                "direct": {"tool": "open_app", "args": {"name": "Notepad"}}, "remember": []})
        return "plan-answer", json.dumps({"needs_tools": False, "answer": "Hello! FAKE-REPLY-OK", "remember": []})
    if "passed is true only when every checkpoint is met" in system:
        checks = [{"checkpoint": line[2:].strip(), "met": True, "seen": "tool log"}
                  for line in request.split("CHECKPOINTS:", 1)[-1].split("TOOL LOG:", 1)[0].splitlines()
                  if line.startswith("- ")]
        return "verify", json.dumps({"checks": checks, "passed": True, "reason": "fake checker"})
    if "To call tools reply with ONLY this JSON" in system:
        return "agent", json.dumps({"final": "Done. FAKE-DONE"})
    return "other", json.dumps({})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return None

    def _send(self, body: dict, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:  # Delta gave up on this request (a stopped task)
            pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/api/v0/models"):
            self._send({"object": "list", "data": [{"id": MODEL, "object": "model", "state": "loaded"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send({"error": "not found"}, 404)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        kind, content = reply_for(body.get("messages") or [])
        log(f"{kind}: {content[:200]}")
        self._send({"id": "fake", "object": "chat.completion", "created": int(time.time()), "model": MODEL,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


if __name__ == "__main__":
    log(f"listening on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
