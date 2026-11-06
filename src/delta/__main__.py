"""Delta command line.

  delta              interactive terminal (same as `delta cli`)
  delta doctor       check models, keys, GPU, audit log
  delta secrets set|delete|list [name]
  delta audit verify
  delta captcha allow|remove|list [host]
  delta schedule add|list|pause|resume|remove|run [text | id]
  delta tasks [list|show|resume|discard|prune] [id]
  delta undo [task id]
  delta eval run [--suite S] [--model M] [--scripted]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

import httpx

from delta import __version__
from delta.config import load_settings
from delta.system.logging import setup_logging
from delta.system.secrets import KNOWN_SECRETS, delete_secret, get_secret, mask, set_secret


async def _cli() -> int:
    from delta.bootstrap import build
    from delta.channels.cli import CliChannel
    from delta.system.lifecycle import AlreadyRunningError

    settings = load_settings()
    setup_logging(settings.data_dir, console_level=logging.ERROR)  # the CLI shows events itself
    c = build(settings)
    try:
        await c.start()
    except AlreadyRunningError as e:
        print(e)
        return 1
    try:
        await CliChannel(c).run()
    finally:
        print("Shutting down: unloading models, stopping monitors…")
        await c.stop()
        print("Delta is fully stopped.")
    return 0


def running_delta(data_dir: Path) -> int | None:
    """The process id of a Delta service or terminal that is running now (its delta.pid), else None."""
    import psutil

    try:
        pid = int((data_dir / "delta.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid() or not psutil.pid_exists(pid):
        return None
    try:
        name = psutil.Process(pid).name().lower()
    except psutil.Error:
        return None
    return pid if ("python" in name or "delta" in name) else None  # a reused pid of something else is not Delta


def _own_processes(root: Path) -> list[int]:
    """This process plus the launchers above it that live in Delta's own environment (delta.exe and the venv's
    python.exe start the real interpreter as a child): the installer waits for all of them to exit."""
    import psutil

    pids = [os.getpid()]
    with contextlib.suppress(psutil.Error):
        for parent in psutil.Process().parents():
            exe = parent.exe()
            if exe and Path(exe).resolve().is_relative_to(root):
                pids.append(parent.pid)
    return pids


def _update(stop: bool = False, root: Path | None = None) -> int:
    """Newest version over the installed one (data untouched): the installer again, from GitHub.

    Safe while Delta runs: files in use cannot be replaced (Windows locks loaded .pyd files), so a running Delta
    must be closed first. Without --stop this only explains how; with --stop the installer closes it. The
    installer itself runs in its own window and starts only after this command has exited, so this command's
    own Python is not holding any of Delta's files either."""
    import subprocess

    root = root or Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        print("This is a developer checkout: update it with 'git pull', then 'uv sync --extra all'.")
        return 1
    script = root / "scripts" / "install.ps1"
    pid = running_delta(load_settings().data_dir)
    if pid is not None and not stop:
        print(f"Delta is running (process {pid}). Its files cannot be replaced while it runs.\n"
              "Close it (the Shut down button in Delta's window), then run 'delta update' again,\n"
              "or run 'delta update --stop' to close it and update now.")
        return 1
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-FromGitHub",
           "-InstallDir", str(root), "-NoLaunch", "-WaitForPid", ",".join(map(str, _own_processes(root)))]
    if stop:
        cmd.append("-StopRunning")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    # A fixed command line that runs Delta's own installer script (nothing from the user or the model).
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)  # nosec B603
    print("The update runs in a new window. It starts as soon as this command has finished, and your data is "
          "kept.\nOpen Delta again from its icon when it says it is done.")
    return 0


