"""`delta logins add|list|remove|test|allow|captcha`: saved site logins from a terminal on the PC.

`delta logins captcha NAME --on|--off` is the same switch as Settings > Logins "This site has a simple text captcha,
let Delta read it": for that login's exact origin it does what `delta captcha allow <host>` does for a host. It is ON
by default for every login with a password (Delta reads the captcha itself: Windows OCR first, then at most one small
picture to your AI model; after 3 tries it sends you the picture). `--off` makes Delta ask you every time.

The password is typed without being shown (never an argument, so it is not in the shell history or the process
list) and never printed. Works whether or not Delta is running: the running service reads the same sealed files.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
from collections.abc import Callable
from typing import Any

from delta.config import Settings
from delta.logins.store import PERMISSION_WORDS, LoginError, LoginStore


def add_parser(sub: Any) -> None:
    p = sub.add_parser("logins", help="saved site logins (college ERP, LMS...): add, list, remove, test, allow, "
                                      "captcha")
    p.add_argument("action", choices=["add", "list", "remove", "test", "allow", "captcha"])
    p.add_argument("name", nargs="?", help="the login's name, e.g. 'College ERP'")
    p.add_argument("--site", default="", help="add: the site's address, e.g. https://erp.college.edu")
    p.add_argument("--login-url", default="", help="add: the sign-in page, if it is not the home page")
    p.add_argument("--username", default=None, help="add: the user name (asked for when not given)")
    p.add_argument("--allow-http", action="store_true", help="add: the site only has plain http (not safe)")
    p.add_argument("--replace", action="store_true", help="add: replace a login with the same name")
    p.add_argument("--always", action="store_true", help="add / allow: use it without asking, on this site only")
    p.add_argument("--ask", action="store_true", help="allow: ask every time (the default)")
    p.add_argument("--simple-captcha", action="store_true",
                   help="add: let Delta read this site's simple text captcha (already the default with a password)")
    p.add_argument("--no-simple-captcha", action="store_true",
                   help="add: never read this site's captcha automatically; send me the picture instead")
    p.add_argument("--on", action="store_true",
                   help="captcha: Delta reads this site's simple text captcha itself, no approval (the default for "
                        "a login with a password; Windows OCR first, 3 tries, then it sends you the picture)")
    p.add_argument("--off", action="store_true",
                   help="captcha: Delta sends me this site's captcha picture to type every time")


def store_for(settings: Settings) -> LoginStore:
    return LoginStore(settings.data_dir / "logins")


def run_cli(settings: Settings, args: argparse.Namespace, ask: Callable[[str], str] = input,
            secret: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print,
            store: LoginStore | None = None) -> int:
    s = store or store_for(settings)
    action = args.action
    if action == "list":
        items = s.list()
        if not items:
            out("No saved logins. Add one: delta logins add \"College ERP\" --site https://erp.college.edu")
            return 0
        for x in items:
            used = f", last used {x.last_used} ({x.last_outcome})" if x.last_used else ""
            own = f", allowed in your own {' and '.join(sorted(x.own_browser))}" if x.own_browser else ""
            pw = "" if x.password else ", NO password saved"
            cap = (", Delta reads its simple captcha" + (" (default)" if x.captcha_choice == "" else "")
                   if x.simple_captcha else ", Delta sends you its captcha")
            out(f"{x.name}: {x.origin} as {x.username or '(no user name)'}{pw}; {PERMISSION_WORDS[x.permission]}{own}"
                f"{cap}{used}")
        return 0
    if not args.name:
        out(f"give the login's name, e.g. delta logins {action} \"College ERP\"")
        return 2
    if action == "remove":
        gone = s.remove(args.name)
        out(f"Removed {gone.name}." if gone else f"No saved login called '{args.name}'.")
        return 0 if gone else 1
    if action == "allow":
        perm = "always" if args.always else "ask"
        try:
            login = s.set_permission(args.name, perm)
        except LoginError as e:
            out(str(e))
            return 1
        out(f"{login.name}: {PERMISSION_WORDS[perm]}.")
        return 0
    if action == "captcha":
        on, off = bool(getattr(args, "on", False)), bool(getattr(args, "off", False))
        if on == off:
            out(f"say --on or --off, e.g. delta logins captcha \"{args.name}\" --on")
            return 2
        try:
            login = s.set_simple_captcha(args.name, on)
        except LoginError as e:
            out(str(e))
            return 1
        out(f"{login.name}: Delta reads the simple text captcha on {login.origin} itself (Windows OCR on this PC "
            "first, at most 3 tries, then it sends you the picture). Use it only for your own account."
            if login.simple_captcha else
            f"{login.name}: Delta sends you the captcha picture on {login.origin} and waits for your reply.")
        return 0
    if action == "add":
        if not s.available():
            out("Delta cannot save passwords here: Windows DPAPI is not available. Repair Delta (run the installer "
                "again or `delta update`). Nothing was saved.")
            return 1
        site = args.site or ask("Site address (e.g. https://erp.college.edu): ").strip()
        username = args.username if args.username is not None else ask("User name (empty if none): ").strip()
        password = secret("Password (not shown; empty if none): ")
        try:
            login = s.add(args.name, site, username, password, login_url=args.login_url,
                          permission="always" if args.always else "ask", allow_http=args.allow_http,
                          replace=args.replace,
                          simple_captcha=True if getattr(args, "simple_captcha", False) else
                          False if getattr(args, "no_simple_captcha", False) else None)
        except LoginError as e:
            out(f"Not saved: {e}")
            return 1
        out(f"Saved {login.name} for {login.origin}, encrypted with Windows DPAPI. "
            f"{PERMISSION_WORDS[login.permission]}. Delta types it only on that exact site.")
        if not login.password:
            out("Note: no password was saved (only a user name). Delta cannot sign in to a site that asks for one; "
                "add it again with --replace and type the password.")
        if not login.site.secure:
            out("Warning: this site uses plain http. Anyone on the same network can read what is sent to it.")
        return 0
    return asyncio.run(_test(settings, s, args.name, out))


async def _test(settings: Settings, store: LoginStore, name: str, out: Callable[[str], None]) -> int:
    """Sign in once in Delta's private browser, as asked from this terminal (that is the permission)."""
    from delta.logins.service import LoginService
    from delta.safety.audit import AuditLog, load_or_create_key
    from delta.tools.browser import BrowserManager, settle

    login = store.get(name)
    if login is None:
        out(f"No saved login called '{name}'.")
        return 1
    audit = AuditLog(settings.data_dir / "audit.jsonl", key_loader=load_or_create_key)
    mgr = BrowserManager(settings.data_dir)
    mgr.engine = settings.browser.private_engine
    service = LoginService(store, audit, approvals=None, settle=settle)

    async def in_terminal(kind: str, host: str, text: str) -> bool:
        answer = await asyncio.to_thread(input, f"{text}. Press Enter when done (or type n to stop): ")
        return answer.strip().lower() not in ("n", "no")

    service.handoff = in_terminal  # type: ignore[method-assign]
    try:
        page = await mgr.page()
        out(f"Signing in to {login.name} ({login.origin}) in Delta's private browser...")
        result = await service.sign_in(login.id, page, ask=False)
    except Exception as e:
        out(f"Could not test: {type(e).__name__}: {str(e)[:200]}")
        return 1
    finally:
        await mgr.close()
    out(result.message)
    return 0 if result.ok else 1
