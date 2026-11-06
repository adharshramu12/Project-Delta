"""The extra browser actions (dropdowns, checkboxes, file upload, hover, waits, tabs, dialogs) in the user's OWN
browser, through Windows UI Automation on Delta's one window there (delta.tools.user_browser.UserBrowser). They have
the same names, arguments and risks as the private-browser tools (delta.tools.browser_actions), so the browser
stand-ins (delta.tools.browser_switch) send each call to whichever browser the task uses.

Differences a person would notice:
- elements are found by their label or visible text (the user's browser has no numbered page listing), so `ref` is
  refused with a plain message;
- tabs: only the tabs of Delta's own window are listed, switched or closed; the user's other windows are never
  touched;
- dialogs: browser_dialog answers the dialog that is showing NOW in Delta's window (the private browser decides the
  next one in advance); accepting one that deletes or sends asks like clicking Delete or Send;
- upload: Delta clicks the upload button, then puts the file's path into the browser's own Open dialog and presses
  Open. It asks first (the file leaves the PC when the form is sent) and the path is checked like any other path.
Every action checks afterwards that it took (the option shows, the box is ticked, the dialog closed) and fails with a
plain reason otherwise.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any

from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.browser_actions import CheckArgs, DialogArgs, SelectArgs, TabsArgs, UploadArgs, WaitArgs, _Target
from delta.tools.desktop import _pg, click_risk
from delta.tools.user_browser import CLICKABLE, UserBrowser, _UBTool

NO_REFS = ("the user's own browser has no numbered page listing: give the element's label or visible text instead "
           "of ref")
OK_WORDS = ("OK", "Leave", "Reload", "Yes")
CANCEL_WORDS = ("Cancel", "Stay", "No")


class ActionError(LookupError):
    """Plain words for the model (LookupError: _UBTool reports it as a failed step)."""


def _need_label(args: _Target) -> str:
    if args.ref is not None and not args.label.strip():
        raise ActionError(NO_REFS)
    return args.label.strip()


def _in_page(el: Any) -> bool:
    """True when the element belongs to the web page (inside a Document), not the browser's own UI."""
    with contextlib.suppress(Exception):
        info = el.element_info
        for _ in range(60):
            info = info.parent
            if info is None:
                return False
            if info.control_type == "Document":
                return True
    return False