def _doctor() -> int:
    from delta.system.governor import SystemSensor

    s = load_settings()
    ok = True
    print(f"Delta {__version__}\nconfig: {s.config_dir}\ndata:   {s.data_dir}\n")
    print("Model providers:")
    for name, p in s.llm.providers.items():
        if p.kind != "openai":
            from delta.llm.cli_providers import CLI_KINDS

            cli = CLI_KINDS[p.kind](name, p)
            exe = cli.executable()
            state = f"found {exe}" if exe else f"not installed (optional: uses your {cli.label} plan)"
            print(f"  {name:<8} {state}")
        elif p.local and not p.keep_alive:  # LM Studio / Jan / llama.cpp: plain OpenAI-style server
            if p.discover == "lmstudio":
                from delta.llm.providers import lmstudio_base_url

                p = p.model_copy(update={"base_url": lmstudio_base_url(p.base_url)})
            try:
                ids = [m["id"] for m in httpx.get(f"{p.base_url.rstrip('/')}/models", timeout=3).json().get("data", [])]
                print(f"  {name:<8} running, models: {', '.join(ids) or 'none loaded'}")
            except (httpx.HTTPError, ValueError):
                print(f"  {name:<8} not running at {p.base_url} (optional)")
        elif p.local:
            root = p.base_url.removesuffix("/v1")
            try:
                tags = {m["name"] for m in httpx.get(f"{root}/api/tags", timeout=3).json().get("models", [])}
                for task, model in p.models.items():
                    have = model in tags or f"{model}:latest" in tags
                    ok &= have or task != "chat"
                    state = "ready" if have else f"NOT PULLED -> ollama pull {model}"
                    print(f"  {name:<8} {task:<7} {model:<28} {state}")
            except httpx.HTTPError:
                print(f"  {name:<8} not reachable at {root} (install/start Ollama; cloud fallback still works)")
        else:
            key = get_secret(p.secret) if p.secret else None
            print(f"  {name:<8} key {mask(key)}")
    print("\nSearch keys:")
    for k in ("tavily_api_key", "serpapi_api_key"):
        print(f"  {k:<16} {mask(get_secret(k))}")
    print("  duckduckgo       free fallback, always on")
    snap = SystemSensor().read()
    print(f"\nHardware: cpu {snap.cpu_pct:.0f}%  ram {snap.ram_pct:.0f}%  gpu {snap.gpu_util_pct}%  "
          f"gpu_temp {snap.gpu_temp_c}C  vram {snap.vram_pct and round(snap.vram_pct)}%  "
          f"battery {snap.battery_pct}% {'(plugged)' if snap.plugged else '(on battery)'}")
    from delta.safety.audit import AuditLog, load_or_create_key

    report = AuditLog(s.data_dir / "audit.jsonl", load_or_create_key(create=False)).verify_report()
    audit_ok = report.ok
    print(f"Audit log chain: {'intact' if audit_ok else 'TAMPERED'} ({report.text()})")
    from delta.config import in_onedrive, known_folder, save_dir

    out = save_dir(s.files, create=False)
    print(f"Save folder: {out} ({'OneDrive' if in_onedrive(out) else 'on this PC'})")
    if in_onedrive(known_folder("Desktop")) and not in_onedrive(out):
        print("  Desktop is synced by OneDrive, so files are kept locally; a 'Delta' shortcut on the "
              "Desktop opens them.")
    print(f"Boundaries: autonomy={s.boundaries.autonomy}, {len(s.boundaries.denied_paths)} denied paths")
    return 0 if ok and audit_ok else 1


def _secrets(action: str, name: str | None) -> int:
    if action == "list":
        for k, desc in KNOWN_SECRETS.items():
            print(f"  {k:<20} {mask(get_secret(k)):<12} {desc}")
        return 0
    if not name:
        print("name required, e.g. `delta secrets set groq_api_key`")
        return 2
    if action == "set":
        value = getpass.getpass(f"{KNOWN_SECRETS.get(name, name)} (input hidden): ").strip()
        if not value:
            print("empty, nothing saved")
            return 1
        set_secret(name, value)
        print(f"saved {name} to Windows Credential Manager")
    elif action == "delete":
        delete_secret(name)
        print(f"deleted {name}")
    return 0


