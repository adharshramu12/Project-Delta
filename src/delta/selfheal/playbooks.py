"""Recovery playbooks: for each failure type, the fixed steps Delta tries before anything else.

Deterministic first: none of these asks an AI model. Each action is carried out by the Healer through the
normal ToolExecutor gate, so policy, approvals and Remote Guard decide every step exactly as they would for
the model. A playbook either retries the failed step (only when that is safe, see `retry_safe`), stops the
task with a plain question or report for the user, or just tells the model what was tried.

Actions:
  settle          wait for the page or app to finish what it is doing (bounded)
  resnapshot      read the page (browser_read) or window (read_window) again: fresh element numbers
  relocate        swap a stale element number for the element's name, so the retry finds it wherever it moved
  dismiss_popup   close a known harmless banner (cookie notice, "not now" prompt) that blocks the click
  reopen          after the watchdog restarted Delta's own browser, open the page the task was on again
  relaunch        the browser window the task was using is gone (closed, crashed): open its page again
  backoff         wait before asking a busy AI service again
  captcha_policy  hand over to the existing captcha policy (solve_captcha decides; most go to the user)
  ask_user        stop and ask the user (sign-in walls: Delta never types passwords)
  report          stop and tell the user what Windows refused
"""

from __future__ import annotations

from dataclasses import dataclass

from delta.selfheal.taxonomy import Failure

F = Failure


@dataclass(frozen=True)
class Playbook:
    failure: Failure
    actions: tuple[str, ...]
    retry: bool              # retry the failed step afterwards (only if retry_safe allows it)
    line_ok: str             # timeline line when the retry worked
    line_tried: str          # timeline line when it did not (or no retry was allowed)
    hint: str = ""           # added to what the model reads when the step is still failed
    stop: bool = False       # end the task here with a question / report for the user


PLAYBOOKS: dict[Failure, Playbook] = {
    F.STALE_ELEMENT: Playbook(F.STALE_ELEMENT, ("resnapshot", "relocate"), True,
                              "Button moved, found it again.",
                              "The page changed under me; read it again.",
                              "The page changed: use the element numbers from the newest listing."),
    F.ELEMENT_NOT_FOUND: Playbook(F.ELEMENT_NOT_FOUND, ("settle", "relaunch", "resnapshot"), True,
                                  "Could not see it at first; waited for the page and found it.",
                                  "Could not find it, even after the page settled.",
                                  "It is not on the screen right now: check the newest listing for its real name, "
                                  "scroll, or open the right page first."),
    F.TIMEOUT: Playbook(F.TIMEOUT, ("settle",), True,
                        "The page was slow; waited for it and tried again.",
                        "It was too slow; waited once, then left it to you or the AI.",
                        "It timed out. Check the current state before trying anything again."),
    F.POPUP_OR_DIALOG: Playbook(F.POPUP_OR_DIALOG, ("dismiss_popup",), True,
                                "Closed a cookie banner that was in the way.",
                                "Something on the page was in the way; I did not close it on my own.",
                                "A dialog or banner is covering the page. Close it only if it is harmless; never "
                                "accept payments, permissions or sign-in prompts for the user."),
    F.SIGN_IN_WALL: Playbook(F.SIGN_IN_WALL, ("ask_user",), False, "",
                             "This site wants you to sign in; asked you to do it.", stop=True),
    F.CAPTCHA: Playbook(F.CAPTCHA, ("captcha_policy",), False, "",
                        "The site showed a captcha; handed it to the captcha rules.",
                        "The page shows a captcha. Use solve_captcha if it is available; it follows the user's captcha "
                        "rules (reCAPTCHA, hCaptcha and Cloudflare Turnstile always go to the user). Never try to get "
                        "around a captcha another way. Otherwise ask the user with NEED_USER."),
    F.NAVIGATION_ERROR: Playbook(F.NAVIGATION_ERROR, ("settle",), True,
                                 "The page did not load the first time; it did on the second try.",
                                 "The page would not load.",
                                 "The address did not load. Check it, or try another source."),
    F.APP_NOT_RESPONDING: Playbook(F.APP_NOT_RESPONDING, ("settle",), True,
                                   "The app was busy; waited and tried again.",
                                   "The app was not responding.",
                                   "The app is not responding. Wait, or check it with read_window/list_windows."),
    F.APP_CRASHED: Playbook(F.APP_CRASHED, ("reopen",), True,
                            "The browser stopped; restarted it and carried on.",
                            "The app or browser stopped.",
                            "The app or browser stopped. Check what is open now before going on."),
    F.PERMISSION_DENIED: Playbook(F.PERMISSION_DENIED, ("report",), False, "",
                                  "Windows did not allow this; stopped and told you.", stop=True),
    F.FILE_LOCKED: Playbook(F.FILE_LOCKED, ("settle",), True,
                            "The file was in use; waited a moment and tried again.",
                            "The file is open in another program.",
                            ("The file is open in another program. Ask the user to close it (NEED_USER), "
                             "then try again.")),
    F.RATE_LIMIT: Playbook(F.RATE_LIMIT, ("backoff",), True,
                           "The AI service was busy; waited and tried again.",
                           "The AI service stayed busy."),
    F.MODEL_ERROR: Playbook(F.MODEL_ERROR, (), True, "Fixed a problem with the AI's reply and carried on.",
                            "The AI's reply could not be used."),
    F.BUDGET: Playbook(F.BUDGET, (), False, "", "Reached a usage limit; stopped to ask you.", stop=True),
    F.UNKNOWN: Playbook(F.UNKNOWN, (), False, "", ""),
}

# Failures that prove the step did nothing: the tool checked for its target (an element number, a name) and
# returned before acting. Only these allow a retry of a step that would change something.
PRE_ACTION = frozenset({F.STALE_ELEMENT, F.ELEMENT_NOT_FOUND})


def playbook_for(failure: Failure) -> Playbook:
    return PLAYBOOKS.get(failure, PLAYBOOKS[F.UNKNOWN])