class MoreActions:
    """The actions themselves, each run on the UI Automation thread (UserBrowser's rules: typing only in front)."""

    def __init__(self, ub: UserBrowser) -> None:
        self.ub = ub

    def _w(self) -> Any:
        w = self.ub._window()
        if w is None:
            raise ActionError("No browser window open yet: use browser_open first.")
        return w

    def _el(self, label: str, types: set[str]) -> Any:
        w = self._w()
        el = self.ub._find(w, label, types)
        if el is None:
            raise ActionError(f"Nothing called '{label}' on the page; use browser_read to see what is there.")
        return el

    # --- select ------------------------------------------------------------------------------------------
    def select(self, label: str, option: str) -> str:
        box = self._el(label, {"ComboBox", "List"})
        want = option.strip().lower()
        try:
            box.select(option)
        except Exception:
            with contextlib.suppress(Exception):
                box.expand()
            time.sleep(0.3)
            item = self.ub._find(self._w(), option, {"ListItem", "MenuItem"})
            if item is None:
                with contextlib.suppress(Exception):
                    box.collapse()
                raise ActionError(f"'{label}' has no option '{option}'") from None
            item.iface_selection_item.Select()
        time.sleep(0.3)
        shown = ""
        with contextlib.suppress(Exception):
            shown = str(box.iface_value.CurrentValue or "")
        if not shown:
            with contextlib.suppress(Exception):
                shown = str(box.selected_text() or "")
        if want not in shown.lower():
            raise ActionError(f"tried to choose '{option}' in '{label}' but it shows '{shown or 'nothing'}'")
        return f"chose '{shown}' in '{label}'"

    # --- check -------------------------------------------------------------------------------------------
    def check(self, label: str, checked: bool) -> str:
        box = self._el(label, {"CheckBox", "RadioButton"})
        kind = box.element_info.control_type

        def state() -> bool:
            if kind == "RadioButton":
                return bool(box.iface_selection_item.CurrentIsSelected)
            return int(box.iface_toggle.CurrentToggleState) == 1

        if state() != checked:
            if kind == "RadioButton":
                if not checked:
                    raise ActionError(f"'{label}' is a radio button: pick another option instead of unticking it")
                box.iface_selection_item.Select()
            else:
                box.iface_toggle.Toggle()
            time.sleep(0.3)
        if state() != checked:
            raise ActionError(f"'{label}' is still {'ticked' if not checked else 'not ticked'} after trying")
        return f"{'ticked' if checked else 'unticked'} '{label}'"

    # --- hover -------------------------------------------------------------------------------------------
    def hover(self, label: str) -> str:
        el = self._el(label, CLICKABLE)
        w = self._w()
        self.ub.ctl.guard()
        self.ub._front(w)
        r = el.rectangle()
        _pg().moveTo((r.left + r.right) // 2, (r.top + r.bottom) // 2, duration=0.2)
        self.ub.ctl.mark()
        time.sleep(0.5)
        return f"pointer over '{el.element_info.name or label}'"

    # --- wait --------------------------------------------------------------------------------------------
    def wait(self, until: str, text: str, timeout_s: float) -> str:
        w = self._w()
        start, end = time.monotonic(), time.monotonic() + timeout_s
        old = self.ub._url(w)
        want = text.strip().lower()

        def page_text() -> str:
            doc = self.ub._document(w)
            if doc is None:
                return ""
            return "\n".join(n for n, _t, _o, _e in self.ub._nodes(doc, 1500) if n).lower()

        last, same = "", 0
        while time.monotonic() < end:
            if until == "navigation" and self.ub._url(w) != old:
                break
            if until == "network_idle":
                url = self.ub._url(w)
                same = same + 1 if url == last and self.ub._document(w) is not None else 0
                last = url
                if same >= 3:
                    break
            if until == "text" and want in page_text():
                break
            if until == "text_gone" and want not in page_text():
                break
            time.sleep(0.4)
        else:
            what = {"navigation": "the page to change", "network_idle": "loading to finish",
                    "text": f"'{text}' to appear", "text_gone": f"'{text}' to go away"}[until]
            raise ActionError(f"waited {timeout_s:g} s for {what}; it did not happen. Read the page to see where it "
                              "stands.")
        return f"waited {time.monotonic() - start:.1f} s: {until.replace('_', ' ')} done"

    # --- tabs (Delta's own window only) ------------------------------------------------------------------
    def _tabs(self) -> list[Any]:
        return [t for t in self._w().descendants(control_type="TabItem") if not _in_page(t)]

    def tabs(self, action: str, index: int | None) -> str:
        tabs = self._tabs()
        if action == "list":
            rows = []
            for i, t in enumerate(tabs, 1):
                current = ""
                with contextlib.suppress(Exception):
                    current = " (current)" if t.iface_selection_item.CurrentIsSelected else ""
                rows.append(f"{i}. {(t.element_info.name or '(no title)')[:80]}{current}")
            return "\n".join(rows) or "no tabs in Delta's window"
        if index is None or not 1 <= index <= len(tabs):
            raise ActionError(f"give a tab number from 1 to {len(tabs)} (browser_tabs action=list)")
        tab = tabs[index - 1]
        if action == "close":
            if len(tabs) == 1:
                raise ActionError("that is the only tab of Delta's window; it stays open")
            close = next((b for b in tab.descendants(control_type="Button")
                          if (b.element_info.name or "").lower().startswith("close")), None)
            if close is None:
                raise ActionError("could not find that tab's close button")
            close.invoke()
            time.sleep(0.4)
            return f"closed tab {index} of Delta's window"
        tab.iface_selection_item.Select()
        time.sleep(0.4)
        return f"switched to tab {index} of Delta's window"

    # --- dialogs (the one showing now) ------------------------------------------------------------------
    def _dialog_buttons(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for b in self._w().descendants(control_type="Button"):
            name = (b.element_info.name or "").strip()
            if name in OK_WORDS + CANCEL_WORDS and name not in found and not _in_page(b):
                found[name] = b
        return found

    def dialog_text(self) -> str:
        buttons = self._dialog_buttons()
        if not buttons:
            return ""
        with contextlib.suppress(Exception):
            box = next(iter(buttons.values())).parent()
            texts = [t.element_info.name for t in box.descendants(control_type="Text") if t.element_info.name]
            return " ".join(texts)[:300]
        return ""

    def dialog(self, accept: bool) -> str:
        buttons = self._dialog_buttons()
        words = OK_WORDS if accept else CANCEL_WORDS
        btn = next((buttons[w] for w in words if w in buttons), None)
        if btn is None:
            raise ActionError("no dialog is showing in Delta's browser window now")
        said = self.dialog_text()
        btn.invoke()
        time.sleep(0.4)
        if self._dialog_buttons().get(btn.element_info.name) is not None:
            raise ActionError("the dialog is still showing after pressing its button")
        return f"{'accepted' if accept else 'cancelled'} the dialog" + (f" (\"{said[:80]}\")" if said else "")

    # --- upload ------------------------------------------------------------------------------------------
    def upload(self, label: str, path: str) -> str:
        from pywinauto import Desktop

        el = self._el(label, CLICKABLE | {"Edit"})
        w = self._w()
        before = {h.handle for h in Desktop(backend="uia").windows()}
        self.ub.ctl.guard()
        self.ub._front(w)
        try:
            el.invoke()
        except Exception:
            el.click_input()
        self.ub.ctl.mark()
        picker = None
        for _ in range(30):
            time.sleep(0.25)
            for win in Desktop(backend="uia").windows():
                with contextlib.suppress(Exception):
                    if (win.handle not in before and win.process_id() == w.process_id()) or (
                            win.element_info.class_name == "#32770" and win.process_id() == w.process_id()):
                        picker = win
                        break
            if picker is not None:
                break
        if picker is None:
            raise ActionError(f"'{label}' did not open a file picker")
        name_box = next((e for e in picker.descendants(control_type="Edit")
                         if "file name" in (e.element_info.name or "").lower()), None)
        open_btn = next((b for b in picker.descendants(control_type="Button")
                         if (b.element_info.name or "").strip().lower() in ("open", "&open")), None)
        if name_box is None or open_btn is None:
            with contextlib.suppress(Exception):
                picker.close()
            raise ActionError("the file picker did not look as expected; nothing was attached")
        name_box.iface_value.SetValue(path)
        open_btn.invoke()
        time.sleep(0.6)
        still_open = False
        with contextlib.suppress(Exception):
            still_open = bool(picker.is_visible())
        if still_open:
            with contextlib.suppress(Exception):
                picker.close()
            raise ActionError("the file picker did not accept the file; nothing was attached")
        return f"attached {Path(path).name} to '{label}' (not sent yet)"


class _MoreTool(_UBTool):
    def __init__(self, ub: UserBrowser, more: MoreActions) -> None:
        super().__init__(ub)
        self.more = more

    def summary(self, args: Any) -> str:
        what = f"'{getattr(args, 'label', '')}'" if getattr(args, "label", "") else ""
        return f"{self.name.removeprefix('browser_')} {what} in your browser on {self.ub.current_url or 'the page'}"

    async def _label_call(self, fn: Any, args: _Target, *rest: Any) -> ToolResult:
        try:
            label = _need_label(args)
        except ActionError as e:
            return ToolResult(False, str(e))
        return await self._do(fn, label, *rest)


class UBSelect(_MoreTool):
    name = "browser_select"
    description = ("Choose an option in a dropdown in the user's browser, by the dropdown's label and the option's "
                   "text. Checks the choice took.")
    risk = Risk.ACT
    Args = SelectArgs

    async def run(self, args: SelectArgs) -> ToolResult:
        return await self._label_call(self.more.select, args, args.option)


class UBCheck(_MoreTool):
    name = "browser_check"
    description = "Tick or untick a checkbox, or pick a radio button, in the user's browser by its label."
    risk = Risk.ACT
    Args = CheckArgs

    async def run(self, args: CheckArgs) -> ToolResult:
        return await self._label_call(self.more.check, args, args.checked)


class UBUpload(_MoreTool):
    name = "browser_upload"
    description = ("Attach a file from this PC with an upload button in the user's browser (asks first: the file "
                   "leaves the PC when the form is sent). Does not submit the form.")
    risk = Risk.EXTERNAL
    Args = UploadArgs

    def paths(self, args: UploadArgs) -> list[str]:
        return [args.path]

    def summary(self, args: UploadArgs) -> str:
        return f"attach {Path(args.path).name} to '{args.label}' in your browser on {self.ub.current_url or 'the page'}"

    async def run(self, args: UploadArgs) -> ToolResult:
        path = Path(os.path.expandvars(os.path.expanduser(args.path)))
        if not path.is_file():
            return ToolResult(False, f"there is no file at {path}")
        return await self._label_call(self.more.upload, args, str(path))


class UBHover(_MoreTool):
    name = "browser_hover"
    description = "Move the pointer over an element in the user's browser (opens hover menus), by its label."
    risk = Risk.ACT
    Args = _Target

    async def run(self, args: _Target) -> ToolResult:
        return await self._label_call(self.more.hover, args)


class UBWait(_MoreTool):
    name = "browser_wait"
    description = ("Wait (up to 30 s) in the user's browser for a navigation, loading to finish, or some text to "
                   "appear or go away.")
    risk = Risk.READ
    Args = WaitArgs

    async def run(self, args: WaitArgs) -> ToolResult:
        if args.until == "element":
            return ToolResult(False, "in the user's own browser wait for text instead (until=text): there are no "
                                     "numbered elements")
        if args.until in ("text", "text_gone") and not args.text.strip():
            return ToolResult(False, "give the text to wait for")
        return await self._do(self.more.wait, args.until, args.text, args.timeout_s)


class UBTabs(_MoreTool):
    name = "browser_tabs"
    description = ("List the tabs of Delta's window in the user's browser, switch to one (by its number) or close "
                   "one. The user's other windows are never touched.")
    risk = Risk.ACT
    Args = TabsArgs

    def risk_for(self, args: TabsArgs) -> Risk:
        return Risk.READ if args.action == "list" else Risk.ACT

    async def run(self, args: TabsArgs) -> ToolResult:
        if args.action == "list":
            return await self._do(self.more.tabs, args.action, args.index, read=False)
        return await self._do(self.more.tabs, args.action, args.index)


class UBDialog(_MoreTool):
    name = "browser_dialog"
    description = ("Answer the pop-up dialog (confirm, leave page) showing NOW in Delta's window of the user's "
                   "browser: accept=true presses OK, false presses Cancel.")
    risk = Risk.ACT
    Args = DialogArgs

    def _said(self) -> str:
        with contextlib.suppress(Exception):
            return str(self.ub.ctl.call_now(self.more.dialog_text, timeout=2.0))
        return ""

    def risk_for(self, args: DialogArgs) -> Risk:
        # Accepting "Delete this file?" is as risky as clicking Delete: judged on what the dialog says.
        return click_risk(self._said() or "OK") if args.accept else Risk.ACT

    def summary(self, args: DialogArgs) -> str:
        said = self._said()
        return f"{'accept' if args.accept else 'cancel'} the dialog" + (f" (\"{said[:80]}\")" if said else "")

    async def run(self, args: DialogArgs) -> ToolResult:
        return await self._do(self.more.dialog, args.accept)


def user_browser_more_tools(ub: UserBrowser) -> list[Tool[Any]]:
    more = MoreActions(ub)
    return [UBSelect(ub, more), UBCheck(ub, more), UBUpload(ub, more), UBHover(ub, more), UBWait(ub, more),
            UBTabs(ub, more), UBDialog(ub, more)]


__all__ = ["MoreActions", "user_browser_more_tools"]
