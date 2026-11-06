"""Delta command line.

  delta              interactive terminal (same as `delta cli`)
  delta doctor       check models, keys, GPU, audit log
  delta secrets set|delete|list [name]
  delta audit verify
  delta captcha allow|remove|list [host]
  delta schedule add|list|pause|resume|remove|run [text | id]
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


def _update() -> int:
    """Newest version over the installed one (data untouched): the installer again, from GitHub."""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        print("This is a developer checkout: update it with 'git pull', then 'uv sync --extra all'.")
        return 1
    script = root / "scripts" / "install.ps1"
    return subprocess.call(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                            "-FromGitHub", "-InstallDir", str(root), "-NoLaunch"])


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
    from delta.safety.audit import AuditLog

    audit_ok = AuditLog(s.data_dir / "audit.jsonl").verify()
    print(f"Audit log chain: {'intact' if audit_ok else 'TAMPERED'}")
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
    sub.add_parser("update", help="update Delta to the newest version (keeps your data)")
    from delta.scheduler.cli import add_parser as add_schedule_parser

    add_schedule_parser(sub)
    args = parser.parse_args()

    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "update":
        sys.exit(_update())
    if args.cmd == "secrets":
        sys.exit(_secrets(args.action, args.name))
    if args.cmd == "schedule":
        from delta.scheduler.cli import run_cli

        sys.exit(run_cli(load_settings(), args))
    if args.cmd == "captcha":
        from delta.tools.captcha import manage_allowlist

        sys.exit(manage_allowlist(load_settings(), args.action, args.host))
    if args.cmd == "audit":
        from delta.safety.audit import AuditLog

        good = AuditLog(load_settings().data_dir / "audit.jsonl").verify()
        print("audit chain intact" if good else "AUDIT CHAIN BROKEN: log was modified")
        sys.exit(0 if good else 1)
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