def main() -> None:
    # Old third-party code (pywinauto) emits SyntaxWarnings on Python 3.12; users should never see them.
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    no_console = sys.stdout is None  # started by double-click (pythonw): give libraries a sink to write to
    if no_console:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if os.name == "nt":
        os.system("")  # enable ANSI colours in classic consoles
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="delta", description="Delta personal AI agent")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("cli", help="interactive terminal")
    rp = sub.add_parser("run", help="background service: dashboard API + Telegram/Slack")
    rp.add_argument("--open", action="store_true", help="open the dashboard window")
    sub.add_parser("app", help="open Delta (starts the service in the background if needed)")
    sub.add_parser("doctor", help="check setup")
    sp = sub.add_parser("secrets", help="manage API keys and tokens")
    sp.add_argument("action", choices=["set", "delete", "list"])
    sp.add_argument("name", nargs="?")
    ap = sub.add_parser("audit", help="audit log tools")
    ap.add_argument("action", choices=["verify"])
    cp = sub.add_parser("captcha", help="sites where Delta may read simple captchas for you")
    cp.add_argument("action", choices=["allow", "remove", "list"])
    cp.add_argument("host", nargs="?")
    up = sub.add_parser("update", help="update Delta to the newest version (keeps your data)")
    up.add_argument("--stop", action="store_true", help="close a running Delta first, then update")
    from delta.scheduler.cli import add_parser as add_schedule_parser

    add_schedule_parser(sub)
    from delta.engine.cli import add_parser as add_tasks_parser
    from delta.evals.cli import add_parser as add_eval_parser
    from delta.learning.cli import add_parser as add_learn_parser
    from delta.memory.cli import add_parser as add_memory_parser
    from delta.selfheal.cli import add_parser as add_undo_parser

    add_tasks_parser(sub)
    add_undo_parser(sub)
    add_eval_parser(sub)
    add_memory_parser(sub)
    add_learn_parser(sub)
    from delta.connectors.cli import add_parser as add_apps_parser

    add_apps_parser(sub)
    args = parser.parse_args()

    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "update":
        sys.exit(_update(stop=args.stop))
    if args.cmd == "secrets":
        sys.exit(_secrets(args.action, args.name))
    if args.cmd == "schedule":
        from delta.scheduler.cli import run_cli

        sys.exit(run_cli(load_settings(), args))
    if args.cmd == "tasks":
        from delta.engine.cli import run_cli as run_tasks_cli

        sys.exit(run_tasks_cli(load_settings(), args))
    if args.cmd == "undo":
        from delta.selfheal.cli import run_cli as run_undo_cli

        sys.exit(run_undo_cli(load_settings(), args))
    if args.cmd == "memory":
        from delta.memory.cli import run_cli as run_memory_cli

        sys.exit(run_memory_cli(load_settings(), args))
    if args.cmd == "learn":
        from delta.learning.cli import run_cli as run_learn_cli

        sys.exit(run_learn_cli(load_settings(), args))
    if args.cmd == "apps":
        from delta.connectors.cli import run_cli as run_apps_cli

        sys.exit(run_apps_cli(load_settings(), args))
    if args.cmd == "eval":
        from delta.evals.cli import run_cli as run_eval_cli

        sys.exit(run_eval_cli(args))
    if args.cmd == "captcha":
        from delta.tools.captcha import manage_allowlist

        sys.exit(manage_allowlist(load_settings(), args.action, args.host))
    if args.cmd == "audit":
        from delta.safety.audit import AuditLog, load_or_create_key

        report = AuditLog(load_settings().data_dir / "audit.jsonl", load_or_create_key(create=False)).verify_report()
        print(report.text())
        sys.exit(0 if report.ok else 1)
    if args.cmd == "app":
        # Run hidden from the Desktop shortcut: an error here would otherwise show NOTHING at all.
        try:
            from delta.daemon import launch_app

            sys.exit(launch_app())
        except Exception:
            import traceback

            detail = traceback.format_exc()
            log_file = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Delta" / "logs" / "launcher.log"
            with contextlib.suppress(OSError):
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{detail}")
            from delta.daemon import alert

            alert(f"Delta could not open.\n\n{detail.strip().splitlines()[-1]}\n\nDetails: {log_file}")
            sys.exit(1)
    if args.cmd == "run":
        from delta.daemon import alert, run

        try:
            sys.exit(asyncio.run(run(open_ui=args.open)))
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            if no_console:  # started from the shortcut (pythonw): no console to print to
                alert(f"Delta could not start:\n\n{e}\n\nRun 'delta doctor' in a terminal for details.")
                sys.exit(1)
            raise
    try:
        sys.exit(asyncio.run(_cli()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
