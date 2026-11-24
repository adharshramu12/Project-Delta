"""A stand-in for `claude -p` in the Build engine tests. It behaves like Claude Code in the ways Delta relies on: it
reads the --settings JSON, runs the PreToolUse hook command before each file action (with the action as JSON on stdin),
does the action only when the hook allows it (exit 0), and streams stream-json events ending in a "result" event.

Script (from the prompt on stdin): read hello.py, try to read ../outside.txt and .env, edit hello.py, create new.py,
try a Bash command. The answer reports which actions the guard allowed, and whether an API key leaked into its
environment.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
settings = json.loads(args[args.index("--settings") + 1])
hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
sys.stdin.read()
root = Path.cwd()


def emit(event):
    print(json.dumps(event), flush=True)


def act(n, tool, data):
    use = {"type": "tool_use", "id": f"t{n}", "name": tool, "input": data}
    emit({"type": "assistant", "message": {"content": [use]}})
    r = subprocess.run(hook, shell=True, input=json.dumps({"tool_name": tool, "tool_input": data}),
                       capture_output=True, text=True)
    if r.returncode != 0:
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "is_error": True,
                                                       "content": r.stderr.strip()}]}})
        return False
    if tool == "Edit":
        p = root / data["file_path"]
        p.write_text(p.read_text().replace(data["old_string"], data["new_string"]))
    elif tool == "Write":
        (root / data["file_path"]).write_text(data["content"])
    return True


done = {
    "read": act(1, "Read", {"file_path": "hello.py"}),
    "outside": act(2, "Read", {"file_path": "../outside.txt"}),
    "env": act(3, "Read", {"file_path": str(root / ".env")}),
    "edit": act(4, "Edit", {"file_path": "hello.py", "old_string": "'hi'", "new_string": "'hello there'"}),
    "write": act(5, "Write", {"file_path": "new.py", "content": "x = 1\n"}),
    "bash": act(6, "Bash", {"command": "echo hi"}),
}
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": json.dumps({"allowed": done, "key_leaked": "ANTHROPIC_API_KEY" in os.environ}),
      "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20}})
