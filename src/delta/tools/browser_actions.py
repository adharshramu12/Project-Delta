"""More hands for Delta's private browser (Playwright): dropdowns, checkboxes, file upload, hover, waits, tabs and
dialogs. Each action finds its element by ref (the [number] from the page listing) or by its label, retries what is
worth retrying, checks afterwards that the page really is how the action meant to leave it (the option is selected,
the box is ticked, the file is attached), and fails with a plain reason and what to try next.

Safety is the same as for the other browser tools: every call goes through ToolExecutor (policy, approvals, Remote
Guard, ExfilGuard). Uploading a file sends it off the PC, so it is EXTERNAL (asks) and the file path is checked like
any other path (Delta's data folder and secrets files are refused). Accepting a dialog that deletes or sends asks
like clicking a Delete or Send button. The user's own browser has the same tools, by label instead of ref
(delta.tools.user_browser_actions); the stand-ins (delta.tools.browser_switch) pick the one the task uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.browser import (
    ActionFailed,
    BrowserManager,
    StaleRef,
    _BrowserTool,
    _observed,
    _ref_target,
    explain,
    page_state,
    settle,
)
from delta.tools.desktop import click_risk

MAX_WAIT_S = 30.0


class _Target(BaseModel):
    ref: int | None = Field(default=None, description="Element number from the latest page listing, e.g. 7 for [7]")
    label: str = Field(default="", description="Its label or visible text, when there is no number")

    @model_validator(mode="after")
    def _one_target(self) -> _Target:
        if self.ref is None and not self.label.strip():
            raise ValueError("give ref (the [number] from the page listing) or label")
        return self


async def _find(page: Any, mgr: BrowserManager, args: _Target, roles: tuple[str, ...]) -> tuple[Any, str]:
    """(live element, its name) by ref or by label. StaleRef / LookupError with a plain message when not found."""
    if args.ref is not None:
        loc, seen, live = await _ref_target(page, mgr, args.ref)
        return loc, live or seen.name
    want = args.label.strip()
    # In order of how sure the match is: the field a label names, an element with that role and name, any text.
    tries = [page.get_by_label(want), *(page.get_by_role(role, name=want) for role in roles),
             page.get_by_text(want, exact=False)]
    for cands in tries:
        for i in range(min(await cands.count(), 10)):
            item = cands.nth(i)
            with contextlib.suppress(Exception):
                if await item.is_visible():
                    return item, want
    raise LookupError(f"nothing visible called '{want}'; use browser_read to see the page")


def _label(mgr: BrowserManager, args: _Target) -> str:
    if args.ref is None:
        return args.label
    seen = mgr.refs.get(args.ref)
    return seen.name if seen else ""


class _TargetTool[A: _Target](_BrowserTool[A]):
    roles: tuple[str, ...] = ()

    def summary(self, args: A) -> str:
        what = f"[{args.ref}] '{_label(self.mgr, args)}'" if args.ref is not None else f"'{args.label}'"
        return f"{self.name.removeprefix('browser_')} {what} on {self.mgr.current_url or 'the page'}"

    async def _locate(self, page: Any, args: A) -> tuple[Any, str] | ToolResult:
        try:
            return await _find(page, self.mgr, args, self.roles)
        except (StaleRef, LookupError) as e:
            return ToolResult(False, str(e))


# ---------------------------------------------------------------- select

class SelectArgs(_Target):
    option: str = Field(description="The option to choose, as shown (its text) or its value")


class BrowserSelect(_TargetTool[SelectArgs]):
    name = "browser_select"
    description = ("Choose an option in a dropdown (a <select> or a custom list box): by ref or label, and the "
                   "option's text. Checks the choice took.")
    risk = Risk.ACT
    Args = SelectArgs
    roles = ("combobox", "listbox")

    async def run(self, args: SelectArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            found = await self._locate(page, args)
            if isinstance(found, ToolResult):
                return found
            box, name = found
            before = await page_state(page)
            tag = str(await box.evaluate("e => e.tagName.toLowerCase()"))
            if tag == "select":
                try:
                    await box.select_option(label=args.option, timeout=5000)
                except Exception:
                    try:
                        await box.select_option(value=args.option, timeout=3000)
                    except Exception as e:
                        options = await box.evaluate("e => Array.from(e.options).map(o => o.text.trim()).slice(0, 15)")
                        raise ActionFailed(f"'{name}' has no option '{args.option}'. Its options: "
                                           f"{', '.join(map(str, options))}") from e
                chosen = str(await box.evaluate("e => (e.options[e.selectedIndex] || {text: ''}).text.trim()"))
                value = str(await box.evaluate("e => e.value"))
                if args.option.strip().lower() not in (chosen.lower(), value.lower()):
                    raise ActionFailed(f"tried to choose '{args.option}' in '{name}' but it shows '{chosen}'")
            else:  # a custom list box: open it, then click the option
                from delta.tools.browser import robust_click

                await robust_click(page, box, what=f"'{name}'")
                await settle(page, idle_ms=600)
                option = page.get_by_role("option", name=args.option).or_(page.get_by_text(args.option, exact=True))
                if not await option.count():
                    raise ActionFailed(f"opened '{name}' but found no option '{args.option}'")
                await robust_click(page, option.first, what=f"option '{args.option}'")
                chosen = args.option
            await settle(page)
            return await _observed(page, self.mgr, f"chose '{chosen}' in '{name}'", before=before)

        return await self._guarded(go)


# ---------------------------------------------------------------- checkbox / radio / switch

class CheckArgs(_Target):
    checked: bool = Field(default=True, description="true = tick / turn on / pick; false = untick / turn off")


class BrowserCheck(_TargetTool[CheckArgs]):
    name = "browser_check"
    description = "Tick or untick a checkbox, turn a switch on or off, or pick a radio button. Checks the result."
    risk = Risk.ACT
    Args = CheckArgs
    roles = ("checkbox", "radio", "switch")

    async def run(self, args: CheckArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            found = await self._locate(page, args)
            if isinstance(found, ToolResult):
                return found
            box, name = found
            before = await page_state(page)
            for attempt in range(2):
                try:
                    await box.set_checked(args.checked, timeout=6000)
                    break
                except Exception as e:
                    if attempt:
                        raise ActionFailed(explain(e, f"'{name}'")) from e
                    await settle(page, idle_ms=600)
            now = await box.is_checked()
            if now != args.checked:
                raise ActionFailed(f"'{name}' is still {'ticked' if now else 'not ticked'} after trying")
            await settle(page, idle_ms=800)
            word = "ticked" if args.checked else "unticked"
            return await _observed(page, self.mgr, f"{word} '{name}'", before=before)

        return await self._guarded(go)


# ---------------------------------------------------------------- file upload

class UploadArgs(_Target):
    path: str = Field(description="Full path of the file on this PC to attach")


class BrowserUpload(_TargetTool[UploadArgs]):
    name = "browser_upload"
    description = ("Attach a file from this PC to a file field or upload button on the page (asks first: the "
                   "file leaves the PC when the form is sent). Does not submit the form.")
    risk = Risk.EXTERNAL
    Args = UploadArgs
    roles = ("button",)

    def paths(self, args: UploadArgs) -> list[str]:
        return [args.path]

    def summary(self, args: UploadArgs) -> str:
        return f"attach {Path(args.path).name} to '{_label(self.mgr, args)}' on {self.mgr.current_url or 'the page'}"

    async def run(self, args: UploadArgs) -> ToolResult:
        async def go() -> ToolResult:
            path = Path(os.path.expandvars(os.path.expanduser(args.path)))
            if not path.is_file():
                return ToolResult(False, f"there is no file at {path}")
            page = await self.mgr.page()
            found = await self._locate(page, args)
            if isinstance(found, ToolResult):
                return found
            box, name = found
            before = await page_state(page)
            kind = str(await box.evaluate("e => (e.tagName + ':' + (e.type || '')).toLowerCase()"))
            if kind == "input:file":
                await box.set_input_files(str(path), timeout=8000)
                count = int(await box.evaluate("e => e.files ? e.files.length : 0") or 0)
                if count < 1:
                    raise ActionFailed(f"'{name}' still has no file attached")
            else:  # a styled button that opens the file picker
                try:
                    async with page.expect_file_chooser(timeout=6000) as chooser:
                        await box.click(timeout=5000)
                    await (await chooser.value).set_files(str(path))
                except Exception as e:
                    raise ActionFailed(f"'{name}' did not open a file picker ({explain(e, 'it')})") from e
            await settle(page)
            return await _observed(page, self.mgr, f"attached {path.name} to '{name}' (not sent yet)", before=before)

        return await self._guarded(go)


# ---------------------------------------------------------------- hover

class BrowserHover(_TargetTool[_Target]):
    name = "browser_hover"
    description = "Move the pointer over an element (opens hover menus and tooltips), by ref or label."
    risk = Risk.ACT
    Args = _Target
    roles = ("link", "button", "menuitem")

    async def run(self, args: _Target) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            found = await self._locate(page, args)
            if isinstance(found, ToolResult):
                return found
            box, name = found
            before = await page_state(page)
            try:
                await box.hover(timeout=6000)
            except Exception as e:
                raise ActionFailed(explain(e, f"'{name}'")) from e
            await settle(page, idle_ms=800)
            return await _observed(page, self.mgr, f"pointer over '{name}'", before=before)

        return await self._guarded(go)


# ---------------------------------------------------------------- waits

class WaitArgs(BaseModel):
    until: Literal["navigation", "network_idle", "text", "text_gone", "element"] = Field(
        description="navigation = the address changes; network_idle = loading stops; text = this text shows; "
                    "text_gone = this text goes away (e.g. 'Loading'); element = element ref shows")
    text: str = Field(default="", description="For text / text_gone")
    ref: int | None = Field(default=None, description="For element")
    timeout_s: float = Field(default=15, gt=0, le=MAX_WAIT_S)


class BrowserWait(_BrowserTool[WaitArgs]):
    name = "browser_wait"
    description = ("Wait (up to 30 s) for the page: a navigation, loading to finish, some text to appear or go "
                   "away, or an element to show. Use after an action that starts slow loading.")
    risk = Risk.READ
    Args = WaitArgs

    async def run(self, args: WaitArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            ms = int(args.timeout_s * 1000)
            started, url = time.monotonic(), page.url
            try:
                if args.until == "navigation":
                    await page.wait_for_url(lambda u: u != url, timeout=ms)
                    await settle(page)
                elif args.until == "network_idle":
                    await settle(page, idle_ms=ms)
                elif args.until in ("text", "text_gone"):
                    if not args.text.strip():
                        return ToolResult(False, "give the text to wait for")
                    state = "visible" if args.until == "text" else "hidden"
                    await page.get_by_text(args.text, exact=False).first.wait_for(state=state, timeout=ms)
                else:
                    if args.ref is None:
                        return ToolResult(False, "give ref (the element to wait for)")
                    _, _, _ = await _ref_target(page, self.mgr, args.ref)
                    from delta.tools.browser import locate_ref

                    loc = await locate_ref(page, args.ref)
                    if loc is None:
                        raise StaleRef(f"element [{args.ref}] is gone")
                    await loc.wait_for(state="visible", timeout=ms)
            except StaleRef as e:
                return ToolResult(False, str(e))
            except Exception as e:
                what = {"navigation": "the page to change", "network_idle": "loading to finish",
                        "text": f"'{args.text}' to appear", "text_gone": f"'{args.text}' to go away",
                        "element": f"element [{args.ref}] to show"}[args.until]
                if "timeout" in str(e).lower():
                    return ToolResult(False, f"waited {args.timeout_s:g} s for {what}; it did not happen. Read the "
                                             "page to see where it stands.")
                return ToolResult(False, explain(e, "the page"))
            took = time.monotonic() - started
            return await _observed(page, self.mgr, f"waited {took:.1f} s: {args.until.replace('_', ' ')} done")

        return await self._guarded(go)


# ---------------------------------------------------------------- tabs

class TabsArgs(BaseModel):
    action: Literal["list", "switch", "close"] = "list"
    index: int | None = Field(default=None, ge=1, description="Tab number from the list (switch, close)")


class BrowserTabs(_BrowserTool[TabsArgs]):
    name = "browser_tabs"
    description = ("List the tabs of Delta's browser, switch to one (by its number) or close one. A tab a click "
                   "opened is used automatically.")
    risk = Risk.ACT
    Args = TabsArgs

    def risk_for(self, args: TabsArgs) -> Risk:
        return Risk.READ if args.action == "list" else Risk.ACT

    async def run(self, args: TabsArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            pages = [p for p in page.context.pages]
            if args.action == "list":
                rows = []
                for i, p in enumerate(pages, 1):
                    title = ""
                    with contextlib.suppress(Exception):
                        title = await asyncio.wait_for(p.title(), 2)
                    mark = " (current)" if p is page else ""
                    rows.append(f"{i}. {title[:80] or '(no title)'} | {p.url[:120]}{mark}")
                return ToolResult(True, "\n".join(rows) or "no tabs", {"tabs": len(pages)})
            if args.index is None or not 1 <= args.index <= len(pages):
                return ToolResult(False, f"give a tab number from 1 to {len(pages)} (browser_tabs action=list)")
            chosen = pages[args.index - 1]
            if args.action == "close":
                if len(pages) == 1:
                    return ToolResult(False, "that is the only tab; it stays open")
                await chosen.close()
                rest = [p for p in page.context.pages if not p.is_closed()]
                self.mgr.active = rest[-1] if chosen is page or self.mgr.active is chosen else page
                now = self.mgr.active
                return await _observed(now, self.mgr, f"closed tab {args.index}; now in {now.url}", force_full=True)
            self.mgr.active = chosen
            with contextlib.suppress(Exception):
                await chosen.bring_to_front()
            await settle(chosen, idle_ms=800)
            return await _observed(chosen, self.mgr, f"switched to tab {args.index}", force_full=True)

        return await self._guarded(go)


# ---------------------------------------------------------------- dialogs

class DialogArgs(BaseModel):
    accept: bool = Field(description="true = press OK on the NEXT confirm/prompt dialog; false = Cancel (default)")


class BrowserDialog(_BrowserTool[DialogArgs]):
    name = "browser_dialog"
    description = ("Decide the NEXT pop-up dialog (confirm, prompt, leave page): accept=true presses OK, then "
                   "repeat the action that showed it. By default dialogs are cancelled and their text reported.")
    risk = Risk.ACT
    Args = DialogArgs

    def risk_for(self, args: DialogArgs) -> Risk:
        # Accepting "Delete this file?" is as risky as clicking Delete: judged on what the last dialog said.
        return click_risk(self.mgr.dialog_text or "OK") if args.accept else Risk.ACT

    def summary(self, args: DialogArgs) -> str:
        said = f" (\"{self.mgr.dialog_text[:80]}\")" if self.mgr.dialog_text else ""
        return f"{'accept' if args.accept else 'cancel'} the next dialog{said} on {self.mgr.current_url or 'the page'}"

    async def run(self, args: DialogArgs) -> ToolResult:
        self.mgr.next_dialog = "accept" if args.accept else "dismiss"
        return ToolResult(True, f"The next dialog will be {'accepted (OK)' if args.accept else 'cancelled'}. Now "
                                "repeat the action that shows it.", {"next_dialog": self.mgr.next_dialog})


def more_browser_tools(mgr: BrowserManager) -> list[Tool[Any]]:
    return [BrowserSelect(mgr), BrowserCheck(mgr), BrowserUpload(mgr), BrowserHover(mgr), BrowserWait(mgr),
            BrowserTabs(mgr), BrowserDialog(mgr)]
