"""Delta command line.

  delta              interactive terminal (same as `delta cli`)
  delta doctor       check models, keys, GPU, audit log
  delta secrets set|delete|list [name]
  delta audit verify
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

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


def _doctor() -> int:
    from delta.system.governor import SystemSensor

    s = load_settings()
    ok = True
    print(f"Delta {__version__}\nconfig: {s.config_dir}\ndata:   {s.data_dir}\n")
    print("Model providers:")
    for name, p in s.llm.providers.items():
        if p.local:
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
    if os.name == "nt":
        os.system("")  # enable ANSI colours in classic consoles
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="delta", description="Delta personal AI agent")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("cli", help="interactive terminal")
    sub.add_parser("doctor", help="check setup")
    sp = sub.add_parser("secrets", help="manage API keys and tokens")
    sp.add_argument("action", choices=["set", "delete", "list"])
    sp.add_argument("name", nargs="?")
    ap = sub.add_parser("audit", help="audit log tools")
    ap.add_argument("action", choices=["verify"])
    args = parser.parse_args()

    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "secrets":
        sys.exit(_secrets(args.action, args.name))
    if args.cmd == "audit":
        from delta.safety.audit import AuditLog

        good = AuditLog(load_settings().data_dir / "audit.jsonl").verify()
        print("audit chain intact" if good else "AUDIT CHAIN BROKEN: log was modified")
        sys.exit(0 if good else 1)
    try:
        sys.exit(asyncio.run(_cli()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
