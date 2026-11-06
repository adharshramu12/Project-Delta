"""Saved site logins (a college ERP, an LMS...): stored sealed with Windows DPAPI, bound to one exact origin, typed by
Delta itself only on that origin, never seen by the model. See docs/LOGINS.md.

    origin.py   exact origins and the anti-phishing checks (lookalike hosts, http downgrade, other ports, frames)
    store.py    the sealed store (one DPAPI-sealed file per login), refused when DPAPI is not available
    signin.py   the sign-in engine on a Playwright page: checks, typing, masking, one-time code and captcha hand-offs
    own_browser.py  the same rules in the user's own browser, plus tab binding and a fill check (driver interface)
    own_uia.py  that driver for the user's everyday Chrome or Edge, through Windows UI Automation
    mask.py     password boxes painted over in screenshots of the user's own browser
    service.py  permission (ask / for this task / always, and the one-time own-browser consent), hand-offs, audit
    tools.py    sign_in(site) and list_logins for the model
    cli.py      `delta logins add|list|remove|test|allow`
"""
